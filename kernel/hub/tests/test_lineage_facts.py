from __future__ import annotations

import concurrent.futures
import datetime
import json
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select

from hub import metadb


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


@pytest.fixture
def catalog_scope():
    prefix = f"mem://lineage-facts/{uuid.uuid4().hex}/"
    try:
        yield prefix
    finally:
        metadb.catalog_delete_prefix(prefix)
        # Production reservations intentionally outlive unregister, but each test owns an isolated
        # namespace and must release its tombstones so a persistent PostgreSQL test DB is rerunnable.
        with metadb.session() as s:
            s.execute(delete(metadb.CatalogPublicationEvent).where(
                metadb.CatalogPublicationEvent.effect_type == "lineage",
                metadb.CatalogPublicationEvent.uri.like(f"{prefix}%"),
            ))


def _register(
        uri: str, version: str | None, name: str | None = None,
        table_id: str | None = None) -> None:
    name = name or uri.rstrip("/").rsplit("/", 1)[-1]
    metadb.catalog_upsert_entry(uri, name, {
        "id": table_id or f"tbl_{uuid.uuid4().hex}",
        "name": name,
        "uri": uri,
        "version": version,
    })


def _lineage(
        key: str, *, producer_version: int = 1,
        mappings: list[dict] | None = None) -> dict:
    canonical_mappings = sorted(
        mappings or [],
        key=lambda mapping: tuple(str(mapping.get(field) or "") for field in (
            "source_dataset_id", "source_version", "source_field",
            "source_field_id", "destination_field",
        )),
    )
    return {
        "idempotency_key": key,
        "run_id": f"run-{key}",
        "attempt_id": None,
        "producer": "canvas-lineage-facts",
        "producer_version": producer_version,
        "step_id": "write-output",
        "provenance": "run",
        "field_mappings": canonical_mappings,
    }


def _max_fact_id() -> int:
    with metadb.session() as s:
        return int(s.scalar(select(func.max(metadb.CatalogLineageFact.id))) or 0)


def _registration_id(uri: str) -> str:
    with metadb.session() as s:
        registration_id = s.scalar(select(metadb.CatalogEntry.registration_id).where(
            metadb.CatalogEntry.uri == uri))
    assert registration_id is not None
    return str(registration_id)


def _mapping(
        source_uri: str, source_version: str, source_field: str,
        destination_field: str, source_field_id: str | None = None) -> dict:
    return {
        "source_dataset_id": _registration_id(source_uri),
        "source_version": source_version,
        "source_field": source_field,
        "source_field_id": source_field_id,
        "destination_field": destination_field,
    }


def _provider_lineage_admission(*, run_id: str, mount_id: str | None = None,
                                revision_id: str = "provider-revision-v1",
                                duplicate: str | None = None,
                                admitted_dataset_id: str | None = None) -> tuple[str, str, str]:
    """Persist one canonical provider binding and exact execution-manifest admission."""
    from types import SimpleNamespace

    from hub.execution_manifest import build_execution_manifest
    from hub.models import Graph
    from hub.nodespecs import BUILTIN_NODE_SPECS
    from hub import workspace_providers

    mount_id = mount_id or f"provider-mount-{uuid.uuid4().hex}"
    source_binding_id = uuid.uuid4().hex
    parent = workspace_providers.provider_dataset_uri(mount_id, source_binding_id)
    dataset_id = f"workspace-provider:{parent.removeprefix('workspace-provider://')}"
    admitted_dataset_id = admitted_dataset_id or dataset_id
    graph = Graph.model_validate({
        "id": f"provider-lineage-{uuid.uuid4().hex}", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": parent}}},
            *([{"id": "source-duplicate", "type": "source",
                "position": {"x": 0, "y": 1},
                "data": {"config": {"uri": parent}}}] if duplicate else []),
            {"id": "write", "type": "write", "position": {"x": 1, "y": 0},
             "data": {"config": {"name": "derived"}}},
        ],
        "edges": [
            {"id": "source-write", "source": "source", "target": "write"},
            *([{"id": "source-duplicate-write", "source": "source-duplicate",
                "target": "write"}] if duplicate else []),
        ],
    })
    inputs = [{
        "node_id": "source", "dataset_id": admitted_dataset_id,
        "revision_id": revision_id, "provider": "fixture-exact",
        "resolved_at": "2026-07-23T00:00:00+00:00",
    }]
    if duplicate:
        inputs.append({
            **inputs[0], "node_id": "source-duplicate",
            "revision_id": (
                "provider-revision-conflict" if duplicate == "conflict" else revision_id),
            "provider": (
                "other-fixture-exact" if duplicate == "provider-conflict"
                else inputs[0]["provider"]),
        })
    digest, document = build_execution_manifest(
        graph, target_node_id="write", target_port_id=None, input_manifest=inputs,
        write_intent=None,
        deps=SimpleNamespace(
            node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS}, plugins=[]),
    )
    provider_dataset_id = f"physical-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.WorkspaceProviderDataset(
            mount_id=mount_id, provider_dataset_id=provider_dataset_id,
            provider="fixture", source_binding_id=source_binding_id,
            uri="fixture://physical/secret-placement", columns_doc="[]", state="current",
        ))
        session.flush()
        # Two display placements intentionally share this canonical source binding. Neither belongs
        # to the retained manifest or to a lineage fact.
        for index in range(2):
            session.add(metadb.WorkspaceProviderBinding(
                id=uuid.uuid4().hex, mount_id=mount_id, provider="fixture",
                container_id=metadb.LOCAL_WORKSPACE_ROOT_ID,
                provider_placement_id=f"placement-{index}-{uuid.uuid4().hex}",
                kind="dataset", name=f"Placement {index}",
                provider_dataset_id=provider_dataset_id,
            ))
        metadb._persist_execution_manifest(session, digest, document)
        session.add(metadb.RunState(
            run_id=run_id, canvas_id=None, status="done", doc="{}",
            execution_manifest_sha256=digest,
        ))
    return parent, dataset_id, digest


