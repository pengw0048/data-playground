"""Product admission contract for default-local create and replace writes."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hub import metadb
from hub.models import Graph
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.plugins.adapters import DuckDBAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.plugins.processors import InMemoryProcessorRegistry
from hub.routers.runs import _write_admission_for_graph
from hub.routers.runs import _inject_write_intent
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
        registry=InMemoryProcessorRegistry(), node_builders={},
        node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS},
    )
    try:
        yield deps, graph
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


def test_preflight_is_metadata_only_and_derives_create_then_replace(contract):
    deps, graph = contract
    before = set(os.listdir(deps.storage.result_root))
    create = _write_admission_for_graph(
        deps, graph, "write", "researcher", "11111111-1111-4111-8111-111111111111")

    assert create.managed is True
    assert create.mode == "create"
    assert create.expected_head is None
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


def test_nonlocal_execution_transport_is_not_mislabeled_managed(contract):
    deps, graph = contract
    deps.runner = object()
    deps.pick_runner = lambda _plan, _uid: object()
    deps.node_ir = {}

    admission = _write_admission_for_graph(
        deps, graph, "write", "researcher", "52222222-2222-4222-8222-222222222222")

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
