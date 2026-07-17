"""Deterministic coverage for core-owned immutable local file revisions."""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub import graph as graph_mod, metadb
from hub.local_run_inputs import bind_manifest
from hub.main import app
from hub.models import Graph, LineagePublication
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
        catalog=catalog, storage=storage, resolve_adapter=lambda _uri: DuckDBAdapter()))
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


def test_managed_local_single_unregister_api_preserves_revision_retention(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "single.parquet")
    artifact, published = _publish(storage, catalog, logical_uri, 1)
    revision_id = published["revision_id"]
    table_id = published["table"].id

    monkeypatch.setattr(catalog_routes, "get_deps", lambda: SimpleNamespace(
        catalog=catalog, resolve_adapter=lambda _uri: DuckDBAdapter()))
    lock_order: list[str] = []
    lock_registry = metadb._lock_local_result_registry
    session_get = Session.get

    def track_registry(session):
        lock_order.append("registry")
        return lock_registry(session)

    def track_session_get(session, entity, ident, **kwargs):
        if entity is metadb.LocalResultArtifact and kwargs.get("with_for_update"):
            lock_order.append("artifact")
        return session_get(session, entity, ident, **kwargs)

    monkeypatch.setattr(metadb, "_lock_local_result_registry", track_registry)
    monkeypatch.setattr(Session, "get", track_session_get)
    response = TestClient(app).delete(f"/api/catalog/tables/{table_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert lock_order.index("registry") < lock_order.index("artifact")
    assert metadb.catalog_get(table_id) is None
    with metadb.session() as session:
        logical = session.scalar(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.logical_uri == logical_uri))
        assert logical is not None and logical.state == "unregistered"
        assert logical.current_uri is None and logical.governance_doc == "{}"
        assert session.get(metadb.ManagedLocalFileRevision, revision_id) is not None
        assert session.get(metadb.LocalResultReference, {
            "uri": artifact, "owner_kind": "managed_file_revision", "owner_key": revision_id,
        }) is not None