def _provider_lineage(
        key: str, *, run_id: str, dataset_id: str, revision_id: str,
        mapping: bool = True) -> dict:
    return _lineage(key, mappings=[{
        "source_dataset_id": dataset_id,
        "source_version": revision_id,
        "source_field": "raw_id",
        "source_field_id": None,
        "destination_field": "id",
    }] if mapping else []) | {"run_id": run_id}


def _provider_binding_for_parent(session, parent: str):
    from hub import workspace_providers

    mount_id, source_binding_id = workspace_providers._decode_source_identity_token(
        parent.removeprefix("workspace-provider://"))
    return session.scalar(select(metadb.WorkspaceProviderDataset).where(
        metadb.WorkspaceProviderDataset.mount_id == mount_id,
        metadb.WorkspaceProviderDataset.source_binding_id == source_binding_id,
    ))


def test_field_mapping_contract_rejects_missing_duplicate_and_contradictory_ownership():
    from hub.models import LineagePublication

    base = {
        "source_dataset_id": "source-dataset",
        "source_version": "source-v1",
        "source_field": "raw_id",
        "source_field_id": "field-id",
        "destination_field": "id",
    }
    with pytest.raises(ValueError, match="exact source dataset identity and version"):
        LineagePublication(
            idempotency_key="missing-source-identity",
            provenance="manual",
            field_mappings=[{
                "source_field": "raw_id",
                "destination_field": "id",
            }],
        )
    with pytest.raises(ValueError, match="duplicate"):
        LineagePublication(
            idempotency_key="duplicate-field-mapping",
            provenance="manual",
            field_mappings=[base, base],
        )
    with pytest.raises(ValueError, match="contradictory source ownership"):
        LineagePublication(
            idempotency_key="contradictory-field-mapping",
            provenance="manual",
            field_mappings=[
                base,
                {**base, "source_version": "source-v2", "source_field_id": "other-id"},
            ],
        )


def test_provider_lineage_uses_admitted_canonical_identity_and_exact_revision(catalog_scope):
    run_id = f"provider-lineage-run-{uuid.uuid4().hex}"
    # A legal token with a 126-character mount produces a stable dataset identity beyond 128.
    parent, dataset_id, manifest_sha256 = _provider_lineage_admission(
        run_id=run_id, mount_id="mount-" + "m" * 120,
        revision_id="provider-revision-exact",
    )
    assert len(dataset_id) > 128
    destination = f"{catalog_scope}provider-destination"
    _register(destination, "destination-v1")
    lineage = _provider_lineage(
        "provider-canonical-success", run_id=run_id, dataset_id=dataset_id,
        revision_id="provider-revision-exact")

    # Publication derives only from the retained manifest, so temporary provider availability does
    # not prevent facts for an already admitted exact revision.
    with metadb.session() as session:
        binding = _provider_binding_for_parent(session, parent)
        assert binding is not None
        binding.state = "offline"
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [parent], lineage) == 1
    # A completed publication remains replayable even after the binding later becomes unavailable.
    with metadb.session() as session:
        binding = _provider_binding_for_parent(session, parent)
        assert binding is not None
        binding.state = "detached"
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [parent], lineage) == 0

    with metadb.session() as session:
        fact = session.scalar(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id == run_id))
        assert fact is not None
        assert (fact.source_key, fact.source_uri, fact.source_version) == (
            dataset_id, parent, "provider-revision-exact")
        assert fact.execution_manifest_sha256 == manifest_sha256
        assert "physical-" not in json.dumps({
            "sourceKey": fact.source_key, "sourceUri": fact.source_uri,
            "sourceVersion": fact.source_version,
        }, sort_keys=True)
    destination_dataset_id = _registration_id(destination)
    projections, _cursor, _truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id, "destination-v1", ["id"])
    assert available is True
    assert [(item["source_dataset_id"], item["source_version"])
            for item in projections] == [(dataset_id, "provider-revision-exact")]


def test_provider_lineage_allows_repeated_nodes_with_one_exact_admitted_identity(catalog_scope):
    run_id = f"provider-lineage-repeat-{uuid.uuid4().hex}"
    parent, dataset_id, _manifest_sha256 = _provider_lineage_admission(
        run_id=run_id, duplicate="same")
    destination = f"{catalog_scope}provider-repeat-destination"
    _register(destination, "destination-v1")

    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [parent], _provider_lineage(
            "provider-repeat", run_id=run_id, dataset_id=dataset_id,
            revision_id="provider-revision-v1", mapping=False)) == 1


