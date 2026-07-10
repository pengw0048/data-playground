"""Unit tests for hub.graph_ops — the shared graph-edit primitives (agent + MCP build on these)."""

from __future__ import annotations

import pytest

from hub import graph_ops
from hub.nodespecs import BUILTIN_NODE_SPECS

SPECS = {s.kind: s for s in BUILTIN_NODE_SPECS}


def _empty() -> dict:
    return {"id": "c", "version": 1, "nodes": [], "edges": []}


def test_add_node_returns_ports_and_appends():
    g = _empty()
    out = graph_ops.add_node(g, SPECS, "source_1", "source", config={"uri": "x.parquet"})
    assert out["node_id"] == "source_1"
    assert [o["id"] for o in out["outputs"]] == ["out"]
    assert g["nodes"][0]["data"]["config"] == {"uri": "x.parquet"}


def test_add_node_unknown_kind_raises():
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.add_node(_empty(), SPECS, "x_1", "frobnicate")


def test_connect_records_source_wire_and_guards_duplicates():
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    out = graph_ops.connect(g, SPECS, "e_1", "source_1", "filter_1")
    assert out["wire"] == "dataset" and len(g["edges"]) == 1
    # a second wire into the same single-fan-in input is refused
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.connect(g, SPECS, "e_2", "source_1", "filter_1")


def test_connect_missing_node_raises():
    g = _empty()
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.connect(g, SPECS, "e_1", "nope", "filter_1")


def test_connect_multi_input_port_accepts_many():
    # union has a single `multi` input port — two sources may both wire into it (regression: the
    # single-fan-in guard must NOT block a legitimate multi-input node).
    g = _empty()
    for i in (1, 2):
        graph_ops.add_node(g, SPECS, f"source_{i}", "source")
    graph_ops.add_node(g, SPECS, "union_1", "union")
    graph_ops.connect(g, SPECS, "e_1", "source_1", "union_1")
    graph_ops.connect(g, SPECS, "e_2", "source_2", "union_1")  # would raise if multi weren't honored
    assert len([e for e in g["edges"] if e["target"] == "union_1"]) == 2


def test_set_config_merges_leaving_other_params():
    g = _empty()
    graph_ops.add_node(g, SPECS, "filter_1", "filter", config={"predicate": "a > 0"})
    graph_ops.set_config(g, "filter_1", {"note": "keep"})
    cfg = g["nodes"][0]["data"]["config"]
    assert cfg == {"predicate": "a > 0", "note": "keep"}
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.set_config(g, "nope", {"x": 1})


def test_remove_node_drops_node_and_its_edges():
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    graph_ops.connect(g, SPECS, "e_1", "source_1", "filter_1")
    out = graph_ops.remove_node(g, "filter_1")
    assert out["removed_edges"] == 1
    assert [n["id"] for n in g["nodes"]] == ["source_1"] and g["edges"] == []
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.remove_node(g, "filter_1")


def test_fresh_id_is_unique_across_nodes_and_edges():
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "source_2", "source")
    assert graph_ops.fresh_id(g, "source") == "source_3"
    g["edges"].append({"id": "e_1"})
    assert graph_ops.fresh_id(g, "e") == "e_2"


def test_layout_new_places_only_new_nodes_below_existing():
    g = _empty()
    g["nodes"].append({"id": "old", "type": "source", "position": {"x": 500, "y": 500}, "data": {}})
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    graph_ops.connect(g, SPECS, "e_1", "source_1", "filter_1")
    graph_ops.layout_new(g, {"old"})
    pos = {n["id"]: n["position"] for n in g["nodes"]}
    assert pos["old"] == {"x": 500, "y": 500}              # existing node untouched
    assert pos["source_1"]["y"] >= 500 + 280               # new nodes placed below existing content
    assert pos["filter_1"]["x"] > pos["source_1"]["x"]     # chained node lands in the next column
