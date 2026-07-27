"""Durable exact-revision row identity certification lifecycle (#875)."""

from __future__ import annotations

import datetime
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from hub import metadb, row_identity_tasks
from hub.api_errors import APIError
from hub.deps import Deps
from hub.main import app
from hub.models import DurableTaskInboxPage, ExactDatasetRef, WorkspaceRunPage
from hub.plugins.adapters import LanceAdapter
from hub.routers import catalog as catalog_routes
from hub.routers import row_identity_certifications as api
from hub.row_identity import (
    RowIdentityValidationError,
    certify_exact_row_identity,
    serialize_row_identity_coverage,
)
from hub.sparse_outputs import SparseOutputAdmissionRequest, admit_sparse_output


@pytest.fixture(autouse=True)
def _metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = (
        os.environ.get("DP_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'row-identity-task.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    token = uuid.uuid4().hex
    deps = Deps(
        str(tmp_path / f"workspace-{token}"),
        str(tmp_path / f"data-{token}"),
        maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)
    monkeypatch.setattr(api, "dispatch", lambda _task_id, _deps: None)
    with metadb.session() as session:
        if session.get(metadb.User, "owner") is None:
            session.add(metadb.User(id="owner", name="Owner"))

    def publish(table: pa.Table, *, name: str = "observations") -> dict:
        run_id = uuid.uuid4().hex
        logical_uri = deps.storage.output_uri(f"identity-{token}-{name}", ".parquet")
        artifact = deps.storage.begin_result(run_id, run_id)
        pq.write_table(table, artifact)
        deps.storage.commit_result(artifact, run_id)
        published = deps.catalog.publish_managed_local_file_output(
            name=name, logical_uri=logical_uri, artifact_uri=artifact)
        assert deps.storage.release_result(artifact, run_id)
        return published

    yield deps, publish
    deps.storage.close()


@pytest.fixture
def lance_dataset(tmp_path, monkeypatch):
    lance = pytest.importorskip("lance")
    token = uuid.uuid4().hex
    deps = Deps(
        str(tmp_path / f"lance-workspace-{token}"),
        str(tmp_path / f"lance-data-{token}"),
        maintain_storage=False)
    monkeypatch.setattr(api, "get_deps", lambda: deps)
    monkeypatch.setattr(api, "dispatch", lambda _task_id, _deps: None)
    monkeypatch.setattr(catalog_routes, "get_deps", lambda: deps)
    with metadb.session() as session:
        if session.get(metadb.User, "owner") is None:
            session.add(metadb.User(id="owner", name="Owner"))

    def publish(table: pa.Table, *, name: str = "lance-observations") -> dict:
        uri = str(tmp_path / f"{name}-{uuid.uuid4().hex}.lance")
        lance.write_dataset(table, uri)
        deps.catalog._add(name=name, uri=uri, strict_probe=True)
        binding = metadb.catalog_revision_binding_for_uri(uri)
        assert binding is not None
        revision_id = LanceAdapter().resolve_revision(uri)["revision_id"]
        return {
            "dataset_id": binding["dataset_id"],
            "revision_id": revision_id,
            "uri": uri,
        }

    yield deps, publish
    deps.storage.close()


def _request(published: dict, keys=("id",), *, submission_id: uuid.UUID | None = None):
    return api.RowIdentityCertificationSubmitV1(
        submission_id=submission_id or uuid.uuid4(),
        dataset_id=published["dataset_id"], revision_id=published["revision_id"],
        key_columns=list(keys))


def _submit(request, uid: str = "owner"):
    response = Response()
    task = api.submit(request, response, uid)
    return task, response.status_code


def _run(deps, task_id: str) -> None:
    row_identity_tasks._worker(task_id, deps)


def _confirmed_lance_request(published: dict, keys=("id",)):
    request = _request(published, keys)
    estimate = api.preflight(request, "owner")
    assert estimate.needs_confirmation is True
    assert estimate.reason == "unknown_size"
    assert estimate.estimated_scan_rows is not None
    assert estimate.estimated_scan_bytes is None
    return request.model_copy(update={
        "confirmation_sha256": estimate.confirmation_sha256,
    }), estimate


def _exact_lance_data_file(lance, uri: str, revision_id: str) -> Path:
    rows = lance.dataset(
        uri, version=int(revision_id)).tracked_files().read_all().to_pylist()
    matches = [
        row for row in rows
        if row["version"] == int(revision_id) and row["type"] == "data file"
    ]
    assert len(matches) == 1
    return Path(str(matches[0]["base_uri"])) / str(matches[0]["path"])


def test_preflight_submit_worker_replay_and_exact_jobs_inbox_deep_link(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({
        "id": pa.array([1, 2, 3], pa.int64()),
        "value": pa.array(["a", "b", "c"]),
    }))
    request = _request(published, submission_id=uuid.uuid4())
    original_scan = __import__("hub.row_identity", fromlist=["DuckDBAdapter"]).DuckDBAdapter.scan
    monkeypatch.setattr(
        "hub.row_identity.DuckDBAdapter.scan",
        lambda *_args, **_kwargs: pytest.fail("only the durable worker may scan"))
    preflight = api.preflight(request, "owner")
    assert preflight.needs_confirmation is False
    assert preflight.estimated_scan_rows == 3
    task, status_code = _submit(request)
    assert status_code == 201 and task.status == "queued"
    assert api.status(task.task_id, "owner").status == "queued"

    monkeypatch.setattr("hub.row_identity.DuckDBAdapter.scan", original_scan)
    _run(deps, task.task_id)
    done = api.status(task.task_id, "owner")
    assert done.status == "done"
    assert done.receipt is not None and done.receipt.outcome == "certified"
    assert done.receipt.certificate is not None
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is not None

    monkeypatch.setattr(
        "hub.row_identity.DuckDBAdapter.scan",
        lambda *_args, **_kwargs: pytest.fail("terminal submission replay must not rescan"))
    replay, replay_status = _submit(request)
    assert replay_status == 200 and replay.task_id == task.task_id
    assert replay.receipt == done.receipt

    jobs = WorkspaceRunPage.model_validate(metadb.list_workspace_runs("owner"))
    job = next(item for item in jobs.items if item.task_id == task.task_id)
    assert job.canvas_id is None and job.dataset_context is not None
    assert job.dataset_context.revision_id == published["revision_id"]
    assert job.dataset_context.deep_link is not None
    assert f"revision={published['revision_id']}" in job.dataset_context.deep_link
    assert "rowIdentityAction=certify" in job.dataset_context.deep_link
    assert f"rowIdentityTask={task.task_id}" in job.dataset_context.deep_link
    inbox = DurableTaskInboxPage.model_validate(
        metadb.list_durable_task_inbox_items("owner", limit=50))
    item = next(item for item in inbox.items if item.task_id == task.task_id)
    assert item.outcome == "completed" and item.dataset_context == job.dataset_context


def test_lance_task_certifies_exact_preview_and_replays_without_rescan(
        lance_dataset, monkeypatch):
    deps, publish = lance_dataset
    published = publish(pa.table({
        "signed": pa.array([-(2**63), 2**63 - 1], pa.int64()),
        "unsigned": pa.array([0, 2**64 - 1], pa.uint64()),
        "label": pa.array(["first", "second"], pa.string()),
    }))
    request, estimate = _confirmed_lance_request(
        published, ("signed", "unsigned", "label"))
    projections = 0
    original_projection = LanceAdapter.open_revision_projection

    def counted_projection(*args, **kwargs):
        nonlocal projections
        projections += 1
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(LanceAdapter, "open_revision_projection", counted_projection)
    task, status_code = _submit(request)
    assert status_code == 201
    assert task.schema_sha256 == estimate.schema_sha256
    assert task.spec_sha256 == estimate.spec_sha256
    _run(deps, task.task_id)

    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "done"
    assert terminal.receipt is not None
    assert terminal.receipt.outcome == "certified"
    assert terminal.receipt.certificate is not None
    assert projections == 2  # one whole key scan plus one bounded preview gate

    detail = catalog_routes.open_dataset_revision(
        published["dataset_id"], published["revision_id"])
    assert detail.row_identity.proof_status == "certified"
    assert detail.row_identity.certification_supported is True
    assert detail.media_cell_supported is False
    assert detail.preview.row_identities is not None
    assert len(detail.preview.row_identities) == len(detail.preview.rows) == 2
    assert [field.value for field in detail.preview.row_identities[0]] == [
        str(-(2**63)), "0", "first",
    ]
    assert [field.value for field in detail.preview.row_identities[1]] == [
        str(2**63 - 1), str(2**64 - 1), "second",
    ]

    replay, replay_status = _submit(request)
    assert replay_status == 200
    assert replay.receipt == terminal.receipt
    assert projections == 2
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityCertificate).where(
                metadb.ManagedLocalLanceRowIdentityCertificate.registration_id
                == published["dataset_id"])) == 1
        assert session.scalar(select(func.count()).select_from(
            metadb.DurableTaskInboxItem).where(
                metadb.DurableTaskInboxItem.task_id == task.task_id)) == 1


