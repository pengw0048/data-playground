"""Product admission contract for default-local create and replace writes."""

from __future__ import annotations

import os
import time
import json
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hub import db, metadb
from hub.api_errors import APIErrorCode
from hub.models import (
    ColumnSchema, Graph, PerNodeStatus, RunOutput, RunStatus, WriteAdmission,
)
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.plugins.processors import ProcessorRegistry, RegisteredProcessor
from hub.routers.runs import _write_admission_for_graph
from hub.routers.runs import _inject_write_intent
from hub.routers.runs import _local_run_intent_sha256
from hub.routers import runs as run_routes
from hub.main import app
from hub.local_writes import write_managed_local_file
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'write-admission.db'}"
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
def contract(tmp_path):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [1, 2]}), source)
    storage = LocalStorage(str(tmp_path / "outputs"))
    adapter = DuckDBAdapter()
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: adapter)
    graph = Graph.model_validate({
        "id": "write-admission-canvas",
        "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "data": {"title": "output", "config": {
                "filename": "output.parquet", "writeMode": "overwrite",
            }}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    deps = SimpleNamespace(
        workspace=str(tmp_path), storage=storage, catalog=catalog,
        resolve_adapter=lambda _uri: adapter,
        registry=ProcessorRegistry(), node_builders={},
        node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS},
    )
    try:
        yield deps, graph
    finally:
        storage.close()


@pytest.fixture
def lance_contract(tmp_path):
    lance = pytest.importorskip("lance")
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [2, 3]}), source)
    storage = LocalStorage(str(tmp_path / "outputs"))
    destination = storage.output_uri("existing", ".lance")
    lance.write_dataset(pa.table({"value": [1]}), destination)
    duckdb_adapter = DuckDBAdapter()
    lance_adapter = LanceAdapter()

    def resolve_adapter(uri):
        return lance_adapter if str(uri).lower().rstrip("/").endswith(".lance") else duckdb_adapter

    catalog = InMemoryCatalog(str(tmp_path / "data"), resolve_adapter)
    table = catalog._add(name="existing", uri=destination, strict_probe=True)
    graph = Graph.model_validate({
        "id": "lance-write-admission-canvas",
        "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "data": {"title": "existing", "config": {
                "filename": "existing.lance", "writeMode": "append",
            }}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    runner_capability = SimpleNamespace(supports_managed_local_write_intents=lambda: True)
    deps = SimpleNamespace(
        workspace=str(tmp_path), storage=storage, catalog=catalog,
        resolve_adapter=resolve_adapter,
        registry=ProcessorRegistry(), node_builders={},
        node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS},
        node_ir={}, runners=[], runner=runner_capability,
        pick_runner=lambda _plan, _uid: runner_capability,
    )
    try:
        yield lance, deps, graph, table
    finally:
        storage.close()


def _publish(deps, admission, values):
    assert admission.intent is not None

    def writer(uri: str) -> None:
        pq.write_table(pa.table({"value": values}), uri)

    return write_managed_local_file(
        storage=deps.storage,
        catalog=deps.catalog,
        intent=admission.intent,
        write_artifact=writer,
    )


def _managed_publication_counts() -> tuple[int, int, int]:
    with metadb.session() as session:
        return tuple(int(session.scalar(
            select(func.count()).select_from(model)) or 0) for model in (
                metadb.CatalogEntry,
                metadb.CatalogLogicalDataset,
                metadb.ManagedLocalFileRevision,
            ))


def _run_allocation_counts() -> tuple[int, int, int, int]:
    with metadb.session() as session:
        return tuple(int(session.scalar(
            select(func.count()).select_from(model)) or 0) for model in (
                metadb.RunState,
                metadb.RunRecord,
                metadb.RunInputAdmission,
                metadb.DurableTask,
            ))


def _set_exact_revision_schema(revision_id: str, columns: list[dict]) -> None:
    with metadb.session() as session:
        row = session.get(metadb.ManagedLocalFileRevision, revision_id)
        assert row is not None
        table = json.loads(row.table_doc)
        table["columns"] = [
            ColumnSchema.model_validate(column).model_dump(by_alias=True, mode="json")
            for column in columns
        ]
        row.table_doc = json.dumps(table)


def _admit_schema_change(contract, monkeypatch, before: list[dict], after: list[dict]):
    deps, graph = contract
    create = _write_admission_for_graph(
        deps, graph, "write", "researcher", "schema-create")
    receipt = _publish(deps, create, [1])
    _set_exact_revision_schema(receipt.revision_id, before)
    proposed = [ColumnSchema.model_validate(column) for column in after]
    monkeypatch.setattr(
        run_routes,
        "schema_for_graph",
        lambda *_args, **_kwargs: {"source": proposed, "write": proposed},
    )
    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "schema-replace")
    assert admission.intent is not None
    assert admission.intent.schema_drift is not None
    return deps, graph, admission


@pytest.mark.parametrize(
    ("filename", "reason"),
    [
        ("", "blank"),
        ("   ", "blank"),
        ("../escape.parquet", "path_syntax"),
        ("nested/output.parquet", "path_syntax"),
        (r"nested\output.parquet", "path_syntax"),
        ("family\u0085cost", "path_syntax"),
        ("*", "path_syntax"),
        ("?", "path_syntax"),
        ("[", "path_syntax"),
    ],
)
def test_managed_name_admission_returns_field_error_without_publication(
        contract, monkeypatch, filename, reason):
    deps, graph = contract
    next(node for node in graph.nodes if node.id == "write").data["config"]["filename"] = filename
    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)
    before_artifacts = set(os.listdir(deps.storage.result_root))
    before_publications = _managed_publication_counts()

    response = TestClient(app).post("/api/run/write-admission", json={
        "graph": graph.model_dump(by_alias=True, mode="json"),
        "nodeId": "write",
        "submissionId": "10111111-1111-4111-8111-111111111111",
    })

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": (
            "managed dataset name must not be blank"
            if reason == "blank"
            else "managed dataset name must be one name, not a path or URI"
        ),
        "code": APIErrorCode.INVALID_MANAGED_DATASET_NAME,
        "retryable": False,
        "field": "filename",
        "reason": reason,
    }
    assert set(os.listdir(deps.storage.result_root)) == before_artifacts
    assert _managed_publication_counts() == before_publications


