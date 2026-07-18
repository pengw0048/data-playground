"""Product consumer for Source -> Select(checkpoint) -> Select(*) -> Write fan-out."""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import HTTPException

from hub import bounded_fanout_tasks as bft
from hub import metadb
from hub.deps import Deps
from hub.models import Graph
from hub.routers.runs import _bounded_fanout_write_shape, start_run


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('bounded-fanout-write') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _source_table(rows: int) -> pa.Table:
    return pa.table({
        "value": pa.array(list(range(rows)), pa.int64()),
        "label": pa.array([f"r{i}" for i in range(rows)], pa.string()),
    })


def _canvas_graph(tmp_path, canvas_id: str, deps, *, rows: int = 3):
    lance = pytest.importorskip("lance")
    source = tmp_path / f"source-{rows}.lance"
    lance.write_dataset(_source_table(rows), str(source))
    deps.catalog._add(name=f"source-{rows}", uri=str(source), strict_probe=True)
    return Graph.model_validate({
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": str(source)}}},
            {"id": "checkpoint", "type": "select", "data": {
                "title": "checkpoint", "config": {"select": "*", "checkpoint": True}}},
            {"id": "identity", "type": "select", "data": {
                "title": "identity", "config": {"select": "*"}}},
            {"id": "write", "type": "write", "data": {"title": "final", "config": {
                "filename": f"final-{rows}.parquet", "writeMode": "overwrite"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "checkpoint",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e2", "source": "checkpoint", "target": "identity",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e3", "source": "identity", "target": "write",
             "sourceHandle": "out", "targetHandle": "in"},
        ],
    })


def _await_status(task_id, statuses=("done", "failed", "cancelled"), timeout=60):
    deadline = time.time() + timeout
    observed = None
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed and observed["status"] in statuses:
            return observed
        time.sleep(0.1)
    return observed


def test_exact_route_rejects_non_matching_shapes():
    # Wrong node count with checkpoint falls through / is rejected by linear.
    graph = Graph.model_validate({
        "id": "g", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "/x"}}},
            {"id": "checkpoint", "type": "select", "data": {
                "config": {"select": "*", "checkpoint": True}}},
            {"id": "identity", "type": "select", "data": {"config": {"select": "*"}}},
            {"id": "extra", "type": "sql", "data": {"config": {"sql": "select 1"}}},
            {"id": "write", "type": "write", "data": {"config": {"filename": "o.parquet"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "checkpoint"},
            {"id": "e2", "source": "checkpoint", "target": "identity"},
            {"id": "e3", "source": "identity", "target": "extra"},
            {"id": "e4", "source": "extra", "target": "write"},
        ],
    })
    assert _bounded_fanout_write_shape(graph, "write") is None

    # Four nodes but identity select has extras → 409
    bad_identity = Graph.model_validate({
        "id": "g", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "/x"}}},
            {"id": "checkpoint", "type": "select", "data": {
                "config": {"select": "*", "checkpoint": True}}},
            {"id": "identity", "type": "select", "data": {
                "config": {"select": "value"}}},
            {"id": "write", "type": "write", "data": {"config": {"filename": "o.parquet"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "checkpoint"},
            {"id": "e2", "source": "checkpoint", "target": "identity"},
            {"id": "e3", "source": "identity", "target": "write"},
        ],
    })
    with pytest.raises(HTTPException) as exc:
        _bounded_fanout_write_shape(bad_identity, "write")
    assert exc.value.status_code == 409

    # checkpoint Select missing select:* → 409
    bad_ck = Graph.model_validate({
        "id": "g", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "/x"}}},
            {"id": "checkpoint", "type": "select", "data": {
                "config": {"checkpoint": True}}},
            {"id": "identity", "type": "select", "data": {"config": {"select": "*"}}},
            {"id": "write", "type": "write", "data": {"config": {"filename": "o.parquet"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "checkpoint"},
            {"id": "e2", "source": "checkpoint", "target": "identity"},
            {"id": "e3", "source": "identity", "target": "write"},
        ],
    })
    with pytest.raises(HTTPException) as exc:
        _bounded_fanout_write_shape(bad_ck, "write")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("rows", [0, 1, 3, 5])