@pytest.mark.parametrize(("table", "keys", "outcome"), [
    pytest.param(
        pa.table({"id": pa.array([1, 1], pa.int64())}),
        ("id",), "duplicate_key", id="duplicate"),
    pytest.param(
        pa.table({"id": pa.array([1, None], pa.int64())}),
        ("id",), "null_key", id="null"),
    pytest.param(
        pa.table({"id": pa.array([1.0, 2.0], pa.float64())}),
        ("id",), "unsupported_type", id="unsupported"),
    pytest.param(
        pa.table({"id": pa.array(["x" * 8193], pa.string())}),
        ("id",), "identity_value_over_limit", id="value-over-limit"),
    pytest.param(
        pa.table({
            "left": pa.array(
                [f"{index:03d}" + "a" * 6997 for index in range(100)],
                pa.string()),
            "right": pa.array(
                [f"{index:03d}" + "b" * 6997 for index in range(100)],
                pa.string()),
        }),
        ("left", "right"), "preview_identity_evidence_over_budget",
        id="preview-over-budget"),
])
def test_lance_invalid_evidence_has_typed_receipt_and_no_certificate(
        lance_dataset, table, keys, outcome, monkeypatch):
    deps, publish = lance_dataset
    published = publish(table, name=f"invalid-{outcome}")
    request, estimate = _confirmed_lance_request(published, keys)
    if outcome == "unsupported_type":
        assert estimate.supported is False
        monkeypatch.setattr(
            row_identity_tasks, "certify_and_commit_managed_local_lance_row_identity",
            lambda *_args, **_kwargs: pytest.fail(
                "unsupported Lance types must not scan"))
    task, status_code = _submit(request)
    assert status_code == 201
    _run(deps, task.task_id)

    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "failed"
    assert terminal.receipt is not None
    assert terminal.receipt.outcome == outcome
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityCertificate).where(
                metadb.ManagedLocalLanceRowIdentityCertificate.registration_id
                == published["dataset_id"])) == 0