@pytest.mark.parametrize(
    "failure",
    ["missing", "mismatched", "revision-conflict", "provider-conflict", "detached",
     "provider_error", "corrupt", "malformed"],
)
def test_provider_lineage_rejects_untrusted_admission_before_output_or_fact(
        catalog_scope, failure):
    run_id = f"provider-lineage-reject-{failure}-{uuid.uuid4().hex}"
    parent, dataset_id, _manifest_sha256 = _provider_lineage_admission(
        run_id=run_id,
        duplicate=("conflict" if failure == "revision-conflict"
                   else "provider-conflict" if failure == "provider-conflict" else None),
        admitted_dataset_id=("workspace-provider:unrelated" if failure == "mismatched" else None),
    )
    if failure == "missing":
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            assert state is not None
            state.execution_manifest_sha256 = None
    elif failure in {"detached", "provider_error"}:
        with metadb.session() as session:
            binding = _provider_binding_for_parent(session, parent)
            assert binding is not None
            binding.state = failure
    elif failure == "corrupt":
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            assert state is not None and state.execution_manifest_sha256 is not None
            manifest = session.get(metadb.ExecutionManifest, state.execution_manifest_sha256)
            assert manifest is not None
            manifest.semantic_doc = "{}"
    elif failure == "malformed":
        parent = "workspace-provider://not-a-canonical-binding"

    destination = f"{catalog_scope}provider-rejected-{failure}"
    lineage = _provider_lineage(
        f"provider-rejected-{failure}", run_id=run_id, dataset_id=dataset_id,
        revision_id="provider-revision-v1")
    with pytest.raises(RuntimeError, match="provider source"):
        metadb.catalog_upsert_entry(
            destination, "destination", {
                "id": f"tbl_provider_rejected_{failure}", "name": "destination",
                "uri": destination, "version": "destination-v1",
            }, parents=[parent], lineage=lineage)

    assert metadb.catalog_get(destination) is None
    with metadb.session() as session:
        assert not session.scalar(select(metadb.CatalogPublicationEvent.event_key).where(
            metadb.CatalogPublicationEvent.uri == destination))
        assert not session.scalar(select(metadb.CatalogLineageFact.id).where(
            metadb.CatalogLineageFact.destination_uri == destination))


def test_fact_identity_versions_mappings_and_pair_history(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v7")
    _register(destination, "destination-v3")
    after_id = _max_fact_id()
    lineage = _lineage(
        "fact-repeat",
        mappings=[
            _mapping(source, "source-v7", "raw_b", "feature_b"),
            _mapping(source, "source-v7", "raw_a", "feature_a"),
        ],
    )

    assert metadb.catalog_record_lineage(destination, "destination-v3", [source], lineage) == 1
    assert metadb.catalog_record_lineage(destination, "destination-v3", [source], lineage) == 0

    rows, cursor, has_more = metadb.catalog_lineage_facts_page(
        limit=10, after_id=after_id)
    assert cursor is None and has_more is False
    assert len(rows) == 1
    fact = rows[0]
    assert set(fact) == {
        "id", "fact_key", "publication_key", "source_key", "source_uri", "source_version",
        "destination_key", "destination_uri", "destination_version",
        "run_id", "execution_manifest_sha256", "attempt_id", "producer",
        "producer_version", "step_id",
        "provenance", "field_mappings", "created_at",
    }
    assert isinstance(fact["id"], int) and fact["id"] > after_id
    assert fact["fact_key"].startswith("lineage-fact:v1:sha256:")
    assert fact["publication_key"].startswith("lineage-publication:v1:sha256:")
    assert {key: fact[key] for key in (
        "source_key", "source_uri", "source_version", "destination_key",
        "destination_uri", "destination_version", "run_id", "attempt_id",
        "producer", "producer_version", "step_id", "provenance", "field_mappings",
    )} == {
        "source_key": source,
        "source_uri": source,
        "source_version": "source-v7",
        "destination_key": destination,
        "destination_uri": destination,
        "destination_version": "destination-v3",
        "run_id": "run-fact-repeat",
        "attempt_id": None,
        "producer": "canvas-lineage-facts",
        "producer_version": 1,
        "step_id": "write-output",
        "provenance": "run",
        "field_mappings": [
            _mapping(source, "source-v7", "raw_a", "feature_a"),
            _mapping(source, "source-v7", "raw_b", "feature_b"),
        ],
    }
    assert fact["created_at"].tzinfo == datetime.timezone.utc

    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_record_lineage(
            destination, "destination-v3", [source],
            _lineage(
                "fact-repeat",
                producer_version=2,
                mappings=[_mapping(source, "source-v7", "raw_a", "feature_a")],
            ))

    assert metadb.catalog_record_lineage(
        destination, "destination-v3", [source], _lineage("fact-distinct")) == 1
    assert [row for row in metadb.catalog_lineage_pairs()
            if row["child"] == destination] == [{
        "parent": source, "child": destination, "fact_count": 2,
    }]


def test_multisource_facts_and_exact_field_projections(catalog_scope):
    sources = [f"{catalog_scope}source-{i}" for i in range(3)]
    destination = f"{catalog_scope}destination"
    for i, source in enumerate(sources):
        _register(source, f"source-v{i}")
    _register(destination, "destination-v1")

    mappings = [
        _mapping(source, f"source-v{i}", f"raw-{i}", f"feature-{i}", f"field-{i}")
        for i, source in enumerate(sources)
    ]
    lineage = _lineage("multi-source", mappings=mappings)
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", sources, lineage) == 3
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", sources, lineage) == 0
    with metadb.session() as s:
        publication_keys = set(s.scalars(select(
            metadb.CatalogLineageFact.publication_key).where(
                metadb.CatalogLineageFact.run_id == "run-multi-source")))
    assert len(publication_keys) == 1
    rows = [row for row in metadb.catalog_lineage_pairs()
            if row["child"] == destination]
    assert rows == [
        {"parent": source, "child": destination, "fact_count": 1}
        for source in sources
    ]

    with metadb.session() as s:
        facts = list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id == "run-multi-source"
        ).order_by(metadb.CatalogLineageFact.source_uri)))
    assert [row.field_mappings_json for row in facts] == [
        json.dumps([mapping], sort_keys=True, separators=(",", ":"))
        for mapping in mappings
    ]

    destination_dataset_id = _registration_id(destination)
    first, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id,
        "destination-v1",
        ["feature-0", "feature-1", "feature-2"],
        limit=2,
    )
    assert len(first) == 2 and cursor == first[-1]["id"]
    assert truncated is True and available is True
    second, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id,
        "destination-v1",
        ["feature-0", "feature-1", "feature-2"],
        limit=2,
        after_id=first[-1]["id"],
    )
    assert len(second) == 1 and cursor is None
    assert truncated is False and available is True
    assert {
        (
            item["source_dataset_id"],
            item["source_version"],
            item["source_field"],
            item["source_field_id"],
            item["destination_field"],
        )
        for item in [*first, *second]
    } == {
        (
            mapping["source_dataset_id"],
            mapping["source_version"],
            mapping["source_field"],
            mapping["source_field_id"],
            mapping["destination_field"],
        )
        for mapping in mappings
    }
    empty, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id,
        "destination-v1",
        ["unmapped-field"],
    )
    assert empty == [] and cursor is None
    assert truncated is False and available is True
    empty, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id,
        "wrong-destination-v9",
        ["unmapped-field"],
    )
    assert empty == [] and cursor is None
    assert truncated is False and available is False

    unadmitted = f"{catalog_scope}unadmitted"
    _register(unadmitted, "unadmitted-v1")
    with pytest.raises(ValueError, match="unadmitted source dataset"):
        metadb.catalog_record_lineage(
            destination, "destination-v1", sources,
            _lineage(
                "unadmitted-mapping",
                mappings=[_mapping(unadmitted, "unadmitted-v1", "left", "out")],
            ))
    with pytest.raises(ValueError, match="wrong exact source version"):
        metadb.catalog_record_lineage(
            destination,
            "destination-v1",
            sources,
            _lineage(
                "wrong-source-version",
                mappings=[_mapping(sources[0], "wrong-v9", "left", "out")],
            ),
        )

    huge_mappings = [
        _mapping(
            sources[0],
            "source-v0",
            f"s{i:03d}" + "x" * 508,
            f"d{i:03d}" + "y" * 508,
        )
        for i in range(64)
    ]
    with pytest.raises(ValueError, match="64 KiB"):
        metadb.catalog_record_lineage(
            destination, "destination-v1", [sources[0]],
            _lineage("oversize-mappings", mappings=huge_mappings))


