"""Hidden immutable DatasetView distribution-report lifecycle contracts."""

from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hub import (
    auth,
    db,
    distribution_report_insights,
    distribution_report_tasks,
    distribution_reports,
    metadb,
)
from hub.main import app
from hub.models import ColumnSchema, DatasetViewDefinitionV1
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import dataset_views as dataset_view_routes
from hub.routers import runs as run_routes
from hub.storage import LocalStorage


class _DeclaredIdentityDuckDBAdapter(DuckDBAdapter):
    """Test provider that supplies one stable field identity independently of its name."""

    def schema(self, uri: str) -> list[ColumnSchema]:
        columns = super().schema(uri)
        return [column.model_copy(update={
            "field_id": f"declared-field-{index}", "provenance": "provider",
        }) for index, column in enumerate(columns)]


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

    def create(
        data: dict[str, list] | None = None,
        sampling: dict | None = None,
    ) -> dict:
        data = data or {"value": [1, 2, 3]}
        sampling = sampling or {"kind": "all"}
        token = uuid.uuid4().hex
        storage = LocalStorage(str(tmp_path / f"outputs-{token}"))
        stores.append(storage)
        catalog = InMemoryCatalog(
            str(tmp_path / f"data-{token}"), lambda _uri: DuckDBAdapter())
        logical_uri = str(tmp_path / "published" / f"{token}.parquet")
        run_id = uuid.uuid4().hex
        artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
        pq.write_table(pa.table(data), artifact)
        storage.commit_result(artifact, run_id)
        published = catalog.publish_managed_local_file_output(
            name=f"report-source-{token}", logical_uri=logical_uri, artifact_uri=artifact)
        assert storage.release_result(artifact, run_id) is True
        workspace = metadb.dataset_view_source_workspace(published["dataset_id"])
        view_id, placement_id = uuid.uuid4().hex, uuid.uuid4().hex
        provenance = None
        if sampling["kind"] == "reservoir":
            returned = min(int(sampling["size"]), len(next(iter(data.values()))))
            provenance = {
                "strategy": "reservoir", "seed": sampling["seed"],
                "requestedRows": sampling["size"], "scannedRows": len(next(iter(data.values()))),
                "returnedRows": returned, "totalRows": len(next(iter(data.values()))),
                "datasetIdentity": published["dataset_id"],
                "datasetRevision": published["revision_id"], "identity": "d" * 64,
                "limitations": ["The deterministic reservoir scanned the complete exact revision."],
            }
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
            "selectedColumns": list(data),
            "predicate": None,
            "sampling": sampling,
            "sampleProvenance": provenance,
            "retentionOwner": "core",
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "semanticSha256": "a" * 64,
            "definitionSha256": "b" * 64,
        })
        definition = definition.model_copy(update={
            "definition_sha256": distribution_reports._view_definition_digest(definition),
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
        create.storage = storage
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


def _report(claim: dict, **changes) -> dict:
    measured = int(changes.pop("rowCount", 3))
    document = {
        "schemaVersion": 1,
        "reportId": claim["report_id"],
        "taskId": claim["task"]["id"],
        "datasetViewId": claim["intent"]["datasetViewId"],
        "datasetId": claim["view_snapshot"]["datasetRef"]["datasetId"],
        "revisionId": claim["view_snapshot"]["datasetRef"]["revisionId"],
        "viewDefinitionSha256": claim["intent"]["viewDefinitionSha256"],
        "computationVersion": claim["intent"]["computationVersion"],
        "measuredRows": measured,
        "complete": True,
        "sampleProvenance": None,
        "limitations": ["test fixture"],
        "sections": [{
            "kind": "coverage_schema", "sectionId": "coverage-schema",
            "selectedColumnCount": 1, "reportedColumnCount": 1,
            "columns": [{"name": "value", "type": "int"}],
        }, {
            "kind": "missingness", "sectionId": "column-000-missingness",
            "columnName": "value", "missingCount": 0,
        }, {
            "kind": "numeric", "sectionId": "column-000-numeric",
            "columnName": "value", "count": measured, "nonFiniteCount": 0,
            "min": 1.0, "max": 3.0, "mean": 2.0, "stddev": 0.0,
            "quantiles": [
                {"probability": value, "value": 2.0}
                for value in (0.0, 0.25, 0.5, 0.75, 1.0)
            ],
            "histogram": [{
                "bucketId": "column-000-numeric-000", "lower": 1.0, "upper": 3.0,
                "count": measured, "upperInclusive": True,
            }],
        }],
    }
    document.update(changes)
    return document


def _expire_latest_attempt(task_id: str) -> None:
    with metadb.session() as session:
        attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == task_id,
        ).order_by(metadb.DurableTaskAttempt.attempt_number.desc()).limit(1))
        assert attempt is not None
        attempt.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=30)


