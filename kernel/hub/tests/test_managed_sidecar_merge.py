"""Acceptance tests for pure exact managed-sidecar merge admission (#767)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select, update

from hub import managed_sidecar_merge as managed_sidecar_merge_module, metadb
from hub.managed_sidecar_merge import (
    ManagedSidecarMergeError, ManagedSidecarMergeIntentV1, ManagedSidecarMergeRequestV1,
    admit_managed_sidecar_merge, prepare_managed_sidecar_merge,
)
from hub.merge_columns import MergeColumnRuleV1
from hub.models import ExactDatasetRef, LineagePublication
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


def _publish_sidecar(
        storage, catalog, logical_uri: str, name: str, table: pa.Table,
        *, base: ExactDatasetRef, identity_columns: list[str]) -> ExactDatasetRef:
    """Publish a sidecar fixture with the normalized exact #805 projections it requires."""
    sidecar = _publish(storage, catalog, logical_uri, name, table)
    fact_key = f"test-sidecar-lineage:{uuid.uuid4().hex}"
    with metadb.session() as session:
        fact = metadb.CatalogLineageFact(
            fact_key=fact_key, publication_key=f"test:{uuid.uuid4().hex}",
            fingerprint=f"test:{uuid.uuid4().hex}", source_key=base.dataset_id,
            destination_key=sidecar.dataset_id, source_uri="test:base", destination_uri="test:sidecar",
            source_key_hash=hashlib.sha256(base.dataset_id.encode()).hexdigest(),
            destination_key_hash=hashlib.sha256(sidecar.dataset_id.encode()).hexdigest(),
            source_uri_hash=hashlib.sha256(b"test:base").hexdigest(),
            destination_uri_hash=hashlib.sha256(b"test:sidecar").hexdigest(),
            source_version=base.revision_id, destination_version=sidecar.revision_id,
            provenance="manual", field_mappings_json="[]")
        session.add(fact)
        session.flush()
        for field in identity_columns:
            semantic = f"{fact_key}:{field}".encode()
            session.add(metadb.CatalogFieldLineageProjection(
                projection_key="test:" + hashlib.sha256(semantic).hexdigest(), fact_id=fact.id,
                fact_key=fact_key, publication_key=fact.publication_key,
                source_dataset_id=base.dataset_id, source_version=base.revision_id,
                source_field=field, source_field_id=None,
                destination_dataset_id=sidecar.dataset_id, destination_revision_id=sidecar.revision_id,
                destination_field=field,
                destination_dataset_hash=hashlib.sha256(sidecar.dataset_id.encode()).hexdigest(),
                destination_revision_hash=hashlib.sha256(sidecar.revision_id.encode()).hexdigest(),
                destination_field_hash=hashlib.sha256(field.encode()).hexdigest(),
                created_at=datetime.now(timezone.utc)))
    return sidecar