def test_field_mapping_admission_failure_rolls_back_new_output(catalog_scope):
    admitted = f"{catalog_scope}admitted"
    unadmitted = f"{catalog_scope}unadmitted"
    destination = f"{catalog_scope}destination"
    _register(admitted, "admitted-v1")
    _register(unadmitted, "unadmitted-v1")
    lineage = _lineage(
        "atomic-unadmitted-source",
        mappings=[_mapping(unadmitted, "unadmitted-v1", "raw_id", "id")],
    )
    with metadb.session() as s:
        before_projections = int(s.scalar(select(func.count()).select_from(
            metadb.CatalogFieldLineageProjection)) or 0)

    with pytest.raises(ValueError, match="unadmitted source dataset"):
        metadb.catalog_upsert_entry(
            destination,
            "destination",
            {
                "id": "tbl_atomic_unadmitted",
                "name": "destination",
                "uri": destination,
                "version": "destination-v1",
            },
            parents=[admitted],
            lineage=lineage,
        )

    assert metadb.catalog_get(destination) is None
    with metadb.session() as s:
        assert not s.scalar(select(metadb.CatalogPublicationEvent.event_key).where(
            metadb.CatalogPublicationEvent.uri == destination))
        assert not s.scalar(select(metadb.CatalogLineageFact.id).where(
            metadb.CatalogLineageFact.destination_uri == destination))
        assert int(s.scalar(select(func.count()).select_from(
            metadb.CatalogFieldLineageProjection)) or 0) == before_projections


def test_changed_source_ownership_collides_and_registration_cannot_rebind(catalog_scope):
    sources = [f"{catalog_scope}source-{index}" for index in range(2)]
    destination = f"{catalog_scope}destination"
    for source in sources:
        _register(source, "source-v1")
    _register(destination, "destination-v1")
    source_ids = [_registration_id(source) for source in sources]
    key = "changed-source-ownership"
    first = _lineage(
        key, mappings=[_mapping(sources[0], "source-v1", "raw_id", "id")])
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", sources, first) == 2

    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_record_lineage(
            destination,
            "destination-v1",
            sources,
            _lineage(
                key,
                mappings=[_mapping(sources[1], "source-v1", "raw_id", "id")],
            ),
        )

    destination_dataset_id = _registration_id(destination)
    rows, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id, "destination-v1", ["id"])
    assert cursor is None and truncated is False and available is True
    assert [row["source_dataset_id"] for row in rows] == [source_ids[0]]

    metadb.catalog_delete_entry(sources[0])
    _register(sources[0], "source-v1")
    assert _registration_id(sources[0]) != source_ids[0]
    rows, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id, "destination-v1", ["id"])
    assert rows == [] and cursor is None and truncated is False and available is True


