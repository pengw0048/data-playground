"""Product consumer for one exact Source -> Select(checkpoint) -> Write durable task."""

from __future__ import annotations

import datetime
import json
import os
import time
import uuid

import pytest
import pyarrow as pa
from sqlalchemy import select

from hub import linear_checkpoint_tasks as lct
from hub import metadb
from hub.deps import Deps
from hub.models import Graph, WriteIntent
from hub.routers.runs import _linear_checkpoint_shape, start_run


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('linear-checkpoint-tasks') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _canvas_graph(tmp_path, canvas_id: str, deps):
    lance = pytest.importorskip("lance")
    source = tmp_path / "source.lance"
    lance.write_dataset(pa.table({"value": [1, 2, 3], "label": ["a", "b", "c"]}), str(source))
    deps.catalog._add(name="source", uri=str(source), strict_probe=True)
    return Graph.model_validate({
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": str(source)}}},
            {"id": "select", "type": "select", "data": {
                "title": "select", "config": {"select": "*", "checkpoint": True}}},
            {"id": "write", "type": "write", "data": {"title": "final", "config": {
                "filename": "final.parquet", "writeMode": "overwrite"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "select",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e2", "source": "select", "target": "write",
             "sourceHandle": "out", "targetHandle": "in"},
        ],
    })


def test_exact_route_rejects_non_matching_shapes():
    graph = Graph.model_validate({
        "id": "g", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "/x"}}},
            {"id": "select", "type": "select", "data": {"config": {"checkpoint": True}}},
            {"id": "extra", "type": "sql", "data": {"config": {"sql": "select 1"}}},
            {"id": "write", "type": "write", "data": {"config": {"filename": "o.parquet"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "select"},
            {"id": "e2", "source": "select", "target": "extra"},
            {"id": "e3", "source": "extra", "target": "write"},
        ],
    })
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _linear_checkpoint_shape(graph, "write")
    assert exc.value.status_code == 409


def test_route_rejects_disabled_or_bypassed_nodes():
    from fastapi import HTTPException

    def _graph(flag_node: str, flag: str) -> Graph:
        nodes = {
            "source": {"id": "source", "type": "source", "data": {"config": {"uri": "/x"}}},
            "select": {"id": "select", "type": "select", "data": {
                "config": {"select": "*", "checkpoint": True}}},
            "write": {"id": "write", "type": "write", "data": {"config": {"filename": "o.parquet"}}},
        }
        nodes[flag_node]["data"][flag] = True
        return Graph.model_validate({
            "id": "g", "version": 1, "nodes": list(nodes.values()),
            "edges": [
                {"id": "e1", "source": "source", "target": "select"},
                {"id": "e2", "source": "select", "target": "write"},
            ],
        })

    for flag_node, flag in (("select", "bypassed"), ("source", "disabled"), ("write", "bypassed")):
        with pytest.raises(HTTPException) as exc:
            _linear_checkpoint_shape(_graph(flag_node, flag), "write")
        assert exc.value.status_code == 409


