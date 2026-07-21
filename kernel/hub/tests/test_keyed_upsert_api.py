"""HTTP admission + durable-task lifecycle for one certified keyed upsert (issue #637)."""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select

from hub import keyed_upsert_tasks, metadb
from hub.api_errors import APIError
from hub.deps import Deps
from hub.models import DurableTaskInboxPage, WorkspaceRunPage
from hub.routers import keyed_upsert as api


def _jobs(uid: str, **kwargs) -> WorkspaceRunPage:
    return WorkspaceRunPage.model_validate(metadb.list_workspace_runs(uid, **kwargs))


def _inbox(uid: str) -> DurableTaskInboxPage:
    return DurableTaskInboxPage.model_validate(metadb.list_durable_task_inbox_items(uid, limit=200))


@pytest.fixture(autouse=True)
def _metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'metadata.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


def _table(ids, values) -> pa.Table:
    return pa.table({"id": pa.array(ids, type=pa.int64()), "value": values})


def _publish(deps, logical_uri: str, name: str, run_id: str, table: pa.Table) -> dict:
    artifact = deps.storage.begin_result(run_id, run_id)
    pq.write_table(table, artifact)
    deps.storage.commit_result(artifact, run_id)
    published = deps.catalog.publish_managed_local_file_output(
        name=name, logical_uri=logical_uri, artifact_uri=artifact)
    assert deps.storage.release_result(artifact, run_id)
    return {**published, "logical_uri": logical_uri}


def _dataset(tmp_path, monkeypatch, *, owner_id: str = "owner"):
    suffix = uuid.uuid4().hex
    deps = Deps(str(tmp_path / f"workspace-{suffix}"), str(tmp_path / f"data-{suffix}"),
                maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)
    monkeypatch.setattr(api, "dispatch", lambda task_id, _deps: None)
    with metadb.session() as session:
        if session.get(metadb.User, owner_id) is None:
            session.add(metadb.User(id=owner_id, name="Owner"))
    base = _publish(deps, deps.storage.output_uri("target", ".parquet"), "target", f"t-{suffix}",
                    _table([1, 2, 3], ["a", "b", "c"]))
    return deps, base


def _payload(deps, table: pa.Table, name: str = "payload") -> dict:
    suffix = uuid.uuid4().hex
    return _publish(deps, deps.storage.output_uri(name, ".parquet"), name, f"p-{suffix}", table)


def _request(base: dict, payload: dict, keys=("id",), submission_id: str = "s1"):
    return api.UpsertRequestV1(
        submission_id=submission_id, dataset_id=base["dataset_id"],
        expected_head_revision_id=base["revision_id"],
        payload_dataset_id=payload["dataset_id"], payload_revision_id=payload["revision_id"],
        keys=list(keys))


def _run(deps, task_id: str) -> None:
    keyed_upsert_tasks._worker(task_id, deps)


def _revisions(deps, dataset_id: str) -> int:
    rows, _cursor = metadb.managed_local_file_revision_history(
        metadb.catalog_revision_binding(dataset_id)["uri"], limit=50)
    return len(rows)


def test_preflight_projects_without_side_effect_then_submit_and_replay(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2, 3, 4], ["B", "C", "D"]))
    request = _request(base, payload)

    preview = api.preflight(request, "owner")
    assert preview.eligible
    assert (preview.evidence.matched, preview.evidence.inserted, preview.evidence.unchanged) == (2, 1, 1)
    assert _revisions(deps, base["dataset_id"]) == 1  # preflight published nothing

    task = api.submit(request, "owner")
    assert task.status == "queued"
    _run(deps, task.task_id)
    done = api.status(task.task_id, "owner")
    assert done.status == "done" and done.receipt is not None
    assert done.evidence.matched == 2 and done.evidence.inserted == 1 and done.evidence.unchanged == 1
    child = done.child_revision_id
    assert child not in (None, base["revision_id"])
    artifact = metadb.managed_local_file_revision_artifact(base["dataset_id"], child)
    published = pq.read_table(artifact).to_pydict()
    assert dict(zip(published["id"], published["value"])) == {1: "a", 2: "B", 3: "C", 4: "D"}

    replay = api.submit(request, "owner")
    assert replay.task_id == task.task_id and replay.child_revision_id == child
    assert _revisions(deps, base["dataset_id"]) == 2  # head moved exactly once