def test_known_exact_output_without_mappings_is_available(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    _register(destination, "destination-v1")
    assert metadb.catalog_record_lineage(
        destination,
        "destination-v1",
        [source],
        _lineage("no-field-mappings"),
    ) == 1

    destination_dataset_id = _registration_id(destination)
    rows, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id, "destination-v1", ["id"])
    assert rows == [] and cursor is None and truncated is False and available is True
    rows, cursor, truncated, available = metadb.catalog_field_lineage_page(
        destination_dataset_id, "missing-v2", ["id"])
    assert rows == [] and cursor is None and truncated is False and available is False


def test_long_raw_identity_is_indexed_by_hash_without_changing_export(catalog_scope):
    source = "opaque://" + "s" * 4_000
    destination = f"{catalog_scope}long-source-destination"
    _register(destination, "destination-v1")
    after_id = _max_fact_id()

    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [source], _lineage("long-raw-source")) == 1
    rows, truncated = metadb.catalog_lineage_pairs_touching([source], 10)
    assert truncated is False
    assert rows == [{"parent": source, "child": destination, "fact_count": 1}]
    facts, cursor, has_more = metadb.catalog_lineage_facts_page(
        limit=10, after_id=after_id)
    assert cursor is None and has_more is False
    assert len(facts) == 1
    assert (facts[0]["source_key"], facts[0]["source_uri"]) == (source, source)

    metadb.catalog_delete_entry(destination)
    with metadb.session() as s:
        assert not list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.source_key_hash
            == metadb._catalog_lineage_identity_hash(source))))


def test_lineage_hash_collision_never_matches_or_deletes_another_identity(
        catalog_scope, monkeypatch):
    sources = [f"{catalog_scope}collision-source-{index}" for index in range(2)]
    destinations = [f"{catalog_scope}collision-destination-{index}" for index in range(2)]
    lineage_keys = [f"forced-hash-collision-{uuid.uuid4().hex}-{index}" for index in range(2)]
    for uri in [*sources, *destinations]:
        _register(uri, "v1")
    monkeypatch.setattr(
        metadb, "_catalog_lineage_identity_hash", lambda _value: "0" * 64)
    for index in range(2):
        assert metadb.catalog_record_lineage(
            destinations[index], "v1", [sources[index]],
            _lineage(lineage_keys[index])) == 1

    rows, truncated = metadb.catalog_lineage_pairs_touching([sources[0]], 10)
    assert truncated is False
    assert rows == [{
        "parent": sources[0], "child": destinations[0], "fact_count": 1,
    }]
    metadb.catalog_delete_entry(destinations[0])
    with metadb.session() as s:
        remaining = list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id.in_([
                f"run-{key}" for key in lineage_keys
            ]))))
    assert len(remaining) == 1
    assert (remaining[0].source_key, remaining[0].destination_key) == (
        sources[1], destinations[1])

    # Teardown while the forced digest is still active so the fixture cannot inherit synthetic rows.
    metadb.catalog_delete_entry(destinations[1])
    metadb.catalog_delete_entry(sources[0])
    metadb.catalog_delete_entry(sources[1])


def test_lineage_rejects_unexportable_parent_tokens_before_reservation(catalog_scope):
    destination = f"{catalog_scope}destination"
    _register(destination, "destination-v1")
    invalid_parent_sets = [
        1,
        [""],
        ["   "],
        ["///"],
        [" leading"],
        ["trailing "],
        ["x" * 8_193],
        [f"{catalog_scope}source-{index}" for index in range(5_001)],
    ]

    with metadb.session() as s:
        before_events = int(s.scalar(select(func.count()).select_from(
            metadb.CatalogPublicationEvent).where(
                metadb.CatalogPublicationEvent.effect_type == "lineage",
                metadb.CatalogPublicationEvent.uri == destination)) or 0)
        before_facts = int(s.scalar(select(func.count()).select_from(
            metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.destination_uri == destination)) or 0)

    for index, parents in enumerate(invalid_parent_sets):
        with pytest.raises(ValueError):
            metadb.catalog_record_lineage(
                destination, "destination-v1", parents,  # type: ignore[arg-type]
                _lineage(f"invalid-parent-{index}"))

    with metadb.session() as s:
        assert int(s.scalar(select(func.count()).select_from(
            metadb.CatalogPublicationEvent).where(
                metadb.CatalogPublicationEvent.effect_type == "lineage",
                metadb.CatalogPublicationEvent.uri == destination)) or 0) == before_events
        assert int(s.scalar(select(func.count()).select_from(
            metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.destination_uri == destination)) or 0) == before_facts


