"""Data Playground kernel — FastAPI app factory.

A shared multi-user workspace server. One FastAPI process serves the SPA, the JSON API, the
collab/run WebSockets, and the data engine — and it runs standalone by default. Users authenticate
per-user (signed session cookies when DP_AUTH_SECRET is set; an open X-DP-User dev mode otherwise);
each user has their own canvases + workspace shares in a SQLite/Postgres metadata DB.

It is also STATELESS-WEB-READY: the runtime coordination state that used to be process-local is now
shared — metadata, run status (`run_states`), and the catalog (`catalog_entries`/`catalog_edges`)
all live in the DB, and the data itself in object storage — so several web instances behind a load
balancer converge (see README "Scaling out"). The per-instance pieces are collab (one in-memory room
per canvas → route each canvas to a consistent instance) and execution (runs on the accepting
instance; status is shared, so any instance can report it). Backend-agnostic core; the default
bundle runs fully offline (DuckDB adapter, local out-of-core runner). All routes under /api, JSON,
camelCase on the wire.

The routes live in hub.routers.{catalog,runs,workspace}; this module wires them onto the app,
gates them (see below), and owns the two WebSockets + the static SPA mount. `current_user` lives
in hub.security so the routers can depend on it without importing this module.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from hub import auth, metadb
from hub.routers import catalog, runs, workspace
from hub.routers.runs import _status_or_lost
from hub.security import current_user

app = FastAPI(title="Data Playground kernel", version="0.1.0")
# Restrict CORS to localhost origins only. The kernel binds to 127.0.0.1 and serves the SPA
# same-origin (and the Vite dev server proxies /api), so a wildcard is unnecessary — and a
# wildcard would let any site the user visits read this local API cross-origin (data exfiltration).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def _limit_request_body(request: Request, call_next):
    # SEC-10: cap every request body so a single JSON/graph/canvas/MCP payload can't exhaust memory.
    # /catalog/upload STREAMS + self-caps at DP_MAX_UPLOAD_BYTES, so it's exempt. Reads only the
    # Content-Length header — never consumes the body — so upload streaming is untouched. NOTE: header-
    # based; a Transfer-Encoding: chunked request with NO Content-Length isn't capped here (normal
    # fetch/JSON clients always set Content-Length). Graph routes still bound node/edge count + code/SQL
    # length at Pydantic parse. A hard byte budget for chunked bodies needs a pure-ASGI receive wrapper —
    # deferred (all routes are authed; local-first tool).
    from fastapi.responses import JSONResponse
    from hub.settings import settings
    if request.url.path != "/api/catalog/upload":
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                over = int(cl) > settings.max_body_bytes
            except ValueError:
                over = False
            if over:
                return JSONResponse(
                    {"detail": f"request body exceeds the {settings.max_body_bytes}-byte limit (raise DP_MAX_BODY_BYTES)"},
                    status_code=413)
    return await call_next(request)
# EVERY /api route requires a resolved user (open mode → local; auth mode → a valid session). Only the
# workspace PUBLIC router (auth status/login/logout + the login roster) is reachable pre-login. This
# keeps auth the SECURE DEFAULT — a route is gated unless it is explicitly put on the public router —
# instead of an opt-in-per-route model that once left /run, /data, /catalog, POST /users wide open.
app.include_router(workspace.public_router, prefix="/api")
_GATE = [Depends(current_user)]
app.include_router(catalog.router, prefix="/api", dependencies=_GATE)
app.include_router(runs.router, prefix="/api", dependencies=_GATE)
app.include_router(workspace.router, prefix="/api", dependencies=_GATE)

auth.reject_weak_secret()  # fail fast on a shipped/known-weak DP_AUTH_SECRET (forgeable sessions)
metadb.init_db()  # create metadata tables (idempotent) + seed the default local user
# user-added datasets survive restart via the per-row catalog_entries store (register_output
# write-throughs there); the catalog's _load_from_db restores them lazily on first read. No
# import-time re-register loop (removed the blocking probe-per-dataset startup pass; F45/F24).


# Periodic reaper — the "on a timer" half of reap_kernels' contract (boot is the other half). Every
# KERNEL_STALE_S: drop dead leases, then fail runs whose owning kernel is gone. Without this a kernel
# that crashed / OOM'd / was restarted mid-run leaves its run stuck 'running' forever (the client
# reattaches and spins), since nothing else fails it while the hub keeps living. only_kernel_runs=True
# so a kernel-less in-process run — which belongs to THIS live hub (or another live instance) — is
# never reaped mid-flight; only its dead-kernel runs are. Idempotent DB writes → safe on every instance.
def _reaper_loop() -> None:
    from hub.deps import get_deps
    while True:
        time.sleep(metadb.KERNEL_STALE_S)
        try:
            reaped = metadb.reap_kernels()
            metadb.reap_orphaned_runs(only_kernel_runs=True)
            # tear down each reaped kernel's substrate too (delete the pod+service; a no-op for the
            # local process spawner), so a crashed/fenced pod's k8s objects don't accumulate as orphans.
            kb = get_deps().kernel_backend() if reaped else None
            for canvas_id, kernel_id in reaped:
                if kb is not None:
                    kb.kill(canvas_id, kernel_id)
        except Exception:  # noqa: BLE001 — a transient DB hiccup must not kill the reaper
            pass


threading.Thread(target=_reaper_loop, daemon=True, name="dp-reaper").start()


def _cross_site_ws(ws: WebSocket) -> bool:
    """A browser page from ANOTHER origin opening this socket (cross-site WebSocket hijacking): the
    browser still attaches our cookies, so a signed session would be replayed. Reject when the Origin
    header is present and its host:port doesn't match Host. A missing Origin = a non-browser client
    (CLI/test) — CSWSH is a browser-only attack, so allow it."""
    origin = ws.headers.get("origin")
    if not origin:
        return False
    from urllib.parse import urlparse
    return urlparse(origin).netloc != ws.headers.get("host", "")


@app.websocket("/ws/run/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    # gate the status stream like the HTTP GET /run/{id} (which is behind the auth router) and ws_collab:
    # a run's status carries row counts, per-node state, error text (may embed paths) and output names.
    uid = auth.verify(ws.cookies.get("dp_session")) if auth.auth_enabled() else None
    if _cross_site_ws(ws) or (auth.auth_enabled() and not uid):
        await ws.close(code=1008)  # policy violation — cross-site origin or no valid session
        return
    # P0-AUTH-02: and, in auth mode, only for a run the caller may reach (its creator or a role on its
    # canvas) — mirrors GET /run/{id}. Off the event loop: it does small DB reads.
    from hub.routers.runs import _run_access
    if not await asyncio.to_thread(_run_access, run_id, uid):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            # _status_or_lost reads the shared run_states DB — run it off the event loop so a slow
            # query can't stall every other connection on this worker.
            st = await asyncio.to_thread(_status_or_lost, run_id)
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
    if _cross_site_ws(ws):
        await ws.close(code=1008)  # cross-site origin — reject before touching the room
        return
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


async def _broadcast_external_edit(canvas_id: str) -> None:
    """Nudge every browser tab in a canvas's collab room that the doc changed out-of-band (an MCP
    client edited it). The tab refetches + applies. A plain relay message the collab client
    understands; carries no clientId so a peer's own self-filter can't drop it."""
    room = _collab_rooms.get(canvas_id)
    if not room:
        return
    for peer in list(room):
        try:
            await peer.send_json({"type": "external-edit", "canvasId": canvas_id})
        except Exception:  # noqa: BLE001 — a dead peer is dropped, exactly like ws_collab's fan-out
            room.discard(peer)


# --- MCP over HTTP: the SAME server as `dataplay mcp` (stdio), but served IN-PROCESS by the web app.
# So a user's own Claude Code can drive this workspace via `claude mcp add --transport http <url>/mcp`
# and every tool runs on the app's real deps / runner / auth — no separate engine, no behavior drift,
# and an edit shows up LIVE in an open browser (the broadcast above). Gated by current_user like /api:
# in open mode that's the local user (zero-config); a multi-user/auth deployment needs a real token
# (MCP OAuth) a CLI can't present yet, so HTTP-MCP is a local-mode feature today (stdio covers the rest).
@app.post("/mcp")
async def mcp_http(request: Request, uid: str = Depends(current_user)):
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse, Response

    from hub import mcp as mcp_mod
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → a JSON-RPC parse error, not a 500
        return JSONResponse(mcp_mod._err_response(None, -32700, "parse error"))
    server = mcp_mod.build_http_server(uid)
    # handle() is sync and a tool (run_canvas) may block on a run — run it off the event loop so one
    # client's long call can't stall the whole server (a freedom the single-threaded stdio loop lacks).
    resp = await run_in_threadpool(server.handle, body)
    for cid in server.pg.changed_canvases:  # live-nudge any open tab for a canvas this call mutated
        await _broadcast_external_edit(cid)
    if resp is None:
        return Response(status_code=202)  # a notification / batch of only notifications — no reply body
    return JSONResponse(resp)


@app.get("/mcp")
def mcp_http_get():
    from fastapi.responses import Response
    return Response(status_code=405)  # we push no server-initiated SSE stream on this endpoint


@app.delete("/mcp")
def mcp_http_delete():
    return {"ok": True}  # stateless server — no session id to terminate


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