def test_reused_submission_id_with_a_different_intent_conflicts(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2, 3], ["B", "C"]))
    first = api.submit(_request(base, payload, keys=("id",)), "owner")
    with pytest.raises(APIError) as caught:  # same id, same operands, different key set
        api.submit(_request(base, payload, keys=("value",)), "owner")
    assert caught.value.status_code == 409
    assert api.submit(_request(base, payload, keys=("id",)), "owner").task_id == first.task_id


def test_all_insert_and_all_update_projections(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    inserts = api.preflight(_request(base, _payload(deps, _table([7, 8], ["g", "h"]))), "owner")
    assert (inserts.evidence.matched, inserts.evidence.inserted, inserts.evidence.unchanged) == (0, 2, 3)
    updates = api.preflight(_request(base, _payload(deps, _table([1, 2, 3], ["x", "y", "z"]))), "owner")
    assert (updates.evidence.matched, updates.evidence.inserted, updates.evidence.unchanged) == (3, 0, 0)


def test_stale_head_is_rejected_before_admission(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2], ["B"]))
    request = _request(base, payload)
    request = request.model_copy(update={"expected_head_revision_id": "0" * 32})
    with pytest.raises(APIError) as caught:
        api.submit(request, "owner")
    assert caught.value.status_code == 409
    assert metadb.durable_task(metadb.keyed_upsert_submission_id("owner", "s1")) is None


def test_unsupported_destination_is_rejected(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2], ["B"]))
    request = _request(base, payload).model_copy(update={"dataset_id": "not-a-dataset"})
    with pytest.raises(APIError) as caught:
        api.preflight(request, "owner")
    assert caught.value.status_code == 404


def test_unavailable_payload_is_rejected(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2], ["B"]))
    request = _request(base, payload).model_copy(update={"payload_revision_id": "0" * 32})
    with pytest.raises(APIError) as caught:
        api.preflight(request, "owner")
    assert caught.value.status_code == 410


def test_duplicate_and_null_and_schema_blockers_reject_at_preflight(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    duplicate = _request(base, _payload(deps, _table([2, 2], ["B", "B2"])))
    with pytest.raises(APIError) as dup:
        api.preflight(duplicate, "owner")
    assert dup.value.status_code == 422
    nulls = _request(base, _payload(deps, pa.table(
        {"id": pa.array([2, None], type=pa.int64()), "value": ["B", "N"]})))
    with pytest.raises(APIError) as null:
        api.preflight(nulls, "owner")
    assert null.value.status_code == 422
    mismatch = _request(base, _payload(deps, pa.table(
        {"id": pa.array([2], type=pa.int64()), "other": ["B"]})))
    with pytest.raises(APIError) as schema:
        api.preflight(mismatch, "owner")
    assert schema.value.status_code == 422
    assert _revisions(deps, base["dataset_id"]) == 1


def test_head_moving_after_admission_fails_closed_and_blocks_retry(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2, 4], ["B", "D"]))
    task = api.submit(_request(base, payload), "owner")
    _publish(deps, base["logical_uri"], "target", "t-moved", _table([1, 2, 3, 5], ["a", "b", "c", "e"]))
    moved = metadb.catalog_managed_local_head_for_dataset(base["dataset_id"])["revision_id"]
    _run(deps, task.task_id)
    failed = api.status(task.task_id, "owner")
    assert failed.status == "failed" and failed.diagnostic_code == "stale_expected_head"
    assert metadb.catalog_managed_local_head_for_dataset(base["dataset_id"])["revision_id"] == moved
    with pytest.raises(APIError) as retry:
        api.retry(task.task_id, api.UpsertRetryRequestV1(retry_request_id="r1"), "owner")
    assert retry.value.status_code == 409


def test_restart_recovery_publishes_a_pending_task(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2], ["B"]))
    task = api.submit(_request(base, payload), "owner")
    assert task.task_id in metadb.recoverable_keyed_upsert_task_ids()
    monkeypatch.setattr(keyed_upsert_tasks, "dispatch",
                        lambda task_id, _deps: keyed_upsert_tasks._worker(task_id, deps))
    keyed_upsert_tasks.recover(deps)
    assert api.status(task.task_id, "owner").status == "done"


