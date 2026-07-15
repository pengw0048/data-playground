"""LLM-backed agent — an actor that BUILDS a real, typed dataflow graph on the canvas.

This is the optional "real LLM" planner. It is **provider-agnostic** and the
tool-use loop runs **in-process via Pydantic AI** — no `claude` CLI, no sidecar proxy. The model is
chosen with DP_AGENT_MODEL: any Pydantic AI provider (openai/anthropic/google/groq/mistral/cohere/
bedrock/…) or any OpenAI-compatible endpoint (a local Ollama, a gateway) via DP_AGENT_BASE_URL. The
matching provider key is read from the environment (ANTHROPIC_API_KEY / OPENAI_API_KEY / …) or the
UI settings and stays in the kernel, never the browser (NFR-4). LiteLLM is kept only to detect which
provider key is configured (agent_status). Tools add/connect/configure/preview nodes on a working
copy of the graph; run_agent returns the (possibly unchanged) graph + a transcript. There is no
plan/build mode — the model decides per message whether to just answer or to call the mutating
tools; the frontend applies the graph only when it actually did. With no provider configured the
agent is simply unavailable (no rule-based stand-in).

Hosted-model tool results pass through the workspace ``AgentDataPolicy`` sanitizer (SEC-01): the
default is metadata-only, so sample row values never leave unless an admin opts into sample-values
or marks the configured endpoint as local.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hub import graph_ops
from hub.agent_policy import load_agent_data_policy, record_tool_audit, sanitize_tool_result
from hub.executors.preview import preview_node
from hub.models import Graph
from hub.settings import settings


AGENT_CREDENTIAL_ERROR_CODE = "agent_credential_unavailable"
AGENT_CREDENTIAL_ERROR_REASON = (
    "The configured Agent credential is unavailable. Update it or clear the selection in Settings."
)


class AgentCredentialError(RuntimeError):
    """Safe public failure for any explicitly configured Agent credential that cannot be used."""

    code = AGENT_CREDENTIAL_ERROR_CODE

    def __init__(self) -> None:
        super().__init__(AGENT_CREDENTIAL_ERROR_REASON)


def _agent_semantic_config() -> tuple[str, str | None]:
    """Non-secret Agent model/endpoint settings used in both success and error responses."""
    from hub import metadb
    return (
        metadb.get_setting("agentModel", "global") or settings.agent_model,
        metadb.get_setting("agentBaseUrl", "global") or settings.agent_base_url,
    )


def _agent_provider(model: str) -> str:
    provider = model.split("/", 1)[0].split(":", 1)[0].lower()
    return {"gemini": "google"}.get(provider, provider)


def _agent_model_name(model: str) -> str:
    for separator in ("/", ":"):
        if separator in model:
            return model.split(separator, 1)[1]
    return model


def _agent_config() -> tuple[str, str | None, str | None]:
    """Resolve (model, api_key, base_url): global DB settings (set in the UI) override env/defaults.

    The agent's API key is stored as a secret reference (``env:…`` / ``file:…``) on the referenced
    ``agent`` cred (or the legacy ``agentApiKey`` setting); the material value is resolved here and
    never written back into settings.
    """
    from hub import metadb
    from hub.secrets import SecretResolveError, resolve_secret_value
    model, base_url = _agent_semantic_config()
    try:
        stored_key = metadb.cred_agent_api_key_ref()
        api_key = resolve_secret_value(stored_key, allow_plaintext=False) if stored_key else None
        if stored_key and (not isinstance(api_key, str) or not api_key):
            raise SecretResolveError("configured Agent credential resolved to an empty value")
    except (metadb.CredResolutionError, SecretResolveError) as exc:
        # Never expose a Cred id, env variable, file path, or resolver error to status/execution APIs.
        # Most importantly, do not reinterpret an explicit failure as permission to use an ambient key.
        raise AgentCredentialError() from exc
    if not stored_key:
        api_key = settings.agent_api_key or None
    if api_key and not base_url and _agent_provider(model) not in _AGENT_API_KEY_PROVIDERS:
        # A configured key with no supported provider binding would otherwise be silently ignored by
        # infer_model(), which could continue under a different ambient identity.
        raise AgentCredentialError()
    return model, api_key, base_url


def agent_credential_error_status() -> dict:
    """The one non-secret wire contract for credential failures across status and execution."""
    model, base_url = _agent_semantic_config()
    policy = load_agent_data_policy(model=model, base_url=base_url)
    disclosure = policy.disclosure()
    return {
        "available": False,
        "errorCode": AGENT_CREDENTIAL_ERROR_CODE,
        "reason": AGENT_CREDENTIAL_ERROR_REASON,
        "model": model,
        "provider": policy.provider,
        "policy": disclosure,
        "disclosure": disclosure,
    }


def agent_status() -> dict:
    """Whether the LLM agent is usable, why not if not, and the active data-egress disclosure."""
    try:
        model, api_key, base_url = _agent_config()
    except AgentCredentialError:
        return agent_credential_error_status()
    policy = load_agent_data_policy(model=model, base_url=base_url)
    disclosure = policy.disclosure()
    try:
        import pydantic_ai  # noqa: F401  — the in-process harness
    except Exception:  # noqa: BLE001
        return {"available": False, "model": model, "provider": policy.provider,
                "reason": "install the agent extra: uv pip install -e 'kernel[agent]'",
                "policy": disclosure, "disclosure": disclosure}
    # a local/self-hosted endpoint OR an explicit key (env or UI setting) needs no env-var provider key
    preconfigured = bool(base_url) or bool(api_key)
    missing: list[str] = []
    if not preconfigured:
        try:
            import litellm
            missing = litellm.validate_environment(model).get("missing_keys") or []
        except Exception:  # noqa: BLE001
            missing = []
    available = preconfigured or not missing
    reason = "" if available else f"set {' or '.join(missing) or 'a provider API key'} to use model '{model}'"
    return {
        "available": available,
        "reason": reason,
        "model": model,
        "provider": policy.provider,
        "policy": disclosure,
        "disclosure": disclosure,
    }


_SYSTEM = """\
You are the agent inside Data Playground — a node-based canvas for data ("like ComfyUI, but for \
typed columnar data"). You can do two things, and you decide which fits each message:
  1. Just answer / advise / think an approach through — reply in text. Use only the read-only \
