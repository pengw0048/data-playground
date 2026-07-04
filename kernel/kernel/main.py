"""Data Playground kernel — FastAPI app (PRD §9).

One kernel per open canvas session. Backend-agnostic core; the default bundle runs fully
offline (DuckDB adapter, in-memory catalog, local runner). All routes under /api, JSON,
camelCase on the wire.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from kernel import compiler
from kernel import graph as graph_mod
from kernel import metadb
from kernel.deps import get_deps
from kernel.executors.preview import preview_node
from kernel.graph import upstream_chain
from kernel.models import (
    CatalogTable,
    ColumnSchema,
    CompilePlan,
    CompileRequest,
    EstimateRequest,
    ImportRequest,
    KernelInfo,
    LineageResult,
    PipelineImport,
    PreviewRequest,
    ProcessorDescriptor,
    RunEstimate,
    RunRequest,
    RunStatus,
    SampleRequest,
    SampleResult,
)
from kernel.plugins.importer import ImporterNotConfigured
from kernel.settings import settings

api = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# Kernel + catalog
# --------------------------------------------------------------------------- #
@api.get("/kernel", response_model=KernelInfo)
def kernel_info() -> KernelInfo:
    return get_deps().info()


@api.get("/nodes")
def list_nodes() -> list[dict]:
    """Schema of every registered node (built-in + plugin) — powers generic rendering (§4.2)."""
    return [s.model_dump(by_alias=True) for s in get_deps().node_specs.values()]


@api.get("/plugins")
def list_plugins() -> list[dict]:
    return get_deps().plugins


@api.get("/catalog/tables", response_model=list[CatalogTable])
def list_tables(q: str | None = None) -> list[CatalogTable]:
    return get_deps().catalog.list_tables(q)


@api.get("/catalog/tables/{table_id}", response_model=CatalogTable)
def get_table(table_id: str) -> CatalogTable:
    try:
        return get_deps().catalog.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")


@api.get("/catalog/lineage", response_model=LineageResult)
def lineage(uri: str = Query(...)) -> LineageResult:
    return get_deps().catalog.lineage(uri)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    uri: str
    name: str | None = None


@api.post("/catalog/register", response_model=CatalogTable)
def catalog_register(req: RegisterRequest) -> CatalogTable:
    deps = get_deps()
    import os
    import re
    has_scheme = bool(re.match(r"^[a-z][a-z0-9+.-]*://", req.uri))
    uri = req.uri if has_scheme else os.path.abspath(os.path.expanduser(req.uri))
    name = req.name or os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
    try:
        deps.resolve_adapter(uri).schema(uri)  # validate readable
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"cannot read '{uri}': {e}")
    t = deps.catalog.register_output(name=name, uri=uri, version="v1", parents=[])
    # persist so user-added datasets survive a kernel restart (re-registered on startup)
    try:
        ds = metadb.get_setting("datasets", "global", default=[]) or []
        if not any(d.get("uri") == uri for d in ds):
            ds.append({"uri": uri, "name": name})
            metadb.set_setting("datasets", ds, "global")
    except Exception:  # noqa: BLE001
        pass
    return t


# --------------------------------------------------------------------------- #
# Data preview
# --------------------------------------------------------------------------- #
@api.post("/data/sample", response_model=SampleResult)
def data_sample(req: SampleRequest) -> SampleResult:
    deps = get_deps()
    if req.k is not None and req.k < 0:
        raise HTTPException(400, "k must be >= 0")
    try:
        from kernel import db
        from kernel.executors.engine import _table_to_rows
        from kernel.plugins.adapters import relation_columns
        adapter = deps.resolve_adapter(req.uri)
        with db.lock():  # serialize DuckDB access
            rel = adapter.scan(req.uri, req.columns, limit=req.k)
            cols = relation_columns(rel)          # schema is metadata — no second scan needed
            rows = _table_to_rows(rel.to_arrow_table())
            total = adapter.count(req.uri)
        return SampleResult(columns=cols, rows=rows, row_count=total,
                            truncated=(total is None or total > len(rows)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Pipeline import (bundle extension point)
# --------------------------------------------------------------------------- #
@api.post("/pipelines/import", response_model=PipelineImport)
def import_pipeline(req: ImportRequest) -> PipelineImport:
    try:
        return get_deps().importer.import_pipeline(req.config, req.params)
    except ImporterNotConfigured as e:
        raise HTTPException(501, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Processors (library picker + promote)
# --------------------------------------------------------------------------- #
@api.get("/processors", response_model=list[ProcessorDescriptor])
def list_processors() -> list[ProcessorDescriptor]:
    return get_deps().registry.list()


class PromoteRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    id: str
    title: str
    mode: str = "map"
    code: str
    input_columns: list[str] = []
    output_schema: list[ColumnSchema] = []
    blurb: str = ""


@api.post("/processors/promote", response_model=ProcessorDescriptor)
def promote_processor(req: PromoteRequest) -> ProcessorDescriptor:
    p = get_deps().registry.promote(
        id=req.id, title=req.title, mode=req.mode, code=req.code,
        input_columns=req.input_columns, output_schema=req.output_schema, blurb=req.blurb,
    )
    return p.descriptor()


# --------------------------------------------------------------------------- #
# Compile / preview / estimate / run
# --------------------------------------------------------------------------- #
def _reject_invalid(graph, deps) -> None:
    """400 on a graph the type system forbids (server-side; the frontend blocks these too)."""
    errs = graph_mod.type_errors(graph, deps.node_specs)
    if errs:
        raise HTTPException(400, "incompatible connection: " + "; ".join(errs[:5]))


@api.post("/graph/compile", response_model=CompilePlan)
def compile_graph(req: CompileRequest) -> CompilePlan:
    deps = get_deps()
    errs = graph_mod.type_errors(req.graph, deps.node_specs)
    if errs:
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=True,
                           error="incompatible connection: " + "; ".join(errs[:5]))
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)


@api.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest) -> SampleResult:
    deps = get_deps()
    k = req.k if req.k is not None else settings.preview_k
    return preview_node(req.graph, req.node_id, k,
                        deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs)


# --------------------------------------------------------------------------- #
# Agent (optional LLM planner — key stays in the kernel, never the browser)
# --------------------------------------------------------------------------- #
class AgentRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    outcome: str
    graph: dict = {}


@api.get("/agent")
def agent_get_status() -> dict:
    from kernel.agent import agent_status
    return agent_status()


@api.post("/agent")
def agent_act(req: AgentRequest) -> dict:
    from kernel.agent import agent_status, run_agent
    st = agent_status()
    if not st["available"]:
        return {"available": False, "reason": st["reason"]}
    try:
        out = run_agent(req.outcome, req.graph, get_deps())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"agent error: {type(e).__name__}: {e}")
    return {"available": True, **out}


def _row_estimate(req_graph, target_node_id, deps) -> int:
    chain = upstream_chain(req_graph, target_node_id) if target_node_id else req_graph.nodes
    for n in chain:
        if n.type == "source":
            cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
            uri = cfg.get("uri") or cfg.get("table")
            if uri:
                try:
                    c = deps.resolve_adapter(uri).count(uri)
                    if c:
                        return c
                except Exception:  # noqa: BLE001
                    pass
    return 1000


@api.post("/run/estimate", response_model=RunEstimate)
def run_estimate(req: EstimateRequest) -> RunEstimate:
    deps = get_deps()
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    return deps.pick_runner(plan).estimate(plan, rows)


@api.post("/run", response_model=RunStatus)
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
        raise HTTPException(409, "run needs confirmation (cost/placement over threshold)")
    status = runner.run(plan, req.graph, req.target_node_id, est.placement)
    deps.run_index[status.run_id] = runner  # so status/cancel/ws reach the right runner
    return status


def _runner_for(run_id: str):
    deps = get_deps()
    return deps.run_index.get(run_id, deps.runner)


@api.get("/run/{run_id}", response_model=RunStatus)
def run_status(run_id: str) -> RunStatus:
    try:
        return _runner_for(run_id).status(run_id)
    except KeyError:
        raise HTTPException(404, f"run '{run_id}' not found")


@api.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str) -> RunStatus:
    try:
        return _runner_for(run_id).cancel(run_id)
    except KeyError:
        raise HTTPException(404, f"run '{run_id}' not found")


# --------------------------------------------------------------------------- #
# Users + canvases + settings (metadata DB — per-user multi-file, internal-tool-grade auth)
# --------------------------------------------------------------------------- #
from sqlalchemy import select as _sa_select  # noqa: E402


def current_user(x_dp_user: str | None = Header(default=None)) -> str:
    """Resolve the request's user id (X-DP-User header) to a valid user, defaulting to local."""
    return metadb.resolve_user(x_dp_user)


class UserBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    name: str
    email: str | None = None


class SettingBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    scope: str = "global"   # 'global' | 'user'
    key: str
    value: object = None


@api.get("/users")
def list_users() -> list[dict]:
    with metadb.session() as s:
        return [{"id": u.id, "name": u.name, "email": u.email} for u in s.scalars(_sa_select(metadb.User))]


@api.post("/users")
def create_user(body: UserBody) -> dict:
    with metadb.session() as s:
        u = metadb.User(name=body.name, email=body.email)
        s.add(u)
        s.flush()
        return {"id": u.id, "name": u.name, "email": u.email}


@api.get("/me")
def whoami(uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        u = s.get(metadb.User, uid)
        return {"id": u.id, "name": u.name, "email": u.email}


@api.get("/canvas")
def list_canvases(uid: str = Depends(current_user)) -> list[dict]:
    with metadb.session() as s:
        rows = s.scalars(_sa_select(metadb.Canvas).where(metadb.Canvas.owner_id == uid)
                         .order_by(metadb.Canvas.updated_at.desc()))
        return [{"id": c.id, "name": c.name, "version": c.version, "updatedAt": c.updated_at.isoformat()} for c in rows]


@api.post("/canvas")
def create_canvas(doc: dict, uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        c = metadb.Canvas(owner_id=uid, name=doc.get("name") or "untitled",
                          version=doc.get("version", 1), doc=json.dumps(doc))
        s.add(c)
        s.flush()
        return {"ok": True, "id": c.id}


@api.get("/canvas/{canvas_id}")
def get_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        if not c or c.owner_id != uid:
            raise HTTPException(404, f"canvas '{canvas_id}' not found")
        return json.loads(c.doc)


@api.put("/canvas/{canvas_id}")
def put_canvas(canvas_id: str, doc: dict, uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        if c and c.owner_id != uid:
            raise HTTPException(403, "not your canvas")
        if not c:
            c = metadb.Canvas(id=canvas_id, owner_id=uid)
            s.add(c)
        c.name = doc.get("name") or c.name or "untitled"
        c.version = doc.get("version", c.version)
        c.doc = json.dumps(doc)
        return {"ok": True, "id": canvas_id}


@api.delete("/canvas/{canvas_id}")
def delete_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        if c and c.owner_id == uid:
            s.delete(c)
        return {"ok": True}


@api.get("/canvas/{canvas_id}/runs")
def canvas_runs(canvas_id: str, uid: str = Depends(current_user)) -> list[dict]:
    """Run history for a canvas (persisted, survives restarts)."""
    return metadb.list_runs(canvas_id)


@api.get("/settings")
def get_settings(uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        rows = s.scalars(_sa_select(metadb.Setting))
        out: dict = {"global": {}, "user": {}}
        for r in rows:
            if r.scope == "global":
                out["global"][r.key] = json.loads(r.value)
            elif r.scope == "user" and r.scope_id == uid:
                out["user"][r.key] = json.loads(r.value)
        return out


@api.put("/settings")
def put_setting(body: SettingBody, uid: str = Depends(current_user)) -> dict:
    scope_id = uid if body.scope == "user" else ""
    metadb.set_setting(body.key, body.value, scope=body.scope, scope_id=scope_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="Data Playground kernel", version="0.1.0")
# Restrict CORS to localhost origins only. The kernel binds to 127.0.0.1 and serves the SPA
# same-origin (and the Vite dev server proxies /api), so a wildcard is unnecessary — and a
# wildcard would let any site the user visits read this local API cross-origin (data exfiltration).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(api)
metadb.init_db()  # create metadata tables (idempotent) + seed the default local user
# re-register user-added datasets (from settings) so they survive a restart
for _d in (metadb.get_setting("datasets", "global", default=[]) or []):
    try:
        catalog_register(RegisterRequest(uri=_d["uri"], name=_d.get("name")))
    except Exception:  # noqa: BLE001
        pass


@app.websocket("/ws/run/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    try:
        while True:
            try:
                st = _runner_for(run_id).status(run_id)
            except KeyError:
                await ws.send_json({"error": "run not found"})
                break
            await ws.send_json(st.model_dump(by_alias=True))
            if st.status in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# Serve the built SPA (P6, single process). Prefer the bundled copy shipped in the wheel
# (kernel/_web), fall back to the dev build (web/dist).
_BUNDLED = os.path.join(os.path.dirname(__file__), "_web")
_DEV = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web", "dist"))
_DIST = _BUNDLED if os.path.isdir(_BUNDLED) else _DEV
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
