"""Data Playground kernel — FastAPI app (PRD §9).

One kernel per open canvas session. Backend-agnostic core; the default bundle runs fully
offline (DuckDB adapter, in-memory catalog, local runner). All routes under /api, JSON,
camelCase on the wire.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
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
                        deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs,
                        offset=max(0, req.offset))


@api.post("/graph/schema")
def graph_schema(req: CompileRequest) -> dict:
    """Per-node output columns (metadata-only) for editor column suggestions — see executors/schema."""
    deps = get_deps()
    from kernel.executors.schema import schema_for_graph
    return schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                            deps.node_lowerings, deps.node_specs)


# --------------------------------------------------------------------------- #
# Destinations (save/open "places") — local + pluggable object-store backends
# --------------------------------------------------------------------------- #
class BrowseRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    destination_id: str
    path: str = ""


@api.get("/destinations")
def list_destinations() -> dict:
    from kernel import destinations
    ws = get_deps().workspace
    return {"destinations": destinations.presets(ws), "backends": destinations.backend_kinds()}


@api.post("/destinations/browse")
def browse_destination(req: BrowseRequest) -> dict:
    from kernel import destinations
    return destinations.browse(get_deps().workspace, req.destination_id, req.path)


class MkdirRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    destination_id: str
    path: str = ""
    name: str


@api.post("/destinations/mkdir")
def mkdir_destination(req: MkdirRequest) -> dict:
    from kernel import destinations
    return destinations.mkdir(get_deps().workspace, req.destination_id, req.path, req.name)


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
        raise HTTPException(409, "run needs confirmation (large or unknown size — a full pass)")
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


def current_user(x_dp_user: str | None = Header(default=None),
                 dp_session: str | None = Cookie(default=None)) -> str:
    """Resolve the request's user. With auth enabled (DP_AUTH_SECRET), identity comes ONLY from a
    valid signed session cookie (a raw header is not trusted); otherwise it's the X-DP-User header
    (open internal-tool mode), defaulting to the local user."""
    from kernel import auth
    if auth.auth_enabled():
        uid = auth.verify(dp_session)
        if not uid:
            raise HTTPException(401, "authentication required")
        return metadb.resolve_user(uid)
    return metadb.resolve_user(x_dp_user)


@api.get("/auth/status")
def auth_status(dp_session: str | None = Cookie(default=None)) -> dict:
    from kernel import auth
    if not auth.auth_enabled():
        return {"authEnabled": False, "userId": metadb.DEFAULT_USER_ID}
    return {"authEnabled": True, "userId": auth.verify(dp_session)}


@api.post("/auth/login")
def auth_login(body: dict, response: Response) -> dict:
    from kernel import auth
    if not auth.auth_enabled():
        return {"ok": True, "userId": metadb.resolve_user(body.get("userId"))}
    uid = body.get("userId") or ""
    # PER-USER: the password must match THIS user's own credential — knowing the instance/bootstrap
    # password no longer lets you sign in as someone else
    if not uid or not auth.verify_password(body.get("password", ""), metadb.user_password_hash(uid)):
        raise HTTPException(401, "invalid user or password")
    # Secure flag opt-in for HTTPS deployments (default off so internal http installs still work)
    response.set_cookie("dp_session", auth.sign(uid), httponly=True, samesite="lax",
                        secure=bool(os.environ.get("DP_AUTH_SECURE_COOKIE")))
    return {"ok": True, "userId": uid}


@api.post("/auth/password")
def change_password(body: dict, uid: str = Depends(current_user)) -> dict:
    """Set/rotate the CURRENT user's password. If one is already set, the old password must match."""
    from kernel import auth
    current = metadb.user_password_hash(uid)
    if current and not auth.verify_password(body.get("oldPassword", ""), current):
        raise HTTPException(403, "current password is incorrect")
    new = body.get("newPassword") or ""
    if len(new) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    metadb.set_user_password(uid, auth.hash_password(new))
    return {"ok": True}


@api.post("/auth/logout")
def auth_logout(response: Response) -> dict:
    response.delete_cookie("dp_session")
    return {"ok": True}


class UserBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    name: str
    email: str | None = None
    password: str | None = None  # set the new user's credential (required for login when auth is on)


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
    from kernel import auth
    with metadb.session() as s:
        u = metadb.User(name=body.name, email=body.email,
                        password_hash=auth.hash_password(body.password) if body.password else None)
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
    return metadb.list_canvases_for(uid)  # owned + shared + workspace-visible


@api.post("/canvas")
def create_canvas(doc: dict, uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        # honor the client's id so the canvas exists under it immediately (no orphan row, and
        # sharing/opening works without waiting for the first autosave to PUT it).
        cid = doc.get("id") or metadb._uid()
        if s.get(metadb.Canvas, cid) is None:
            s.add(metadb.Canvas(id=cid, owner_id=uid, name=doc.get("name") or "untitled",
                                version=doc.get("version", 1), doc=json.dumps(doc)))
        return {"ok": True, "id": cid}


@api.get("/canvas/{canvas_id}")
def get_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) is None:  # owner, shared, or workspace-visible
        raise HTTPException(404, f"canvas '{canvas_id}' not found")
    with metadb.session() as s:
        return json.loads(s.get(metadb.Canvas, canvas_id).doc)


