"""Compile / preview / schema / estimate / run / cancel, plus destinations and the agent —
the execution routes (and where a run writes). Split out of main.py; all authed at include time.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hub import auth, compiler, destinations, metadb, placement
from hub import graph as graph_mod
from hub.agent import agent_status, run_agent
from hub.deps import get_deps
from hub.executors.preview import preview_node
from hub.executors.profile import profile_node
from hub.executors.schema import schema_for_graph
from hub.security import current_user
from hub.settings import settings
from hub.storage import ManagedSourceReadError
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
_RUN_MUTATE_ROLES = ("owner", "editor")


def _require_graph_read_access(graph, uid: str) -> tuple[str | None, str | None]:
    """Authorize a caller-supplied graph by its saved-canvas identity before touching its sources.

    In shared/auth mode, graph analysis is only meaningful inside a real canvas the caller can read;
    owner, editor, and viewer are all read roles. Unknown ids and private canvases both return 404 so
    the endpoint does not become a canvas-enumeration oracle. Open single-user mode keeps supporting
    ad-hoc graphs. This is an identity check only: pinning the payload to a saved revision is separate.
    """
    if not auth.auth_enabled():
        return None, None
    cid = graph.get("id") if isinstance(graph, dict) else getattr(graph, "id", None)
    cid = str(cid or "")
    role = metadb.canvas_role(cid, uid) if cid else None
    if role is None:
        raise HTTPException(404, f"canvas '{cid}' not found")
    return cid, role


def _unknown_kind_error(graph, deps) -> str | None:
    """P0-DATA-02: a node whose kind isn't registered (a missing plugin, a misspelling) used to compile
    and execute as a silent passthrough — the pipeline reports success while omitting the work. Return a
    human error naming the first offender, or None if every kind is recognized."""
    unknown = graph_mod.unknown_kinds(graph, set(deps.node_specs) | set(deps.node_builders))
    if not unknown:
        return None
    nid, t = unknown[0]
    return f"unknown node kind '{t}' (node '{nid}') — install its plugin or remove the node"


def _invalid_graph(graph, deps, target_node_id: str | None = None) -> tuple[str, bool] | None:
    """The single validation path for every API that consumes a caller-supplied graph.

    Returns ``(message, acyclic)`` so compile can preserve its error-plan contract while the other
    endpoints consistently reject the same malformed graph with HTTP 400.
    """
    unknown = _unknown_kind_error(graph, deps)
    if unknown:
        return unknown, True  # preserve P0-DATA-02's existing error text and compile semantics
    structural = graph_mod.structural_errors(graph, deps.node_specs, target_node_id)
    if structural:
        return "invalid graph: " + "; ".join(structural[:5]), True
    errs = graph_mod.type_errors(graph, deps.node_specs)
    if errs:
        return "incompatible connection: " + "; ".join(errs[:5]), True
    if not graph_mod.is_acyclic(graph):
        return "graph has a cycle — control flow must be encapsulated (§5.7)", False
    return None


def _reject_invalid(graph, deps, target_node_id: str | None = None) -> None:
    """400 on any graph that compile would reject."""
    invalid = _invalid_graph(graph, deps, target_node_id)
    if invalid:
        raise HTTPException(400, invalid[0])


@router.post("/graph/compile", response_model=CompilePlan)
def compile_graph(req: CompileRequest, uid: str = Depends(current_user)) -> CompilePlan:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    invalid = _invalid_graph(req.graph, deps, req.target_node_id)
    if invalid:
        error, acyclic = invalid
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=acyclic, error=error)
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)


@router.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest, uid: str = Depends(current_user)) -> SampleResult:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.node_id)
    k = req.k if req.k is not None else settings.preview_k
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            return SampleResult(**kb.preview(req.graph, req.node_id, k, max(0, req.offset)))  # on the canvas's warm kernel
        except Exception as e:  # noqa: BLE001 — kernel unreachable / spawn timeout → a clean error, not a raw 500
            return SampleResult(error=True, reason=f"kernel unavailable: {type(e).__name__}: {e}")
    return preview_node(req.graph, req.node_id, k,
                        deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs,
                        offset=max(0, req.offset), storage=deps.storage)


@router.post("/run/profile", response_model=ProfileResult)
def run_profile(req: PreviewRequest, uid: str = Depends(current_user)) -> ProfileResult:
    """Per-column stats (null/distinct/min/max/mean) over a node's output — the previewed sample, or the
    WHOLE dataset (a full pass) when `full` is set."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.node_id)
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            return ProfileResult(**kb.profile(req.graph, req.node_id, full=req.full))  # on the canvas's warm kernel
        except Exception as e:  # noqa: BLE001 — kernel unreachable → a clean error, not a raw 500
            return ProfileResult(error=True, reason=f"kernel unavailable: {type(e).__name__}: {e}")
    return profile_node(req.graph, req.node_id, deps.resolve_adapter, deps.registry,
                        deps.node_builders, deps.node_specs, full=req.full, storage=deps.storage)


