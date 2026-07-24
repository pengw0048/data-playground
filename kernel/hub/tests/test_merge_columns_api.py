"""Headless product admission for the certified local merge-columns path."""
from __future__ import annotations

import os
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hub import linear_checkpoint as lc, merge_columns_tasks, metadb
from hub.api_errors import APIError
from hub.deps import Deps
from hub.models import (
    DatasetRevision, Graph, LineagePublication, WritePublicationIdentity,
    WriteProvenance, WriteReceipt,
)
from hub.main import app
from hub.routers import catalog as catalog_api, merge_columns as api


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


def _request(
        tmp_path, monkeypatch, *, owner_id: str = "owner", editor_id: str = "editor",
        canvas_id: str = "canvas", submission_id: str = "merge-api-submission"):
    deps = Deps(str(tmp_path / "workspace"), str(tmp_path / "data"), maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)
    logical_uri = deps.storage.output_uri("base", ".parquet")
    artifact = deps.storage.begin_result("merge-api-fixture", "fixture")
    pq.write_table(pa.table({
        "id": pa.array([1, 2], type=pa.int32()), "value": ["a", "b"], "untouched": [7, 8],
    }), artifact)
    deps.storage.commit_result(artifact, "fixture")
    published = deps.catalog.publish_managed_local_file_output(
        name="base", logical_uri=logical_uri, artifact_uri=artifact)
    assert deps.storage.release_result(artifact, "fixture")
    binding = metadb.catalog_revision_binding(published["dataset_id"])
    assert binding is not None
    with metadb.session() as session:
        session.add(metadb.User(id=owner_id, name="Owner"))
        session.add(metadb.User(id=editor_id, name="Editor"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=owner_id, name="Canvas", doc="{}"))
        session.add(metadb.CanvasShare(canvas_id=canvas_id, user_id=editor_id, role="editor"))
    return api.MergeColumnsRequestV1(
        graph=Graph.model_validate({
            "id": canvas_id,
            "nodes": [
                {"id": "source", "type": "source", "data": {"config": {
                    "uri": binding["uri"], "datasetRef": {"kind": "exact",
                        "datasetId": published["dataset_id"], "revisionId": published["revision_id"]},
                }}},
                {"id": "select", "type": "select", "data": {"config": {
                    "select": "id, value AS replacement"}}},
                {"id": "write", "type": "write", "data": {"config": {
                    "filename": "base.parquet", "writeMode": "overwrite"}}},
            ],
            "edges": [
                {"id": "source-select", "source": "source", "target": "select"},
                {"id": "select-write", "source": "select", "target": "write"},
            ],
        }),
        submission_id=submission_id, identity_columns=["id"],
        rules=[{"source": "replacement", "target": "value", "mode": "replace"}],
    )


def _managed_sidecar_request(tmp_path, monkeypatch, *, submission_id: str | None = None):
    deps = Deps(str(tmp_path / "workspace"), str(tmp_path / "data"), maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)

    def publish(
            logical: str, name: str, table: pa.Table, *, parents: list[str] | None = None,
            lineage: LineagePublication | None = None):
        artifact = deps.storage.begin_result(logical, f"managed-sidecar:{name}")
        pq.write_table(table, artifact)
        deps.storage.commit_result(artifact, f"managed-sidecar:{name}")
        row = deps.catalog.publish_managed_local_file_output(
            name=name, logical_uri=logical, artifact_uri=artifact,
            parents=parents, lineage=lineage)
        assert deps.storage.release_result(artifact, f"managed-sidecar:{name}")
        return row

    base_publication = publish(deps.storage.output_uri("base", ".parquet"), "base", pa.table({
        "id": pa.array([1, 2], type=pa.int32()), "value": [10, 20], "keep": ["a", "b"],
    }))
    base = {"kind": "exact", "datasetId": base_publication["dataset_id"],
            "revisionId": base_publication["revision_id"]}
    sidecar_publication = publish(
        deps.storage.output_uri("sidecar", ".parquet"), "sidecar", pa.table({
            "id": pa.array([2, 1], type=pa.int32()), "replacement": [200, 100], "added": [2, 1],
        }),
        parents=[base_publication["table"].uri],
        lineage=LineagePublication(
            idempotency_key=f"managed-sidecar-fixture:{uuid.uuid4().hex}", provenance="manual",
            field_mappings=[{
                "source_dataset_id": base_publication["dataset_id"],
                "source_version": base_publication["revision_id"],
                "source_field": "id", "source_field_id": None, "destination_field": "id",
            }],
        ),
    )
    sidecar = {"kind": "exact", "datasetId": sidecar_publication["dataset_id"],
               "revisionId": sidecar_publication["revision_id"]}
    with metadb.session() as session:
        if session.get(metadb.User, "owner") is None:
            session.add(metadb.User(id="owner", name="Owner"))
    return api.ManagedSidecarMergeTaskRequestV1(
        submission_id=submission_id or f"managed-sidecar-{uuid.uuid4().hex}",
        base=base, sidecar=sidecar, expected_head=base,
        identity_columns=["id"], rules=[
            {"source": "replacement", "target": "value", "mode": "replace"},
            {"source": "added", "target": "derived", "mode": "add"},
        ])