def test_admit_real_parquet_add_replace_reordered_composite(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "account": pa.array([1, 1, 2], type=pa.int64()),
        "day": ["2026-01-02", "2026-01-01", "2026-01-01"],
        "keep": ["a", "b", "c"], "old": [10, 20, 30],
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "account": pa.array([2, 1, 1], type=pa.int64()),
        "day": ["2026-01-01", "2026-01-01", "2026-01-02"],
        "replacement": [300, 200, 100], "addition": [3, 2, 1],
    }), base=base, identity_columns=["account", "day"])
    key = "issue-767-happy"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["account", "day"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace"),
               MergeColumnRuleV1(source="addition", target="new", mode="add")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
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


def test_missing_ambiguous_and_unavailable_identity_evidence_are_explicit(local_catalog, tmp_path, monkeypatch):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({"id": [1], "old": [1]}))
    missing = _publish(storage, catalog, str(tmp_path / "missing.parquet"), "missing", pa.table({"id": [1], "replacement": [2]}))
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=missing, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key="identity-evidence-missing",
        publication=LineagePublication(idempotency_key="identity-evidence-missing", provenance="manual"))
    with pytest.raises(ManagedSidecarMergeError, match="identity_reference_required"):
        prepare_managed_sidecar_merge(storage=storage, request=request)
    ambiguous = _publish_sidecar(storage, catalog, str(tmp_path / "ambiguous.parquet"), "ambiguous", pa.table({"id": [1], "replacement": [2]}), base=base, identity_columns=["id"])
    with metadb.session() as session:
        row = session.scalar(select(metadb.CatalogFieldLineageProjection).where(
            metadb.CatalogFieldLineageProjection.destination_dataset_id == ambiguous.dataset_id))
        assert row is not None
        session.add(metadb.CatalogFieldLineageProjection(
            projection_key="test:" + uuid.uuid4().hex, fact_id=row.fact_id, fact_key=row.fact_key,
            publication_key=row.publication_key, source_dataset_id="other", source_version="o1",
            source_field="id", source_field_id=None, destination_dataset_id=ambiguous.dataset_id,
            destination_revision_id=ambiguous.revision_id, destination_field="id",
            destination_dataset_hash=row.destination_dataset_hash,
            destination_revision_hash=row.destination_revision_hash,
            destination_field_hash=row.destination_field_hash, created_at=datetime.now(timezone.utc)))
    with pytest.raises(ManagedSidecarMergeError, match="identity_reference_required"):
        prepare_managed_sidecar_merge(storage=storage, request=request.model_copy(update={"sidecar": ambiguous}))
    monkeypatch.setattr(metadb, "catalog_field_lineage_page", lambda **_kwargs: ([], None, False, False))
    with pytest.raises(ManagedSidecarMergeError, match="identity_reference_unavailable"):
        prepare_managed_sidecar_merge(storage=storage, request=request)


def test_mismatched_identity_rejects_before_task_attempt_or_child_reservation(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({"id": [1], "old": [1]}))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({"id": [1], "replacement": [2]}), base=base, identity_columns=["id"])
    with metadb.session() as session:
        session.execute(update(metadb.CatalogFieldLineageProjection).where(
            metadb.CatalogFieldLineageProjection.destination_dataset_id == sidecar.dataset_id).values(
            source_field="other_id"))
        before = (
            session.scalar(select(func.count(metadb.DurableTask.id))),
            session.scalar(select(func.count(metadb.DurableTaskAttempt.id))),
            session.scalar(select(func.count(metadb.ManagedLocalFileRevision.revision_id))),
            session.scalar(select(func.count(metadb.ObjectAttempt.uri))),
        )
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key="identity-mismatch-no-state",
        publication=LineagePublication(idempotency_key="identity-mismatch-no-state", provenance="manual"))
    with pytest.raises(ManagedSidecarMergeError, match="row_reference_target_mismatch"):
        admit_managed_sidecar_merge(storage=storage, request=request)
    with metadb.session() as session:
        after = (
            session.scalar(select(func.count(metadb.DurableTask.id))),
            session.scalar(select(func.count(metadb.DurableTaskAttempt.id))),
            session.scalar(select(func.count(metadb.ManagedLocalFileRevision.revision_id))),
            session.scalar(select(func.count(metadb.ObjectAttempt.uri))),
        )
    assert after == before


def test_admission_rejects_moved_head_and_incomplete_identity_coverage(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base_uri = str(tmp_path / "base.parquet")
    base = _publish(storage, catalog, base_uri, "base", pa.table({"id": [1, 2], "old": [1, 2]}))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": [1, 3], "replacement": [10, 30],
    }), base=base, identity_columns=["id"])
    key = "issue-767-blocker"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    )
    with pytest.raises(ManagedSidecarMergeError, match="complete identity coverage"):
        admit_managed_sidecar_merge(storage=storage, request=request)

    complete_sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "complete.parquet"), "complete", pa.table({
        "id": [1, 2], "replacement": [10, 20],
    }), base=base, identity_columns=["id"])
    complete = request.model_copy(update={"sidecar": complete_sidecar})
    _publish(storage, catalog, base_uri, "base", pa.table({"id": [1, 2], "old": [3, 4]}))
    with pytest.raises(ManagedSidecarMergeError, match="base head moved"):
        admit_managed_sidecar_merge(storage=storage, request=complete)