@pytest.mark.parametrize("invalid_version", ["", "v" * 513], ids=["empty", "too-long"])
@pytest.mark.parametrize("invalid_side", ["source", "destination"])
def test_invalid_catalog_versions_cannot_poison_lineage_export(
        catalog_scope, invalid_version, invalid_side):
    from hub.models import LineageFact

    source = f"{catalog_scope}source-{invalid_side}-{len(invalid_version)}"
    destination = f"{catalog_scope}destination-{invalid_side}-{len(invalid_version)}"
    _register(source, invalid_version if invalid_side == "source" else "source-v1")
    _register(
        destination,
        invalid_version if invalid_side == "destination" else "destination-v1",
    )
    key = f"invalid-{invalid_side}-version-{len(invalid_version)}"

    with pytest.raises(ValueError):
        metadb.catalog_record_lineage(
            destination, "destination-v1", [source], _lineage(key))

    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(
            metadb.CatalogPublicationEvent).where(
                metadb.CatalogPublicationEvent.effect_type == "lineage",
                metadb.CatalogPublicationEvent.uri == destination)) == 0
        assert s.scalar(select(func.count()).select_from(
            metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.destination_uri == destination)) == 0

    rows, _cursor, _has_more = metadb.catalog_lineage_facts_page(limit=500, after_id=0)
    for row in rows:
        LineageFact.model_validate({**row, "id": str(row["id"])})


def test_pair_cap_and_fact_cursor(catalog_scope):
    source = f"{catalog_scope}hub"
    destinations = [f"{catalog_scope}destination-{i}" for i in range(3)]
    _register(source, "hub-v1")
    for destination in destinations:
        _register(destination, "destination-v1")
    after_id = _max_fact_id()
    for i, destination in enumerate(destinations):
        assert metadb.catalog_record_lineage(
            destination, "destination-v1", [source], _lineage(f"cursor-{i}")) == 1

    pairs, truncated = metadb.catalog_lineage_pairs_touching([source], limit=2)
    assert len(pairs) == 2 and truncated is True
    all_pairs, truncated = metadb.catalog_lineage_pairs_touching([source], limit=3)
    assert len(all_pairs) == 3 and truncated is False

    first, cursor, has_more = metadb.catalog_lineage_facts_page(
        limit=2, after_id=after_id)
    assert len(first) == 2 and has_more is True
    assert isinstance(cursor, int) and cursor == first[-1]["id"]
    second, final_cursor, has_more = metadb.catalog_lineage_facts_page(
        limit=2, after_id=cursor)
    assert len(second) == 1 and final_cursor is None and has_more is False
    assert {row["destination_uri"] for row in [*first, *second]} == set(destinations)


def test_upsert_collision_rolls_back_entry_and_fact_together(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    lineage = _lineage("atomic-upsert")
    metadb.catalog_upsert_entry(destination, "destination", {
        "id": "tbl_atomic_destination", "name": "destination",
        "uri": destination, "version": "destination-v1",
    }, parents=[source], lineage=lineage)

    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_upsert_entry(destination, "destination", {
            "id": "tbl_atomic_destination", "name": "destination",
            "uri": destination, "version": "destination-v2",
        }, parents=[source], lineage=lineage)

    assert metadb.catalog_get(destination)["version"] == "destination-v1"
    pairs = [row for row in metadb.catalog_lineage_pairs()
             if row["child"] == destination]
    assert pairs == [{"parent": source, "child": destination, "fact_count": 1}]


def test_stale_exact_upsert_replay_never_rolls_back_newer_projection(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    first = _lineage("stale-replay-v1")
    second = _lineage("stale-replay-v2")
    v1 = {
        "id": "tbl_stale_destination", "name": "destination",
        "uri": destination, "version": "destination-v1",
    }
    v2 = {**v1, "version": "destination-v2"}

    assert metadb.catalog_upsert_entry(
        destination, "destination", v1, parents=[source], lineage=first) is True
    assert metadb.catalog_upsert_entry(
        destination, "destination", v2, parents=[source], lineage=second) is True
    assert metadb.catalog_upsert_entry(
        destination, "destination", v1, parents=[source], lineage=first) is False

    assert metadb.catalog_get(destination)["version"] == "destination-v2"
    with metadb.session() as s:
        facts = list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.destination_uri == destination
        ).order_by(metadb.CatalogLineageFact.id)))
    assert [fact.destination_version for fact in facts] == [
        "destination-v1", "destination-v2"]


def test_exact_lineage_replay_does_not_reresolve_advanced_source(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    _register(destination, "destination-v1")
    lineage = _lineage("source-projection-advanced")
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [source], lineage) == 1

    _register(source, "source-v2")
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [source], lineage) == 0
    with metadb.session() as s:
        facts = list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id == "run-source-projection-advanced")))
    assert len(facts) == 1
    assert (facts[0].source_uri, facts[0].source_version) == (source, "source-v1")


def test_empty_and_nonempty_source_sets_collide_in_both_directions(catalog_scope):
    source = f"{catalog_scope}source"
    _register(source, "source-v1")

    empty_first = f"{catalog_scope}empty-first"
    empty_lineage = _lineage("empty-first")
    assert metadb.catalog_upsert_entry(empty_first, "empty-first", {
        "id": "tbl_empty_first", "name": "empty-first",
        "uri": empty_first, "version": "v1",
    }, parents=[], lineage=empty_lineage) is True
    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_upsert_entry(empty_first, "empty-first", {
            "id": "tbl_empty_first", "name": "empty-first",
            "uri": empty_first, "version": "v1",
        }, parents=[source], lineage=empty_lineage)

    nonempty_first = f"{catalog_scope}nonempty-first"
    nonempty_lineage = _lineage("nonempty-first")
    assert metadb.catalog_upsert_entry(nonempty_first, "nonempty-first", {
        "id": "tbl_nonempty_first", "name": "nonempty-first",
        "uri": nonempty_first, "version": "v1",
    }, parents=[source], lineage=nonempty_lineage) is True
    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_upsert_entry(nonempty_first, "nonempty-first", {
            "id": "tbl_nonempty_first", "name": "nonempty-first",
            "uri": nonempty_first, "version": "v1",
        }, parents=[], lineage=nonempty_lineage)