def test_managed_local_prefix_and_batch_unregister_preserve_revision_retention(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    prefix = str(tmp_path / "outputs" / ".dp-results")
    first_artifact, first = _publish(storage, catalog, str(tmp_path / "published" / "first.parquet"), 1)
    second_artifact, second = _publish(storage, catalog, str(tmp_path / "published" / "second.parquet"), 2)

    assert metadb.catalog_delete_prefix(prefix) == 2
    with metadb.session() as session:
        for artifact, published in ((first_artifact, first), (second_artifact, second)):
            assert session.get(metadb.ManagedLocalFileRevision, published["revision_id"]) is not None
            assert session.get(metadb.LocalResultReference, {
                "uri": artifact,
                "owner_kind": "managed_file_revision",
                "owner_key": published["revision_id"],
            }) is not None

    third_artifact, third = _publish(storage, catalog, str(tmp_path / "published" / "third.parquet"), 3)
    fourth_artifact, fourth = _publish(storage, catalog, str(tmp_path / "published" / "fourth.parquet"), 4)
    monkeypatch.setattr(catalog_routes, "get_deps", lambda: SimpleNamespace(
        catalog=catalog, resolve_adapter=lambda _uri: DuckDBAdapter()))
    response = TestClient(app).post("/api/catalog/tables/delete", json={
        "ids": [third["table"].id, fourth["table"].id, "missing"],
    })

    assert response.status_code == 200, response.text
    assert set(response.json()["deleted"]) == {third["table"].id, fourth["table"].id}
    assert response.json()["missing"] == ["missing"]
    with metadb.session() as session:
        for artifact, published in ((third_artifact, third), (fourth_artifact, fourth)):
            assert session.get(metadb.ManagedLocalFileRevision, published["revision_id"]) is not None
            assert session.get(metadb.LocalResultReference, {
                "uri": artifact,
                "owner_kind": "managed_file_revision",
                "owner_key": published["revision_id"],
            }) is not None


def test_managed_local_unregister_fails_closed_when_retention_ownership_changes(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "ownership.parquet")
    artifact, published = _publish(storage, catalog, logical_uri, 1)
    with metadb.session() as session:
        ref = session.get(metadb.LocalResultReference, {
            "uri": artifact,
            "owner_kind": "managed_file_revision",
            "owner_key": published["revision_id"],
        })
        assert ref is not None
        session.delete(ref)

    with pytest.raises(RuntimeError, match="ownership changed concurrently"):
        metadb.catalog_delete_entry(artifact)
    assert metadb.catalog_get(published["table"].id) is not None


def test_committed_publication_rejects_a_different_lineage_replay(local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "managed.parquet")
    source_uri = _register_source(catalog, tmp_path)
    run_id = f"managed-local-lineage-mismatch-{uuid.uuid4().hex}"
    artifact = storage.begin_result("lineage-mismatch", run_id)
    pq.write_table(pa.table({"value": [9]}), artifact)
    storage.commit_result(artifact, run_id)

    published = catalog.publish_managed_local_file_output(
        name="managed_local", logical_uri=logical_uri, artifact_uri=artifact,
        parents=[source_uri], lineage=_lineage("managed-local-original-lineage"))
    with pytest.raises(RuntimeError, match="catalog publication key collision"):
        catalog.publish_managed_local_file_output(
            name="managed_local", logical_uri=logical_uri, artifact_uri=artifact,
            parents=[source_uri], lineage=_lineage("managed-local-different-lineage"))

    assert storage.release_result(artifact, run_id) is True
    assert published["table"].uri == artifact
    with metadb.session() as session:
        lineage_events = list(session.scalars(select(metadb.CatalogPublicationEvent).where(
            metadb.CatalogPublicationEvent.effect_type == "lineage")))
        assert len(lineage_events) == 1
        assert len(list(session.scalars(select(metadb.CatalogLineageFact)))) == 1


def _exact_canvas_doc(canvas_id: str, uri: str, dataset_id: str, revision_id: str) -> dict:
    return {
        "id": canvas_id,
        "name": "Pinned revision",
        "version": 1,
        "nodes": [{
            "id": "source",
            "type": "source",
            "position": {"x": 0, "y": 0},
            "data": {"config": {
                "uri": uri,
                "datasetRef": {
                    "kind": "exact", "datasetId": dataset_id, "revisionId": revision_id,
                },
            }},
        }],
        "edges": [],
    }


def test_revision_gc_waits_for_canvas_and_live_reader_then_converges(local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "retained.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    second_uri, second = _publish(storage, catalog, logical_uri, 2)
    canvas_id = f"canvas-{uuid.uuid4().hex}"
    doc = _exact_canvas_doc(
        canvas_id, second_uri, first["dataset_id"], first["revision_id"])
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id="owner", name="Pinned revision", version=1,
            doc=json.dumps(doc)))
        session.flush()
        metadb.sync_local_result_owner(session, "canvas", canvas_id, doc)

    assert metadb.managed_local_file_revision_gc_batch(0, limit=1) == {
        "retired": 0, "has_more": False,
    }
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id, with_for_update=True)
        assert canvas is not None
        empty = {**doc, "nodes": [], "version": 2}
        canvas.doc, canvas.version = json.dumps(empty), 2
        metadb.sync_local_result_owner(session, "canvas", canvas_id, empty)

    with storage.acquire_result_read(first_uri, "revision-gc-test"):
        assert metadb.managed_local_file_revision_gc_batch(0, limit=1)["retired"] == 0

    assert metadb.managed_local_file_revision_gc_batch(0, limit=1) == {
        "retired": 1, "has_more": False,
    }
    storage.prune_results()
    assert not pathlib.Path(first_uri).exists()
    assert pathlib.Path(second_uri).exists()
    with metadb.session() as session:
        assert session.get(metadb.ManagedLocalFileRevision, first["revision_id"]) is None
        assert session.get(metadb.ManagedLocalFileRevision, second["revision_id"]) is not None


