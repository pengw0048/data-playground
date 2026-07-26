"""Durable reusable logical-row proof contracts for exact managed-local revisions."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hub import metadb
from hub.models import ExactDatasetRef
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import catalog as catalog_routes
from hub.row_identity import (
    RowIdentityValidationError,
    certify_and_persist_exact_row_identity,
    certify_exact_row_identity,
    serialize_row_identity_coverage,
)
from hub.sparse_outputs import SparseOutputAdmissionRequest, admit_sparse_output
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'row-identity-certificates.db'}")
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
def local_catalog(tmp_path):
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        yield storage, catalog
    finally:
        storage.close()


def _publish(storage, catalog, logical_uri: str, table: pa.Table) -> dict:
    run_id = f"row-identity-certificate-{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
    pq.write_table(table, artifact)
    storage.commit_result(artifact, run_id)
    try:
        published = catalog.publish_managed_local_file_output(
            name="row_identity_certificate", logical_uri=logical_uri, artifact_uri=artifact)
    except Exception:
        storage.abort_result(artifact, run_id)
        raise
    assert storage.release_result(artifact, run_id) is True
    return published


def _exact(published: dict) -> ExactDatasetRef:
    return ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"])


def _revision_detail(monkeypatch, catalog, storage, published: dict):
    monkeypatch.setattr(catalog_routes, "get_deps", lambda: SimpleNamespace(
        catalog=catalog, storage=storage, resolve_adapter=lambda _uri: DuckDBAdapter()))
    return catalog_routes.open_dataset_revision(published["dataset_id"], published["revision_id"])


def _concurrent_standalone_certifications(
        storage, exact: ExactDatasetRef, key_specs: tuple[tuple[str, ...], tuple[str, ...]],
        monkeypatch, *, synchronize_sqlite_missing_reads: bool,
) -> list[dict | Exception]:
    store_barrier = threading.Barrier(2)
    original_store = metadb.managed_local_row_identity_certificate_store

    def synchronized_store(*args, **kwargs):
        store_barrier.wait(timeout=10)
        return original_store(*args, **kwargs)

    def certify(keys: tuple[str, ...]) -> dict | Exception:
        try:
            return certify_and_persist_exact_row_identity(storage, exact, list(keys))
        except Exception as exc:  # noqa: BLE001 — the assertion verifies the public exception type
            return exc

    with monkeypatch.context() as patch:
        patch.setattr(
            metadb, "managed_local_row_identity_certificate_store", synchronized_store)
        if synchronize_sqlite_missing_reads:
            missing_barrier = threading.Barrier(2)
            original_get = Session.get

            def synchronized_missing_get(self, entity, ident, **kwargs):
                row = original_get(self, entity, ident, **kwargs)
                if entity is metadb.ManagedLocalRowIdentityCertificate and row is None:
                    missing_barrier.wait(timeout=10)
                return row

            patch.setattr(Session, "get", synchronized_missing_get)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(certify, keys) for keys in key_specs]
            return [future.result(timeout=20) for future in futures]


def _assert_concurrent_first_certifications(
        storage, catalog, tmp_path, monkeypatch, *,
        key_specs: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    published = _publish(storage, catalog, str(tmp_path / "concurrent.parquet"), pa.table({
        "id": pa.array([1, 2], pa.int32()),
        "name": pa.array(["first", "second"]),
    }))
    exact = _exact(published)
    dialect = metadb.engine().dialect.name
    results = _concurrent_standalone_certifications(
        storage, exact, key_specs, monkeypatch,
        synchronize_sqlite_missing_reads=dialect == "sqlite")

    if key_specs[0] == key_specs[1]:
        assert all(isinstance(result, dict) for result in results)
        assert results[0] == results[1]
        expected = results[0]
    else:
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], metadb.RowIdentityCertificateConflict)
        assert not isinstance(failures[0], IntegrityError)
        expected = successes[0]

    assert metadb.managed_local_row_identity_certificate_descriptor(
        storage, exact.dataset_id, exact.revision_id) == expected


@pytest.mark.parametrize("key_specs", [
    (("id",), ("id",)),
    (("id",), ("name",)),
], ids=["same-spec", "different-spec"])
def test_sqlite_concurrent_first_standalone_certification_has_one_typed_winner(
        local_catalog, tmp_path, monkeypatch, key_specs):
    if metadb.engine().dialect.name != "sqlite":
        pytest.skip("SQLite concurrency contract")
    _assert_concurrent_first_certifications(
        *local_catalog, tmp_path, monkeypatch, key_specs=key_specs)


@pytest.mark.parametrize("key_specs", [
    (("id",), ("id",)),
    (("id",), ("name",)),
], ids=["same-spec", "different-spec"])
def test_postgres_concurrent_first_standalone_certification_has_one_typed_winner(
        local_catalog, tmp_path, monkeypatch, key_specs):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency contract")
    _assert_concurrent_first_certifications(
        *local_catalog, tmp_path, monkeypatch, key_specs=key_specs)


def test_certified_descriptor_is_reusable_after_head_moves_without_rescanning(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "identity.parquet")
    first = _publish(storage, catalog, logical_uri, pa.table({"id": pa.array([1, 2], pa.int32())}))
    descriptor = certify_and_persist_exact_row_identity(storage, _exact(first), ["id"])
    _publish(storage, catalog, logical_uri, pa.table({"id": pa.array([3], pa.int32())}))

    assert descriptor == {
        "datasetId": first["dataset_id"], "revisionId": first["revision_id"],
        "proofStatus": "certified", "fields": [{"name": "id", "arrowType": "int32"}],
        "encodingVersion": "row-identity-v1",
    }
    monkeypatch.setattr(
        "hub.row_identity.certify_exact_row_identity",
        lambda *_args, **_kwargs: pytest.fail("revision detail must not scan for row identity"))
    detail = _revision_detail(monkeypatch, catalog, storage, first)

    assert detail.row_identity.model_dump(by_alias=True, exclude_none=True) == descriptor
    assert detail.preview.rows == [{"id": 1}, {"id": 2}]


def test_declared_or_invalid_key_does_not_advertise_row_addressing(local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    declared = _publish(storage, catalog, str(tmp_path / "declared.parquet"),
                        pa.table({"id": pa.array([1], pa.int32())}))
    catalog.set_declared_key(metadb.managed_local_file_revision_artifact(
        declared["dataset_id"], declared["revision_id"]), ["id"])
    detail = _revision_detail(monkeypatch, catalog, storage, declared)
    assert detail.row_identity.proof_status == "unavailable"
    assert detail.row_identity.fields == []

    invalid = _publish(storage, catalog, str(tmp_path / "invalid.parquet"),
                       pa.table({"id": pa.array([1, 1], pa.int32())}))
    with pytest.raises(RowIdentityValidationError, match="row identity evidence is invalid"):
        certify_and_persist_exact_row_identity(storage, _exact(invalid), ["id"])
    assert metadb.managed_local_row_identity_certificate_descriptor(
        storage, invalid["dataset_id"], invalid["revision_id"]) is None


def test_certificate_replay_is_idempotent_but_refuses_different_order_or_corruption(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "composite.parquet"), pa.table({
        "id": pa.array([1, 2], pa.int32()),
        "name": pa.array(["private-row-key-a", "private-row-key-b"]),
    }))
    exact = _exact(published)
    first = certify_and_persist_exact_row_identity(storage, exact, ["id", "name"])
    assert certify_and_persist_exact_row_identity(storage, exact, ["id", "name"]) == first
    different = certify_exact_row_identity(storage, exact, ["name", "id"])
    with metadb.session() as session:
        retained = session.get(metadb.ManagedLocalRowIdentityCertificate, exact.revision_id)
        artifact_identity = retained.artifact_dev, retained.artifact_ino
    with pytest.raises(metadb.RowIdentityCertificateConflict):
        metadb.managed_local_row_identity_certificate_store(
            exact.dataset_id, exact.revision_id,
            serialize_row_identity_coverage(different, exact, different.spec.digest),
            artifact_dev=artifact_identity[0], artifact_ino=artifact_identity[1])

    with metadb.session() as session:
        row = session.get(metadb.ManagedLocalRowIdentityCertificate, exact.revision_id)
        assert "private-row-key" not in row.certificate_doc
        row.certificate_doc = "{}"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        storage, exact.dataset_id, exact.revision_id) is None


def test_certificate_fails_closed_when_the_exact_artifact_is_not_ready(local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "unavailable.parquet"),
                         pa.table({"id": pa.array([1], pa.int32())}))
    exact = _exact(published)
    certify_and_persist_exact_row_identity(storage, exact, ["id"])

    with metadb.session() as session:
        revision = session.get(metadb.ManagedLocalFileRevision, exact.revision_id)
        session.get(metadb.LocalResultArtifact, revision.artifact_uri).state = "writing"
    assert metadb.managed_local_row_identity_certificate_descriptor(
        storage, exact.dataset_id, exact.revision_id) is None


def test_descriptor_shape_overflow_fails_closed_without_breaking_revision_detail(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    key = "k" * 257
    published = _publish(storage, catalog, str(tmp_path / "long-key.parquet"), pa.table({
        key: pa.array([1], pa.int32()), "payload": pa.array(["a"]),
    }))
    owner, canvas = f"owner-{uuid.uuid4().hex}", f"canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=owner, name="Long key owner"))
        session.add(metadb.Canvas(id=canvas, owner_id=owner, name="Long key", doc="{}"))

    request = SparseOutputAdmissionRequest(
        owner_id=owner, canvas_id=canvas, submission_id="long-key",
        dataset_ref=_exact(published),
        select_config={"expr": f'"{key}", payload AS score'},
        identity_columns=[key],
        provenance={"idempotencyKey": "long-key", "provenance": "manual"},
    )
    admitted = admit_sparse_output(storage, request)
    replay = admit_sparse_output(storage, request)

    assert admitted.created is True
    assert replay.created is False and replay.id == admitted.id
    assert metadb.managed_local_row_identity_certificate_descriptor(
        storage, published["dataset_id"], published["revision_id"]) is None
    detail = _revision_detail(monkeypatch, catalog, storage, published)
    assert detail.preview.rows == [{key: 1, "payload": "a"}]
    assert detail.row_identity.proof_status == "unavailable"


@pytest.mark.parametrize("mutation", ["missing", "replaced"])
def test_descriptor_fails_closed_when_artifact_file_is_missing_or_replaced(
        local_catalog, tmp_path, mutation):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / f"{mutation}.parquet"),
                         pa.table({"id": pa.array([1], pa.int32())}))
    exact = _exact(published)
    certify_and_persist_exact_row_identity(storage, exact, ["id"])
    artifact = metadb.managed_local_file_revision_artifact(exact.dataset_id, exact.revision_id)
    assert artifact is not None
    if mutation == "missing":
        os.unlink(artifact)
    else:
        replacement = tmp_path / "replacement.parquet"
        pq.write_table(pa.table({"id": pa.array([2], pa.int32())}), replacement)
        os.chmod(replacement, 0o600)
        os.replace(replacement, artifact)

    assert metadb.managed_local_row_identity_certificate_descriptor(
        storage, exact.dataset_id, exact.revision_id) is None
