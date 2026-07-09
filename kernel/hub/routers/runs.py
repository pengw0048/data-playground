"""Compile / preview / schema / estimate / run / cancel, plus destinations and the agent —
the execution routes (and where a run writes). Split out of main.py; all authed at include time.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hub import compiler, destinations, metadb, placement
from hub import graph as graph_mod
from hub.agent import agent_status, run_agent
from hub.deps import get_deps
from hub.executors.preview import preview_node
from hub.executors.profile import profile_node
from hub.executors.schema import schema_for_graph
from hub.security import current_user
from hub.settings import settings
from hub.models import (
    CompilePlan,
    CompileRequest,
    EstimateRequest,
    JoinAnalysis,
    PreviewRequest,
    ProfileResult,
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
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)


@router.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest, uid: str = Depends(current_user)) -> SampleResult:
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    k = req.k if req.k is not None else settings.preview_k
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            return SampleResult(**kb.preview(req.graph, req.node_id, k, max(0, req.offset)))  # on the canvas's warm kernel
        except Exception as e:  # noqa: BLE001 — kernel unreachable / spawn timeout → a clean error, not a raw 500
            return SampleResult(error=True, reason=f"kernel unavailable: {type(e).__name__}: {e}")
    return preview_node(req.graph, req.node_id, k,
                        deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs,
                        offset=max(0, req.offset))


@router.post("/run/profile", response_model=ProfileResult)
def run_profile(req: PreviewRequest, uid: str = Depends(current_user)) -> ProfileResult:
    """Per-column stats (null/distinct/min/max/mean) over the previewed sample of a node's output."""
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            return ProfileResult(**kb.profile(req.graph, req.node_id))   # on the canvas's warm kernel
        except Exception as e:  # noqa: BLE001 — kernel unreachable → a clean error, not a raw 500
            return ProfileResult(error=True, reason=f"kernel unavailable: {type(e).__name__}: {e}")
    return profile_node(req.graph, req.node_id,
                        deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs)


@router.post("/graph/schema")
def graph_schema(req: CompileRequest) -> dict:
    """Per-node output columns (metadata-only) for editor column suggestions — see executors/schema."""
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    return schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                            deps.node_builders, deps.node_specs)


@router.post("/graph/estimate")
def graph_estimate(req: CompileRequest) -> dict:
    """Per-node output-SIZE estimate (rows + confidence) for the card size hint — see hub.estimate.
    Conservative + honest: an unknown count comes back rows=null so the UI shows nothing, not a guess."""
    from hub.estimate import estimate_sizes
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    try:
        sizes = estimate_sizes(req.graph, deps.resolve_adapter)
    except Exception:  # noqa: BLE001 — a hint must never 500
        return {}
    return {nid: {"rows": s.rows, "confidence": s.confidence} for nid, s in sizes.items()}


@router.post("/graph/plan")
def graph_plan(req: CompileRequest) -> dict:
    """The execution plan for a target: the regions it splits into, each with backend + boundary tier +
    estimated size — the UI 'run plan' preview that makes cost-based placement + tiering visible. A plain
    graph is one 'default' region (runs locally); placement (a cluster backend / engine label / checkpoint)
    splits it. Never 500s."""
    deps = get_deps()
    if not req.target_node_id:
        return {"regions": []}
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    try:
        return {"regions": deps.controller.plan_summary(req.graph, req.target_node_id)}
    except Exception as e:  # noqa: BLE001 — a preview must never 500
        return {"regions": [], "error": f"{type(e).__name__}: {e}"}


@router.post("/graph/join-analysis", response_model=JoinAnalysis)
def join_analysis(req: CompileRequest) -> JoinAnalysis:
    """Catalog-driven join hints for a join node (target_node_id): ranked key suggestions for its
    two inputs (cardinality from measured/grain-derived key uniqueness) + a fan-out warning."""
    from hub import relationships as rel
    deps = get_deps()
    if not req.target_node_id:
        return JoinAnalysis(note="no join node selected")
    cols = schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                            deps.node_builders, deps.node_specs)
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


def _cone_size(req_graph, target_node_id, deps) -> "tuple[int | None, int | None]":
    """The largest data volume this run moves — the MAX estimated rows AND bytes across the target's cone
    (source counts + a downstream sample's smaller output). Uses hub.estimate so the confirm-gate, the
    placement policy, and the UI hint all share ONE estimator. (None, None) when nothing is countable —
    the gate then errs toward NOT blocking (an uncountable source can't be scanned → fails fast anyway)."""
    from hub.estimate import estimate_sizes
    try:  # per-node schemas sharpen the byte width (else a flat default/row makes the byte gate meaningless)
        schemas = schema_for_graph(req_graph, deps.resolve_adapter, deps.registry,
                                   deps.node_builders, deps.node_specs)
    except Exception:  # noqa: BLE001 — schema inference is best-effort; fall back to default widths
        schemas = None
    try:
        sizes = estimate_sizes(req_graph, deps.resolve_adapter, target=target_node_id, schemas=schemas)
    except Exception:  # noqa: BLE001 — a bad estimate must not block the gate
        return None, None
    rows = [s.rows for s in sizes.values() if s.rows is not None]
    byts = [s.bytes for s in sizes.values() if s.bytes is not None]
    return (max(rows) if rows else None), (max(byts) if byts else None)


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
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    rows, byts = _cone_size(req.graph, req.target_node_id, deps)
    runner = deps.pick_runner(plan, uid)
    est = runner.estimate(plan, rows, byts)
    if est.needs_confirm and _cached_noop(runner, req.graph, req.target_node_id):
        est.needs_confirm = False  # reusing a cached result is a no-op, not a big pass
    return est


@router.post("/run", response_model=RunStatus)
def run(req: RunRequest, uid: str = Depends(current_user)) -> RunStatus:
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    runner = _route_by_capability(deps, deps.pick_runner(plan, uid), req.graph)  # honor node requires
    rows, byts = _cone_size(req.graph, req.target_node_id, deps)
    est = runner.estimate(plan, rows, byts)
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
        from hub import metadb
        persisted = metadb.get_run_state(run_id)
        if persisted is not None:
            return RunStatus(**persisted)
        return RunStatus(run_id=run_id, status="failed",
                         error="run not found — it was evicted or the kernel restarted")


try:  # a malformed DP_STALL_S must degrade to the default, not crash the whole app at import
    _STALL_S = float(os.environ.get("DP_STALL_S", "120"))  # a running run with no step completed for this long
except ValueError:
    _STALL_S = 120.0


@router.get("/run/{run_id}", response_model=RunStatus)
def run_status(run_id: str) -> RunStatus:
    st = _status_or_lost(run_id)
    if st.status == "running" and metadb.run_stalled(run_id, _STALL_S):
        st = st.model_copy(update={"stalled": True})  # copy — don't mutate the runner's live object
    return st


@router.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str) -> RunStatus:
    deps = get_deps()
    owner = deps.run_index.get(run_id)
    if owner is not None:
        return owner.cancel(run_id)  # this instance ran it → cancel in-process
    # not owned here (the hub restarted, or another stateless instance accepted the run) — route via the
    # DB-backed kernel backend, which resolves the owning kernel from run_states and cancels it (or
    # returns the last-known persisted status). Mirrors _status_or_lost so cancel never 404s a live run.
    kb = deps.kernel_backend()
    if kb is not None:
        return kb.cancel(run_id)
    from hub import metadb
    persisted = metadb.get_run_state(run_id)
    if persisted is not None:
        return RunStatus(**persisted)
    raise HTTPException(404, f"run '{run_id}' not found")

