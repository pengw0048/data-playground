"""Certified local Lance media reads stay exact, bounded, and predicate-addressed."""

from __future__ import annotations

import base64
import os
import uuid

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from hub import media_cells, metadb
from hub.main import app
from hub.media_cells import (
    MediaCellIdentityUnavailable,
    MediaCellOffline,
    MediaCellRowAmbiguous,
    MediaCellRowNotFound,
    MediaCellSourceDenied,
    MediaCellTooLarge,
    MediaCellUnavailable,
    MediaCellUnsupported,
    read_exact_media_cell,
)
from hub.models import ExactDatasetRef, MediaCellRequest
from hub.plugins.adapters import (
    DuckDBAdapter,
    LanceAdapter,
    RevisionPermissionLost,
    RevisionProviderOffline,
    RevisionUnavailable,
)
from hub.plugins.catalog import InMemoryCatalog
from hub.row_identity import certify_and_persist_managed_local_lance_row_identity


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBM = b"\x1aE\xdf\xa3webm"
client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    old_url, old_engine, old_session = settings.database_url, metadb._engine, metadb._Session
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (
        os.environ.get("DP_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'lance-media-cells.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = old_url, old_engine, old_session


def _registered_lance(
        tmp_path, table: pa.Table, *, keys: tuple[str, ...] = ("id",),
        write_kwargs: dict | None = None):
    lance = pytest.importorskip("lance")
    uri = str(tmp_path / f"media-{uuid.uuid4().hex}.lance")
    lance.write_dataset(table, uri, **(write_kwargs or {}))
    catalog = InMemoryCatalog(str(tmp_path / "catalog"), lambda value: (
        LanceAdapter() if str(value).lower().rstrip("/").endswith(".lance")
        else DuckDBAdapter()))
    registered = catalog._add(name="lance-media", uri=uri, strict_probe=True)
    binding = metadb.catalog_revision_binding_for_uri(uri)
    assert binding is not None
    revision_id = LanceAdapter().resolve_revision(uri)["revision_id"]
    exact = ExactDatasetRef(
        kind="exact", dataset_id=binding["dataset_id"], revision_id=revision_id)
    certify_and_persist_managed_local_lance_row_identity(exact, list(keys))
    return lance, registered, exact


def _blob_table(payloads: list[bytes], *, inline: int = 32, dedicated: int = 128):
    lance = pytest.importorskip("lance")
    schema = pa.schema([
        pa.field("id", pa.int64()),
        lance.blob_field(
            "media",
            inline_size_threshold=inline,
            dedicated_size_threshold=dedicated,
        ),
    ])
    return pa.Table.from_arrays([
        pa.array(range(1, len(payloads) + 1), pa.int64()),
        lance.blob_array(payloads),
    ], schema=schema)


def _request(
        value: str, *, identity: list[dict] | None = None,
        column: str = "media") -> MediaCellRequest:
    return MediaCellRequest.model_validate({
        "identity": identity or [
            {"name": "id", "arrowType": "int64", "value": value},
        ],
        "column": column,
    })


def _read(
        adapter, registered, exact, value: str, *, max_bytes: int = 1024,
        identity: list[dict] | None = None):
    return read_exact_media_cell(
        storage=None,
        adapter=adapter,
        dataset_uri=registered.uri,
        dataset_id=exact.dataset_id,
        revision_id=exact.revision_id,
        request=_request(value, identity=identity),
        max_bytes=max_bytes,
    )


@pytest.mark.parametrize("arrow_type", [pa.binary(), pa.large_binary()])
def test_plain_binary_columns_fail_closed_without_a_payload_scan(
        tmp_path, monkeypatch, arrow_type):
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([PNG], arrow_type),
        }),
    )
    adapter = LanceAdapter()
    original_dataset = adapter._dataset

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, *_args, **_kwargs):
            raise AssertionError("plain binary capability checks must not scan payloads")

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    assert adapter.supports_media_cell(registered.uri, exact.revision_id) is True
    with pytest.raises(MediaCellUnsupported):
        _read(adapter, registered, exact, "1")


