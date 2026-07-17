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
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.plugins.processors import InMemoryProcessorRegistry
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
        registry=InMemoryProcessorRegistry(), node_builders={},
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
        registry=InMemoryProcessorRegistry(), node_builders={},
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
