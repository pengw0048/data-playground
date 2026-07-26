"""Durable reusable logical-row proof contracts for exact managed-local revisions."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hub import metadb
from hub.media_cells import read_managed_local_media_cell
from hub.models import DatasetRevisionDetail, ExactDatasetRef, MediaCellRequest
from hub.plugins.adapters import DuckDBAdapter, ManagedLocalFileRevisionAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import catalog as catalog_routes
from hub.row_identity import (
    PREVIEW_ROW_IDENTITIES_MAX_JSON_BYTES,
    RowIdentityValidationError,
    canonicalize_preview_row_identities,
    certify_and_persist_exact_row_identity,
    certify_exact_row_identity,
    serialize_row_identity_coverage,
)
from hub.sparse_outputs import SparseOutputAdmissionRequest, admit_sparse_output
from hub.storage import LocalStorage


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


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
        "proofStatus": "certified", "certificationSupported": True,
        "fields": [{"name": "id", "arrowType": "int32"}],
        "encodingVersion": "row-identity-v1",
    }
    monkeypatch.setattr(
        "hub.row_identity.certify_exact_row_identity",
        lambda *_args, **_kwargs: pytest.fail("revision detail must not scan for row identity"))
    detail = _revision_detail(monkeypatch, catalog, storage, first)

    assert detail.row_identity.model_dump(by_alias=True, exclude_none=True) == descriptor
    assert detail.preview.rows == [{"id": 1}, {"id": 2}]
    assert detail.preview.model_dump(by_alias=True)["rowIdentities"] == [
        [{"name": "id", "arrowType": "int32", "value": "1"}],
        [{"name": "id", "arrowType": "int32", "value": "2"}],
    ]


def test_declared_or_invalid_key_does_not_advertise_row_addressing(local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    declared = _publish(storage, catalog, str(tmp_path / "declared.parquet"),
                        pa.table({"id": pa.array([1], pa.int32())}))
    catalog.set_declared_key(metadb.managed_local_file_revision_artifact(
        declared["dataset_id"], declared["revision_id"]), ["id"])
    detail = _revision_detail(monkeypatch, catalog, storage, declared)
    assert detail.row_identity.proof_status == "unavailable"
    assert detail.row_identity.certification_supported is True
    assert detail.row_identity.fields == []
    assert detail.preview.row_identities is None

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
    assert detail.row_identity.certification_supported is True
    assert detail.preview.row_identities is None


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


def test_revision_detail_model_requires_one_to_one_complete_certified_identities():
    identity = [
        {"name": "id", "arrowType": "int64", "value": "1"},
        {"name": "key", "arrowType": "string", "value": "row-1"},
    ]
    document = {
        "datasetId": "dataset-1",
        "revisionId": "revision-1",
        "summary": {},
        "preview": {
            "columns": [],
            "rows": [{"id": 1}, {"id": 2}],
            "rowIdentities": [identity, None],
            "hasMore": False,
        },
        "rowIdentity": {
            "datasetId": "dataset-1",
            "revisionId": "revision-1",
            "proofStatus": "certified",
            "certificationSupported": True,
            "fields": [
                {"name": "id", "arrowType": "int64"},
                {"name": "key", "arrowType": "string"},
            ],
            "encodingVersion": "row-identity-v1",
        },
    }

    parsed = DatasetRevisionDetail.model_validate(document)
    assert parsed.preview.row_identities is not None
    assert parsed.preview.row_identities[1] is None

    misaligned = copy.deepcopy(document)
    misaligned["preview"]["rowIdentities"] = [identity]
    with pytest.raises(ValidationError, match="align one-to-one"):
        DatasetRevisionDetail.model_validate(misaligned)

    partial = copy.deepcopy(document)
    partial["preview"]["rowIdentities"][0] = identity[:1]
    with pytest.raises(ValidationError, match="every certified field in order"):
        DatasetRevisionDetail.model_validate(partial)

    reordered = copy.deepcopy(document)
    reordered["preview"]["rowIdentities"][0] = list(reversed(identity))
    with pytest.raises(ValidationError, match="every certified field in order"):
        DatasetRevisionDetail.model_validate(reordered)

    missing_sidecar = copy.deepcopy(document)
    missing_sidecar["preview"]["rowIdentities"] = None
    with pytest.raises(ValidationError, match="requires aligned preview identities"):
        DatasetRevisionDetail.model_validate(missing_sidecar)

    unavailable = copy.deepcopy(document)
    unavailable["rowIdentity"].update({
        "proofStatus": "unavailable",
        "fields": [],
        "encodingVersion": None,
    })
    with pytest.raises(ValidationError, match="cannot expose preview identities"):
        DatasetRevisionDetail.model_validate(unavailable)


def test_preview_identities_canonicalize_exact_composite_values_and_feed_media_reads(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    signed = [-2**63, 2**63 - 1]
    unsigned = [0, 2**64 - 1]
    strings = ["\N{LATIN SMALL LETTER E WITH ACUTE}", "e\N{COMBINING ACUTE ACCENT}"]
    media = [PNG, PNG + b"second"]
    published = _publish(storage, catalog, str(tmp_path / "canonical.parquet"), pa.table({
        "signed": pa.array(signed, pa.int64()),
        "unsigned": pa.array(unsigned, pa.uint64()),
        "key": pa.array(strings, pa.string()),
        "media": pa.array(media, pa.binary()),
    }))
    certify_and_persist_exact_row_identity(
        storage, _exact(published), ["signed", "unsigned", "key"])

    detail = _revision_detail(monkeypatch, catalog, storage, published)
    assert detail.preview.rows == [
        {"signed": signed[0], "unsigned": unsigned[0], "key": strings[0],
         "media": f"<{len(PNG)} bytes>"},
        {"signed": signed[1], "unsigned": unsigned[1], "key": strings[1],
         "media": f"<{len(PNG + b'second')} bytes>"},
    ]
    sidecar = detail.preview.model_dump(by_alias=True, mode="json")["rowIdentities"]
    assert sidecar == [
        [
            {"name": "signed", "arrowType": "int64", "value": str(signed[0])},
            {"name": "unsigned", "arrowType": "uint64", "value": str(unsigned[0])},
            {"name": "key", "arrowType": "string", "value": strings[0]},
        ],
        [
            {"name": "signed", "arrowType": "int64", "value": str(signed[1])},
            {"name": "unsigned", "arrowType": "uint64", "value": str(unsigned[1])},
            {"name": "key", "arrowType": "string", "value": strings[1]},
        ],
    ]
    assert detail.row_identity.proof_status == "certified"
    assert detail.row_identity.certification_supported is True

    for identity, expected_media in zip(sidecar, media, strict=True):
        request = MediaCellRequest.model_validate({
            "identity": identity,
            "column": "media",
        })
        content, content_type = read_managed_local_media_cell(
            storage=storage,
            dataset_uri=published["table"].uri,
            dataset_id=published["dataset_id"],
            revision_id=published["revision_id"],
            request=request,
        )
        assert content == expected_media
        assert content_type == "image/png"


def test_exact_preview_tags_only_bounded_raw_media_evidence(local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "raw-media.parquet"), pa.table({
        "id": pa.array([1], pa.int32()),
        "image": pa.array([PNG], pa.binary()),
        "video": pa.array([b"\x1aE\xdf\xa3webm"], pa.binary()),
        "bytes": pa.array([b"not a supported media value"], pa.binary()),
    }))

    detail = _revision_detail(monkeypatch, catalog, storage, published)
    columns = {column.name: column for column in detail.preview.columns}

    assert columns["image"].capabilities == ["media"]
    assert columns["image"].media_kind == "image"
    assert columns["video"].capabilities == ["media"]
    assert columns["video"].media_kind == "video"
    # No field-name heuristic or arbitrary binary fallback may manufacture a media capability.
    assert columns["bytes"].capabilities == []
    assert columns["bytes"].media_kind is None
    assert detail.preview.rows == [{
        "id": 1, "image": f"<{len(PNG)} bytes>", "video": "<8 bytes>",
        "bytes": "<27 bytes>",
    }]


def test_preview_identity_rows_fail_closed_without_breaking_certified_proof(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    within_limit = "\N{CJK UNIFIED IDEOGRAPH-754C}" * 8192
    over_limit = "\N{CJK UNIFIED IDEOGRAPH-754C}" * 8193
    published = _publish(storage, catalog, str(tmp_path / "bounded-values.parquet"), pa.table({
        "key": pa.array([within_limit, over_limit], pa.string()),
    }))
    certify_and_persist_exact_row_identity(storage, _exact(published), ["key"])

    detail = _revision_detail(monkeypatch, catalog, storage, published)

    assert detail.row_identity.proof_status == "certified"
    assert detail.preview.row_identities is not None
    assert detail.preview.row_identities[0][0].value == within_limit
    assert detail.preview.row_identities[1] is None


def test_preview_identity_schema_mismatch_fails_the_whole_sidecar_closed(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "schema-bound.parquet"), pa.table({
        "id": pa.array([1, 2], pa.int32()),
        "key": pa.array(["one", "two"], pa.string()),
        "payload": pa.array(["first", "second"], pa.string()),
    }))
    certificate = certify_exact_row_identity(storage, _exact(published), ["id", "key"])

    null_row = canonicalize_preview_row_identities(pa.table({
        "id": pa.array([1, 2], pa.int32()),
        "key": pa.array(["one", None], pa.string()),
        "payload": pa.array(["first", "second"], pa.string()),
    }), certificate)
    assert null_row == [
        [
            {"name": "id", "arrowType": "int32", "value": "1"},
            {"name": "key", "arrowType": "string", "value": "one"},
        ],
        None,
    ]
    assert canonicalize_preview_row_identities(pa.table({
        "id": pa.array([1, 2], pa.int32()),
        "payload": pa.array(["first", "second"], pa.string()),
    }), certificate) is None
    assert canonicalize_preview_row_identities(pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "key": pa.array(["one", "two"], pa.string()),
        "payload": pa.array(["first", "second"], pa.string()),
    }), certificate) is None
    assert canonicalize_preview_row_identities(pa.table({
        "id": pa.array([1, 2], pa.int32()),
        "key": pa.array(["one", "two"], pa.string()),
        "payload": pa.array([1, 2], pa.int32()),
    }), certificate) is None


def test_preview_identity_budget_preserves_alignment_and_allows_later_small_rows(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    left = [f"{index:03d}" + "a" * 8189 for index in range(99)] + ["last-left"]
    right = [f"{index:03d}" + "b" * 8189 for index in range(99)] + ["last-right"]
    published = _publish(storage, catalog, str(tmp_path / "budget.parquet"), pa.table({
        "left": pa.array(left, pa.string()),
        "right": pa.array(right, pa.string()),
    }))
    certify_and_persist_exact_row_identity(storage, _exact(published), ["left", "right"])

    detail = _revision_detail(monkeypatch, catalog, storage, published)
    payload = detail.preview.model_dump(by_alias=True, mode="json")["rowIdentities"]
    assert len(payload) == len(detail.preview.rows) == 100
    assert payload[0] is not None
    assert any(identity is None for identity in payload[:-1])
    assert payload[-1] == [
        {"name": "left", "arrowType": "string", "value": "last-left"},
        {"name": "right", "arrowType": "string", "value": "last-right"},
    ]
    assert len(json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ) <= PREVIEW_ROW_IDENTITIES_MAX_JSON_BYTES
    assert all(
        identity is None or all(len(value["value"]) in {8192, 9, 10} for value in identity)
        for identity in payload
    )


def test_empty_certified_preview_has_an_empty_aligned_sidecar(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "empty.parquet"), pa.table({
        "id": pa.array([], pa.int32()),
    }))
    certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])

    detail = _revision_detail(monkeypatch, catalog, storage, published)

    assert detail.row_identity.proof_status == "certified"
    assert detail.preview.rows == []
    assert detail.preview.row_identities == []


def test_revision_detail_holds_one_artifact_guard_through_materialization_and_identity(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "held-guard.parquet"), pa.table({
        "id": pa.array([1], pa.int32()),
    }))
    certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])
    state = SimpleNamespace(
        active=False, scope_calls=0, read_all=0, certificate=0,
        canonicalize=0, artifact=None)
    original_scope = catalog_routes.source_read_scope

    @contextlib.contextmanager
    def tracked_scope(*args, **kwargs):
        state.scope_calls += 1
        with original_scope(*args, **kwargs) as guards:
            assert state.active is False
            state.active = True
            state.artifact = os.fstat(guards[0].artifact_fileno())
            try:
                yield guards
            finally:
                state.active = False

    class GuardedReader:
        def __init__(self, table):
            self.table = table

        def read_all(self):
            assert state.active is True
            state.read_all += 1
            return self.table

    class GuardedAdapter(ManagedLocalFileRevisionAdapter):
        def revision_detail(self, uri: str, revision_id: str, *, preview_limit: int) -> dict:
            assert state.active is True
            raw = super().revision_detail(uri, revision_id, preview_limit=preview_limit)
            raw["preview_table"] = GuardedReader(raw["preview_table"])
            return raw

    original_certificate = metadb.managed_local_row_identity_certificate_for_artifact

    def tracked_certificate(*args, **kwargs):
        assert state.active is True
        artifact = state.artifact
        assert kwargs["artifact_dev"] == artifact.st_dev
        assert kwargs["artifact_ino"] == artifact.st_ino
        state.certificate += 1
        return original_certificate(*args, **kwargs)

    original_canonicalize = catalog_routes.canonicalize_preview_row_identities

    def tracked_canonicalize(*args, **kwargs):
        assert state.active is True
        state.canonicalize += 1
        return original_canonicalize(*args, **kwargs)

    monkeypatch.setattr(catalog_routes, "source_read_scope", tracked_scope)
    monkeypatch.setattr(catalog_routes, "_revision_adapter", lambda _uri: GuardedAdapter())
    monkeypatch.setattr(
        metadb, "managed_local_row_identity_certificate_for_artifact", tracked_certificate)
    monkeypatch.setattr(
        metadb, "managed_local_row_identity_certificate_descriptor",
        lambda *_args, **_kwargs: pytest.fail(
            "revision detail must reuse the held artifact instead of opening a descriptor guard"))
    monkeypatch.setattr(
        catalog_routes, "canonicalize_preview_row_identities", tracked_canonicalize)

    detail = _revision_detail(monkeypatch, catalog, storage, published)

    assert detail.preview.row_identities is not None
    assert state.active is False
    assert state.scope_calls == 1
    assert state.read_all == 1
    assert state.certificate == 1
    assert state.canonicalize == 1


def test_revision_detail_downgrades_invalid_certificate_sidecars(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    published = _publish(storage, catalog, str(tmp_path / "invalid-sidecar.parquet"), pa.table({
        "id": pa.array([1], pa.int32()),
    }))
    certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])
    monkeypatch.setattr(
        catalog_routes, "canonicalize_preview_row_identities",
        lambda _table, _certificate: None)

    detail = _revision_detail(monkeypatch, catalog, storage, published)

    assert detail.row_identity.certification_supported is True
    assert detail.row_identity.proof_status == "unavailable"
    assert detail.preview.row_identities is None
