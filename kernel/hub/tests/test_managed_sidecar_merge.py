"""Acceptance tests for pure exact managed-sidecar merge admission (#767)."""
from __future__ import annotations

import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hub import metadb
from hub.managed_sidecar_merge import (
    ManagedSidecarMergeError, ManagedSidecarMergeRequestV1, admit_managed_sidecar_merge,
)
from hub.merge_columns import MergeColumnRuleV1
from hub.models import ExactDatasetRef, LineagePublication, WriteProvenance
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def isolated_metadata(tmp_path):
    from hub.settings import settings

    engine, factory, url = metadb._engine, metadb._Session, settings.database_url
    if engine is not None:
        engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'sidecar-merge.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url, metadb._engine, metadb._Session = url, engine, factory


@pytest.fixture
def local_catalog(tmp_path):
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        yield storage, catalog
    finally:
        storage.close()


def _publish(storage, catalog, logical_uri: str, name: str, table: pa.Table) -> ExactDatasetRef:
    run_id = f"issue-767:{uuid.uuid4().hex}"
    artifact = storage.begin_result(logical_uri, run_id)
    pq.write_table(table, artifact)
    storage.commit_result(artifact, run_id)
    published = catalog.publish_managed_local_file_output(
        name=name, logical_uri=logical_uri, artifact_uri=artifact)
    assert storage.release_result(artifact, run_id)
    return ExactDatasetRef(kind="exact", dataset_id=published["dataset_id"],
                           revision_id=published["revision_id"])


def test_admit_real_parquet_add_replace_reordered_composite(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "account": pa.array([1, 1, 2], type=pa.int64()),
        "day": ["2026-01-02", "2026-01-01", "2026-01-01"],
        "keep": ["a", "b", "c"], "old": [10, 20, 30],
    }))
    sidecar = _publish(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "account": pa.array([2, 1, 1], type=pa.int64()),
        "day": ["2026-01-01", "2026-01-01", "2026-01-02"],
        "replacement": [300, 200, 100], "addition": [3, 2, 1],
    }))
    key = "issue-767-happy"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["account", "day"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace"),
               MergeColumnRuleV1(source="addition", target="new", mode="add")],
        idempotency_key=key, provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]),
    )

    intent = admit_managed_sidecar_merge(storage=storage, request=request)

    assert intent.base == base and intent.sidecar == sidecar
    assert intent.expected_head == base and intent.write_intent.expected_head == base
    assert [column.name for column in intent.output_schema] == [
        "account", "day", "keep", "old", "new"]
    assert intent.coverage["status"] == "complete"
    assert intent.coverage["candidate"]["rows"] == 3
    assert intent.merge_sha256
    document = intent.model_dump_json(by_alias=True)
    assert str(tmp_path / "outputs") not in document


def test_admission_rejects_moved_head_and_incomplete_identity_coverage(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base_uri = str(tmp_path / "base.parquet")
    base = _publish(storage, catalog, base_uri, "base", pa.table({"id": [1, 2], "old": [1, 2]}))
    sidecar = _publish(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": [1, 3], "replacement": [10, 30],
    }))
    key = "issue-767-blocker"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key, provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual"), parents=[]),
    )
    with pytest.raises(ManagedSidecarMergeError, match="complete identity coverage"):
        admit_managed_sidecar_merge(storage=storage, request=request)

    complete_sidecar = _publish(storage, catalog, str(tmp_path / "complete.parquet"), "complete", pa.table({
        "id": [1, 2], "replacement": [10, 20],
    }))
    complete = request.model_copy(update={"sidecar": complete_sidecar})
    _publish(storage, catalog, base_uri, "base", pa.table({"id": [1, 2], "old": [3, 4]}))
    with pytest.raises(ManagedSidecarMergeError, match="base head moved"):
        admit_managed_sidecar_merge(storage=storage, request=complete)