def test_direct_run_returns_the_same_name_error_before_run_allocation(
        contract, monkeypatch):
    deps, graph = contract
    next(node for node in graph.nodes if node.id == "write").data["config"][
        "filename"] = "../escape.parquet"
    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)
    before_runs = _run_allocation_counts()
    before_publications = _managed_publication_counts()
    before_artifacts = set(os.listdir(deps.storage.result_root))

    response = TestClient(app).post("/api/run", json={
        "graph": graph.model_dump(by_alias=True, mode="json"),
        "targetNodeId": "write",
        "confirmed": True,
    })

    assert response.status_code == 422, response.text
    assert response.json()["code"] == APIErrorCode.INVALID_MANAGED_DATASET_NAME
    assert response.json()["field"] == "filename"
    assert response.json()["reason"] == "path_syntax"
    assert _run_allocation_counts() == before_runs
    assert _managed_publication_counts() == before_publications
    assert set(os.listdir(deps.storage.result_root)) == before_artifacts


def test_sink_config_treats_injected_null_filename_as_absent():
    from hub.sinks import SinkSpec

    spec = SinkSpec.from_config({
        "filename": None,
        "name": "aggregate output",
        "format": "parquet",
    })

    assert spec.name == "aggregate output"
    assert spec.filename == "aggregate output.parquet"


def test_normal_managed_name_is_preserved_across_admission_receipt_catalog_and_revision(
        contract, monkeypatch):
    from hub.routers import catalog as catalog_routes

    deps, graph = contract
    write = next(node for node in graph.nodes if node.id == "write")
    write.data["config"]["filename"] = "family cost"
    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)
    monkeypatch.setattr(catalog_routes, "get_deps", lambda: deps)

    response = TestClient(app).post("/api/run/write-admission", json={
        "graph": graph.model_dump(by_alias=True, mode="json"),
        "nodeId": "write",
        "submissionId": "10222222-2222-4222-8222-222222222222",
    })

    assert response.status_code == 200, response.text
    admission = WriteAdmission.model_validate(response.json())
    assert admission.intent is not None
    assert write.data["config"]["filename"] == admission.intent.destination.name == "family cost"

    receipt = _publish(deps, admission, [1, 2])
    table = deps.catalog.get_table(receipt.dataset_id)
    assert receipt.name == table.name == admission.intent.destination.name
    assert receipt.publication.logical_uri == admission.intent.destination.logical_uri
    assert os.path.splitext(os.path.basename(receipt.publication.logical_uri))[0] == table.name

    with metadb.session() as session:
        revision = session.get(metadb.ManagedLocalFileRevision, receipt.revision_id)
        assert revision is not None
        assert json.loads(revision.table_doc)["name"] == table.name

    exact = TestClient(app).get(
        f"/api/catalog/revisions/{receipt.dataset_id}/{receipt.revision_id}")
    assert exact.status_code == 200, exact.text
    assert exact.json()["datasetId"] == receipt.dataset_id
    assert exact.json()["revisionId"] == receipt.revision_id
    assert exact.json()["name"] == receipt.name
    assert exact.json()["preview"]["rows"] == [{"value": 1}, {"value": 2}]

    metadb.catalog_set_metadata(
        table.uri,
        folder="",
        owner=None,
        description=None,
        tags=[],
        name="current friendly name",
    )
    assert deps.catalog.get_table(receipt.dataset_id).name == "current friendly name"
    exact_after_rename = TestClient(app).get(
        f"/api/catalog/revisions/{receipt.dataset_id}/{receipt.revision_id}")
    assert exact_after_rename.status_code == 200, exact_after_rename.text
    assert exact_after_rename.json()["name"] == receipt.name


def test_every_execution_sink_boundary_rejects_invalid_name_before_effect(contract):
    from hub.compiler import compile_plan
    from hub.plugins.runner import LocalRunner, _CancelToken
    from hub.sinks import ManagedDatasetNameError, SinkSpec
    from hub.subprocess_runner import SubprocessRunner

    deps, graph = contract
    write = next(node for node in graph.nodes if node.id == "write")
    write.data["config"]["filename"] = "../escape.parquet"
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs)
    status = RunStatus(
        run_id="invalid-name-execution",
        status="queued",
        target_node_id="write",
        per_node=[PerNodeStatus(node_id="write", status="queued", label="write")],
        outputs=[RunOutput(
            node_id="write",
            port_id="out",
            wire="dataset",
            publication_kind="catalog",
            outcome="pending",
        )],
    )
    before_artifacts = set(os.listdir(deps.storage.result_root))
    before_publications = _managed_publication_counts()
    local = LocalRunner(
        deps.resolve_adapter,
        deps.registry,
        deps.catalog,
        deps.workspace,
        node_specs=deps.node_specs,
        storage=deps.storage,
    )

    with pytest.raises(ManagedDatasetNameError, match="not a path"):
        local._run_object_store_cfg(plan, {"write": write})
    with pytest.raises(ManagedDatasetNameError, match="not a path"):
        local._commit_write(
            write, graph, None, status, None, _CancelToken())

    isolated = SubprocessRunner(
        deps.workspace,
        deps.catalog.data_dir,
        catalog=deps.catalog,
        storage=deps.storage,
        resolve_adapter=deps.resolve_adapter,
        node_specs=deps.node_specs,
        registry=deps.registry,
    )
    with pytest.raises(ManagedDatasetNameError, match="not a path"):
        isolated._claim_sink_contracts(plan, graph, status.run_id, status)

    with pytest.raises(ManagedDatasetNameError, match="not a path"):
        SinkSpec(
            name="../escape",
            filename="../escape.parquet",
            extension=".parquet",
            mode="overwrite",
            destination_id=None,
            destination_path="",
            partition_by="",
        )
    assert set(os.listdir(deps.storage.result_root)) == before_artifacts
    assert _managed_publication_counts() == before_publications


