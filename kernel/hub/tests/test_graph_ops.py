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


def test_add_section_returns_effective_dynamic_outputs_in_declaration_order():
    g = _empty()
    out = graph_ops.add_node(
        g, SPECS, "section_1", "section", config={"outputs": ["left", "right"]})
    assert out["outputs"] == [
        {"id": "left", "label": "left", "wire": "dataset"},
        {"id": "right", "label": "right", "wire": "dataset"},
    ]
    assert [node["id"] for node in g["nodes"]] == ["section_1"]


def test_add_section_rejects_invalid_outputs_without_partially_mutating_graph():
    g = _empty()
    with pytest.raises(graph_ops.GraphOpError, match="duplicate output port"):
        graph_ops.add_node(
            g, SPECS, "section_1", "section", config={"outputs": ["same", "same"]})
    assert g["nodes"] == []


def test_connect_records_source_wire_and_guards_duplicates():
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    out = graph_ops.connect(g, SPECS, "e_1", "source_1", "filter_1")
    assert out["wire"] == "dataset" and len(g["edges"]) == 1
    assert g["edges"][0]["sourceHandle"] == "out"
    assert g["edges"][0]["targetHandle"] == "in"
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


def test_connect_multi_port_still_rejects_the_exact_same_wire_twice():
    # a `multi` port takes many DISTINCT sources but the same source twice would double its rows
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "union_1", "union")
    graph_ops.connect(g, SPECS, "e_1", "source_1", "union_1")
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.connect(g, SPECS, "e_2", "source_1", "union_1")


def test_connect_unknown_target_handle_raises():
    # a handle that matches no input port would silently fall back to the first port — reject it
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "join_1", "join")
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.connect(g, SPECS, "e_1", "source_1", "join_1", target_handle="zzz")


def test_connect_empty_target_handle_is_explicit_and_invalid():
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")

    with pytest.raises(graph_ops.GraphOpError, match="no input handle"):
        graph_ops.connect(g, SPECS, "e_1", "source_1", "filter_1", target_handle="")

    assert g["edges"] == []


def test_connect_multi_output_requires_and_persists_source_handle():
    g = _empty()
    graph_ops.add_node(g, SPECS, "check", "assert")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    with pytest.raises(graph_ops.GraphOpError, match="source_handle is required"):
        graph_ops.connect(g, SPECS, "missing", "check", "filter_1")

    out = graph_ops.connect(
        g, SPECS, "passes", "check", "filter_1", source_handle="pass")
    assert out["source_handle"] == "pass"
    assert g["edges"] == [{
        "id": "passes", "source": "check", "target": "filter_1",
        "sourceHandle": "pass", "targetHandle": "in", "data": {"wire": "dataset"},
    }]


def test_connect_rejects_unknown_source_handle():
    g = _empty()
    graph_ops.add_node(g, SPECS, "check", "assert")
    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    with pytest.raises(graph_ops.GraphOpError, match="no output handle 'bogus'"):
        graph_ops.connect(
            g, SPECS, "e", "check", "filter_1", source_handle="bogus")


def test_connect_canonicalizes_omitted_and_explicit_default_port_aliases():
    g = _empty()
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.add_node(g, SPECS, "source_2", "source")
    graph_ops.add_node(g, SPECS, "union_1", "union")
    graph_ops.connect(g, SPECS, "first", "source_1", "union_1")
    with pytest.raises(graph_ops.GraphOpError, match="already wired"):
        graph_ops.connect(
            g, SPECS, "source-alias", "source_1", "union_1",
            target_handle="in", source_handle="out")

    graph_ops.add_node(g, SPECS, "filter_1", "filter")
    graph_ops.connect(g, SPECS, "input", "source_1", "filter_1")
    with pytest.raises(graph_ops.GraphOpError, match="already connected"):
        graph_ops.connect(
            g, SPECS, "target-alias", "source_2", "filter_1", target_handle="in")


def test_layout_new_ignores_section_child_coords_for_anchor():
    # a section child's position is parent-relative; it must not skew the absolute anchor for new nodes
    g = _empty()
    g["nodes"].append({"id": "sec", "type": "section", "position": {"x": 40, "y": 40}, "data": {}})
    g["nodes"].append({"id": "child", "type": "filter", "position": {"x": -300, "y": 999},
                       "parentId": "sec", "data": {}})
    graph_ops.add_node(g, SPECS, "source_1", "source")
    graph_ops.layout_new(g, {"sec", "child"})
    pos = {n["id"]: n["position"] for n in g["nodes"]}
    # anchored off the top-level section (x=40), NOT the child's relative x=-300
    assert pos["source_1"]["x"] == 40


def test_set_config_merges_leaving_other_params():
    g = _empty()
    graph_ops.add_node(g, SPECS, "filter_1", "filter", config={"predicate": "a > 0"})
    graph_ops.set_config(g, SPECS, "filter_1", {"note": "keep"})
    cfg = g["nodes"][0]["data"]["config"]
    assert cfg == {"predicate": "a > 0", "note": "keep"}
    with pytest.raises(graph_ops.GraphOpError):
        graph_ops.set_config(g, SPECS, "nope", {"x": 1})


def test_set_config_validates_section_outputs_before_mutating():
    g = _empty()
    graph_ops.add_node(g, SPECS, "section_1", "section", config={"outputs": ["out"]})

    with pytest.raises(graph_ops.GraphOpError, match="duplicate output"):
        graph_ops.set_config(g, SPECS, "section_1", {"outputs": ["left", "left"]})

    assert g["nodes"][0]["data"]["config"]["outputs"] == ["out"]


def test_set_config_canonicalizes_or_drops_section_output_edges():
    g = _empty()
    graph_ops.add_node(g, SPECS, "section_1", "section", config={"outputs": ["out"]})
    graph_ops.add_node(g, SPECS, "left_target", "filter")
    graph_ops.add_node(g, SPECS, "removed_target", "filter")
    graph_ops.connect(g, SPECS, "kept", "section_1", "left_target")
    graph_ops.connect(g, SPECS, "removed", "section_1", "removed_target")
    g["edges"][0].pop("sourceHandle")
    g["edges"][1]["sourceHandle"] = "removed"

    result = graph_ops.set_config(
        g, SPECS, "section_1", {"outputs": ["out", "right"]})

    assert result["removed_edges"] == 1
    assert [(edge["id"], edge["sourceHandle"]) for edge in g["edges"]] == [
        ("kept", "out")]


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