def test_owner_can_cancel_then_idempotently_retry(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2, 4], ["B", "D"]))
    task = api.submit(_request(base, payload), "owner")
    api.cancel(task.task_id, "owner")  # requests cancellation on the queued task
    _run(deps, task.task_id)           # the worker observes it and settles cancelled
    assert api.status(task.task_id, "owner").status == "cancelled"
    first = api.retry(task.task_id, api.UpsertRetryRequestV1(retry_request_id="r1"), "owner")
    assert first.status in ("queued", "running", "done")
    api.retry(task.task_id, api.UpsertRetryRequestV1(retry_request_id="r1"), "owner")  # idempotent
    with metadb.session() as session:
        attempts = session.scalars(select(metadb.DurableTaskAttempt.id).where(
            metadb.DurableTaskAttempt.task_id == task.task_id)).all()
    assert len(attempts) == 2
    _run(deps, task.task_id)
    assert api.status(task.task_id, "owner").status == "done"


def test_status_and_cancel_are_owner_scoped(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add(metadb.User(id="intruder", name="Intruder"))
    payload = _payload(deps, _table([2], ["B"]))
    task = api.submit(_request(base, payload), "owner")
    for call in (lambda: api.status(task.task_id, "intruder"),
                 lambda: api.cancel(task.task_id, "intruder")):
        with pytest.raises(APIError) as caught:
            call()
        assert caught.value.status_code == 404 and task.task_id not in str(caught.value.detail)


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"),
                    reason="requires a dedicated PostgreSQL for the admission race")
def test_postgres_submission_serializes_on_the_owner_row(tmp_path, monkeypatch):
    import threading

    deps, base = _dataset(tmp_path, monkeypatch)
    payload = _payload(deps, _table([2, 4], ["B", "D"]))
    request = _request(base, payload)
    gate = threading.Event()
    original = metadb.submit_keyed_upsert_task

    def barrier(**kwargs):
        gate.wait(timeout=10)
        return original(**kwargs)

    monkeypatch.setattr(api.metadb, "submit_keyed_upsert_task", barrier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(api.submit, request, "owner")
        second = pool.submit(api.submit, request, "owner")
        gate.set()
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert results[0].task_id == results[1].task_id
    assert _revisions(deps, base["dataset_id"]) == 1


def test_keyed_upsert_task_surfaces_in_jobs_and_inbox_for_owner_only(tmp_path, monkeypatch):
    deps, base = _dataset(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add(metadb.User(id="stranger", name="Stranger"))
    payload = _payload(deps, _table([2, 3, 4], ["B", "C", "D"]))
    task = api.submit(_request(base, payload), "owner")

    # Running: one canvas-less Jobs row keyed to the dataset; no Inbox item yet.
    queued = [item for item in _jobs("owner").items if item.run_id == task.task_id]
    assert len(queued) == 1
    row = queued[0]
    assert row.status == "queued" and row.canvas_id is None and row.can_cancel is True
    assert row.dataset_context is not None
    assert row.dataset_context.task_kind == "keyed_upsert_write"
    assert row.dataset_context.dataset_id == base["dataset_id"]
    assert metadb.catalog_revision_binding(row.dataset_context.dataset_id) is not None
    assert _inbox("owner").items == []

    _run(deps, task.task_id)

    # Terminal: still exactly one Jobs row (no history twin), done with a receipt.
    done = [item for item in _jobs("owner").items if item.run_id == task.task_id]
    assert len(done) == 1
    assert done[0].status == "done" and done[0].output_receipt is not None
    assert done[0].can_cancel is False

    inbox = [item for item in _inbox("owner").items if item.task_id == task.task_id]
    assert len(inbox) == 1
    assert inbox[0].outcome == "completed" and inbox[0].job_available is True
    assert inbox[0].canvas_id is None
    assert inbox[0].dataset_context is not None
    assert inbox[0].dataset_context.dataset_id == base["dataset_id"]
    assert inbox[0].completed_write is None

    # Owner-scoped: a stranger sees neither surface, and a canvas filter excludes the canvas-less row.
    assert [item for item in _jobs("stranger").items if item.run_id == task.task_id] == []
    assert _inbox("stranger").items == []
    assert [item for item in _jobs("owner", canvas_id=base["dataset_id"]).items
            if item.run_id == task.task_id] == []
