"""Deterministic coverage for core-owned immutable local file revisions."""

from __future__ import annotations

import os
import pathlib
import uuid
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select

from hub import metadb
from hub.models import LineagePublication
from hub.plugins.adapters import DuckDBAdapter, ManagedLocalFileRevisionAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import catalog as catalog_routes
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'managed-local-revisions.db'}")
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


def _publish(storage, catalog, logical_uri: str, value: int) -> tuple[str, dict]:
    run_id = f"managed-local-{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
    pq.write_table(pa.table({"value": [value]}), artifact)
    storage.commit_result(artifact, run_id)
    try:
        published = catalog.publish_managed_local_file_output(
            name="managed_local", logical_uri=logical_uri, artifact_uri=artifact)
    except Exception:
        storage.abort_result(artifact, run_id)
        raise
    assert storage.release_result(artifact, run_id) is True
    return artifact, published


def _lineage(key: str) -> LineagePublication:
    return LineagePublication(idempotency_key=key, provenance="manual")


def _register_source(catalog, tmp_path) -> str:
    source_uri = str(tmp_path / "source.parquet")
    pq.write_table(pa.table({"source": [1]}), source_uri)
    catalog._add(name="source", uri=source_uri, strict_probe=True)
    return source_uri


def test_local_managed_revision_history_and_exact_open_survive_head_replacement(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "managed.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    selected_before_replacement = ManagedLocalFileRevisionAdapter().open_revision(
        first_uri, first["revision_id"])
    second_uri, second = _publish(storage, catalog, logical_uri, 2)

    assert first_uri != second_uri
    assert first["dataset_id"] == second["dataset_id"]
    adapter = ManagedLocalFileRevisionAdapter()
    history, cursor = adapter.revision_history(second_uri, limit=1)
    assert history[0]["revision_id"] == second["revision_id"]
    assert cursor == second["revision_id"]
    older, next_cursor = adapter.revision_history(second_uri, limit=1, cursor=cursor)
    assert next_cursor is None
    assert older[0]["revision_id"] == first["revision_id"]
    assert selected_before_replacement.fetchall() == [(1,)]
    selected = adapter.open_revision(second_uri, first["revision_id"])
    assert selected.fetchall() == [(1,)]
    assert adapter.open_revision(second_uri, second["revision_id"]).fetchall() == [(2,)]

    monkeypatch.setattr(catalog_routes, "get_deps", lambda: SimpleNamespace(
        catalog=catalog, resolve_adapter=lambda _uri: DuckDBAdapter()))
    table_id = catalog.get_table(second_uri).id
    page = catalog_routes.list_dataset_revisions(table_id, limit=1, cursor=None)
    assert page.items[0].dataset_id == second["dataset_id"]
    assert page.items[0].revision_id == second["revision_id"]
    assert page.items[0].retention_owner == "core"
    exact = catalog_routes.open_dataset_revision(second["dataset_id"], first["revision_id"])
    assert exact.revision_id == first["revision_id"]
    assert exact.retention_owner == "core"
    assert exact.parent_revision_id is None
    assert exact.summary.row_count == 1 and exact.summary.data_file_count == 1
    assert exact.preview.rows == [{"value": 1}]

    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "managed_file_revision",
        )))
    assert {ref.uri for ref in refs} == {first_uri, second_uri}


def test_failed_local_publication_never_advances_the_catalog_head(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "managed.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    source_uri = _register_source(catalog, tmp_path)
    run_id = f"managed-local-failure-{uuid.uuid4().hex}"
    unpublished = storage.begin_result("failed-publication", run_id)
    pq.write_table(pa.table({"value": [2]}), unpublished)
    storage.commit_result(unpublished, run_id)
    def fail_lineage(*_args, **_kwargs):
        raise RuntimeError("lineage failed")

    monkeypatch.setattr(metadb, "_catalog_apply_lineage_in_session", fail_lineage)
    with pytest.raises(RuntimeError, match="lineage failed"):
        catalog.publish_managed_local_file_output(
            name="managed_local", logical_uri=logical_uri, artifact_uri=unpublished,
            parents=[source_uri], lineage=_lineage("managed-local-lineage-failure"))

    with metadb.session() as session:
        logical = session.scalar(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.logical_uri == logical_uri))
        assert logical is not None and logical.current_uri == first_uri
        assert session.scalar(select(metadb.ManagedLocalFileRevision).where(
            metadb.ManagedLocalFileRevision.artifact_uri == unpublished)) is None
        assert session.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == unpublished)) is None
        assert session.scalar(select(metadb.CatalogLineageFact)) is None
    storage.abort_result(unpublished, run_id)
    assert not pathlib.Path(unpublished).exists()

    adapter = ManagedLocalFileRevisionAdapter()
    resolved = adapter.resolve_revision(first_uri)
    assert resolved["revision_id"] == first["revision_id"]
    assert adapter.open_revision(first_uri, first["revision_id"]).fetchall() == [(1,)]
    assert not pathlib.Path(unpublished).exists()


def test_committed_publication_response_loss_recovers_exact_receipt(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "managed.parquet")
    source_uri = _register_source(catalog, tmp_path)
    lineage = _lineage("managed-local-response-loss")
    run_id = f"managed-local-response-loss-{uuid.uuid4().hex}"
    artifact = storage.begin_result("response-loss", run_id)
    pq.write_table(pa.table({"value": [7]}), artifact)
    storage.commit_result(artifact, run_id)

    publish = metadb.catalog_publish_managed_local_file

    def commit_then_lose_response(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("publication response lost")

    monkeypatch.setattr(metadb, "catalog_publish_managed_local_file", commit_then_lose_response)
    published = catalog.publish_managed_local_file_output(
        name="managed_local", logical_uri=logical_uri, artifact_uri=artifact,
        parents=[source_uri], lineage=lineage)
    monkeypatch.setattr(metadb, "catalog_publish_managed_local_file", publish)
    replayed = catalog.publish_managed_local_file_output(
        name="managed_local", logical_uri=logical_uri, artifact_uri=artifact,
        parents=[source_uri], lineage=lineage)
    assert storage.release_result(artifact, run_id) is True
    assert published["table"].uri == artifact
    assert replayed["revision_id"] == published["revision_id"]
    assert ManagedLocalFileRevisionAdapter().open_revision(
        artifact, published["revision_id"]).fetchall() == [(7,)]
    with metadb.session() as session:
        assert session.scalar(select(metadb.CatalogLineageFact)) is not None
        assert len(list(session.scalars(select(metadb.CatalogLineageFact)))) == 1
