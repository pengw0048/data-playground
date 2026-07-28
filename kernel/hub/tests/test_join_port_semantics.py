"""Join operands are defined by target handles, never serialized edge order."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pyarrow as pa

from hub import db, ir, relationships
from hub.executors.engine import BuildEngine
from hub.executors.schema import schema_for_graph
from hub.models import ColumnSchema, Graph
from hub.nodespecs import BUILTIN_NODE_SPECS

SPECS = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}


def _reference(dataset_id: str) -> dict:
    return {
        "target": {"kind": "exact", "datasetId": dataset_id, "revisionId": "r1"},
        "keyFields": ["id"],
        "provenance": "provider",
    }


class _Adapter:
    name = "duckdb"

    def __init__(self, tables: dict[str, pa.Table], schemas: dict[str, list[ColumnSchema]]):
        self.tables = tables
        self.schemas = schemas

    def scan(self, uri: str, *, columns=None, limit: int | None = None, **_kwargs):
        table = self.tables[uri]
        if columns:
            table = table.select(columns)
        relation = db.conn().from_arrow(table)
        return relation.limit(limit) if limit is not None else relation

    def preview_scan(self, uri: str, *, limit: int = 2000, **kwargs):
        return self.scan(uri, limit=limit, **kwargs)

    def schema(self, uri: str) -> list[ColumnSchema]:
        return self.schemas[uri]

    @staticmethod
    def fingerprint(uri: str) -> str:
        return f"join-port:{uri}"


class _NoCatalogMetadata:
    @staticmethod
    def get_table(_token):
        raise KeyError("not registered")

    @staticmethod
    def relationships():
        return []


def _node(node_id: str, kind: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "position": {"x": 0, "y": 0},
        "data": {"title": node_id, "config": config or {}},
    }


def _edge(edge_id: str, source: str, handle: str) -> dict:
    return {
        "id": edge_id,
        "source": source,
        "target": "join",
        "sourceHandle": "out",
        "targetHandle": handle,
        "data": {"wire": "dataset"},
    }


def _graph(config: dict, *, reversed_edges: bool) -> Graph:
    left = _edge("left-edge", "left", "a")
    right = _edge("right-edge", "right", "b")
    return Graph.model_validate({
        "id": "join-port-semantics",
        "version": 1,
        "nodes": [
            _node("left", "source", {"uri": "left"}),
            _node("right", "source", {"uri": "right"}),
            _node("join", "join", config),
        ],
        "edges": [right, left] if reversed_edges else [left, right],
    })


def _rows(graph: Graph, adapter: _Adapter) -> tuple[list[str], list[dict]]:
    with db.run_scope():
        table = BuildEngine(
            graph,
            lambda _uri: adapter,
            {},
            full=True,
            node_specs=SPECS,
        ).relation("join").order("left_id").to_arrow_table()
    return table.column_names, table.to_pylist()


def test_reversed_edges_preserve_local_rows_schema_references_suggestions_and_ir():
    adapter = _Adapter(
        {
            "left": pa.table({"left_id": [1, 2], "left_value": ["l1", "l2"]}),
            "right": pa.table({"right_id": [2, 1], "right_value": ["r2", "r1"]}),
        },
        {
            "left": [
                ColumnSchema.model_validate({
                    "name": "left_id", "type": "int64",
                    "rowReference": _reference("left-rows"),
                }),
                ColumnSchema(name="left_value", type="string"),
            ],
            "right": [
                ColumnSchema.model_validate({
                    "name": "right_id", "type": "int64",
                    "rowReference": _reference("right-rows"),
                }),
                ColumnSchema(name="right_value", type="string"),
            ],
        },
    )
    config = {"condition": "a.left_id = b.right_id", "how": "inner"}
    canonical = _graph(config, reversed_edges=False)
    reversed_graph = _graph(config, reversed_edges=True)

    assert _rows(reversed_graph, adapter) == _rows(canonical, adapter)
    assert _rows(reversed_graph, adapter) == (
        ["left_id", "left_value", "right_id", "right_value"],
        [
            {"left_id": 1, "left_value": "l1", "right_id": 1, "right_value": "r1"},
            {"left_id": 2, "left_value": "l2", "right_id": 2, "right_value": "r2"},
        ],
    )

    canonical_schema = schema_for_graph(
        canonical, lambda _uri: adapter, {}, node_specs=SPECS)
    reversed_schema = schema_for_graph(
        reversed_graph, lambda _uri: adapter, {}, node_specs=SPECS)
    assert reversed_schema == canonical_schema
    join_columns = {column["name"]: column for column in reversed_schema["join"]}
    assert join_columns["left_id"]["rowReference"]["target"]["datasetId"] == "left-rows"
    assert join_columns["right_id"]["rowReference"]["target"]["datasetId"] == "right-rows"

    analysis_columns = {
        **reversed_schema,
        "left": [
            ColumnSchema(name="left_id", type="int64"),
            ColumnSchema(name="left_value", type="string"),
        ],
        "right": [
            ColumnSchema(name="right_id", type="int64"),
            ColumnSchema(name="right_value", type="string"),
        ],
    }
    analysis = relationships.analyze_join(
        reversed_graph,
        "join",
        analysis_columns,
        _NoCatalogMetadata(),
        lambda _uri: adapter,
    )
    assert analysis.suggestions[0].left_columns == ["left_id"]
    assert analysis.suggestions[0].right_columns == ["right_id"]

    lowered = ir.lower_to_ir(reversed_graph, "join", SPECS)
    assert lowered.by_id()["join"].inputs == [("left", "out"), ("right", "out")]

    # The reference Ray backend consumes this shared IR. Its eligibility check proves the reversed
    # serialized graph reaches the distributed Join path with the same semantic operand ordering.
    plugin_path = Path(__file__).resolve().parents[3] / "examples/plugins/dp_ray/__init__.py"
    spec = importlib.util.spec_from_file_location("dp_ray_join_port_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = object.__new__(module.RayRunner)
    assert runner._ray_runnable(lowered) is True


def test_same_name_join_is_also_edge_order_independent():
    adapter = _Adapter(
        {
            "left": pa.table({"left_id": [1, 2], "value": ["l1", "l2"]}),
            "right": pa.table({"left_id": [2, 1], "value": ["r2", "r1"]}),
        },
        {
            "left": [
                ColumnSchema(name="left_id", type="int64"),
                ColumnSchema(name="value", type="string"),
            ],
            "right": [
                ColumnSchema(name="left_id", type="int64"),
                ColumnSchema(name="value", type="string"),
            ],
        },
    )
    config = {"on": "left_id", "how": "inner"}
    assert _rows(
        _graph(config, reversed_edges=True), adapter
    ) == _rows(_graph(config, reversed_edges=False), adapter)