def test_certified_string_cells_reuse_core_data_and_local_source_policy(tmp_path):
    local = tmp_path / "allowed.png"
    local.write_bytes(PNG + b"local")
    data_uri = "data:image/png;base64," + base64.b64encode(PNG + b"inline").decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1, 2], pa.int64()),
            "media": pa.array([data_uri, str(local)], pa.string()),
        }),
    )

    assert _read(LanceAdapter(), registered, exact, "1")[0] == PNG + b"inline"
    assert _read(LanceAdapter(), registered, exact, "2")[0] == PNG + b"local"


def test_certified_lance_object_reference_reuses_core_bounded_filesystem_policy(
        tmp_path, monkeypatch):
    class Input:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(size: int):
            assert size == 1025
            return PNG + b"object"

    class Filesystem:
        @staticmethod
        def get_file_info(path: str):
            assert path == "bucket/private.png"
            return type("Info", (), {"is_file": True, "size": len(PNG + b"object")})()

        @staticmethod
        def open_input_file(path: str):
            assert path == "bucket/private.png"
            return Input()

    monkeypatch.setattr(
        media_cells, "object_fs",
        lambda uri: (Filesystem(), uri.removeprefix("s3://")),
    )
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array(["s3://bucket/private.png"], pa.string()),
        }),
    )

    assert _read(LanceAdapter(), registered, exact, "1")[0] == PNG + b"object"


def test_lance_detail_tags_supported_private_references(
        tmp_path, monkeypatch):
    local = tmp_path / "asset.png"
    local.write_bytes(PNG + b"local")
    object_payload = PNG + b"object"

    class Input:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(size: int):
            assert size == 16 * 1024 * 1024 + 1
            return object_payload

    class Filesystem:
        @staticmethod
        def get_file_info(path: str):
            assert path == "bucket/asset.png"
            return type("Info", (), {"is_file": True, "size": len(object_payload)})()

        @staticmethod
        def open_input_file(path: str):
            assert path == "bucket/asset.png"
            return Input()

    monkeypatch.setattr(
        media_cells, "object_fs",
        lambda uri: (Filesystem(), uri.removeprefix("s3://")),
    )
    values = {
        "local_media": str(local),
        "file_media": local.as_uri(),
        "object_media": "s3://bucket/asset.png",
        "non_media": str(tmp_path / "document.pdf"),
        "unsupported_media": "ftp://example.test/asset.png",
    }
    _lance, _registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            **{name: pa.array([value], pa.string()) for name, value in values.items()},
        }),
    )

    detail_response = client.get(
        f"/api/catalog/revisions/{exact.dataset_id}/{exact.revision_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    columns = {column["name"]: column for column in detail["preview"]["columns"]}
    for name in ("local_media", "file_media", "object_media"):
        assert columns[name]["capabilities"] == ["media"]
        assert columns[name]["mediaKind"] == "image"
    for name in ("non_media", "unsupported_media"):
        assert columns[name]["capabilities"] == []
        assert columns[name]["mediaKind"] is None
    assert detail["preview"]["rows"][0] == {"id": 1, **values}
    assert "rowIdentities" not in detail["preview"]
    assert "mediaCellSupported" not in detail


def test_string_scanners_use_exact_bounded_plans_without_take_or_encoding(
        tmp_path, monkeypatch):
    data = [
        "data:image/png;base64," + base64.b64encode(PNG).decode(),
        "data:image/png;base64," + base64.b64encode(PNG + b"selected").decode(),
    ]
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1, 2], pa.int64()),
            "media": pa.array(data, pa.string()),
            "unrelated": pa.array(["secret-a", "secret-b"], pa.string()),
        }),
    )

    adapter = LanceAdapter()
    original_dataset = adapter._dataset
    scans: list[dict] = []
    plans: list[str] = []

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, *args, **kwargs):
            assert not args
            scanner = self.dataset.scanner(**kwargs)
            scans.append(dict(kwargs))
            plans.append(scanner.explain_plan())
            return scanner

        def take(self, *_args, **_kwargs):
            raise AssertionError("media reads must not use positional take")

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    assert _read(adapter, registered, exact, "2")[0] == PNG + b"selected"
    assert len(scans) == 2
    assert scans[0]["limit"] == scans[1]["limit"] == 2
    assert set(scans[0]["columns"]) == {
        "__dp_media_cell_length", "__dp_media_cell_data_uri"}
    assert scans[1]["columns"] == ["media"]
    assert scans[0]["filter"] == scans[1]["filter"]
    assert all(scan["late_materialization"] is False for scan in scans)
    assert all("unrelated" not in str(scan["columns"]) for scan in scans)
    assert all("Take:" not in plan for plan in plans)
    assert all("encode" not in plan.lower() and "base64" not in plan.lower() for plan in plans)


