from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hub import metadb
from hub.api_errors import api_error_response
from hub.contracts.openapi import render_openapi
from hub.models import (
    ColumnSchema, LineagePublication, TemporalResampleTaskResponseV1, TemporalResampleWriteRequestV1,
    WriteDestination, WriteIntent, WriteProvenance,
)
from hub.routers.temporal_resample_tasks import _error
from hub.temporal_resample_tasks import (
    TemporalResampleAdmissionError, _TemporalCandidateChanged, _worker_diagnostic, public_task,
)
from hub.temporal_resample_diagnostics import TemporalResampleDiagnosticCode


@pytest.fixture(autouse=True)
def isolated_metadata(tmp_path):
    from hub.settings import settings
    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'temporal-task.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


def _request(*, submission: str = "submission-1", tolerance: str = "0", key: str | None = None,
             compound: str = "fixture-compound-timeline") -> TemporalResampleWriteRequestV1:
    key = key or f"temporal-task:{submission}"
    write = WriteIntent(
        destination=WriteDestination(logical_uri=f"/tmp/{submission}.parquet", name="derived"),
        mode="create", idempotency_key=key,
        provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]),
    )
    return TemporalResampleWriteRequestV1(
        submissionId=submission, compoundDatasetId=compound, compoundRevisionId="a" * 64,
        episodeId="episode-1", sourceStreamId="source", targetStreamId="target",
        outputStreamId="derived", outputMemberId="derived-member", sourceViewId="view-source",
        targetViewId="view-target", window={"timeField": "tick", "timeDomain": "reference",
                                               "startTick": "0", "endTick": "10"},
        toleranceTicks=tolerance, selectedFields=[{"field": "value", "unit": "m"}], writeIntent=write,
    )


@pytest.fixture
def real_case(tmp_path):
    from hub import deps as deps_module
    from hub.compound_fixture import current_user_fixture_reference
    from hub.deps import set_workspace
    from hub.temporal_resample import _output_schema
    from hub.tests.test_temporal_resample import _spec

    previous = deps_module._deps
    owner = f"temporal-owner-{uuid.uuid4().hex}"
    root = tmp_path / "workspace"
    deps = set_workspace(str(root), str(root / "data"), maintain_storage=False)
    with metadb.session() as session:
        session.add(metadb.User(id=owner, name=owner))
    authority = current_user_fixture_reference(owner)
    spec = _spec(authority.manifest)
    canonical_types = dict(metadb._temporal_expected_output_schema_for(authority.manifest, spec))
    expected_schema = [ColumnSchema(
        name=item["name"], type=canonical_types[item["name"]], nullable=item["nullable"])
        for item in _output_schema(authority.manifest, spec)]
    submission = f"temporal-{uuid.uuid4().hex}"
    key = f"temporal-write:{uuid.uuid4().hex}"
    write = WriteIntent(
        destination=WriteDestination(
            logical_uri=str(root / "derived-episode-1.parquet"), name="derived episode 1"),
        mode="create", expected_schema=expected_schema, idempotency_key=key,
        provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]),
    )
    request = TemporalResampleWriteRequestV1(
        submissionId=submission,
        compoundDatasetId=authority.manifest.ref.dataset_id,
        compoundRevisionId=authority.manifest.ref.revision_id,
        episodeId="episode-1", sourceStreamId="numeric-sensor",
        targetStreamId="target-observations", outputStreamId="derived-resample",
        outputMemberId="derived-resample-member",
        sourceViewId=authority.views["numeric-sensor"].id,
        targetViewId=authority.views["target-observations"].id,
        window={"timeField": "reference_tick", "timeDomain": "reference",
                "startTick": "0", "endTick": "10000"},
        toleranceTicks="1", selectedFields=[{
            "field": "value", "unit": "arbitrary fixture units"}],
        candidateCap=10_000, outputCap=10_000, writeIntent=write,
    )
    case = SimpleNamespace(
        root=root, deps=deps, owner=owner, authority=authority, request=request,
        request_doc=request.model_dump(by_alias=True, mode="json"),
    )
    try:
        yield case
    finally:
        case.deps.storage.close()
        deps_module._deps = previous


def _admit_real(case, *, max_attempts: int = 3):
    from hub.temporal_resample_tasks import candidate_for_request

    _manifest, candidate = candidate_for_request(case.owner, case.request)
    task, created = metadb.submit_temporal_resample_task(
        uid=case.owner, request=case.request, candidate_sha256=candidate.digest,
        max_attempts=max_attempts)
    return task, created, candidate