@api.put("/canvas/{canvas_id}")
def put_canvas(canvas_id: str, doc: dict, uid: str = Depends(current_user)) -> dict:
    role = metadb.canvas_role(canvas_id, uid)  # None if the canvas doesn't exist yet
    doc_json = json.dumps(doc)
    version = doc.get("version", 1)
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        if c and role not in ("owner", "editor"):
            raise HTTPException(403, "you don't have edit access to this canvas")
        if not c:
            c = metadb.Canvas(id=canvas_id, owner_id=uid)  # first save → the creator owns it
            s.add(c)
        c.name = doc.get("name") or c.name or "untitled"
        c.version = version
        c.doc = doc_json
    # keep a throttled snapshot history so a bad edit is recoverable (autosave fires ~every 400ms; the
    # snapshotter dedups + rate-limits so it doesn't store every keystroke)
    metadb.snapshot_canvas(canvas_id, doc_json, version, author_id=uid)
    return {"ok": True, "id": canvas_id}


@api.get("/canvas/{canvas_id}/versions")
def get_canvas_versions(canvas_id: str, uid: str = Depends(current_user)) -> list[dict]:
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    return metadb.list_versions(canvas_id)


class RestoreRequest(BaseModel):
    version_id: str
    label: str | None = None  # optional name for the safety snapshot taken of the pre-restore state


@api.post("/canvas/{canvas_id}/restore")
def restore_canvas(canvas_id: str, req: RestoreRequest, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) not in ("owner", "editor"):
        raise HTTPException(403, "you don't have edit access to this canvas")
    doc = metadb.get_version_doc(canvas_id, req.version_id)
    if doc is None:
        raise HTTPException(404, "version not found")
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        if c is None:
            raise HTTPException(404, "not found")
        # snapshot the CURRENT state first so a restore is itself undoable, then swap in the old doc
        metadb.snapshot_canvas(canvas_id, c.doc, c.version, author_id=uid, label="before restore")
        c.doc = doc
        c.version = (c.version or 1) + 1
    return {"ok": True, "id": canvas_id, "doc": json.loads(doc)}


@api.delete("/canvas/{canvas_id}")
def delete_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) == "owner":  # only the owner can delete
        metadb.delete_canvas_cascade(canvas_id)  # also drop shares + run history + versions (no FK cascade)
    return {"ok": True}


@api.get("/canvas/{canvas_id}/shares")
def get_shares(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        vis = c.visibility if c else "private"
    return {"visibility": vis, "shares": metadb.list_shares(canvas_id)}


@api.post("/canvas/{canvas_id}/share")
def add_share(canvas_id: str, body: dict, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) != "owner":
        raise HTTPException(403, "only the owner can share")
    if "visibility" in body:
        metadb.set_visibility(canvas_id, body["visibility"])
    if body.get("userId"):
        metadb.share_canvas(canvas_id, body["userId"], body.get("role", "editor"))
    return {"ok": True}


@api.delete("/canvas/{canvas_id}/share/{user_id}")
def remove_share(canvas_id: str, user_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) != "owner":
        raise HTTPException(403, "only the owner can unshare")
    metadb.unshare_canvas(canvas_id, user_id)
    return {"ok": True}


@api.get("/canvas/{canvas_id}/runs")
def canvas_runs(canvas_id: str, uid: str = Depends(current_user)) -> list[dict]:
    """Run history for a canvas (persisted, survives restarts)."""
    if metadb.canvas_role(canvas_id, uid) is None:  # same authz as the other canvas endpoints
        raise HTTPException(404, "not found")
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


# --- realtime collaboration: a broadcast room per canvas (presence + live doc updates) ---------- #
# A dumb relay: clients hold the canvas; the server fans out each message (presence/cursor or a doc
# update) to the room's other peers, and tells peers when someone leaves. This is a usable first
# version (last-write-wins on the doc); a CRDT (Yjs) is the conflict-free hardening.
_collab_rooms: dict[str, set[WebSocket]] = {}
_collab_ids: dict[WebSocket, str] = {}  # socket -> its clientId (for leave notifications)


@app.websocket("/ws/collab/{canvas_id}")
async def ws_collab(ws: WebSocket, canvas_id: str):
    # when auth is enabled, the collab channel is gated exactly like the HTTP canvas routes: a valid
    # signed session cookie + some role on this canvas. (Open mode: unauthenticated, like the rest.)
    from kernel import auth
    can_write = True  # open mode (no auth): a single-user/trusted instance — everyone may edit
    if auth.auth_enabled():
        uid = auth.verify(ws.cookies.get("dp_session"))
        role = metadb.canvas_role(canvas_id, uid) if uid else None
        if role is None:
            await ws.close(code=1008)  # policy violation
            return
        can_write = role in ("owner", "editor")  # a viewer may watch, not mutate
    await ws.accept()
    room = _collab_rooms.setdefault(canvas_id, set())
    room.add(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("clientId"):
                _collab_ids[ws] = msg["clientId"]
            # a viewer may receive edits + presence, but its own doc updates ('yjs' carries CRDT state)
            # must NOT be relayed — else an editor peer would merge + autosave them, laundering a change
            # past the read-only boundary that put_canvas enforces.
            if not can_write and isinstance(msg, dict) and msg.get("type") == "yjs":
                continue
            for peer in list(room):
                if peer is not ws:
                    try:
                        await peer.send_json(msg)
                    except Exception:  # noqa: BLE001
                        room.discard(peer)
    except WebSocketDisconnect:
        pass
    finally:
        room.discard(ws)
        cid = _collab_ids.pop(ws, None)
        if cid:  # let peers drop this collaborator's cursor/avatar
            for peer in list(room):
                try:
                    await peer.send_json({"type": "leave", "clientId": cid})
                except Exception:  # noqa: BLE001
                    room.discard(peer)
        if not room:
            _collab_rooms.pop(canvas_id, None)


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
