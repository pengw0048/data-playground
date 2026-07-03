"""LLM-backed agent — an actor that BUILDS a real, typed dataflow graph on the canvas.

This is the optional "real LLM" planner (PRD §5.8 / FR-A3). It is **provider-agnostic**: it runs a
tool-use loop server-side through LiteLLM, so the model is chosen with DP_AGENT_MODEL — any provider
LiteLLM supports (anthropic/claude-*, openai/gpt-*, gemini/*, openrouter/*, bedrock/*, azure/*, or a
local ollama/* + DP_AGENT_BASE_URL). The matching provider key is read from the environment
(ANTHROPIC_API_KEY / OPENAI_API_KEY / …) and stays in the kernel, never the browser (NFR-4). Tools
add/connect/configure/preview nodes on a working copy of the graph; run_agent returns the finished
graph + a transcript. When no provider is configured the frontend falls back to the offline planner.
"""

from __future__ import annotations

import json

from kernel import graph as g
from kernel.executors.preview import preview_node
from kernel.models import Graph
from kernel.settings import settings


def agent_status() -> dict:
    """Whether the LLM agent is usable, and why not if not (provider-agnostic via LiteLLM)."""
    model = settings.agent_model
    try:
        import litellm
        installed = True
    except Exception:  # noqa: BLE001
        installed = False
    if not installed:
        return {"available": False, "model": model,
                "reason": "install the agent extra: pip install 'data-playground[agent]'"}
    # a local/self-hosted endpoint OR an explicit key override needs no env-var provider key
    preconfigured = bool(settings.agent_base_url) or bool(settings.agent_api_key)
    missing: list[str] = []
    if not preconfigured:
        try:
            missing = litellm.validate_environment(model).get("missing_keys") or []
        except Exception:  # noqa: BLE001
            missing = []
    available = preconfigured or not missing
    reason = "" if available else f"set {' or '.join(missing) or 'a provider API key'} to use model '{model}'"
    return {"available": available, "reason": reason, "model": model}


_SYSTEM = """\
You are the agent inside Data Playground — a node-based canvas for data ("like ComfyUI, but for \
typed columnar data"). You BUILD real, inspectable pipelines by calling tools that add nodes, \
connect them, and configure them. Nodes lower to a typed logical plan (a DuckDB relation), so the \
same graph runs on a preview sample or at full scale.

How to work:
- First call list_catalog and list_node_kinds to see the available datasets and node kinds.
- Every pipeline starts from a `source` node whose `uri` is a catalog table's uri.
- Connect nodes with `connect(source_id, target_id)`. Multi-input nodes (e.g. `join`) expose \
named input handles — pass target_handle for those.
- Configure nodes with the params shown by list_node_kinds. For a `filter`, set `predicate` to a \
SQL boolean expression over the columns. For `sql`, write a query using `input` as the table name. \
For `transform`, write a Python function `def fn(row): ...` (mode "map") that returns the row.
- Use `preview(node_id)` to SEE real sample rows and verify a step before continuing. Adapt to \
what the data actually looks like.
- Build the MINIMUM graph that achieves the user's outcome. Don't add nodes they didn't ask for.
- When the graph is complete, call `finish` with a one-sentence summary. If you cannot map the \
request to a pipeline, call finish and explain why.

Be concise. Prefer relational nodes (filter/select/sql/aggregate/join) over Python transforms when \
they suffice — they push down and run out-of-core."""


def _node_kinds(deps) -> list[dict]:
    out = []
    for spec in deps.node_specs.values():
        d = spec.model_dump(by_alias=False)
        out.append({
            "kind": d["kind"], "title": d.get("title"), "blurb": d.get("blurb", ""),
            "previewable": d.get("previewable", True),
            "inputs": [{"id": p["id"], "wire": p.get("wire"), "accepts": p.get("accepts")} for p in d.get("inputs", [])],
            "outputs": [{"id": p["id"], "wire": p.get("wire")} for p in d.get("outputs", [])],
            "params": [{"name": p["name"], "type": p["type"], "default": p.get("default"), "options": p.get("options")}
                       for p in d.get("params", [])],
        })
    return out


