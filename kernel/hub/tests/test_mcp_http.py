"""The MCP-over-HTTP transport (POST /mcp) — the SAME MCPServer as stdio, served in-process by the
web app. Asserts the endpoint speaks JSON-RPC, is gated like /api, runs a real pipeline end-to-end
through the shared run path, and that a mutation nudges the canvas's collab room (live update).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app

client = TestClient(app)


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def rpc(method: str, params: dict | None = None, mid: int | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    r = client.post("/mcp", json=msg)
    assert r.status_code == 200, r.text
    return r.json()


def tool(name: str, arguments: dict) -> dict:
    res = rpc("tools/call", {"name": name, "arguments": arguments})["result"]
    assert res.get("isError") is not True, res["content"][0]["text"]
    return res["structuredContent"]


def test_http_initialize_and_tools_list():
    r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert r["serverInfo"]["name"] == "data-playground"
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert {"create_canvas", "add_node", "run_canvas", "sample_result"} <= names


def test_http_notification_yields_202_no_body():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and not r.content


def test_http_get_is_405_and_delete_ok():
    assert client.get("/mcp").status_code == 405   # no server-initiated SSE stream here
    assert client.delete("/mcp").status_code == 200  # stateless — nothing to tear down


def test_http_parse_error_is_jsonrpc_not_500():
    r = client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32700


def test_http_builds_and_runs_a_pipeline_end_to_end_via_the_shared_run_path():
    cid = tool("create_canvas", {"name": "http-built"})["canvasId"]
    s = tool("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})["nodeId"]
    w = tool("add_node", {"canvasId": cid, "kind": "write", "config": {"name": "http_out"}})["nodeId"]
    tool("connect", {"canvasId": cid, "sourceId": s, "targetId": w})
    out = tool("run_canvas", {"canvasId": cid, "confirm": True})
    assert out["status"] == "done" and out["error"] is None
    # the same canvas the browser would load, served by the HTTP API
    doc = client.get(f"/api/canvas/{cid}").json()
    assert {s, w} == {n["id"] for n in doc["nodes"]}
    # and the run's OUTPUT is samplable back through MCP (author -> run -> inspect loop)
    rows = tool("sample_result", {"runId": out["runId"], "limit": 3})
    assert rows["columns"] and len(rows["rows"]) <= 3


def test_http_mutation_nudges_the_collab_room():
    # an MCP edit must nudge an open browser tab (a peer in the canvas's collab room) to refetch —
    # this is the live-update path. Connect a collab websocket, edit via /mcp, expect an external-edit.
    cid = tool("create_canvas", {"name": "live"})["canvasId"]
    with client.websocket_connect(f"/ws/collab/{cid}") as wsock:
        plan = wsock.receive_json()
        assert plan["type"] == "server" and plan["event"] == "room-state" and plan["mode"] == "seed"
        tool("add_node", {"canvasId": cid, "kind": "source", "config": {"uri": _uri("events")}})
        msg = wsock.receive_json()
        assert msg["type"] == "server" and msg["event"] == "external-edit" and msg["canvasId"] == cid
        assert "clientId" not in msg  # must not be self-filtered by the receiving tab
