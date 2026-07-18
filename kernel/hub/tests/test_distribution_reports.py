"""Hidden immutable DatasetView distribution-report lifecycle contracts."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from hub import distribution_reports, metadb
from hub.models import DatasetViewDefinitionV1
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'distribution-reports.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@pytest.fixture
def core_view_factory(tmp_path):
    stores: list[LocalStorage] = []

    def create() -> dict:
        token = uuid.uuid4().hex
        storage = LocalStorage(str(tmp_path / f"outputs-{token}"))
        stores.append(storage)
        catalog = InMemoryCatalog(
            str(tmp_path / f"data-{token}"), lambda _uri: DuckDBAdapter())
        logical_uri = str(tmp_path / "published" / f"{token}.parquet")
        run_id = uuid.uuid4().hex
        artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
        pq.write_table(pa.table({"value": [1, 2, 3]}), artifact)
        storage.commit_result(artifact, run_id)
        published = catalog.publish_managed_local_file_output(
            name=f"report-source-{token}", logical_uri=logical_uri, artifact_uri=artifact)
        assert storage.release_result(artifact, run_id) is True
        workspace = metadb.dataset_view_source_workspace(published["dataset_id"])
        view_id, placement_id = uuid.uuid4().hex, uuid.uuid4().hex
        definition = DatasetViewDefinitionV1.model_validate({
            "schemaVersion": 1,
            "id": view_id,
            "creatorId": metadb.DEFAULT_USER_ID,
            "name": "Report population",
            "datasetRef": {
                "kind": "exact",
                "datasetId": published["dataset_id"],
                "revisionId": published["revision_id"],
            },
            "placement": {
                "containerId": workspace["containerId"],
                "placementId": placement_id,
                "sourceRegistrationId": workspace["sourceRegistrationId"],
            },
            "selectedColumns": ["value"],
            "predicate": None,
            "sampling": {"kind": "all"},
            "sampleProvenance": None,
            "retentionOwner": "core",
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "semanticSha256": "a" * 64,
            "definitionSha256": "b" * 64,
        })
        digest_payload = definition.model_dump(by_alias=True, mode="json")
        digest_payload.pop("definitionSha256")
        digest_payload.pop("temporalWindow")
        definition = definition.model_copy(update={
            "definition_sha256": hashlib.sha256(json.dumps(
                digest_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        })
        document = json.dumps(
            definition.model_dump(by_alias=True, mode="json"),
            sort_keys=True, separators=(",", ":"))
        stored, created = metadb.dataset_view_create(
            uid=metadb.DEFAULT_USER_ID,
            view_id=view_id,
            placement_id=placement_id,
            submission_id=uuid.uuid4().hex,
            request_sha256="c" * 64,
            definition_sha256=definition.definition_sha256,
            definition_doc=document,
            source_dataset_id=published["dataset_id"],
            source_registration_id=workspace["sourceRegistrationId"],
            expected_container_id=workspace["containerId"],
        )
        assert created is True
        return stored

    yield create
    for storage in stores:
        storage.close()


def _intent(view: dict, submission_id: str | None = None, **changes) -> dict:
    value = {
        "schemaVersion": 1,
        "submissionId": submission_id or uuid.uuid4().hex,
        "datasetViewId": view["id"],
        "viewDefinitionSha256": view["definitionSha256"],
        "computationVersion": "distribution-v1",
        "maxAttempts": 3,
    }
    value.update(changes)
    return value


def _report(claim: dict, **content) -> dict:
    return {
        "schemaVersion": 1,
        "reportId": claim["report_id"],
        "taskId": claim["task"]["id"],
        "datasetViewId": claim["intent"]["datasetViewId"],
        "viewDefinitionSha256": claim["intent"]["viewDefinitionSha256"],
        "computationVersion": claim["intent"]["computationVersion"],
        "content": content or {"rowCount": 3},
    }


def _expire_latest_attempt(task_id: str) -> None:
    with metadb.session() as session:
        attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id,
        ).order_by(metadb.DurableTaskAttempt.attempt_number.desc()).limit(1))
        assert attempt is not None
        attempt.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=30)


def test_atomic_admission_replay_conflict_and_persistence_rollback(
    core_view_factory, monkeypatch,
):
    view = core_view_factory()
    intent = _intent(view)

    def admit(_index: int):
        return distribution_reports.admit_distribution_report(
            owner_id=metadb.DEFAULT_USER_ID, intent=intent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))
    assert sorted(created for _doc, created in results) == [False, True]
    assert results[0][0]["report_id"] == results[1][0]["report_id"]
    with pytest.raises(distribution_reports.DistributionReportSubmissionConflict):
        distribution_reports.admit_distribution_report(
            owner_id=metadb.DEFAULT_USER_ID,
            intent={**intent, "computationVersion": "distribution-v2"})
    with metadb.session() as session:
        task_id = results[0][0]["task"]["id"]
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask).where(
            metadb.DurableTask.id == task_id)) == 1
        assert session.scalar(select(func.count()).select_from(
            metadb.DurableTaskAttempt).where(
                metadb.DurableTaskAttempt.task_id == task_id)) == 1
        assert session.scalar(select(func.count()).select_from(
            metadb.DistributionReportEnvelope).where(
                metadb.DistributionReportEnvelope.task_id == task_id)) == 1

    other = core_view_factory()
    broken = _intent(other)
    with monkeypatch.context() as patch:
        patch.setattr(
            metadb, "sync_local_result_owner",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("persistence failed")))
        with pytest.raises(RuntimeError, match="persistence failed"):
            distribution_reports.admit_distribution_report(
                owner_id=metadb.DEFAULT_USER_ID, intent=broken)
    task_id = distribution_reports._task_id(
        metadb.DEFAULT_USER_ID, broken["submissionId"])
    with metadb.session() as session:
        assert session.get(metadb.DurableTask, task_id) is None
        assert session.get(metadb.DistributionReportEnvelope, task_id) is None


def test_db_time_duplicate_owner_fencing_terminal_atomicity_and_response_loss(
    core_view_factory,
):
    view = core_view_factory()
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    task_id = admitted["task"]["id"]
    assert task_id in distribution_reports.due_distribution_report_task_ids()
    first = distribution_reports.claim_distribution_report(task_id, "owner-one")
    assert first is not None
    assert task_id not in distribution_reports.due_distribution_report_task_ids()
    first_attempt = first["task"]["attempts"][-1]["id"]
    assert distribution_reports.claim_distribution_report(task_id, "owner-two") is None
    _expire_latest_attempt(task_id)
    assert task_id in distribution_reports.due_distribution_report_task_ids()
    second = distribution_reports.claim_distribution_report(task_id, "owner-two")
    assert second is not None
    second_attempt = second["task"]["attempts"][-1]["id"]
    assert second_attempt != first_attempt
    assert distribution_reports.heartbeat_distribution_report(
        task_id, first_attempt, "owner-one") is False
    assert distribution_reports.complete_distribution_report(
        task_id=task_id, attempt_id=first_attempt, owner_token="owner-one",
        report=_report(second)) is None

    document = _report(second, rowCount=3, columns=[])
    completed = distribution_reports.complete_distribution_report(
        task_id=task_id, attempt_id=second_attempt, owner_token="owner-two",
        report=document)
    assert completed is not None and completed["task"]["status"] == "done"
    replay = distribution_reports.complete_distribution_report(
        task_id=task_id, attempt_id=second_attempt, owner_token="owner-two",
        report=document)
    assert replay == completed
    assert task_id not in distribution_reports.due_distribution_report_task_ids()
    assert distribution_reports.complete_distribution_report(
        task_id=task_id, attempt_id=second_attempt, owner_token="owner-two",
        report={**document, "content": {"rowCount": 4}}) is None
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        row = session.get(metadb.DistributionReportEnvelope, task_id)
        assert task is not None and row is not None
        assert task.status == "done" and row.report_doc is not None
        assert row.completed_at == task.completed_at
        inbox = list(session.scalars(select(metadb.DurableTaskInboxItem).where(
            metadb.DurableTaskInboxItem.task_id == task_id)))
        assert len(inbox) == 1
        assert inbox[0].canvas_id is None and inbox[0].dataset_view_id == view["id"]
        inbox_id = inbox[0].id
    assert metadb.list_durable_task_inbox_items(
        metadb.DEFAULT_USER_ID)["items"] == []
    assert metadb.count_durable_task_inbox_unread(metadb.DEFAULT_USER_ID) == 0
    assert metadb.durable_task_inbox_item(metadb.DEFAULT_USER_ID, inbox_id) is None
    assert metadb.mark_durable_task_inbox_item_read(
        metadb.DEFAULT_USER_ID, inbox_id) is None
    assert metadb.list_workspace_runs(
        metadb.DEFAULT_USER_ID, run_id=task_id)["items"] == []
    with metadb.session() as session:
        internal = session.get(metadb.DurableTaskInboxItem, inbox_id)
        assert internal is not None and internal.read_at is None


def test_cancel_retry_bounds_and_report_hold_survive_view_delete_and_restart(
    core_view_factory,
):
    from hub.settings import settings

    view = core_view_factory()
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    task_id, report_id = admitted["task"]["id"], admitted["report_id"]
    claimed = distribution_reports.claim_distribution_report(task_id, "cancel-owner")
    assert claimed is not None
    attempt_id = claimed["task"]["attempts"][-1]["id"]
    cancelled = distribution_reports.request_distribution_report_cancel(
        owner_id=metadb.DEFAULT_USER_ID, task_id=task_id)
    assert cancelled is not None and cancelled["task"]["cancel_requested"] is True
    assert distribution_reports.distribution_report_should_stop(
        task_id, attempt_id, "cancel-owner") is True
    assert distribution_reports.claim_distribution_report(task_id, "recovery-owner") is None
    _expire_latest_attempt(task_id)
    assert distribution_reports.claim_distribution_report(task_id, "recovery-owner") is None
    terminal = distribution_reports.distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, task_id=task_id)
    assert terminal is not None and terminal["task"]["status"] == "cancelled"

    action = uuid.uuid4().hex
    retried = distribution_reports.retry_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, task_id=task_id, retry_request_id=action)
    replay = distribution_reports.retry_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, task_id=task_id, retry_request_id=action)
    assert len(retried["task"]["attempts"]) == len(replay["task"]["attempts"]) == 3
    claim = distribution_reports.claim_distribution_report(task_id, "failure-owner")
    assert claim is not None
    attempt_id = claim["task"]["attempts"][-1]["id"]
    failed = distribution_reports.fail_distribution_report(
        task_id=task_id, attempt_id=attempt_id, owner_token="failure-owner")
    assert failed is not None and failed["task"]["status"] == "failed"
    assert distribution_reports.fail_distribution_report(
        task_id=task_id, attempt_id=attempt_id,
        owner_token="failure-owner") == failed
    assert distribution_reports.fail_distribution_report(
        task_id=task_id, attempt_id=attempt_id,
        owner_token="different-owner") is None
    assert distribution_reports.complete_distribution_report(
        task_id=task_id, attempt_id=attempt_id, owner_token="failure-owner",
        report=_report(claim)) is None
    with pytest.raises(ValueError, match="retry limit"):
        distribution_reports.retry_distribution_report(
            owner_id=metadb.DEFAULT_USER_ID,
            task_id=task_id,
            retry_request_id=uuid.uuid4().hex)

    assert metadb.dataset_view_delete(metadb.DEFAULT_USER_ID, view["id"]) is True
    with metadb.session() as session:
        assert session.scalar(select(metadb.LocalResultReference.uri).where(
            metadb.LocalResultReference.owner_kind == "dataset_view",
            metadb.LocalResultReference.owner_key == view["id"])) is None
        assert session.scalar(select(metadb.LocalResultReference.uri).where(
            metadb.LocalResultReference.owner_kind == "distribution_report",
            metadb.LocalResultReference.owner_key == report_id)) is not None
    assert settings.database_url
    assert metadb._engine is not None
    metadb._engine.dispose()
    metadb._engine = metadb._Session = None
    reopened = distribution_reports.distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, task_id=task_id)
    assert reopened is not None and reopened["view_snapshot"]["id"] == view["id"]


def test_corrupt_frozen_snapshot_fails_closed(core_view_factory):
    view = core_view_factory()
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    task_id = admitted["task"]["id"]
    with metadb.session() as session:
        row = session.get(metadb.DistributionReportEnvelope, task_id)
        assert row is not None
        row.view_snapshot_doc = "{}"
    assert distribution_reports.claim_distribution_report(task_id, "recovery-owner") is None
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id))
        row = session.get(metadb.DistributionReportEnvelope, task_id)
        assert task is not None and attempt is not None and row is not None
        assert task.status == attempt.status == "failed"
        assert row.report_doc is None and row.completed_at == task.completed_at


@pytest.mark.parametrize("entrypoint", ["read", "claim"])
def test_missing_report_owned_revision_hold_fails_closed_and_terminalizes(
    core_view_factory, entrypoint,
):
    view = core_view_factory()
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    task_id, report_id = admitted["task"]["id"], admitted["report_id"]
    with metadb.session() as session:
        reference = session.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "distribution_report",
            metadb.LocalResultReference.owner_key == report_id))
        assert reference is not None
        session.delete(reference)

    result = (
        distribution_reports.distribution_report(
            owner_id=metadb.DEFAULT_USER_ID, task_id=task_id)
        if entrypoint == "read"
        else distribution_reports.claim_distribution_report(task_id, "missing-hold-owner")
    )
    assert result is None
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id))
        row = session.get(metadb.DistributionReportEnvelope, task_id)
        inbox = session.scalar(select(metadb.DurableTaskInboxItem).where(
            metadb.DurableTaskInboxItem.task_id == task_id))
        assert task is not None and attempt is not None and row is not None
        assert task.status == attempt.status == "failed"
        assert row.report_doc is None and row.completed_at == task.completed_at
        assert inbox is not None
        assert inbox.diagnostic_code == "distribution_report_snapshot_invalid"


def test_postgres_read_and_terminal_commit_observe_one_locked_report_version(
    core_view_factory, monkeypatch,
):
    with metadb.session() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("PostgreSQL READ COMMITTED lock interleaving contract")
    view = core_view_factory()
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    task_id = admitted["task"]["id"]
    claim = distribution_reports.claim_distribution_report(task_id, "read-race-owner")
    assert claim is not None
    attempt_id = claim["task"]["attempts"][-1]["id"]
    document = _report(claim)

    reader_locked = threading.Event()
    writer_lock_attempted = threading.Event()
    writer_done = threading.Event()
    thread_role = threading.local()
    original_doc = distribution_reports._envelope_doc
    original_locked_rows = distribution_reports._locked_report_rows

    def observe_writer_lock(*args, **kwargs):
        if getattr(thread_role, "value", None) == "writer":
            writer_lock_attempted.set()
        return original_locked_rows(*args, **kwargs)

    def pause_locked_reader(*args, **kwargs):
        if getattr(thread_role, "value", None) == "reader":
            reader_locked.set()
            assert writer_lock_attempted.wait(timeout=5)
            assert not writer_done.wait(timeout=.25)
        return original_doc(*args, **kwargs)

    monkeypatch.setattr(distribution_reports, "_locked_report_rows", observe_writer_lock)
    monkeypatch.setattr(distribution_reports, "_envelope_doc", pause_locked_reader)

    def read_report():
        thread_role.value = "reader"
        return distribution_reports.distribution_report(
            owner_id=metadb.DEFAULT_USER_ID, task_id=task_id)

    def finish_report():
        thread_role.value = "writer"
        try:
            return distribution_reports.complete_distribution_report(
                task_id=task_id, attempt_id=attempt_id,
                owner_token="read-race-owner", report=document)
        finally:
            writer_done.set()

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="report-reader") as pool:
        read_future = pool.submit(read_report)
        assert reader_locked.wait(timeout=5)
        write_future = pool.submit(finish_report)
        read_version = read_future.result(timeout=10)
        terminal_version = write_future.result(timeout=10)
    assert read_version is not None and read_version["task"]["status"] == "running"
    assert terminal_version is not None and terminal_version["task"]["status"] == "done"
