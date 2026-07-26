"""Exact-cell media bytes stay bound to retained identity proof and source policy."""

from __future__ import annotations

import base64
import os
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from hub import metadb
from hub import media_cells
from hub.media_cells import (
    MediaCellIdentityInvalid,
    MediaCellIdentityUnavailable,
    MediaCellRowAmbiguous,
    MediaCellRowNotFound,
    MediaCellSourceDenied,
    MediaCellTooLarge,
    MediaCellUnsupported,
    read_managed_local_media_cell,
)
from hub.models import ExactDatasetRef, MediaCellRequest
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.row_identity import certify_and_persist_exact_row_identity
from hub.storage import LocalStorage

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'media-cells.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        if original_engine is None:
            metadb.init_db()


@pytest.fixture
def local_catalog(tmp_path):
    storage = LocalStorage(str(tmp_path / "allowed" / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        yield storage, catalog
    finally:
        storage.close()


def _publish(storage, catalog, tmp_path, table: pa.Table) -> dict:
    logical_uri = str(tmp_path / f"logical-{uuid.uuid4().hex}.parquet")
    run_id = f"media-cell-{uuid.uuid4().hex}"
    artifact = storage.begin_result("media-cell", run_id)
    pq.write_table(table, artifact)
    storage.commit_result(artifact, run_id)
    try:
        published = catalog.publish_managed_local_file_output(
            name="media_cells", logical_uri=logical_uri, artifact_uri=artifact)
    except Exception:
        storage.abort_result(artifact, run_id)
        raise
    assert storage.release_result(artifact, run_id) is True
    return published


def _exact(published: dict) -> ExactDatasetRef:
    return ExactDatasetRef(
        kind="exact", dataset_id=published["dataset_id"], revision_id=published["revision_id"])


def _request(value: str, *, column: str = "media", name: str = "id",
             arrow_type: str = "int32") -> MediaCellRequest:
    return MediaCellRequest.model_validate({
        "identity": [{"name": name, "arrowType": arrow_type, "value": value}],
        "column": column,
    })


def _read(storage, published: dict, request: MediaCellRequest, *, max_bytes: int = 1024):
    return read_managed_local_media_cell(
        storage=storage,
        dataset_uri=published["table"].uri,
        dataset_id=published["dataset_id"],
        revision_id=published["revision_id"],
        request=request,
        max_bytes=max_bytes,
    )


def test_certified_exact_cell_returns_bytes_with_conservative_headers_contract(
        local_catalog, tmp_path, monkeypatch):
    from hub.main import app
    from hub.routers import catalog as catalog_routes

    storage, catalog = local_catalog
    published = _publish(
        storage, catalog, tmp_path,
        pa.table({"id": pa.array([1, 2], pa.int32()), "media": [PNG, PNG + b"second"]}),
    )
    certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])

    content, content_type = _read(storage, published, _request("2"))

    assert content == PNG + b"second"
    assert content_type == "image/png"

    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.setattr(
        catalog_routes, "get_deps",
        lambda: type("Deps", (), {"storage": storage})(),
    )
    response = TestClient(app).post(
        f"/api/catalog/revisions/{published['dataset_id']}/{published['revision_id']}/media-cell",
        headers={"X-DP-User": "media-cell-test"},
        json={
            "identity": [{"name": "id", "arrowType": "int32", "value": "2"}],
            "column": "media",
        },
    )
    assert response.status_code == 200
    assert response.content == PNG + b"second"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    oversized_identity = "9" * 8192
    with pytest.raises(MediaCellIdentityInvalid):
        _read(storage, published, _request(oversized_identity))
    rejected = TestClient(app).post(
        f"/api/catalog/revisions/{published['dataset_id']}/{published['revision_id']}/media-cell",
        headers={"X-DP-User": "media-cell-test"},
        json={
            "identity": [{
                "name": "id", "arrowType": "int32", "value": oversized_identity,
            }],
            "column": "media",
        },
    )
    assert rejected.status_code == 422
    assert rejected.json() == {
        "detail": "media_cell_identity_invalid",
        "code": "media_cell_identity_invalid",
        "retryable": False,
    }