def test_postgres_compatible_managed_sidecar_task_replays_jobs_and_inbox(
        tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch)
    preflight = api.managed_sidecar_preflight(request, "owner")
    assert preflight.eligible is True
    assert [column.name for column in preflight.output_schema] == ["id", "value", "keep", "derived"]
    assert "uri" not in preflight.model_dump_json().lower()

    task = api.submit_managed_sidecar_merge(request, "owner")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = api.managed_sidecar_status(task.task_id, "owner")
        if current.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert current.status == "done" and current.receipt is not None
    assert current.child_revision_id == current.receipt.revision_id
    assert current.coverage["status"] == "complete"
    assert current.identity_columns == ["id"]
    assert api.submit_managed_sidecar_merge(request, "owner").task_id == task.task_id
    with metadb.session() as session:
        assert not list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "durable_task",
            metadb.LocalResultReference.owner_key == task.task_id,
        )))

    jobs = metadb.list_workspace_runs("owner", run_id=task.task_id)["items"]
    assert len(jobs) == 1 and jobs[0]["mergeColumns"]["baseDatasetId"] == request.base.dataset_id
    assert jobs[0]["mergeColumns"]["producerKind"] == "managed-sidecar"
    inbox = metadb.list_durable_task_inbox_items("owner")["items"]
    assert any(item["task_id"] == task.task_id and item["completed_write"] for item in inbox)

    with TestClient(app) as client:
        jobs_response = client.get("/api/jobs", params={"run_id": task.task_id},
                                   headers={"x-dp-user": "owner"})
        inbox_response = client.get("/api/inbox", headers={"x-dp-user": "owner"})
    assert jobs_response.status_code == inbox_response.status_code == 200
    job = jobs_response.json()["items"][0]
    assert job["datasetContext"]["taskKind"] == "merge_columns_write"
    assert job["mergeColumns"]["producerKind"] == "managed-sidecar"
    assert any(item["taskId"] == task.task_id for item in inbox_response.json()["items"])

    changed = request.model_copy(deep=True)
    changed.rules[0].target = "keep"
    with pytest.raises(APIError) as conflict:
        api.submit_managed_sidecar_merge(changed, "owner")
    assert conflict.value.status_code == 409
    changed_identity = request.model_copy(deep=True)
    changed_identity.identity_columns = ["value"]
    with pytest.raises(APIError) as identity_conflict:
        api.submit_managed_sidecar_merge(changed_identity, "owner")
    assert identity_conflict.value.status_code == 409


def test_managed_sidecar_invalid_or_moved_head_creates_no_task(tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch)
    invalid = request.model_copy(deep=True)
    invalid.identity_columns = ["missing"]
    with pytest.raises(APIError) as invalid_error:
        api.submit_managed_sidecar_merge(invalid, "owner")
    assert invalid_error.value.status_code in (410, 422)

    deps = api.get_deps()
    artifact = deps.storage.begin_result("moved-base", "moved-base")
    pq.write_table(pa.table({"id": pa.array([1, 2], type=pa.int32()), "value": [30, 40],
                             "keep": ["c", "d"]}), artifact)
    deps.storage.commit_result(artifact, "moved-base")
    deps.catalog.publish_managed_local_file_output(
        name="base", logical_uri=deps.storage.output_uri("base", ".parquet"), artifact_uri=artifact)
    assert deps.storage.release_result(artifact, "moved-base")
    with pytest.raises(APIError) as moved_error:
        api.submit_managed_sidecar_merge(request, "owner")
    assert moved_error.value.status_code == 409
    with metadb.session() as session:
        assert session.get(metadb.DurableTask, metadb.managed_sidecar_merge_submission_id(
            "owner", request.submission_id)) is None


def test_managed_sidecar_task_retains_exact_inputs_until_terminal_cleanup(tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id="retained-inputs")
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    task = api.submit_managed_sidecar_merge(request, "owner")
    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "durable_task",
            metadb.LocalResultReference.owner_key == task.task_id,
        )))
    assert len(refs) == 2

    deps = api.get_deps()
    for name, table in (("base", pa.table({"id": pa.array([1, 2], type=pa.int32()),
                                             "value": [30, 40], "keep": ["c", "d"]})),
                        ("sidecar", pa.table({"id": pa.array([1, 2], type=pa.int32()),
                                                "replacement": [300, 400], "added": [3, 4]}))):
        artifact = deps.storage.begin_result(f"moved-{name}", f"moved-{name}")
        pq.write_table(table, artifact)
        deps.storage.commit_result(artifact, f"moved-{name}")
        deps.catalog.publish_managed_local_file_output(
            name=name, logical_uri=deps.storage.output_uri(name, ".parquet"), artifact_uri=artifact)
        assert deps.storage.release_result(artifact, f"moved-{name}")
    metadb.managed_local_file_revision_gc_batch(0)
    assert metadb.managed_local_file_revision_artifact(
        request.base.dataset_id, request.base.revision_id) is not None
    assert metadb.managed_local_file_revision_artifact(
        request.sidecar.dataset_id, request.sidecar.revision_id) is not None