def test_publication_key_reserves_complete_evidence(catalog_scope):
    source_one = f"{catalog_scope}source-one"
    source_two = f"{catalog_scope}source-two"
    destination_one = f"{catalog_scope}destination-one"
    destination_two = f"{catalog_scope}destination-two"
    for uri in (source_one, source_two, destination_one, destination_two):
        _register(uri, "v1")
    lineage = _lineage("publication-wide-key")

    assert metadb.catalog_record_lineage(
        destination_one, "v1", [source_one], lineage) == 1
    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_record_lineage(
            destination_two, "v1", [source_two], lineage)
    with pytest.raises(RuntimeError, match="publication key collision"):
        metadb.catalog_record_lineage(
            destination_one, "v1", [source_one, source_two], lineage)

    assert [row for row in metadb.catalog_lineage_pairs()
            if row["child"] in (destination_one, destination_two)] == [{
                "parent": source_one, "child": destination_one, "fact_count": 1,
            }]


def test_concurrent_disjoint_publications_cannot_share_one_key(catalog_scope):
    sources = [f"{catalog_scope}source-{index}" for index in range(2)]
    destinations = [f"{catalog_scope}destination-{index}" for index in range(2)]
    for uri in [*sources, *destinations]:
        _register(uri, "v1")
    lineage = _lineage("concurrent-publication-wide-key")
    barrier = threading.Barrier(2)

    def publish(index: int):
        barrier.wait(timeout=5)
        return metadb.catalog_record_lineage(
            destinations[index], "v1", [sources[index]], lineage)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, index) for index in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except RuntimeError as error:
                outcomes.append(str(error))
    assert outcomes.count(1) == 1
    assert sum("publication key collision" in str(value) for value in outcomes) == 1


def test_in_place_version_transition_preserves_exact_fact(catalog_scope):
    uri = f"{catalog_scope}in-place"
    table_id = f"tbl_in_place_{uuid.uuid4().hex}"
    _register(uri, "v1", table_id=table_id)

    metadb.catalog_upsert_entry(uri, "in-place", {
        "id": table_id, "name": "in-place", "uri": uri, "version": "v2",
    }, parents=[uri], lineage=_lineage("in-place-version-transition"))

    with metadb.session() as s:
        facts = list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id == "run-in-place-version-transition")))
    assert len(facts) == 1
    assert (facts[0].source_key, facts[0].destination_key) == (uri, uri)
    assert (facts[0].source_version, facts[0].destination_version) == ("v1", "v2")


def test_unregister_and_reregister_do_not_inherit_facts(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    _register(destination, "destination-v1")
    retired_registration_id = _registration_id(source)
    original_lineage = _lineage("before-unregister")
    metadb.catalog_record_lineage(
        destination, "destination-v1", [source], original_lineage)
    assert any(row["child"] == destination for row in metadb.catalog_lineage_pairs())
    with metadb.session() as s:
        retired_fact_id = int(s.scalar(select(func.max(
            metadb.CatalogLineageFact.id)).where(
                metadb.CatalogLineageFact.destination_uri == destination)))

    metadb.catalog_delete_entry(source)
    assert not any(destination in (row["parent"], row["child"])
                   for row in metadb.catalog_lineage_pairs())
    _register(source, "source-v2")
    assert _registration_id(source) != retired_registration_id
    assert not any(destination in (row["parent"], row["child"])
                   for row in metadb.catalog_lineage_pairs())

    # The durable publication reservation survives unregister. An old retry cannot attach its
    # evidence to a replacement registration that happens to reuse the same URI.
    assert metadb.catalog_record_lineage(
        destination, "destination-v1", [source], original_lineage) == 0

    metadb.catalog_record_lineage(
        destination, "destination-v1", [source], _lineage("after-reregister"))
    assert any(row["child"] == destination for row in metadb.catalog_lineage_pairs())
    with metadb.session() as s:
        replacement_fact_id = int(s.scalar(select(func.max(
            metadb.CatalogLineageFact.id)).where(
                metadb.CatalogLineageFact.destination_uri == destination)))
    assert replacement_fact_id > retired_fact_id
    metadb.catalog_delete_prefix(destination)
    assert not any(destination in (row["parent"], row["child"])
                   for row in metadb.catalog_lineage_pairs())


def test_concurrent_source_unregister_serializes_after_upsert(
        catalog_scope, monkeypatch):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    _register(destination, "destination-v1")
    source_validated = threading.Event()
    allow_publication = threading.Event()
    delete_started = threading.Event()
    original_validate = metadb._catalog_validate_lineage_parent_entries

    def pause_after_source_validation(snapshots, locked_entries):
        original_validate(snapshots, locked_entries)
        source_validated.set()
        assert allow_publication.wait(timeout=5)

    monkeypatch.setattr(
        metadb, "_catalog_validate_lineage_parent_entries", pause_after_source_validation)

    def publish() -> None:
        metadb.catalog_upsert_entry(destination, "destination", {
            "id": "tbl_race_destination", "name": "destination",
            "uri": destination, "version": "destination-v2",
        }, parents=[source], lineage=_lineage("source-unregister-race"))

    def unregister() -> bool:
        delete_started.set()
        return metadb.catalog_delete_entry(source)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(publish)
        deletion = None
        try:
            assert source_validated.wait(timeout=5)
            deletion = pool.submit(unregister)
            assert delete_started.wait(timeout=5)
            with pytest.raises(concurrent.futures.TimeoutError):
                deletion.result(timeout=0.05)
        finally:
            allow_publication.set()
        publication.result(timeout=10)
        assert deletion is not None
        assert deletion.result(timeout=10) is None

    assert metadb.catalog_get(destination)["version"] == "destination-v2"
    assert not any(row["child"] == destination
                   for row in metadb.catalog_lineage_pairs())
    _register(source, "source-v2")
    assert not any(row["child"] == destination
                   for row in metadb.catalog_lineage_pairs())


def test_concurrent_replay_inserts_once(catalog_scope):
    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    _register(destination, "destination-v1")
    lineage = _lineage("concurrent-replay")
    barrier = threading.Barrier(2)

    def publish() -> int:
        barrier.wait(timeout=5)
        return metadb.catalog_record_lineage(
            destination, "destination-v1", [source], lineage)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in
                   [pool.submit(publish), pool.submit(publish)]]
    assert sorted(results) == [0, 1]
    assert [row for row in metadb.catalog_lineage_pairs()
            if row["child"] == destination] == [{
                "parent": source, "child": destination, "fact_count": 1,
            }]


