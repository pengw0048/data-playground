"""Focused #787 coverage for the shared Canvas row-reference derivation contract."""

from __future__ import annotations

import uuid

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from hub import db
from hub.deps import get_deps
from hub.executors.engine import BuildEngine
from hub.executors.preview import preview_node
from hub.executors.schema import schema_for_graph
from hub.main import app
from hub.models import ColumnSchema, Graph
from hub.nodespecs import BUILTIN_NODE_SPECS
from hub.plugins.runner import LocalRunner

SPECS = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}


def _reference(target: str, *, provenance: str = "provider") -> dict:
    return {
        "target": {"kind": "exact", "datasetId": target, "revisionId": "r1"},
        "keyFields": ["id"],
        "semanticType": "row",
        "provenance": provenance,
    }


def _column(name: str, *, target: str | None = None,
            provenance: str = "provider") -> ColumnSchema:
    payload = {
        "name": name,
        "type": "int64",
        "physicalType": "int64",
        "provenance": provenance,
    }
    if target is not None:
        payload["rowReference"] = _reference(target, provenance=provenance)
    return ColumnSchema.model_validate(payload)


class ReferenceAdapter:
    name = "duckdb"

    def __init__(self, schemas: dict[str, list[ColumnSchema]]):
        self.schemas = schemas

    def schema(self, uri: str) -> list[ColumnSchema]:
        return self.schemas[uri]

    def matches(self, uri: str) -> bool:
        return uri in self.schemas

    def scan(self, uri: str, *, limit: int | None = None, **_kwargs):
        schema = pa.schema([
            pa.field(column.name, pa.int64()) for column in self.schemas[uri]
        ])
        table = pa.Table.from_pylist(
            [{column.name: index + 1 for index, column in enumerate(self.schemas[uri])}],
            schema=schema,
        )
        relation = db.conn().from_arrow(table)
        return relation.limit(limit) if limit is not None else relation

    def preview_scan(self, uri: str, *, limit: int = 2000, **kwargs):
        return self.scan(uri, limit=limit, **kwargs)

    @staticmethod
    def fingerprint(uri: str) -> str:
        return f"reference:{uri}"


def _node(node_id: str, kind: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "position": {"x": 0, "y": 0},
        "data": {"title": node_id, "config": config or {}},
    }


def _edge(source: str, target: str, *, handle: str | None = None) -> dict:
    return {
        "id": f"{source}-{target}-{handle or 'in'}",
        "source": source,
        "target": target,
        "targetHandle": handle,
        "data": {"wire": "dataset"},
    }


def _schemas(graph: Graph, adapter: ReferenceAdapter) -> dict:
    return schema_for_graph(
        graph, lambda _uri: adapter, {}, node_specs=SPECS, storage=None)


def _row_reference(schema: dict, node_id: str, field: str):
    column = next(column for column in schema[node_id] if column["name"] == field)
    return column["rowReference"]


def test_filter_select_rename_and_preview_share_exact_reference():
    adapter = ReferenceAdapter({
        "left": [_column("owner_id", target="owners"), _column("value")],
    })
    graph = Graph.model_validate({
        "id": "direct", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "left"}),
            _node("filter", "filter", {"predicate": "value > 0"}),
            _node("select", "select", {
                "select": "owner_id AS renamed_owner, value + 1 AS computed",
            }),
        ],
        "edges": [_edge("source", "filter"), _edge("filter", "select")],
    })

    schema = _schemas(graph, adapter)
    source_reference = _row_reference(schema, "source", "owner_id")
    assert _row_reference(schema, "filter", "owner_id") == source_reference
    assert _row_reference(schema, "select", "renamed_owner") == source_reference
    assert _row_reference(schema, "select", "computed") is None

    preview = preview_node(
        graph, "select", 5, lambda _uri: adapter, {},
        node_specs=SPECS, storage=None,
    )
    assert not preview.error
    assert preview.columns[0].row_reference is not None
    assert (
        preview.columns[0].row_reference.model_dump(by_alias=True, mode="json")
        == source_reference
    )
    assert preview.columns[1].row_reference is None


def test_select_duplicate_alias_is_unknown_instead_of_using_last_source():
    adapter = ReferenceAdapter({
        "duplicate": [
            _column("a", target="a-target"),
            _column("b", target="b-target"),
        ],
    })
    graph = Graph.model_validate({
        "id": "duplicate-alias", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "duplicate"}),
            _node("select", "select", {"select": "a AS x, b AS x"}),
        ],
        "edges": [_edge("source", "select")],
    })

    schema = _schemas(graph, adapter)
    assert [column["name"] for column in schema["select"]][0] == "x"
    assert all(column["rowReference"] is None for column in schema["select"])