@router.post("/graph/schema")
def graph_schema(req: CompileRequest, uid: str = Depends(current_user)) -> dict:
    """Per-node output columns (metadata-only) for editor column suggestions — see executors/schema."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.target_node_id)
    try:
        return schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))


@router.post("/graph/estimate")
def graph_estimate(req: CompileRequest, uid: str = Depends(current_user)) -> dict:
    """Per-node output-SIZE estimate (rows + confidence) for the card size hint — see hub.estimate.
    Conservative + honest: an unknown count comes back rows=null so the UI shows nothing, not a guess."""
    _require_graph_read_access(req.graph, uid)
    from hub.estimate import estimate_sizes
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    _reject_invalid(req.graph, deps, req.target_node_id)
    try:
        sizes = estimate_sizes(
            req.graph, deps.resolve_adapter, actuals=_actuals_for(req.graph, deps),
            storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — a hint must never 500
        return {}
    return {nid: {"rows": s.rows, "confidence": s.confidence} for nid, s in sizes.items()}


@router.post("/graph/plan")
def graph_plan(req: CompileRequest, uid: str = Depends(current_user)) -> dict:
    """The execution plan for a target: the regions it splits into, each with backend + boundary tier +
    estimated size — the UI 'run plan' preview that makes cost-based placement + tiering visible. A plain
    graph is one 'default' region (runs locally); placement (a cluster backend / engine label / checkpoint)
    splits it. Never 500s."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    _reject_invalid(req.graph, deps, req.target_node_id)
    if not req.target_node_id:
        return {"regions": []}
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    try:
        return {"regions": deps.controller.plan_summary(req.graph, req.target_node_id)}
    except ManagedSourceReadError as e:
        return {"regions": [], "error": str(e)}
    except Exception as e:  # noqa: BLE001 — a preview must never 500
        return {"regions": [], "error": f"{type(e).__name__}: {e}"}


@router.post("/graph/join-analysis", response_model=JoinAnalysis)
def join_analysis(req: CompileRequest, uid: str = Depends(current_user)) -> JoinAnalysis:
    """Catalog-driven join hints for a join node (target_node_id): ranked key suggestions for its
    two inputs (cardinality from measured/grain-derived key uniqueness) + a fan-out warning."""
    _require_graph_read_access(req.graph, uid)
    from hub import relationships as rel
    deps = get_deps()
    _reject_invalid(req.graph, deps, req.target_node_id)
    if not req.target_node_id:
        return JoinAnalysis(note="no join node selected")
    try:
        cols = schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, storage=deps.storage)
        return rel.analyze_join(
            req.graph, req.target_node_id, cols, deps.catalog, deps.resolve_adapter,
            storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))


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
def agent_act(req: AgentRequest, uid: str = Depends(current_user)) -> dict:
    # The agent's preview/validate tools can execute the caller-supplied graph, so apply the same
    # read boundary as the explicit graph-analysis routes before checking provider availability.
    _require_graph_read_access(req.graph, uid)
    st = agent_status()
    if not st["available"]:
        return {"available": False, "reason": st["reason"]}
    try:
        out = run_agent(req.outcome, req.graph, get_deps())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"agent error: {type(e).__name__}: {e}")
    return {"available": True, **out}


