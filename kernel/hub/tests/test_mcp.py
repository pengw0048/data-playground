"""End-to-end tests for the MCP server (hub.mcp) — the JSON-RPC surface a user's Claude Code drives.

Exercises the REAL dispatch, tools, and engine (seeded data). Also asserts cross-surface parity: a
canvas an MCP client builds is the SAME persisted canvas the HTTP API serves to the browser.
"""

from __future__ import annotations

import base64
import io
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app
from hub.mcp import _catalog_failure, build_server, serve_stdio
from hub.models import CatalogPage, CatalogQuery, CatalogTable, ColumnSchema, Relationship

client = TestClient(app)
server = build_server(base_url="http://test.local")


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def rpc(method: str, params: dict | None = None, mid: int | None = 1):
    msg = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def call(name: str, arguments: dict) -> dict:
    """Invoke a tool; return its result envelope ({content, structuredContent?, isError})."""
    return rpc("tools/call", {"name": name, "arguments": arguments})["result"]


def data(name: str, arguments: dict):
    """A tool's structured result, asserting it did not error."""
    res = call(name, arguments)
    assert res.get("isError") is not True, res["content"][0]["text"]
    return res["structuredContent"]


# --------------------------------------------------------------------------- #
# Protocol handshake
# --------------------------------------------------------------------------- #
def test_initialize_negotiates_version_and_advertises_tools():
    r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert r["serverInfo"]["name"] == "data-playground"
    assert "tools" in r["capabilities"] and "resources" in r["capabilities"]
    assert r["instructions"]  # a non-empty how-to-use string guides the connected model


def test_initialize_falls_back_to_latest_for_unknown_version():
    r = rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert r["protocolVersion"] == "2025-06-18"


def test_notification_yields_no_response():
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_and_unknown_method():
    assert rpc("ping")["result"] == {}
    err = rpc("does/not/exist")["error"]
    assert err["code"] == -32601


def test_empty_batch_is_invalid_request():
    assert server.handle([])["error"]["code"] == -32600


def test_positional_params_are_invalid_params():
    # JSON-RPC permits array params, but every MCP method takes a by-name object → -32602, not a crash
    resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": ["2025-06-18"]})
    assert resp["error"]["code"] == -32602


def test_tools_list_every_tool_has_a_schema():
    tools = rpc("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"search_catalog", "get_dataset_context", "get_relationship_graph",
            "get_dataset_lineage", "create_canvas", "add_node", "connect", "set_transform",
            "preview_node", "run_canvas"} <= names
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"


# --------------------------------------------------------------------------- #
# Catalog / discovery
# --------------------------------------------------------------------------- #
def test_search_catalog_is_bounded_and_continuable(monkeypatch):
    first = CatalogTable(id="one", name="one", uri="memory://one", columns=[ColumnSchema(name="id", type="int")])
    second = CatalogTable(id="two", name="two", uri="memory://two", columns=[ColumnSchema(name="id", type="int")])
    seen: list[CatalogQuery] = []

    def page(query):
        seen.append(query)
        return CatalogPage(items=[first] if query.offset == 0 else [second], total=2,
                           offset=query.offset, limit=query.limit, has_more=query.offset == 0)

    monkeypatch.setattr(get_deps().catalog, "list_page", page)
    one = data("search_catalog", {"text": "sales", "tags": ["curated"],
                                   "requiredColumns": ["id"], "limit": 1})
    two = data("search_catalog", {"cursor": one["nextCursor"]})
    assert one["state"] == "available" and one["hasMore"] is True
    assert one["nextCursor"] != "1"
    assert two["hasMore"] is False and two["datasets"][0]["id"] == "two"
    assert seen[0].q == "sales" and seen[0].tags == ["curated"]
    assert seen[0].has_columns == ["id"] and seen[0].limit == 1
    assert seen[1].model_copy(update={"offset": 0}) == seen[0] and seen[1].offset == 1
    ambiguous = call("search_catalog", {"cursor": one["nextCursor"], "text": "different"})
    assert ambiguous["isError"] is True


@pytest.mark.parametrize("arguments", [
    {"text": "é" * 513},
    {"folder": "f" * 1_025},
    {"owner": "o" * 513},
    {"tags": ["t" * 129]},
    {"requiredColumns": ["c" * 129]},
    {"cursor": "a" * 131_073},
])
def test_search_catalog_bounds_every_caller_controlled_string(arguments):
    assert call("search_catalog", arguments)["isError"] is True