def test_managed_sidecar_missing_input_ref_fails_closed(tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id="missing-input-ref")
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    task = api.submit_managed_sidecar_merge(request, "owner")
    with metadb.session() as session:
        ref = session.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "durable_task",
            metadb.LocalResultReference.owner_key == task.task_id,
        ))
        assert ref is not None
        session.delete(ref)
    with pytest.raises(RuntimeError, match="input retention"):
        metadb.claim_merge_columns_task(task.task_id, "missing-ref-owner")


def test_managed_sidecar_admission_race_rejects_changed_request_digest(tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id="digest-race")
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    api.submit_managed_sidecar_merge(request, "owner")
    intent = api.admit_managed_sidecar_merge(
        storage=api.get_deps().storage, request=api._managed_sidecar_intent(request, "owner"))
    with pytest.raises(metadb.DurableTaskSubmissionConflict, match="frozen admission"):
        metadb.submit_managed_sidecar_merge_task(
            uid="owner", submission_id=request.submission_id,
            intent=intent.model_dump(by_alias=True, mode="json"), request_sha256="0" * 64)


def test_managed_sidecar_router_race_fallback_rejects_changed_digest(tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id="router-digest-race")
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    original = api.submit_managed_sidecar_merge(request, "owner")
    changed = request.model_copy(deep=True)
    changed.rules[0] = api.MergeColumnRuleV1(
        source="replacement", target="replacement_copy", mode="add")

    real_durable_task = metadb.durable_task
    hidden = False

    def hide_existing_once(task_id, **kwargs):
        nonlocal hidden
        if str(task_id) == original.task_id and not hidden:
            hidden = True
            return None
        return real_durable_task(task_id, **kwargs)

    monkeypatch.setattr(api.metadb, "durable_task", hide_existing_once)
    with pytest.raises(APIError) as conflict:
        api.submit_managed_sidecar_merge(changed, "owner")
    assert conflict.value.status_code == 409


def test_merge_producer_routes_cannot_observe_or_mutate_each_other(tmp_path, monkeypatch):
    sparse_request = _request(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    sparse_task = api.submit(sparse_request, "editor")
    sidecar_request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id="producer-isolation")
    sidecar_task = api.submit_managed_sidecar_merge(sidecar_request, "owner")

    for call in (
            lambda: api.status(sidecar_task.task_id, "owner"),
            lambda: api.managed_sidecar_status(sparse_task.task_id, "editor"),
            lambda: api.cancel(sidecar_task.task_id, "owner"),
            lambda: api.cancel_managed_sidecar_merge(sparse_task.task_id, "editor")):
        with pytest.raises(APIError) as error:
            call()
        assert error.value.status_code == 404
    with metadb.session() as session:
        sparse = session.get(metadb.DurableTask, sparse_task.task_id)
        sidecar = session.get(metadb.DurableTask, sidecar_task.task_id)
        assert sparse is not None and sidecar is not None
        sparse_attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == sparse_task.task_id))
        sidecar_attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == sidecar_task.task_id))
        assert sparse.cancel_requested is sidecar.cancel_requested is False
        assert sparse_attempt is not None and sparse_attempt.cancel_requested_at is None
        assert sidecar_attempt is not None and sidecar_attempt.cancel_requested_at is None
        sparse.status = sidecar.status = "cancelled"
        sparse.cancel_requested = sidecar.cancel_requested = False
    for call in (
            lambda: api.retry(sidecar_task.task_id, api._RetryRequest(retry_request_id="wrong-side"), "owner"),
            lambda: api.retry_managed_sidecar_merge(
                sparse_task.task_id, api._RetryRequest(retry_request_id="wrong-sparse"), "editor")):
        with pytest.raises(APIError) as error:
            call()
        assert error.value.status_code == 404
    with metadb.session() as session:
        assert session.get(metadb.DurableTask, sparse_task.task_id).retry_count == 0
        assert session.get(metadb.DurableTask, sidecar_task.task_id).retry_count == 0