tools (list_catalog, list_node_kinds, preview) if you need to look first.
  2. Build or change the canvas — call add_node / connect / set_config to construct real, \
inspectable typed nodes. Nodes build a typed logical plan (a DuckDB relation), so the same \
graph runs on a preview sample or at full scale.

Decide from the message: if the user is asking a question, exploring, or doesn't clearly want the \
canvas changed, answer in text and DON'T call the mutating tools (add_node/connect/set_config). \
Call those only when they want the canvas built or modified. When in doubt, propose the plan in \
words and let them ask you to build it.

When you DO build:
- Call list_catalog and list_node_kinds first to see the datasets (with their columns + primary-key \
candidates) and node kinds.
- Every pipeline starts from a `source` node whose `uri` is a catalog table's uri.
- Connect with `connect(source_id, target_id)`. Multi-input nodes (e.g. `join`) expose named \
input handles — pass target_handle.
- BEFORE joining two datasets, call `join_hints(left_uri, right_uri)` — don't guess the key. It \
gives the right key column(s) and the MEASURED cardinality. A 1:N / N:M join multiplies rows, so if \
you then need one row per parent, add an `aggregate`. Set the join's `on` (same-named keys) or \
`condition` (`a.x = b.y` for differing names).
- Configure with the params shown by list_node_kinds. For a `filter`, set `predicate` to a SQL \
boolean expression over the columns. For `sql`, write a query using `input` as the table name. For \
`transform`, write a Python function `def fn(row): ...` (mode "map") that returns the row.
- Use `preview(node_id)` to verify a step. Under the default metadata-only egress policy it may \
return columns and row count without sample values — that is intentional. Adapt using metadata \
when values are withheld.
- Build the MINIMUM graph that achieves the outcome. Don't add nodes they didn't ask for.
- Before you finish, call `validate` to confirm there are no typed-wire errors and no unintended \
join fan-out. Then STOP calling tools and reply with a one-sentence summary of what you built.