def test_search_catalog_rejects_an_out_of_range_cursor_offset():
    payload = {
        "v": 1,
        "query": {
            "text": None, "folder": None, "tags": [], "owner": None,
            "requiredColumns": [], "sort": "name", "order": "asc", "limit": 1,
        },
        "offset": 2_147_483_648,
    }
    cursor = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    assert call("search_catalog", {"cursor": cursor})["isError"] is True


@pytest.mark.parametrize(("error", "failure"), [
    (NotImplementedError("provider secret"), "unsupported"),
    (PermissionError("provider secret"), "permission_lost"),
    (ConnectionError("provider secret"), "offline"),
    (KeyError("provider secret"), "not_found"),
    (RuntimeError("provider secret"), "provider_error"),
    (HTTPException(501, "provider secret"), "unsupported"),
    (HTTPException(403, "provider secret"), "permission_lost"),
    (HTTPException(503, "provider secret"), "offline"),
    (HTTPException(404, "provider secret"), "not_found"),
    (HTTPException(502, "provider secret"), "provider_error"),
])
def test_catalog_failure_classifier_is_structured_and_secret_free(error, failure):
    result = _catalog_failure(error)
    assert result == {"state": "unavailable", "failure": failure}
    assert "secret" not in json.dumps(result)


def test_catalog_read_surfaces_keep_failures_distinct_from_available_empty(monkeypatch):
    catalog = get_deps().catalog
    original_page = catalog.list_page
    monkeypatch.setattr(catalog, "list_page", lambda _query: (_ for _ in ()).throw(
        RuntimeError("provider secret")))
    assert data("search_catalog", {}) == {"state": "unavailable", "failure": "provider_error"}
    monkeypatch.setattr(catalog, "list_page", original_page)

    missing = data("get_dataset_context", {"dataset": "definitely-not-a-dataset"})
    assert missing == {"state": "unavailable", "failure": "not_found"}
    empty = data("search_catalog", {"text": "definitely-no-catalog-match"})
    assert empty["state"] == "available" and empty["datasets"] == [] and empty["hasMore"] is False


def test_context_classifies_relationship_and_revision_failures(monkeypatch):
    from hub.routers import catalog as catalog_router

    catalog = get_deps().catalog
    monkeypatch.setattr(
        catalog, "relationships",
        lambda _uri=None: (_ for _ in ()).throw(PermissionError("provider secret")),
    )
    monkeypatch.setattr(
        catalog_router, "dataset_revision_capabilities",
        lambda _table_id: (_ for _ in ()).throw(ConnectionError("provider secret")),
    )
    context = data("get_dataset_context", {"dataset": "events"})
    assert context["state"] == "available"
    assert context["relationships"] == {
        "state": "unavailable", "failure": "permission_lost",
    }
    assert context["capabilities"]["revisions"] == {
        "state": "unavailable", "failure": "offline",
    }


def test_related_datasets_requires_a_structured_stable_identity():
    table = get_deps().catalog.get_table("tbl_events")
    result = data("related_datasets", {
        "source": {
            "kind": "local", "registrationId": table.registration_id,
            "revisionMode": "current",
        },
        "limit": 1,
    })
    assert result["state"] == "available"
    assert call("related_datasets", {"dataset": table.uri})["isError"] is True
    schema = next(item for item in rpc("tools/list")["result"]["tools"]
                  if item["name"] == "related_datasets")["inputSchema"]
    assert set(schema["properties"]) == {"source", "text", "folder", "limit"}
    assert "uri" not in schema["properties"]["source"]["properties"]


def test_context_relationship_window_is_independently_bounded(monkeypatch):
    events, images = _uri("events"), _uri("images")
    relationships = [
        Relationship(
            left_uri=events, left_columns=[f"left_{index}"],
            right_uri=images, right_columns=[f"right_{index}"],
        )
        for index in range(1_001)
    ]
    monkeypatch.setattr(get_deps().catalog, "relationships", lambda _uri=None: relationships)
    context = data("get_dataset_context", {"dataset": "events", "relationshipLimit": 10})
    assert context["relationships"]["state"] == "available"
    assert len(context["relationships"]["items"]) == 10
    assert context["relationships"] | {"items": []} == {
        "state": "available", "items": [], "limit": 10, "total": 1_001, "truncated": True,
    }