def test_oversized_string_is_rejected_before_the_value_projection(
        tmp_path, monkeypatch):
    data_uri = (
        "data:image/png;base64,"
        + base64.b64encode(PNG + b"x" * 2048).decode()
    )
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([data_uri], pa.string()),
        }),
    )
    adapter = LanceAdapter()
    original_dataset = adapter._dataset
    scans: list[dict] = []

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, **kwargs):
            scans.append(dict(kwargs))
            return self.dataset.scanner(**kwargs)

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    with pytest.raises(MediaCellTooLarge):
        _read(adapter, registered, exact, "1", max_bytes=128)
    assert len(scans) == 1
    assert set(scans[0]["columns"]) == {
        "__dp_media_cell_length", "__dp_media_cell_data_uri"}
    assert scans[0]["limit"] == 2


@pytest.mark.parametrize(("payload", "expected_kind"), [
    (PNG + b"i" * 8, 0),
    (PNG + b"p" * 64, 1),
])
def test_blob_v2_inline_and_packed_payloads_use_descriptor_then_blobfile(
        tmp_path, payload, expected_kind):
    _lance, registered, exact = _registered_lance(
        tmp_path, _blob_table([payload]),
        write_kwargs={"data_storage_version": "2.2"},
    )
    dataset = LanceAdapter()._dataset(registered.uri, version=int(exact.revision_id))
    descriptor = dataset.scanner(
        columns=["media"], blob_handling="blobs_descriptions",
        late_materialization=False,
    ).to_table().column(0)[0].as_py()

    assert descriptor["kind"] == expected_kind
    assert LanceAdapter().revision_schema(
        registered.uri, exact.revision_id)[1].type == "bytes"
    detail = LanceAdapter().revision_detail(
        registered.uri, exact.revision_id, preview_limit=100)
    assert detail["columns"][1].type == "bytes"
    assert detail["columns"][1].capabilities == ["media"]
    assert detail["columns"][1].media_kind == "image"
    assert detail["preview_table"].to_pylist()[0]["media"] == f"<{len(payload)} bytes>"
    assert _read(LanceAdapter(), registered, exact, "1") == (payload, "image/png")


def test_mixed_blob_and_plain_binary_revision_detail(
        tmp_path):
    inline = "data:image/png;base64," + base64.b64encode(PNG + b"string").decode()
    table = (
        _blob_table([PNG + b"route"])
        .append_column("raw", pa.array([b"not public media"], pa.binary()))
        .append_column("string_media", pa.array([inline], pa.string()))
    )
    _lance, registered, exact = _registered_lance(
        tmp_path, table, write_kwargs={"data_storage_version": "2.2"})

    detail_response = client.get(
        f"/api/catalog/revisions/{exact.dataset_id}/{exact.revision_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    columns = {column["name"]: column for column in detail["preview"]["columns"]}
    assert columns["media"]["type"] == "bytes"
    assert columns["media"]["capabilities"] == ["media"]
    assert columns["media"]["mediaKind"] == "image"
    assert columns["raw"]["type"] == "bytes"
    assert "media" not in columns["raw"]["capabilities"]
    assert columns["raw"]["mediaKind"] is None
    assert columns["string_media"]["capabilities"] == ["media"]
    assert columns["string_media"]["mediaKind"] == "image"
    assert detail["preview"]["rows"] == [{
        "id": 1,
        "media": f"<{len(PNG + b'route')} bytes>",
        "raw": None,
        "string_media": inline,
    }]
    assert "rowIdentities" not in detail["preview"]
    assert "mediaCellSupported" not in detail


def test_blob_preview_sniff_has_strict_cell_and_total_byte_budgets(
        tmp_path, monkeypatch):
    payloads = [PNG + b"x" * 5000 for _ in range(20)]
    table = _blob_table(payloads).append_column(
        "raw", pa.array([b"hidden"] * len(payloads), pa.binary()))
    _lance, registered, exact = _registered_lance(
        tmp_path, table, write_kwargs={"data_storage_version": "2.2"})
    adapter = LanceAdapter()
    original_dataset = adapter._dataset
    scanner_options: list[dict] = []
    take_options: list[dict] = []
    read_sizes: list[int] = []

    class ObservedBlob:
        def __init__(self, blob):
            self.blob = blob

        def size(self):
            return self.blob.size()

        def read(self, size):
            read_sizes.append(size)
            return self.blob.read(size)

        def close(self):
            self.blob.close()

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, **kwargs):
            scanner_options.append(dict(kwargs))
            return self.dataset.scanner(**kwargs)

        def take_blobs(self, **kwargs):
            take_options.append(dict(kwargs))
            return [ObservedBlob(blob) for blob in self.dataset.take_blobs(**kwargs)]

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    detail = adapter.revision_detail(
        registered.uri, exact.revision_id, preview_limit=100)
    assert scanner_options == [{
        "columns": ["id", "media"],
        "limit": 101,
        "late_materialization": False,
        "blob_handling": "blobs_descriptions",
        "with_row_id": True,
    }]
    assert 0 < len(take_options) <= 32
    assert all(set(options) == {"blob_column", "ids"} for options in take_options)
    assert all(size <= 4096 for size in read_sizes)
    assert sum(read_sizes) <= 64 * 1024
    assert all(
        row["media"] == f"<{len(payloads[0])} bytes>" and row["raw"] is None
        for row in detail["preview_table"].to_pylist()
    )