def test_intent_reload_is_complete_canonical_and_tamper_proof(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "id": [1, 2], "old": [1, 2],
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": [1, 2], "replacement": [10, 20],
    }), base=base, identity_columns=["id"])
    key = "issue-767-reload"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    )
    intent = admit_managed_sidecar_merge(storage=storage, request=request)
    assert ManagedSidecarMergeIntentV1.model_validate_json(intent.model_dump_json()) == intent
    assert intent.write_intent.provenance.parents == []

    missing = intent.model_dump()
    missing.pop("merge_sha256")
    with pytest.raises(ValueError):
        ManagedSidecarMergeIntentV1.model_validate(missing)
    tampered = intent.model_dump()
    tampered["coverage"]["status"] = "partial"
    with pytest.raises(ValueError):
        ManagedSidecarMergeIntentV1.model_validate(tampered)
    inconsistent = intent.model_dump()
    inconsistent["write_intent"]["expected_schema"] = []
    with pytest.raises(ValueError):
        ManagedSidecarMergeIntentV1.model_validate(inconsistent)
    self_merge = intent.model_dump()
    self_merge["sidecar"] = self_merge["base"]
    with pytest.raises(ValueError, match="revisions must be distinct"):
        ManagedSidecarMergeIntentV1.model_validate(self_merge)


@pytest.mark.parametrize(("ids", "expected_status", "field", "value"), [
    ([None, 2], "invalid", "null_rows", 1),
    ([1, 1], "invalid", "duplicate_groups", 1),
    ([1], "partial", "missing_identities", 1),
    ([1, 2, 3], "partial", "extra_identities", 1),
])
def test_prepare_preserves_distinguishable_identity_blockers(
        local_catalog, tmp_path, ids, expected_status, field, value):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "id": pa.array([1, 2], type=pa.int64()), "old": [1, 2],
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": pa.array(ids, type=pa.int64()), "replacement": list(range(len(ids))),
    }), base=base, identity_columns=["id"])
    key = f"issue-767-{field}"
    prepared = prepare_managed_sidecar_merge(storage=storage, request=ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    ))
    assert prepared.coverage.status == expected_status
    evidence = prepared.coverage.candidate if field in {"null_rows", "duplicate_groups"} else prepared.coverage
    assert getattr(evidence, field) == value


def test_request_canonicalizes_last_known_and_rejects_arbitrary_parents(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "id": [1], "old": [1],
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": [1], "replacement": [2],
    }), base=base, identity_columns=["id"])
    key = "issue-767-canonical"
    common = dict(identity_columns=["id"], rules=[
        MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key, publication=LineagePublication(idempotency_key=key, provenance="manual"))
    clean = ManagedSidecarMergeRequestV1(base=base, sidecar=sidecar, expected_head=base, **common)
    noisy_base = base.model_copy(update={"last_known": {"committedAt": "2026-01-01T00:00:00Z"}})
    noisy = ManagedSidecarMergeRequestV1(
        base=noisy_base, sidecar=sidecar, expected_head=noisy_base, **common)
    assert admit_managed_sidecar_merge(storage=storage, request=clean) == (
        admit_managed_sidecar_merge(storage=storage, request=noisy))
    with pytest.raises(ValueError, match="revisions must be distinct"):
        ManagedSidecarMergeRequestV1(base=base, sidecar=base, expected_head=base, **common)
    with pytest.raises(ValueError):
        ManagedSidecarMergeRequestV1.model_validate({**clean.model_dump(), "provenance": {}})


def test_prepare_rejects_identity_rule_and_schema_collision(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "id": [1], "old": [1],
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": [1], "replacement": [2],
    }), base=base, identity_columns=["id"])
    key = "issue-767-rules"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="id", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    )
    with pytest.raises(ManagedSidecarMergeError, match="merge rules"):
        prepare_managed_sidecar_merge(storage=storage, request=request)