def _fn(name: str, description: str, params: dict) -> dict:
    # OpenAI-style function tool (the shape LiteLLM expects and normalizes across all providers)
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


def _tool_defs() -> list[dict]:
    return [
        _fn("list_catalog", "List the datasets registered in the local catalog (name, uri, columns).",
            {"type": "object", "properties": {}}),
        _fn("list_node_kinds", "List available node kinds with their params and input/output ports.",
            {"type": "object", "properties": {}}),
        _fn("add_node", "Add a node to the canvas. Returns its node_id and port handles.",
            {"type": "object", "properties": {
                "kind": {"type": "string"}, "title": {"type": "string"},
                "config": {"type": "object", "description": "param name -> value"}},
                "required": ["kind"]}),
        _fn("connect", "Connect one node's output to another node's input.",
            {"type": "object", "properties": {
                "source_id": {"type": "string"}, "target_id": {"type": "string"},
                "target_handle": {"type": "string", "description": "input handle id for multi-input nodes (e.g. join 'a'/'b')"}},
                "required": ["source_id", "target_id"]}),
        _fn("set_config", "Merge config values into an existing node.",
            {"type": "object", "properties": {
                "node_id": {"type": "string"}, "config": {"type": "object"}},
                "required": ["node_id", "config"]}),
        _fn("preview", "Preview a node over a small sample. Returns columns and up to 8 rows.",
            {"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]}),
        _fn("finish", "Finish and summarize what you built.",
            {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}),
    ]


def run_agent(outcome: str, graph: dict, deps) -> dict:
    """Run the tool-use loop; return {graph, transcript, summary}. Raises if the SDK/key is absent."""
    import litellm

    wg = {
        "id": graph.get("id", "canvas"), "version": graph.get("version", 1),
        "nodes": [dict(n) for n in graph.get("nodes", [])],
        "edges": [dict(e) for e in graph.get("edges", [])],
    }
    existing_ids = {n["id"] for n in wg["nodes"]}
    specs = deps.node_specs
    transcript: list[dict] = []
    seq = [0]

    def new_id(kind: str) -> str:
        seq[0] += 1
        return f"{kind}_a{seq[0]}"

    def find(nid: str):
        return next((n for n in wg["nodes"] if n["id"] == nid), None)

    def do_add(inp: dict) -> dict:
        kind = inp.get("kind")
        if kind not in specs:
            return {"error": f"unknown node kind '{kind}'. Call list_node_kinds."}
        spec = specs[kind]
        nid = new_id(kind)
        wg["nodes"].append({"id": nid, "type": kind, "position": {"x": 0, "y": 0},
                            "data": {"title": inp.get("title") or kind, "config": inp.get("config") or {}}})
        return {"node_id": nid,
                "inputs": [{"id": p.id, "wire": p.wire} for p in spec.inputs],
                "outputs": [{"id": p.id, "wire": p.wire} for p in spec.outputs]}

    def do_connect(inp: dict) -> dict:
        src, tgt = find(inp.get("source_id")), find(inp.get("target_id"))
        if not src or not tgt:
            return {"error": "source_id or target_id not found"}
        sspec = specs.get(src["type"])
        wire = sspec.outputs[0].wire if sspec and sspec.outputs else "dataset"
        th = inp.get("target_handle")
        # reject a second edge into an occupied single-input handle
        if any(e["target"] == tgt["id"] and (e.get("targetHandle") or None) == (th or None) for e in wg["edges"]):
            return {"error": f"input {th or 'in'} of {tgt['id']} is already connected"}
        wg["edges"].append({"id": new_id("e"), "source": src["id"], "target": tgt["id"],
                            "sourceHandle": None, "targetHandle": th, "data": {"wire": wire}})
        return {"ok": True}

    def do_set(inp: dict) -> dict:
        n = find(inp.get("node_id"))
        if not n:
            return {"error": "node_id not found"}
        n["data"].setdefault("config", {}).update(inp.get("config") or {})
        return {"ok": True}

    def do_preview(inp: dict) -> dict:
        nid = inp.get("node_id")
        if not find(nid):
            return {"error": "node_id not found"}
        try:
            res = preview_node(Graph(**wg), nid, 8, deps.resolve_adapter, deps.registry,
                               deps.node_lowerings, deps.node_specs)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        if res.not_previewable:
            return {"not_previewable": True, "reason": res.reason}
        if res.error:
            return {"error": res.reason}
        cols = [c.name for c in res.columns]
        return {"columns": cols, "rows": res.rows[:8], "row_count": res.row_count}

    dispatch = {"list_catalog": lambda _: {"tables": [
        {"name": t.name, "uri": t.uri, "columns": [c.name for c in t.columns]} for t in deps.catalog.list_tables(None)]},
        "list_node_kinds": lambda _: {"kinds": _node_kinds(deps)},
        "add_node": do_add, "connect": do_connect, "set_config": do_set, "preview": do_preview}

    ctx = (f"Outcome: {outcome}\n\nCurrent canvas has {len(wg['nodes'])} node(s) and "
           f"{len(wg['edges'])} edge(s). Build (or extend) a pipeline to achieve the outcome. "
           "Start by listing the catalog and node kinds.")
    # OpenAI-style message list (LiteLLM normalizes it for every provider): system prompt goes in
    # the list, not a separate kwarg.
    messages: list = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": ctx}]
    tools = _tool_defs()
    summary = "Done."
    extra: dict = {"drop_params": True}  # silently drop params a given provider doesn't accept
    if settings.agent_base_url:
        extra["api_base"] = settings.agent_base_url  # local / self-hosted OpenAI-compatible endpoint
    if settings.agent_api_key:
        extra["api_key"] = settings.agent_api_key

    for _ in range(settings.agent_max_steps):
        resp = litellm.completion(model=settings.agent_model, max_tokens=4096,
                                  messages=messages, tools=tools, tool_choice="auto", **extra)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:  # no tool calls → the model is done
            summary = (msg.content or "").strip() or summary
            break
        messages.append(msg)  # carry the assistant turn (with its tool_calls) back into history
        finished = False
        for tc in calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")  # arguments is a JSON string
            except Exception:  # noqa: BLE001
                args = {}
            if not isinstance(args, dict):  # a weaker model may emit non-object JSON — don't crash
                args = {}
            if name == "finish":
                summary = args.get("summary", summary)
                finished = True
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": name, "content": "ok"})
                continue
            out = dispatch.get(name, lambda _: {"error": "unknown tool"})(args)
            transcript.append({"tool": name, "input": args, "result": out})
            # OpenAI turn structure: one tool message per tool_call, keyed by tool_call_id.
            # default=str guards against odd nested cell types leaking into a tool result.
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": name,
                             "content": json.dumps(out, default=str)[:4000]})
        if finished:
            break

    _layout(wg, existing_ids)
    return {"graph": wg, "transcript": transcript, "summary": summary}