def test_preflight_is_metadata_only_and_derives_create_then_replace(contract):
    deps, graph = contract
    before = set(os.listdir(deps.storage.result_root))
    create = _write_admission_for_graph(
        deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111111")

    assert create.managed is True
    assert create.mode == "create"
    assert create.expected_head is None
    assert create.intent is not None and create.intent.schema_drift is None
    assert [(column.name, column.type) for column in create.expected_schema] == [("value", "int")]
    assert set(os.listdir(deps.storage.result_root)) == before

    receipt = _publish(deps, create, [1, 2])
    replace = _write_admission_for_graph(
        deps, graph, "write", "researcher", "22222222-2222-4222-8222-222222222222")
    assert replace.mode == "replace"
    assert replace.expected_head is not None
    assert replace.expected_head.revision_id == receipt.revision_id
    assert replace.intent is not None
    assert replace.intent.destination.dataset_id == receipt.dataset_id
    assert replace.intent.schema_drift is not None
    assert replace.intent.schema_drift.requires_confirmation is False


@pytest.mark.parametrize(
    ("before", "after", "expected_kind", "expected_status", "requires_confirmation"),
    [
        (
            [{"name": "value", "type": "int", "nullable": None}],
            [{"name": "value", "type": "int", "nullable": None}],
            "unchanged", "unknown", False,
        ),
        (
            [{"name": "value", "type": "int", "nullable": True}],
            [
                {"name": "value", "type": "int", "nullable": True},
                {"name": "extra", "type": "string", "nullable": True},
            ],
            "added", "compatible", True,
        ),
        (
            [{"name": "value", "type": "int", "nullable": True}],
            [{"name": "other", "type": "int", "nullable": True}],
            "removed", "unknown", True,
        ),
        (
            [{"fieldId": "field-1", "name": "value", "type": "int", "nullable": True}],
            [{"fieldId": "field-1", "name": "amount", "type": "int", "nullable": True}],
            "renamed", "compatible", True,
        ),
        (
            [{"fieldId": "field-1", "name": "value", "type": "int", "nullable": True}],
            [{"fieldId": "field-2", "name": "value", "type": "int", "nullable": True}],
            "changed", "unknown", True,
        ),
        (
            [{"name": "value", "type": "int", "nullable": True}],
            [{"name": "value", "type": "int32", "nullable": True}],
            "unchanged", "compatible", False,
        ),
        (
            [{"name": "value", "type": "int", "nullable": True}],
            [{"name": "value", "type": "bigint", "nullable": True}],
            "unchanged", "compatible", True,
        ),
        (
            [{"name": "value", "type": "int", "nullable": True}],
            [{"name": "value", "type": "string", "nullable": True}],
            "unchanged", "breaking", True,
        ),
    ],
)
def test_replace_freezes_bounded_exact_head_schema_drift(
        contract, monkeypatch, before, after, expected_kind, expected_status,
        requires_confirmation):
    _deps, _graph, admission = _admit_schema_change(
        contract, monkeypatch, before, after)
    evidence = admission.intent.schema_drift
    assert evidence is not None
    assert evidence.compared_head == admission.expected_head
    field = next(item for item in evidence.compatibility.fields
                 if item.kind == expected_kind and item.status == expected_status)
    assert field is not None
    assert evidence.requires_confirmation is requires_confirmation


def test_replace_fails_closed_when_exact_head_schema_metadata_is_corrupt(
        contract, monkeypatch):
    deps, graph = contract
    create = _write_admission_for_graph(
        deps, graph, "write", "researcher", "corrupt-schema-create")
    receipt = _publish(deps, create, [1])
    with metadb.session() as session:
        row = session.get(metadb.ManagedLocalFileRevision, receipt.revision_id)
        assert row is not None
        row.table_doc = "{not-json"

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "corrupt-schema-replace")

    assert admission.intent is None
    assert admission.expected_head is not None
    assert admission.blocker == \
        "the exact destination head has no valid retained schema metadata"


def test_structural_drift_requires_the_displayed_admission_before_allocation(
        contract, monkeypatch):
    deps, graph, admission = _admit_schema_change(
        contract,
        monkeypatch,
        [{"name": "value", "type": "int", "nullable": True}],
        [
            {"name": "value", "type": "int", "nullable": True},
            {"name": "extra", "type": "string", "nullable": True},
        ],
    )
    monkeypatch.setattr(run_routes.auth, "auth_enabled", lambda: False)
    before_runs = _run_allocation_counts()
    before_publications = _managed_publication_counts()
    before_artifacts = set(os.listdir(deps.storage.result_root))

    with pytest.raises(HTTPException, match="explicit confirmation") as unconfirmed:
        run_routes.start_run(
            deps, graph.model_copy(deep=True), "write", "researcher",
            confirmed=False, submission_id="schema-replace",
            write_intent=admission.intent,
        )
    assert unconfirmed.value.status_code == 409

    with pytest.raises(HTTPException, match="displayed write admission") as undisplayed:
        run_routes.start_run(
            deps, graph.model_copy(deep=True), "write", "researcher",
            confirmed=True, submission_id="drift-gate-undisplayed",
        )
    assert undisplayed.value.status_code == 409
    assert _run_allocation_counts() == before_runs
    assert _managed_publication_counts() == before_publications
    assert set(os.listdir(deps.storage.result_root)) == before_artifacts

    class ConfirmedAdmissionReached(RuntimeError):
        pass

    monkeypatch.setattr(
        run_routes,
        "_local_run_intent_sha256",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfirmedAdmissionReached()),
    )
    with pytest.raises(ConfirmedAdmissionReached):
        run_routes.start_run(
            deps, graph.model_copy(deep=True), "write", "researcher",
            confirmed=True, submission_id="schema-replace",
            write_intent=admission.intent,
        )
    assert _run_allocation_counts() == before_runs