def test_join_keeps_reference_ownership_from_both_source_sides():
    adapter = ReferenceAdapter({
        "left": [
            _column("id"),
            _column("owner_id", target="left-owners"),
        ],
        "right": [
            _column("id"),
            _column("owner_id", target="right-owners"),
        ],
    })
    graph = Graph.model_validate({
        "id": "join", "version": 1,
        "nodes": [
            _node("left", "source", {"uri": "left"}),
            _node("right", "source", {"uri": "right"}),
            _node("join", "join", {"on": "id"}),
        ],
        "edges": [
            _edge("left", "join", handle="a"),
            _edge("right", "join", handle="b"),
        ],
    })

    schema = _schemas(graph, adapter)
    assert (
        _row_reference(schema, "join", "owner_id")["target"]["datasetId"]
        == "left-owners"
    )
    right_owner = next(
        column for column in schema["join"]
        if column["name"] != "owner_id" and column["name"].startswith("owner_id_")
    )
    assert (
        right_owner["rowReference"]["target"]["datasetId"]
        == "right-owners"
    )


def test_aggregate_preserves_only_direct_group_key_reference():
    adapter = ReferenceAdapter({
        "left": [
            _column("group_id", target="groups"),
            _column("value", target="values"),
        ],
    })
    graph = Graph.model_validate({
        "id": "aggregate", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "left"}),
            _node("aggregate", "aggregate", {
                "groupBy": "group_id",
                "aggs": "sum(value) AS total",
            }),
        ],
        "edges": [_edge("source", "aggregate")],
    })

    schema = _schemas(graph, adapter)
    assert (
        _row_reference(schema, "aggregate", "group_id")["target"]["datasetId"]
        == "groups"
    )
    assert _row_reference(schema, "aggregate", "total") is None


def test_union_requires_complete_compatible_reference_evidence():
    adapter = ReferenceAdapter({
        "left": [_column("owner_id", target="owners")],
        "same": [_column("owner_id", target="owners", provenance="declared")],
        "conflict": [_column("owner_id", target="other")],
        "missing": [_column("owner_id")],
    })

    def union(right_uri: str) -> dict:
        graph = Graph.model_validate({
            "id": f"union-{right_uri}", "version": 1,
            "nodes": [
                _node("left", "source", {"uri": "left"}),
                _node("right", "source", {"uri": right_uri}),
                _node("union", "union"),
            ],
            "edges": [_edge("left", "union"), _edge("right", "union")],
        })
        return _schemas(graph, adapter)

    compatible = _row_reference(union("same"), "union", "owner_id")
    assert compatible["target"]["datasetId"] == "owners"
    assert compatible["provenance"] == "lineage"
    assert _row_reference(union("conflict"), "union", "owner_id") is None
    assert _row_reference(union("missing"), "union", "owner_id") is None


def test_declared_sql_and_code_references_round_trip_while_opaque_code_is_unknown():
    adapter = ReferenceAdapter({
        "left": [_column("owner_id", target="source-owners")],
    })
    declared = [{
        "name": "copied",
        "type": "int64",
        "rowReference": _reference("declared-owners", provenance="declared"),
    }]
    graph = Graph.model_validate({
        "id": "declarations", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "left"}),
            _node("sql", "sql", {
                "sql": "SELECT owner_id AS copied FROM input",
                "outputSchema": declared,
                "enforceSchema": True,
            }),
            _node("code", "transform", {
                "mode": "map",
                "code": "def fn(row): return {'copied': row['owner_id']}",
                "outputSchema": declared,
                "enforceSchema": True,
            }),
        ],
        "edges": [_edge("source", "sql"), _edge("source", "code")],
    })

    schema = _schemas(graph, adapter)
    assert (
        _row_reference(schema, "sql", "copied")["target"]["datasetId"]
        == "declared-owners"
    )
    assert _row_reference(schema, "code", "copied") == _row_reference(
        schema, "sql", "copied")

    processor = Graph.model_validate({
        "id": "processor-declaration", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "left"}),
            _node("processor", "custom-processor", {"outputSchema": declared}),
        ],
        "edges": [_edge("source", "processor")],
    })
    processor_schema = schema_for_graph(
        processor, lambda _uri: adapter, {},
        node_builders={"custom-processor": lambda *_args: None},
        node_specs=SPECS, storage=None,
    )
    assert (
        _row_reference(processor_schema, "processor", "copied")["target"]["datasetId"]
        == "declared-owners"
    )

    opaque = Graph.model_validate({
        "id": "opaque", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "left"}),
            _node("code", "transform", {
                "mode": "map",
                "code": "def fn(row): return row",
            }),
        ],
        "edges": [_edge("source", "code")],
    })
    assert _schemas(opaque, adapter)["code"] is None
    preview = preview_node(
        opaque, "code", 5, lambda _uri: adapter, {},
        node_specs=SPECS, storage=None,
    )
    assert not preview.error
    assert preview.columns[0].name == "owner_id"
    assert preview.columns[0].row_reference is None