def test_dataset_context_uses_canonical_alias_and_redacts_legacy_meta():
    table = get_deps().catalog.get_table("tbl_events")
    context = data("get_dataset_context", {"dataset": table.uri})
    assert context["state"] == "available"
    assert context["dataset"]["id"] == table.id and context["dataset"]["uri"] == table.uri
    assert context["dataset"]["resourceUri"] == f"dataplay://dataset/{table.registration_id}"
    assert {"id", "user_id", "event", "amount"} <= {c["name"] for c in context["dataset"]["columns"]}
    assert "meta" not in context["dataset"] and context["capabilities"]["revisions"]["state"] in {"available", "unavailable"}


def test_relationship_graph_is_declared_only_and_truthfully_bounded():
    catalog = get_deps().catalog
    events, images = _uri("events"), _uri("images")
    relationship = Relationship(left_uri=events, left_columns=["id"], right_uri=images,
                                right_columns=["id"], cardinality="1:1")
    catalog.add_relationship(relationship)
    try:
        graph = data("get_relationship_graph", {"dataset": "events", "maxHops": 1, "maxNodes": 2})
        bounded = data("get_relationship_graph", {"dataset": "events", "maxHops": 0, "maxNodes": 1})
    finally:
        catalog.remove_relationship(relationship)
    assert relationship.model_dump(by_alias=True) in graph["edges"]
    assert graph["truncated"] is False and {node["uri"] for node in graph["nodes"]} >= {events, images}
    assert bounded["truncated"] is True and bounded["nodes"] == [graph["nodes"][0]] and not bounded["edges"]


def test_relationship_graph_caps_one_thousand_edges_truthfully(monkeypatch):
    events, images = _uri("events"), _uri("images")
    relationships = [
        Relationship(
            left_uri=events, left_columns=[f"left_{index}"],
            right_uri=images, right_columns=[f"right_{index}"],
        )
        for index in range(1_001)
    ]
    monkeypatch.setattr(get_deps().catalog, "relationships", lambda _uri=None: relationships)
    graph = data("get_relationship_graph", {
        "dataset": "events", "maxHops": 1, "maxNodes": 2, "maxEdges": 1_000,
    })
    assert graph["state"] == "available" and len(graph["edges"]) == 1_000
    assert graph["maxEdges"] == 1_000 and graph["truncated"] is True


def test_relationship_graph_and_lineage_classify_provider_failures(monkeypatch):
    catalog = get_deps().catalog
    original_relationships = catalog.relationships
    monkeypatch.setattr(
        catalog, "relationships",
        lambda _uri=None: (_ for _ in ()).throw(ConnectionError("provider secret")),
    )
    assert data("get_relationship_graph", {"dataset": "events"}) == {
        "state": "unavailable", "failure": "offline",
    }
    monkeypatch.setattr(catalog, "relationships", original_relationships)
    monkeypatch.setattr(
        catalog, "lineage",
        lambda _uri, depth=6, max_nodes=500: (_ for _ in ()).throw(
            NotImplementedError("provider secret")),
    )
    lineage = data("get_dataset_lineage", {"dataset": "events"})
    assert lineage["state"] == "unavailable" and lineage["failure"] == "unsupported"
    assert "secret" not in json.dumps(lineage)


def test_dataset_lineage_is_bounded_and_empty_is_not_unavailable():
    result = data("get_dataset_lineage", {"dataset": "events", "depth": 1, "maxNodes": 1})
    assert result["state"] == "available"
    assert result["lineage"]["rootUri"] == _uri("events")
    assert isinstance(result["lineage"]["truncated"], bool)


def test_sample_dataset_by_name_returns_real_rows():
    s = data("sample_dataset", {"dataset": "events", "limit": 3})
    assert len(s["rows"]) == 3 and s["columns"]
    assert s["completeness"] == "page"
    assert s["rowCount"] is not None and s["hasMore"] is True
    assert s["truncated"] is True
    assert s["rowLimit"] is None and s["limitScope"] is None


def test_sample_dataset_bad_ref_is_a_tool_error_not_a_crash():
    res = call("sample_dataset", {"dataset": "/no/such/file.parquet"})
    assert res["isError"] is True and res["content"][0]["text"]


def test_limit_zero_is_honored_not_replaced_by_default():
    # falsy-zero regression: limit:0 must mean zero rows (schema-only), not silently become the default
    s = data("sample_dataset", {"dataset": "events", "limit": 0})
    assert s["rows"] == [] and s["columns"]