def test_postgres_concurrent_identical_initial_upsert_is_one_publication(catalog_scope):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    _register(source, "source-v1")
    lineage = _lineage(
        "concurrent-initial-upsert",
        mappings=[_mapping(source, "source-v1", "raw_id", "id")],
    )
    barrier = threading.Barrier(2)

    def publish() -> bool:
        barrier.wait(timeout=5)
        return metadb.catalog_upsert_entry(destination, "destination", {
            "id": "tbl_concurrent_destination", "name": "destination",
            "uri": destination, "version": "destination-v1",
        }, parents=[source], lineage=lineage)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in (
            pool.submit(publish), pool.submit(publish))]
    assert sorted(results) == [False, True]
    with metadb.session() as s:
        facts = list(s.scalars(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id == "run-concurrent-initial-upsert")))
        projections = list(s.scalars(select(
            metadb.CatalogFieldLineageProjection).where(
                metadb.CatalogFieldLineageProjection.fact_id.in_(
                    [fact.id for fact in facts]))))
    assert len(facts) == 1 and len(projections) == 1


@pytest.mark.parametrize("retired_target", ["source", "destination"])
def test_postgres_record_lineage_rejects_same_uri_registration_aba(
        catalog_scope, monkeypatch, retired_target):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    table_ids = {
        "source": f"tbl_source_{uuid.uuid4().hex}",
        "destination": f"tbl_destination_{uuid.uuid4().hex}",
    }
    _register(source, None, table_id=table_ids["source"])
    _register(destination, None, table_id=table_ids["destination"])
    retired_uri = source if retired_target == "source" else destination
    retired_registration_id = _registration_id(retired_uri)
    original_lock = metadb._lock_catalog_mutation_targets
    replaced = False

    def replace_after_snapshot(s, tokens, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            metadb.catalog_delete_entry(retired_uri)
            _register(retired_uri, None, table_id=table_ids[retired_target])
        return original_lock(s, tokens, **kwargs)

    monkeypatch.setattr(
        metadb, "_lock_catalog_mutation_targets", replace_after_snapshot)

    with pytest.raises(RuntimeError, match="lineage entry changed concurrently"):
        metadb.catalog_record_lineage(
            destination, None, [source], _lineage(f"record-aba-{retired_target}"))

    assert replaced is True
    assert _registration_id(retired_uri) != retired_registration_id
    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(
            metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.run_id
                == f"run-record-aba-{retired_target}")) == 0


def test_postgres_upsert_rejects_same_uri_source_registration_aba(
        catalog_scope, monkeypatch):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    source = f"{catalog_scope}source"
    destination = f"{catalog_scope}destination"
    source_table_id = f"tbl_source_{uuid.uuid4().hex}"
    _register(source, None, table_id=source_table_id)
    retired_registration_id = _registration_id(source)
    original_snapshot = metadb._catalog_lineage_parent_snapshot
    replaced = False

    def replace_after_snapshot(session, token):
        nonlocal replaced
        snapshot = original_snapshot(session, token)
        if token == source and not replaced:
            replaced = True
            metadb.catalog_delete_entry(source)
            _register(source, None, table_id=source_table_id)
        return snapshot

    monkeypatch.setattr(
        metadb, "_catalog_lineage_parent_snapshot", replace_after_snapshot)

    with pytest.raises(RuntimeError, match="lineage entry changed concurrently"):
        metadb.catalog_upsert_entry(destination, "destination", {
            "id": f"tbl_destination_{uuid.uuid4().hex}",
            "name": "destination",
            "uri": destination,
            "version": None,
        }, parents=[source], lineage=_lineage("upsert-source-aba"))

    assert replaced is True
    assert _registration_id(source) != retired_registration_id
    assert metadb.catalog_get(destination) is None
    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(
            metadb.CatalogLineageFact).where(
                metadb.CatalogLineageFact.run_id == "run-upsert-source-aba")) == 0
