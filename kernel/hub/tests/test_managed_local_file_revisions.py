"""Deterministic coverage for core-owned immutable local file revisions."""

from __future__ import annotations

import json
import os
import pathlib
import threading
import uuid
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub import graph as graph_mod, metadb
from hub.execution_manifest import build_execution_manifest
from hub.local_run_inputs import bind_manifest
from hub.main import app
from hub.local_writes import write_managed_local_file
from hub.models import (
    ColumnSchema,
    ExactDatasetRef,
    Graph,
    LineagePublication,
    WriteDestination,
    WriteIntent,
    WriteProvenance,
)
from hub.nodespecs import BUILTIN_NODE_SPECS
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


def _lineage(key: str, *, run_id: str | None = None) -> LineagePublication:
    if run_id is None:
        return LineagePublication(idempotency_key=key, provenance="manual")
    return LineagePublication(
        idempotency_key=key,
        run_id=run_id,
        producer="managed-local-test-canvas",
        producer_version=1,
        step_id="write",
        provenance="run",
    )


def _write_intent(
        logical_uri: str, key: str, *, head: dict | None = None,
        name: str = "managed_local",
        expected_schema: list[ColumnSchema] | None = None,
        run_id: str | None = None,
        parents: list[str] | None = None) -> WriteIntent:
    replacing = head is not None
    return WriteIntent(
        destination=WriteDestination(
            logical_uri=logical_uri,
            name=name,
            dataset_id=(head["dataset_id"] if replacing else None),
        ),
        mode=("replace" if replacing else "create"),
        expected_schema=(expected_schema if expected_schema is not None
                         else [ColumnSchema(name="value", type="int")]),
        expected_head=(ExactDatasetRef(
            kind="exact",
            dataset_id=head["dataset_id"],
            revision_id=head["revision_id"],
        ) if replacing else None),
        idempotency_key=key,
        provenance=WriteProvenance(
            publication=_lineage(key, run_id=run_id),
            parents=parents or [],
        ),
    )


def _typed_write(storage, catalog, intent: WriteIntent, value: int):
    def writer(uri: str) -> None:
        pq.write_table(pa.table({"value": [value]}), uri)

    return write_managed_local_file(
        storage=storage,
        catalog=catalog,
        intent=intent,
        write_artifact=writer,
    )


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


def test_typed_local_create_replace_receipts_reopen_exact_revisions_after_restart(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "typed.parquet")
    created = _typed_write(
        storage, catalog, _write_intent(logical_uri, "typed-create"), 1)
    frozen_head = metadb.catalog_managed_local_write_head(logical_uri)
    assert frozen_head is not None
    replaced = _typed_write(
        storage, catalog,
        _write_intent(logical_uri, "typed-replace", head=frozen_head),
        2,
    )

    assert created.dataset_id == replaced.dataset_id
    assert replaced.parent_head is not None
    assert replaced.parent_head.revision_id == created.revision_id
    assert replaced.head.revision_id == replaced.revision_id
    assert replaced.rows == 1 and replaced.bytes > 0
    assert [(column.name, column.type) for column in replaced.schema] == [("value", "int")]
    assert replaced.publication.publish_sequence == 2
    assert replaced.provenance.publication.idempotency_key == "typed-replace"

    restarted = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    recovered = metadb.catalog_managed_local_write_receipt(
        _write_intent(logical_uri, "typed-replace", head=frozen_head).model_dump(
            by_alias=True, mode="json"))
    assert recovered is not None and recovered["revisionId"] == replaced.revision_id
    adapter = ManagedLocalFileRevisionAdapter()
    assert adapter.open_revision(
        replaced.publication.artifact_uri, created.revision_id).fetchall() == [(1,)]
    assert adapter.open_revision(
        replaced.publication.artifact_uri, replaced.revision_id).fetchall() == [(2,)]
    assert restarted.get_table(replaced.publication.artifact_uri).id


