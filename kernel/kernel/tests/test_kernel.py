"""End-to-end kernel tests — the real out-of-core lowering engine on real files."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from kernel.deps import get_deps
from kernel.main import app

client = TestClient(app)


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def N(nid, t, cfg):
    return {"id": nid, "type": t, "position": {"x": 0, "y": 0}, "data": {"title": nid, "config": cfg}}


def E(s, tg, sh=None, th=None):
    return {"id": f"{s}-{tg}", "source": s, "target": tg, "sourceHandle": sh, "targetHandle": th,
            "data": {"wire": "dataset"}}


def _poll(run_id, tries=150):
    for _ in range(tries):
        st = client.get(f"/api/run/{run_id}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.1)
    return st


def test_kernel_info():
    info = client.get("/api/kernel").json()
    assert info["backend"] == "duckdb+polars+arrow"
    assert "duckdb" in info["adapters"] and "lance" in info["adapters"]
    assert info["runners"] == ["local-out-of-core"]
    assert {"media", "vector"} <= set(info["capabilities"])


def test_nodes_endpoint():
    specs = {s["kind"]: s for s in client.get("/api/nodes").json()}
    assert {"source", "filter", "select", "transform", "sql", "join", "aggregate", "sort",
            "dedup", "write", "metric", "vector-search"} <= set(specs)
    assert specs["aggregate"]["previewable"] is False
    assert specs["filter"]["params"][0]["name"] == "predicate"


def test_catalog_and_capabilities():
    tabs = {t["name"]: t for t in client.get("/api/catalog/tables").json()}
    assert {"images", "movies", "events"} <= set(tabs)
    caps = {c["name"]: c["capabilities"] for c in tabs["images"]["columns"]}
    assert "media" in caps["image_url"] and "vector" in caps["embedding"]
    assert tabs["images"]["rowCount"] == 500


def test_sample():
    r = client.post("/api/data/sample", json={"uri": _uri("images"), "k": 5}).json()
    assert len(r["rows"]) == 5 and r["rowCount"] == 500 and r["truncated"] is True


def test_preview_pipeline_derives_and_sorts():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("flt", "filter", {"predicate": "is_valid = true"}),
        N("sel", "select", {"select": "id, width, height, width*height AS area"}),
        N("srt", "sort", {"by": "area DESC"}),
    ], "edges": [E("src", "flt"), E("flt", "sel"), E("sel", "srt")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "srt", "k": 10}).json()
    assert not r["notPreviewable"]
    assert "area" in [c["name"] for c in r["columns"]]
    areas = [row["area"] for row in r["rows"]]
    assert areas == sorted(areas, reverse=True)


def test_aggregate_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("agg", "aggregate", {"groupBy": "format", "aggs": "count(*) AS n"}),
    ], "edges": [E("src", "agg")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "agg", "k": 10}).json()
    assert r["notPreviewable"] is True and "full pass" in r["reason"]


def test_transform_arrow_batches():
    code = "def fn(row):\n    row['area'] = row['width'] * row['height']\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
    ], "edges": [E("src", "xf")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "xf", "k": 20}).json()
    assert not r["notPreviewable"]
    assert all(row["area"] == row["width"] * row["height"] for row in r["rows"])


def test_join_and_sql():
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("events")}),
        N("j", "join", {"on": "user_id", "how": "inner"}),
        N("q", "sql", {"sql": "SELECT count(*) AS n FROM input"}),
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b"), E("j", "q")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "q", "k": 5}).json()
    assert not r["notPreviewable"] and r["rows"][0]["n"] > 0


def test_dedup_and_metric():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("sel", "select", {"select": "event"}),
        N("dd", "dedup", {}),
    ], "edges": [E("src", "sel"), E("sel", "dd")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "dd", "k": 50}).json()
    assert len(r["rows"]) == 4  # view/click/purchase/signup


def test_run_write_and_lineage():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("flt", "filter", {"predicate": "is_valid = true"}),
        N("wr", "write", {"name": "images_valid", "format": "parquet"}),
    ], "edges": [E("src", "flt"), E("flt", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    st = _poll(r["runId"])
    assert st["status"] == "done" and st["outputTable"] == "images_valid"
    assert st["totalRows"] and st["totalRows"] < 500  # filtered out invalids
    lin = client.get("/api/catalog/lineage", params={"uri": _uri("images")}).json()
    assert any(e["child"].endswith("images_valid.parquet") for e in lin["edges"])


def test_write_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("wr", "write", {"name": "x"}),
    ], "edges": [E("src", "wr")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "wr", "k": 10}).json()
    assert r["notPreviewable"] is True


def test_register_endpoint():
    uri = _uri("movies")
    t = client.post("/api/catalog/register", json={"uri": uri, "name": "movies_again"}).json()
    assert t["name"] == "movies_again" and t["rowCount"] == 200


# --------------------------------------------------------------------------- #
# Regression tests for adversarial-acceptance findings
# --------------------------------------------------------------------------- #
def test_aggregate_keeps_group_key():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("agg", "aggregate", {"groupBy": "event", "aggs": "count(*) AS n"}),
        N("wr", "write", {"name": "agg_out"}),
    ], "edges": [E("src", "agg"), E("agg", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_agg_out").uri, "k": 10}).json()
    assert "event" in [c["name"] for c in out["columns"]]  # group key retained
    assert "n" in [c["name"] for c in out["columns"]]


def test_metric_preview_is_true_value(tmp_path):
    import duckdb
    p = str(tmp_path / "big5k.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT 1 AS v FROM range(0,5000)) TO '{p}' (FORMAT PARQUET)")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [E("src", "m")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5}).json()
    assert not r["notPreviewable"] and not r.get("error")
    assert r["rows"][0]["value"] == 5000.0  # TRUE count over 5000 rows, not the 2000 preview sample


def test_join_duplicate_columns_preserved():
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("events")}),
        N("b", "source", {"uri": _uri("images")}),
        N("j", "join", {"how": "inner"}),  # no key → cross join; both have 'id'
    ], "edges": [E("a", "j", None, "a"), E("b", "j", None, "b")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "j", "k": 3}).json()
    names = [c["name"] for c in r["columns"]]
    assert "id" in names and "id_2" in names           # both id columns kept, de-duped
    assert all(len(row) == len(names) for row in r["rows"])  # no column dropped


def test_cancel_finished_run_stays_done():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("f", "filter", {"predicate": "amount > 1"}),
    ], "edges": [E("src", "f")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "f", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    after = client.post(f"/api/run/{r['runId']}/cancel").json()
    assert after["status"] == "done"  # a finished run is never relabeled cancelled


def test_typecheck_rejects_incompatible_connection():
    # metric outputs `metric`; aggregate's input accepts only `dataset` → must be rejected server-side
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("m", "metric", {"agg": "count"}),
        N("agg", "aggregate", {"aggs": "count(*) AS n"}),
    ], "edges": [E("src", "m"), {"id": "bad", "source": "m", "target": "agg", "data": {"wire": "metric"}}]}
    assert client.post("/api/run", json={"graph": g, "targetNodeId": "agg", "confirmed": True}).status_code == 400


def test_metric_edge_wire_does_not_422():
    # models.WireType must include 'metric'/'value' so an edge tagged with them parses (not a 422);
    # the tag is cosmetic (type-checking recomputes from specs), so source→metric stays valid.
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [{"id": "sm", "source": "src", "target": "m", "sourceHandle": None,
                  "targetHandle": None, "data": {"wire": "metric"}}]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5})
    assert r.status_code == 200  # not 422


def test_decimal_serialized_as_number():
    r = client.post("/api/data/sample", json={"uri": _uri("events"), "k": 3}).json()
    amounts = [row["amount"] for row in r["rows"]]
    assert all(isinstance(a, (int, float)) for a in amounts)  # not strings


def test_plugin_run_applies_lowering(tmp_path):
    # the critical bug: plugin lowerings were dropped on a full run → untransformed writes
    from kernel.sdk import NodeSpec, PortSpec, ParamSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="const42", title="const42", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[])
    deps.node_specs[spec.kind] = spec
    deps.node_lowerings[spec.kind] = lambda engine, node, inputs: ctx.sql(inputs[0], "SELECT *, 42 AS c FROM {input}")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        N("p", "const42", {}),
        N("wr", "write", {"name": "plugin_out"}),
    ], "edges": [E("src", "p"), E("p", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    assert _poll(r["runId"])["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_plugin_out").uri, "k": 3}).json()
    assert "c" in [c["name"] for c in out["columns"]]           # plugin lowering was applied
    assert all(row["c"] == 42 for row in out["rows"])           # transformed, not passthrough


# --------------------------------------------------------------------------- #
# Regression tests for code-review findings (concurrency / correctness / security)
# --------------------------------------------------------------------------- #
def test_two_sql_nodes_no_view_collision():
    # two sql nodes in one graph both use `FROM input` — must not clobber each other (finding #1)
    g = {"id": "c", "version": 1, "nodes": [
        N("a", "source", {"uri": _uri("images")}),
        N("qa", "sql", {"sql": "SELECT id FROM input"}),
        N("b", "source", {"uri": _uri("events")}),
        N("qb", "sql", {"sql": "SELECT id FROM input"}),
    ], "edges": [E("a", "qa"), E("b", "qb")]}
    ra = client.post("/api/run/preview", json={"graph": g, "nodeId": "qa", "k": 5}).json()
    rb = client.post("/api/run/preview", json={"graph": g, "nodeId": "qb", "k": 5}).json()
    assert not ra["notPreviewable"] and not rb["notPreviewable"]
    assert [c["name"] for c in ra["columns"]] == ["id"]  # both resolve their OWN input


def test_concurrent_previews_do_not_corrupt():
    # the shared DuckDB connection + unique views must survive concurrent evaluation (findings #2/#3)
    import concurrent.futures as cf
    from kernel.deps import get_deps
    from kernel.executors.preview import preview_node
    deps = get_deps()

    def graph_for(uri, pred):
        return __import__("kernel.models", fromlist=["Graph"]).Graph(**{
            "id": "c", "version": 1,
            "nodes": [N("src", "source", {"uri": uri}), N("f", "filter", {"predicate": pred}),
                      N("d", "dedup", {})],
            "edges": [E("src", "f"), E("f", "d")],
        })

    imgs, evs = _uri("images"), _uri("events")

    def run(i):
        if i % 2 == 0:
            r = preview_node(graph_for(imgs, "is_valid = true"), "d", 20,
                             deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs)
            return "images", all(row.get("is_valid") for row in r.rows), r.not_previewable
        r = preview_node(graph_for(evs, "amount > 1"), "d", 20,
                         deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs)
        return "events", all(row.get("amount", 0) > 1 for row in r.rows), r.not_previewable

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, range(24)))
    assert all(ok and not npv for _, ok, npv in results)  # no cross-contamination, no crash


def test_sandbox_blocks_dunder_escape():
    # the classic ().__class__.__mro__ escape must be rejected (finding #4)
    code = "def fn(row):\n    row['x'] = ().__class__.__mro__[-1].__subclasses__()\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
    ], "edges": [E("src", "xf")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "xf", "k": 5}).json()
    assert r["error"] or r["notPreviewable"]  # blocked, not executed
    assert "__" in (r.get("reason") or "") or "allowed" in (r.get("reason") or "")


def test_write_mode_append_rejected_not_silent_overwrite():
    # append/merge must error rather than silently overwrite (finding #7)
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("wr", "write", {"name": "append_test", "writeMode": "append"}),
    ], "edges": [E("src", "wr")]}
    r = client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()
    st = _poll(r["runId"])
    assert st["status"] == "failed" and "append" in (st.get("error") or "").lower()


def test_metric_over_transform_upstream_not_previewable():
    # a metric whose upstream has a Python transform must refuse preview, not spill all rows (finding #6)
    code = "def fn(row):\n    row['w2'] = row['width'] * 2\n    return row"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("xf", "transform", {"source": "adhoc", "mode": "map", "code": code}),
        N("m", "metric", {"agg": "count"}),
    ], "edges": [E("src", "xf"), E("xf", "m")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "m", "k": 5}).json()
    assert r["notPreviewable"] and "full pass" in (r["reason"] or "")


def test_agent_status_and_fallback_without_key(monkeypatch):
    # with no provider key (agent_model defaults to anthropic/*) the endpoint reports unavailable
    # and POST returns available:false (frontend then falls back to the offline planner) — never 500
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = client.get("/api/agent").json()
    assert st["available"] is False and st["reason"]
    act = client.post("/api/agent", json={"outcome": "sample images", "graph": {"nodes": [], "edges": []}}).json()
    assert act["available"] is False


def test_agent_builds_graph_via_tool_loop():
    # Drive run_agent with a Pydantic AI FunctionModel — no network, fully offline, and it exercises
    # the REAL add_node/connect dispatch + layout through pydantic-ai's actual tool loop. Ids are
    # deterministic (source_a1, filter_a2); the final text output becomes the summary (no finish tool).
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kernel.agent import run_agent

    steps = [
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": _uri("images")}}),
        ToolCallPart(tool_name="add_node", args={"kind": "filter", "config": {"predicate": "is_valid = true"}}),
        ToolCallPart(tool_name="connect", args={"source_id": "source_a1", "target_id": "filter_a2"}),
        TextPart("Built source -> filter."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    out = run_agent("filter images where valid", {"nodes": [], "edges": []}, get_deps(),
                    model=FunctionModel(fn))
    kinds = [n["type"] for n in out["graph"]["nodes"]]
    assert kinds == ["source", "filter"]
    assert len(out["graph"]["edges"]) == 1
    assert out["summary"] == "Built source -> filter."
    xs = [n["position"]["x"] for n in out["graph"]["nodes"]]
    assert xs[0] != xs[1]  # layout gave distinct columns (source col 0, filter col 1)
    assert [t["tool"] for t in out["transcript"]] == ["add_node", "add_node", "connect"]


def test_agent_status_honors_explicit_api_key(monkeypatch):
    # an explicit DP_AGENT_API_KEY override must make the agent available even with no env-var key
    # (regression: status previously only checked agent_base_url and mis-reported unavailable)
    from kernel.agent import agent_status
    from kernel.settings import settings
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "agent_api_key", "sk-explicit-override")
    assert agent_status()["available"] is True


def test_agent_recovers_from_tool_error_and_summarizes():
    # A tool that returns an {"error": ...} dict (here: connect before any node exists) must NOT crash
    # the run — the model sees the error, recovers, and finishes with a plain-text summary. Also proves
    # the failed connect left no dangling edge and was recorded in the transcript.
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kernel.agent import run_agent

    steps = [
        ToolCallPart(tool_name="connect", args={"source_id": "nope", "target_id": "nah"}),  # error, no crash
        ToolCallPart(tool_name="add_node", args={"kind": "source", "config": {"uri": _uri("images")}}),
        TextPart("Recovered and added a source."),
    ]
    calls = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        part = steps[calls["i"]]
        calls["i"] += 1
        return ModelResponse(parts=[part])

    out = run_agent("build something", {"nodes": [], "edges": []}, get_deps(), model=FunctionModel(fn))
    assert [n["type"] for n in out["graph"]["nodes"]] == ["source"]
    assert out["graph"]["edges"] == []  # the failed connect added no dangling edge
    assert out["summary"] == "Recovered and added a source."
    assert any(t["tool"] == "connect" and "error" in t["result"] for t in out["transcript"])


def test_plugin_node_lowering():
    # simulate a plugin registering a typed node via the SDK contract
    from kernel.sdk import NodeSpec, PortSpec, ParamSpec, ctx
    deps = get_deps()
    spec = NodeSpec(kind="upper_fmt", title="upper", category="compute",
                    inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                    params=[ParamSpec(name="column", type="string", default="format")])
    def lower(engine, node, inputs):
        col = node.data.get("config", {}).get("column", "format")
        return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM _')
    deps.node_specs[spec.kind] = spec
    deps.node_lowerings[spec.kind] = lower

    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("images")}),
        N("up", "upper_fmt", {"column": "format"}),
    ], "edges": [E("src", "up")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "up", "k": 5}).json()
    assert not r["notPreviewable"]
    assert all(row["format"] in ("PNG", "JPG") for row in r["rows"])


# --------------------------------------------------------------------------- #
# Metadata DB: users, per-user canvases (multi-file), settings
# --------------------------------------------------------------------------- #
def test_me_defaults_to_local_user():
    me = client.get("/api/me").json()
    assert me["id"] == "local"


def test_users_create_and_list():
    before = {u["id"] for u in client.get("/api/users").json()}
    created = client.post("/api/users", json={"name": "Alice", "email": "a@x.io"}).json()
    assert created["name"] == "Alice"
    ids = {u["id"] for u in client.get("/api/users").json()}
    assert created["id"] in ids and "local" in ids and created["id"] not in before


def test_canvas_crud_is_per_user():
    doc = {"id": "cv1", "name": "My Canvas", "version": 3, "nodes": [], "edges": []}
    r = client.put("/api/canvas/cv1", json=doc).json()
    assert r["ok"]
    listing = client.get("/api/canvas").json()
    assert any(c["id"] == "cv1" and c["name"] == "My Canvas" and c["version"] == 3 for c in listing)
    assert client.get("/api/canvas/cv1").json()["name"] == "My Canvas"
    # a different user cannot see it
    other = client.post("/api/users", json={"name": "Bob"}).json()["id"]
    assert client.get("/api/canvas", headers={"X-DP-User": other}).json() == []
    assert client.get("/api/canvas/cv1", headers={"X-DP-User": other}).status_code == 404
    # delete
    client.delete("/api/canvas/cv1")
    assert client.get("/api/canvas/cv1").status_code == 404


def test_settings_global_and_user_scope():
    client.put("/api/settings", json={"scope": "global", "key": "agentModel", "value": "openai/gpt-4o"})
    u = client.post("/api/users", json={"name": "Carol"}).json()["id"]
    client.put("/api/settings", json={"scope": "user", "key": "theme", "value": "dark"}, headers={"X-DP-User": u})
    g = client.get("/api/settings").json()
    assert g["global"]["agentModel"] == "openai/gpt-4o"
    # Carol sees her user setting; the default user does not
    assert client.get("/api/settings", headers={"X-DP-User": u}).json()["user"].get("theme") == "dark"
    assert client.get("/api/settings").json()["user"].get("theme") is None


def test_agent_activates_from_settings_key(monkeypatch):
    # setting a provider key via /settings (the UI path) must make the agent available — no env var
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/agent").json()["available"] is False
    client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": "sk-from-ui"})
    try:
        assert client.get("/api/agent").json()["available"] is True
    finally:
        client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": ""})


# --------------------------------------------------------------------------- #
# Meta-programming: a `section` = a driver script over contained nodes (bounded control flow)
# --------------------------------------------------------------------------- #
def _section(nid, script, subnodes, params=None, max_runs=200):
    return N(nid, "section", {"script": script, "subnodes": subnodes,
                              "params": params or {}, "maxRuns": max_runs})


def _seq_parquet(tmp_path, n=1000):
    import duckdb
    p = str(tmp_path / "seq.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT i AS v FROM range(0,{n}) t(i)) TO '{p}' (FORMAT PARQUET)")
    return p


def test_section_for_each_over_a_list(tmp_path):
    # for-each: run a filter per predicate in a list, concat the results (graph isn't fixed — a `for`)
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("parts = []\n"
              "for pred in params['preds']:\n"
              "    parts.append(run(f, data=inputs['in'], predicate=pred))\n"
              "emit(concat(parts))\n")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, [{"alias": "f", "type": "filter", "config": {}}],
                 {"preds": ["v >= 0 AND v < 100", "v >= 900"]}),  # 100 + 100 rows, disjoint
        N("wr", "write", {"name": "sec_foreach"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done" and st["outputTable"] == "sec_foreach"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_foreach").uri, "k": 5}).json()
    assert out["rowCount"] == 200  # concat of the two per-predicate runs


def test_section_iterate_until_condition(tmp_path):
    # iterate-until: shrink the dataset each pass; stop when a metric crosses a threshold (a while+if)
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("state = inputs['in']\n"
              "for i in range(params['max_iters']):\n"
              "    state = run(shrink, data=state, predicate='v >= %d' % (i * 200))\n"
              "    if value(run(cnt, data=state)) < params['target']:\n"
              "        break\n"
              "emit(state)\n")
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, [
            {"alias": "shrink", "type": "filter", "config": {}},
            {"alias": "cnt", "type": "metric", "config": {"agg": "count"}},
        ], {"max_iters": 10, "target": 300}),
        N("wr", "write", {"name": "sec_iter"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_iter").uri, "k": 5}).json()
    # v>=0(1000) v>=200(800) v>=400(600) v>=600(400) v>=800(200 < 300 → break) → 200 rows
    assert out["rowCount"] == 200


def test_section_multi_output_routes_by_port(tmp_path):
    # a section can emit several named output ports; each downstream node is wired to one port
    # (source_handle) and must receive exactly that port's rows — ComfyUI/Weave-style multi-output.
    p = _seq_parquet(tmp_path)  # v = 0..999
    script = ("emit('low', run(f, data=inputs['in'], predicate='v < 100'))\n"    # 100 rows
              "emit('high', run(f, data=inputs['in'], predicate='v >= 900'))\n")  # 100 rows
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}),
        _section("sec", script, [{"alias": "f", "type": "filter", "config": {}}]),
        N("wl", "write", {"name": "sec_low"}),
        N("wh", "write", {"name": "sec_high"}),
    ], "edges": [E("src", "sec"), E("sec", "wl", sh="low"), E("sec", "wh", sh="high")]}
    _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wl", "confirmed": True}).json()["runId"])
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wh", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    import duckdb
    def stats(name):
        uri = get_deps().catalog.get_table(f"tbl_{name}").uri
        return duckdb.connect(":memory:").execute(f"SELECT count(*), min(v), max(v) FROM read_parquet('{uri}')").fetchone()
    # assert CONTENT, not just row count — both ports are 100 rows, so a handle-routing bug that
    # writes the wrong port's data would pass a count-only check but fail here (min/max differ).
    assert stats("sec_low") == (100, 0, 99)      # the "low" port: v < 100
    assert stats("sec_high") == (100, 900, 999)  # the "high" port: v >= 900


def test_section_runs_its_parentid_children(tmp_path):
    # visual containment: a canvas node whose parentId is the section is a callable child — its
    # alias is its title, so the driver calls run("keep", …). No form-declared subnodes needed.
    p = _seq_parquet(tmp_path)  # v = 0..999
    child = {"id": "child1", "type": "filter", "parentId": "sec", "position": {"x": 0, "y": 0},
             "data": {"title": "keep", "config": {"predicate": "v < 300"}}}
    sec = {"id": "sec", "type": "section", "position": {"x": 0, "y": 0},
           "data": {"title": "sec", "config": {"script": "emit(run('keep', data=inputs['in']))\n",
                                               "subnodes": [], "params": {}, "maxRuns": 50}}}
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": p}), sec, child, N("wr", "write", {"name": "sec_parent"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "done"
    out = client.post("/api/data/sample", json={"uri": get_deps().catalog.get_table("tbl_sec_parent").uri, "k": 5}).json()
    assert out["rowCount"] == 300  # the contained 'keep' filter kept v < 300


def test_section_not_previewable():
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        _section("sec", "emit(inputs['in'])", []),
    ], "edges": [E("src", "sec")]}
    r = client.post("/api/run/preview", json={"graph": g, "nodeId": "sec", "k": 5}).json()
    assert r["notPreviewable"] is True


def test_section_maxruns_is_bounded():
    # an unbounded-looking loop must fail closed at maxRuns, not run away
    script = "while True:\n    run(f, data=inputs['in'], predicate='amount > 0')\n"
    g = {"id": "c", "version": 1, "nodes": [
        N("src", "source", {"uri": _uri("events")}),
        _section("sec", script, [{"alias": "f", "type": "filter", "config": {}}], max_runs=3),
        N("wr", "write", {"name": "sec_runaway"}),
    ], "edges": [E("src", "sec"), E("sec", "wr")]}
    st = _poll(client.post("/api/run", json={"graph": g, "targetNodeId": "wr", "confirmed": True}).json()["runId"])
    assert st["status"] == "failed" and "maxRuns" in (st.get("error") or "")