def test_join_hints_measures_cardinality():
    j = data("join_hints", {"left": "images", "right": "events"})
    cards = {tuple(s["rightColumns"]): s["cardinality"] for s in j["suggestions"]}
    assert cards.get(("id",)) == "1:1" and cards.get(("user_id",)) == "1:N"


# --------------------------------------------------------------------------- #
# Canvas CRUD + graph building (and cross-surface persistence)
# --------------------------------------------------------------------------- #
def test_build_is_persisted_and_visible_through_the_http_api():
    cid = data("create_canvas", {"name": "built-by-mcp"})["canvasId"]
    src = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})
    flt = data("add_node", {"canvasId": cid, "kind": "filter", "config": {"predicate": "amount > 0"}})
    data("connect", {"canvasId": cid, "sourceId": src["nodeId"], "targetId": flt["nodeId"]})

    # the very same canvas the browser would load
    doc = client.get(f"/api/canvas/{cid}").json()
    assert {n["id"] for n in doc["nodes"]} == {src["nodeId"], flt["nodeId"]}
    assert len(doc["edges"]) == 1
    assert any(c["id"] == cid for c in data("list_canvases", {})["canvases"])
    # and get_canvas gives a clickable url + a compact node view
    gc = data("get_canvas", {"canvasId": cid})
    assert gc["url"] == f"http://test.local/#/canvas/{cid}"
    assert {n["type"] for n in gc["nodes"]} == {"source", "filter"}


def test_structural_edit_preserves_section_child_positions():
    # a canvas with a `section` uses parent-RELATIVE child positions; a structural MCP edit must not
    # relayout them into absolute coords (which would fling them out of the frame). Seed such a canvas
    # through the HTTP API (MCP can't set parentId), then add a top-level node via MCP.
    cid = "canvas_section_pos"
    doc = {"id": cid, "name": "sec", "version": 1, "edges": [],
           "nodes": [{"id": "sec_1", "type": "section", "position": {"x": 0, "y": 0}, "data": {"config": {}}},
                     {"id": "child_1", "type": "filter", "position": {"x": 10, "y": 20},
                      "parentId": "sec_1", "data": {"config": {}}}]}
    assert client.put(f"/api/canvas/{cid}", json=doc).status_code == 200
    data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})
    after = {n["id"]: n for n in client.get(f"/api/canvas/{cid}").json()["nodes"]}
    assert after["child_1"]["position"] == {"x": 10, "y": 20}     # relative child position untouched
    assert after["child_1"]["parentId"] == "sec_1"


def test_get_missing_canvas_is_a_tool_error():
    res = call("get_canvas", {"canvasId": "canvas_does_not_exist"})
    assert res["isError"] is True and "not found" in res["content"][0]["text"].lower()


def test_add_node_unknown_kind_is_iserror():
    cid = data("create_canvas", {})["canvasId"]
    res = call("add_node", {"canvasId": cid, "kind": "frobnicate"})
    assert res["isError"] is True and "frobnicate" in res["content"][0]["text"]


def test_connect_duplicate_single_input_is_iserror():
    cid = data("create_canvas", {})["canvasId"]
    a = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    b = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("images")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter"})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": a, "targetId": f})
    res = call("connect", {"canvasId": cid, "sourceId": b, "targetId": f})
    assert res["isError"] is True and "already connected" in res["content"][0]["text"]


def test_connect_rejects_an_explicit_empty_target_handle():
    cid = data("create_canvas", {})["canvasId"]
    source = data("add_node", {"canvasId": cid, "kind": "source"})["nodeId"]
    target = data("add_node", {"canvasId": cid, "kind": "filter"})["nodeId"]

    result = call("connect", {
        "canvasId": cid, "sourceId": source, "targetId": target, "targetHandle": "",
    })

    assert result["isError"] is True
    assert client.get(f"/api/canvas/{cid}").json()["edges"] == []


def test_section_output_config_edit_preserves_only_exact_named_edges():
    cid = data("create_canvas", {})["canvasId"]
    section = data("add_node", {
        "canvasId": cid, "kind": "section", "config": {"outputs": ["out"]},
    })["nodeId"]
    target = data("add_node", {"canvasId": cid, "kind": "filter"})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": section, "targetId": target})

    invalid = call("set_node_config", {
        "canvasId": cid, "nodeId": section, "config": {"outputs": ["left", "left"]},
    })
    assert invalid["isError"] is True
    unchanged = client.get(f"/api/canvas/{cid}").json()
    assert unchanged["nodes"][0]["data"]["config"]["outputs"] == ["out"]
    assert unchanged["edges"][0]["sourceHandle"] == "out"

    data("set_node_config", {
        "canvasId": cid, "nodeId": section, "config": {"outputs": ["out", "right"]},
    })
    retained = client.get(f"/api/canvas/{cid}").json()
    assert retained["edges"][0]["sourceHandle"] == "out"

    data("set_node_config", {
        "canvasId": cid, "nodeId": section, "config": {"outputs": ["left", "right"]},
    })
    assert client.get(f"/api/canvas/{cid}").json()["edges"] == []