def test_blob_v2_read_uses_stable_row_id_and_one_bounded_blobfile_read(
        tmp_path, monkeypatch):
    payload = WEBM + b"x" * 80
    _lance, registered, exact = _registered_lance(
        tmp_path, _blob_table([PNG, payload]),
        write_kwargs={"data_storage_version": "2.2"},
    )
    adapter = LanceAdapter()
    original_dataset = adapter._dataset
    scans: list[dict] = []
    plans: list[str] = []
    takes: list[dict] = []
    reads: list[int] = []

    class ObservedBlob:
        def __init__(self, blob):
            self.blob = blob

        def size(self):
            return self.blob.size()

        def read(self, size):
            reads.append(size)
            return self.blob.read(size)

        def close(self):
            self.blob.close()

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, *args, **kwargs):
            assert not args
            scanner = self.dataset.scanner(**kwargs)
            scans.append(dict(kwargs))
            plans.append(scanner.explain_plan())
            return scanner

        def take(self, *_args, **_kwargs):
            raise AssertionError("Blob V2 reads must not use positional dataset.take")

        def take_blobs(self, **kwargs):
            takes.append(dict(kwargs))
            return [ObservedBlob(blob) for blob in self.dataset.take_blobs(**kwargs)]

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    assert _read(adapter, registered, exact, "2", max_bytes=128) == (
        payload, "video/webm")
    assert len(scans) == 2
    assert all(scan["columns"] == ["media"] for scan in scans)
    assert all(scan["blob_handling"] == "blobs_descriptions" for scan in scans)
    assert all(scan["late_materialization"] is False for scan in scans)
    assert "with_row_id" not in scans[0]
    assert scans[1]["with_row_id"] is True
    assert takes == [{"blob_column": "media", "ids": [1]}]
    assert reads == [129]
    assert all("Take:" not in plan for plan in plans)
    assert all("encode" not in plan.lower() and "base64" not in plan.lower() for plan in plans)


def test_oversized_blob_v2_is_rejected_from_descriptor_without_blob_fetch(
        tmp_path, monkeypatch):
    _lance, registered, exact = _registered_lance(
        tmp_path, _blob_table([PNG + b"x" * 512]),
        write_kwargs={"data_storage_version": "2.2"},
    )
    adapter = LanceAdapter()
    original_dataset = adapter._dataset
    scans: list[dict] = []

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, **kwargs):
            scans.append(dict(kwargs))
            return self.dataset.scanner(**kwargs)

        def take_blobs(self, **_kwargs):
            raise AssertionError("oversized Blob V2 values must not be fetched")

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    with pytest.raises(MediaCellTooLarge):
        _read(adapter, registered, exact, "1", max_bytes=128)
    assert len(scans) == 1
    assert scans[0]["blob_handling"] == "blobs_descriptions"