def test_managed_sidecar_cancel_retry_retains_inputs_and_can_claim(tmp_path, monkeypatch):
    request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id="cancel-retry")
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    task = api.submit_managed_sidecar_merge(request, "owner")
    assert api.cancel_managed_sidecar_merge(task.task_id, "owner").can_cancel is True
    with metadb.session() as session:
        durable = session.get(metadb.DurableTask, task.task_id)
        assert durable is not None
        durable.status, durable.cancel_requested = "cancelled", False
    first = api.retry_managed_sidecar_merge(
        task.task_id, api._RetryRequest(retry_request_id="retry-once"), "owner")
    second = api.retry_managed_sidecar_merge(
        task.task_id, api._RetryRequest(retry_request_id="retry-once"), "owner")
    assert first.task_id == second.task_id == task.task_id
    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "durable_task",
            metadb.LocalResultReference.owner_key == task.task_id,
        )))
    assert len(refs) == 2
    assert metadb.claim_merge_columns_task(task.task_id, "retry-owner") is not None


def test_managed_sidecar_terminal_cleanup_race_refreshes_status_and_jobs(tmp_path, monkeypatch):
    """A stale queued read must accept the worker's atomic terminal ref cleanup."""
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)

    def terminalize(task_id: str) -> None:
        with metadb.session() as session:
            durable = metadb._lock_durable_task_for_write(session, task_id)
            assert durable is not None
            attempt = session.scalar(select(metadb.DurableTaskAttempt).where(
                metadb.DurableTaskAttempt.task_id == task_id,
            ).order_by(metadb.DurableTaskAttempt.attempt_number.desc()).with_for_update())
            assert attempt is not None
            now = metadb._durable_task_db_now(session)
            receipt = WriteReceipt(
                dataset_id="terminal-race", revision_id=f"revision-{task_id}",
                head=DatasetRevision(
                    dataset_id="terminal-race", revision_id=f"revision-{task_id}"),
                rows=3, bytes=30, schema=[],
                publication=WritePublicationIdentity(
                    logical_uri=f"/terminal-race/{task_id}.parquet",
                    artifact_uri=f"/terminal-race/{task_id}.parquet",
                    publish_sequence=1, idempotency_key=f"terminal-race-{task_id}"),
                provenance=WriteProvenance(publication=LineagePublication(
                    idempotency_key=f"terminal-race-{task_id}", provenance="manual")),
            )
            receipt_json = json.dumps(receipt.model_dump(by_alias=True, mode="json"), sort_keys=True)
            durable.status = attempt.status = "done"
            durable.error = attempt.error = None
            status_doc = {
                "run_id": durable.id, "status": "done",
                "target_node_id": durable.target_node_id, "outputs": [],
                "total_rows": receipt.rows,
            }
            durable.status_doc = json.dumps(status_doc)
            durable.output_receipt = attempt.output_receipt = receipt_json
            durable.completed_at = attempt.completed_at = durable.updated_at = now
            metadb._terminalize_hidden_task_envelope(session, durable, now)

    status_task = api.submit_managed_sidecar_merge(_managed_sidecar_request(
        tmp_path, monkeypatch, submission_id="status-terminal-race"), "owner")
    original_admission = metadb._durable_task_admission
    raced_status = False

    def terminalize_before_stale_admission(session, durable):
        nonlocal raced_status
        if durable.id == status_task.task_id and not raced_status:
            raced_status = True
            terminalize(durable.id)
        return original_admission(session, durable)

    with monkeypatch.context() as patch:
        patch.setattr(metadb, "_durable_task_admission", terminalize_before_stale_admission)
        status = api.managed_sidecar_status(status_task.task_id, "owner")
    assert raced_status and status.status == "done" and status.receipt is not None
    assert status.receipt.rows == 3 and status.child_revision_id == status.receipt.revision_id

    jobs_task = api.submit_managed_sidecar_merge(_managed_sidecar_request(
        tmp_path, monkeypatch, submission_id="jobs-terminal-race"), "owner")
    raced_jobs = False

    def terminalize_before_jobs_admission(session, durable):
        nonlocal raced_jobs
        if durable.id == jobs_task.task_id and not raced_jobs:
            raced_jobs = True
            terminalize(durable.id)
        return original_admission(session, durable)

    with monkeypatch.context() as patch:
        patch.setattr(metadb, "_durable_task_admission", terminalize_before_jobs_admission)
        jobs = metadb.list_workspace_runs("owner", run_id=jobs_task.task_id)["items"]
    assert raced_jobs and len(jobs) == 1 and jobs[0]["status"] == "done"
    assert jobs[0]["rows"] == 3 and jobs[0]["outputReceipt"]["rows"] == 3
    assert jobs[0]["taskAttempts"][-1]["status"] == "done"