def test_drift_receipt_preserves_exact_comparison_and_recovers_after_response_loss(
        contract, monkeypatch):
    deps, graph, admission = _admit_schema_change(
        contract,
        monkeypatch,
        [{"name": "value", "type": "int", "nullable": True}],
        [
            {"name": "value", "type": "int", "nullable": True},
            {"name": "extra", "type": "int", "nullable": True},
        ],
    )
    assert admission.intent is not None

    receipt = write_managed_local_file(
        storage=deps.storage,
        catalog=deps.catalog,
        intent=admission.intent,
        write_artifact=lambda uri: pq.write_table(
            pa.table({"value": [1], "extra": [2]}), uri),
    )
    recovered = _write_admission_for_graph(
        deps, graph, "write", "researcher", "schema-replace",
        supplied=admission.intent,
    )

    assert receipt.schema_drift == admission.intent.schema_drift
    assert receipt.parent_head == admission.intent.schema_drift.compared_head
    assert recovered.recovered_receipt == receipt


def test_drift_runtime_schema_mismatch_publishes_nothing(contract, monkeypatch):
    deps, _graph, admission = _admit_schema_change(
        contract,
        monkeypatch,
        [{"name": "value", "type": "int", "nullable": True}],
        [
            {"name": "value", "type": "int", "nullable": True},
            {"name": "extra", "type": "int", "nullable": True},
        ],
    )
    assert admission.intent is not None
    before = _managed_publication_counts()

    with pytest.raises(ValueError, match="output schema does not match"):
        write_managed_local_file(
            storage=deps.storage,
            catalog=deps.catalog,
            intent=admission.intent,
            write_artifact=lambda uri: pq.write_table(pa.table({"value": [1]}), uri),
        )

    assert _managed_publication_counts() == before