def test_lance_admission_requires_the_exact_core_adapter(lance_dataset, monkeypatch):
    deps, publish = lance_dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))

    class LanceSubclass(LanceAdapter):
        pass

    monkeypatch.setattr(deps, "resolve_adapter", lambda _uri: LanceSubclass())
    with pytest.raises(APIError) as caught:
        api.preflight(_request(published), "owner")
    assert caught.value.status_code == 422


def test_lance_registration_loss_after_admission_fails_without_publication(
        lance_dataset):
    deps, publish = lance_dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    request, _estimate = _confirmed_lance_request(published)
    task, status_code = _submit(request)
    assert status_code == 201
    assert metadb.catalog_delete_entry(published["uri"], report_result=True) is True

    _run(deps, task.task_id)

    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "failed"
    assert terminal.receipt is not None
    assert terminal.receipt.outcome == "stale_or_unavailable_revision"
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityCertificate).where(
                metadb.ManagedLocalLanceRowIdentityCertificate.registration_id
                == published["dataset_id"])) == 0


def test_lance_finish_rechecks_physical_incarnation_after_worker_scan(
        lance_dataset, tmp_path, monkeypatch):
    lance = pytest.importorskip("lance")
    deps, publish = lance_dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    request, _estimate = _confirmed_lance_request(published)
    task, status_code = _submit(request)
    assert status_code == 201

    replacement_uri = str(tmp_path / f"finish-replacement-{uuid.uuid4().hex}.lance")
    lance.write_dataset(
        pa.table({"id": pa.array([9, 10], pa.int64())}), replacement_uri)
    target = _exact_lance_data_file(
        lance, published["uri"], published["revision_id"])
    replacement = _exact_lance_data_file(lance, replacement_uri, "1")
    original_finish = (
        metadb.finish_managed_local_lance_row_identity_certification_scan)
    replaced = False

    def replace_after_worker_fence(*args, **kwargs):
        nonlocal replaced
        assert replaced is False
        shutil.copyfile(replacement, target)
        replaced = True
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(
        row_identity_tasks.metadb,
        "finish_managed_local_lance_row_identity_certification_scan",
        replace_after_worker_fence)
    _run(deps, task.task_id)

    terminal = api.status(task.task_id, "owner")
    assert replaced is True
    assert terminal.status == "failed"
    assert terminal.receipt is not None
    assert terminal.receipt.outcome == "stale_or_unavailable_revision"
    assert terminal.receipt.certificate is None
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityFence).where(
                metadb.ManagedLocalLanceRowIdentityFence.registration_id
                == published["dataset_id"])) == 0
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityCertificate).where(
                metadb.ManagedLocalLanceRowIdentityCertificate.registration_id
                == published["dataset_id"])) == 0


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL Lance certificate race")
def test_postgres_lance_concurrent_first_certification_publishes_one_certificate(
        lance_dataset):
    deps, publish = lance_dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    first_request, _estimate = _confirmed_lance_request(published)
    second_request = first_request.model_copy(update={
        "submission_id": uuid.uuid4(),
    })
    first, _ = _submit(first_request)
    second, _ = _submit(second_request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda task_id: _run(deps, task_id), (
            first.task_id, second.task_id)))

    first_receipt = api.status(first.task_id, "owner").receipt
    second_receipt = api.status(second.task_id, "owner").receipt
    assert first_receipt is not None and second_receipt is not None
    outcomes = {first_receipt.outcome, second_receipt.outcome}
    assert outcomes == {"certified", "already_certified_same_spec"}
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityCertificate).where(
                metadb.ManagedLocalLanceRowIdentityCertificate.registration_id
                == published["dataset_id"])) == 1


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL registration ABA race")
def test_postgres_lance_finish_vs_reregistration_fails_closed(lance_dataset):
    deps, publish = lance_dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    request, _estimate = _confirmed_lance_request(published)
    task, _ = _submit(request)
    start = threading.Barrier(2)

    def finish():
        start.wait(timeout=5)
        _run(deps, task.task_id)

    def reregister():
        start.wait(timeout=5)
        assert metadb.catalog_delete_entry(
            published["uri"], report_result=True) is True
        deps.catalog._add(
            name="registered-again", uri=published["uri"], strict_probe=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = pool.submit(finish)
        rebound = pool.submit(reregister)
        finished.result(timeout=20)
        rebound.result(timeout=20)

    current = metadb.catalog_revision_binding_for_uri(published["uri"])
    assert current is not None
    assert current["dataset_id"] != published["dataset_id"]
    terminal = api.status(task.task_id, "owner")
    assert terminal.status in {"done", "failed"}
    assert terminal.receipt is not None
    assert terminal.receipt.outcome in {
        "certified", "stale_or_unavailable_revision",
    }
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalLanceRowIdentityCertificate).where(
                metadb.ManagedLocalLanceRowIdentityCertificate.registration_id
                == published["dataset_id"])) == 0