@pytest.mark.parametrize("loss", ["candidate", "publication"])
def test_managed_sidecar_reconciles_candidate_and_publication_response_loss(
        tmp_path, monkeypatch, loss):
    request = _managed_sidecar_request(tmp_path, monkeypatch, submission_id=f"loss-{loss}")
    if loss == "candidate":
        real_commit = lc.materialize_and_commit_checkpoint

        def committed_then_lost(*args, **kwargs):
            real_commit(*args, **kwargs)
            raise RuntimeError("candidate commit response lost")

        monkeypatch.setattr(merge_columns_tasks.lc, "materialize_and_commit_checkpoint", committed_then_lost)
    else:
        real_write = merge_columns_tasks.write_managed_local_file

        def published_then_lost(*args, **kwargs):
            real_write(*args, **kwargs)
            raise RuntimeError("publication response lost")

        monkeypatch.setattr(merge_columns_tasks, "write_managed_local_file", published_then_lost)
    task = api.submit_managed_sidecar_merge(request, "owner")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = api.managed_sidecar_status(task.task_id, "owner")
        if current.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert current.status == "done" and current.receipt is not None
    assert api.submit_managed_sidecar_merge(request, "owner").task_id == task.task_id


def test_editor_preflights_without_side_effect_then_submits_and_replays(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
    preflight = api.preflight(request, "editor")
    assert preflight.coverage.status == "complete"
    assert preflight.coverage.matched_identities == 2
    assert preflight.output_schema[-1].name == "untouched"
    assert "sha" not in preflight.model_dump_json().lower()
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0

    submitted = api.submit(request, "editor")
    durable = metadb.durable_task(submitted.task_id, include_admission=False)
    assert durable is not None and durable["target_node_id"] == "write"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = api.status(submitted.task_id, "editor")
        if current.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert current.status == "done"
    assert "sparseOutputId" not in current.model_dump_json(by_alias=True)
    durable = metadb.durable_task(submitted.task_id, include_admission=False)
    assert durable is not None
    assert durable["status_doc"]["target_node_id"] == "write"
    assert durable["status_doc"]["outputs"][0]["node_id"] == "write"
    job = next(item for item in metadb.list_workspace_runs(
        "editor", run_id=submitted.task_id)["items"] if item["taskId"] == submitted.task_id)
    assert job["outputs"][0]["node_id"] == "write"
    replayed = api.submit(request, "editor")
    assert replayed.task_id == submitted.task_id
    durable = metadb.durable_task(replayed.task_id, include_admission=False)
    assert durable is not None and durable["target_node_id"] == "write"

    presentation_replay = request.model_copy(deep=True)
    presentation_replay.graph.version = 99
    presentation_replay.graph.nodes[0].position.x = 400
    presentation_replay.graph.nodes[0].data["title"] = "Moved source"
    assert api.submit(presentation_replay, "editor").task_id == submitted.task_id

    moved_head = request.model_copy(update={"submission_id": "after-real-head-move"})
    with pytest.raises(APIError) as moved_preflight:
        api.preflight(moved_head, "editor")
    assert moved_preflight.value.status_code == 409
    with pytest.raises(APIError) as moved_submit:
        api.submit(moved_head, "editor")
    assert moved_submit.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 1
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 1

    changed_select = request.model_copy(deep=True)
    changed_select.graph.nodes[1].data["config"]["select"] = "id, value AS changed"
    with pytest.raises(APIError) as select_error:
        api.submit(changed_select, "editor")
    assert select_error.value.status_code == 409
    changed_rules = request.model_copy(deep=True)
    changed_rules.rules[0].target = "untouched"
    with pytest.raises(APIError) as rules_error:
        api.submit(changed_rules, "editor")
    assert rules_error.value.status_code == 409
    changed_source = request.model_copy(deep=True)
    changed_source.graph.nodes[0].data["config"]["uri"] = "file:///different.parquet"
    with pytest.raises(APIError) as source_error:
        api.submit(changed_source, "editor")
    assert source_error.value.status_code == 409
    changed_write = request.model_copy(deep=True)
    changed_write.graph.nodes[2].data["config"]["filename"] = "different.parquet"
    with pytest.raises(APIError) as write_error:
        api.submit(changed_write, "editor")
    assert write_error.value.status_code == 409
    changed_target = request.model_copy(deep=True)
    changed_target.graph.nodes[2].id = "other-write"
    changed_target.graph.edges[1].target = "other-write"
    with pytest.raises(APIError) as target_error:
        api.submit(changed_target, "editor")
    assert target_error.value.status_code == 409


def test_add_mapping_preflights_and_publishes_full_schema(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    base_revision_id = request.graph.nodes[0].data["config"]["datasetRef"]["revisionId"]
    request.graph.nodes[1].data["config"]["select"] = "id, value AS added"
    request.rules[0].source = "added"
    request.rules[0].target = "derived"
    request.rules[0].mode = "add"
    preflight = api.preflight(request, "editor")
    assert [column.name for column in preflight.output_schema] == ["id", "value", "untouched", "derived"]

    submitted = api.submit(request, "editor")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = api.status(submitted.task_id, "editor")
        if current.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert current.status == "done"
    dataset_id = request.graph.nodes[0].data["config"]["datasetRef"]["datasetId"]
    with metadb.session() as session:
        logical = session.get(metadb.CatalogLogicalDataset, dataset_id)
        assert logical is not None
        logical_uri = logical.logical_uri
    head = metadb.catalog_managed_local_write_head(logical_uri)
    assert head is not None
    artifact = metadb.managed_local_file_revision_artifact(dataset_id, head["revision_id"])
    assert artifact is not None
    table = pq.read_table(artifact)
    assert table.schema.names == ["id", "value", "untouched", "derived"]
    assert table.column("derived").to_pylist() == ["a", "b"]

    # Reopen both immutable revisions through the ordinary catalog API handler.  This is a
    # release-level aggregation over the existing retry/restart/cancel/concurrency coverage,
    # not another merge execution matrix.
    monkeypatch.setattr(catalog_api, "get_deps", api.get_deps)
    base = catalog_api.open_dataset_revision(dataset_id, base_revision_id)
    final = catalog_api.open_dataset_revision(dataset_id, head["revision_id"])
    assert base.revision_id == base_revision_id and base.preview.rows[0]["value"] == "a"
    assert final.parent_revision_id == base_revision_id
    assert [field.name for field in final.preview.columns] == ["id", "value", "untouched", "derived"]


def test_partial_or_invalid_admission_never_creates_merge_side_effects(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    partial = request.model_copy(deep=True)
    partial.graph.nodes[1].data["config"]["select"] = "id + 1 AS id, value AS replacement"
    assert api.preflight(partial, "editor").eligible is False
    with pytest.raises(APIError) as partial_error:
        api.submit(partial, "editor")
    assert partial_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0

    invalid = request.model_copy(deep=True)
    invalid.graph.nodes[1].data["config"]["select"] = "1 AS id, value AS replacement"
    assert api.preflight(invalid, "editor").eligible is False
    with pytest.raises(APIError) as invalid_error:
        api.submit(invalid, "editor")
    assert invalid_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


def test_submission_conflict_releases_only_unclaimed_merge_sidecars(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    real_submit = metadb.submit_merge_columns_task

    def unclaimed_conflict(**_kwargs):
        raise metadb.DurableTaskSubmissionConflict("stale expected head")

    monkeypatch.setattr(api.metadb, "submit_merge_columns_task", unclaimed_conflict)
    with pytest.raises(APIError) as unclaimed_error:
        api.submit(request, "editor")
    assert unclaimed_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0

    def claimed_conflict(**kwargs):
        real_submit(**kwargs)
        raise metadb.DurableTaskSubmissionConflict("concurrent admission")

    monkeypatch.setattr(api.metadb, "submit_merge_columns_task", claimed_conflict)
    reconciled = api.submit(request, "editor")
    assert reconciled.status == "queued"
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 1
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 1


def test_unsupported_destination_is_a_conflict_without_side_effects(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    request.graph.nodes[2].data["config"]["destId"] = "missing-destination"
    with pytest.raises(APIError) as error:
        api.preflight(request, "editor")
    assert error.value.status_code == 409
    with pytest.raises(APIError) as submit_error:
        api.submit(request, "editor")
    assert submit_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


@pytest.mark.parametrize("blocker", [
    "latest-source", "mutable-source", "wrong-shape", "multiple-inputs", "non-default-port",
    "csv-destination", "lance-destination", "arrow-destination", "provider-destination",
    "object-destination",
    "append", "upsert",
])
def test_documented_router_blockers_reject_before_side_effects(tmp_path, monkeypatch, blocker):
    request = _request(tmp_path, monkeypatch)
    if blocker == "latest-source":
        request.graph.nodes[0].data["config"]["datasetRef"] = {
            "kind": "latest",
            "datasetId": request.graph.nodes[0].data["config"]["datasetRef"]["datasetId"],
        }
    elif blocker == "mutable-source":
        request.graph.nodes[0].data["config"]["uri"] = "file:///mutable-source.parquet"
    elif blocker == "wrong-shape":
        request.graph.edges.pop()
    elif blocker == "multiple-inputs":
        request.graph.edges.append(request.graph.edges[0].model_copy(update={
            "id": "extra-input", "target": "write"}))
    elif blocker == "non-default-port":
        request.graph.edges[0].source_handle = "alternate"
    elif blocker in ("csv-destination", "lance-destination", "arrow-destination"):
        request.graph.nodes[2].data["config"]["filename"] = f"base.{blocker.split('-')[0]}"
    elif blocker == "provider-destination":
        request.graph.nodes[2].data["config"]["destId"] = "provider-destination"
    elif blocker == "object-destination":
        from hub import destinations
        monkeypatch.setattr(destinations, "presets", lambda _workspace: [{
            "id": "object", "name": "Object", "backend": "s3", "root": "s3://bucket",
        }])
        request.graph.nodes[2].data["config"]["destId"] = "object"
    else:
        request.graph.nodes[2].data["config"]["writeMode"] = blocker

    with pytest.raises(APIError) as preflight_error:
        api.preflight(request, "editor")
    assert preflight_error.value.status_code == 409
    with pytest.raises(APIError) as submit_error:
        api.submit(request, "editor")
    assert submit_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


def test_unauthorized_preflight_and_submit_leave_no_merge_side_effects(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add(metadb.User(id="intruder", name="Intruder"))
    for endpoint in (api.preflight, api.submit):
        with pytest.raises(APIError) as error:
            endpoint(request, "intruder")
        assert error.value.status_code == 404
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization)) == 0
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask)) == 0


def test_revoked_editor_replay_cannot_dispatch_existing_task(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(api, "dispatch", lambda task_id, _deps: dispatched.append(task_id))
    task = api.submit(request, "editor")
    assert dispatched == [task.task_id]
    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)
    with pytest.raises(APIError) as error:
        api.submit(request, "editor")
    assert error.value.status_code == 404
    assert dispatched == [task.task_id]
    with metadb.session() as session:
        durable = session.get(metadb.DurableTask, task.task_id)
        assert durable is not None and durable.status == "queued"


def _queued(request, uid, monkeypatch):
    monkeypatch.setattr(api, "dispatch", lambda *_args: None)
    return api.submit(request, uid)


def _cancelled(task_id):
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, task_id)
        assert task is not None
        task.status = "cancelled"
        task.cancel_requested = False


def test_owner_and_editor_can_cancel_and_idempotently_retry_their_own_merge_tasks(tmp_path, monkeypatch):
    editor_request = _request(tmp_path, monkeypatch)
    editor_task = _queued(editor_request, "editor", monkeypatch)
    owner_request = editor_request.model_copy(update={"submission_id": "owner-merge-submission"})
    owner_task = _queued(owner_request, "owner", monkeypatch)

    assert api.cancel(editor_task.task_id, "editor").can_cancel is True
    assert api.cancel(owner_task.task_id, "owner").can_cancel is True
    _cancelled(editor_task.task_id)
    _cancelled(owner_task.task_id)

    first = api.retry(editor_task.task_id, api._RetryRequest(retry_request_id="editor-retry"), "editor")
    second = api.retry(editor_task.task_id, api._RetryRequest(retry_request_id="editor-retry"), "editor")
    assert first.task_id == second.task_id == editor_task.task_id
    assert api.retry(owner_task.task_id, api._RetryRequest(retry_request_id="owner-retry"), "owner").task_id == owner_task.task_id
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == editor_task.task_id)) == 2