def test_happy_path_rows_match_select_star(tmp_path, rows):
    uid, canvas_id = f"fan-user-{uuid.uuid4().hex}", f"fan-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Fanout"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Fanout"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps, rows=rows)
    with metadb.session() as session:
        session.get(metadb.Canvas, canvas_id).doc = json.dumps(
            graph.model_dump(by_alias=True, mode="json"))

    status, owner = start_run(
        deps, graph, "write", uid, confirmed=True, submission_id=submission)
    assert owner is None
    assert status.status in ("queued", "running")
    task_id = status.run_id
    observed = _await_status(task_id)
    assert observed is not None and observed["status"] == "done", observed
    assert observed["task_kind"] == "bounded_fanout_write"
    assert observed["output_receipt"] is not None
    assert observed["output_receipt"]["rows"] == rows

    # Jobs exclusion
    page = metadb.list_workspace_runs(uid, run_id=task_id)
    assert page["items"] == []

    # Direct lookup still works
    direct = metadb.durable_task(task_id)
    assert direct is not None and direct["status"] == "done"

    # Inbox: one terminal item with job_available False
    inbox = metadb.list_durable_task_inbox_items(uid, limit=50)
    items = [item for item in inbox["items"] if item["task_id"] == task_id]
    assert len(items) == 1
    assert items[0]["outcome"] == "completed"
    assert items[0]["job_available"] is False
    assert items[0]["task_kind"] == "bounded_fanout_write"

    # Arrow schema/values/order equal SELECT *
    receipt = observed["output_receipt"]
    uri = receipt["publication"]["artifactUri"]
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    published = pq.read_table(path)
    expected = _source_table(rows)
    assert published.schema.equals(expected.schema)
    assert published.to_pydict() == expected.to_pydict()
    deps.storage.close()


def test_append_write_rejected(tmp_path):
    uid, canvas_id = f"append-user-{uuid.uuid4().hex}", f"append-canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Append"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Append"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps, rows=2)
    write = next(node for node in graph.nodes if node.id == "write")
    write.data["config"]["writeMode"] = "append"
    with pytest.raises(HTTPException) as exc:
        start_run(deps, graph, "write", uid, confirmed=True, submission_id=str(uuid.uuid4()))
    assert exc.value.status_code == 409
    deps.storage.close()


def test_same_semantic_replay_idempotent(tmp_path):
    uid, canvas_id = f"replay-user-{uuid.uuid4().hex}", f"replay-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Replay"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Replay"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps, rows=2)
    first, _ = start_run(deps, graph.model_copy(deep=True), "write", uid,
                         confirmed=True, submission_id=submission)
    second, _ = start_run(deps, graph.model_copy(deep=True), "write", uid,
                          confirmed=True, submission_id=submission)
    assert first.run_id == second.run_id
    deps.storage.close()


def test_changed_semantics_conflict(tmp_path):
    uid, canvas_id = f"conflict-user-{uuid.uuid4().hex}", f"conflict-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Conflict"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Conflict"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps, rows=2)
    start_run(deps, graph.model_copy(deep=True), "write", uid,
              confirmed=True, submission_id=submission)
    changed = graph.model_copy(deep=True)
    identity = next(node for node in changed.nodes if node.id == "identity")
    identity.data["config"]["select"] = "value"
    with pytest.raises(HTTPException) as exc:
        start_run(deps, changed, "write", uid, confirmed=True, submission_id=submission)
    # Shape rejection (409) before or as submission conflict
    assert exc.value.status_code == 409
    deps.storage.close()


def test_prefix_execute_once_across_post_commit_retry(tmp_path, monkeypatch):
    uid, canvas_id = f"once-user-{uuid.uuid4().hex}", f"once-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Once"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Once"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps, rows=3)
    counts = {"prefix": 0}
    original = bft._materialize_prefix

    def counted(*args, **kwargs):
        counts["prefix"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bft, "_materialize_prefix", counted)
    status, _ = start_run(deps, graph, "write", uid, confirmed=True, submission_id=submission)
    task_id = status.run_id
    assert _await_status(task_id)["status"] == "done"
    assert counts["prefix"] == 1

    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        task.status = "failed"
        task.error = "forced"
        task.completed_at = metadb._now()
        from sqlalchemy import select
        attempt = session.scalars(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id).order_by(
            metadb.DurableTaskAttempt.attempt_number.desc())).first()
        attempt.status = "failed"
        attempt.completed_at = metadb._now()
    from hub.durable_tasks import retry
    retry(task_id, str(uuid.uuid4()), deps)
    deadline = time.time() + 60
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed and observed["status"] == "done" and len(observed["attempts"]) >= 2:
            break
        time.sleep(0.1)
    assert counts["prefix"] == 1
    assert metadb.durable_task(task_id)["status"] == "done"
    deps.storage.close()