def test_reload_reuses_shared_schema_rules_and_rejects_write_parents(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "id": [1], "old": [1],
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": [1], "replacement": [2],
    }), base=base, identity_columns=["id"])
    key = "issue-767-reload-rules"
    intent = admit_managed_sidecar_merge(storage=storage, request=ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    ))
    prepared = prepare_managed_sidecar_merge(storage=storage, request=ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    ))
    assert not hasattr(prepared, "head")
    assert prepared.destination.dataset_id == base.dataset_id
    assert isinstance(prepared.base_schema, list) and isinstance(prepared.sidecar_schema, list)

    tampered = intent.model_dump()
    tampered["rules"] = [{"source": "replacement", "target": "old", "mode": "add"}]
    draft = intent.model_copy(deep=True)
    draft.rules = [MergeColumnRuleV1(source="replacement", target="old", mode="add")]
    tampered["merge_sha256"] = hashlib.sha256(
        managed_sidecar_merge_module._canonical(
            managed_sidecar_merge_module._semantic_payload(draft)).encode()).hexdigest()
    with pytest.raises(ValueError, match="rules"):
        ManagedSidecarMergeIntentV1.model_validate(tampered)

    parents = intent.model_dump()
    parents["write_intent"]["provenance"]["parents"] = ["file:///not-an-allowed-parent"]
    draft = intent.model_copy(deep=True)
    draft.write_intent.provenance.parents = ["file:///not-an-allowed-parent"]
    parents["merge_sha256"] = hashlib.sha256(
        managed_sidecar_merge_module._canonical(
            managed_sidecar_merge_module._semantic_payload(draft)).encode()).hexdigest()
    with pytest.raises(ValueError, match="write intent"):
        ManagedSidecarMergeIntentV1.model_validate(parents)


def test_preflight_and_reload_reject_integer_width_drift(local_catalog, tmp_path):
    storage, catalog = local_catalog
    base = _publish(storage, catalog, str(tmp_path / "base.parquet"), "base", pa.table({
        "id": pa.array([1], type=pa.int64()), "old": pa.array([1], type=pa.int32()),
    }))
    sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "sidecar.parquet"), "sidecar", pa.table({
        "id": pa.array([1], type=pa.int64()), "replacement": pa.array([2], type=pa.int64()),
    }), base=base, identity_columns=["id"])
    key = "issue-767-width"
    request = ManagedSidecarMergeRequestV1(
        base=base, sidecar=sidecar, expected_head=base, identity_columns=["id"],
        rules=[MergeColumnRuleV1(source="replacement", target="old", mode="replace")],
        idempotency_key=key,
        publication=LineagePublication(idempotency_key=key, provenance="manual"),
    )
    with pytest.raises(ManagedSidecarMergeError, match="replace type"):
        prepare_managed_sidecar_merge(storage=storage, request=request)

    compatible_sidecar = _publish_sidecar(storage, catalog, str(tmp_path / "compatible.parquet"), "compatible", pa.table({
        "id": pa.array([1], type=pa.int64()), "replacement": pa.array([2], type=pa.int32()),
    }), base=base, identity_columns=["id"])
    intent = admit_managed_sidecar_merge(
        storage=storage, request=request.model_copy(update={"sidecar": compatible_sidecar}))
    tampered = intent.model_dump()
    tampered["base_schema"][0]["physical_type"] = "int32"
    draft = intent.model_copy(deep=True)
    draft.base_schema[0].physical_type = "int32"
    tampered["merge_sha256"] = hashlib.sha256(
        managed_sidecar_merge_module._canonical(
            managed_sidecar_merge_module._semantic_payload(draft)).encode()).hexdigest()
    with pytest.raises(ValueError, match="rules"):
        ManagedSidecarMergeIntentV1.model_validate(tampered)