def test_missing_proof_and_missing_or_ambiguous_exact_rows_fail_closed(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(
        storage, catalog, tmp_path,
        pa.table({"id": pa.array([1, 2], pa.int32()), "media": [PNG, PNG]}),
    )
    with pytest.raises(MediaCellIdentityUnavailable):
        _read(storage, published, _request("1"))

    certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])
    with pytest.raises(MediaCellRowNotFound):
        _read(storage, published, _request("3"))

    artifact = metadb.managed_local_file_revision_artifact(
        published["dataset_id"], published["revision_id"])
    assert artifact is not None
    before = os.stat(artifact)
    with open(artifact, "wb") as stream:
        pq.write_table(
            pa.table({"id": pa.array([1, 1], pa.int32()), "media": [PNG, PNG]}),
            stream,
        )
    after = os.stat(artifact)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    with pytest.raises(MediaCellRowAmbiguous):
        _read(storage, published, _request("1"))


def test_identity_shape_values_columns_and_response_size_are_bounded(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    published = _publish(
        storage, catalog, tmp_path,
        pa.table({
            "key": ["safe", "' OR TRUE --"],
            "media": [PNG + b"safe", PNG + b"quoted"],
            "plain": [b"not-media", b"not-media"],
        }),
    )
    certify_and_persist_exact_row_identity(storage, _exact(published), ["key"])

    content, _content_type = _read(
        storage, published,
        _request("' OR TRUE --", name="key", arrow_type="string"),
    )
    assert content == PNG + b"quoted"
    with pytest.raises(MediaCellIdentityInvalid):
        _read(storage, published, _request("1", name="wrong"))
    with pytest.raises(MediaCellUnsupported):
        _read(
            storage, published,
            _request("safe", column='media" FROM read_parquet(\'/etc/passwd\') --',
                     name="key", arrow_type="string"),
        )
    with pytest.raises(MediaCellTooLarge):
        _read(
            storage, published,
            _request("safe", name="key", arrow_type="string"),
            max_bytes=len(PNG),
        )
    with pytest.raises(ValueError, match="Extra inputs"):
        MediaCellRequest.model_validate({
            "identity": [{"name": "key", "arrowType": "string", "value": "safe"}],
            "column": "media",
            "uri": "/etc/passwd",
        })


def test_exact_query_does_not_return_oversized_inline_cells_to_python(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    blob = _publish(
        storage, catalog, tmp_path,
        pa.table({
            "id": pa.array([1], pa.int32()),
            "media": [PNG + b"x" * 10_000],
        }),
    )
    data_uri = _publish(
        storage, catalog, tmp_path,
        pa.table({
            "id": pa.array([1], pa.int32()),
            "media": ["data:image/png;base64," + "A" * 10_000],
        }),
    )
    upper_payload = PNG + b"u" * 7000
    upper_cell = "DATA:image/png;base64," + base64.b64encode(upper_payload).decode()
    assert len(upper_cell.encode()) > 8192
    upper_data_uri = _publish(
        storage, catalog, tmp_path,
        pa.table({
            "id": pa.array([1], pa.int32()),
            "media": [upper_cell],
        }),
    )
    for published in (blob, data_uri, upper_data_uri):
        certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])

    content, content_type = _read(
        storage, upper_data_uri, _request("1"), max_bytes=10_000)
    assert content == upper_payload
    assert content_type == "image/png"

    monkeypatch.setattr(
        media_cells, "_cell_bytes",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized inline cells must be refused inside the exact query"),
    )
    for published in (blob, data_uri):
        with pytest.raises(MediaCellTooLarge):
            _read(storage, published, _request("1"), max_bytes=32)


