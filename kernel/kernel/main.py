"""Data Playground kernel — FastAPI app factory (PRD §9).

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

The routes live in kernel.routers.{catalog,runs,workspace}; this module wires them onto the app,
gates them (see below), and owns the two WebSockets + the static SPA mount. `current_user` lives
in kernel.security so the routers can depend on it without importing this module.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from kernel import auth, metadb
from kernel.routers import catalog, runs, workspace
from kernel.routers.catalog import RegisterRequest, catalog_register
from kernel.routers.runs import _status_or_lost
from kernel.security import current_user

app = FastAPI(title="Data Playground kernel", version="0.1.0")
# Restrict CORS to localhost origins only. The kernel binds to 127.0.0.1 and serves the SPA
# same-origin (and the Vite dev server proxies /api), so a wildcard is unnecessary — and a
# wildcard would let any site the user visits read this local API cross-origin (data exfiltration).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"], allow_headers=["*"],
)
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
# re-register user-added datasets (from settings) so they survive a restart
for _d in (metadb.get_setting("datasets", "global", default=[]) or []):
    try:
        catalog_register(RegisterRequest(uri=_d["uri"], name=_d.get("name")))
    except Exception:  # noqa: BLE001
        pass


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
    if _cross_site_ws(ws) or (auth.auth_enabled() and not auth.verify(ws.cookies.get("dp_session"))):
        await ws.close(code=1008)  # policy violation — cross-site origin or no valid session
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