def test_remove_node_drops_node_and_edges():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter"})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": f})
    out = data("remove_node", {"canvasId": cid, "nodeId": f})
    assert out["removedEdges"] == 1
    doc = client.get(f"/api/canvas/{cid}").json()
    assert [n["id"] for n in doc["nodes"]] == [s] and doc["edges"] == []


def test_structural_edit_preserves_hand_arranged_positions():
    # regression for the full-relayout bug: a structural MCP edit must position only the NEW node and
    # leave a human's hand-arranged positions intact (the canvas is live in the browser).
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter", "config": {"predicate": "amount > 0"}})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": f})
    # the user drags the source somewhere specific (PUT through the HTTP API, as the browser would)
    doc = client.get(f"/api/canvas/{cid}").json()
    for n in doc["nodes"]:
        if n["id"] == s:
            n["position"] = {"x": 999, "y": 777}
    doc["version"] = doc.get("version", 1) + 1
    assert client.put(f"/api/canvas/{cid}", json=doc).status_code == 200
    # the agent adds another node via MCP → the hand-set position must survive
    data("add_node", {"canvasId": cid, "kind": "select", "config": {"columns": "id"}})
    after = {n["id"]: n["position"] for n in client.get(f"/api/canvas/{cid}").json()["nodes"]}
    assert after[s] == {"x": 999, "y": 777}


def test_set_node_config_leaves_positions_untouched():
    # a pure config edit is not a structural change → no relayout at all
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    doc = client.get(f"/api/canvas/{cid}").json()
    doc["nodes"][0]["position"] = {"x": 42, "y": 24}
    doc["version"] = doc.get("version", 1) + 1
    client.put(f"/api/canvas/{cid}", json=doc)
    data("set_node_config", {"canvasId": cid, "nodeId": s, "config": {"limit": 5}})
    pos = {n["id"]: n["position"] for n in client.get(f"/api/canvas/{cid}").json()["nodes"]}
    assert pos[s] == {"x": 42, "y": 24}


def test_union_accepts_distinct_sources_but_rejects_the_same_one_twice():
    cid = data("create_canvas", {})["canvasId"]
    a = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    b = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("images")}})["nodeId"]
    u = data("add_node", {"canvasId": cid, "kind": "union"})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": a, "targetId": u})   # a multi-input port…
    data("connect", {"canvasId": cid, "sourceId": b, "targetId": u})   # …accepts a second DISTINCT source
    dup = call("connect", {"canvasId": cid, "sourceId": a, "targetId": u})  # but not the same one twice
    assert dup["isError"] is True and "already wired" in dup["content"][0]["text"]
    doc = client.get(f"/api/canvas/{cid}").json()
    assert len(doc["edges"]) == 2


def test_connect_unknown_target_handle_is_rejected():
    cid = data("create_canvas", {})["canvasId"]
    a = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("images")}})["nodeId"]
    j = data("add_node", {"canvasId": cid, "kind": "join", "config": {"on": "id"}})["nodeId"]
    res = call("connect", {"canvasId": cid, "sourceId": a, "targetId": j, "targetHandle": "zzz"})
    assert res["isError"] is True and "no input handle" in res["content"][0]["text"]


# --------------------------------------------------------------------------- #
# The headline feature: authoring transform code + verifying it
# --------------------------------------------------------------------------- #
def test_set_transform_writes_code_and_previews_the_new_column():
    cid = data("create_canvas", {"name": "xform"})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    code = "def fn(row):\n    row['is_purchase'] = row.get('event') == 'purchase'\n    return row"
    out = data("set_transform", {"canvasId": cid, "code": code, "mode": "map", "upstreamNodeId": s})
    assert out["created"] is True
    cols = {c["name"] for c in out["preview"]["columns"]}
    assert "is_purchase" in cols  # the preview proves the authored code actually ran