def _layout(wg: dict, keep_ids: set) -> None:
    """Assign positions to newly-added nodes via a left-to-right topological layering, placed
    below any pre-existing content so the agent's build never overlaps the user's nodes."""
    new = [n for n in wg["nodes"] if n["id"] not in keep_ids]
    if not new:
        return
    old = [n for n in wg["nodes"] if n["id"] in keep_ids]
    base_y = (max((n["position"]["y"] for n in old), default=0) + 280) if old else 80
    base_x = (min((n["position"]["x"] for n in old), default=80)) if old else 80

    # depth = longest path from a root, within the new nodes
    parents: dict[str, list[str]] = {n["id"]: [] for n in new}
    idset = {n["id"] for n in new}
    for e in wg["edges"]:
        if e["target"] in idset and e["source"] in idset:
            parents[e["target"]].append(e["source"])
    depth: dict[str, int] = {}

    def d(nid: str, seen=None) -> int:
        seen = seen or set()
        if nid in depth:
            return depth[nid]
        if nid in seen or not parents.get(nid):
            depth[nid] = 0
            return 0
        depth[nid] = 1 + max(d(p, seen | {nid}) for p in parents[nid])
        return depth[nid]

    per_col: dict[int, int] = {}
    for n in new:
        col = d(n["id"])
        row = per_col.get(col, 0)
        per_col[col] = row + 1
        n["position"] = {"x": base_x + col * 280, "y": base_y + row * 170}