def _temporal_counts(owner: str) -> tuple[int, int, int, int]:
    with metadb.session() as session:
        publications = int(session.scalar(select(func.count()).select_from(
            metadb.TemporalResamplePublication).where(
                metadb.TemporalResamplePublication.owner_id == owner)) or 0)
        revisions = int(session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalFileRevision).join(
                metadb.TemporalResamplePublication,
                metadb.TemporalResamplePublication.output_revision_id
                == metadb.ManagedLocalFileRevision.revision_id).where(
                    metadb.TemporalResamplePublication.owner_id == owner)) or 0)
        compound = int(session.scalar(select(func.count()).select_from(
            metadb.CompoundDatasetRevision).where(
                metadb.CompoundDatasetRevision.owner_id == owner)) or 0)
        heads = int(session.scalar(select(func.count()).select_from(
            metadb.CompoundDatasetHead).where(
                metadb.CompoundDatasetHead.owner_id == owner)) or 0)
    return revisions, publications, compound, heads


def _staged_result_files(case) -> list[str]:
    return sorted(
        name for name in os.listdir(case.deps.storage.result_root)
        if name.endswith(".parquet"))


def test_temporal_task_admission_replays_or_conflicts_without_publication():
    owner = f"temporal-task-owner-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=owner, name=owner))
    submission = f"submission-{uuid.uuid4().hex}"
    request = _request(submission=submission)
    task, created = metadb.submit_temporal_resample_task(
        uid=owner, request=request, candidate_sha256="b" * 64)
    replay, replay_created = metadb.submit_temporal_resample_task(
        uid=owner, request=request, candidate_sha256="b" * 64)
    assert created is True and replay_created is False and replay["id"] == task["id"]
    assert task["status"] == "queued" and task["output_receipt"] is None
    assert metadb.temporal_resample_task_result(task["id"]) is None
    with pytest.raises(metadb.DurableTaskSubmissionConflict):
        metadb.submit_temporal_resample_task(
            uid=owner, request=_request(submission=submission, tolerance="1"),
            candidate_sha256="b" * 64)
    with metadb.session() as session:
        envelope = session.get(metadb.TemporalResampleTaskEnvelope, task["id"])
        assert envelope is not None and json.loads(envelope.request_doc)["submissionId"] == submission
    assert _temporal_counts(owner) == (0, 0, 0, 0)
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_temporal_resample_task(task["id"], "test-cleanup") is None


def test_temporal_submission_and_write_key_are_single_task_scoped():
    owner = f"temporal-task-owner-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=owner, name=owner))
    submission = f"submission-{uuid.uuid4().hex}"
    write_key = f"temporal-task:{submission}"
    task, _ = metadb.submit_temporal_resample_task(
        uid=owner, request=_request(submission=submission, key=write_key), candidate_sha256="b" * 64)
    with pytest.raises(metadb.DurableTaskSubmissionConflict, match="frozen admission"):
        metadb.submit_temporal_resample_task(
            uid=owner, request=_request(
                submission=submission, key=write_key, compound="another-compound"),
            candidate_sha256="b" * 64)
    with pytest.raises(metadb.DurableTaskSubmissionConflict, match="write key"):
        metadb.submit_temporal_resample_task(
            uid=owner, request=_request(submission=f"submission-{uuid.uuid4().hex}", key=write_key),
            candidate_sha256="b" * 64)
    assert metadb.durable_task(task["id"])["status"] == "queued"
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_temporal_resample_task(task["id"], "test-cleanup") is None


def test_temporal_public_status_redacts_physical_write_receipt():
    raw_receipt = {
        "datasetId": "dataset", "revisionId": "revision", "parentHead": None,
        "head": {"datasetId": "dataset", "revisionId": "revision", "retentionOwner": "core"},
        "rows": 3, "bytes": 20, "schema": [{"name": "tick", "type": "int"}], "durable": True,
        "publication": {"provider": "managed-local-file", "logicalUri": "/private/logical",
                        "artifactUri": "/private/artifact", "publishSequence": 1,
                        "idempotencyKey": "secret-key"},
        "provenance": {"publication": {"idempotencyKey": "secret-key", "provenance": "manual"},
                       "parents": []},
    }
    task = {
        "id": "task", "status": "done", "cancel_requested": False, "retry_count": 0, "max_attempts": 3,
        "error": None,
        "temporal_resample_result": {"receipt": raw_receipt, "evidence": {
            "schemaVersion": 1, "computationVersion": "temporal-resample-v1", "spec": {},
            "sourcePointsSha256": "a" * 64, "targetPointsSha256": "b" * 64,
            "sourcePointCount": 3, "targetPointCount": 3, "matchedCount": 3,
            "unmatchedTargetCount": 0, "gapTargetObservationIds": [], "signedDeltaTicks": {},
            "absoluteDeltaTicks": {}, "complete": True,
        }, "child": {"datasetId": "compound", "revisionId": "child"}},
    }
    response = public_task(task)
    assert isinstance(response, TemporalResampleTaskResponseV1)
    public = response.model_dump(by_alias=True, mode="json")
    assert public["receipt"]["datasetId"] == "dataset"
    assert public["receipt"]["revisionId"] == "revision"
    assert public["receipt"]["head"]["retentionOwner"] == "core"
    assert public["receipt"]["schema"][0]["name"] == "tick"
    assert public["receipt"]["durable"] is True
    assert "artifactUri" not in json.dumps(public)
    assert "idempotencyKey" not in json.dumps(public)