def test_blob_v2_missing_and_ambiguous_results_are_typed(
        tmp_path, monkeypatch):
    _lance, registered, exact = _registered_lance(
        tmp_path, _blob_table([PNG]),
        write_kwargs={"data_storage_version": "2.2"},
    )
    with pytest.raises(MediaCellRowNotFound):
        _read(LanceAdapter(), registered, exact, "9")

    adapter = LanceAdapter()
    original_dataset = adapter._dataset

    class DuplicateScanner:
        def __init__(self, scanner):
            self.scanner = scanner

        def to_table(self):
            table = self.scanner.to_table()
            return pa.concat_tables([table, table])

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, **kwargs):
            return DuplicateScanner(self.dataset.scanner(**kwargs))

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )
    with pytest.raises(MediaCellRowAmbiguous):
        _read(adapter, registered, exact, "1")


def test_uint_identity_predicate_is_typed_and_string_values_are_hex_encoded(
        tmp_path, monkeypatch):
    string_key = "x' OR true --\\\n🙂\x00"
    payload = "data:image/png;base64," + base64.b64encode(PNG).decode()
    table = pa.table({
        "u8": pa.array([255], pa.uint8()),
        "u16": pa.array([65535], pa.uint16()),
        "u32": pa.array([4294967295], pa.uint32()),
        "u64": pa.array([18446744073709551615], pa.uint64()),
        "label": pa.array([string_key], pa.string()),
        "media": pa.array([payload], pa.string()),
    })
    keys = ("u8", "u16", "u32", "u64", "label")
    _lance, registered, exact = _registered_lance(tmp_path, table, keys=keys)
    identity = [
        {"name": "u8", "arrowType": "uint8", "value": "255"},
        {"name": "u16", "arrowType": "uint16", "value": "65535"},
        {"name": "u32", "arrowType": "uint32", "value": "4294967295"},
        {"name": "u64", "arrowType": "uint64", "value": "18446744073709551615"},
        {"name": "label", "arrowType": "string", "value": string_key},
    ]
    adapter = LanceAdapter()
    original_dataset = adapter._dataset
    predicates: list[str] = []
    plans: list[str] = []

    class ObservedDataset:
        def __init__(self, dataset):
            self.dataset = dataset

        def scanner(self, **kwargs):
            predicates.append(kwargs["filter"])
            scanner = self.dataset.scanner(**kwargs)
            plans.append(scanner.explain_plan())
            return scanner

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        adapter, "_dataset",
        lambda uri, **kwargs: ObservedDataset(original_dataset(uri, **kwargs)),
    )

    assert _read(
        adapter, registered, exact, "", identity=identity)[0] == PNG
    assert len(set(predicates)) == 1
    predicate = predicates[0]
    assert string_key not in predicate
    assert string_key.encode().hex() in predicate
    assert "TINYINT UNSIGNED" in predicate
    assert "SMALLINT UNSIGNED" in predicate
    assert "INT UNSIGNED" in predicate
    assert "BIGINT UNSIGNED" in predicate
    joined_plans = "\n".join(plans)
    assert "UInt8(255)" in joined_plans
    assert "UInt16(65535)" in joined_plans
    assert "UInt32(4294967295)" in joined_plans
    assert "UInt64(18446744073709551615)" in joined_plans
    assert "Take:" not in joined_plans


def test_stored_data_uri_kind_must_match_the_sniffed_payload(tmp_path):
    mismatched = "data:image/png;base64," + base64.b64encode(WEBM).decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([mismatched], pa.string()),
        }),
    )

    with pytest.raises(MediaCellUnsupported):
        _read(LanceAdapter(), registered, exact, "1")


def test_stored_private_reference_kind_must_match_the_sniffed_payload(tmp_path):
    mismatched = tmp_path / "claimed-image.png"
    mismatched.write_bytes(WEBM)
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([str(mismatched)], pa.string()),
        }),
    )

    with pytest.raises(MediaCellUnsupported):
        _read(LanceAdapter(), registered, exact, "1")