def test_merge_task_observation_is_shared_but_actions_remain_with_original_owner(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        session.add_all((
            metadb.User(id="viewer", name="Viewer"),
            metadb.User(id="stranger", name="Stranger"),
            metadb.CanvasShare(canvas_id="canvas", user_id="viewer", role="viewer"),
        ))

    task = _queued(request, "editor", monkeypatch)
    assert metadb.durable_task(task.task_id, include_admission=False)["target_node_id"] == "write"

    owner_view = api.status(task.task_id, "owner")
    editor_view = api.status(task.task_id, "editor")
    viewer_view = api.status(task.task_id, "viewer")
    assert owner_view.task_id == editor_view.task_id == viewer_view.task_id == task.task_id
    assert (editor_view.can_cancel, editor_view.can_retry) == (True, False)
    assert (owner_view.can_cancel, owner_view.can_retry) == (False, False)
    assert (viewer_view.can_cancel, viewer_view.can_retry) == (False, False)
    assert editor_view.merge_columns is not None
    assert owner_view.merge_columns is not None
    assert viewer_view.merge_columns is not None
    assert editor_view.merge_columns.can_cancel is True
    assert owner_view.merge_columns.can_cancel is False
    assert viewer_view.merge_columns.can_cancel is False

    for uid, expected in (("editor", True), ("owner", False), ("viewer", False)):
        job = next(item for item in metadb.list_workspace_runs(uid, run_id=task.task_id)["items"]
                   if item["taskId"] == task.task_id)
        assert job["targetNodeId"] == "write"
        assert job["canCancel"] is expected and job["canRetry"] is False
        assert job["mergeColumns"]["producerKind"] == "sparse-output"
        assert job["mergeColumns"]["canCancel"] is expected
        assert job["mergeColumns"]["canRetry"] is False

    for uid in ("owner", "viewer", "stranger"):
        with pytest.raises(APIError) as cancel_error:
            api.cancel(task.task_id, uid)
        assert cancel_error.value.status_code == 404
    _cancelled(task.task_id)
    for uid in ("owner", "viewer", "stranger"):
        with pytest.raises(APIError) as retry_error:
            api.retry(task.task_id, api._RetryRequest(retry_request_id=f"{uid}-retry"), uid)
        assert retry_error.value.status_code == 404
    assert api.retry(task.task_id, api._RetryRequest(retry_request_id="editor-retry"), "editor").task_id == task.task_id

    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)
    for uid in ("editor", "stranger"):
        with pytest.raises(APIError) as status_error:
            api.status(task.task_id, uid)
        assert status_error.value.status_code == 404
        assert not metadb.list_workspace_runs(uid, run_id=task.task_id)["items"]