def test_admitted_exact_source_schema_uses_its_revision_without_mutable_scan(tmp_path):
    class ExactOnlyAdapter:
        name = "exact-only"

        def __init__(self):
            self.opened: list[str] = []
            self.mutable_scan_calls = 0

        def scan(self, *_args, **_kwargs):
            self.mutable_scan_calls += 1
            raise AssertionError("schema-only admitted Source must not scan the mutable provider head")

        def open_revision(self, uri, revision_id):
            assert uri == source_uri
            self.opened.append(revision_id)
            if revision_id == "gone":
                raise RuntimeError("revision unavailable")
            return db.conn().from_arrow(pa.table({"value": [1]}))

    source_uri = str(tmp_path / "exact-source")
    source_adapter = ExactOnlyAdapter()
    storage = LocalStorage(str(tmp_path / "outputs"))
    output_adapter = DuckDBAdapter()
    catalog = InMemoryCatalog(
        str(tmp_path / "data"),
        lambda uri: source_adapter if uri == source_uri else output_adapter,
    )
    graph = Graph.model_validate({
        "id": "exact-schema-admission", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {
                "uri": source_uri,
                "_input_provider_uri": source_uri,
                "_input_revision_id": "revision-1",
            }}},
            {"id": "select", "type": "select", "data": {"config": {"select": "value"}}},
            {"id": "write", "type": "write", "data": {"config": {
                "filename": "output.parquet", "writeMode": "overwrite",
            }}},
        ],
        "edges": [
            {"id": "source-select", "source": "source", "target": "select"},
            {"id": "select-write", "source": "select", "target": "write"},
        ],
    })
    deps = SimpleNamespace(
        workspace=str(tmp_path), storage=storage, catalog=catalog,
        resolve_adapter=lambda uri: source_adapter if uri == source_uri else output_adapter,
        registry=ProcessorRegistry(), node_builders={},
        node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS},
    )
    try:
        admitted = _write_admission_for_graph(
            deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111114")
        assert [(column.name, column.type) for column in admitted.expected_schema] == [("value", "int")]
        assert admitted.intent is not None and admitted.blocker is None
        assert source_adapter.opened == ["revision-1"]
        assert source_adapter.mutable_scan_calls == 0

        retained_artifact = tmp_path / "retained-exact.parquet"
        pq.write_table(pa.table({"retained": [1]}), retained_artifact)
        graph.nodes[0].data["config"].update({
            "_input_revision_id": "retained-revision",
            "_input_artifact_uri": str(retained_artifact),
        })
        graph.nodes[1].data["config"]["select"] = "retained"
        graph._input_artifact_uris["source"] = str(retained_artifact)
        retained = _write_admission_for_graph(
            deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111116")
        assert [(column.name, column.type) for column in retained.expected_schema] == [("retained", "int")]
        assert retained.intent is not None and retained.blocker is None
        assert source_adapter.opened == ["revision-1"]
        assert source_adapter.mutable_scan_calls == 0

        graph._input_artifact_uris.clear()
        graph.nodes[0].data["config"].pop("_input_artifact_uri")
        graph.nodes[0].data["config"]["_input_revision_id"] = "gone"
        unavailable = _write_admission_for_graph(
            deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111115")
        assert unavailable.intent is None
        assert unavailable.blocker == (
            "the upstream transform with node ID “select” does not have a bounded output schema "
            "contract. "
            "Select the upstream transform with node ID “select”, then in the Inspector choose "
            "Output schema (contract) → Infer from sample.")
        assert source_adapter.opened[0] == "revision-1"
        assert source_adapter.opened[1:] and set(source_adapter.opened[1:]) == {"gone"}
        assert source_adapter.mutable_scan_calls == 0
    finally:
        storage.close()


def test_missing_schema_blocker_names_the_direct_upstream_transform(contract, monkeypatch):
    deps, graph = contract
    graph.nodes[0].data["title"] = "Normalize purchases"
    monkeypatch.setattr(run_routes, "schema_for_graph", lambda *_args, **_kwargs: {})

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111117")

    assert admission.intent is None
    assert admission.blocker == (
        "the upstream transform “Normalize purchases” does not have a bounded output schema "
        "contract. "
        "Select the upstream transform “Normalize purchases”, then in the Inspector choose "
        "Output schema (contract) → Infer from sample.")


def test_missing_schema_blocker_guides_a_write_without_upstream(contract, monkeypatch):
    deps, graph = contract
    graph.edges = []
    monkeypatch.setattr(run_routes, "schema_for_graph", lambda *_args, **_kwargs: {})

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111118")

    assert admission.intent is None
    assert admission.blocker == "Connect a dataset-producing node to this Write."


def test_direct_local_admission_uses_write_predecessor_regardless_of_node_order(contract):
    deps, graph = contract
    source, write = graph.nodes
    source.data["config"]["outputSchema"] = [{"name": "wrong_source", "type": "string"}]
    select = {
        "id": "select", "type": "select", "data": {"config": {
            "outputSchema": [{"name": "selected_value", "type": "int"}],
        }},
    }
    graph = Graph.model_validate({
        **graph.model_dump(by_alias=True),
        "nodes": [source.model_dump(by_alias=True), select, write.model_dump(by_alias=True)],
        "edges": [
            {"id": "source-select", "source": "source", "target": "select"},
            {"id": "select-write", "source": "select", "target": "write"},
        ],
    })

    reversed_graph = graph.model_copy(deep=True)
    reversed_graph.nodes.reverse()
    admissions = [
        _write_admission_for_graph(
            deps, candidate, "write", "researcher", f"direct-local-{index}", direct_local=True)
        for index, candidate in enumerate((graph, reversed_graph))
    ]

    assert all(admission.intent is not None for admission in admissions)
    assert [
        [(column.name, column.type) for column in admission.expected_schema]
        for admission in admissions
    ] == [[("selected_value", "int")], [("selected_value", "int")]]


def test_direct_local_admission_blocks_ambiguous_predecessors_regardless_of_edge_order(contract):
    deps, graph = contract
    source, write = graph.nodes
    other_source = source.model_copy(deep=True)
    other_source.id = "other-source"
    source.data["config"]["outputSchema"] = [{"name": "left_value", "type": "int"}]
    other_source.data["config"]["outputSchema"] = [{"name": "right_value", "type": "string"}]
    graph = Graph.model_validate({
        **graph.model_dump(by_alias=True),
        "nodes": [
            source.model_dump(by_alias=True),
            other_source.model_dump(by_alias=True),
            write.model_dump(by_alias=True),
        ],
        "edges": [
            {"id": "source-write", "source": "source", "target": "write"},
            {"id": "other-write", "source": "other-source", "target": "write"},
        ],
    })

    reversed_edges = graph.model_copy(deep=True)
    reversed_edges.edges.reverse()
    admissions = [
        _write_admission_for_graph(
            deps, candidate, "write", "researcher", f"ambiguous-{index}", direct_local=True)
        for index, candidate in enumerate((graph, reversed_edges))
    ]

    assert all(admission.intent is None for admission in admissions)
    assert all(
        admission.blocker
        == "one or more direct upstream transforms do not have a bounded output schema contract. "
        "Select each direct upstream transform, then in the Inspector choose Output schema "
        "(contract) → Infer from sample."
        for admission in admissions
    )


def test_stale_admission_fails_before_artifact_and_preserves_new_head(contract):
    deps, graph = contract
    create = _write_admission_for_graph(
        deps, graph, "write", "researcher", "31111111-1111-4111-8111-111111111111")
    _publish(deps, create, [1])
    stale = _write_admission_for_graph(
        deps, graph, "write", "researcher", "32222222-2222-4222-8222-222222222222")
    winner = _write_admission_for_graph(
        deps, graph, "write", "researcher", "33333333-3333-4333-8333-333333333333")
    winning_receipt = _publish(deps, winner, [2])
    before = set(os.listdir(deps.storage.result_root))

    with pytest.raises(HTTPException, match="stale") as exc:
        _write_admission_for_graph(
            deps, graph, "write", "researcher",
            "32222222-2222-4222-8222-222222222222", supplied=stale.intent)

    assert exc.value.status_code == 409
    assert set(os.listdir(deps.storage.result_root)) == before
    assert metadb.catalog_managed_local_write_head(
        winner.destination)["revision_id"] == winning_receipt.revision_id


def test_repeated_admission_recovers_the_exact_durable_receipt(contract):
    deps, graph = contract
    submission = "41111111-1111-4111-8111-111111111111"
    admitted = _write_admission_for_graph(
        deps, graph, "write", "researcher", submission)
    receipt = _publish(deps, admitted, [7])

    recovered = _write_admission_for_graph(
        deps, graph, "write", "researcher", submission, supplied=admitted.intent)

    assert recovered.recovered_receipt == receipt
    assert recovered.recovered_receipt is not None
    assert recovered.recovered_receipt.publication.artifact_uri == receipt.publication.artifact_uri


def test_write_submission_identity_ignores_only_operational_node_status(contract):
    deps, graph = contract
    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "43333333-3333-4333-8333-333333333333")
    assert admission.intent is not None
    initial = _local_run_intent_sha256(graph, "write", write_intent=admission.intent)

    retried = graph.model_copy(deep=True)
    next(node for node in retried.nodes if node.id == "write").data["status"] = "failed"
    assert _local_run_intent_sha256(
        retried, "write", write_intent=admission.intent) == initial

    next(node for node in retried.nodes if node.id == "write").data["config"]["filename"] = "other.parquet"
    assert _local_run_intent_sha256(
        retried, "write", write_intent=admission.intent) != initial


def test_durable_submission_mismatch_is_a_bounded_conflict(contract, monkeypatch):
    _deps, graph = contract

    def conflict(*_args, **_kwargs):
        raise metadb.DurableTaskSubmissionConflict(
            "durable task submission does not match its frozen admission")

    monkeypatch.setattr(run_routes, "start_run", conflict)
    response = TestClient(app).post("/api/run", json={
        "graph": graph.model_dump(by_alias=True, mode="json"),
        "targetNodeId": "write", "confirmed": True,
    })

    assert response.status_code == 409
    assert response.json()["detail"] == \
        "durable task submission does not match its frozen admission"


def test_external_destination_keeps_provider_neutral_mode(contract):
    deps, graph = contract
    write = next(node for node in graph.nodes if node.id == "write")
    write.data["config"] = {
        "filename": "output.csv", "writeMode": "append",
    }

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "51111111-1111-4111-8111-111111111111")

    assert admission.managed is False
    assert admission.mode == "append"
    assert admission.intent is None


def test_unknown_destination_is_rejected_before_admission(contract):
    from hub.api_errors import APIError
    deps, graph = contract
    write = next(node for node in graph.nodes if node.id == "write")
    write.data["config"]["destId"] = "ghost-destination"

    with pytest.raises(APIError) as excinfo:
        _write_admission_for_graph(
            deps, graph, "write", "researcher", "71111111-1111-4111-8111-111111111111")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == APIErrorCode.INVALID_REQUEST
    assert "unknown destination" in str(excinfo.value.detail)


def test_unknown_destination_admission_api_returns_the_typed_envelope(contract, monkeypatch):
    deps, graph = contract
    next(node for node in graph.nodes if node.id == "write").data["config"]["destId"] = "ghost"
    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)

    response = TestClient(app).post("/api/run/write-admission", json={
        "graph": graph.model_dump(by_alias=True, mode="json"),
        "nodeId": "write",
        "submissionId": "72222222-2222-4222-8222-222222222222",
    })

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == APIErrorCode.INVALID_REQUEST
    assert body["retryable"] is False
    assert "unknown destination" in body["detail"]


