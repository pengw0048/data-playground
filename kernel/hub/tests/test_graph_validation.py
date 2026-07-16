"""Regression tests for the authoritative server-side graph structure validator."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hub import graph as graph_mod
from hub.executors.engine import BuildEngine, NotPreviewable
from hub.main import app
from hub.models import Graph, GraphEdge, GraphNode
from hub.nodespecs import BUILTIN_NODE_SPECS, NodeSpec, ParamSpec, PortSpec

SPECS = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
client = TestClient(app)


def _node(node_id: str, kind: str, config: dict | None = None) -> GraphNode:
    return GraphNode(id=node_id, type=kind, data={"config": config or {}})


def _edge(edge_id: str, source: str, target: str, source_handle: str | None = None,
          target_handle: str | None = None) -> GraphEdge:
    return GraphEdge(id=edge_id, source=source, target=target,
                     source_handle=source_handle, target_handle=target_handle)


def _graph(nodes: list[GraphNode], edges: list[GraphEdge]) -> Graph:
    return Graph(id="validation", nodes=nodes, edges=edges)


@pytest.mark.parametrize(("graph", "message"), [
    (_graph([_node("same", "source"), _node("same", "filter")], []), "duplicate node id 'same'"),
    (_graph([_node("s", "source"), _node("f", "filter")],
            [_edge("same", "s", "f"), _edge("same", "s", "f")]), "duplicate edge id 'same'"),
    (_graph([_node("f", "filter")], [_edge("e", "missing", "f")]), "missing source node 'missing'"),
    (_graph([_node("s", "source")], [_edge("e", "s", "missing")]), "missing target node 'missing'"),
    (_graph([_node("s", "source"), _node("f", "filter")],
            [_edge("e", "s", "f", source_handle="bogus")]), "unknown source handle 'bogus'"),
    (_graph([_node("s", "source"), _node("j", "join")],
            [_edge("e", "s", "j", target_handle="bogus")]), "unknown target handle 'bogus'"),
])
def test_structural_errors_reject_ambiguous_graphs(graph: Graph, message: str):
    assert any(message in error for error in graph_mod.structural_errors(graph, SPECS))


def test_default_handles_are_normalized_before_single_input_fan_in_check():
    graph = _graph(
        [_node("a", "source"), _node("b", "source"), _node("f", "filter")],
        [_edge("ea", "a", "f"), _edge("eb", "b", "f", target_handle="in")],
    )
    errors = graph_mod.structural_errors(graph, SPECS)
    assert errors == ["input 'in' on node 'f' has multiple incoming edges ('ea' and 'eb')"]


@pytest.mark.parametrize(("config", "message"), [
    ({"count": "12abc"}, "complete safe integer"),
    ({"count": True}, "complete safe integer"),
    ({"count": 2**53}, "complete safe integer"),
    ({"count": 1, "ratio": "1.2"}, "finite number"),
    ({"count": 1, "ratio": float("nan")}, "finite number"),
    ({"count": 1, "ratio": float("inf")}, "finite number"),
    ({"count": 1, "ratio": 10**400}, "finite number"),
])
def test_numeric_plugin_parameters_require_declared_json_number_types(config: dict, message: str):
    spec = NodeSpec(
        kind="numeric-plugin", title="numeric plugin", category="compute",
        outputs=[PortSpec(id="out")],
        params=[ParamSpec(name="count", type="int", required=True),
                ParamSpec(name="ratio", type="float", default=0.5)],
    )
    graph = _graph([_node("numeric", spec.kind, config)], [])
    assert any(message in error for error in graph_mod.parameter_errors(graph, {spec.kind: spec}))


@pytest.mark.parametrize("config", [
    {"count": 0}, {"count": -7, "ratio": 1}, {"count": 42, "ratio": -1.25e2},
])
def test_numeric_plugin_parameters_accept_zero_signs_and_finite_exponents(config: dict):
    spec = NodeSpec(
        kind="numeric-plugin", title="numeric plugin", category="compute",
        outputs=[PortSpec(id="out")],
        params=[ParamSpec(name="count", type="int", required=True),
                ParamSpec(name="ratio", type="float", default=0.5)],
    )
    assert graph_mod.parameter_errors(_graph([_node("numeric", spec.kind, config)], []), {spec.kind: spec}) == []


def test_multi_inputs_and_dynamic_section_outputs_preserve_valid_contracts():
    # union and SQL both intentionally accept many edges on their single logical input. SQL exposes
    # those relations as input/input2/…; its NodeSpec must describe what the executor already supports.
    for kind in ("union", "sql"):
        graph = _graph(
            [_node("a", "source"), _node("b", "source"), _node("m", kind)],
            [_edge("ea", "a", "m"), _edge("eb", "b", "m", target_handle="in")],
        )
        assert graph_mod.structural_errors(graph, SPECS) == []

    # Section declarations are strict; there is no implicit dynamic namespace or legacy fallback.
    strict = _graph([_node("sec", "section", {"outputs": ["low"]}), _node("f", "filter")],
                    [_edge("e", "sec", "f", source_handle="high")])
    assert "unknown source handle 'high'" in graph_mod.structural_errors(strict, SPECS)[0]
    legacy = _graph([_node("sec", "section"), _node("f", "filter")],
                    [_edge("e", "sec", "f", source_handle="high")])
    assert "unknown source handle 'high'" in graph_mod.structural_errors(legacy, SPECS)[0]


def test_multi_output_edges_require_an_explicit_source_handle():
    graph = _graph(
        [_node("check", "assert"), _node("f", "filter")],
        [_edge("e", "check", "f")],
    )
    assert graph_mod.structural_errors(graph, SPECS) == [
        "edge 'e' must identify a source handle on multi-output node 'check'"
    ]


@pytest.mark.parametrize(("outputs", "message"), [
    ([], "at least one output port"),
    (None, "output declaration must be a list"),
    ([None], "port ids must be strings"),
    ([" out"], "surrounding whitespace"),
    (["out", "out"], "duplicate output port 'out'"),
    (["x" * 129], "exceeds 128 characters"),
    ([f"p{i}" for i in range(65)], "supported maximum is 64"),
])
def test_section_output_declarations_are_canonical_and_bounded(outputs, message: str):
    graph = _graph([_node("sec", "section", {"outputs": outputs})], [])
    assert any(message in error for error in graph_mod.structural_errors(graph, SPECS))


def test_non_section_outputs_config_cannot_override_node_spec_ports():
    graph = _graph(
        [_node("s", "source", {"outputs": ["injected"]}), _node("f", "filter")],
        [_edge("valid", "s", "f", source_handle="out")],
    )
    assert graph_mod.structural_errors(graph, SPECS) == []
    ports = graph_mod.effective_output_ports(graph, "s", SPECS)
    assert [port.id for port in ports] == ["out"]


def test_structural_output_validation_does_not_rescan_the_graph(monkeypatch):
    graph = _graph([_node(f"s{i}", "source") for i in range(100)], [])
    calls = 0
    original = graph_mod.node_map

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(graph_mod, "node_map", counted)
    assert graph_mod.structural_errors(graph, SPECS) == []
    assert calls == 0


def test_build_engine_reuses_one_node_map_for_output_validation_and_selection(monkeypatch):
    graph = _graph([_node("s", "source")], [])
    calls = 0
    original = graph_mod.node_map

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(graph_mod, "node_map", counted)
    engine = BuildEngine(graph, None, None, node_specs=SPECS)
    relation = object()
    monkeypatch.setattr(engine, "_warm_or_lower", lambda _node_id: relation)
    assert engine.relation("s") is relation
    assert engine.relation("s", "out") is relation
    assert calls == 1


@pytest.mark.parametrize(("runtime", "message"), [
    ({"left": object()}, "missing ['right']"),
    ({"left": object(), "right": object(), "extra": object()}, "unexpected ['extra']"),
])
def test_build_engine_rejects_runtime_outputs_that_drift_from_the_declaration(
        monkeypatch, runtime: dict, message: str):
    graph = _graph([_node("s", "section", {"outputs": ["left", "right"]})], [])
    engine = BuildEngine(graph, None, None, node_specs=SPECS)
    monkeypatch.setattr(engine, "_warm_or_lower", lambda _node_id: runtime)

    with pytest.raises(RuntimeError, match=message.replace("[", r"\[").replace("]", r"\]")):
        engine.relation("s", "left")


def test_build_engine_relations_projects_runtime_outputs_in_declaration_order(monkeypatch):
    graph = _graph([_node("gate", "assert")], [])
    engine = BuildEngine(graph, None, None, node_specs=SPECS)
    passing, violations = object(), object()
    # The built-in assert currently constructs violations first. Runtime dict insertion order must
    # never leak into materialization/history; NodeSpec declares pass before out.
    monkeypatch.setattr(
        engine, "_warm_or_lower",
        lambda _node_id: {"out": violations, "pass": passing},
    )

    relations = engine.relations("gate")
    assert list(relations) == ["pass", "out"]
    assert list(relations.values()) == [passing, violations]
    with pytest.raises(NotPreviewable, match="select an output port"):
        engine.relation("gate")


def test_structural_errors_reject_unknown_requested_target():
    graph = _graph([_node("s", "source")], [])
    assert graph_mod.structural_errors(graph, SPECS, "missing") == ["target node 'missing' does not exist"]


@pytest.mark.parametrize("kind", ["notebook", "loop", "variable", "opaque"])
def test_obsolete_literal_node_kinds_are_unknown(kind: str):
    graph = _graph([_node("old", kind)], [])
    assert graph_mod.unknown_kinds(graph, SPECS) == [("old", kind)]

    payload = graph.model_dump(by_alias=True)
    compiled = client.post("/api/graph/compile", json={"graph": payload, "targetNodeId": "old"})
    assert compiled.status_code == 200
    assert f"unknown node kind '{kind}'" in compiled.json()["error"]

    preview = client.post("/api/run/preview", json={"graph": payload, "nodeId": "old", "k": 5})
    assert preview.status_code == 400
    assert f"unknown node kind '{kind}'" in preview.text


def _wire(node_id: str, kind: str) -> dict:
    return {"id": node_id, "type": kind, "position": {"x": 0, "y": 0}, "data": {"config": {}}}


def _malformed_body() -> dict:
    return {
        "id": "api-validation", "version": 1,
        "nodes": [_wire("f", "filter")],
        "edges": [{"id": "broken", "source": "missing", "target": "f", "data": {"wire": "dataset"}}],
    }


def test_every_graph_consuming_api_rejects_the_same_malformed_graph_without_500():
    graph = _malformed_body()
    compiled = client.post("/api/graph/compile", json={"graph": graph, "targetNodeId": "f"})
    assert compiled.status_code == 200
    assert "missing source node 'missing'" in compiled.json()["error"]

    requests = [
        ("/api/run/preview", {"graph": graph, "nodeId": "f"}),
        ("/api/run/profile", {"graph": graph, "nodeId": "f"}),
        ("/api/graph/schema", {"graph": graph, "targetNodeId": "f"}),
        ("/api/graph/estimate", {"graph": graph, "targetNodeId": "f"}),
        ("/api/graph/plan", {"graph": graph, "targetNodeId": "f"}),
        ("/api/graph/join-analysis", {"graph": graph, "targetNodeId": "f"}),
        ("/api/run/estimate", {"graph": graph, "targetNodeId": "f"}),
        ("/api/run", {"graph": graph, "targetNodeId": "f", "confirmed": True}),
    ]
    for path, body in requests:
        response = client.post(path, json=body)
        assert response.status_code == 400, (path, response.status_code, response.text)
        assert "missing source node 'missing'" in response.text


def test_unknown_target_is_an_error_not_an_empty_plan_or_success():
    graph = {"id": "api-target", "version": 1, "nodes": [_wire("s", "source")], "edges": []}
    compiled = client.post("/api/graph/compile", json={"graph": graph, "targetNodeId": "missing"})
    assert compiled.status_code == 200 and "target node 'missing'" in compiled.json()["error"]
    for path, body in [
        ("/api/run/preview", {"graph": graph, "nodeId": "missing"}),
        ("/api/graph/estimate", {"graph": graph, "targetNodeId": "missing"}),
        ("/api/graph/plan", {"graph": graph, "targetNodeId": "missing"}),
        ("/api/run", {"graph": graph, "targetNodeId": "missing", "confirmed": True}),
    ]:
        response = client.post(path, json=body)
        assert response.status_code == 400, (path, response.status_code, response.text)
        assert "target node 'missing'" in response.text


def test_run_all_with_multiple_writes_is_a_stable_preflight_error():
    graph = {
        "id": "ambiguous-run-all", "version": 1,
        "nodes": [
            {**_wire("left", "write"), "data": {"config": {"name": "left"}}},
            {**_wire("right", "write"), "data": {"config": {"name": "right"}}},
        ],
        "edges": [],
    }
    for path, body in [
        ("/api/run/estimate", {"graph": graph}),
        ("/api/run", {"graph": graph, "confirmed": True}),
    ]:
        response = client.post(path, json=body)
        assert response.status_code == 400, (path, response.text)
        assert response.json()["code"] == "multi_output_unsupported"
        assert "multiple write outputs" in response.json()["detail"]


def test_inspection_port_failures_have_stable_machine_codes():
    graph = {
        "id": "inspection-port-errors", "version": 1,
        "nodes": [_wire("quality", "assert")], "edges": [],
    }
    for path in ("/api/run/preview", "/api/run/profile"):
        missing = client.post(path, json={"graph": graph, "nodeId": "quality"})
        assert missing.status_code == 400
        assert missing.json()["code"] == "output_port_required"
        assert missing.json()["retryable"] is False

        unknown = client.post(
            path, json={"graph": graph, "nodeId": "quality", "portId": "missing"})
        assert unknown.status_code == 400
        assert unknown.json()["code"] == "output_port_not_found"
        assert unknown.json()["retryable"] is False