def test_revoked_editor_cannot_cancel_or_retry_and_leaves_task_state_unchanged(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    queued = _queued(request, "editor", monkeypatch)
    _cancelled(queued.task_id)
    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)

    with pytest.raises(APIError) as retry_error:
        api.retry(queued.task_id, api._RetryRequest(retry_request_id="revoked-retry"), "editor")
    assert retry_error.value.status_code == 404
    with pytest.raises(APIError) as cancel_error:
        api.cancel(queued.task_id, "editor")
    assert cancel_error.value.status_code == 404
    with metadb.session() as session:
        task = session.get(metadb.DurableTask, queued.task_id)
        assert task is not None
        assert (task.status, task.cancel_requested, task.retry_count) == ("cancelled", False, 0)
        assert session.scalar(select(func.count()).select_from(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == queued.task_id)) == 1


def test_workspace_editor_can_submit_cancel_and_retry_merge_task(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch)
    with metadb.session() as session:
        share = session.scalar(select(metadb.CanvasShare).where(
            metadb.CanvasShare.canvas_id == "canvas", metadb.CanvasShare.user_id == "editor"))
        assert share is not None
        session.delete(share)
        canvas = session.get(metadb.Canvas, "canvas")
        assert canvas is not None
        canvas.visibility = "workspace"

    queued = _queued(request, "editor", monkeypatch)
    assert api.cancel(queued.task_id, "editor").task_id == queued.task_id
    _cancelled(queued.task_id)
    assert api.retry(queued.task_id, api._RetryRequest(retry_request_id="workspace-retry"), "editor").task_id == queued.task_id


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"),
                    reason="requires dedicated PostgreSQL")
