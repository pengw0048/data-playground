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
from contextlib import asynccontextmanager
import logging
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


_object_attempt_reaper_stop = threading.Event()
_object_attempt_reaper_thread: threading.Thread | None = None


@asynccontextmanager
async def _lifespan(_app):
    global _object_attempt_reaper_stop, _object_attempt_reaper_thread
    _object_attempt_reaper_stop = threading.Event()
    _object_attempt_reaper_thread = threading.Thread(
        target=_object_attempt_reaper_loop, args=(_object_attempt_reaper_stop,),
        daemon=True, name="dp-object-attempt-reaper")
    _object_attempt_reaper_thread.start()
    try:
        yield
    finally:
        _object_attempt_reaper_stop.set()
        _object_attempt_reaper_thread.join(timeout=5)
        _object_attempt_reaper_thread = None

app = FastAPI(title="Data Playground kernel", version="0.1.0", lifespan=_lifespan)
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
# write-throughs there); the catalog serves them straight from the DB with indexed, paginated
# queries (no import-time re-register loop, no load-everything-into-memory on read).


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
        except Exception:  # noqa: BLE001 — a transient DB hiccup must not kill the reaper, but log it:
            # a SILENTLY failing reaper leaves stale kernels/runs un-reaped with no trace of why.
            logging.getLogger("hub").warning("reaper cycle failed (continuing)", exc_info=True)


threading.Thread(target=_reaper_loop, daemon=True, name="dp-reaper").start()


def _object_attempt_reaper_loop(stop: threading.Event) -> None:
    """Keep provider I/O off startup and the kernel/substrate reaper's progress path."""
    from hub.handoff import reap_attempts
    while not stop.wait(metadb.KERNEL_STALE_S):
        try:
            reap_attempts()
        except Exception:  # noqa: BLE001 — provider failure must not stop future GC cycles
            logging.getLogger("hub").warning("object attempt GC cycle failed (continuing)", exc_info=True)

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
    from hub.routers.runs import _run_read_access
    if not await asyncio.to_thread(_run_read_access, run_id, uid):
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
_collab_users: dict[WebSocket, str | None] = {}  # None = trusted open-mode socket; otherwise auth uid
_COLLAB_DOC_MESSAGES = frozenset(("yjs", "ysync"))


async def _live_collab_role(ws: WebSocket, canvas_id: str) -> str | None:
    """Current role for a connected socket, re-read at the document-message boundary.

    Authenticated collaboration permissions are mutable while a socket is open. Keep the uid captured
    from its signed handshake, but never cache its canvas role. The metadata DB is shared across web
    instances; running the lookup off-loop makes revocation visible without blocking other sockets.
    """
    if ws not in _collab_users:
        return None
    uid = _collab_users[ws]
    if uid is None:  # open mode is the existing trusted/single-user behavior
        return "editor"
    try:
        return await asyncio.to_thread(metadb.canvas_role, canvas_id, uid)
    except Exception:  # noqa: BLE001 — fail closed if the authorization store is unavailable
        logging.getLogger("hub").warning("collab role revalidation failed", exc_info=True)
        return None


async def _close_revoked_collab_peer(ws: WebSocket, room: set[WebSocket]) -> None:
    """Stop future fan-out to a peer whose current read access is gone."""
    room.discard(ws)
    try:
        await ws.close(code=1008)
    except Exception:  # noqa: BLE001 — already-disconnected peers are simply gone from the room
        pass