def test_set_transform_update_surfaces_a_code_error_for_the_next_fix():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    made = data("set_transform", {"canvasId": cid, "code": "def fn(row): return row", "upstreamNodeId": s})
    nid = made["nodeId"]
    broken = data("set_transform", {"canvasId": cid, "code": "def fn(row): return 1 / 0", "nodeId": nid})
    assert broken["created"] is False
    pv = broken["preview"]
    # a runtime cell error comes back with a human-readable reason the client can act on
    assert (pv.get("error") or pv.get("notPreviewable")) and "ZeroDivision" in pv["reason"]


def test_set_transform_map_batches_passes_batch_format_through():
    # the batchFormat argument must reach the node config, not be silently dropped
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    out = data("set_transform", {"canvasId": cid, "upstreamNodeId": s, "mode": "map_batches",
                                 "batchFormat": "arrow", "code": "def fn(batch):\n    return batch"})
    node = next(n for n in data("get_canvas", {"canvasId": cid})["nodes"] if n["id"] == out["nodeId"])
    assert node["config"]["mode"] == "map_batches" and node["config"]["batchFormat"] == "arrow"


# --------------------------------------------------------------------------- #
# Preview / validate / run
# --------------------------------------------------------------------------- #
def test_preview_node_returns_columns_and_rows():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    pv = data("preview_node", {"canvasId": cid, "nodeId": s, "limit": 5})
    assert pv["columns"] and len(pv["rows"]) <= 5
    assert pv["rowCount"] is None and pv["completeness"] == "sample"
    assert pv["truncated"] is True
    assert pv["rowLimit"] == 2000 and pv["limitReason"] == "preview-scan"
    assert pv["limitScope"] == "each-source"


def test_validate_flags_a_typed_wire_error():
    # metric emits a scalar `metric` wire; feeding it into a filter (wants a dataset) is a type error
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    m = data("add_node", {"canvasId": cid, "kind": "metric", "config": {"agg": "count"}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter"})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": m})
    data("connect", {"canvasId": cid, "sourceId": m, "targetId": f})
    v = data("validate_canvas", {"canvasId": cid})
    assert v["type_errors"]


def test_validate_reports_join_cardinality():
    cid = data("create_canvas", {})["canvasId"]
    a = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("images")}})["nodeId"]
    b = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    j = data("add_node", {"canvasId": cid, "kind": "join", "config": {"on": "id"}})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": a, "targetId": j, "targetHandle": "a"})
    data("connect", {"canvasId": cid, "sourceId": b, "targetId": j, "targetHandle": "b"})
    v = data("validate_canvas", {"canvasId": cid})
    assert v["joins"][j]["cardinality"] == "1:1"


def test_run_canvas_executes_to_completion():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter", "config": {"predicate": "amount > 0"}})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": f})
    # a single sink → nodeId is inferred
    out = data("run_canvas", {"canvasId": cid, "confirm": True})
    assert out["status"] == "done" and out["error"] is None and out["targetNodeId"] == f


def test_run_canvas_unknown_destination_is_a_tool_error():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    w = data("add_node", {"canvasId": cid, "kind": "write", "config": {
        "filename": "out.parquet", "writeMode": "overwrite", "destId": "ghost-destination"}})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": w})
    res = call("run_canvas", {"canvasId": cid, "confirm": True})
    assert res["isError"] is True and "unknown destination" in res["content"][0]["text"]


def test_run_canvas_multiple_sinks_requires_node_id():
    cid = data("create_canvas", {})["canvasId"]
    data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})
    data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("images")}})
    res = call("run_canvas", {"canvasId": cid})
    assert res["isError"] is True and "specify nodeId" in res["content"][0]["text"]


@pytest.mark.parametrize("value", [{}, "", 0, False])
@pytest.mark.parametrize("tool", ["preview_node", "run_canvas"])
def test_typed_parameter_bindings_reject_explicit_falsey_non_arrays(value, tool):
    cid = data("create_canvas", {})["canvasId"]
    source = data("add_node", {
        "canvasId": cid, "kind": "source", "config": {"uri": _uri("events")},
    })["nodeId"]
    arguments = {"canvasId": cid, "parameterBindings": value}
    if tool == "preview_node":
        arguments["nodeId"] = source
    result = call(tool, arguments)
    assert result["isError"] is True
    assert "parameterBindings must be an array" in result["content"][0]["text"]