def test_nonlocal_execution_transport_is_not_mislabeled_managed(contract):
    deps, graph = contract
    deps.runner = SimpleNamespace(supports_managed_local_write_intents=lambda: True)
    deps.pick_runner = lambda _plan, _uid: object()
    deps.runners = []
    deps.node_ir = {}

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "52222222-2222-4222-8222-222222222222")

    assert admission.managed is False
    assert admission.mode == "overwrite"
    assert admission.intent is None


def test_runner_without_typed_write_capability_is_not_mislabeled_managed(contract):
    deps, graph = contract
    deps.runner = object()
    deps.pick_runner = lambda _plan, _uid: deps.runner
    deps.runners = []
    deps.node_ir = {}

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "53333333-3333-4333-8333-333333333333")

    assert admission.managed is False
    assert admission.mode == "overwrite"
    assert admission.intent is None


def test_write_admission_api_returns_the_frozen_camel_case_contract(
        contract, monkeypatch):
    deps, graph = contract
    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)

    response = TestClient(app).post("/api/run/write-admission", json={
        "graph": graph.model_dump(by_alias=True, mode="json"),
        "nodeId": "write",
        "submissionId": "61111111-1111-4111-8111-111111111111",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["managed"] is True and body["mode"] == "create"
    assert body["intent"]["mode"] == "create"
    assert body["intent"]["destination"]["logicalUri"] == body["destination"]
    assert body["intent"]["expectedSchema"] == body["expectedSchema"]


def test_local_runner_consumes_frozen_intent_and_publishes_receipt(
        contract, monkeypatch):
    from hub.compiler import compile_plan
    from hub.plugins.runner import LocalRunner

    deps, base_graph = contract
    node_specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    runner = LocalRunner(
        deps.resolve_adapter, deps.registry, deps.catalog, deps.workspace,
        node_specs=node_specs, storage=deps.storage)

    def execute(graph, admission, submission):
        assert admission.intent is not None
        _inject_write_intent(graph, "write", admission.intent)
        run_id = metadb.local_run_submission_id(
            "researcher", graph.id, submission)
        started = runner.run(
            compile_plan(graph, "write", deps.registry, node_specs),
            graph, "write", "local", run_id=run_id)
        for _ in range(200):
            status = runner.status(started.run_id)
            if status.status in ("done", "failed", "cancelled"):
                return status
            time.sleep(0.01)
        raise AssertionError("managed local write did not finish")

    create_submission = "71111111-1111-4111-8111-111111111111"
    create_graph = base_graph.model_copy(deep=True)
    create = _write_admission_for_graph(
        deps, create_graph, "write", "researcher", create_submission)
    created = execute(create_graph, create, create_submission)
    assert created.status == "done", created.error
    first = created.outputs[0].write_receipt
    assert first is not None and first.parent_head is None

    replace_submission = "72222222-2222-4222-8222-222222222222"
    replace_graph = base_graph.model_copy(deep=True)
    replace = _write_admission_for_graph(
        deps, replace_graph, "write", "researcher", replace_submission)
    assert replace.expected_head is not None
    monkeypatch.setattr(
        metadb, "catalog_managed_local_write_head",
        lambda _uri: pytest.fail("execution re-resolved a newer destination head"))
    replaced = execute(replace_graph, replace, replace_submission)

    assert replaced.status == "done", replaced.error
    second = replaced.outputs[0].write_receipt
    assert second is not None
    assert second.parent_head is not None
    assert second.parent_head.revision_id == first.revision_id
    assert second.revision_id != first.revision_id
    assert replaced.outputs[0].uri == second.publication.artifact_uri
    assert replaced.outputs[0].version == second.publication.catalog_version


def test_precise_library_integer_schema_matches_managed_write_runtime(contract):
    from hub.compiler import compile_plan
    from hub.plugins.runner import LocalRunner

    deps, base_graph = contract
    source_uri = next(node for node in base_graph.nodes if node.id == "source").data["config"]["uri"]

    def processor_factory(_params):
        def add_integer_widths(row):
            return {
                "signed_value": pa.scalar(row["value"], type=pa.int32()),
                "unsigned_value": pa.scalar((1 << 63) + row["value"], type=pa.uint64()),
            }

        return add_integer_widths

    deps.registry.register(RegisteredProcessor(
        id="test.precise-integers",
        version="v1",
        title="Precise integers",
        mode="map",
        input_schema=[ColumnSchema(name="value", type="int")],
        output_schema=[
            ColumnSchema(name="signed_value", type="int32"),
            ColumnSchema(name="unsigned_value", type="uint64"),
        ],
        fn_factory=processor_factory,
    ))
    graph = Graph.model_validate({
        "id": "precise-integer-write-admission",
        "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": source_uri}}},
            {"id": "transform", "type": "transform", "data": {"config": {
                "source": "library",
                "processor": "test.precise-integers",
                "version": "v1",
                "mode": "map",
                "outputSchema": [
                    {"name": "signed_value", "type": "int32"},
                    {"name": "unsigned_value", "type": "uint64"},
                ],
            }}},
            {"id": "write", "type": "write", "data": {"title": "precise-output", "config": {
                "filename": "precise-output.parquet",
                "writeMode": "overwrite",
            }}},
        ],
        "edges": [
            {"id": "source-transform", "source": "source", "target": "transform"},
            {"id": "transform-write", "source": "transform", "target": "write"},
        ],
    })
    submission = "73333333-3333-4333-8333-333333333333"
    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", submission)
    assert admission.intent is not None
    assert [
        (column.name, column.type, column.physical_type, column.nullable)
        for column in admission.expected_schema
    ] == [
        ("signed_value", "int", "INTEGER", None),
        ("unsigned_value", "int", "UBIGINT", None),
    ]

    _inject_write_intent(graph, "write", admission.intent)
    node_specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    runner = LocalRunner(
        deps.resolve_adapter, deps.registry, deps.catalog, deps.workspace,
        node_specs=node_specs, storage=deps.storage)
    run_id = metadb.local_run_submission_id("researcher", graph.id, submission)
    started = runner.run(
        compile_plan(graph, "write", deps.registry, node_specs),
        graph, "write", "local", run_id=run_id)
    deadline = time.monotonic() + 2
    status = started
    while status.status not in ("done", "failed", "cancelled"):
        assert time.monotonic() < deadline, status
        time.sleep(0.01)
        status = runner.status(run_id)
    assert runner.wait_for_worker(run_id, timeout=2)
    assert status.status == "done", status.error
    receipt = status.outputs[0].write_receipt
    assert receipt is not None
    published = pq.read_table(receipt.publication.artifact_uri)
    assert published.schema.types == [pa.int32(), pa.uint64()]
    assert published.to_pylist() == [
        {"signed_value": 1, "unsigned_value": (1 << 63) + 1},
        {"signed_value": 2, "unsigned_value": (1 << 63) + 2},
    ]