def test_admission_and_durable_profile_own_exact_revision_artifacts(local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "jobs.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    second_uri, _second = _publish(storage, catalog, logical_uri, 2)
    manifest = [{
        "node_id": "source",
        "dataset_id": first["dataset_id"],
        "revision_id": first["revision_id"],
        "provider": "managed-local-file",
        "resolved_at": "2026-07-16T00:00:00+00:00",
    }]
    run_id, created = metadb.admit_local_run_inputs(
        uid="owner", canvas_id=None, submission_id=str(uuid.uuid4()),
        target_node_id=None, intent_sha256="a" * 64, manifest=manifest)
    assert created is True

    canvas_id = f"profile-canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id="owner", name="Profile", version=1,
            doc=json.dumps({"id": canvas_id, "nodes": [], "edges": []})))
    profile = metadb.preallocate_or_adopt_profile_run_owner(
        str(uuid.uuid4()), "owner", canvas_id, canvas_id,
        "source", "out", "b" * 64, input_manifest=manifest)

    with metadb.session() as session:
        admission_ref = session.get(metadb.LocalResultReference, {
            "uri": first_uri, "owner_kind": "run_input_admission", "owner_key": run_id,
        })
        profile_ref = session.get(metadb.LocalResultReference, {
            "uri": first_uri, "owner_kind": "profile_job", "owner_key": profile.run_id,
        })
        assert admission_ref is not None and profile_ref is not None
    assert metadb.managed_local_file_revision_gc_batch(0)["retired"] == 0
    assert pathlib.Path(first_uri).exists() and pathlib.Path(second_uri).exists()


def test_bound_execution_fences_the_selected_artifact_without_trusting_client_private_data(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "bound.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    second_uri, _second = _publish(storage, catalog, logical_uri, 2)
    graph = Graph.model_validate({
        "id": "bound-canvas",
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {
                "uri": second_uri,
                "_input_artifact_uri": "/tmp/client-forged.parquet",
                "datasetRef": {
                    "kind": "exact", "datasetId": first["dataset_id"],
                    "revisionId": first["revision_id"],
                },
            }},
        }],
        "edges": [],
    })
    assert graph_mod.execution_source_uris(graph, "source") == [second_uri]

    manifest = [{
        "node_id": "source", "dataset_id": first["dataset_id"],
        "revision_id": first["revision_id"], "provider": "managed-local-file",
        "resolved_at": "2026-07-16T00:00:00+00:00",
    }]
    bound = bind_manifest(graph, "source", manifest, lambda _uri: DuckDBAdapter())
    assert graph_mod.execution_source_uris(bound, "source") == [first_uri]


def test_exact_canvas_reads_fence_the_selected_artifact_and_not_the_mutable_head(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "pinned-read.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    second_uri, _second = _publish(storage, catalog, logical_uri, 2)
    graph = Graph.model_validate({
        "id": "pinned-read-canvas",
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {
                "uri": second_uri,
                "datasetRef": {
                    "kind": "exact", "datasetId": first["dataset_id"],
                    "revisionId": first["revision_id"],
                },
            }},
        }],
        "edges": [],
    })

    graph_mod.resolve_source_refs(graph, lambda uri: uri)

    assert graph_mod.execution_source_uris(graph, "source") == [first_uri]
    with storage.acquire_result_read(first_uri, "exact-canvas-read"):
        assert metadb.managed_local_file_revision_gc_batch(0)["retired"] == 0


def test_provider_owned_manifests_do_not_create_core_revision_ownership(local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "provider-owned.parquet")
    first_uri, first = _publish(storage, catalog, logical_uri, 1)
    manifest = [{
        "node_id": "source",
        "dataset_id": first["dataset_id"],
        "revision_id": first["revision_id"],
        "provider": "external-provider",
        "resolved_at": "2026-07-16T00:00:00+00:00",
    }]

    with metadb.session() as session:
        metadb.sync_local_result_owner(session, "run_input_admission", "external-run", manifest)
    with metadb.session() as session:
        assert session.get(metadb.LocalResultReference, {
            "uri": first_uri,
            "owner_kind": "run_input_admission",
            "owner_key": "external-run",
        }) is None
