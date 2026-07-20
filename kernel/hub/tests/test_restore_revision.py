"""Durable restore of a retained revision as a new current head."""
from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hub import metadb, restore_revision_tasks
from hub.api_errors import APIError
from hub.deps import Deps
from hub.models import RestoreRevisionRequestV1
from hub.routers import restore_revision as api


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


def _publish(deps, logical_uri: str, run_id: str, table: pa.Table) -> dict:
    artifact = deps.storage.begin_result(run_id, run_id)
    pq.write_table(table, artifact)
    deps.storage.commit_result(artifact, run_id)
    published = deps.catalog.publish_managed_local_file_output(
        name="base", logical_uri=logical_uri, artifact_uri=artifact)
    assert deps.storage.release_result(artifact, run_id)
    return published


def _dataset(tmp_path, monkeypatch, *, owner_id: str = "owner"):
    deps = Deps(str(tmp_path / "workspace"), str(tmp_path / "data"), maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)
    monkeypatch.setattr(api, "dispatch", lambda task_id, _deps: None)
    with metadb.session() as session:
        session.add(metadb.User(id=owner_id, name="Owner"))
    logical_uri = deps.storage.output_uri("base", ".parquet")
    first = _publish(deps, logical_uri, "rev-one", pa.table({
        "id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"]}))
    second = _publish(deps, logical_uri, "rev-two", pa.table({
        "id": pa.array([1, 2, 3], type=pa.int32()), "value": ["c", "d", "e"]}))
    return deps, first["dataset_id"], first["revision_id"], second["revision_id"]


def _run(deps, task_id: str) -> None:
    restore_revision_tasks._worker(task_id, deps)


def test_restore_old_revision_publishes_new_head_and_keeps_history(tmp_path, monkeypatch):
    deps, dataset_id, old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    task = api.submit(dataset_id, old_revision,
                      RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=head_revision),
                      "owner")
    assert task.status == "queued"
    _run(deps, task.task_id)
    done = api.status(task.task_id, "owner")
    assert done.status == "done" and done.receipt is not None
    child = done.child_revision_id
    assert child not in (None, old_revision, head_revision)
    assert done.receipt.parent_head is not None
    assert done.receipt.parent_head.revision_id == head_revision
    # The restored head carries the old revision's exact contents.
    artifact = metadb.managed_local_file_revision_artifact(dataset_id, child)
    assert pq.read_table(artifact).to_pydict() == {"id": [1, 2], "value": ["a", "b"]}
    # History remains append-only: old, head, and the new child all remain retained.
    rows, _cursor = metadb.managed_local_file_revision_history(
        metadb.catalog_revision_binding(dataset_id)["uri"], limit=10)
    ids = [row["revision_id"] for row in rows]
    assert ids[0] == child and set(ids) == {child, head_revision, old_revision}
    # Provenance links the new revision to the restored source.
    assert done.receipt.provenance.publication.step_id == f"restore-revision:{old_revision}"


def test_response_loss_replay_returns_the_original_receipt(tmp_path, monkeypatch):
    deps, dataset_id, old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    request = RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=head_revision)
    first = api.submit(dataset_id, old_revision, request, "owner")
    _run(deps, first.task_id)
    done = api.status(first.task_id, "owner")
    # A lost response replays the same submission; the head must not move twice.
    replay = api.submit(dataset_id, old_revision, request, "owner")
    assert replay.task_id == first.task_id
    assert replay.status == "done"
    assert replay.child_revision_id == done.child_revision_id
    rows, _cursor = metadb.managed_local_file_revision_history(
        metadb.catalog_revision_binding(dataset_id)["uri"], limit=10)
    assert len(rows) == 3


def test_concurrent_head_movement_is_rejected_before_admission(tmp_path, monkeypatch):
    deps, dataset_id, old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    # The caller previews a head that is already stale.
    with pytest.raises(APIError) as caught:
        api.submit(dataset_id, old_revision,
                   RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=old_revision),
                   "owner")
    assert caught.value.status_code == 409
    assert metadb.durable_task(metadb.restore_revision_submission_id("owner", "s1")) is None


def test_head_that_moves_after_admission_fails_closed_without_mutation(tmp_path, monkeypatch):
    deps, dataset_id, old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    task = api.submit(dataset_id, old_revision,
                      RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=head_revision),
                      "owner")
    # A different publication advances the head between admission and worker publication.
    logical_uri = deps.storage.output_uri("base", ".parquet")
    _publish(deps, logical_uri, "rev-three", pa.table({
        "id": pa.array([9], type=pa.int32()), "value": ["z"]}))
    moved_head = metadb.catalog_managed_local_head_for_dataset(dataset_id)["revision_id"]
    _run(deps, task.task_id)
    failed = api.status(task.task_id, "owner")
    assert failed.status == "failed" and failed.diagnostic_code == "stale_expected_head"
    assert metadb.catalog_managed_local_head_for_dataset(dataset_id)["revision_id"] == moved_head


def test_unavailable_source_revision_is_rejected(tmp_path, monkeypatch):
    deps, dataset_id, _old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    with pytest.raises(APIError) as caught:
        api.submit(dataset_id, "0" * 32,
                   RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=head_revision),
                   "owner")
    assert caught.value.status_code == 410


def test_restart_recovery_publishes_a_pending_task(tmp_path, monkeypatch):
    deps, dataset_id, old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    task = api.submit(dataset_id, old_revision,
                      RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=head_revision),
                      "owner")
    assert task.task_id in metadb.recoverable_restore_revision_task_ids()
    monkeypatch.setattr(restore_revision_tasks, "dispatch",
                        lambda task_id, _deps: restore_revision_tasks._worker(task_id, deps))
    restore_revision_tasks.recover(deps)
    assert api.status(task.task_id, "owner").status == "done"


def test_status_is_owner_scoped_and_never_echoes_the_task_id(tmp_path, monkeypatch):
    deps, dataset_id, old_revision, head_revision = _dataset(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add(metadb.User(id="intruder", name="Intruder"))
    task = api.submit(dataset_id, old_revision,
                      RestoreRevisionRequestV1(submission_id="s1", expected_head_revision_id=head_revision),
                      "owner")
    with pytest.raises(APIError) as caught:
        api.status(task.task_id, "intruder")
    assert caught.value.status_code == 404 and task.task_id not in str(caught.value.detail)