@app.websocket("/ws/collab/{canvas_id}")
async def ws_collab(ws: WebSocket, canvas_id: str):
    # when auth is enabled, the collab channel is gated exactly like the HTTP canvas routes: a valid
    # signed session cookie + some role on this canvas. (Open mode: unauthenticated, like the rest.)
    if _cross_site_ws(ws):
        await ws.close(code=1008)  # cross-site origin — reject before touching the room
        return
    uid: str | None = None
    if auth.auth_enabled():
        uid = auth.verify(ws.cookies.get("dp_session"))
        try:
            role = await asyncio.to_thread(metadb.canvas_role, canvas_id, uid) if uid else None
        except Exception:  # noqa: BLE001 — admission fails closed when the role store is unavailable
            logging.getLogger("hub").warning("collab admission role lookup failed", exc_info=True)
            role = None
        if role is None:
            await ws.close(code=1008)  # policy violation
            return
    await ws.accept()
    room = _collab_rooms.setdefault(canvas_id, set())
    room.add(ws)
    _collab_users[ws] = uid
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("clientId"):
                _collab_ids[ws] = msg["clientId"]
            msg_type = msg.get("type") if isinstance(msg, dict) else None
            if msg_type in _COLLAB_DOC_MESSAGES:
                role = await _live_collab_role(ws, canvas_id)
                if role is None:  # access removed after this socket connected
                    await _close_revoked_collab_peer(ws, room)
                    break
                # A viewer may request/read Yjs sync and send presence, but its own updates must never
                # be relayed — otherwise an editor peer could merge + autosave a laundered write.
                if msg_type == "yjs" and role not in ("owner", "editor"):
                    continue
            for peer in list(room):
                if peer is not ws:
                    # A share can be removed while this recipient's socket remains open. Revalidate
                    # before every document-bearing fan-out; presence remains cheap and viewer-safe.
                    if msg_type in _COLLAB_DOC_MESSAGES and await _live_collab_role(peer, canvas_id) is None:
                        await _close_revoked_collab_peer(peer, room)
                        continue
                    try:
                        await peer.send_json(msg)
                    except Exception:  # noqa: BLE001
                        room.discard(peer)
    except WebSocketDisconnect:
        pass
    finally:
        room.discard(ws)
        _collab_users.pop(ws, None)
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
        if await _live_collab_role(peer, canvas_id) is None:
            await _close_revoked_collab_peer(peer, room)
            continue
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


@app.get("/api/health")     # back-compat alias for /api/livez
@app.get("/api/livez")
def livez() -> dict:
    return {"ok": True}     # the process is up and serving — a pure liveness signal (no dep checks)


@app.get("/api/readyz")
def readyz():
    # readiness = can this instance actually serve? Real dep checks (not a static ok): the metadata DB
    # answers, and the DuckDB engine is responsive (not wedged). 503 (not 200) when not ready, so a load
    # balancer / k8s readiness probe pulls the instance out of rotation instead of routing to a dead one.
    from fastapi.responses import JSONResponse

    from hub import db, metadb
    checks = {"db": metadb.ping(), "engine": db.responsive(3.0)}
    ready = all(checks.values())
    return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)


@app.get("/api/version")
def version() -> dict:
    # deployment identity for operability — sha + the pluggable-backend choices + core lib versions.
    # SECRETS ARE REDACTED: the DB is reported as its dialect only (never the DP_DATABASE_URL creds).
    import platform

    import duckdb
    import pyarrow

    from hub import auth
    from hub.settings import settings
    sha = os.environ.get("DP_GIT_SHA", "").strip()
    if not sha:
        try:
            import subprocess
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                 timeout=2, cwd=os.path.dirname(__file__)).stdout.strip() or "unknown"
        except Exception:  # noqa: BLE001 — no git in a wheel deploy → set DP_GIT_SHA at build time
            sha = "unknown"
    # SCHEME only, never the full value: a scheme'd url → its scheme; a bare value (a SQLAlchemy url always
    # has "://"; DP_STORAGE_URL MAY be a bare absolute path) → a category, so an internal FS path is never
    # echoed to an unauthenticated caller.
    _db, _storage = settings.database_url, os.environ.get("DP_STORAGE_URL", "").strip()
    return {
        "sha": sha,
        "spawner": settings.kernel_spawner,
        "db": (_db.split("://", 1)[0] if "://" in _db else "sqlite"),   # dialect only — never the creds
        "storage": (_storage.split("://", 1)[0] if "://" in _storage else "local"),  # scheme only — never a path
        "auth": "enabled" if auth.auth_enabled() else "open",
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "pyarrow": pyarrow.__version__,
    }


# Serve the built SPA (P6, single process). Prefer the bundled copy shipped in the wheel
# (kernel/_web), fall back to the dev build (web/dist).
_BUNDLED = os.path.join(os.path.dirname(__file__), "_web")
_DEV = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web", "dist"))
_DIST = _BUNDLED if os.path.isdir(_BUNDLED) else _DEV
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