def test_typed_local_publication_persists_admitted_manifest_on_receipt_and_lineage(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    canvas_id = "managed-local-test-canvas"
    submission_id = str(uuid.uuid4())
    run_id = metadb.local_run_submission_id("local", canvas_id, submission_id)
    logical_uri = str(tmp_path / "published" / "manifest.parquet")
    source_uri = _register_source(catalog, tmp_path)
    intent = _write_intent(
        logical_uri,
        "manifest-create",
        run_id=run_id,
        parents=[source_uri],
    )
    digest, manifest_doc = build_execution_manifest(
        Graph(id=canvas_id, version=1, nodes=[], edges=[]),
        target_node_id=None,
        target_port_id=None,
        input_manifest=[],
        write_intent=intent,
        deps=SimpleNamespace(
            node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS},
            plugins=[],
        ),
    )
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id="local", name="Manifest publication"))
    admitted_run_id, created = metadb.admit_local_run_inputs(
        uid="local",
        canvas_id=canvas_id,
        submission_id=submission_id,
        target_node_id=None,
        intent_sha256="a" * 64,
        manifest=[],
        execution_manifest_sha256=digest,
        execution_manifest_doc=manifest_doc,
    )
    assert created is True and admitted_run_id == run_id

    receipt = _typed_write(storage, catalog, intent, 1)
    recovered = metadb.catalog_managed_local_write_receipt(
        intent.model_dump(by_alias=True, mode="json"))
    facts, _cursor, _has_more = metadb.catalog_lineage_facts_page(limit=10, after_id=0)
    with metadb.session() as session:
        revision = session.scalar(select(metadb.ManagedLocalFileRevision).where(
            metadb.ManagedLocalFileRevision.write_idempotency_key == "manifest-create"))

    assert receipt.execution_manifest_sha256 == digest
    assert recovered is not None and recovered["executionManifestSha256"] == digest
    assert revision is not None
    assert revision.run_id == run_id
    assert revision.execution_manifest_sha256 == digest
    run_facts = [fact for fact in facts if fact["run_id"] == run_id]
    assert len(run_facts) == 1
    assert run_facts[0]["execution_manifest_sha256"] == digest


def test_typed_local_write_preconditions_fail_before_artifact_allocation(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "preconditions.parquet")
    created = _typed_write(
        storage, catalog, _write_intent(logical_uri, "precondition-create"), 1)
    old_head = metadb.catalog_managed_local_write_head(logical_uri)
    assert old_head is not None and old_head["revision_id"] == created.revision_id
    replaced = _typed_write(
        storage, catalog,
        _write_intent(logical_uri, "precondition-replace", head=old_head),
        2,
    )

    allocations = 0
    begin = storage.begin_result

    def track_begin(*args, **kwargs):
        nonlocal allocations
        allocations += 1
        return begin(*args, **kwargs)

    monkeypatch.setattr(storage, "begin_result", track_begin)
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="already exists"):
        _typed_write(
            storage, catalog, _write_intent(logical_uri, "duplicate-create"), 3)
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="stale"):
        _typed_write(
            storage, catalog,
            _write_intent(logical_uri, "stale-replace", head=old_head),
            3,
        )
    # A reused idempotency key bound to a different intent is a typed conflict (409), never a raw 500.
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="idempotency key collision"):
        _typed_write(
            storage, catalog,
            _write_intent(logical_uri, "precondition-create", head=old_head),
            3,
        )
    missing = str(tmp_path / "published" / "missing.parquet")
    forged_head = {
        "dataset_id": replaced.dataset_id,
        "revision_id": replaced.revision_id,
    }
    with pytest.raises(metadb.ManagedLocalWriteConflict, match="does not exist"):
        _typed_write(
            storage, catalog,
            _write_intent(missing, "missing-replace", head=forged_head),
            3,
        )
    assert allocations == 0