def test_list_column_uses_one_schema_digest_from_preflight_through_worker(dataset):
    deps, publish = dataset
    published = publish(pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "vector": pa.array(
            [[1.0, 2.0], [3.0, 4.0]],
            type=pa.list_(pa.float32()),
        ),
    }))
    request = _request(published)

    preflight = api.preflight(request, "owner")
    task, status_code = _submit(request)
    assert status_code == 201
    assert task.schema_sha256 == preflight.schema_sha256
    assert task.spec_sha256 == preflight.spec_sha256

    _run(deps, task.task_id)
    done = api.status(task.task_id, "owner")

    assert done.status == "done"
    assert done.receipt is not None
    assert done.receipt.outcome == "certified"
    assert done.receipt.certificate is not None
    assert done.receipt.certificate.dataset_id == published["dataset_id"]
    assert done.receipt.certificate.revision_id == published["revision_id"]


@pytest.mark.parametrize(
    ("vector_type", "values"),
    [
        pytest.param(
            pa.list_(pa.float32()),
            [[1.0, 2.0], [3.0, 4.0]],
            id="regular-list",
        ),
        pytest.param(
            pa.list_(pa.float32(), 2),
            [[1.0, 2.0], [3.0, 4.0]],
            id="fixed-size-list",
        ),
    ],
)
def test_sparse_and_standalone_certification_share_the_footer_spec(
        dataset, vector_type, values):
    deps, publish = dataset
    published = publish(pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "vector": pa.array(values, type=vector_type),
    }), name="sparse-interop")
    canvas_id = f"canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id="owner", name="Sparse interop", doc="{}"))
    admit_sparse_output(deps.storage, SparseOutputAdmissionRequest(
        owner_id="owner",
        canvas_id=canvas_id,
        submission_id=str(uuid.uuid4()),
        dataset_ref=ExactDatasetRef(
            kind="exact", dataset_id=published["dataset_id"],
            revision_id=published["revision_id"]),
        select_config={"expr": "id, vector"},
        identity_columns=["id"],
        provenance={
            "idempotencyKey": f"sparse-{uuid.uuid4().hex}",
            "provenance": "manual",
        },
    ))

    request = _request(published)
    preflight = api.preflight(request, "owner")
    task, status_code = _submit(request)
    assert status_code == 201
    assert task.schema_sha256 == preflight.schema_sha256
    assert task.spec_sha256 == preflight.spec_sha256

    _run(deps, task.task_id)

    done = api.status(task.task_id, "owner")
    assert done.status == "done"
    assert done.receipt is not None
    assert done.receipt.outcome == "already_certified_same_spec"


def test_confirmation_binds_the_exact_preflight_evidence(dataset, monkeypatch):
    _deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1], pa.int64())}))
    request = _request(published)
    original = metadb.catalog_managed_local_revision_certification_facts

    def unknown(exact):
        return {**original(exact), "row_count": None}

    monkeypatch.setattr(
        api.metadb, "catalog_managed_local_revision_certification_facts", unknown)
    estimate = api.preflight(request, "owner")
    assert estimate.needs_confirmation is True and estimate.reason == "unknown_size"
    with pytest.raises(APIError) as missing:
        _submit(request)
    assert missing.value.status_code == 409
    with pytest.raises(APIError) as wrong:
        _submit(request.model_copy(update={"confirmation_sha256": "0" * 64}))
    assert wrong.value.status_code == 409
    task, status_code = _submit(request.model_copy(update={
        "confirmation_sha256": estimate.confirmation_sha256}))
    assert status_code == 201 and task.status == "queued"