def test_happy_path_materialize_publish_and_jobs(tmp_path):
    uid, canvas_id = f"cp-user-{uuid.uuid4().hex}", f"cp-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Checkpoint researcher"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Linear checkpoint"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps)
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id)
        canvas.doc = json.dumps(graph.model_dump(by_alias=True, mode="json"))

    status, owner = start_run(
        deps, graph, "write", uid, confirmed=True, submission_id=submission)
    assert owner is None
    assert status.status in ("queued", "running")
    task_id = status.run_id

    deadline = time.time() + 30
    observed = None
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed and observed["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert observed is not None and observed["status"] == "done", observed
    assert observed["output_receipt"] is not None
    assert observed["output_receipt"]["rows"] == 3

    page = metadb.list_workspace_runs(uid, run_id=task_id)
    assert len(page["items"]) == 1
    job = page["items"][0]
    assert job["checkpoint"]["phase"] == "terminal"
    assert job["checkpoint"]["resumeEligible"] is False
    assert job["checkpoint"]["clientKey"] == f"checkpoint:{task_id}"
    assert job["canRetry"] is False
    assert "uri" not in (job["checkpoint"] or {})
    receipt = observed["output_receipt"]
    assert job["outputReceipt"]["revisionId"] == receipt.get("revisionId", receipt.get("revision_id"))
    assert job["checkpoint"]["rows"] == 3

    resolved = metadb.resolve_checkpoint_full_result(task_id)
    assert resolved is not None and resolved["rows"] == 3
    assert os.path.isfile(resolved["uri"])
    deps.storage.close()


def test_same_semantic_replay_returns_one_task(tmp_path):
    uid, canvas_id = f"replay-user-{uuid.uuid4().hex}", f"replay-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Replay"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Replay"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps)
    first, _ = start_run(deps, graph.model_copy(deep=True), "write", uid,
                         confirmed=True, submission_id=submission)
    second, _ = start_run(deps, graph.model_copy(deep=True), "write", uid,
                          confirmed=True, submission_id=submission)
    assert first.run_id == second.run_id
    deps.storage.close()


def test_changed_semantics_conflict(tmp_path):
    from fastapi import HTTPException

    uid, canvas_id = f"conflict-user-{uuid.uuid4().hex}", f"conflict-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Conflict"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Conflict"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps)
    start_run(deps, graph.model_copy(deep=True), "write", uid,
              confirmed=True, submission_id=submission)
    changed = graph.model_copy(deep=True)
    select = next(node for node in changed.nodes if node.id == "select")
    select.data["config"]["select"] = "value"
    with pytest.raises(HTTPException) as exc:
        start_run(deps, changed, "write", uid, confirmed=True, submission_id=submission)
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
    graph = _canvas_graph(tmp_path, canvas_id, deps)
    counts = {"prefix": 0}
    original = lct._materialize_prefix

    def counted(*args, **kwargs):
        counts["prefix"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(lct, "_materialize_prefix", counted)
    status, _ = start_run(deps, graph, "write", uid, confirmed=True, submission_id=submission)
    task_id = status.run_id
    deadline = time.time() + 30
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed and observed["status"] == "done":
            break
        time.sleep(0.1)
    assert metadb.durable_task(task_id)["status"] == "done"
    assert counts["prefix"] == 1

    # Force a failed terminal then retry: Phase 2 only.
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
    deadline = time.time() + 30
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed and observed["status"] == "done" and len(observed["attempts"]) >= 2:
            break
        time.sleep(0.1)
    assert counts["prefix"] == 1
    assert metadb.durable_task(task_id)["status"] == "done"
    deps.storage.close()


def test_checkpoint_invalid_error_surfaces_as_jobs_diagnostic():
    """A wrapped 'checkpoint_invalid' failure must reach the Jobs diagnosticCode."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    checkpoint = SimpleNamespace(
        phase="committed", content_sha256="a" * 64, committed_bytes=10, committed_rows=5,
        checkpoint_node_id="sel", output_port_id="out",
        committed_at=datetime(2026, 7, 17, tzinfo=timezone.utc))
    invalid = SimpleNamespace(
        id="t1", status="failed", error="RuntimeError: checkpoint_invalid: OSError")
    view = metadb._sanitized_checkpoint_jobs_view(invalid, checkpoint, {}, can_retry=False)
    assert view["diagnosticCode"] == "checkpoint_invalid"

    unrelated = SimpleNamespace(id="t2", status="failed", error="RuntimeError: disk full")
    other = metadb._sanitized_checkpoint_jobs_view(unrelated, checkpoint, {}, can_retry=False)
    assert other["diagnosticCode"] is None


def _await_status(task_id, statuses=("done", "failed", "cancelled"), timeout=30):
    deadline = time.time() + timeout
    observed = None
    while time.time() < deadline:
        observed = metadb.durable_task(task_id)
        if observed and observed["status"] in statuses:
            return observed
        time.sleep(0.05)
    return observed


def _run_then_orphan_after_publish(tmp_path, deps):
    """Run the task to done, then reset the DB to the crash-after-publish window: task running,
    latest attempt running with an expired lease, receipt/revision already durable."""
    uid, canvas_id = f"u-{uuid.uuid4().hex}", f"c-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="R"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="R"))
    graph = _canvas_graph(tmp_path, canvas_id, deps)
    with metadb.session() as session:
        session.get(metadb.Canvas, canvas_id).doc = json.dumps(
            graph.model_dump(by_alias=True, mode="json"))
    status, _ = start_run(
        deps, graph, "write", uid, confirmed=True, submission_id=str(uuid.uuid4()))
    task_id = status.run_id
    assert _await_status(task_id)["status"] == "done"
    intent_doc = WriteIntent.model_validate(
        metadb.durable_task(task_id)["write_intent"]).model_dump(by_alias=True, mode="json")
    head = metadb.catalog_managed_local_write_head(intent_doc["destination"]["logicalUri"])
    with metadb.session() as s:
        task = s.get(metadb.DurableTask, task_id)
        task.status, task.completed_at, task.output_receipt = "running", None, None
        attempt = s.scalars(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id).order_by(
            metadb.DurableTaskAttempt.attempt_number.desc())).first()
        attempt.status, attempt.completed_at = "running", None
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=300)
    return task_id, intent_doc, head


def test_cancel_after_landed_publish_reconciles_done(tmp_path):
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    task_id, intent_doc, head = _run_then_orphan_after_publish(tmp_path, deps)
    metadb.request_durable_task_cancel(task_id)
    lct.recover(deps)
    assert _await_status(task_id)["status"] == "done"
    assert metadb.catalog_managed_local_write_receipt(intent_doc) is not None
    assert metadb.catalog_managed_local_write_head(
        intent_doc["destination"]["logicalUri"])["revision_id"] == head["revision_id"]
    deps.storage.close()


def test_exhausted_recovery_after_landed_publish_reconciles_done(tmp_path):
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    task_id, intent_doc, _ = _run_then_orphan_after_publish(tmp_path, deps)
    with metadb.session() as s:
        task = s.get(metadb.DurableTask, task_id)
        latest = s.scalars(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id).order_by(
            metadb.DurableTaskAttempt.attempt_number.desc())).first()
        latest.attempt_number = task.max_attempts
    lct.recover(deps)
    final = _await_status(task_id)
    assert final["status"] == "done" and final["error"] is None
    assert metadb.catalog_managed_local_write_receipt(intent_doc) is not None
    deps.storage.close()