def test_declared_sql_reuses_existing_runtime_schema_drift_gate():
    adapter = ReferenceAdapter({
        "left": [_column("owner_id", target="source-owners")],
    })
    declared = [{
        "name": "copied",
        "type": "int64",
        "rowReference": _reference("declared-owners", provenance="declared"),
    }]

    def graph(sql: str) -> Graph:
        return Graph.model_validate({
            "id": "sql-drift", "version": 1,
            "nodes": [
                _node("source", "source", {"uri": "left"}),
                _node("sql", "sql", {
                    "sql": sql,
                    "outputSchema": declared,
                    "enforceSchema": True,
                }),
            ],
            "edges": [_edge("source", "sql")],
        })

    with db.run_scope():
        current = graph("SELECT owner_id AS copied FROM input")
        engine = BuildEngine(
            current, lambda _uri: adapter, {}, full=True, node_specs=SPECS)
        LocalRunner._check_schema(None, current.nodes[1], engine)

    with db.run_scope():
        drifted = graph("SELECT owner_id AS changed FROM input")
        engine = BuildEngine(
            drifted, lambda _uri: adapter, {}, full=True, node_specs=SPECS)
        with pytest.raises(RuntimeError, match="schema contract.*violated"):
            LocalRunner._check_schema(None, drifted.nodes[1], engine)


def test_declared_sql_reference_overlays_actual_name_and_type_without_replacing_them():
    adapter = ReferenceAdapter({
        "left": [_column("owner_id", target="source-owners")],
    })
    declared_reference = _reference("declared-owners", provenance="declared")

    def schema(sql: str, declared_name: str) -> list[dict]:
        graph = Graph.model_validate({
            "id": "sql-observed", "version": 1,
            "nodes": [
                _node("source", "source", {"uri": "left"}),
                _node("sql", "sql", {
                    "sql": sql,
                    "outputSchema": [{
                        "name": declared_name,
                        "type": "string",
                        "rowReference": declared_reference,
                    }],
                }),
            ],
            "edges": [_edge("source", "sql")],
        })
        return _schemas(graph, adapter)["sql"]

    same_name = schema("SELECT owner_id AS copied FROM input", "copied")
    assert same_name[0]["name"] == "copied"
    assert same_name[0]["type"] != "string"
    assert same_name[0]["rowReference"]["target"]["datasetId"] == "declared-owners"

    changed_name = schema("SELECT owner_id AS actual FROM input", "declared")
    assert changed_name[0]["name"] == "actual"
    assert changed_name[0]["type"] != "string"
    assert changed_name[0]["rowReference"] is None


def test_public_schema_api_serializes_the_shared_reference_fact():
    uri = f"reference-api://{uuid.uuid4().hex}"
    adapter = ReferenceAdapter({
        uri: [_column("owner_id", target="api-owners")],
    })
    graph = Graph.model_validate({
        "id": "api-reference", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": uri}),
            _node("filter", "filter", {"predicate": "owner_id > 0"}),
        ],
        "edges": [_edge("source", "filter")],
    })
    deps = get_deps()
    deps.adapters.insert(0, adapter)
    try:
        response = TestClient(app).post(
            "/api/graph/schema",
            json={"graph": graph.model_dump(by_alias=True, mode="json")},
        )
        assert response.status_code == 200, response.text
        reference = response.json()["filter"]["out"][0]["rowReference"]
        assert reference["target"]["datasetId"] == "api-owners"
    finally:
        deps.adapters.remove(adapter)


def test_preview_reference_derivation_does_not_touch_nodes_outside_target_cone():
    adapter = ReferenceAdapter({
        "target": [_column("owner_id", target="target-owners")],
    })
    unrelated_adapter_calls: list[str] = []
    unrelated_node_calls: list[str] = []

    class UnrelatedAdapter:
        name = "offline"

        @staticmethod
        def schema(uri: str):
            unrelated_adapter_calls.append(f"schema:{uri}")
            raise ConnectionError("unrelated source is offline")

        @staticmethod
        def preview_scan(uri: str, **_kwargs):
            unrelated_adapter_calls.append(f"preview:{uri}")
            raise ConnectionError("unrelated source is offline")

        @staticmethod
        def scan(uri: str, **_kwargs):
            unrelated_adapter_calls.append(f"scan:{uri}")
            raise ConnectionError("unrelated source is offline")

    def resolve(uri: str):
        return adapter if uri == "target" else UnrelatedAdapter()

    def unrelated_builder(_engine, node, _inputs):
        unrelated_node_calls.append(node.id)
        return db.conn().sql("SELECT 1 AS unrelated")

    graph = Graph.model_validate({
        "id": "preview-cone", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "target"}),
            _node("filter", "filter", {"predicate": "owner_id > 0"}),
            _node("offline", "source", {"uri": "offline"}),
            _node("unrelated", "unrelated-plugin"),
        ],
        "edges": [_edge("source", "filter")],
    })

    preview = preview_node(
        graph, "filter", 5, resolve, {},
        node_builders={"unrelated-plugin": unrelated_builder},
        node_specs=None, storage=None,
    )

    assert not preview.error
    assert preview.columns[0].row_reference is not None
    assert unrelated_adapter_calls == []
    assert unrelated_node_calls == []
