"""Compile / preview / schema / estimate / run / cancel, plus destinations and the agent —
the execution routes (and where a run writes). Split out of main.py; all authed at include time.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from kernel import compiler, destinations
from kernel import graph as graph_mod
from kernel.agent import agent_status, run_agent
from kernel.deps import get_deps
from kernel.executors.preview import preview_node
from kernel.executors.schema import schema_for_graph
from kernel.graph import upstream_chain
from kernel.settings import settings
from kernel.models import (
    CompilePlan,
    CompileRequest,
    EstimateRequest,
    PreviewRequest,
    RunEstimate,
    RunRequest,
    RunStatus,
    SampleResult,
)

router = APIRouter()

_RUN_INDEX_MAX = 1000  # cap deps.run_index (run_id -> owning runner); well above either runner's own cap


def _reject_invalid(graph, deps) -> None:
    """400 on a graph the type system forbids (server-side; the frontend blocks these too)."""
    errs = graph_mod.type_errors(graph, deps.node_specs)
    if errs:
        raise HTTPException(400, "incompatible connection: " + "; ".join(errs[:5]))


@router.post("/graph/compile", response_model=CompilePlan)
def compile_graph(req: CompileRequest) -> CompilePlan:
    deps = get_deps()
    errs = graph_mod.type_errors(req.graph, deps.node_specs)
    if errs:
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=True,
                           error="incompatible connection: " + "; ".join(errs[:5]))
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)


@router.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest) -> SampleResult:
    deps = get_deps()
    k = req.k if req.k is not None else settings.preview_k
    return preview_node(req.graph, req.node_id, k,
                        deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs,
                        offset=max(0, req.offset))


@router.post("/graph/schema")
def graph_schema(req: CompileRequest) -> dict:
    """Per-node output columns (metadata-only) for editor column suggestions — see executors/schema."""
    deps = get_deps()
    return schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                            deps.node_lowerings, deps.node_specs)


# --------------------------------------------------------------------------- #
# Destinations (save/open "places") — local + pluggable object-store backends
# --------------------------------------------------------------------------- #
class BrowseRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    destination_id: str
    path: str = ""


@router.get("/destinations")
def list_destinations() -> dict:
    ws = get_deps().workspace
    return {"destinations": destinations.presets(ws), "backends": destinations.backend_kinds()}


@router.post("/destinations/browse")
def browse_destination(req: BrowseRequest) -> dict:
    return destinations.browse(get_deps().workspace, req.destination_id, req.path)


class MkdirRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    destination_id: str
    path: str = ""
    name: str


@router.post("/destinations/mkdir")
def mkdir_destination(req: MkdirRequest) -> dict:
    return destinations.mkdir(get_deps().workspace, req.destination_id, req.path, req.name)


# --------------------------------------------------------------------------- #
# Agent (optional LLM planner — key stays in the kernel, never the browser)
# --------------------------------------------------------------------------- #
class AgentRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    outcome: str
    graph: dict = {}


@router.get("/agent")
def agent_get_status() -> dict:
    return agent_status()


@router.post("/agent")
def agent_act(req: AgentRequest) -> dict:
    st = agent_status()
    if not st["available"]:
        return {"available": False, "reason": st["reason"]}
    try:
        out = run_agent(req.outcome, req.graph, get_deps())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"agent error: {type(e).__name__}: {e}")
    return {"available": True, **out}


def _row_estimate(req_graph, target_node_id, deps) -> int | None:
    """Largest real source-row count feeding this run, or None when no source is countable. Returning
    None (rather than a fabricated number) lets the estimator err toward confirmation on unknown size
    instead of silently slipping the confirm gate."""
    chain = upstream_chain(req_graph, target_node_id) if target_node_id else req_graph.nodes
    counts: list[int] = []
    for n in chain:
        if n.type == "source":
            cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
            uri = cfg.get("uri") or cfg.get("table")
            if uri:
                try:
                    c = deps.resolve_adapter(uri).count(uri)
                    if c is not None:
                        counts.append(c)
                except Exception:  # noqa: BLE001
                    pass
    return max(counts) if counts else None


@router.post("/run/estimate", response_model=RunEstimate)
def run_estimate(req: EstimateRequest) -> RunEstimate:
    deps = get_deps()
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    return deps.pick_runner(plan).estimate(plan, rows)


@router.post("/run", response_model=RunStatus)
def run(req: RunRequest) -> RunStatus:
    deps = get_deps()
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    runner = deps.pick_runner(plan)
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    est = runner.estimate(plan, rows)
    if est.needs_confirm and not req.confirmed:
        raise HTTPException(409, "run needs confirmation (large or unknown size — a full pass)")
    status = runner.run(plan, req.graph, req.target_node_id, est.placement)
    deps.run_index[status.run_id] = runner  # so status/cancel/ws reach the right runner
    # bound run_index (insertion-ordered) so it can't grow for the process lifetime — the runners
    # themselves only retain the last _MAX_RUNS, and _status_or_lost already tolerates a missing id.
    while len(deps.run_index) > _RUN_INDEX_MAX:
        deps.run_index.pop(next(iter(deps.run_index)))
    return status


def _runner_for(run_id: str):
    deps = get_deps()
    return deps.run_index.get(run_id, deps.runner)


def _status_or_lost(run_id: str) -> RunStatus:
    """This run's status, resolved in order: (1) the owning runner's in-memory status (freshest — this
    instance ran it); (2) the shared DB (run_states) — so ANOTHER stateless web instance, or this one
    after a restart, can still answer; (3) a synthetic terminal status. Returning terminal instead of a
    404 lets the client resolve the node cleanly instead of exhausting its retries and stranding it."""
    try:
        return _runner_for(run_id).status(run_id)
    except KeyError:
        from kernel import metadb
        persisted = metadb.get_run_state(run_id)
        if persisted is not None:
            return RunStatus(**persisted)
        return RunStatus(run_id=run_id, status="failed",
                         error="run not found — it was evicted or the kernel restarted")


@router.get("/run/{run_id}", response_model=RunStatus)
def run_status(run_id: str) -> RunStatus:
    return _status_or_lost(run_id)


@router.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str) -> RunStatus:
    try:
        return _runner_for(run_id).cancel(run_id)
    except KeyError:
        raise HTTPException(404, f"run '{run_id}' not found")