def test_lance_read_requires_the_current_registration_identity_and_certificate(tmp_path):
    value = "data:image/png;base64," + base64.b64encode(PNG).decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([value], pa.string()),
        }),
    )
    with pytest.raises(MediaCellRowNotFound):
        _read(LanceAdapter(), registered, exact, "9")
    with pytest.raises(MediaCellUnavailable):
        read_exact_media_cell(
            storage=None,
            adapter=LanceAdapter(),
            dataset_uri=registered.uri,
            dataset_id="wrong-registration",
            revision_id=exact.revision_id,
            request=_request("1"),
            max_bytes=1024,
        )

    metadb.catalog_delete_entry(registered.uri)
    with pytest.raises(MediaCellIdentityUnavailable):
        LanceAdapter().media_cell_identity_descriptor(registered.uri, exact.revision_id)


def test_lance_media_binding_requires_the_direct_current_catalog_uri(tmp_path):
    value = "data:image/png;base64," + base64.b64encode(PNG).decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([value], pa.string()),
        }),
    )
    alias = f"file://{registered.uri}"

    binding = metadb.managed_local_lance_row_identity_binding_for_uri(registered.uri)
    assert binding is not None and binding["dataset_id"] == exact.dataset_id
    assert metadb.managed_local_lance_row_identity_binding_for_uri(alias) is None
    assert LanceAdapter().supports_media_cell(alias, exact.revision_id) is False
    with pytest.raises(MediaCellIdentityUnavailable):
        LanceAdapter().media_cell_identity_descriptor(alias, exact.revision_id)


def test_retained_old_exact_revision_survives_append_but_new_head_needs_certification(tmp_path):
    old = "data:image/png;base64," + base64.b64encode(PNG + b"old").decode()
    new = "data:image/png;base64," + base64.b64encode(PNG + b"new").decode()
    lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([old], pa.string()),
        }),
    )
    lance.write_dataset(pa.table({
        "id": pa.array([2], pa.int64()),
        "media": pa.array([new], pa.string()),
    }), registered.uri, mode="append")
    new_revision = LanceAdapter().resolve_revision(registered.uri)["revision_id"]

    assert _read(LanceAdapter(), registered, exact, "1")[0] == PNG + b"old"
    assert LanceAdapter().supports_media_cell(registered.uri, new_revision) is True
    with pytest.raises(MediaCellIdentityUnavailable):
        LanceAdapter().media_cell_identity_descriptor(registered.uri, new_revision)


def test_lance_read_revalidates_the_exact_fence_after_source_policy(
        tmp_path, monkeypatch):
    value = "data:image/png;base64," + base64.b64encode(PNG).decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([value], pa.string()),
        }),
    )
    adapter = LanceAdapter()
    original = adapter.exact_revision_incarnation
    calls = 0

    def changed_final_fence(uri, revision_id):
        nonlocal calls
        schema, physical = original(uri, revision_id)
        calls += 1
        return schema, physical if calls < 3 else "0" * 64

    monkeypatch.setattr(adapter, "exact_revision_incarnation", changed_final_fence)
    with pytest.raises(MediaCellUnavailable):
        _read(adapter, registered, exact, "1")
    assert calls == 3


def test_junk_lance_payload_retains_the_typed_unsupported_result(tmp_path):
    junk = "data:application/octet-stream;base64," + base64.b64encode(b"not media").decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([junk], pa.string()),
        }),
    )
    with pytest.raises(MediaCellUnsupported):
        _read(LanceAdapter(), registered, exact, "1")


@pytest.mark.parametrize(("failure", "error"), [
    (RevisionPermissionLost("denied"), MediaCellSourceDenied),
    (RevisionProviderOffline("offline"), MediaCellOffline),
    (RevisionUnavailable("gone"), MediaCellUnavailable),
])
def test_lance_fence_failures_keep_the_media_access_taxonomy(
        tmp_path, monkeypatch, failure, error):
    value = "data:image/png;base64," + base64.b64encode(PNG).decode()
    _lance, registered, exact = _registered_lance(
        tmp_path,
        pa.table({
            "id": pa.array([1], pa.int64()),
            "media": pa.array([value], pa.string()),
        }),
    )
    adapter = LanceAdapter()

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(adapter, "exact_revision_incarnation", fail)
    with pytest.raises(error):
        _read(adapter, registered, exact, "1")
