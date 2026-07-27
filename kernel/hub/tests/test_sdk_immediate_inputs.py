"""Public plugin builder context conformance for bounded immediate-input inspection."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from hub import db, graph as graph_mod, metadb, workspace_providers
from hub.executors.engine import BuildEngine
from hub.models import Graph, GraphNode, ResourceSpec
from hub.nodespecs import BUILTIN_NODE_SPECS, NodeSpec, PortSpec
from hub.planner import Region
from hub.sdk import UnsupportedUpstreamError, ctx


class _Adapter:
    name = "fixture"

    def scan(self, _uri, **_kwargs):
        return db.conn().sql("SELECT 1 AS id, 4 AS signal")

    def open_revision(self, _uri, _revision_id):
        return db.conn().sql("SELECT 1 AS id, 4 AS signal")


def _node(node_id: str, kind: str, config: dict | None = None) -> dict:
    return {"id": node_id, "type": kind, "position": {"x": 0, "y": 0},
            "data": {"config": config or {}}}


def _engine(deps, graph: Graph) -> BuildEngine:
    return BuildEngine(
        graph, lambda _uri: _Adapter(), deps.registry, full=True,
        node_builders=deps.node_builders, node_specs=deps.node_specs,
    )


def _node_specs(*specs: NodeSpec) -> dict[str, NodeSpec]:
    return {spec.kind: spec for spec in [*BUILTIN_NODE_SPECS, *specs]}


def _fixture_deps(tmp_path):
    import shutil
    from pathlib import Path

    from hub import metadb
    from hub.deps import Deps

    source = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_sidecar_fixture"
    workspace = tmp_path / "workspace"
    (workspace / "plugins").mkdir(parents=True)
    shutil.copytree(source, workspace / "plugins" / "dp_sidecar_fixture")
    metadb.init_db()
    return Deps(str(workspace), str(tmp_path / "data"))


def test_sidecar_fixture_checks_direct_single_source_and_identity_before_its_build(tmp_path, monkeypatch):
    deps = _fixture_deps(tmp_path)
    exact = {"kind": "exact", "datasetId": "base-dataset", "revisionId": "r1"}
    monkeypatch.setattr(
        metadb, "catalog_revision_binding_for_uri", lambda _uri: {"dataset_id": "base-dataset"})
    config = {
        "identity": "id", "value": "signal", "output": "derived",
        "sourceDatasetId": "base-dataset",
    }
    sql_calls: list[str] = []
    real_sql = ctx.sql

    def record_sql(*args, **kwargs):
        sql_calls.append("sidecar")
        return real_sql(*args, **kwargs)

    monkeypatch.setattr(ctx, "sql", record_sql)

    correct = Graph(**{"id": "correct", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://base", "datasetRef": exact}),
        _node("sidecar", "derive_sidecar_column", config),
    ], "edges": [{"id": "edge", "source": "source", "target": "sidecar",
                    "data": {"wire": "dataset"}}]})
    with db.run_scope():
        assert _engine(deps, correct).relation("sidecar").fetchall() == [(1, 8.0)]
    assert sql_calls == ["sidecar"]
    sql_calls.clear()

    multi = correct.model_copy(deep=True)
    multi.nodes.append(GraphNode.model_validate(_node("other", "source", {
        "uri": "fixture://other", "datasetRef": exact,
    })))
    multi.edges.append(type(multi.edges[0]).model_validate({
        "id": "second", "source": "other", "target": "sidecar", "data": {"wire": "dataset"},
    }))
    assert graph_mod.structural_errors(multi, deps.node_specs) == [
        "input 'in' on node 'sidecar' has multiple incoming edges ('edge' and 'second')"
    ]
    assert not sql_calls

    wrong_kind = Graph(**{"id": "wrong-kind", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://base", "datasetRef": exact}),
        _node("filter", "filter", {"predicate": "id > 0"}),
        _node("sidecar", "derive_sidecar_column", config),
    ], "edges": [
        {"id": "first", "source": "source", "target": "filter", "data": {"wire": "dataset"}},
        {"id": "second", "source": "filter", "target": "sidecar", "data": {"wire": "dataset"}},
    ]})
    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="direct Source"):
        _engine(deps, wrong_kind).relation("sidecar")
    assert not sql_calls

    ambiguous = Graph(**{"id": "ambiguous", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://unbound"}),
        _node("sidecar", "derive_sidecar_column", config),
    ], "edges": [{"id": "edge", "source": "source", "target": "sidecar",
                    "data": {"wire": "dataset"}}]})
    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="proved dataset identity"):
        _engine(deps, ambiguous).relation("sidecar")
    assert not sql_calls


@pytest.mark.parametrize(
    ("port_ids", "expected_port"),
    [
        (["fallback", "in"], "in"),
        (["primary", "secondary"], "primary"),
    ],
)
def test_omitted_target_handle_uses_core_default_port_in_real_dispatch(port_ids, expected_port):
    kind = f"default-port-{expected_port}"
    spec = NodeSpec(
        kind=kind, title="default port fixture", category="compute",
        inputs=[PortSpec(id=port_id) for port_id in port_ids],
        outputs=[PortSpec(id="out")],
    )
    snapshots = []

    def build(engine, node, inputs):
        snapshots.append(ctx.immediate_inputs(engine, node))
        return inputs[0]

    graph = Graph(**{"id": kind, "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://source"}),
        _node("consumer", kind),
    ], "edges": [
        {"id": "edge", "source": "source", "target": "consumer", "data": {"wire": "dataset"}},
    ]})
    specs = _node_specs(spec)
    assert graph_mod.structural_errors(graph, specs) == []
    with db.run_scope():
        relation = BuildEngine(
            graph, lambda _uri: _Adapter(), {}, full=True,
            node_builders={kind: build}, node_specs=specs,
        ).relation("consumer")
        assert relation.fetchall() == [(1, 4)]

    assert len(snapshots) == 1
    assert snapshots[0].port(expected_port).count == 1
    assert all(
        port.count == (1 if port.id == expected_port else 0)
        for port in snapshots[0].ports
    )


def test_legal_multi_port_builder_rejects_multiple_inputs_before_work():
    kind = "guarded-multi-input"
    spec = NodeSpec(
        kind=kind, title="guarded multi input", category="compute",
        inputs=[PortSpec(id="in", multi=True)],
        outputs=[PortSpec(id="out")],
    )
    work_calls: list[str] = []

    def build(engine, node, inputs):
        upstream = ctx.immediate_inputs(engine, node).port("in")
        if upstream.count != 1:
            raise UnsupportedUpstreamError("guarded fixture requires exactly one immediate input")
        work_calls.append("built")
        return inputs[0]

    graph = Graph(**{"id": kind, "version": 1, "nodes": [
        _node("first", "source", {"uri": "fixture://first"}),
        _node("second", "source", {"uri": "fixture://second"}),
        _node("consumer", kind),
    ], "edges": [
        {"id": "first-edge", "source": "first", "target": "consumer",
         "data": {"wire": "dataset"}},
        {"id": "second-edge", "source": "second", "target": "consumer",
         "targetHandle": "in", "data": {"wire": "dataset"}},
    ]})
    specs = _node_specs(spec)
    assert graph_mod.structural_errors(graph, specs) == []

    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="exactly one"):
        BuildEngine(
            graph, lambda _uri: _Adapter(), {}, full=True,
            node_builders={kind: build}, node_specs=specs,
        ).relation("consumer")
    assert not work_calls


def test_bound_section_input_does_not_claim_transitive_source_identity(tmp_path, monkeypatch):
    deps = _fixture_deps(tmp_path)
    exact = {"kind": "exact", "datasetId": "base-dataset", "revisionId": "r1"}
    monkeypatch.setattr(
        metadb, "catalog_revision_binding_for_uri", lambda _uri: {"dataset_id": "base-dataset"})
    source_graph = Graph(**{"id": "source", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://base", "datasetRef": exact}),
    ], "edges": []})
    section_graph = Graph(**{"id": "section-run", "version": 1, "nodes": [
        _node("sidecar", "derive_sidecar_column", {
            "identity": "id", "value": "signal", "output": "derived",
            "sourceDatasetId": "base-dataset",
        }),
    ], "edges": []})

    with db.run_scope():
        source_relation = _engine(deps, source_graph).relation("source")
        subengine = BuildEngine(
            section_graph, lambda _uri: _Adapter(), deps.registry, full=True,
            node_builders=deps.node_builders, node_specs=deps.node_specs,
            bound_inputs={"sidecar": source_relation},
        )
        sidecar = section_graph.nodes[0]
        assert ctx.immediate_inputs(subengine, sidecar).port("in").count == 0
        with pytest.raises(UnsupportedUpstreamError, match="exactly one"):
            subengine.relation("sidecar")


def test_generated_region_ref_is_not_reported_as_a_direct_source_after_worker_restore(
        tmp_path, monkeypatch):
    from hub.workload_env import prepare_workload_graph, restore_workload_graph

    deps = _fixture_deps(tmp_path)
    original = Graph(**{"id": "region-source-boundary", "version": 1, "nodes": [
        _node("source", "source", {"uri": "fixture://base"}),
        _node("filter", "filter", {"predicate": "id > 0"}),
        _node("sidecar", "derive_sidecar_column", {
            "identity": "id", "value": "signal", "output": "derived",
        }),
    ], "edges": [
        {"id": "source-filter", "source": "source", "target": "filter",
         "data": {"wire": "dataset"}},
        {"id": "filter-sidecar", "source": "filter", "target": "sidecar",
         "data": {"wire": "dataset"}},
    ]})
    final_region = Region(
        id="final", node_ids={"sidecar"}, output_node="sidecar",
        backend="default", worker=None, requires=ResourceSpec(),
        cut_inputs=[("filter", None, "sidecar", None)],
    )
    subgraph = deps.controller._subgraph(
        original, final_region, {"filter": "fixture://region-ref"})
    assert graph_mod.structural_errors(subgraph, deps.node_specs) == []
    generated = next(node for node in subgraph.nodes if node.type == "source")
    payload = prepare_workload_graph(subgraph, "sidecar", deps.registry)
    assert payload["_controllerGeneratedSourceIds"] == [generated.id]
    assert "_publication_source_uris" not in payload

    worker_graph = restore_workload_graph(json.loads(json.dumps(payload)), "sidecar")
    for malformed_ids in (
            generated.id, [generated.id, generated.id], ["sidecar"], ["missing"]):
        malformed = json.loads(json.dumps(payload))
        malformed["_controllerGeneratedSourceIds"] = malformed_ids
        with pytest.raises(RuntimeError, match="generated Source classification"):
            restore_workload_graph(malformed, "sidecar")

    public_graph = Graph.model_validate(payload)
    assert "_controllerGeneratedSourceIds" not in prepare_workload_graph(
        public_graph, "sidecar", deps.registry)

    sidecar = next(node for node in worker_graph.nodes if node.id == "sidecar")
    engine = _engine(deps, worker_graph)
    port = ctx.immediate_inputs(engine, sidecar).port("in")
    assert port.count == 1
    assert port.inputs[0].kind is None
    assert port.inputs[0].dataset is None
    assert port.inputs[0].provider is None

    sql_calls: list[str] = []
    monkeypatch.setattr(ctx, "sql", lambda *_args, **_kwargs: sql_calls.append("built"))
    with db.run_scope(), pytest.raises(UnsupportedUpstreamError, match="direct Source"):
        engine.relation("sidecar")
    assert not sql_calls


def test_immediate_inputs_projects_only_the_admitted_provider_binding(monkeypatch):
    mount_id = "fixture-mount"
    source_binding_id = "a" * 32
    provider_uri = workspace_providers.provider_dataset_uri(mount_id, source_binding_id)
    provider_identity = provider_uri.replace("workspace-provider://", "workspace-provider:")
    source = GraphNode.model_validate(_node("source", "source", {
        "uri": provider_uri,
        "datasetRef": {"kind": "exact", "datasetId": provider_identity, "revisionId": "r1"},
        "providerReadMode": "exact",
    }))
    consumer = GraphNode.model_validate(_node("consumer", "consumer"))
    engine = SimpleNamespace(
        graph=Graph(**{"id": "snapshot", "version": 1, "nodes": [source, consumer], "edges": [
            {"id": "edge", "source": "source", "target": "consumer", "data": {"wire": "dataset"}},
        ]}),
        _nodes={"source": source, "consumer": consumer},
        node_specs={"consumer": NodeSpec(kind="consumer", title="consumer", category="compute",
                                           inputs=[PortSpec(id="in")])},
    )
    monkeypatch.setattr(workspace_providers, "provider_dataset_identity", lambda _uri: provider_identity)
    monkeypatch.setattr(
        metadb, "workspace_provider_usable_dataset_for_source_binding",
        lambda **kwargs: {
            "mountId": mount_id, "sourceBindingId": source_binding_id,
            "provider": "fixture-provider", "providerDatasetId": "native-dataset",
        } if kwargs == {"mount_id": mount_id, "source_binding_id": source_binding_id} else None,
    )

    port = ctx.immediate_inputs(engine, consumer).port("in")
    assert port.count == 1
    assert port.inputs[0].kind == "source"
    assert port.inputs[0].dataset is not None
    assert port.inputs[0].dataset.dataset_id == provider_identity
    assert port.inputs[0].dataset.revision_id == "r1"
    assert port.inputs[0].provider is not None
    assert port.inputs[0].provider.provider == "fixture-provider"
    assert port.inputs[0].provider.mount_id == mount_id
    assert port.inputs[0].provider.provider_dataset_id == "native-dataset"
    assert port.inputs[0].provider.read_mode == "exact"
    assert port.inputs[0].provider.revision_id == "r1"
    assert set(vars(port.inputs[0].provider)) == {
        "provider", "mount_id", "provider_dataset_id", "read_mode", "revision_id",
    }

    source.data["config"].pop("datasetRef")
    source.data["config"]["providerReadMode"] = "mutable"
    current = ctx.immediate_inputs(engine, consumer).port("in").inputs[0]
    assert current.dataset is not None
    assert current.dataset.revision_id is None
    assert current.provider is not None
    assert current.provider.read_mode == "current"
    assert current.provider.revision_id is None

    source.data["config"]["datasetRef"] = {
        "kind": "exact", "datasetId": "placement-or-uri-is-not-an-identity", "revisionId": "r1",
    }
    source.data["config"]["providerReadMode"] = "exact"
    mismatched = ctx.immediate_inputs(engine, consumer).port("in").inputs[0]
    assert mismatched.dataset is None
    assert mismatched.provider is None


def test_immediate_inputs_omits_provider_projection_for_ambiguous_and_non_provider_sources(
        monkeypatch):
    mount_id = "fixture-mount"
    source_binding_id = "b" * 32
    provider_uri = workspace_providers.provider_dataset_uri(mount_id, source_binding_id)
    provider_identity = provider_uri.replace("workspace-provider://", "workspace-provider:")
    provider_source = GraphNode.model_validate(_node("provider-source", "source", {
        "uri": provider_uri,
        "datasetRef": {"kind": "exact", "datasetId": provider_identity, "revisionId": "r1"},
    }))
    plain_source = GraphNode.model_validate(_node("plain-source", "source", {
        "uri": "fixture://plain",
        "datasetRef": {"kind": "exact", "datasetId": "plain", "revisionId": "r1"},
    }))
    consumer = GraphNode.model_validate(_node("consumer", "consumer"))
    engine = SimpleNamespace(
        graph=Graph(**{"id": "snapshot", "version": 1,
                        "nodes": [provider_source, plain_source, consumer], "edges": [
                            {"id": "provider-edge", "source": "provider-source", "target": "consumer",
                             "data": {"wire": "dataset"}},
                            {"id": "plain-edge", "source": "plain-source", "target": "consumer",
                             "targetHandle": "in", "data": {"wire": "dataset"}},
                        ]}),
        _nodes={"provider-source": provider_source, "plain-source": plain_source,
                "consumer": consumer},
        node_specs={"consumer": NodeSpec(kind="consumer", title="consumer", category="compute",
                                           inputs=[PortSpec(id="in", multi=True)])},
    )
    monkeypatch.setattr(workspace_providers, "provider_dataset_identity", lambda _uri: provider_identity)

    inputs = ctx.immediate_inputs(engine, consumer).port("in").inputs
    assert inputs[0].dataset is not None
    assert inputs[0].provider is None  # no admitted read mode means the projection is ambiguous
    assert inputs[1].dataset is not None
    assert inputs[1].provider is None


def test_immediate_inputs_omits_detached_and_transformed_provider_sources(monkeypatch):
    mount_id = "fixture-mount"
    source_binding_id = "c" * 32
    provider_uri = workspace_providers.provider_dataset_uri(mount_id, source_binding_id)
    provider_identity = provider_uri.replace("workspace-provider://", "workspace-provider:")
    source = GraphNode.model_validate(_node("source", "source", {
        "uri": provider_uri,
        "datasetRef": {"kind": "exact", "datasetId": provider_identity, "revisionId": "r1"},
        "providerReadMode": "exact",
    }))
    consumer = GraphNode.model_validate(_node("consumer", "consumer"))

    def engine_for(nodes, edges):
        return SimpleNamespace(
            graph=Graph(**{"id": "snapshot", "version": 1, "nodes": nodes, "edges": edges}),
            _nodes={node.id: node for node in nodes},
            node_specs={"consumer": NodeSpec(kind="consumer", title="consumer", category="compute",
                                               inputs=[PortSpec(id="in")])},
        )

    direct = engine_for([source, consumer], [
        {"id": "edge", "source": "source", "target": "consumer", "data": {"wire": "dataset"}},
    ])
    monkeypatch.setattr(workspace_providers, "provider_dataset_identity", lambda _uri: provider_identity)
    monkeypatch.setattr(
        metadb, "workspace_provider_usable_dataset_for_source_binding", lambda **_kwargs: None)
    detached = ctx.immediate_inputs(direct, consumer).port("in").inputs[0]
    assert detached.dataset is not None
    assert detached.provider is None

    transform = GraphNode.model_validate(_node("filter", "filter", {"predicate": "id > 0"}))
    transformed = engine_for([source, transform, consumer], [
        {"id": "source-filter", "source": "source", "target": "filter",
         "data": {"wire": "dataset"}},
        {"id": "filter-consumer", "source": "filter", "target": "consumer",
         "data": {"wire": "dataset"}},
    ])
    input = ctx.immediate_inputs(transformed, consumer).port("in").inputs[0]
    assert input.kind == "filter"
    assert input.dataset is None
    assert input.provider is None


def test_provider_projection_fails_closed_on_real_sqlite_state_race(monkeypatch):
    metadb.init_db()
    token = uuid.uuid4().hex
    mount_id = f"sdk-provider-{token}"
    provider_dataset_id = f"native-dataset-{token}"
    cached = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="fixture-provider",
        container_id=metadb.LOCAL_WORKSPACE_ROOT_ID,
        provider_placement_id=f"placement-{token}",
        provider_dataset_id=provider_dataset_id,
        uri=f"fixture://{provider_dataset_id}",
        columns=[{"name": "id", "type": "int64"}],
        kind="dataset",
        name="Provider dataset",
    )
    source_binding = metadb.workspace_provider_source_binding(cached["bindingId"])
    assert source_binding is not None
    source_binding_id = source_binding["sourceBindingId"]
    provider_uri = workspace_providers.provider_dataset_uri(mount_id, source_binding_id)
    provider_identity = provider_uri.replace("workspace-provider://", "workspace-provider:")
    consumer = GraphNode.model_validate(_node("consumer", "consumer"))

    def snapshot(config):
        source = GraphNode.model_validate(_node("source", "source", config))
        engine = SimpleNamespace(
            graph=Graph(**{"id": "snapshot", "version": 1, "nodes": [source, consumer], "edges": [
                {"id": "edge", "source": "source", "target": "consumer",
                 "data": {"wire": "dataset"}},
            ]}),
            _nodes={"source": source, "consumer": consumer},
            node_specs={
                "consumer": NodeSpec(
                    kind="consumer", title="consumer", category="compute",
                    inputs=[PortSpec(id="in")],
                ),
            },
        )
        return ctx.immediate_inputs(engine, consumer).port("in").inputs[0]

    exact_config = {
        "uri": provider_uri,
        "datasetRef": {
            "kind": "exact", "datasetId": provider_identity, "revisionId": "revision-1",
        },
        "providerReadMode": "exact",
    }
    exact = snapshot(exact_config)
    assert exact.provider is not None
    assert exact.provider.provider == "fixture-provider"
    assert exact.provider.provider_dataset_id == provider_dataset_id
    assert exact.provider.read_mode == "exact"
    assert exact.provider.revision_id == "revision-1"

    current = snapshot({"uri": provider_uri, "providerReadMode": "mutable"})
    assert current.provider is not None
    assert current.provider.provider_dataset_id == provider_dataset_id
    assert current.provider.read_mode == "current"
    assert current.provider.revision_id is None

    real_identity = workspace_providers.provider_dataset_identity

    def degrade_after_identity(uri):
        identity = real_identity(uri)
        metadb.workspace_provider_mark_dataset(
            mount_id=mount_id,
            provider_dataset_id=provider_dataset_id,
            state="provider_error",
            error="provider returned invalid metadata",
        )
        return identity

    monkeypatch.setattr(
        workspace_providers, "provider_dataset_identity", degrade_after_identity)
    raced = snapshot(exact_config)
    assert raced.dataset is not None
    assert raced.dataset.dataset_id == provider_identity
    assert raced.provider is None