def test_run_canvas_confirm_gate_trips_on_real_size(monkeypatch):
    # The gate must fire from the REAL estimator, not a mock: lower the row threshold so the seeded
    # events table trips it, and assert the reported estRows is the dataset's actual measured count
    # (this is the regression guard — run_canvas used to call estimate(None, None), a dead gate).
    import hub.plugins.runner as runner_mod
    monkeypatch.setattr(runner_mod, "_CONFIRM_ROWS", 1)
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    out = data("run_canvas", {"canvasId": cid, "nodeId": s})
    assert out["needsConfirm"] is True and "runId" not in out
    assert isinstance(out["estRows"], int) and out["estRows"] >= 1  # a real count from _cone_size, not None
    # ...and passing confirm:true runs it to completion despite the gate
    done = data("run_canvas", {"canvasId": cid, "nodeId": s, "confirm": True})
    assert done["status"] == "done"


def test_run_canvas_small_run_does_not_confirm():
    # a genuinely small run must NOT trip the (un-mocked) gate — proves the real estimate path returns
    # small sizes rather than defaulting to 'unknown → needs_confirm'
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter", "config": {"predicate": "amount > 0"}})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": f})
    out = data("run_canvas", {"canvasId": cid})  # no confirm
    assert out.get("needsConfirm") is not True and out["status"] == "done"


def test_run_status_and_cancel_are_recoverable(monkeypatch):
    # a run that outlasts the poll window returns a recoverable envelope (timedOut + runId), and
    # run_status follows it to completion; cancel_run on a finished run returns it unchanged.
    import hub.mcp as mcp_mod
    monkeypatch.setattr(mcp_mod, "_RUN_POLL_TIMEOUT_S", 0.0)  # give up immediately → exercise the timeout path
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter", "config": {"predicate": "amount > 0"}})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": f})
    out = data("run_canvas", {"canvasId": cid, "confirm": True})
    assert out.get("timedOut") is True and out["runId"] and out["status"] != "done"
    rid = out["runId"]
    # poll to completion via run_status (bounded)
    import time
    deadline = time.monotonic() + 10
    st = data("run_status", {"runId": rid})
    while st["status"] not in ("done", "failed", "cancelled") and time.monotonic() < deadline:
        time.sleep(0.05)
        st = data("run_status", {"runId": rid})
    assert st["status"] == "done" and st["runId"] == rid
    # cancel on an already-finished run is a no-op returning it unchanged (not an error)
    assert data("cancel_run", {"runId": rid})["status"] == "done"


def test_cancel_run_persists_durable_intent_when_plugin_is_unavailable():
    from hub import metadb

    run_id = f"mcp_missing_durable_cancel_{uuid.uuid4().hex}"
    ref = {
        "backend": "missing-mcp-durable-cancel-test",
        "cluster_ref": "test-cluster",
        "attempt_id": f"attempt-{run_id}",
        "submission_id": f"submission-{run_id}",
        "job_uri": f"s3://test-control/{run_id}.dpjob",
        "result_uri": f"s3://test-control/{run_id}.dpresult",
    }
    status = {
        "run_id": run_id,
        "status": "running",
        "placement": "distributed",
        "per_node": [],
    }
    try:
        metadb.preallocate_run_owner(run_id, metadb.DEFAULT_USER_ID, None)
        _stored, created = metadb.bind_backend_job(run_id, ref, status)
        assert created is True

        cancelled = data("cancel_run", {"runId": run_id})

        assert cancelled["runId"] == run_id and cancelled["status"] == "running"
        assert cancelled["timedOut"] is True
        assert metadb.backend_job(run_id)["cancel_requested"] is True
    finally:
        with metadb.session() as session:
            job = session.get(metadb.RunBackendJob, run_id)
            state = session.get(metadb.RunState, run_id)
            if job is not None:
                session.delete(job)
            if state is not None:
                session.delete(state)


def test_run_envelope_flags_a_non_terminal_run_as_timed_out():
    # unit-level, race-free: the envelope marks a still-running status as recoverable, a terminal one not
    from hub.mcp import Playground
    from hub.models import RunOutput, RunStatus

    output = RunOutput(
        node_id="n", port_id="out", wire="dataset", publication_kind="result",
        outcome="committed", uri="/tmp/result.parquet", rows=3,
    )
    done_status = RunStatus(
        run_id="r", status="done", target_node_id="n", total_rows=3, ms=9,
        outputs=[output],
    )
    running_status = RunStatus(
        run_id="r", status="running", target_node_id="n", total_rows=None, ms=9,
        outputs=[output.model_copy(update={"outcome": "pending", "uri": None, "rows": None})],
    )

    done = Playground._run_envelope(done_status, "n")
    assert done.get("timedOut") is not True and done["status"] == "done" and "hint" not in done
    assert done["outputs"][0]["uri"] == "/tmp/result.parquet"
    running = Playground._run_envelope(running_status, "n")
    assert running["timedOut"] is True and running["runId"] == "r" and running["hint"]