def test_typed_local_write_response_loss_replays_one_durable_receipt(
        local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "response-loss-typed.parquet")
    intent = _write_intent(logical_uri, "typed-response-loss")
    publish = metadb.catalog_publish_managed_local_file

    def commit_then_lose_response(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("write response lost")

    monkeypatch.setattr(
        metadb, "catalog_publish_managed_local_file", commit_then_lose_response)
    first = _typed_write(storage, catalog, intent, 7)
    monkeypatch.setattr(metadb, "catalog_publish_managed_local_file", publish)
    replayed = _typed_write(storage, catalog, intent, 999)

    assert replayed == first
    assert ManagedLocalFileRevisionAdapter().open_revision(
        first.publication.artifact_uri, first.revision_id).fetchall() == [(7,)]
    with metadb.session() as session:
        revisions = list(session.scalars(select(metadb.ManagedLocalFileRevision).where(
            metadb.ManagedLocalFileRevision.write_idempotency_key == "typed-response-loss")))
        assert len(revisions) == 1


def test_typed_local_write_precommit_failure_aborts_candidate_and_preserves_head(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "precommit.parquet")
    created = _typed_write(
        storage, catalog, _write_intent(logical_uri, "precommit-create"), 1)
    head = metadb.catalog_managed_local_write_head(logical_uri)
    assert head is not None
    candidate = None

    def fail_after_write(uri: str) -> None:
        nonlocal candidate
        candidate = uri
        pq.write_table(pa.table({"value": [2]}), uri)
        raise RuntimeError("writer failed before commit")

    with pytest.raises(RuntimeError, match="writer failed before commit"):
        write_managed_local_file(
            storage=storage,
            catalog=catalog,
            intent=_write_intent(logical_uri, "precommit-replace", head=head),
            write_artifact=fail_after_write,
        )
    assert candidate is not None and not pathlib.Path(candidate).exists()
    current = metadb.catalog_managed_local_write_head(logical_uri)
    assert current is not None and current["revision_id"] == created.revision_id

    schema_candidate = None

    def write_wrong_schema(uri: str) -> None:
        nonlocal schema_candidate
        schema_candidate = uri
        pq.write_table(pa.table({"value": [3]}), uri)

    with pytest.raises(ValueError, match="schema does not match"):
        write_managed_local_file(
            storage=storage,
            catalog=catalog,
            intent=_write_intent(
                logical_uri,
                "precommit-schema-mismatch",
                head=head,
                expected_schema=[ColumnSchema(name="other", type="int")],
            ),
            write_artifact=write_wrong_schema,
        )
    assert schema_candidate is not None and not pathlib.Path(schema_candidate).exists()
    current = metadb.catalog_managed_local_write_head(logical_uri)
    assert current is not None and current["revision_id"] == created.revision_id


def test_typed_local_concurrent_replacements_have_one_cas_winner(
        local_catalog, tmp_path):
    storage, catalog = local_catalog
    logical_uri = str(tmp_path / "published" / "concurrent.parquet")
    _typed_write(storage, catalog, _write_intent(logical_uri, "concurrent-create"), 0)
    head = metadb.catalog_managed_local_write_head(logical_uri)
    assert head is not None
    barrier = threading.Barrier(2)
    receipts = []
    errors = []
    candidates: list[str] = []

    def replace(key: str, value: int) -> None:
        def writer(uri: str) -> None:
            candidates.append(uri)
            pq.write_table(pa.table({"value": [value]}), uri)
            barrier.wait(timeout=10)

        try:
            receipts.append(write_managed_local_file(
                storage=storage,
                catalog=catalog,
                intent=_write_intent(logical_uri, key, head=head),
                write_artifact=writer,
            ))
        except Exception as exc:  # noqa: BLE001 - the assertion checks the exact typed conflict
            errors.append(exc)

    threads = [
        threading.Thread(target=replace, args=("concurrent-a", 1)),
        threading.Thread(target=replace, args=("concurrent-b", 2)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert len(receipts) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], metadb.ManagedLocalWriteConflict)
    assert sum(pathlib.Path(uri).exists() for uri in candidates) == 1
    with metadb.session() as session:
        assert len(list(session.scalars(select(metadb.ManagedLocalFileRevision).where(
            metadb.ManagedLocalFileRevision.logical_id == receipts[0].dataset_id)))) == 2


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
    table = catalog.get_table(table_id)
    response = TestClient(app).delete(f"/api/catalog/tables/{table_id}", params={
        "expected_registration_id": table.registration_id,
        "expected_revision": table.metadata_revision,
    })

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
    third_table = catalog.get_table(third["table"].id)
    fourth_table = catalog.get_table(fourth["table"].id)
    response = TestClient(app).post("/api/catalog/tables/delete", json={
        "targets": [
            {"id": third_table.id, "expectedRegistrationId": third_table.registration_id,
             "expectedRevision": third_table.metadata_revision},
            {"id": fourth_table.id, "expectedRegistrationId": fourth_table.registration_id,
             "expectedRevision": fourth_table.metadata_revision},
            {"id": "missing", "expectedRegistrationId": "missing-registration",
             "expectedRevision": "m1_missing"},
        ],
    })

    assert response.status_code == 200, response.text
    assert {(item["id"], item["status"]) for item in response.json()["results"]} == {
        (third_table.id, "unregistered"),
        (fourth_table.id, "unregistered"),
        ("missing", "missing"),
    }
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