def test_postgres_merge_api_revoke_serializes_admission_and_blocks_later_dispatch(
        tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex
    owner, editor, canvas = (f"owner-{suffix}", f"editor-{suffix}", f"canvas-{suffix}")
    request = _request(
        tmp_path, monkeypatch, owner_id=owner, editor_id=editor, canvas_id=canvas,
        submission_id=f"submission-{suffix}")
    dispatched: list[str] = []
    monkeypatch.setattr(api, "dispatch", lambda task_id, _deps: dispatched.append(task_id))
    about_to_admit = threading.Event()
    revoke_locked = threading.Event()
    release_revoke = threading.Event()
    original_submit = metadb.submit_merge_columns_task

    def gate_admission(**kwargs):
        about_to_admit.set()
        assert revoke_locked.wait(timeout=10)
        return original_submit(**kwargs)

    def revoke_editor():
        assert about_to_admit.wait(timeout=10)
        with metadb.session() as session:
            locked_canvas = session.get(metadb.Canvas, canvas, with_for_update=True)
            assert locked_canvas is not None
            share = session.scalar(select(metadb.CanvasShare).where(
                metadb.CanvasShare.canvas_id == canvas,
                metadb.CanvasShare.user_id == editor,
            ).with_for_update())
            assert share is not None
            session.delete(share)
            revoke_locked.set()
            assert release_revoke.wait(timeout=10)

    monkeypatch.setattr(api.metadb, "submit_merge_columns_task", gate_admission)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = pool.submit(api.submit, request, editor)
        revoked = pool.submit(revoke_editor)
        assert revoke_locked.wait(timeout=10)
        release_revoke.set()
        revoked.result(timeout=10)
        with pytest.raises(APIError) as admission_error:
            admitted.result(timeout=10)
    assert admission_error.value.status_code == 409
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(metadb.DurableTask).where(
            metadb.DurableTask.owner_id == editor,
            metadb.DurableTask.canvas_id == canvas,
        )) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutput).where(
            metadb.SparseOutput.canvas_id == canvas,
        )) == 0
        assert session.scalar(select(func.count()).select_from(metadb.SparseOutputMaterialization).join(
            metadb.SparseOutput,
            metadb.SparseOutput.id == metadb.SparseOutputMaterialization.sparse_id,
        ).where(metadb.SparseOutput.canvas_id == canvas)) == 0
    with pytest.raises(APIError) as replay_error:
        api.submit(request, editor)
    assert replay_error.value.status_code == 404
    assert dispatched == []