@pytest.mark.parametrize(("code", "status", "retryable"), [
    (TemporalResampleDiagnosticCode.PERMISSION_DENIED, 403, False),
    (TemporalResampleDiagnosticCode.PROVIDER_OFFLINE, 503, True),
    (TemporalResampleDiagnosticCode.REVISION_UNAVAILABLE, 410, False),
    (TemporalResampleDiagnosticCode.INPUT_CORRUPT, 422, False),
    (TemporalResampleDiagnosticCode.INPUT_TRUNCATED, 422, False),
    (TemporalResampleDiagnosticCode.INPUT_DUPLICATE, 422, False),
])
def test_temporal_admission_exposes_exact_machine_diagnostic(code, status, retryable):
    error = _error(TemporalResampleAdmissionError(code))

    assert error.status_code == status
    assert error.code is code
    assert error.retryable is retryable
    assert error.detail == "temporal resample admission rejected"
    wire = json.loads(api_error_response(
        status_code=error.status_code, detail=error.detail, code=error.code,
        retryable=error.retryable).body)
    assert wire == {
        "detail": "temporal resample admission rejected", "code": code.value,
        "retryable": retryable,
    }


@pytest.mark.parametrize(("error", "expected"), [
    (TemporalResampleAdmissionError(TemporalResampleDiagnosticCode.PERMISSION_DENIED),
     TemporalResampleDiagnosticCode.PERMISSION_DENIED),
    (TemporalResampleAdmissionError(TemporalResampleDiagnosticCode.PROVIDER_OFFLINE),
     TemporalResampleDiagnosticCode.PROVIDER_OFFLINE),
    (TemporalResampleAdmissionError(TemporalResampleDiagnosticCode.REVISION_UNAVAILABLE),
     TemporalResampleDiagnosticCode.EXACT_REVISION_LOST),
    (TemporalResampleAdmissionError(TemporalResampleDiagnosticCode.INPUT_CORRUPT),
     TemporalResampleDiagnosticCode.INPUT_CORRUPT),
    (TemporalResampleAdmissionError(TemporalResampleDiagnosticCode.INPUT_TRUNCATED),
     TemporalResampleDiagnosticCode.INPUT_TRUNCATED),
    (TemporalResampleAdmissionError(TemporalResampleDiagnosticCode.INPUT_DUPLICATE),
     TemporalResampleDiagnosticCode.INPUT_DUPLICATE),
    (_TemporalCandidateChanged(), TemporalResampleDiagnosticCode.EXACT_REVISION_LOST),
    (RuntimeError("provider /private/path failed"), TemporalResampleDiagnosticCode.PUBLICATION_FAILED),
])
def test_temporal_worker_diagnostic_is_finite_and_redacted(error, expected):
    assert _worker_diagnostic(error) is expected


@pytest.mark.parametrize("code", list(TemporalResampleDiagnosticCode))
def test_temporal_failed_status_uses_the_shared_finite_diagnostic(code):
    response = public_task({
        "id": "task", "status": "failed", "cancel_requested": False,
        "retry_count": 0, "max_attempts": 3, "error": code.value,
    })

    assert response.diagnostic_code is code
    assert response.model_dump(by_alias=True, mode="json")["diagnosticCode"] == code.value
    assert response.can_retry is (code in {
        TemporalResampleDiagnosticCode.PROVIDER_OFFLINE,
        TemporalResampleDiagnosticCode.PUBLICATION_FAILED,
    })


def test_temporal_failed_status_rejects_unknown_diagnostic():
    with pytest.raises(ValueError):
        public_task({
            "id": "task", "status": "failed", "cancel_requested": False,
            "retry_count": 0, "max_attempts": 3, "error": "provider /private/path failed",
        })


