"""End-to-end tests for the MCP server (hub.mcp) — the JSON-RPC surface a user's Claude Code drives.

Exercises the REAL dispatch, tools, and engine (seeded data). Also asserts cross-surface parity: a
canvas an MCP client builds is the SAME persisted canvas the HTTP API serves to the browser.
"""

from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app
from hub.mcp import build_server, serve_stdio

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


def test_tools_list_every_tool_has_a_schema():
    tools = rpc("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_datasets", "create_canvas", "add_node", "connect", "set_transform",
            "preview_node", "run_canvas"} <= names
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"


# --------------------------------------------------------------------------- #
# Catalog / discovery
# --------------------------------------------------------------------------- #
def test_list_datasets_surfaces_seeded_tables_with_types_and_keys():
    ds = data("list_datasets", {})["datasets"]
    events = next(d for d in ds if d["name"] == "events")
    assert {"id", "user_id", "event", "amount"} <= {c["name"] for c in events["columns"]}
    assert all("type" in c for c in events["columns"])
    assert any(d["keys"] for d in ds)  # at least one dataset exposes a primary-key candidate


def test_sample_dataset_by_name_returns_real_rows():
    s = data("sample_dataset", {"dataset": "events", "limit": 3})
    assert len(s["rows"]) == 3 and s["columns"]


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


def test_remove_node_drops_node_and_edges():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    f = data("add_node", {"canvasId": cid, "kind": "filter"})["nodeId"]
    data("connect", {"canvasId": cid, "sourceId": s, "targetId": f})
    out = data("remove_node", {"canvasId": cid, "nodeId": f})
    assert out["removedEdges"] == 1
    doc = client.get(f"/api/canvas/{cid}").json()
    assert [n["id"] for n in doc["nodes"]] == [s] and doc["edges"] == []


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


# --------------------------------------------------------------------------- #
# Preview / validate / run
# --------------------------------------------------------------------------- #
def test_preview_node_returns_columns_and_rows():
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    pv = data("preview_node", {"canvasId": cid, "nodeId": s, "limit": 5})
    assert pv["columns"] and len(pv["rows"]) <= 5


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


def test_run_canvas_multiple_sinks_requires_node_id():
    cid = data("create_canvas", {})["canvasId"]
    data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})
    data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("images")}})
    res = call("run_canvas", {"canvasId": cid})
    assert res["isError"] is True and "specify nodeId" in res["content"][0]["text"]


def test_run_canvas_confirm_gate(monkeypatch):
    # a large/unknown-size run must ask for confirmation instead of silently doing a full pass
    from hub.models import RunEstimate
    cid = data("create_canvas", {})["canvasId"]
    s = data("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    runner = server.pg.deps.runner
    monkeypatch.setattr(runner, "estimate",
                        lambda plan, rows, byts=None: RunEstimate(rows=None, placement="local",
                                                                  needs_confirm=True, breakdown="big"))
    out = data("run_canvas", {"canvasId": cid, "nodeId": s})
    assert out["needsConfirm"] is True and "runId" not in out


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
def test_resources_list_and_read():
    res = rpc("resources/list")["result"]["resources"]
    uris = [r["uri"] for r in res]
    assert any(u.startswith("dataplay://dataset/") for u in uris)
    ds_uri = next(u for u in uris if u.startswith("dataplay://dataset/"))
    content = rpc("resources/read", {"uri": ds_uri})["result"]["contents"][0]
    assert content["mimeType"] == "application/json"
    assert "columns" in json.loads(content["text"])


def test_resources_read_unknown_uri_errors():
    err = rpc("resources/read", {"uri": "bogus://x"})["error"]
    assert err["code"] == -32602


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