def _complete_computed_report(view: dict) -> dict:
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    owner_token = f"test-compute-{uuid.uuid4().hex}"
    claim = distribution_reports.claim_distribution_report(
        admitted["task"]["id"], owner_token)
    assert claim is not None
    attempt = claim["task"]["attempts"][-1]
    document = distribution_report_tasks.compute_distribution_report(
        claim, distribution_report_tasks._LeaseState(time.monotonic() + 30))
    completed = distribution_reports.complete_distribution_report(
        task_id=claim["task"]["id"], attempt_id=attempt["id"],
        owner_token=owner_token, report=document)
    assert completed is not None and completed["report"] is not None
    return completed


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

    document = _report(second, rowCount=3)
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
        report={**document, "limitations": ["changed response-loss payload"]}) is None
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
    job = metadb.list_workspace_runs(
        metadb.DEFAULT_USER_ID, run_id=task_id)["items"][0]
    assert job["jobType"] == "distribution_report"
    assert job["distributionReport"] == {
        "reportId": completed["report_id"],
        "datasetViewId": view["id"],
        "computationVersion": "distribution-v1",
        "measuredRows": 3,
        "complete": True,
        "reportedColumnCount": 1,
        "deepLink": f"/distribution-reports/{completed['report_id']}",
    }
    assert "ownerToken" not in json.dumps(job)
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
    if entrypoint == "read":
        assert result is not None
        assert result["task"]["status"] == "failed"
        assert result["task"]["error"] == "distribution report revision unavailable"
    else:
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
        assert inbox.diagnostic_code == "distribution_report_revision_unavailable"


def test_report_read_truth_after_revision_loss_and_corruption(core_view_factory):
    active_view = core_view_factory()
    active, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(active_view))
    active_report_id = active["report_id"]

    completed_view = core_view_factory()
    completed, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(completed_view))
    completed_task_id, completed_report_id = (
        completed["task"]["id"], completed["report_id"])
    completed_claim = distribution_reports.claim_distribution_report(
        completed_task_id, "completed-owner")
    assert completed_claim is not None
    completed_attempt_id = completed_claim["task"]["attempts"][-1]["id"]
    terminal = distribution_reports.complete_distribution_report(
        task_id=completed_task_id, attempt_id=completed_attempt_id,
        owner_token="completed-owner", report=_report(completed_claim))
    assert terminal is not None and terminal["task"]["status"] == "done"

    corrupt_view = core_view_factory()
    corrupt, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(corrupt_view))
    corrupt_task_id, corrupt_report_id = corrupt["task"]["id"], corrupt["report_id"]
    corrupt_claim = distribution_reports.claim_distribution_report(
        corrupt_task_id, "corrupt-owner")
    assert corrupt_claim is not None
    corrupt_attempt_id = corrupt_claim["task"]["attempts"][-1]["id"]
    assert distribution_reports.complete_distribution_report(
        task_id=corrupt_task_id, attempt_id=corrupt_attempt_id,
        owner_token="corrupt-owner", report=_report(corrupt_claim)) is not None

    other = f"report-read-other-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=other, name="Other"))
        for report_id in (active_report_id, completed_report_id):
            reference = session.scalar(select(metadb.LocalResultReference).where(
                metadb.LocalResultReference.owner_kind == "distribution_report",
                metadb.LocalResultReference.owner_key == report_id))
            assert reference is not None
            session.delete(reference)
        corrupt_row = session.get(metadb.DistributionReportEnvelope, corrupt_task_id)
        assert corrupt_row is not None
        corrupt_row.report_doc = "{}"

    with TestClient(app, raise_server_exceptions=False) as client:
        active_response = client.get(f"/api/distribution-reports/{active_report_id}")
        assert active_response.status_code == 200, active_response.text
        assert active_response.json()["task"]["status"] == "failed"
        assert active_response.json()["task"]["error"] == (
            "distribution report revision unavailable")

        retained_response = client.get(
            f"/api/distribution-reports/{completed_report_id}")
        assert retained_response.status_code == 200, retained_response.text
        assert retained_response.json()["task"]["status"] == "done"
        assert retained_response.json()["report"]["reportId"] == completed_report_id

        corrupt_response = client.get(f"/api/distribution-reports/{corrupt_report_id}")
        assert corrupt_response.status_code == 500, corrupt_response.text
        assert corrupt_response.json() == {
            "detail": "Retained distribution report state is corrupt",
            "code": "internal_error",
            "retryable": False,
        }
        assert client.get(
            f"/api/distribution-reports/{corrupt_report_id}",
            headers={"X-DP-User": other}).status_code == 404