def test_cell_uri_is_read_only_after_exact_lookup_and_shared_source_policy(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    allowed = tmp_path / "allowed"
    permitted_media = allowed / "permitted.png"
    permitted_media.write_bytes(PNG + b"permitted")
    outside_media = tmp_path / "outside.png"
    outside_media.write_bytes(PNG + b"outside")
    oversized_media = allowed / "oversized.png"
    oversized_media.write_bytes(PNG + b"x" * 1024)
    data_uri = "data:image/png;base64," + base64.b64encode(PNG + b"inline").decode()
    published = _publish(
        storage, catalog, tmp_path,
        pa.table({
            "id": pa.array([1, 2, 3, 4, 5], pa.int32()),
            "media": [
                str(permitted_media),
                str(outside_media),
                "https://private.example.test/secret.png",
                str(oversized_media),
                data_uri,
            ],
        }),
    )
    certify_and_persist_exact_row_identity(storage, _exact(published), ["id"])
    monkeypatch.setenv("DP_AUTH_SECRET", "media-cell-policy-test")
    monkeypatch.setenv("DP_DATASET_ROOTS", str(allowed))

    content, content_type = _read(storage, published, _request("1"))
    assert content == PNG + b"permitted"
    assert content_type == "image/png"
    with pytest.raises(MediaCellSourceDenied):
        _read(storage, published, _request("2"))
    with pytest.raises(MediaCellSourceDenied):
        _read(storage, published, _request("3"))
    with pytest.raises(MediaCellTooLarge):
        _read(storage, published, _request("4"))
    inline, _content_type = _read(storage, published, _request("5"))
    assert inline == PNG + b"inline"


def test_object_cell_uses_existing_filesystem_contract_and_bounded_reads(monkeypatch):
    class Input:
        def __init__(self, payload: bytes, calls: list[int]):
            self.payload = payload
            self.calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int) -> bytes:
            self.calls.append(size)
            return self.payload

    class Filesystem:
        def __init__(self, *, size: int, payload: bytes, is_file: bool = True):
            self.size = size
            self.payload = payload
            self.is_file = is_file
            self.paths: list[str] = []
            self.opens: list[str] = []
            self.reads: list[int] = []

        def get_file_info(self, path: str):
            self.paths.append(path)
            return type("Info", (), {"is_file": self.is_file, "size": self.size})()

        def open_input_file(self, path: str):
            self.opens.append(path)
            return Input(self.payload, self.reads)

    filesystem = Filesystem(size=len(PNG), payload=PNG)
    object_uris: list[str] = []

    def configured_object_fs(uri: str):
        object_uris.append(uri)
        return filesystem, "bucket/private/key.png"

    monkeypatch.setattr(media_cells, "object_fs", configured_object_fs)
    assert media_cells._uri_bytes(None, "S3://bucket/private/key.png", 1024) == PNG
    assert object_uris == ["s3://bucket/private/key.png"]
    assert filesystem.paths == ["bucket/private/key.png"]
    assert filesystem.opens == ["bucket/private/key.png"]
    assert filesystem.reads == [1025]

    non_file = Filesystem(size=0, payload=b"", is_file=False)
    monkeypatch.setattr(
        media_cells, "object_fs", lambda _uri: (non_file, "bucket/prefix"))
    with pytest.raises(MediaCellSourceDenied, match="source is not permitted"):
        media_cells._uri_bytes(None, "s3://bucket/prefix", 32)
    assert non_file.opens == []

    too_large = Filesystem(size=33, payload=PNG)
    monkeypatch.setattr(
        media_cells, "object_fs", lambda _uri: (too_large, "bucket/large.png"))
    with pytest.raises(MediaCellTooLarge):
        media_cells._uri_bytes(None, "s3://bucket/large.png", 32)
    assert too_large.opens == []

    changed_after_stat = Filesystem(size=32, payload=b"x" * 33)
    monkeypatch.setattr(
        media_cells, "object_fs",
        lambda _uri: (changed_after_stat, "bucket/changed.png"))
    with pytest.raises(MediaCellTooLarge):
        media_cells._uri_bytes(None, "s3://bucket/changed.png", 32)
    assert changed_after_stat.reads == [33]

    def failed_object_fs(_uri: str):
        raise RuntimeError("secret endpoint and credentials")

    monkeypatch.setattr(media_cells, "object_fs", failed_object_fs)
    with pytest.raises(MediaCellSourceDenied) as denied:
        media_cells._uri_bytes(None, "s3://bucket/private.png", 32)
    assert str(denied.value) == "media cell source is not permitted"

    monkeypatch.setattr(
        media_cells, "object_fs",
        lambda _uri: pytest.fail("HTTP must not reach the object-store credential path"),
    )
    with pytest.raises(MediaCellSourceDenied):
        media_cells._uri_bytes(None, "https://private.example.test/image.png", 32)


def test_media_cell_http_surface_requires_authentication(monkeypatch):
    from hub.main import app

    monkeypatch.setenv("DP_AUTH_SECRET", "media-cell-auth-test")
    response = TestClient(app).post(
        "/api/catalog/revisions/dataset/revision/media-cell",
        json={
            "identity": [{"name": "id", "arrowType": "int32", "value": "1"}],
            "column": "media",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
