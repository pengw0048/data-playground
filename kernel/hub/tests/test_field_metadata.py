"""Contract coverage for bounded field metadata and typed row references."""

from __future__ import annotations

import base64
import uuid

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hub.deps import get_deps
from hub.main import app
from hub.models import ColumnSchema, FieldAnnotation, TypedRowReference, normalize_column_schemas
from hub.plugins.adapters import (
    DuckDBAdapter,
    adapter_arrow_schema_columns,
    arrow_schema_columns,
)
from hub.plugins.catalog import InMemoryCatalog

client = TestClient(app)


def _write_arrow(path, schema: pa.Schema) -> None:
    table = pa.Table.from_arrays([pa.array([1])], schema=schema)
    with pa.OSFile(str(path), "wb") as sink:
        with ipc.new_file(sink, schema) as writer:
            writer.write_table(table)


def test_arrow_field_metadata_round_trips_through_catalog_and_http(tmp_path):
    path = tmp_path / "annotated.arrow"
    schema = pa.schema([pa.field("id", pa.int64(), metadata={
        b"z.note": b"kept", b"a.binary": b"\xff\x00",
        b"tokenized.algorithm": b"BPE", b"token_count": b"42", b"tokenizer": b"cl100k",
        b"storage-options": b'{"secret":"never-public"}',
        b"storage.options": b"never-public",
        b"opaque-header": b"Authorization: Bearer never-public",
        b"opaque-pem": b"-----BEGIN PRIVATE KEY-----\nnever-public",
        b"opaque-json": b'{"storage_options":{"access_key":"never-public"}}',
        b"opaque-userinfo": b"s3://access:never-public@example.test/data",
        b"opaque-query": b"s3://example.test/data?access_key=never-public",
    })])
    _write_arrow(path, schema)

    expected = [
        {"key": "a.binary", "value": base64.b64encode(b"\xff\x00").decode(),
         "encoding": "base64", "provenance": "provider"},
        {"key": "token_count", "value": "42", "encoding": "utf8", "provenance": "provider"},
        {"key": "tokenized.algorithm", "value": "BPE", "encoding": "utf8", "provenance": "provider"},
        {"key": "tokenizer", "value": "cl100k", "encoding": "utf8", "provenance": "provider"},
        {"key": "z.note", "value": "kept", "encoding": "utf8", "provenance": "provider"},
    ]
    assert DuckDBAdapter().schema(str(path))[0].model_dump(by_alias=True)["annotations"] == expected

    response = client.post("/api/catalog/register", json={
        "uri": str(path), "name": f"annotated-{uuid.uuid4().hex}",
    })
    assert response.status_code == 200, response.text
    assert response.json()["columns"][0]["annotations"] == expected
    assert "never-public" not in response.text
    detail = client.get(f"/api/catalog/tables/{response.json()['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["columns"][0]["annotations"] == expected

    changed_schema = pa.schema([pa.field("id", pa.int64(), metadata={b"z.note": b"changed"})])
    _write_arrow(path, changed_schema)
    changed = client.post("/api/catalog/register", json={
        "uri": str(path), "name": response.json()["name"],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] != response.json()["version"]


@pytest.mark.parametrize("key,value", [
    ("authorization", "Bearer never-public"),
    ("profile", "Authorization: Basic never-public"),
    ("profile", "-----BEGIN RSA PRIVATE KEY-----\nnever-public"),
    ("profile", '{"credentials":{"token":"never-public"}}'),
    ("profile", "https://user:never-public@example.test/data"),
    ("profile", "https://example.test/data?access_token=never-public"),
    ("profile", "https://example.test/data?X-Amz-Credential=never-public"),
])
def test_explicit_unsafe_metadata_is_rejected(key, value):
    with pytest.raises(ValidationError):
        ColumnSchema.model_validate({
            "name": "id", "type": "int", "annotations": [{
                "key": key, "value": value, "encoding": "utf8",
                "provenance": "provider",
            }],
        })


def test_malformed_metadata_is_rejected_but_raw_metadata_is_dropped():
    with pytest.raises(ValidationError):
        ColumnSchema.model_validate({
            "name": "id", "type": "int", "annotations": [{
                "key": "opaque", "value": "not valid base64!", "encoding": "base64",
                "provenance": "provider",
            }],
        })
    raw = pa.schema([pa.field("id", pa.int64(), metadata={
        b"password": b"secret", b"note": b"safe",
    })])
    assert [annotation.key for annotation in arrow_schema_columns(raw)[0].annotations] == ["note"]


def test_provider_reference_hook_preserves_exact_target_and_key_order():
    schema = pa.schema([pa.field("owner", pa.int64(), metadata={b"source": b"producer"})])
    columns = arrow_schema_columns(schema, reference_normalizer=lambda _column: {
        "target": {"kind": "exact", "datasetId": "dataset-7", "revisionId": "r-2"},
        "keyFields": ["tenant", "user"], "semanticType": "owner", "provenance": "provider",
    })
    reference = columns[0].model_dump(by_alias=True)["rowReference"]
    assert reference == {
        "target": {"kind": "exact", "datasetId": "dataset-7", "revisionId": "r-2", "lastKnown": None},
        "keyFields": ["tenant", "user"], "semanticType": "owner", "provenance": "provider",
    }
    assert arrow_schema_columns(schema)[0].row_reference is None


def test_feature_detected_hook_sees_only_sanitized_annotations_and_rejects_mutated_result():
    observed = []
    malformed = TypedRowReference.model_validate({
        "target": {"kind": "canonical", "datasetId": "orders"},
        "keyFields": ["tenant", "id"], "provenance": "provider",
    })
    malformed.key_fields.append("id")

    class Adapter:
        @staticmethod
        def normalize_field_reference(column):
            observed.extend(annotation.key for annotation in column.annotations)
            return malformed

    schema = pa.schema([pa.field("owner", pa.int64(), metadata={
        b"note": b"safe", b"password": b"never-public",
    })])
    with pytest.raises(ValidationError, match="unique"):
        adapter_arrow_schema_columns(Adapter(), schema)
    assert observed == ["note"]

    class OrganizationSpecificAdapter:
        @staticmethod
        def normalize_field_reference(_column):
            return {
                "target": {"kind": "canonical", "datasetId": "orders"},
                "keyFields": ["id"], "provenance": "provider", "organizationKey": "private",
            }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter_arrow_schema_columns(OrganizationSpecificAdapter(), schema)


def test_normalization_deeply_rejects_a_mutated_column_instance():
    annotation = FieldAnnotation(
        key="note", value="safe", encoding="utf8", provenance="provider")
    column = ColumnSchema(name="id", type="int", annotations=[annotation])
    annotation.key = "private-key"

    with pytest.raises(ValidationError, match="credential metadata"):
        normalize_column_schemas([column])


def test_provider_exact_reference_round_trips_through_catalog_without_retargeting():
    uri = f"metadata-provider://{uuid.uuid4().hex}"

    class Provider:
        name = "metadata-test"

        @staticmethod
        def matches(candidate):
            return candidate == uri

        @staticmethod
        def schema(_uri):
            return [ColumnSchema.model_validate({
                "name": "owner", "type": "int", "provenance": "provider",
                "rowReference": {
                    "target": {"kind": "exact", "datasetId": "orders", "revisionId": "v7"},
                    "keyFields": ["tenant", "id"], "provenance": "provider",
                },
            })]

        @staticmethod
        def count(_uri):
            return 1

        @staticmethod
        def fingerprint(_uri):
            return "metadata-test-v1"

    deps = get_deps()
    provider = Provider()
    deps.adapters.insert(0, provider)
    try:
        response = client.post("/api/catalog/register", json={"uri": uri, "name": f"provider-{uuid.uuid4().hex}"})
        assert response.status_code == 200, response.text
        assert response.json()["columns"][0]["rowReference"] == {
            "target": {"kind": "exact", "datasetId": "orders", "revisionId": "v7", "lastKnown": None},
            "keyFields": ["tenant", "id"], "semanticType": None, "provenance": "provider",
        }
    finally:
        deps.adapters.remove(provider)


def test_invalid_provider_dto_is_rejected_without_echoing_sensitive_payload():
    uri = f"metadata-invalid://{uuid.uuid4().hex}"

    class Provider:
        name = "metadata-invalid-test"

        @staticmethod
        def matches(candidate):
            return candidate == uri

        @staticmethod
        def schema(_uri):
            return [{
                "name": "owner", "type": "int", "annotations": [{
                    "key": "profile", "value": "Authorization: Bearer never-public",
                    "encoding": "utf8", "provenance": "provider",
                }],
            }]

        @staticmethod
        def count(_uri):
            return 1

        @staticmethod
        def fingerprint(_uri):
            return "metadata-invalid-v1"

    deps = get_deps()
    provider = Provider()
    deps.adapters.insert(0, provider)
    try:
        response = client.post("/api/catalog/register", json={
            "uri": uri, "name": f"invalid-provider-{uuid.uuid4().hex}",
        })
        assert response.status_code == 400
        assert "adapter returned invalid schema metadata" in response.text
        assert "never-public" not in response.text
    finally:
        deps.adapters.remove(provider)


def test_catalog_probe_keeps_read_failures_evidence_poor_but_retains_valid_schema(tmp_path):
    class OfflineSchema:
        @staticmethod
        def schema(_uri):
            raise ConnectionError("provider offline")

    offline = InMemoryCatalog(str(tmp_path / "offline"), lambda _uri: OfflineSchema())
    unknown = offline._add(
        "offline", "offline://dataset", _persist_table=False, _embed_table=False)
    assert unknown.columns == []
    assert unknown.row_count is None

    class OfflineCount:
        @staticmethod
        def schema(_uri):
            return [{"name": "id", "type": "int"}]

        @staticmethod
        def count(_uri):
            raise ConnectionError("provider offline")

        @staticmethod
        def fingerprint(_uri):
            return "unknown"

    partial = InMemoryCatalog(str(tmp_path / "partial"), lambda _uri: OfflineCount())._add(
        "partial", "partial://dataset", _persist_table=False, _embed_table=False)
    assert [column.name for column in partial.columns] == ["id"]
    assert partial.row_count is None


def test_schema_annotation_bounds_drop_raw_excess_and_reject_explicit_excess():
    raw = pa.schema([pa.field("id", pa.int64(), metadata={
        f"k{index:02}".encode(): b"x" * 1024 for index in range(32)
    })])
    assert len(arrow_schema_columns(raw)[0].annotations) == 16
    with pytest.raises(ValidationError):
        ColumnSchema.model_validate({
            "name": "id", "type": "int", "annotations": [{
                "key": f"k{index}", "value": "x" * 1024, "encoding": "utf8",
                "provenance": "provider",
            } for index in range(17)],
        })

    aggregate = [{
        "name": f"field-{field_index}", "type": "int", "annotations": [{
            "key": f"k{annotation_index}", "value": "x" * 1024, "encoding": "utf8",
            "provenance": "provider",
        } for annotation_index in range(16)],
    } for field_index in range(17)]
    with pytest.raises(ValueError, match="256 KiB"):
        normalize_column_schemas(aggregate)

    raw_aggregate = pa.schema([
        pa.field(f"field-{field_index}", pa.int64(), metadata={
            f"k{annotation_index}".encode(): b"x" * 1024 for annotation_index in range(16)
        })
        for field_index in range(17)
    ])
    bounded = arrow_schema_columns(raw_aggregate)
    assert sum(len(annotation.decoded_value()) for column in bounded
               for annotation in column.annotations) == 256 * 1024
    assert bounded[-1].annotations == []

    with pytest.raises(ValidationError):
        ColumnSchema.model_validate({
            "name": "id", "type": "int", "annotations": [{
                "key": "binary", "value": "/w===", "encoding": "base64",
                "provenance": "provider",
            }],
        })
