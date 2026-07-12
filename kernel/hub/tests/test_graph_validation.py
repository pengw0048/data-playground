"""Regression tests for the authoritative server-side graph structure validator."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hub import graph as graph_mod
from hub.main import app
from hub.models import Graph, GraphEdge, GraphNode
from hub.nodespecs import BUILTIN_NODE_SPECS

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
    (_graph([_node("s", "notebook"), _node("f", "filter")],
            [_edge("e", "s", "f", source_handle="bogus")]), "unknown source handle 'bogus'"),
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


def test_multi_inputs_and_dynamic_section_outputs_preserve_valid_contracts():
    # union and SQL both intentionally accept many edges on their single logical input. SQL exposes
    # those relations as input/input2/…; its NodeSpec must describe what the executor already supports.
    for kind in ("union", "sql"):
        graph = _graph(
            [_node("a", "source"), _node("b", "source"), _node("m", kind)],
            [_edge("ea", "a", "m"), _edge("eb", "b", "m", target_handle="in")],
        )
        assert graph_mod.structural_errors(graph, SPECS) == []

    # New sections declare their named emits and are strict. Old saved sections predate config.outputs;
    # they retain their dynamic named-output behavior so this validation change does not break them.
    strict = _graph([_node("sec", "section", {"outputs": ["low"]}), _node("f", "filter")],
                    [_edge("e", "sec", "f", source_handle="high")])
    assert "unknown source handle 'high'" in graph_mod.structural_errors(strict, SPECS)[0]
    legacy = _graph([_node("sec", "section"), _node("f", "filter")],
                    [_edge("e", "sec", "f", source_handle="high")])
    assert graph_mod.structural_errors(legacy, SPECS) == []


def test_structural_errors_reject_unknown_requested_target():
    graph = _graph([_node("s", "source")], [])
    assert graph_mod.structural_errors(graph, SPECS, "missing") == ["target node 'missing' does not exist"]


def test_intrinsic_legacy_ports_are_structurally_and_wire_type_validated():
    graph = _graph([_node("n", "notebook"), _node("v", "variable")], [_edge("e", "n", "v")])
    assert graph_mod.structural_errors(graph, SPECS) == []
    assert graph_mod.type_errors(graph, SPECS) == []


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