def test_api_computes_closed_bounded_sections_and_replays_submission(
    core_view_factory, monkeypatch,
):
    view = core_view_factory({
        "number": [1.0, None, float("nan"), 3.0],
        "category": ["a", "b", "a", None],
        "flag": [True, False, True, None],
        "observed_at": [
            datetime.datetime(2025, 1, 1), datetime.datetime(2025, 1, 2),
            None, datetime.datetime(2025, 1, 4),
        ],
        "payload": [b"a", b"b", b"c", None],
    })
    other = f"report-other-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=other, name="Other"))
    from hub.deps import get_deps
    deps = get_deps()
    monkeypatch.setattr(dataset_view_routes, "get_deps", lambda: SimpleNamespace(
        storage=getattr(core_view_factory, "storage"), resolve_adapter=deps.resolve_adapter))
    submission = str(uuid.uuid4())
    with TestClient(app) as client:
        estimate = client.post(
            f"/api/dataset-views/{view['id']}/distribution-reports/estimate")
        assert estimate.status_code == 200, estimate.text
        assert estimate.json()["estimatedScanRows"] == 4
        assert estimate.json()["needsConfirmation"] is False

        submitted = client.post(
            f"/api/dataset-views/{view['id']}/distribution-reports",
            json={"submissionId": submission, "confirmed": False})
        assert submitted.status_code == 201, submitted.text
        report_id = submitted.json()["reportId"]
        task_id = submitted.json()["task"]["id"]
        listed = client.get(
            f"/api/dataset-views/{view['id']}/distribution-reports")
        assert listed.status_code == 200
        assert [item["reportId"] for item in listed.json()] == [report_id]
        terminal = None
        for _ in range(100):
            response = client.get(f"/api/distribution-reports/{report_id}")
            assert response.status_code == 200, response.text
            terminal = response.json()
            if terminal["task"]["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(.02)
        assert terminal is not None and terminal["task"]["status"] == "done", terminal
        document = terminal["report"]
        assert document["measuredRows"] == 4 and document["complete"] is True
        assert document["datasetViewId"] == view["id"]
        sections = {section["kind"] + ":" + section.get("columnName", ""): section
                    for section in document["sections"]}
        assert sections["missingness:number"]["missingCount"] == 1
        numeric = sections["numeric:number"]
        assert numeric["count"] == 2 and numeric["nonFiniteCount"] == 1
        assert sum(bucket["count"] for bucket in numeric["histogram"]) == 2
        categorical = sections["categorical:category"]
        assert sum(item["count"] for item in categorical["top"]) == 3
        assert categorical["otherCount"] == 0
        assert sections["categorical:flag"]["distinctCountApproximate"] is False
        temporal = sections["temporal:observed_at"]
        assert sum(bucket["count"] for bucket in temporal["buckets"]) == 3
        assert sections["unsupported:payload"]["reason"] == "unsupported_type"

        replay = client.post(
            f"/api/dataset-views/{view['id']}/distribution-reports",
            json={"submissionId": submission, "confirmed": False})
        assert replay.status_code == 200 and replay.json()["reportId"] == report_id
        assert client.get(f"/api/run/{task_id}").json()["status"] == "done"
        jobs = client.get("/api/jobs", params={"run_id": task_id})
        assert jobs.status_code == 200, jobs.text
        assert jobs.json()["items"][0]["distributionReport"]["reportId"] == report_id
        headers = {"X-DP-User": other}
        assert client.get(
            f"/api/distribution-reports/{report_id}", headers=headers).status_code == 404
        assert client.get(
            "/api/jobs", params={"run_id": task_id}, headers=headers).json()["items"] == []

        monkeypatch.setenv(
            "DP_AUTH_SECRET", "distribution-report-auth-test-secret-0123456789")
        assert run_routes._run_read_access(task_id, metadb.DEFAULT_USER_ID) is True
        assert run_routes._run_mutate_access(task_id, metadb.DEFAULT_USER_ID) is True
        assert run_routes._run_read_access(task_id, other) is False
        assert run_routes._run_mutate_access(task_id, other) is False
        other_session = {"Cookie": f"dp_session={auth.sign(other)}"}
        assert client.get(f"/api/run/{task_id}", headers=other_session).status_code == 404
        assert client.post(
            f"/api/run/{task_id}/cancel", headers=other_session).status_code == 404
        assert client.post(
            f"/api/run/{task_id}/retry", headers=other_session,
            json={"actionId": str(uuid.uuid4())}).status_code == 404
        monkeypatch.delenv("DP_AUTH_SECRET")

        monkeypatch.setattr(distribution_report_tasks, "CONFIRM_ROWS", 1)
        confirmation = client.post(
            f"/api/dataset-views/{view['id']}/distribution-reports",
            json={"submissionId": str(uuid.uuid4()), "confirmed": False})
        assert confirmation.status_code == 409


def test_reservoir_report_is_exact_only_over_its_deterministic_sample(
    core_view_factory, monkeypatch,
):
    view = core_view_factory(
        {"value": list(range(100))},
        sampling={"kind": "reservoir", "size": 10, "seed": 42})
    from hub.deps import get_deps
    deps = get_deps()
    monkeypatch.setattr(dataset_view_routes, "get_deps", lambda: SimpleNamespace(
        storage=getattr(core_view_factory, "storage"), resolve_adapter=deps.resolve_adapter))
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    claim = distribution_reports.claim_distribution_report(
        admitted["task"]["id"], "sample-owner")
    assert claim is not None
    document = distribution_report_tasks.compute_distribution_report(
        claim, distribution_report_tasks._LeaseState(time.monotonic() + 30))
    parsed = distribution_reports.DistributionReportDocumentV1.model_validate(document)
    assert parsed.measured_rows == 10 and parsed.complete is False
    assert parsed.sample_provenance is not None
    assert parsed.sample_provenance.identity == view["sampleProvenance"]["identity"]
    assert any("no full-population claim" in item for item in parsed.limitations)
    numeric = next(section for section in parsed.sections if section.kind == "numeric")
    assert numeric.count == sum(bucket.count for bucket in numeric.histogram) == 10


def test_ephemeral_compare_never_invents_incompatible_deltas(core_view_factory):
    view = core_view_factory()
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    claim = distribution_reports.claim_distribution_report(
        admitted["task"]["id"], "compare-owner")
    assert claim is not None
    base = distribution_reports.DistributionReportDocumentV1.model_validate(_report(claim))

    equal = distribution_report_insights.compare_reports(base, base)
    assert equal.coverage.reason == "compatible_full_coverage"
    equal_column = equal.columns[0]
    assert equal_column.missing_count_delta == 0
    assert equal_column.metric_delta is not None
    equal_delta = equal_column.metric_delta.model_dump(by_alias=True)
    assert equal_delta["countDelta"] == 0
    assert equal_delta["meanDelta"] == 0
    assert equal_delta["histogramReason"] == "equal_edges"
    assert equal_delta["histogram"][0]["countDelta"] == 0

    unequal_edges_payload = base.model_dump(by_alias=True, mode="json")
    unequal_edges_payload["sections"][-1]["histogram"] = [{
        "bucketId": "right-0", "lower": 1.0, "upper": 2.0,
        "count": 1, "upperInclusive": False,
    }, {
        "bucketId": "right-1", "lower": 2.0, "upper": 3.0,
        "count": 2, "upperInclusive": True,
    }]
    unequal_edges = distribution_report_insights.compare_reports(
        base,
        distribution_reports.DistributionReportDocumentV1.model_validate(
            unequal_edges_payload),
    )
    unequal_delta = unequal_edges.columns[0].metric_delta
    assert unequal_delta is not None
    assert unequal_delta.model_dump(by_alias=True)["histogram"] is None
    assert unequal_delta.model_dump(by_alias=True)["histogramReason"] == "unequal_edges"

    incompatible_payload = base.model_dump(by_alias=True, mode="json")
    incompatible_payload["computationVersion"] = "distribution-v2"
    incompatible = distribution_report_insights.compare_reports(
        base,
        distribution_reports.DistributionReportDocumentV1.model_validate(
            incompatible_payload),
    )
    assert incompatible.columns[0].reason == "computation_version_mismatch"
    assert incompatible.columns[0].metric_delta is None

    sample_payload = base.model_dump(by_alias=True, mode="json")
    sample_payload["complete"] = False
    sample_payload["sampleProvenance"] = {
        "strategy": "reservoir", "seed": 7, "requestedRows": 3,
        "scannedRows": 3, "returnedRows": 3, "totalRows": 3,
        "datasetIdentity": base.dataset_id, "datasetRevision": base.revision_id,
        "identity": "e" * 64, "limitations": ["test sample"],
    }
    sampled = distribution_report_insights.compare_reports(
        base,
        distribution_reports.DistributionReportDocumentV1.model_validate(sample_payload),
    )
    assert sampled.coverage.reason == "full_sample_coverage_mismatch"
    assert sampled.columns[0].reason == "coverage_mismatch"
    assert sampled.columns[0].metric_delta is None

    left_category = base.model_dump(by_alias=True, mode="json")
    left_category["sections"] = [{
        "kind": "coverage_schema", "sectionId": "coverage-schema",
        "selectedColumnCount": 1, "reportedColumnCount": 1,
        "columns": [{"name": "category", "type": "string"}],
    }, {
        "kind": "missingness", "sectionId": "left-missing",
        "columnName": "category", "missingCount": 0,
    }, {
        "kind": "categorical", "sectionId": "left-category",
        "columnName": "category", "top": [
            {"bucketId": "left-a", "label": "a", "count": 1},
            {"bucketId": "left-b", "label": "b", "count": 1},
        ], "otherCount": 1, "distinctCount": 3, "distinctCountApproximate": True,
    }]
    right_category = json.loads(json.dumps(left_category))
    right_category["sections"][2].update({
        "top": [
            {"bucketId": "right-a", "label": "a", "count": 1},
            {"bucketId": "right-c", "label": "c", "count": 1},
        ],
    })
    categorical = distribution_report_insights.compare_reports(
        distribution_reports.DistributionReportDocumentV1.model_validate(left_category),
        distribution_reports.DistributionReportDocumentV1.model_validate(right_category),
    )
    assert categorical.columns[0].match_reason == "name_and_logical_type"
    category_delta = categorical.columns[0].metric_delta
    assert category_delta is not None
    categories = {
        item["label"]: item
        for item in category_delta.model_dump(by_alias=True)["categories"]
    }
    assert categories["b"]["rightCount"] is None
    assert categories["b"]["countDelta"] is None
    assert categories["b"]["reason"] == "outside_right_top_k"

    attempt = claim["task"]["attempts"][-1]
    completed = distribution_reports.complete_distribution_report(
        task_id=claim["task"]["id"], attempt_id=attempt["id"],
        owner_token="compare-owner", report=base.model_dump(by_alias=True, mode="json"))
    assert completed is not None
    other = f"compare-other-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=other, name="Other"))
    request = {"leftReportId": base.report_id, "rightReportId": base.report_id}
    with TestClient(app) as client:
        compared = client.post("/api/distribution-reports/compare", json=request)
        assert compared.status_code == 200, compared.text
        assert compared.json()["coverage"]["reason"] == "compatible_full_coverage"
        assert client.post(
            "/api/distribution-reports/compare", json=request,
            headers={"X-DP-User": other},
        ).status_code == 404


def test_retained_revision_field_identity_matches_a_real_cross_revision_rename(
    tmp_path, monkeypatch,
):
    storage = LocalStorage(str(tmp_path / "identity-outputs"))
    catalog = InMemoryCatalog(
        str(tmp_path / "identity-data"), lambda _uri: _DeclaredIdentityDuckDBAdapter())
    logical_uri = str(tmp_path / "identity-published" / "renamed.parquet")

    def publish(column: str, values: list[int]) -> dict:
        run_id = uuid.uuid4().hex
        artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
        pq.write_table(pa.table({column: values}), artifact)
        storage.commit_result(artifact, run_id)
        published = catalog.publish_managed_local_file_output(
            name=f"identity-{column}", logical_uri=logical_uri, artifact_uri=artifact)
        assert storage.release_result(artifact, run_id) is True
        return published

    from hub.deps import get_deps
    deps = get_deps()
    monkeypatch.setattr(dataset_view_routes, "get_deps", lambda: SimpleNamespace(
        storage=storage, resolve_adapter=deps.resolve_adapter))
    try:
        first_revision = publish("old_name", [1, 2, 3])
        with TestClient(app) as client:
            first_view_response = client.post("/api/dataset-views", json={
                "submissionId": uuid.uuid4().hex,
                "name": "Before rename",
                "datasetRef": {
                    "kind": "exact", "datasetId": first_revision["dataset_id"],
                    "revisionId": first_revision["revision_id"],
                },
                "selectedColumns": ["old_name"],
                "sampling": {"kind": "all"},
            })
            assert first_view_response.status_code == 201, first_view_response.text
            second_revision = publish("new_name", [1, 2, 3])
            assert second_revision["dataset_id"] == first_revision["dataset_id"]
            second_view_response = client.post("/api/dataset-views", json={
                "submissionId": uuid.uuid4().hex,
                "name": "After rename",
                "datasetRef": {
                    "kind": "exact", "datasetId": second_revision["dataset_id"],
                    "revisionId": second_revision["revision_id"],
                },
                "selectedColumns": ["new_name"],
                "sampling": {"kind": "all"},
            })
            assert second_view_response.status_code == 201, second_view_response.text

        first_definition = DatasetViewDefinitionV1.model_validate(first_view_response.json())
        assert first_definition.definition_sha256 == distribution_reports._view_definition_digest(
            first_definition), first_definition.model_dump(by_alias=True, mode="json")
        left = _complete_computed_report(first_view_response.json())
        right = _complete_computed_report(second_view_response.json())
        left_document = distribution_reports.DistributionReportDocumentV1.model_validate_json(
            json.dumps(left["report"]))
        right_document = distribution_reports.DistributionReportDocumentV1.model_validate_json(
            json.dumps(right["report"]))
        left_column = distribution_report_insights._coverage(left_document)[0]
        right_column = distribution_report_insights._coverage(right_document)[0]
        assert (left_column.name, right_column.name) == ("old_name", "new_name")
        assert left_column.field_id == right_column.field_id == "declared-field-0"
        comparison = distribution_report_insights.compare_reports(
            left_document, right_document)
        assert len(comparison.columns) == 1
        assert comparison.columns[0].match_reason == "stable_field_identity"
        assert comparison.columns[0].metric_delta is not None
        assert comparison.columns[0].metric_delta.model_dump(
            by_alias=True)["meanDelta"] == 0
    finally:
        storage.close()


def test_numeric_float_wire_boundaries_fail_closed_and_reconcile(core_view_factory, monkeypatch):
    hugeint_table = db.unique_view("numeric_boundary")
    con = db.conn()
    con.execute(f'CREATE TEMP TABLE "{hugeint_table}" (value HUGEINT)')
    try:
        con.execute(
            f'INSERT INTO "{hugeint_table}" VALUES (0), ({2**53 + 1}), (NULL)')
        hugeint = distribution_report_tasks._numeric_section(
            con, hugeint_table, "value", "HUGEINT", 0, 1)[0]
        assert hugeint == {
            "kind": "unsupported", "sectionId": "column-000-numeric",
            "columnName": "value", "reason": "numeric_precision_unsupported",
            "partial": True,
        }
    finally:
        con.execute(f'DROP TABLE "{hugeint_table}"')

    view = core_view_factory({
        "safe_bigint": [-(2**53), 0, 2**53, None],
        "unsafe_bigint": [0, 1, 2**53 + 1, None],
        "decimal_value": [Decimal("0.1"), Decimal("0.5"), Decimal("1.0"), None],
        "constant_extreme": [sys.float_info.max, sys.float_info.max, sys.float_info.max, None],
        "extreme_span": [-sys.float_info.max, 0.0, sys.float_info.max, None],
        "stddev_overflow": [0.0, 1e200, 1e200, None],
        "subnormal_width": [0.0, 0.0, 5e-324, None],
    })
    from hub.deps import get_deps
    deps = get_deps()
    monkeypatch.setattr(dataset_view_routes, "get_deps", lambda: SimpleNamespace(
        storage=getattr(core_view_factory, "storage"), resolve_adapter=deps.resolve_adapter))
    admitted, _created = distribution_reports.admit_distribution_report(
        owner_id=metadb.DEFAULT_USER_ID, intent=_intent(view))
    claim = distribution_reports.claim_distribution_report(
        admitted["task"]["id"], "numeric-boundary-owner")
    assert claim is not None
    document = distribution_report_tasks.compute_distribution_report(
        claim, distribution_report_tasks._LeaseState(time.monotonic() + 30))
    parsed = distribution_reports.DistributionReportDocumentV1.model_validate(document)

    by_column = {
        section.column_name: section
        for section in parsed.sections if getattr(section, "column_name", None) is not None
    }
    safe = by_column["safe_bigint"]
    assert safe.kind == "numeric"
    assert safe.count + safe.non_finite_count + 1 == parsed.measured_rows
    assert sum(bucket.count for bucket in safe.histogram) == safe.count
    constant = by_column["constant_extreme"]
    assert constant.kind == "numeric"
    assert constant.min == constant.max == constant.mean == sys.float_info.max
    assert constant.stddev == 0
    expected_reasons = {
        "unsafe_bigint": "numeric_precision_unsupported",
        "decimal_value": "numeric_precision_unsupported",
        "extreme_span": "numeric_range_unsupported",
        "stddev_overflow": "numeric_range_unsupported",
        "subnormal_width": "numeric_range_unsupported",
    }
    for column, reason in expected_reasons.items():
        section = by_column[column]
        assert section.kind == "unsupported"
        assert section.reason == reason and section.partial is True
        missing = next(
            item for item in parsed.sections
            if item.kind == "missingness" and item.column_name == column)
        assert missing.missing_count == 1


def test_deadline_monitor_interrupts_an_active_query(monkeypatch):
    interrupted = threading.Event()
    done = threading.Event()
    state = distribution_report_tasks._LeaseState(
        deadline_at=time.monotonic() - 1, interrupt=interrupted.set)
    monkeypatch.setattr(
        distribution_reports, "heartbeat_distribution_report", lambda *_args: True)
    monkeypatch.setattr(
        distribution_reports, "distribution_report_should_stop", lambda *_args: False)
    monitor = threading.Thread(
        target=distribution_report_tasks._lease_monitor,
        args=("task", "attempt", "owner", state, done), daemon=True)
    monitor.start()
    assert interrupted.wait(timeout=2)
    assert state.deadline is True
    done.set()
    monitor.join(timeout=2)
    assert monitor.is_alive() is False


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