def test_oversized_key_name_is_rejected_by_direct_and_http_admission(
        dataset, monkeypatch):
    _deps, publish = dataset
    key = "k" * 257
    published = publish(pa.table({key: pa.array([1, 2], pa.int64())}))
    body = {
        "datasetId": published["dataset_id"],
        "revisionId": published["revision_id"],
        "keyColumns": [key],
    }
    with pytest.raises(ValidationError):
        api.RowIdentityCertificationRequestV1.model_validate(body)
    with pytest.raises(ValueError):
        metadb.submit_row_identity_certification_task(
            uid="owner", submission_id=str(uuid.uuid4()),
            dataset_id=published["dataset_id"], revision_id=published["revision_id"],
            dataset_name="long key", keys=[key],
            schema_sha256="0" * 64, spec_sha256="1" * 64,
            supported=True, confirmation_sha256="2" * 64,
            estimated_rows=2, estimated_bytes=16, artifact_uri="unused")
    monkeypatch.setattr(
        api, "_preflight",
        lambda *_args, **_kwargs: pytest.fail("invalid key names must not reach preflight"))

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/catalog/row-identity-certifications/preflight",
        headers={"X-DP-User": "owner"}, json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_long"


@pytest.mark.parametrize(("table", "outcome"), [
    (pa.table({"id": pa.array([1, 1], pa.int64())}), "duplicate_key"),
    (pa.table({"id": pa.array([1, None], pa.int64())}), "null_key"),
    (pa.table({"id": pa.array([1.0, 2.0], pa.float64())}), "unsupported_type"),
])
def test_invalid_proofs_have_typed_receipts_and_leave_no_certificate(
        dataset, table, outcome, monkeypatch):
    deps, publish = dataset
    published = publish(table)
    request = _request(published)
    estimate = api.preflight(request, "owner")
    if outcome == "unsupported_type":
        assert estimate.supported is False
        monkeypatch.setattr(
            row_identity_tasks, "certify_and_commit_exact_row_identity",
            lambda *_args, **_kwargs: pytest.fail("unsupported types must not scan"))
    task, _status_code = _submit(request)
    _run(deps, task.task_id)
    failed = api.status(task.task_id, "owner")
    assert failed.status == "failed"
    assert failed.receipt is not None and failed.receipt.outcome == outcome
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is None


def test_same_and_conflicting_retained_specs_have_distinct_receipts(dataset):
    deps, publish = dataset
    published = publish(pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "name": pa.array(["a", "b"]),
    }))
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"],
        revision_id=published["revision_id"])
    from hub.row_identity import certify_and_persist_exact_row_identity
    certify_and_persist_exact_row_identity(deps.storage, exact, ["id"])

    same, _ = _submit(_request(published, ("id",)))
    _run(deps, same.task_id)
    same_done = api.status(same.task_id, "owner")
    assert same_done.status == "done"
    assert same_done.receipt is not None
    assert same_done.receipt.outcome == "already_certified_same_spec"

    conflict, _ = _submit(_request(published, ("name",)))
    _run(deps, conflict.task_id)
    conflict_done = api.status(conflict.task_id, "owner")
    assert conflict_done.status == "failed"
    assert conflict_done.receipt is not None
    assert conflict_done.receipt.outcome == "conflicting_retained_spec"


def test_cancel_and_unavailable_revision_never_leave_a_certificate(dataset):
    deps, publish = dataset
    cancelled_source = publish(pa.table({"id": pa.array([1, 2], pa.int64())}), name="cancel")
    cancelled, _ = _submit(_request(cancelled_source))
    result = api.cancel(cancelled.task_id, "owner")
    assert result.status == "queued" and result.can_cancel is True
    _run(deps, cancelled.task_id)
    result = api.status(cancelled.task_id, "owner")
    assert result.status == "cancelled"
    assert result.receipt is not None and result.receipt.outcome == "cancelled"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, cancelled_source["dataset_id"], cancelled_source["revision_id"]) is None

    stale_source = publish(pa.table({"id": pa.array([1, 2], pa.int64())}), name="stale")
    stale, _ = _submit(_request(stale_source))
    artifact = metadb.managed_local_file_revision_artifact(
        stale_source["dataset_id"], stale_source["revision_id"])
    assert artifact is not None
    os.unlink(artifact)
    _run(deps, stale.task_id)
    failed = api.status(stale.task_id, "owner")
    assert failed.status == "failed"
    assert failed.receipt is not None
    assert failed.receipt.outcome == "stale_or_unavailable_revision"


