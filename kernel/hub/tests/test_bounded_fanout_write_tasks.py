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
from hub.tests.task_manifest_helpers import with_task_manifest


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


@pytest.fixture(autouse=True)
def _reset_fanout_state():
    """Isolate the global 4-slot pool + plans/units between tests so the suite is safe on a shared
    (PostgreSQL) database, not only a fresh per-module SQLite file."""
    yield
    with bft._active_lock:
        active = [state[1] for state in bft._active.values() if state[1].is_alive()]
    deadline = time.time() + 5.0
    for thread in active:
        thread.join(timeout=max(0.0, deadline - time.time()))
    from sqlalchemy import delete, update

    from hub import bounded_fanout as fanout
    with metadb.session() as s:
        s.execute(update(fanout.BoundedFanoutSlot).values(
            holder_attempt_id=None, claim_token=None, lease_until=None))
        s.execute(delete(fanout.BoundedFanoutUnitAttempt))
        s.execute(delete(fanout.BoundedFanoutUnit))
        s.execute(delete(fanout.BoundedFanoutPlan))
        s.execute(delete(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind.in_(
                (fanout.CHILD_OWNER, fanout.GATHER_OWNER))))
    with bft._active_lock:
        remaining = {
            task_id: state[1].name
            for task_id, state in bft._active.items()
            if state[1].is_alive()
        }
    assert remaining == {}, f"bounded fan-out workers outlived their test: {remaining}"


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
            with bft._active_lock:
                active = bft._active.get(str(task_id))
            if active is not None:
                active[1].join(timeout=max(0.0, deadline - time.time()))
                assert not active[1].is_alive(), f"fan-out worker did not exit: {task_id}"
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


def test_route_rejects_disabled_or_bypassed_nodes():
    def _graph(flag_node: str, flag: str) -> Graph:
        nodes = {
            "source": {"id": "source", "type": "source", "data": {"config": {"uri": "/x"}}},
            "checkpoint": {"id": "checkpoint", "type": "select", "data": {
                "config": {"select": "*", "checkpoint": True}}},
            "identity": {"id": "identity", "type": "select", "data": {"config": {"select": "*"}}},
            "write": {"id": "write", "type": "write", "data": {"config": {"filename": "o.parquet"}}},
        }
        nodes[flag_node]["data"][flag] = True
        return Graph.model_validate({
            "id": "g", "version": 1, "nodes": list(nodes.values()),
            "edges": [
                {"id": "e1", "source": "source", "target": "checkpoint"},
                {"id": "e2", "source": "checkpoint", "target": "identity"},
                {"id": "e3", "source": "identity", "target": "write"},
            ],
        })

    for flag_node, flag in (("identity", "bypassed"), ("source", "disabled"),
                            ("checkpoint", "bypassed"), ("write", "disabled")):
        with pytest.raises(HTTPException) as exc:
            _bounded_fanout_write_shape(_graph(flag_node, flag), "write")
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

    # Parent-only Jobs projection (#423)
    page = metadb.list_workspace_runs(uid, run_id=task_id)
    assert len(page["items"]) == 1
    job = page["items"][0]
    assert job["taskId"] == task_id
    fanout = job["boundedFanout"]
    assert fanout["stage"] == "terminal"
    assert fanout["checkpoint"] == "reused"
    assert fanout["gather"] == "committed"
    assert fanout["partitionCount"] in (1, 2, 3, 4)
    assert fanout["completedPartitions"] == fanout["partitionCount"]
    assert fanout["failedPartitions"] == 0
    assert job["updatedAt"].endswith("+00:00")
    assert job["taskAttempts"][-1]["updatedAt"].endswith("+00:00")
    forbidden = (
        "planDigest", "plan_digest", "ranges", "unitId", "unit_id", "slot",
        "lease", "token", "digest", "uri", "schema", "attempt_id",
    )
    encoded = json.dumps(fanout)
    assert not any(key in encoded for key in forbidden)
    assert "units" not in fanout

    # Direct lookup still works
    direct = metadb.durable_task(task_id)
    assert direct is not None and direct["status"] == "done"

    # Inbox: one terminal item with job_available True once Jobs-visible
    inbox = metadb.list_durable_task_inbox_items(uid, limit=50)
    items = [item for item in inbox["items"] if item["task_id"] == task_id]
    assert len(items) == 1
    assert items[0]["outcome"] == "completed"
    assert items[0]["job_available"] is True
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
    assert _await_status(first.run_id)["status"] == "done"
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
    status, _ = start_run(deps, graph.model_copy(deep=True), "write", uid,
                          confirmed=True, submission_id=submission)
    changed = graph.model_copy(deep=True)
    identity = next(node for node in changed.nodes if node.id == "identity")
    identity.data["config"]["select"] = "value"
    with pytest.raises(HTTPException) as exc:
        start_run(deps, changed, "write", uid, confirmed=True, submission_id=submission)
    # Shape rejection (409) before or as submission conflict
    assert exc.value.status_code == 409
    assert _await_status(status.run_id)["status"] == "done"
    deps.storage.close()


