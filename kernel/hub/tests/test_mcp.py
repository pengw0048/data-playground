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


def test_empty_batch_is_invalid_request():
    assert server.handle([])["error"]["code"] == -32600


def test_positional_params_are_invalid_params():
    # JSON-RPC permits array params, but every MCP method takes a by-name object → -32602, not a crash
    resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": ["2025-06-18"]})
    assert resp["error"]["code"] == -32602


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


def test_run_envelope_flags_a_non_terminal_run_as_timed_out():
    # unit-level, race-free: the envelope marks a still-running status as recoverable, a terminal one not
    from hub.mcp import Playground

    class _St:
        def __init__(self, status):
            self.run_id, self.status, self.total_rows, self.ms = "r", status, 3, 9
            self.output_table = self.output_uri = self.error = None

    done = Playground._run_envelope(_St("done"), "n")
    assert done.get("timedOut") is not True and done["status"] == "done" and "hint" not in done
    running = Playground._run_envelope(_St("running"), "n")
    assert running["timedOut"] is True and running["runId"] == "r" and running["hint"]


def test_run_status_unknown_id_is_a_tool_error():
    res = call("run_status", {"runId": "run_does_not_exist"})
    assert res["isError"] is True and "unknown runId" in res["content"][0]["text"]


def test_run_status_and_cancel_are_listed_tools():
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert {"run_status", "cancel_run"} <= names


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