def _actuals_for(graph, deps) -> dict[str, int]:  # noqa: ARG001 — deps kept for signature symmetry
    """Measured per-node rows from the last successful run, kept ONLY for nodes still 'latest' — an
    edited (now 'stale') node's old count would mislead the estimate. Lets a not-yet-run downstream node
    inherit a real upstream count instead of 'unknown'. Best-effort: any hiccup → no actuals."""
    from hub import metadb
    try:
        a = metadb.latest_actuals(getattr(graph, "id", None))
        if not a:
            return {}
        latest = {n.id for n in graph.nodes
                  if (n.data.get("status") if isinstance(n.data, dict) else getattr(n.data, "status", None)) == "latest"}
        return {k: v for k, v in a.items() if k in latest}
    except Exception:  # noqa: BLE001
        return {}


def _cone_size(req_graph, target_node_id, deps) -> "tuple[int | None, int | None, dict]":
    """The largest data volume this run moves — the MAX estimated rows AND bytes across the target's cone
    (source counts + a downstream sample's smaller output). Uses hub.estimate so the confirm-gate, the
    placement policy, and the UI hint all share ONE estimator: also returns the full per-node `sizes` so
    the caller can hand THIS schema+actual-aware estimate to the RunController's placement (else placement
    would re-estimate with coarse default widths and the measured vector/decimal widths would be inert
    there). (None, None, {}) when nothing is countable — the gate then errs toward NOT blocking (an
    uncountable source can't be scanned → fails fast anyway)."""
    from hub.estimate import estimate_sizes
    try:  # per-node schemas sharpen the byte width (else a flat default/row makes the byte gate meaningless)
        schemas = schema_for_graph(req_graph, deps.resolve_adapter, deps.registry,
                                   deps.node_builders, deps.node_specs, storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — schema inference is best-effort; fall back to default widths
        schemas = None
    try:
        sizes = estimate_sizes(req_graph, deps.resolve_adapter, target=target_node_id, schemas=schemas,
                               actuals=_actuals_for(req_graph, deps), storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — a bad estimate must not block the gate
        return None, None, {}
    rows = [s.rows for s in sizes.values() if s.rows is not None]
    byts = [s.bytes for s in sizes.values() if s.bytes is not None]
    return (max(rows) if rows else None), (max(byts) if byts else None), sizes


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


@router.post("/run/estimate", response_model=RunEstimate)
def run_estimate(req: EstimateRequest, uid: str = Depends(current_user)) -> RunEstimate:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.target_node_id)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    rows, byts, _ = _cone_size(req.graph, req.target_node_id, deps)
    runner = deps.pick_runner(plan, uid)
    est = runner.estimate(plan, rows, byts)
    return est


class RunNeedsConfirm(Exception):
    """The confirm gate tripped (large/unknown size) and the caller didn't pass confirmed=True. Carries
    the estimate so the caller can surface estRows/reason. HTTP maps it to 409; the MCP tool returns a
    needsConfirm result. Raising (not returning) keeps `start_run` a single 'started, here's the owner'
    contract for both surfaces."""

    def __init__(self, estimate: RunEstimate):
        super().__init__("run needs confirmation")
        self.estimate = estimate


def start_run(deps, graph, target_node_id: str | None, uid: str, confirmed: bool = False):
    """Start a run — the ONE code path behind both POST /run and the MCP run_canvas tool, so a run an
    agent launches is placed, gated, and owned exactly like one the browser launches. Resolves source
    refs, rejects an invalid/cyclic graph (HTTPException), sizes + gates the run (RunNeedsConfirm), then
    hands to the RunController (placement-splitting) or the base runner and records the owner in
    run_index. Returns (status, owner); poll the owner via _status_or_lost / cancel via run_index."""
    # A run is a mutation of a saved canvas's operational state/history, so auth mode requires a REAL
    # reachable canvas plus owner/editor (viewer is read-only). Only open single-user mode keeps ad-hoc
    # graph execution. Authorize before resolving or compiling so an invented/private id cannot make the
    # server touch caller-selected sources through POST /run after the read routes have been closed.
    auth_canvas = None
    if auth.auth_enabled():
        cid, role = _require_graph_read_access(graph, uid)
        assert cid is not None and role is not None  # auth mode returns one authoritative role read
        if role not in _RUN_MUTATE_ROLES:
            raise HTTPException(403, f"canvas '{cid}' requires owner or editor to run")
        auth_canvas = cid  # a real writable canvas → all collaborators may observe this run
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(graph, deps, target_node_id)
    plan = compiler.compile_plan(graph, target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    runner = _route_by_capability(deps, deps.pick_runner(plan, uid), graph)  # honor node requires
    rows, byts, sizes = _cone_size(graph, target_node_id, deps)
    est = runner.estimate(plan, rows, byts)
    if est.needs_confirm and not confirmed:
        raise RunNeedsConfirm(est)
    from hub.observability import (
        AuditAction, AuditOutcome, emit_audit, get_request_id, invoke_backend_run,
    )
    request_id = get_request_id()
    # a run that splits across placement regions (a placed node / checkpoint / fan-out) is owned by the
    # RunController; a single default region returns None → the base runner, exactly as before. Hand it the
    # schema+actual-aware `sizes` we just computed so cost-based placement routes on the SAME measured
    # widths the gate saw — not a second, coarse re-estimate.
    overall = deps.controller.run(graph, target_node_id, uid, sizes=sizes, request_id=request_id)
    if overall is not None:
        status, owner = overall, deps.controller
    else:
        status = invoke_backend_run(
            runner, plan, graph, target_node_id, est.placement, request_id=request_id)
        owner = runner
    if request_id and not status.request_id:
        status.request_id = request_id
    deps.run_index[status.run_id] = owner  # so status/cancel/ws reach the right owner
    deps.run_owner[status.run_id] = uid  # fast in-process creator lookup; auth-mode runs are canvas-bound
    if auth.auth_enabled():  # persist the owner so authz survives restart / other stateless instances
        metadb.bind_run_owner(status.run_id, uid, auth_canvas, request_id=request_id)
    elif request_id:
        # Open mode has no durable owner bind; still stamp request_id for OPS-01 correlation.
        metadb.bind_run_request_id(status.run_id, request_id, canvas_id=auth_canvas or getattr(graph, "id", None))
    emit_audit(AuditAction.JOB_SUBMIT, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="run", resource_id=status.run_id, run_id=status.run_id,
               request_id=request_id,
               attrs={"placement": str(status.placement or "local")[:32]})
    # bound both (insertion-ordered) so they can't grow for the process lifetime — the runners
    # themselves only retain the last _MAX_RUNS, and _status_or_lost already tolerates a missing id.
    while len(deps.run_index) > _RUN_INDEX_MAX:
        deps.run_index.pop(next(iter(deps.run_index)))
    while len(deps.run_owner) > _RUN_INDEX_MAX:
        deps.run_owner.pop(next(iter(deps.run_owner)))
    return status, owner


@router.post("/run", response_model=RunStatus)
def run(req: RunRequest, uid: str = Depends(current_user)) -> RunStatus:
    try:
        status, _ = start_run(get_deps(), req.graph, req.target_node_id, uid, req.confirmed)
    except RunNeedsConfirm:
        raise HTTPException(409, "run needs confirmation (large or unknown size — a full pass)")
    return status


def _run_read_access(run_id: str, uid: str | None) -> bool:
    """Whether `uid` may observe this run.

    Open mode is one trusted user. In auth mode, the creator or any current collaborator on the REAL
    canvas may read status/output. A legacy ad-hoc run remains private to its creator, and a later
    canvas that reuses the graph id cannot claim it.
    """
    if not auth.auth_enabled():
        return True
    if not uid:
        return False
    if get_deps().run_owner.get(run_id) == uid:  # fast in-process path (before the DB bind lands)
        return True
    creator, auth_canvas = metadb.run_auth(run_id)
    if creator is not None:
        if creator == uid:
            return True
        return bool(auth_canvas and metadb.canvas_role(auth_canvas, uid) is not None)
    # a legacy run persisted before the creator column existed → best-effort canvas grant
    cid = metadb.run_canvas_id(run_id)
    return bool(cid and metadb.canvas_role(cid, uid) is not None)


def _run_mutate_access(run_id: str, uid: str | None) -> bool:
    """Whether `uid` may cancel this run.

    A real-canvas run follows the caller's CURRENT canvas role: owner/editor may mutate; viewer may
    only observe. A legacy ad-hoc run has no canvas role, so its creator remains its sole operator.
    Rows without durable creator metadata fall back to the persisted canvas role, then the in-process
    owner only when there is no persisted canvas association at all.
    """
    if not auth.auth_enabled():
        return True
    if not uid:
        return False
    creator, auth_canvas = metadb.run_auth(run_id)
    if creator is not None:
        if auth_canvas:
            return metadb.canvas_role(auth_canvas, uid) in _RUN_MUTATE_ROLES
        return creator == uid
    cid = metadb.run_canvas_id(run_id)
    if cid:
        return metadb.canvas_role(cid, uid) in _RUN_MUTATE_ROLES
    return get_deps().run_owner.get(run_id) == uid


def _require_run_read_access(run_id: str, uid: str) -> None:
    if not _run_read_access(run_id, uid):  # 404, not 403 — don't reveal that someone else's run id exists
        raise HTTPException(404, f"run '{run_id}' not found")


def _require_run_mutate_access(run_id: str, uid: str) -> None:
    if _run_mutate_access(run_id, uid):
        return
    # A viewer can already enumerate this shared run through status/history, so distinguish read-only
    # from not-found. A stranger still gets 404 and learns nothing about the run id.
    if _run_read_access(run_id, uid):
        raise HTTPException(403, f"run '{run_id}' requires canvas owner or editor to cancel")
    raise HTTPException(404, f"run '{run_id}' not found")


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
def run_status(run_id: str, uid: str = Depends(current_user)) -> RunStatus:
    _require_run_read_access(run_id, uid)  # status carries row counts, paths in errors, output names
    st = _status_or_lost(run_id)
    if st.status == "running" and metadb.run_stalled(run_id, _STALL_S):
        st = st.model_copy(update={"stalled": True})  # copy — don't mutate the runner's live object
    return st


@router.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str, uid: str = Depends(current_user)) -> RunStatus:
    from hub.observability import AuditAction, AuditOutcome, emit_audit, get_request_id
    _require_run_mutate_access(run_id, uid)  # only owner/editor may disrupt a shared canvas run
    deps = get_deps()
    owner = deps.run_index.get(run_id)
    if owner is not None:
        status = owner.cancel(run_id)  # this instance ran it → cancel in-process
    else:
        # not owned here (the hub restarted, or another stateless instance accepted the run) — route via the
        # DB-backed kernel backend, which resolves the owning kernel from run_states and cancels it (or
        # returns the last-known persisted status). Mirrors _status_or_lost so cancel never 404s a live run.
        kb = deps.kernel_backend()
        if kb is not None:
            status = kb.cancel(run_id)
        else:
            from hub import metadb
            persisted = metadb.get_run_state(run_id)
            if persisted is not None:
                status = RunStatus(**persisted)
            else:
                raise HTTPException(404, f"run '{run_id}' not found")
    emit_audit(AuditAction.JOB_CANCEL, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="run", resource_id=run_id, run_id=run_id,
               request_id=get_request_id(),
               attrs={"status": str(getattr(status, "status", "unknown"))[:32]})
    return status