def test_openapi_exposes_the_finite_temporal_diagnostic_contract():
    contract = json.loads(render_openapi())
    schemas = contract["components"]["schemas"]

    assert schemas["TemporalResampleDiagnosticCode"]["enum"] == [
        code.value for code in TemporalResampleDiagnosticCode]
    assert {item.get("$ref") for item in schemas["APIErrorResponse"]["properties"]["code"]["anyOf"]} >= {
        "#/components/schemas/TemporalResampleDiagnosticCode",
    }
    assert {item.get("$ref") for item in
            schemas["TemporalResampleTaskResponseV1"]["properties"]["diagnosticCode"]["anyOf"]} >= {
        "#/components/schemas/TemporalResampleDiagnosticCode",
    }


def test_public_fixture_submit_publishes_exact_sanitized_result_and_replays(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks
    from hub.compound_datasets import open_compound_manifest
    from hub.main import app
    from hub.routers import temporal_resample_tasks as task_router
    from hub.temporal_resample import _manifest_document

    monkeypatch.setattr(task_router, "dispatch", lambda task_id, deps: tasks._worker(task_id, deps))
    headers = {"X-DP-User": real_case.owner}
    parent_before = _manifest_document(real_case.authority.manifest)
    with TestClient(app) as client:
        submitted = client.post(
            "/api/temporal-resample-write", headers=headers, json=real_case.request_doc)
        assert submitted.status_code == 200
        body = submitted.json()
        assert body["status"] == "done"
        task_id = body["taskId"]
        assert body["receipt"]["rows"] == 7
        assert body["receipt"]["durable"] is True
        assert body["evidence"] == {
            **body["evidence"],
            "sourcePointCount": 6, "targetPointCount": 7, "matchedCount": 6,
            "unmatchedTargetCount": 1,
            "gapTargetObservationIds": ["episode-1-target-007"],
            "signedDeltaTicks": {"count": 6, "minimum": 0, "maximum": 0},
            "absoluteDeltaTicks": {"count": 6, "minimum": 0, "maximum": 0},
            "complete": True,
        }
        encoded = json.dumps(body)
        for private_key in ("artifactUri", "logicalUri", "idempotencyKey", "provider", "credentials"):
            assert private_key not in encoded

        refetched = client.get(f"/api/temporal-resample-write/{task_id}", headers=headers)
        replayed = client.post(
            "/api/temporal-resample-write", headers=headers, json=real_case.request_doc)
        assert refetched.status_code == replayed.status_code == 200
        assert refetched.json() == replayed.json() == body
        unauthorized = client.get(
            f"/api/temporal-resample-write/{task_id}", headers={"X-DP-User": "another-user"})
        assert unauthorized.status_code == 404
        assert unauthorized.json()["code"] == "not_found"
        assert task_id not in unauthorized.text
        late_cancel = client.post(
            f"/api/temporal-resample-write/{task_id}/cancel", headers=headers)
        assert late_cancel.status_code == 200
        assert late_cancel.json() == body

    result = metadb.temporal_resample_task_result(task_id)
    assert result is not None
    receipt = result["receipt"]
    artifact = metadb.managed_local_file_revision_artifact(
        receipt["datasetId"], receipt["revisionId"])
    assert artifact is not None
    rows = pq.read_table(artifact).to_pylist()
    assert [(row["observation_id"], row["source_observation_id"], row["source_tick"],
             row["mapped_source_tick"], row["signed_delta_ticks"], row["value"])
            for row in rows] == [
        ("episode-1-target-001", "episode-1-sensor-001", 1_000_000, 876, 0, 0.125),
        ("episode-1-target-002", "episode-1-sensor-002", 2_000_000, 1_877, 0, 0.25),
        ("episode-1-target-003", "episode-1-sensor-003", 3_000_000, 2_878, 0, 0.375),
        ("episode-1-target-004", "episode-1-sensor-004", 7_000_000, 6_882, 0, 0.625),
        ("episode-1-target-005", "episode-1-sensor-005", 8_000_000, 7_883, 0, 0.75),
        ("episode-1-target-006", "episode-1-sensor-006", 9_000_000, 8_884, 0, 0.875),
        ("episode-1-target-007", None, None, None, None, None),
    ]
    child_id = result["child"]["revisionId"]
    with metadb.session() as session:
        parent = session.get(metadb.CompoundDatasetRevision, {
            "owner_id": real_case.owner,
            "dataset_id": real_case.authority.manifest.ref.dataset_id,
            "revision_id": real_case.authority.manifest.ref.revision_id,
        })
        child_row = session.get(metadb.CompoundDatasetRevision, {
            "owner_id": real_case.owner,
            "dataset_id": real_case.authority.manifest.ref.dataset_id,
            "revision_id": child_id,
        })
        head = session.get(metadb.CompoundDatasetHead, {
            "owner_id": real_case.owner,
            "dataset_id": real_case.authority.manifest.ref.dataset_id,
        })
        assert parent is not None and json.loads(parent.manifest_doc) == parent_before
        assert child_row is not None and child_row.parent_revision_id == parent.revision_id
        assert head is not None and head.revision_id == child_id
        child = open_compound_manifest(child_row.manifest_doc.encode())
    assert [member for member in child.members if member.id != "derived-resample-member"] == list(
        real_case.authority.manifest.members)
    assert _manifest_document(real_case.authority.manifest) == parent_before


def test_bad_expected_schema_fails_public_preflight_without_task_or_output(real_case):
    from hub.main import app

    columns = list(real_case.request.write_intent.expected_schema)
    columns[0] = columns[0].model_copy(update={"type": "float"})
    bad_write = real_case.request.write_intent.model_copy(update={"expected_schema": columns})
    bad_request = real_case.request.model_copy(update={"write_intent": bad_write})
    with TestClient(app) as client:
        response = client.post(
            "/api/temporal-resample-write", headers={"X-DP-User": real_case.owner},
            json=bad_request.model_dump(by_alias=True, mode="json"))

    assert response.status_code == 422
    assert response.json() == {
        "detail": "temporal resample admission rejected",
        "code": "temporal_spec_invalid", "retryable": False,
    }
    with metadb.session() as session:
        assert int(session.scalar(select(func.count()).select_from(metadb.DurableTask).where(
            metadb.DurableTask.owner_id == real_case.owner)) or 0) == 0
    assert _temporal_counts(real_case.owner) == (0, 0, 0, 0)


def test_existing_output_member_fails_public_preflight_without_task_or_output(real_case):
    from hub.main import app

    bad_request = real_case.request.model_copy(update={
        "output_member_id": real_case.authority.manifest.members[0].id,
    })
    with TestClient(app) as client:
        response = client.post(
            "/api/temporal-resample-write", headers={"X-DP-User": real_case.owner},
            json=bad_request.model_dump(by_alias=True, mode="json"))

    assert response.status_code == 422
    assert response.json() == {
        "detail": "temporal resample admission rejected",
        "code": "temporal_spec_invalid", "retryable": False,
    }
    with metadb.session() as session:
        assert int(session.scalar(select(func.count()).select_from(metadb.DurableTask).where(
            metadb.DurableTask.owner_id == real_case.owner)) or 0) == 0
    assert _temporal_counts(real_case.owner) == (0, 0, 0, 0)


@pytest.mark.parametrize(("field", "value"), [
    ("submission_id", "bad\x00submission"),
    ("submission_id", " surrounding-space "),
    ("output_stream_id", "bad/output"),
    ("output_member_id", "bad/output"),
])
def test_temporal_request_rejects_noncanonical_persisted_identities(field, value):
    alias = {
        "submission_id": "submissionId", "output_stream_id": "outputStreamId",
        "output_member_id": "outputMemberId",
    }[field]
    document = _request().model_dump(by_alias=True, mode="json")
    document[alias] = value
    with pytest.raises(ValueError):
        TemporalResampleWriteRequestV1.model_validate(document)


def test_restart_recovery_reopens_exact_views_and_converges_without_polling(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks
    from hub.deps import set_workspace

    task, created, _candidate = _admit_real(real_case)
    assert created is True and task["status"] == "queued"
    real_case.deps.storage.close()
    real_case.deps = set_workspace(
        str(real_case.root), str(real_case.root / "data"), maintain_storage=False)
    dispatched: list[str] = []

    def run_recovered(task_id, deps):
        dispatched.append(task_id)
        tasks._worker(task_id, deps)

    monkeypatch.setattr(tasks, "dispatch", run_recovered)
    tasks.recover(real_case.deps)

    recovered = metadb.durable_task(task["id"])
    assert dispatched == [task["id"]]
    assert recovered is not None and recovered["status"] == "done"
    assert recovered["temporal_resample_result"] is not None
    assert _temporal_counts(real_case.owner) == (1, 1, 2, 1)
    tasks.recover(real_case.deps)
    assert dispatched == [task["id"]]


def test_transient_recompute_failure_retries_once_and_retry_request_replays(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks
    from hub.main import app
    from hub.routers import temporal_resample_tasks as task_router

    task, _created, _candidate = _admit_real(real_case)
    original_candidate = tasks.candidate_for_request
    monkeypatch.setattr(
        tasks, "candidate_for_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TemporalResampleAdmissionError(
            TemporalResampleDiagnosticCode.PROVIDER_OFFLINE)))
    tasks._worker(task["id"], real_case.deps)
    failed = public_task(metadb.durable_task(task["id"])).model_dump(by_alias=True, mode="json")
    assert failed["status"] == "failed"
    assert failed["diagnosticCode"] == "temporal_provider_offline"
    assert failed["canRetry"] is True
    assert _temporal_counts(real_case.owner) == (0, 0, 0, 0)

    monkeypatch.setattr(tasks, "candidate_for_request", original_candidate)
    monkeypatch.setattr(task_router, "dispatch", lambda task_id, deps: tasks._worker(task_id, deps))
    retry_doc = {"retryRequestId": f"retry-{uuid.uuid4().hex}"}
    headers = {"X-DP-User": real_case.owner}
    with TestClient(app) as client:
        retried = client.post(
            f"/api/temporal-resample-write/{task['id']}/retry", headers=headers, json=retry_doc)
        replayed = client.post(
            f"/api/temporal-resample-write/{task['id']}/retry", headers=headers, json=retry_doc)
    assert retried.status_code == replayed.status_code == 200
    assert retried.json() == replayed.json()
    assert retried.json()["status"] == "done"
    final = metadb.durable_task(task["id"])
    assert final is not None and len(final["attempts"]) == 2
    assert _temporal_counts(real_case.owner) == (1, 1, 2, 1)


def test_committed_publication_wins_when_worker_loses_the_response(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks

    task, _created, _candidate = _admit_real(real_case)
    original_write = tasks.write_managed_local_file

    def lose_response(**kwargs):
        original_write(**kwargs)
        raise RuntimeError("injected response loss after commit")

    monkeypatch.setattr(tasks, "write_managed_local_file", lose_response)
    tasks._worker(task["id"], real_case.deps)
    committed = metadb.durable_task(task["id"])
    assert committed is not None and committed["status"] == "done"
    first_result = committed["temporal_resample_result"]
    assert first_result is not None
    assert _temporal_counts(real_case.owner) == (1, 1, 2, 1)

    tasks._worker(task["id"], real_case.deps)
    replay, created, _candidate = _admit_real(real_case)
    assert created is False
    assert replay["temporal_resample_result"] == first_result
    assert _temporal_counts(real_case.owner) == (1, 1, 2, 1)


def test_cancel_at_publication_fence_exposes_no_output_or_child(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks

    task, _created, _candidate = _admit_real(real_case)
    original_fence = tasks._publish_fence
    fenced = threading.Event()

    def cancel_before_publish(task_id, attempt_id, token):
        assert metadb.request_durable_task_cancel(task_id) is not None
        fenced.set()
        original_fence(task_id, attempt_id, token)

    monkeypatch.setattr(tasks, "_publish_fence", cancel_before_publish)
    tasks._worker(task["id"], real_case.deps)

    cancelled = metadb.durable_task(task["id"])
    assert fenced.is_set()
    assert cancelled is not None and cancelled["status"] == "cancelled"
    assert cancelled["temporal_resample_result"] is None
    assert _temporal_counts(real_case.owner) == (0, 0, 1, 1)
    assert _staged_result_files(real_case) == []


def test_failure_before_managed_write_exposes_no_output_or_child(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks

    task, _created, _candidate = _admit_real(real_case)
    monkeypatch.setattr(
        tasks, "write_managed_local_file",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("injected pre-publication failure")))
    tasks._worker(task["id"], real_case.deps)

    failed = metadb.durable_task(task["id"])
    assert failed is not None and failed["status"] == "failed"
    assert failed["error"] == "temporal_publication_failed"
    assert failed["temporal_resample_result"] is None
    assert _temporal_counts(real_case.owner) == (0, 0, 1, 1)
    assert _staged_result_files(real_case) == []


def test_lost_task_lease_fences_old_publication_without_partial_output(real_case, monkeypatch):
    from hub import temporal_resample_tasks as tasks

    task, _created, _candidate = _admit_real(real_case)
    original_write = tasks.write_managed_local_file
    replacement: dict = {}

    def replace_owner_before_publish(**kwargs):
        current = metadb.durable_task(task["id"])
        assert current is not None
        stale_attempt = current["attempts"][-1]
        with metadb.session() as session:
            attempt = session.get(metadb.DurableTaskAttempt, stale_attempt["id"])
            assert attempt is not None
            attempt.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        claimed = metadb.claim_temporal_resample_task(task["id"], "replacement-owner")
        assert claimed is not None
        replacement.update(claimed["attempts"][-1])
        return original_write(**kwargs)

    monkeypatch.setattr(tasks, "write_managed_local_file", replace_owner_before_publish)
    tasks._worker(task["id"], real_case.deps)

    raced = metadb.durable_task(task["id"])
    assert raced is not None and raced["status"] == "running"
    assert [attempt["status"] for attempt in raced["attempts"]] == ["fenced", "running"]
    assert raced["temporal_resample_result"] is None
    assert _temporal_counts(real_case.owner) == (0, 0, 1, 1)
    assert _staged_result_files(real_case) == []
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.finish_durable_task_attempt(
        task["id"], replacement["id"], "replacement-owner", {
            "run_id": task["id"], "status": "cancelled", "target_node_id": "temporal-resample"})


def test_moved_compound_parent_fails_task_with_stable_stale_diagnostic(real_case):
    from hub import temporal_resample_tasks as tasks
    from hub.main import app
    from hub.temporal_publication import publish_candidate, register_parent

    task, _created, candidate = _admit_real(real_case)
    advance_key = f"advance-parent:{uuid.uuid4().hex}"
    advance_intent = WriteIntent(
        destination=WriteDestination(
            logical_uri=str(real_case.root / "advance-parent.parquet"), name="advance parent"),
        mode="create", expected_schema=real_case.request.write_intent.expected_schema,
        idempotency_key=advance_key,
        provenance=WriteProvenance(publication=LineagePublication(
            idempotency_key=advance_key, provenance="manual"), parents=[]),
    )
    table = tasks._table(real_case.authority.manifest, candidate)
    register_parent(owner_id=real_case.owner, manifest=real_case.authority.manifest)
    advanced = publish_candidate(
        storage=real_case.deps.storage, catalog=real_case.deps.catalog,
        owner_id=real_case.owner, parent_manifest=real_case.authority.manifest,
        candidate=candidate, output_member_id="advance-parent-member", intent=advance_intent,
        write_artifact=lambda uri: pq.write_table(table, uri))

    tasks._worker(task["id"], real_case.deps)
    failed = metadb.durable_task(task["id"])
    assert failed is not None and failed["status"] == "failed"
    public = public_task(failed)
    assert public.diagnostic_code is TemporalResampleDiagnosticCode.STALE_PARENT
    assert public.can_retry is False
    assert failed["temporal_resample_result"] is None
    assert _temporal_counts(real_case.owner) == (1, 1, 2, 1)
    with metadb.session() as session:
        head = session.get(metadb.CompoundDatasetHead, {
            "owner_id": real_case.owner,
            "dataset_id": real_case.authority.manifest.ref.dataset_id,
        })
        own_publication = session.get(
            metadb.TemporalResamplePublication,
            real_case.request.write_intent.idempotency_key)
        assert head is not None and head.revision_id == advanced.child["revisionId"]
        assert own_publication is None
    with TestClient(app) as client:
        retry = client.post(
            f"/api/temporal-resample-write/{task['id']}/retry",
            headers={"X-DP-User": real_case.owner},
            json={"retryRequestId": f"retry-{uuid.uuid4().hex}"})
    assert retry.status_code == 409
    assert retry.json() == {
        "detail": "temporal resample failure requires a new submission",
        "code": "conflict", "retryable": False,
    }
    retained = metadb.durable_task(task["id"])
    assert retained is not None and retained["status"] == "failed"
    assert len(retained["attempts"]) == 1


def _exercise_concurrent_temporal_submission(real_case):
    from hub.temporal_resample_tasks import candidate_for_request

    _manifest, candidate = candidate_for_request(real_case.owner, real_case.request)

    def race(requests):
        barrier = threading.Barrier(2)

        def submit(request):
            barrier.wait(timeout=10)
            try:
                return metadb.submit_temporal_resample_task(
                    uid=real_case.owner, request=request, candidate_sha256=candidate.digest)
            except metadb.DurableTaskSubmissionConflict as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(submit, requests))

    identical = race((real_case.request, real_case.request))
    assert all(isinstance(outcome, tuple) for outcome in identical)
    assert len({outcome[0]["id"] for outcome in identical}) == 1
    assert sorted(outcome[1] for outcome in identical) == [False, True]

    shared_key = f"cross-task-key:{uuid.uuid4().hex}"
    shared_write = real_case.request.write_intent.model_copy(update={
        "idempotency_key": shared_key,
        "provenance": WriteProvenance(publication=LineagePublication(
            idempotency_key=shared_key, provenance="manual"), parents=[]),
    })
    cross_requests = tuple(real_case.request.model_copy(update={
        "submission_id": f"cross-task-{index}-{uuid.uuid4().hex}",
        "write_intent": shared_write,
    }) for index in range(2))
    cross = race(cross_requests)
    successes = [outcome for outcome in cross if isinstance(outcome, tuple)]
    conflicts = [outcome for outcome in cross if isinstance(outcome, metadb.DurableTaskSubmissionConflict)]
    assert len(successes) == len(conflicts) == 1
    assert str(conflicts[0]) == "temporal resample write key is bound to another task"

    with metadb.session() as session:
        tasks = list(session.scalars(select(metadb.DurableTask).where(
            metadb.DurableTask.owner_id == real_case.owner)))
        attempts = list(session.scalars(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id.in_([task.id for task in tasks]))))
    assert len(tasks) == len(attempts) == 2
    assert {task.status for task in tasks} == {"queued"}
    for task in tasks:
        assert metadb.request_durable_task_cancel(task.id) is not None
        assert metadb.claim_temporal_resample_task(task.id, "cancel-race") is None


@pytest.mark.skipif(bool(os.environ.get("DP_TEST_DATABASE_URL")), reason="SQLite contract")
def test_sqlite_concurrent_temporal_submissions_converge_or_conflict(real_case):
    _exercise_concurrent_temporal_submission(real_case)


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated PostgreSQL")
def test_postgres_concurrent_temporal_submissions_converge_or_conflict(real_case):
    _exercise_concurrent_temporal_submission(real_case)


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated PostgreSQL")
def test_postgres_cross_owner_write_key_race_returns_one_stable_conflict():
    owners = tuple(f"temporal-owner-{uuid.uuid4().hex}" for _ in range(2))
    with metadb.session() as session:
        session.add_all(metadb.User(id=owner, name=owner) for owner in owners)
    key = f"cross-owner-key:{uuid.uuid4().hex}"
    requests = tuple(_request(
        submission=f"cross-owner-{index}-{uuid.uuid4().hex}", key=key)
        for index in range(2))
    barrier = threading.Barrier(2)

    def submit(index):
        barrier.wait(timeout=10)
        try:
            return metadb.submit_temporal_resample_task(
                uid=owners[index], request=requests[index], candidate_sha256="b" * 64)
        except metadb.DurableTaskSubmissionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))

    successes = [outcome for outcome in outcomes if isinstance(outcome, tuple)]
    conflicts = [outcome for outcome in outcomes
                 if isinstance(outcome, metadb.DurableTaskSubmissionConflict)]
    assert len(successes) == len(conflicts) == 1
    assert str(conflicts[0]) == "temporal resample write key is bound to another task"
    with metadb.session() as session:
        tasks = list(session.scalars(select(metadb.DurableTask).where(
            metadb.DurableTask.owner_id.in_(owners))))
        attempts = list(session.scalars(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id.in_([task.id for task in tasks]))))
        envelopes = list(session.scalars(select(metadb.TemporalResampleTaskEnvelope).where(
            metadb.TemporalResampleTaskEnvelope.task_id.in_([task.id for task in tasks]))))
    assert len(tasks) == len(attempts) == len(envelopes) == 1
    assert tasks[0].status == "queued"
    assert metadb.request_durable_task_cancel(tasks[0].id) is not None
    assert metadb.claim_temporal_resample_task(tasks[0].id, "test-cleanup") is None


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated PostgreSQL")
def test_postgres_real_temporal_task_lifecycle(real_case):
    from hub import temporal_resample_tasks as tasks

    assert metadb.engine().dialect.name == "postgresql"
    task, created, _candidate = _admit_real(real_case)
    assert created is True
    tasks._worker(task["id"], real_case.deps)

    completed = metadb.durable_task(task["id"])
    replay, replay_created, _candidate = _admit_real(real_case)
    assert completed is not None and completed["status"] == "done"
    assert replay_created is False and replay["id"] == task["id"]
    assert replay["temporal_resample_result"] == completed["temporal_resample_result"]
    assert _temporal_counts(real_case.owner) == (1, 1, 2, 1)


def test_exhausted_recovery_attempt_has_finite_public_status(real_case):
    from hub.main import app

    task, _created, _candidate = _admit_real(real_case, max_attempts=1)
    claimed = metadb.claim_temporal_resample_task(task["id"], "expired-owner")
    assert claimed is not None
    attempt_id = claimed["attempts"][-1]["id"]
    with metadb.session() as session:
        attempt = session.get(metadb.DurableTaskAttempt, attempt_id)
        assert attempt is not None
        attempt.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)

    assert metadb.claim_temporal_resample_task(task["id"], "replacement-owner") is None
    failed = metadb.durable_task(task["id"])
    assert failed is not None and failed["status"] == "failed"
    assert failed["error"] == "temporal_attempts_exhausted"
    with TestClient(app) as client:
        response = client.get(
            f"/api/temporal-resample-write/{task['id']}",
            headers={"X-DP-User": real_case.owner})
    assert response.status_code == 200
    assert response.json()["diagnosticCode"] == "temporal_attempts_exhausted"
    assert response.json()["canRetry"] is False