def test_lance_append_admission_freezes_registered_exact_head_without_allocation(
        lance_contract):
    _lance, deps, graph, table = lance_contract
    before = {
        path: set(os.listdir(os.path.join(table.uri, path)))
        for path in ("data", "_transactions")
    }

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "81111111-1111-4111-8111-111111111111")

    assert admission.managed is True
    assert admission.mode == "append"
    assert admission.provider == "managed-local-lance"
    assert admission.expected_head is not None
    assert admission.expected_head.revision_id == "1"
    assert admission.intent is not None
    assert admission.intent.schema_drift is None
    assert admission.intent.destination.logical_uri == table.uri
    assert admission.intent.destination.dataset_id == admission.expected_head.dataset_id
    assert [(column.name, column.type) for column in admission.expected_schema] == [("value", "int")]
    assert before == {
        path: set(os.listdir(os.path.join(table.uri, path)))
        for path in ("data", "_transactions")
    }


def test_lance_append_admission_blocks_incompatible_schema_before_publication(
        lance_contract):
    _lance, deps, graph, table = lance_contract
    source = next(node for node in graph.nodes if node.id == "source")
    incompatible = os.path.join(deps.workspace, "incompatible.parquet")
    pq.write_table(pa.table({"other": [2]}), incompatible)
    source.data["config"]["uri"] = incompatible
    before = set(os.listdir(os.path.join(table.uri, "data")))

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "82222222-2222-4222-8222-222222222222")

    assert admission.managed is True
    assert admission.intent is None
    assert admission.expected_head is not None
    assert admission.blocker == "input schema is incompatible with the existing Lance destination"
    assert set(os.listdir(os.path.join(table.uri, "data"))) == before


def test_lance_append_admission_preserves_physical_integer_width(lance_contract):
    _lance, deps, graph, table = lance_contract
    source = next(node for node in graph.nodes if node.id == "source")
    narrow = os.path.join(deps.workspace, "narrow.parquet")
    pq.write_table(pa.table({"value": pa.array([2], type=pa.int32())}), narrow)
    source.data["config"]["uri"] = narrow

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "82333333-3333-4233-8233-333333333333")

    assert admission.intent is None
    assert admission.expected_head is not None
    assert admission.blocker == "input schema is incompatible with the existing Lance destination"
    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == "1"


def test_lance_append_admission_accepts_equivalent_arrow_decimal_spelling(
        lance_contract):
    lance, deps, graph, table = lance_contract
    decimal_type = pa.decimal128(21, 1)
    source = next(node for node in graph.nodes if node.id == "source")
    pq.write_table(
        pa.table({"amount": pa.array([Decimal("1.0"), Decimal("2.0")], type=decimal_type)}),
        source.data["config"]["uri"],
    )
    lance.write_dataset(
        pa.table({"amount": pa.array([Decimal("3.0")], type=decimal_type)}),
        table.uri,
        mode="overwrite",
    )

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "82444444-4444-4244-8244-444444444444")

    assert admission.blocker is None
    assert admission.intent is not None
    assert admission.expected_head is not None
    assert admission.expected_schema[0].physical_type == "DECIMAL(21,1)"