def test_run_status_unknown_id_reports_a_failed_status():
    # resolved like the web GET /run/{id}: an unknown/evicted run comes back as a terminal 'failed'
    # status with a reason, not a hard error, so the client resolves cleanly instead of retrying.
    st = data("run_status", {"runId": "run_does_not_exist"})
    assert st["status"] == "failed" and "not found" in (st["error"] or "").lower()


def test_cancel_run_unknown_id_reports_a_failed_status():
    # like the web POST /run/{id}/cancel: an unknown run resolves to a terminal 'failed' status via the
    # DB-backed backend, not a hard error (the ToolError path is only for a workspace with no backend).
    st = data("cancel_run", {"runId": "run_does_not_exist"})
    assert st["status"] == "failed" and "not found" in (st["error"] or "").lower()


def test_run_status_and_cancel_are_listed_tools():
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert {"run_status", "cancel_run"} <= names


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
def test_resources_list_and_read():
    res = rpc("resources/list")["result"]["resources"]
    # Dataset discovery is deliberately absent here: MCP resources/list has no continuation field.
    # search_catalog returns the bounded resource URI for an exact dataset instead.
    assert not any(r["uri"].startswith("dataplay://dataset/") for r in res)
    ds_uri = data("search_catalog", {"text": "events", "limit": 1})["datasets"][0]["resourceUri"]
    content = rpc("resources/read", {"uri": ds_uri})["result"]["contents"][0]
    assert content["mimeType"] == "application/json"
    payload = json.loads(content["text"])
    assert "columns" in payload["dataset"] and "meta" not in payload["dataset"]


def test_dataset_resource_registration_identity_does_not_rebind_after_reregister(tmp_path):
    path = tmp_path / f"mcp-aba-{uuid.uuid4().hex}.csv"
    path.write_text("id,value\n1,old\n")
    catalog = get_deps().catalog
    replacement = None
    try:
        created = client.post("/api/catalog/register", json={
            "uri": str(path), "name": f"mcp_aba_{uuid.uuid4().hex}",
        }).json()
        old_registration = created["registrationId"]
        old_resource = f"dataplay://dataset/{old_registration}"

        # The current catalog resolver accepts the same stable identity used by the resource URI.
        context = data("get_dataset_context", {"dataset": old_registration})
        assert context["dataset"]["resourceUri"] == old_resource
        assert "result" in rpc("resources/read", {"uri": old_resource})

        removed = client.delete(f"/api/catalog/tables/{created['id']}", params={
            "expected_registration_id": old_registration,
            "expected_revision": created["metadataRevision"],
        })
        assert removed.status_code == 200
        replacement = client.post("/api/catalog/register", json={
            # Deliberately collide with the stale registration token's generic name namespace.
            # resources/read must still resolve that token as one exact registration or fail closed.
            "uri": str(path), "name": old_registration,
        }).json()
        assert replacement["registrationId"] != old_registration

        stale = rpc("resources/read", {"uri": old_resource})
        assert stale["error"]["code"] == -32002
        new_resource = f"dataplay://dataset/{replacement['registrationId']}"
        fresh = rpc("resources/read", {"uri": new_resource})["result"]["contents"][0]
        assert json.loads(fresh["text"])["dataset"]["registrationId"] == replacement["registrationId"]
    finally:
        # This test owns the unique path and removes whichever generation is current.
        catalog.unregister(str(path))


def test_resources_read_unknown_uri_errors():
    err = rpc("resources/read", {"uri": "bogus://x"})["error"]
    assert err["code"] == -32002  # MCP 'Resource not found', distinct from -32602 invalid params


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #
def test_serve_stdio_reads_lines_and_keeps_stdout_clean():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no response expected
        "this is not json",                                          # parse error, own response
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) if isinstance(m, dict) else m for m in lines) + "\n")
    stdout = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout)
    out = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    # initialize (id 1), the parse error (id null), tools/list (id 2) — the notification produced nothing
    assert [o.get("id") for o in out] == [1, None, 2]
    assert "error" in out[1] and out[1]["error"]["code"] == -32700