def test_schema_change_after_admission_is_stale_and_retains_no_certificate(dataset):
    deps, publish = dataset
    original = publish(pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "vector": pa.array(
            [[1.0, 2.0], [3.0, 4.0]],
            type=pa.list_(pa.float32(), 2),
        ),
    }), name="schema-change")
    task, _ = _submit(_request(original))
    artifact = metadb.managed_local_file_revision_artifact(
        original["dataset_id"], original["revision_id"])
    assert artifact is not None
    replacement = f"{artifact}.replacement"
    pq.write_table(pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "vector": pa.array(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
            type=pa.list_(pa.float32(), 3),
        ),
    }), replacement)
    os.chmod(replacement, 0o600)
    os.replace(replacement, artifact)

    _run(deps, task.task_id)

    failed = api.status(task.task_id, "owner")
    assert failed.status == "failed"
    assert failed.receipt is not None
    assert failed.receipt.outcome == "stale_or_unavailable_revision"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, original["dataset_id"], original["revision_id"]) is None


def test_internal_validation_failure_is_not_misreported_as_stale(dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))

    def fail_validation(*_args, **_kwargs):
        raise RowIdentityValidationError("internal validation failed")

    monkeypatch.setattr(
        row_identity_tasks, "certify_and_commit_exact_row_identity",
        fail_validation)

    _run(deps, task.task_id)

    failed = api.status(task.task_id, "owner")
    assert failed.status == "failed"
    assert failed.receipt is not None and failed.receipt.outcome == "failed"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is None


def test_recovery_claims_one_pending_submission(dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))
    assert task.task_id in metadb.recoverable_row_identity_certification_task_ids()
    monkeypatch.setattr(
        row_identity_tasks, "dispatch",
        lambda task_id, _deps: row_identity_tasks._worker(task_id, deps))
    row_identity_tasks.recover(deps)
    assert api.status(task.task_id, "owner").status == "done"


def test_running_cancel_interrupts_and_fences_the_proof_before_commit(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))
    proof_ready = threading.Event()
    interrupted = threading.Event()
    original_certify = row_identity_tasks.certify_and_commit_exact_row_identity
    original_scope = row_identity_tasks.db.run_scope

    @contextmanager
    def tracked_scope():
        with original_scope() as scope:
            class TrackedScope:
                def interrupt(self) -> None:
                    interrupted.set()
                    scope.interrupt()

            yield TrackedScope()

    def pause_after_proof(*args, commit, **kwargs):
        def delayed_commit(*commit_args):
            proof_ready.set()
            assert interrupted.wait(5), "running cancellation did not interrupt its scope"
            return commit(*commit_args)

        return original_certify(*args, commit=delayed_commit, **kwargs)

    monkeypatch.setattr(row_identity_tasks.db, "run_scope", tracked_scope)
    monkeypatch.setattr(
        row_identity_tasks, "certify_and_commit_exact_row_identity", pause_after_proof)
    worker = threading.Thread(target=_run, args=(deps, task.task_id), daemon=True)
    worker.start()
    assert proof_ready.wait(5), "worker did not finish the full proof"
    assert api.cancel(task.task_id, "owner").status == "running"
    worker.join(timeout=7)
    assert not worker.is_alive()
    terminal = api.status(task.task_id, "owner")
    assert interrupted.is_set()
    assert terminal.status == "cancelled"
    assert terminal.receipt is not None and terminal.receipt.outcome == "cancelled"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is None


def test_postgres_and_sqlite_expired_attempt_cannot_publish_a_certificate(dataset):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))
    first = metadb.claim_row_identity_certification_task(task.task_id, "expired-owner")
    assert first is not None
    first_attempt = first["attempts"][-1]["id"]
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"],
        revision_id=published["revision_id"])
    certificate = certify_exact_row_identity(deps.storage, exact, ["id"])
    certificate_doc = serialize_row_identity_coverage(
        certificate, exact, task.spec_sha256)
    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert artifact is not None
    artifact_info = os.stat(artifact)
    with metadb.session() as session:
        attempt = session.get(metadb.DurableTaskAttempt, first_attempt)
        assert attempt is not None
        attempt.lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    second = metadb.claim_row_identity_certification_task(task.task_id, "replacement-owner")
    assert second is not None and second["attempts"][-1]["id"] != first_attempt
    assert metadb.finish_row_identity_certification_scan(
        task.task_id, first_attempt, "expired-owner", certificate_doc,
        artifact_dev=artifact_info.st_dev, artifact_ino=artifact_info.st_ino,
    ) is False
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is None