def test_lance_append_unknown_schema_keeps_truthful_mode(lance_contract, monkeypatch):
    _lance, deps, graph, _table = lance_contract
    monkeypatch.setattr(run_routes, "schema_for_graph", lambda *_args, **_kwargs: {})

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "82888888-8888-4888-8888-888888888888")

    assert admission.managed is True
    assert admission.mode == "append"
    assert admission.provider == "managed-local-lance"
    assert admission.intent is None
    assert admission.blocker is not None


def test_lance_append_admission_rejects_stale_head_and_one_of_two_admissions(
        lance_contract):
    lance, deps, graph, table = lance_contract
    stale = _write_admission_for_graph(
        deps, graph, "write", "researcher", "83333333-3333-4333-8333-333333333333")
    competing = _write_admission_for_graph(
        deps, graph, "write", "researcher", "84444444-4444-4444-8444-444444444444")
    assert stale.expected_head == competing.expected_head
    lance.write_dataset(pa.table({"value": [9]}), table.uri, mode="append")
    before_version = LanceAdapter().resolve_revision(table.uri)["revision_id"]

    with pytest.raises(HTTPException, match="stale") as exc:
        _write_admission_for_graph(
            deps, graph, "write", "researcher",
            "83333333-3333-4333-8333-333333333333", supplied=stale.intent)

    assert exc.value.status_code == 409
    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == before_version


def test_lance_append_requires_registration_and_in_process_runner(lance_contract):
    _lance, deps, graph, _table = lance_contract
    unsupported = object()
    deps.pick_runner = lambda _plan, _uid: unsupported
    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "85555555-5555-4555-8555-555555555555")
    assert admission.managed is False
    assert admission.mode == "append"
    assert admission.intent is None

    deps.pick_runner = lambda _plan, _uid: deps.runner
    write = next(node for node in graph.nodes if node.id == "write")
    write.data["config"]["filename"] = "missing.lance"
    missing = _write_admission_for_graph(
        deps, graph, "write", "researcher", "86666666-6666-4666-8666-666666666666")
    assert missing.managed is False
    assert missing.mode == "append"
    assert missing.intent is None


def test_controller_owned_lance_append_is_not_admitted(lance_contract):
    _lance, deps, graph, table = lance_contract
    calls = []

    class Controller:
        def plan_for_run(self, _graph, _target, *, sizes):
            calls.append(sizes)
            return [object(), object()]

    deps.controller = Controller()
    before_version = LanceAdapter().resolve_revision(table.uri)["revision_id"]
    before_rows = LanceAdapter()._dataset(table.uri).count_rows()

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "86888888-8888-4888-8888-888888888888")

    assert calls
    assert admission.managed is False
    assert admission.mode == "append"
    assert admission.intent is None
    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == before_version
    assert LanceAdapter()._dataset(table.uri).count_rows() == before_rows


def test_lance_append_dispatch_rejects_a_late_controller_owner(
        lance_contract, monkeypatch):
    _lance, deps, base_graph, table = lance_contract

    class Controller:
        def __init__(self):
            self.plan_calls = 0
            self.run_called = False

        def plan_for_run(self, _graph, _target, *, sizes):
            assert isinstance(sizes, dict)
            self.plan_calls += 1
            return [] if self.plan_calls == 1 else [object(), object()]

        def run(self, *_args, **_kwargs):
            self.run_called = True
            pytest.fail("controller allocated work for a managed-local write")

    controller = Controller()
    deps.controller = controller
    monkeypatch.setattr(run_routes.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(
        run_routes, "_require_destination_credential_preflight", lambda *_args: None)
    before_version = LanceAdapter().resolve_revision(table.uri)["revision_id"]
    before_rows = LanceAdapter()._dataset(table.uri).count_rows()

    with pytest.raises(HTTPException, match="selected execution owner") as caught:
        run_routes.start_run(
            deps, base_graph.model_copy(deep=True), "write", "researcher", confirmed=True,
            submission_id="86999999-9999-4999-8999-999999999999",
        )

    assert caught.value.status_code == 409
    assert controller.plan_calls == 2
    assert controller.run_called is False
    assert LanceAdapter().resolve_revision(table.uri)["revision_id"] == before_version
    assert LanceAdapter()._dataset(table.uri).count_rows() == before_rows


def test_local_runner_consumes_lance_append_intent_and_recovers_exact_receipt(
        lance_contract):
    from hub.compiler import compile_plan
    from hub.plugins.runner import LocalRunner

    lance, deps, base_graph, table = lance_contract
    node_specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    runner = LocalRunner(
        deps.resolve_adapter, deps.registry, deps.catalog, deps.workspace,
        node_specs=node_specs, storage=deps.storage)
    submission = "87777777-7777-4777-8777-777777777777"
    graph = base_graph.model_copy(deep=True)
    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", submission)
    assert admission.intent is not None
    _inject_write_intent(graph, "write", admission.intent)
    run_id = metadb.local_run_submission_id("researcher", graph.id, submission)
    started = runner.run(
        compile_plan(graph, "write", deps.registry, node_specs),
        graph, "write", "local", run_id=run_id)
    for _ in range(400):
        status = runner.status(started.run_id)
        if status.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("managed local Lance append did not finish")

    assert status.status == "done", status.error
    receipt = status.outputs[0].write_receipt
    assert receipt is not None
    assert receipt.parent_head == admission.expected_head
    assert receipt.revision_id == "2"
    assert receipt.publication.provider == "managed-local-lance"
    assert receipt.publication.backend_version == lance.__version__
    assert LanceAdapter().open_revision(table.uri, receipt.revision_id).fetchall() == [
        (1,), (2,), (3,)]

    recovered = _write_admission_for_graph(
        deps, base_graph.model_copy(deep=True), "write", "researcher", submission,
        supplied=admission.intent)
    assert recovered.recovered_receipt == receipt
    lance.write_dataset(pa.table({"value": [99]}), table.uri, mode="append")
    assert LanceAdapter().open_revision(table.uri, receipt.revision_id).fetchall() == [
        (1,), (2,), (3,)]