Be concise. Prefer relational nodes (filter/select/sql/aggregate/join) over Python transforms when \
they suffice — they push down and run out-of-core."""


# --------------------------------------------------------------------------- #
# The agent: a Pydantic AI tool-use loop over a working copy of the graph.
# Deps carry the per-run state (the working graph + the kernel deps); the tools mutate it.
# --------------------------------------------------------------------------- #
@dataclass
class _Ctx:
    kdeps: Any            # kernel Deps: catalog / node_specs / resolve_adapter / registry / node_builders
    wg: dict              # working graph {id, version, nodes, edges}
    seq: list             # id counter [int]
    transcript: list      # [{tool, input, result}] for the UI
    policy: Any           # resolved AgentDataPolicy for this run
    principal_id: str | None
    request_id: str | None


def _new_id(ctx: _Ctx, kind: str) -> str:
    ctx.seq[0] += 1
    return f"{kind}_a{ctx.seq[0]}"


def _finish(ctx: _Ctx, tool: str, tool_input: dict, result: dict) -> dict:
    """Sanitize → audit → transcript. The single egress gate for every tool result."""
    policy = ctx.policy
    sanitized = sanitize_tool_result(result, allows_sample_values=policy.allows_sample_values)
    record_tool_audit(
        policy,
        tool,
        tool_input,
        sanitized,
        principal_id=ctx.principal_id,
        canvas_id=ctx.wg.get("id"),
        request_id=ctx.request_id,
    )
    ctx.transcript.append({"tool": tool, "input": tool_input, "result": sanitized})
    return sanitized


try:
    # pydantic_ai is an optional extra — import lazily so a kernel without it still boots (the
    # agent just reports unavailable). Tools are registered once here; the model is supplied per-run.
    from pydantic_ai import Agent, RunContext

    _agent: "Agent[_Ctx, str] | None" = Agent(deps_type=_Ctx, output_type=str, system_prompt=_SYSTEM)

    @_agent.tool
    def list_catalog(ctx: RunContext[_Ctx]) -> dict:
        """List the datasets in the catalog: name, uri, columns, row count, and detected primary-key
        candidate column(s) — the keys you join on. Call join_hints to see how two datasets relate."""
        out = {"tables": [{"name": t.name, "uri": t.uri, "rowCount": t.row_count,
                           "columns": [c.name for c in t.columns],
                           "keys": [k.columns for k in t.keys]}
                          for t in ctx.deps.kdeps.catalog.list_tables(None)]}
        return _finish(ctx.deps, "list_catalog", {}, out)

    @_agent.tool
    def join_hints(ctx: RunContext[_Ctx], left_uri: str, right_uri: str) -> dict:
        """How two catalog datasets can join: ranked key column pairs with the join CARDINALITY
        MEASURED from the data (1:1 / 1:N / N:1 / N:M), plus any owner-declared relationship. Use this
        to pick the right join key and to know whether a join fans out (a 1:N/N:M join multiplies rows
        — aggregate afterward if you need the parent grain)."""
        try:
            out = graph_ops.join_hints(ctx.deps.kdeps, left_uri, right_uri)
        except Exception as e:  # noqa: BLE001
            out = {"error": f"{type(e).__name__}: {e}"}
        return _finish(ctx.deps, "join_hints", {"left_uri": left_uri, "right_uri": right_uri}, out)

    @_agent.tool
    def validate(ctx: RunContext[_Ctx]) -> dict:
        """Check the canvas you've built so far WITHOUT running it: typed-wire errors (incompatible
        connections) and, for each join node, its measured cardinality + a fan-out warning. Call this
        before you finish to confirm the graph is correct."""
        try:
            out = graph_ops.validate_graph(ctx.deps.kdeps, ctx.deps.wg)
        except Exception as e:  # noqa: BLE001
            out = {"error": f"{type(e).__name__}: {e}"}
        return _finish(ctx.deps, "validate", {}, out)

    @_agent.tool
    def list_node_kinds(ctx: RunContext[_Ctx]) -> dict:
        """List available node kinds with their params and input/output ports."""
        out = {"kinds": graph_ops.node_kinds(ctx.deps.kdeps)}
        return _finish(ctx.deps, "list_node_kinds", {}, out)

    @_agent.tool
    def add_node(ctx: RunContext[_Ctx], kind: str, title: str | None = None,
                 config: dict | None = None) -> dict:
        """Add a node to the canvas. Returns its node_id and port handles. `config` maps param name -> value."""
        try:
            out = graph_ops.add_node(ctx.deps.wg, ctx.deps.kdeps.node_specs,
                                     _new_id(ctx.deps, kind), kind, title, config)
        except graph_ops.GraphOpError as e:
            out = {"error": f"{e}. Call list_node_kinds."}
        return _finish(ctx.deps, "add_node", {"kind": kind, "title": title, "config": config}, out)

    @_agent.tool
    def connect(ctx: RunContext[_Ctx], source_id: str, target_id: str,
                target_handle: str | None = None) -> dict:
        """Connect one node's output to another node's input. target_handle picks a multi-input handle (e.g. join 'a'/'b')."""
        try:
            out = graph_ops.connect(ctx.deps.wg, ctx.deps.kdeps.node_specs, _new_id(ctx.deps, "e"),
                                    source_id, target_id, target_handle)
        except graph_ops.GraphOpError as e:
            out = {"error": str(e)}
        return _finish(ctx.deps, "connect",
                       {"source_id": source_id, "target_id": target_id, "target_handle": target_handle}, out)

    @_agent.tool
    def set_config(ctx: RunContext[_Ctx], node_id: str, config: dict) -> dict:
        """Merge config values into an existing node."""
        try:
            out = graph_ops.set_config(ctx.deps.wg, node_id, config)
        except graph_ops.GraphOpError as e:
            out = {"error": str(e)}
        return _finish(ctx.deps, "set_config", {"node_id": node_id, "config": config}, out)

    @_agent.tool
    def preview(ctx: RunContext[_Ctx], node_id: str) -> dict:
        """Preview a node over a small sample. Returns columns and up to 8 rows when policy allows;
        under metadata-only, columns and row count stay but sample values are withheld."""
        d = ctx.deps.kdeps
        if not graph_ops.find_node(ctx.deps.wg, node_id):
            out: dict = {"error": "node_id not found"}
        else:
            try:
                res = preview_node(Graph(**ctx.deps.wg), node_id, 8, d.resolve_adapter, d.registry,
                                   d.node_builders, d.node_specs, storage=d.storage)
                if res.not_previewable:
                    out = {"not_previewable": True, "reason": res.reason}
                elif res.error:
                    out = {"error": res.reason}
                else:
                    out = {"columns": [c.name for c in res.columns], "rows": res.rows[:8], "row_count": res.row_count}
            except Exception as e:  # noqa: BLE001
                out = {"error": f"{type(e).__name__}: {e}"}
        return _finish(ctx.deps, "preview", {"node_id": node_id}, out)

except ImportError:  # pydantic_ai not installed — agent_status() reports it; run_agent raises
    _agent = None


# Native providers whose Pydantic AI constructors accept one explicit API key. Unsupported provider
# shapes fail closed instead of ignoring a selected Cred and letting provider inference use ambient env.
_AGENT_API_KEY_PROVIDERS = frozenset({
    "openai", "openai-chat", "openai-responses", "anthropic", "google", "groq",
    "mistral", "cohere",
})


def _build_model(model: str, api_key: str | None, base_url: str | None):
    """Build a Pydantic AI model from the (litellm-style) config — in-process, no proxy."""
    name = _agent_model_name(model)
    if base_url:  # any OpenAI-compatible endpoint (local Ollama, a gateway, …)
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=api_key or "not-needed"))
    from pydantic_ai.models import infer_model
    inferred_name = f"{_agent_provider(model)}:{name}"
    if not api_key:
        return infer_model(inferred_name)
    if _agent_provider(model) not in _AGENT_API_KEY_PROVIDERS:
        raise AgentCredentialError()

    def configured_provider(provider_name: str):
        from pydantic_ai.providers import infer_provider_class
        try:
            return infer_provider_class(provider_name)(api_key=api_key)
        except (ImportError, TypeError, ValueError) as exc:
            raise AgentCredentialError() from exc

    # Pass the selected material directly to the provider. Process environment is never mutated, so
    # concurrent requests, credential rotation, and deletion cannot observe or retain an old identity.
    return infer_model(inferred_name, provider_factory=configured_provider)


def run_agent(
    outcome: str,
    graph: dict,
    deps,
    model=None,
    policy=None,
    *,
    principal_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Run the tool-use loop; return {graph, transcript, summary}. `model`/`policy` injected in tests."""
    config = _agent_config()
    if _agent is None:
        raise RuntimeError("agent extra not installed: from a clone, run `uv pip install -e 'kernel[agent]'`")
    from pydantic_ai.usage import UsageLimits

    cfg_model, _, cfg_base = config
    effective_policy = policy if policy is not None else load_agent_data_policy(
        model=cfg_model, base_url=cfg_base)

    wg = {
        "id": graph.get("id", "canvas"), "version": graph.get("version", 1),
        "nodes": [dict(n) for n in graph.get("nodes", [])],
        "edges": [dict(e) for e in graph.get("edges", [])],
    }
    existing_ids = {n["id"] for n in wg["nodes"]}
    ctx = _Ctx(
        kdeps=deps,
        wg=wg,
        seq=[0],
        transcript=[],
        policy=effective_policy,
        principal_id=principal_id,
        request_id=request_id,
    )
    m = model if model is not None else _build_model(*config)

    prompt = (f"{outcome}\n\n(The canvas currently has {len(wg['nodes'])} node(s) and "
              f"{len(wg['edges'])} edge(s).) Respond to this — answer or advise in text, or build/"
              "modify the canvas by calling tools, whichever the message calls for.")
    # request_limit bounds the loop (was the manual step cap) so a confused model can't run away.
    # Hitting the cap must NOT discard the work done so far — the extra read tools (join_hints /
    # validate) make the cap more reachable, so return the partial graph + transcript instead of 502.
    from pydantic_ai.exceptions import UsageLimitExceeded
    try:
        result = _agent.run_sync(prompt, model=m, deps=ctx,
                                 usage_limits=UsageLimits(request_limit=settings.agent_max_steps))
        summary = (result.output or "").strip() or "Done."
    except UsageLimitExceeded:
        summary = (f"Stopped at the {settings.agent_max_steps}-step limit — returning the partial build "
                   "so far. Ask me to continue if it's incomplete.")
    graph_ops.layout_new(wg, existing_ids)
    return {"graph": wg, "transcript": ctx.transcript, "summary": summary,
            "policy": effective_policy.disclosure()}