def test_terminal_commit_response_loss_reopens_the_same_certified_receipt(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    request = _request(published, submission_id=uuid.uuid4())
    task, _ = _submit(request)
    original = metadb.finish_row_identity_certification_scan
    original_scan = __import__("hub.row_identity", fromlist=["DuckDBAdapter"]).DuckDBAdapter.scan
    scans = 0

    def counted_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        return original_scan(*args, **kwargs)

    def lose_response(*args, **kwargs):
        assert original(*args, **kwargs) is True
        raise RuntimeError("simulated response loss after commit")

    monkeypatch.setattr("hub.row_identity.DuckDBAdapter.scan", counted_scan)
    monkeypatch.setattr(
        row_identity_tasks.metadb, "finish_row_identity_certification_scan", lose_response)
    _run(deps, task.task_id)
    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "done"
    assert terminal.receipt is not None and terminal.receipt.outcome == "certified"
    assert task.task_id not in metadb.recoverable_row_identity_certification_task_ids()
    row_identity_tasks.recover(deps)
    replay, replay_status = _submit(request)
    assert replay_status == 200 and replay.receipt == terminal.receipt
    assert scans == 1
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalRowIdentityCertificate).where(
            metadb.ManagedLocalRowIdentityCertificate.revision_id
            == published["revision_id"])) == 1
        assert session.scalar(select(func.count()).select_from(
            metadb.DurableTaskInboxItem).where(
            metadb.DurableTaskInboxItem.task_id == task.task_id)) == 1


def test_uncertain_certificate_transaction_rolls_back_before_failure_receipt(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))
    original = metadb._managed_local_row_identity_certificate_store

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated transaction uncertainty")

    monkeypatch.setattr(
        metadb, "_managed_local_row_identity_certificate_store", fail_after_insert)
    _run(deps, task.task_id)
    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "failed"
    assert terminal.receipt is not None and terminal.receipt.outcome == "failed"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is None


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL admission race")
def test_postgres_concurrent_submit_has_one_task_attempt_and_revision_pin(dataset):
    _deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    for _round in range(5):
        request = _request(published, submission_id=uuid.uuid4())

        def submit(_index: int):
            return _submit(request)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        assert results[0].task_id == results[1].task_id
        with metadb.session() as session:
            assert session.scalar(select(func.count()).select_from(
                metadb.DurableTaskAttempt).where(
                metadb.DurableTaskAttempt.task_id == results[0].task_id)) == 1
            assert session.scalar(select(func.count()).select_from(
                metadb.LocalResultReference).where(
                metadb.LocalResultReference.owner_kind == "durable_task",
                metadb.LocalResultReference.owner_key == results[0].task_id)) == 1


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL lifecycle lock race")
def test_postgres_finish_and_preflight_share_registry_revision_artifact_lock_order(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    request = _request(published)
    task, _ = _submit(request)
    claim = metadb.claim_row_identity_certification_task(task.task_id, "finisher")
    assert claim is not None
    attempt_id = claim["attempts"][-1]["id"]
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"],
        revision_id=published["revision_id"])
    certificate = certify_exact_row_identity(deps.storage, exact, ["id"])
    certificate_doc = serialize_row_identity_coverage(
        certificate, exact, task.spec_sha256)
    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert artifact is not None
    artifact_info = os.stat(artifact)

    original_lock = metadb._lock_local_result_registry
    reader_has_registry = threading.Event()
    finisher_entered_registry = threading.Event()
    calls: dict[str, int] = {}
    calls_lock = threading.Lock()

    def ordered_lock(session):
        name = threading.current_thread().name
        with calls_lock:
            calls[name] = calls.get(name, 0) + 1
            first = calls[name] == 1
        if name == "preflight-reader" and first:
            row = original_lock(session)
            reader_has_registry.set()
            assert finisher_entered_registry.wait(5)
            return row
        if name == "certificate-finisher" and first:
            finisher_entered_registry.set()
            assert reader_has_registry.wait(5)
        return original_lock(session)

    monkeypatch.setattr(metadb, "_lock_local_result_registry", ordered_lock)

    def finish():
        threading.current_thread().name = "certificate-finisher"
        return metadb.finish_row_identity_certification_scan(
            task.task_id, attempt_id, "finisher", certificate_doc,
            artifact_dev=artifact_info.st_dev, artifact_ino=artifact_info.st_ino)

    def preflight():
        threading.current_thread().name = "preflight-reader"
        return api.preflight(request, "owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = pool.submit(finish)
        read = pool.submit(preflight)
        assert finished.result(timeout=10) is True
        assert read.result(timeout=10).dataset_ref == exact

    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "done"
    assert terminal.receipt is not None and terminal.receipt.outcome == "certified"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        deps.storage, published["dataset_id"], published["revision_id"]) is not None


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL certificate lock race")
def test_postgres_existing_certificate_finish_and_standalone_store_do_not_deadlock(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))
    claim = metadb.claim_row_identity_certification_task(task.task_id, "finisher")
    assert claim is not None
    attempt_id = claim["attempts"][-1]["id"]
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"],
        revision_id=published["revision_id"])
    certificate = certify_exact_row_identity(deps.storage, exact, ["id"])
    certificate_doc = serialize_row_identity_coverage(
        certificate, exact, task.spec_sha256)
    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert artifact is not None
    artifact_info = os.stat(artifact)
    metadb.managed_local_row_identity_certificate_store(
        published["dataset_id"], published["revision_id"], certificate_doc,
        artifact_dev=artifact_info.st_dev, artifact_ino=artifact_info.st_ino)

    original_store = metadb._managed_local_row_identity_certificate_store
    standalone_has_artifact = threading.Event()
    finisher_entered_store = threading.Event()

    def ordered_store(*args, **kwargs):
        if threading.current_thread().name == "certificate-finisher":
            finisher_entered_store.set()
            assert standalone_has_artifact.wait(5)
        return original_store(*args, **kwargs)

    monkeypatch.setattr(
        metadb, "_managed_local_row_identity_certificate_store", ordered_store)

    def standalone_store():
        threading.current_thread().name = "standalone-store"
        with metadb.session() as session:
            revision = session.get(
                metadb.ManagedLocalFileRevision, published["revision_id"],
                with_for_update=True)
            assert revision is not None
            assert session.get(
                metadb.LocalResultArtifact, revision.artifact_uri,
                with_for_update=True) is not None
            standalone_has_artifact.set()
            assert finisher_entered_store.wait(5)
            return original_store(
                session, published["dataset_id"], published["revision_id"],
                certificate_doc, artifact_info.st_dev, artifact_info.st_ino)

    def finish():
        threading.current_thread().name = "certificate-finisher"
        return metadb.finish_row_identity_certification_scan(
            task.task_id, attempt_id, "finisher", certificate_doc,
            artifact_dev=artifact_info.st_dev, artifact_ino=artifact_info.st_ino)

    with ThreadPoolExecutor(max_workers=2) as pool:
        standalone = pool.submit(standalone_store)
        assert standalone_has_artifact.wait(5)
        finished = pool.submit(finish)
        assert standalone.result(timeout=10)[1] is False
        assert finished.result(timeout=10) is True

    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "done"
    assert terminal.receipt is not None
    assert terminal.receipt.outcome == "already_certified_same_spec"


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"),
    reason="requires a dedicated PostgreSQL certificate insert race")