def test_prefix_execute_once_across_post_commit_retry(tmp_path, monkeypatch):
    from hub import bounded_fanout as fanout

    # Keep the production lease lengths and execution path, but own time and worker
    # coordination so CI scheduling cannot inject an unrelated recovery attempt.
    controlled_now = metadb._now()
    monkeypatch.setattr(metadb, "_durable_task_db_now", lambda _session: controlled_now)
    monkeypatch.setattr(fanout, "_durable_task_db_now", lambda _session: controlled_now)
    monkeypatch.setattr(bft, "dispatch", lambda task_id, deps: bft._worker(task_id, deps))
    uid, canvas_id = f"once-user-{uuid.uuid4().hex}", f"once-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Once"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Once"))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    graph = _canvas_graph(tmp_path, canvas_id, deps, rows=3)
    counts: dict[str, int] = {}
    original = bft._materialize_prefix

    def counted(worker_deps, claimed, attempt_id, owner_token, candidate):
        counted_task_id = str(claimed["id"])
        counts[counted_task_id] = counts.get(counted_task_id, 0) + 1
        return original(worker_deps, claimed, attempt_id, owner_token, candidate)

    monkeypatch.setattr(bft, "_materialize_prefix", counted)
    status, _ = start_run(deps, graph, "write", uid, confirmed=True, submission_id=submission)
    task_id = status.run_id
    assert _await_status(task_id)["status"] == "done"
    assert counts.get(task_id) == 1

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
    observed = metadb.durable_task(task_id)
    assert observed is not None
    assert [attempt["status"] for attempt in observed["attempts"]] == ["failed", "done"]
    assert counts.get(task_id) == 1
    assert observed["status"] == "done"
    deps.storage.close()


def test_jobs_stage_follows_sql_when_fanout_phase_absent(tmp_path):
    """Jobs stage must not depend on status_doc.fanout_phase (stripped by RunStatus)."""
    from hub import bounded_fanout as fanout
    from hub.storage import LocalStorage
    from hub.tests.test_linear_checkpoint_admission import _identity
    from hub.tests.test_linear_checkpoint_commit import _commit, _parquet_bytes

    values = {**_identity(), "task_kind": "bounded_fanout_write"}
    store = LocalStorage(str(tmp_path / "outputs"))
    admission, _ = metadb.submit_linear_checkpoint_task(**with_task_manifest(
        values, target_key="final_target_node_id"))
    task_id = admission["task_id"]
    claimed = metadb.claim_bounded_fanout_write_task(task_id, "jobs-owner")
    assert claimed is not None
    attempt_id = claimed["attempts"][-1]["id"]
    candidate = metadb.reserve_linear_checkpoint_candidate(
        task_id=task_id, attempt_id=attempt_id, owner_token="jobs-owner",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    _commit(store, {
        "task_id": task_id, "attempt_id": attempt_id, "owner": "jobs-owner",
        "candidate": candidate,
    }, _parquet_bytes(5))

    # status_doc deliberately omits fanout_phase (as production persistence does).
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        task.status = "running"
        task.status_doc = json.dumps({
            "runId": task_id, "status": "running", "targetNodeId": "final",
            "progress": 0.55,
        })
        task.error = None

    # Committed checkpoint, no plan yet → planning + committed.
    page = metadb.list_workspace_runs(values["uid"], run_id=task_id)
    assert len(page["items"]) == 1
    view = page["items"][0]["boundedFanout"]
    assert view["stage"] == "planning"
    assert view["checkpoint"] == "committed"
    assert view["gather"] == "pending"
    assert view["partitionCount"] is None
    assert view["completedPartitions"] == 0

    plan = fanout.create_or_reopen_plan(
        parent_task_id=task_id, parent_attempt_id=attempt_id, owner_token="jobs-owner")
    children = [unit for unit in plan["units"] if unit["kind"] == "child"]
    assert len(children) == 4

    # Mark two children done without touching status_doc phase.
    with metadb.session() as session:
        for unit in children[:2]:
            row = session.get(fanout.BoundedFanoutUnit, unit["unit_id"])
            row.status = "done"
            row.result_rows = int(unit["range_end"]) - int(unit["range_start"])

    page = metadb.list_workspace_runs(values["uid"], run_id=task_id)
    view = page["items"][0]["boundedFanout"]
    assert view["stage"] == "running_partitions"
    assert view["checkpoint"] == "reused"
    assert view["partitionCount"] == 4
    assert view["completedPartitions"] == 2
    assert view["failedPartitions"] == 0
    assert view["gather"] == "pending"

    # All children done → gathering even before gather claim.
    with metadb.session() as session:
        for unit in children[2:]:
            row = session.get(fanout.BoundedFanoutUnit, unit["unit_id"])
            row.status = "done"

    page = metadb.list_workspace_runs(values["uid"], run_id=task_id)
    view = page["items"][0]["boundedFanout"]
    assert view["stage"] == "gathering"
    assert view["completedPartitions"] == 4

    # Broad "exhausted" errors must not invent a parent diagnostic.
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        task.error = "RuntimeError: connection pool exhausted"

    page = metadb.list_workspace_runs(values["uid"], run_id=task_id)
    assert page["items"][0]["boundedFanout"]["diagnosticCode"] is None
    store.close()
