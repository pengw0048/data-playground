"""SEC-01 AgentDataPolicy — metadata-only default, sanitizer, audit, admin gate."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app

client = TestClient(app)


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def N(nid, t, cfg):
    return {"id": nid, "type": t, "position": {"x": 0, "y": 0}, "data": {"title": nid, "config": cfg}}


def _fixture_cell_markers() -> list[str]:
    """Distinctive sample cell values that must never appear under metadata-only."""
    return [
        "picsum.photos",
        "Movie 0",
        "purchase",
        "signup",
    ]


def _contains_fixture_value(blob: str) -> bool:
    return any(m in blob for m in _fixture_cell_markers())


def _run_preview_via_agent(*, policy, graph=None):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from hub.agent import run_agent

    uri = _uri("images")
    g = graph or {
        "nodes": [N("src", "source", {"uri": uri})],
        "edges": [],
    }
    steps = [
        ToolCallPart(tool_name="preview", args={"node_id": "src"}),
        TextPart("Previewed."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    return run_agent("preview the source", g, get_deps(), model=FunctionModel(fn), policy=policy)


def test_hosted_default_preview_withholds_sample_values():
    # AC1: hosted model + no opt-in → no fixture cell value in tool results or transcript.
    from hub.agent_policy import resolve_agent_data_policy

    policy = resolve_agent_data_policy(
        None, model="anthropic/claude-opus-4-8", base_url=None)
    assert policy.hosted and not policy.allows_sample_values

    out = _run_preview_via_agent(policy=policy)
    preview = next(t for t in out["transcript"] if t["tool"] == "preview")
    result = preview["result"]
    assert result.get("columns")
    assert result.get("rows") == []
    assert "policy" in result and "metadata-only" in result["policy"]
    blob = json.dumps(out)
    assert not _contains_fixture_value(blob), blob


def test_sample_values_opt_in_and_local_endpoint_return_rows():
    # AC2: sample-values opt-in returns rows; marking the endpoint local also returns rows.
    from hub.agent_policy import resolve_agent_data_policy

    hosted_opt_in = resolve_agent_data_policy(
        {"level": "sample-values"}, model="anthropic/claude-opus-4-8", base_url=None)
    assert hosted_opt_in.allows_sample_values
    out = _run_preview_via_agent(policy=hosted_opt_in)
    preview = next(t for t in out["transcript"] if t["tool"] == "preview")
    rows = preview["result"].get("rows") or []
    assert 1 <= len(rows) <= 8
    assert _contains_fixture_value(json.dumps(rows))

    local = resolve_agent_data_policy(
        {"level": "metadata-only", "endpointIsLocal": True},
        model="ollama/llama3.3",
        base_url="http://127.0.0.1:11434/v1",
    )
    assert not local.hosted and local.allows_sample_values
    out_local = _run_preview_via_agent(policy=local)
    rows_local = next(t for t in out_local["transcript"] if t["tool"] == "preview")["result"]["rows"]
    assert 1 <= len(rows_local) <= 8


def test_agent_data_policy_admin_gate(monkeypatch):
    # AC3: in auth mode a non-admin cannot PUT the global policy key; an admin can.
    # Open/local mode keeps working without extra configuration.
    from hub import auth, metadb
    from hub.metadb import User, session

    # Open mode: anyone can write global settings.
    r = client.put("/api/settings", json={
        "scope": "global", "key": "agentDataPolicy",
        "value": {"level": "metadata-only", "endpointIsLocal": False},
    })
    assert r.status_code == 200

    with session() as s:
        bob = User(name="policy-bob")
        s.add(bob)
        s.flush()
        bob_id = bob.id
    assert metadb.is_admin(bob_id) is False
    assert metadb.is_admin("local") is True

    monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)
    client.cookies.clear()
    try:
        bob_headers = {"Cookie": f"dp_session={auth.sign(bob_id)}"}
        admin_headers = {"Cookie": f"dp_session={auth.sign('local')}"}
        denied = client.put("/api/settings", json={
            "scope": "global", "key": "agentDataPolicy",
            "value": {"level": "sample-values", "endpointIsLocal": False},
        }, headers=bob_headers)
        assert denied.status_code == 403

        ok = client.put("/api/settings", json={
            "scope": "global", "key": "agentDataPolicy",
            "value": {"level": "sample-values", "endpointIsLocal": False},
        }, headers=admin_headers)
        assert ok.status_code == 200
        stored = metadb.get_setting("agentDataPolicy", "global")
        assert stored["level"] == "sample-values"
    finally:
        client.cookies.clear()
        monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
        metadb.set_setting("agentDataPolicy", {"level": "metadata-only", "endpointIsLocal": False}, "global")


def test_sanitizer_is_central_enforcement_seam():
    # AC4: a tool result containing row-shaped data is scrubbed under metadata-only.
    from hub.agent_policy import sanitize_tool_result

    raw = {
        "columns": ["id", "image_url"],
        "row_count": 2,
        "rows": [
            {"id": 0, "image_url": "https://picsum.photos/seed/0/320/240"},
            {"id": 1, "image_url": "https://picsum.photos/seed/1/320/240"},
        ],
    }
    clean = sanitize_tool_result(raw, allows_sample_values=False)
    assert clean["rows"] == []
    assert clean["columns"] == ["id", "image_url"]
    assert clean["row_count"] == 2
    assert "metadata-only" in clean["policy"]
    assert "picsum.photos" not in json.dumps(clean)

    nested = {"preview": raw, "ok": True}
    nested_clean = sanitize_tool_result(nested, allows_sample_values=False)
    assert nested_clean["preview"]["rows"] == []
    assert "picsum.photos" not in json.dumps(nested_clean)

    passthrough = sanitize_tool_result(raw, allows_sample_values=True)
    assert passthrough["rows"][0]["image_url"].startswith("https://picsum.photos")


def test_catalog_reading_tools_emit_value_free_audit_events():
    # AC5: each catalog-reading tool under a hosted model produces an audit event without values.
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from hub import metadb
    from hub.agent import run_agent
    from hub.agent_policy import resolve_agent_data_policy

    before = {e["id"] for e in metadb.list_agent_egress_events(limit=500)}
    policy = resolve_agent_data_policy(None, model="anthropic/claude-opus-4-8", base_url=None)
    uri = _uri("images")
    steps = [
        ToolCallPart(tool_name="list_catalog", args={}),
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": uri}}),
        ToolCallPart(tool_name="preview", args={"node_id": "source_a1"}),
        ToolCallPart(tool_name="join_hints", args={"left_uri": uri, "right_uri": _uri("events")}),
        TextPart("Done."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    out = run_agent("inspect catalog", {"nodes": [], "edges": []}, get_deps(),
                    model=FunctionModel(fn), policy=policy)
    assert not _contains_fixture_value(json.dumps(out["transcript"]))

    events = [e for e in metadb.list_agent_egress_events(limit=500) if e["id"] not in before]
    tools = {e["tool"] for e in events}
    assert {"list_catalog", "preview", "join_hints"} <= tools
    assert "add_node" not in tools  # mutating tools are not catalog-reading audits
    blob = json.dumps(events)
    assert not _contains_fixture_value(blob), blob
    for e in events:
        assert e.get("provider") == "anthropic"
        assert e.get("model") == "anthropic/claude-opus-4-8"
        assert "rows" not in e
        assert "columns" in e


def test_agent_status_includes_disclosure():
    st = client.get("/api/agent").json()
    assert "disclosure" in st and "policy" in st
    d = st["disclosure"]
    assert "provider" in d and "model" in d
    assert "rowValuesMayLeave" in d
    assert d["level"] in ("metadata-only", "sample-values")