def test_postgres_first_same_spec_race_reports_finish_as_the_loser(
        dataset, monkeypatch):
    deps, publish = dataset
    published = publish(pa.table({"id": pa.array([1, 2], pa.int64())}))
    task, _ = _submit(_request(published))
    claim = metadb.claim_row_identity_certification_task(task.task_id, "finisher")
    assert claim is not None
    attempt_id = claim["attempts"][-1]["id"]
    exact = ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"],
        revision_id=published["revision_id"])
    certificate = certify_exact_row_identity(deps.storage, exact, ["id"])
    certificate_doc = serialize_row_identity_coverage(
        certificate, exact, task.spec_sha256)
    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert artifact is not None
    artifact_info = os.stat(artifact)

    original_store = metadb._managed_local_row_identity_certificate_store
    finisher_entered_store = threading.Event()
    standalone_committed = threading.Event()

    def delayed_store(*args, **kwargs):
        if threading.current_thread().name == "certificate-finisher":
            finisher_entered_store.set()
            assert standalone_committed.wait(5)
        return original_store(*args, **kwargs)

    monkeypatch.setattr(
        metadb, "_managed_local_row_identity_certificate_store", delayed_store)

    def finish():
        threading.current_thread().name = "certificate-finisher"
        return metadb.finish_row_identity_certification_scan(
            task.task_id, attempt_id, "finisher", certificate_doc,
            artifact_dev=artifact_info.st_dev, artifact_ino=artifact_info.st_ino)

    def standalone_store():
        threading.current_thread().name = "standalone-store"
        assert finisher_entered_store.wait(5)
        descriptor = metadb.managed_local_row_identity_certificate_store(
            published["dataset_id"], published["revision_id"], certificate_doc,
            artifact_dev=artifact_info.st_dev, artifact_ino=artifact_info.st_ino)
        standalone_committed.set()
        return descriptor

    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = pool.submit(finish)
        standalone = pool.submit(standalone_store)
        assert standalone.result(timeout=10)["proofStatus"] == "certified"
        assert finished.result(timeout=10) is True

    terminal = api.status(task.task_id, "owner")
    assert terminal.status == "done"
    assert terminal.receipt is not None
    assert terminal.receipt.outcome == "already_certified_same_spec"
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.ManagedLocalRowIdentityCertificate).where(
            metadb.ManagedLocalRowIdentityCertificate.revision_id
            == published["revision_id"])) == 1
