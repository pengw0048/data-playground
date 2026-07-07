"""Compile / preview / schema / estimate / run / cancel, plus destinations and the agent —
the execution routes (and where a run writes). Split out of main.py; all authed at include time.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from kernel import compiler, destinations, placement
from kernel import graph as graph_mod
from kernel.agent import agent_status, run_agent
from kernel.deps import get_deps
from kernel.executors.preview import preview_node
from kernel.executors.schema import schema_for_graph
from kernel.security import current_user
from kernel.graph import upstream_chain
from kernel.settings import settings
from kernel.models import (
    CompilePlan,
    CompileRequest,
    EstimateRequest,
    JoinAnalysis,
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
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    errs = graph_mod.type_errors(req.graph, deps.node_specs)
    if errs:
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=True,
                           error="incompatible connection: " + "; ".join(errs[:5]))
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)


@router.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest) -> SampleResult:
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    k = req.k if req.k is not None else settings.preview_k
    return preview_node(req.graph, req.node_id, k,
                        deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs,
                        offset=max(0, req.offset))


@router.post("/graph/schema")
def graph_schema(req: CompileRequest) -> dict:
    """Per-node output columns (metadata-only) for editor column suggestions — see executors/schema."""
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    return schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                            deps.node_lowerings, deps.node_specs)


@router.post("/graph/join-analysis", response_model=JoinAnalysis)
def join_analysis(req: CompileRequest) -> JoinAnalysis:
    """Catalog-driven join hints for a join node (target_node_id): ranked key suggestions for its
    two inputs (cardinality from measured/grain-derived key uniqueness) + a fan-out warning."""
    from kernel import relationships as rel
    deps = get_deps()
    if not req.target_node_id:
        return JoinAnalysis(note="no join node selected")
    cols = schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                            deps.node_lowerings, deps.node_specs)
    return rel.analyze_join(req.graph, req.target_node_id, cols, deps.catalog, deps.resolve_adapter)


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


def _route_by_capability(deps, chosen, graph):
    """If the graph declares a compute requirement the chosen backend can't place, prefer a backend
    whose place() satisfies it (e.g. the GPU pool). A routing HINT, not a hard gate: if nothing can
    place it (no matching pool configured), fall back to the chosen backend (OSS simulates GPUs, so an
    unmet requirement still runs locally rather than blocking)."""
    req = placement.graph_requires(graph, deps.node_specs)
    if not (req.cpu or req.gpu or req.gpu_type or req.mem or req.labels):  # no requirement → leave choice
        return chosen

    def _can_place(r):
        return hasattr(r, "place") and r.place(req) is not None

    if _can_place(chosen):
        return chosen
    return next((r for r in deps.runners if _can_place(r)), chosen)


def _cached_noop(runner, graph, target) -> bool:
    """True if this exact plan already has a reusable result — re-running just re-points at an existing
    output, so it needs no size confirmation (fixes the 'full cache hit still prompts' false gate)."""
    if not all(hasattr(runner, m) for m in ("_plan_hash", "_plan_cacheable", "_cache_get", "_output_exists")):
        return False
    try:
        if not runner._plan_cacheable(graph, target):
            return False
        c = runner._cache_get(runner._plan_hash(graph, target))
        if c is None:
            return False
        uri = c.get("uri")
        return (not uri) or runner._output_exists(uri)  # non-sink hit (rows cached) OR the artifact still exists
    except Exception:  # noqa: BLE001
        return False


@router.post("/run/estimate", response_model=RunEstimate)
def run_estimate(req: EstimateRequest, uid: str = Depends(current_user)) -> RunEstimate:
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    runner = deps.pick_runner(plan, uid)
    est = runner.estimate(plan, rows)
    if est.needs_confirm and _cached_noop(runner, req.graph, req.target_node_id):
        est.needs_confirm = False  # reusing a cached result is a no-op, not a big pass
    return est


@router.post("/run", response_model=RunStatus)
def run(req: RunRequest, uid: str = Depends(current_user)) -> RunStatus:
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    runner = _route_by_capability(deps, deps.pick_runner(plan, uid), req.graph)  # honor node requires
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    est = runner.estimate(plan, rows)
    if est.needs_confirm and not req.confirmed and not _cached_noop(runner, req.graph, req.target_node_id):
        raise HTTPException(409, "run needs confirmation (large or unknown size — a full pass)")
    # a run that splits across placement regions (a placed node / checkpoint / fan-out) is owned by the
    # RunController; a single default region returns None → the base runner, exactly as before.
    overall = deps.controller.run(req.graph, req.target_node_id)
    if overall is not None:
        status, owner = overall, deps.controller
    else:
        status, owner = runner.run(plan, req.graph, req.target_node_id, est.placement), runner
    deps.run_index[status.run_id] = owner  # so status/cancel/ws reach the right owner
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

