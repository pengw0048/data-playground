"""Data Playground kernel — FastAPI app factory.

A shared multi-user workspace server. One FastAPI process serves the SPA, the JSON API, the
collab/run WebSockets, and the data engine — and it runs standalone by default. Users authenticate
per-user (signed session cookies when DP_AUTH_SECRET is set; an open X-DP-User dev mode otherwise);
each user has their own canvases + workspace shares in a SQLite/Postgres metadata DB.

It is also STATELESS-WEB-READY: the runtime coordination state that used to be process-local is now
shared — metadata, run status (`run_states`), and the catalog (`catalog_entries`/
`catalog_lineage_facts`)
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
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hub import auth, metadb
from hub.api_errors import (
    API_ERROR_RESPONSES,
    APIErrorCode,
    api_error_response,
    classify_http_error,
)
from hub.routers import (
    catalog, dataset_views, distribution_reports, runs, keyed_upsert, merge_columns,
    restore_revision, workspace,
)
from hub.routers.runs import _status_or_lost
from hub.security import current_user


class _RequestBodyTooLarge(HTTPException):
    def __init__(self, limit: int):
        super().__init__(
            status_code=413,
            detail=f"request body exceeds the {limit}-byte limit (raise DP_MAX_BODY_BYTES)",
        )


class TrustedProxyHeadersMiddleware:
    """Honor X-Forwarded-* only when the immediate peer is listed in DP_TRUSTED_PROXIES.

    Reads the allow-list at request time so ASGI imports (not just ``dataplay``/uvicorn) honor the
    same declared proxies, and so tests can monkeypatch the env without rebuilding the app. When the
    list is empty the ASGI peer address is left untouched — matching local-mode rate-limit identity.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        trusted = auth.trusted_proxies()
        if not trusted:
            await self.app(scope, receive, send)
            return
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        await ProxyHeadersMiddleware(self.app, trusted_hosts=trusted)(scope, receive, send)


class RequestBodyLimitMiddleware:
    """Limit non-upload request body bytes as downstream consumers receive them from ASGI."""

    def __init__(self, app: ASGIApp):
        self.app = app

    @staticmethod
    def _response(limit: int, path: str) -> JSONResponse:
        content = {"detail": f"request body exceeds the {limit}-byte limit (raise DP_MAX_BODY_BYTES)"}
        if path == "/api" or path.startswith("/api/"):
            content.update(code=APIErrorCode.PAYLOAD_TOO_LARGE, retryable=False)
        return JSONResponse(
            content,
            status_code=413,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/api/catalog/upload":
            await self.app(scope, receive, send)
            return

        from hub.settings import settings

        limit = settings.max_body_bytes
        content_length = next(
            (value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                declared_over_limit = int(content_length) > limit
            except ValueError:
                declared_over_limit = False
            if declared_over_limit:
                await self._response(limit, scope.get("path") or "")(scope, receive, send)
                return

        received = 0
        too_large = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                raise _RequestBodyTooLarge(limit)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    raise _RequestBodyTooLarge(limit)
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if not too_large:
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _RequestBodyTooLarge:
            pass
        if too_large:
            if response_started:
                # ASGI forbids a second response start. No current route reads request bytes after
                # starting its response; if a plugin does, abort that response instead of emitting an
                # invalid 200-then-413 sequence.
                raise _RequestBodyTooLarge(limit)
            # A route may translate body-read exceptions (for example JSON-RPC parse errors). Discard
            # that response and keep the transport-level size contract authoritative.
            await self._response(limit, scope.get("path") or "")(scope, receive, send)


class RequestIdMiddleware:
    """Mint/echo ``X-Request-Id``, bind it for the request, and emit bounded HTTP metrics (OPS-01)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from hub.observability import (
            MetricName, MetricUnit, emit_metric, normalize_request_id,
            reset_request_id, route_class, set_request_id,
        )

        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        raw = next(
            (value.decode("latin1") for name, value in scope.get("headers", ())
             if name.lower() == b"x-request-id"),
            None,
        )
        request_id = normalize_request_id(raw)
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id
        token = set_request_id(request_id)
        started = time.perf_counter()
        status_code = 0

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status") or 0)
                headers = list(message.get("headers") or [])
                headers = [(n, v) for n, v in headers if n.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
            else:
                await self.app(scope, receive, send_with_id)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                path = scope.get("path") or ""
                method = (scope.get("method") or "GET").upper()
                outcome = "success" if 200 <= status_code < 400 else (
                    "denied" if status_code in (401, 403) else "failure")
                labels = {
                    "method": method if method in (
                        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS") else "OTHER",
                    "route_class": route_class(path),
                    "outcome": outcome,
                }

                # Sink delivery is a bounded enqueue; plugin I/O never runs on the event loop.
                emit_metric(MetricName.HTTP_REQUESTS, 1.0, labels=labels, request_id=request_id)
                emit_metric(MetricName.HTTP_DURATION_MS, elapsed_ms, unit=MetricUnit.MILLISECONDS,
                            labels=labels, request_id=request_id)
        finally:
            reset_request_id(token)


_reaper_lifecycle_lock = threading.Lock()
_reaper_lifespan_users = 0
_object_attempt_reaper_stop: threading.Event | None = None
_object_attempt_reaper_thread: threading.Thread | None = None
_local_result_reaper_thread: threading.Thread | None = None
_durable_task_recovery_thread: threading.Thread | None = None


@asynccontextmanager
async def _lifespan(_app):
    global _reaper_lifespan_users, _object_attempt_reaper_stop
    global _object_attempt_reaper_thread, _local_result_reaper_thread
    global _durable_task_recovery_thread
    with _reaper_lifecycle_lock:
        if _reaper_lifespan_users == 0:
            existing = tuple(
                thread for thread in (
                    _object_attempt_reaper_thread, _local_result_reaper_thread,
                    _durable_task_recovery_thread)
                if thread is not None
            )
            if any(thread.is_alive() for thread in existing):
                raise RuntimeError("previous background reaper has not stopped")
            _object_attempt_reaper_stop = None
            _object_attempt_reaper_thread = None
            _local_result_reaper_thread = None
            _durable_task_recovery_thread = None

            stop = threading.Event()
            object_thread = threading.Thread(
                target=_object_attempt_reaper_loop, args=(stop,),
                daemon=True, name="dp-object-attempt-reaper")
            local_thread = threading.Thread(
                target=_local_result_reaper_loop, args=(stop,),
                daemon=True, name="dp-local-result-reaper")
            durable_thread = None
            try:
                object_thread.start()
                local_thread.start()
                # This narrow scanner is not a scheduler: it can only reclaim the single durable-local
                # Write Task type after a lease that was still live at startup expires.
                from hub.deps import get_deps
                from hub.durable_tasks import start_recovery_loop
                durable_thread = start_recovery_loop(get_deps(), stop)
            except BaseException:
                stop.set()
                for thread in (object_thread, local_thread, durable_thread):
                    if thread is None:
                        continue
                    if thread.ident is not None:
                        thread.join(timeout=5)
                _object_attempt_reaper_thread = object_thread if object_thread.is_alive() else None
                _local_result_reaper_thread = local_thread if local_thread.is_alive() else None
                _durable_task_recovery_thread = (
                    durable_thread if durable_thread is not None and durable_thread.is_alive() else None)
                if any(thread is not None for thread in (
                        _object_attempt_reaper_thread, _local_result_reaper_thread,
                        _durable_task_recovery_thread)):
                    _object_attempt_reaper_stop = stop
                raise
            _object_attempt_reaper_stop = stop
            _object_attempt_reaper_thread = object_thread
            _local_result_reaper_thread = local_thread
            _durable_task_recovery_thread = durable_thread
        _reaper_lifespan_users += 1
    try:
        yield
    finally:
        drain_observability = False
        with _reaper_lifecycle_lock:
            _reaper_lifespan_users -= 1
            if _reaper_lifespan_users == 0:
                assert _object_attempt_reaper_stop is not None
                assert _object_attempt_reaper_thread is not None
                assert _local_result_reaper_thread is not None
                assert _durable_task_recovery_thread is not None
                _object_attempt_reaper_stop.set()
                _object_attempt_reaper_thread.join(timeout=5)
                _local_result_reaper_thread.join(timeout=5)
                _durable_task_recovery_thread.join(timeout=5)
                if not _object_attempt_reaper_thread.is_alive():
                    _object_attempt_reaper_thread = None
                if not _local_result_reaper_thread.is_alive():
                    _local_result_reaper_thread = None
                if not _durable_task_recovery_thread.is_alive():
                    _durable_task_recovery_thread = None
                if all(thread is None for thread in (
                        _object_attempt_reaper_thread, _local_result_reaper_thread,
                        _durable_task_recovery_thread)):
                    _object_attempt_reaper_stop = None
                drain_observability = True
        if drain_observability:
            # Registrations are process-global and may outlive this app lifespan (embedding/tests).
            # Drain with one shared deadline; daemon workers themselves stop with the process.
            from hub.observability import drain_sinks
            drain_sinks()


app = FastAPI(title="Data Playground kernel", version="0.2.2", lifespan=_lifespan)
_SENSITIVE_VALIDATION_BODY_DETAILS = {
    "/api/auth/login": "invalid authentication request body",
    "/api/auth/password": "invalid authentication request body",
    "/api/users": "invalid authentication request body",
    "/api/settings": "invalid setting request body",
    "/api/settings/batch": "invalid settings batch request body",
}


@app.exception_handler(StarletteHTTPException)
async def _stable_api_http_error(request: Request, exc: StarletteHTTPException):
    """Add machine fields to API errors while preserving FastAPI's existing ``detail`` value."""
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        code, retryable = classify_http_error(exc)
        return api_error_response(
            status_code=exc.status_code,
            detail=exc.detail,
            code=code,
            retryable=retryable,
            headers=exc.headers,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _stable_api_internal_error(request: Request, _exc: Exception):
    """Give production callers a stable 500 body; ServerErrorMiddleware still re-raises for logs."""
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        return api_error_response(
            status_code=500,
            detail="internal server error",
            code=APIErrorCode.INTERNAL_ERROR,
            # The route may have committed before response construction failed. Unknown commit
            # outcomes must never invite an automatic repeat of a non-idempotent request.
            retryable=False,
        )
    return PlainTextResponse("Internal Server Error", status_code=500)


@app.exception_handler(RequestValidationError)
async def _safe_auth_validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's default validation response includes rejected input. Never echo a password, and never
    # ask the JSON encoder to serialize an unpaired surrogate from an attacker-controlled auth body.
    # Other routes retain FastAPI's useful field-level diagnostics.
    sensitive_detail = _SENSITIVE_VALIDATION_BODY_DETAILS.get(request.url.path)
    if sensitive_detail is not None:
        return api_error_response(
            status_code=422,
            detail=sensitive_detail,
            code=APIErrorCode.VALIDATION_ERROR,
            retryable=False,
        )
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        return api_error_response(
            status_code=422,
            detail=exc.errors(),
            code=APIErrorCode.VALIDATION_ERROR,
            retryable=False,
        )
    return await request_validation_exception_handler(request, exc)


# Restrict CORS to localhost origins only. The kernel binds to 127.0.0.1 and serves the SPA
# same-origin (and the Vite dev server proxies /api), so a wildcard is unnecessary — and a
# wildcard would let any site the user visits read this local API cross-origin (data exfiltration).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"], allow_headers=["*"],
)
# SEC-10: count actual ASGI request bytes as endpoints consume them instead of trusting Content-Length.
# An endpoint that ignores its body does not drain it into application memory. Dataset uploads remain
# exempt because /catalog/upload streams and independently enforces DP_MAX_UPLOAD_BYTES as bytes arrive.
app.add_middleware(RequestBodyLimitMiddleware)
# OPS-01: every HTTP response echoes X-Request-Id; the id is available via contextvar for run/audit
# correlation. Installed after body-limit so it is the outer middleware (runs first / finishes last).
app.add_middleware(RequestIdMiddleware)
# Shared-mode reverse proxies declare DP_TRUSTED_PROXIES; normalize client/scheme from X-Forwarded-*
# only for those peers so login rate limiting keys the real client and spoofed headers are ignored.
app.add_middleware(TrustedProxyHeadersMiddleware)
# EVERY /api route requires a resolved user (open mode → local; auth mode → a valid session). Only the
# workspace PUBLIC router (auth status/login/logout + the login roster) is reachable pre-login. This
# keeps auth the SECURE DEFAULT — a route is gated unless it is explicitly put on the public router —
# instead of an opt-in-per-route model that once left /run, /data, /catalog, POST /users wide open.
app.include_router(workspace.public_router, prefix="/api", responses=API_ERROR_RESPONSES)
_GATE = [Depends(current_user)]
app.include_router(catalog.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)
app.include_router(dataset_views.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)
app.include_router(
    distribution_reports.router, prefix="/api", dependencies=_GATE,
    responses=API_ERROR_RESPONSES)
app.include_router(
    merge_columns.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)
app.include_router(
    restore_revision.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)
app.include_router(
    keyed_upsert.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)
app.include_router(runs.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)
app.include_router(workspace.router, prefix="/api", dependencies=_GATE, responses=API_ERROR_RESPONSES)

# Fail before opening the metadata DB when the hub has a missing/known-weak signing secret, or when
# shared mode lacks Secure cookies / a trusted TLS-proxy declaration. A spawned kernel
# child keeps DP_AUTH_MODE only for confinement and never imports this web-app module.
auth.reject_weak_secret()
auth.reject_unsafe_transport()
metadb.init_db()  # SQLite: locked local init; production DB: strict schema-head check, never DDL
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


def _local_result_reaper_loop(stop: threading.Event) -> None:
    """Run bounded exact local-result retention away from run completion latency."""
    from hub.deps import get_deps

    while not stop.wait(metadb.KERNEL_STALE_S):
        try:
            raw_retention = os.environ.get(
                "DP_MANAGED_REVISION_RETENTION_SECONDS", str(7 * 24 * 60 * 60))
            retention = float(raw_retention)
            if not math.isfinite(retention) or retention < 0:
                raise ValueError(
                    "DP_MANAGED_REVISION_RETENTION_SECONDS must be a finite non-negative number")
            revision_gc = metadb.managed_local_file_revision_gc_batch(
                retention, limit=50)
            if revision_gc["retired"]:
                logging.getLogger("hub").info(
                    "managed local revision GC retired %s ledger entries",
                    revision_gc["retired"])
            if revision_gc["has_more"]:
                logging.getLogger("hub").warning(
                    "managed local revision retention pressure remains after bounded GC batch")
        except Exception:  # one transient DB/config failure must not stop result-artifact cleanup
            logging.getLogger("hub").warning(
                "managed local revision GC cycle failed (continuing)", exc_info=True)
        try:
            prune = getattr(get_deps().storage, "prune_results", None)
            if callable(prune):
                prune()
        except Exception:  # one transient DB/filesystem failure must not stop future passes
            logging.getLogger("hub").warning(
                "local result GC cycle failed (continuing)", exc_info=True)

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
    auth_mode = auth.auth_enabled()
    token = ws.cookies.get("dp_session") if auth_mode else None
    uid = await asyncio.to_thread(auth.verify, token) if auth_mode else None
    if _cross_site_ws(ws) or (auth_mode and not uid):
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
            # The cookie was authenticated at admission, but its epoch and this run's canvas share are
            # both mutable. Recheck both at the payload boundary so an already-open socket cannot keep
            # receiving row counts, errors, or output locations after either permission is revoked.
            if token is not None:
                try:
                    still_allowed = await asyncio.to_thread(
                        lambda: auth.verify(token) == uid and _run_read_access(run_id, uid)
                    )
                except Exception:  # noqa: BLE001 — authorization-store failure must fail closed
                    logging.getLogger("hub").warning("run websocket access revalidation failed", exc_info=True)
                    still_allowed = False
                if not still_allowed:
                    await ws.close(code=1008)
                    break
            await ws.send_json(st.model_dump(by_alias=True))
            if st.status in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass


# --- realtime collaboration: a room per canvas (presence + directed Yjs bootstrap) --------------- #
# The relay remains opaque to Yjs bytes, but it owns the authenticity and readiness state machine.
# Exactly one ready writer answers each joiner's state-vector request; when no ready writer exists,
# exactly one writer is elected to seed. An unsynchronized or read-only socket can never volunteer as
# authority, and client frames can never impersonate the relay's `type: server` control envelope.
_collab_rooms: dict[str, set[WebSocket]] = {}
_collab_ids: dict[WebSocket, str] = {}  # socket -> its clientId (for leave notifications)
_collab_canvas: dict[WebSocket, str] = {}
_collab_synced: set[WebSocket] = set()
_collab_order: dict[WebSocket, int] = {}
_collab_order_sequence = 0
_collab_room_locks: dict[str, asyncio.Lock] = {}
_collab_room_lock_refs: dict[str, int] = {}
_collab_room_lock_guard = threading.Lock()
_collab_last_state: dict[WebSocket, tuple[str, str | None]] = {}
_collab_roles: dict[WebSocket, str] = {}
_collab_outboxes: dict[WebSocket, asyncio.Queue[dict[str, object]]] = {}
_collab_sender_tasks: dict[WebSocket, asyncio.Task[None]] = {}


def _collab_now() -> float:
    """Monotonic collaboration clock, isolated so authorization bounds are deterministic in tests."""
    return time.monotonic()


@dataclass
class _CollabSession:
    user_id: str | None
    token: str | None  # the exact admission token; None only for a trusted open-mode socket
    role: str = "editor"
    role_revalidated_at: float = 0.0
    revalidation_task: asyncio.Task[str | None] | None = field(default=None, repr=False)


_collab_sessions: dict[WebSocket, _CollabSession] = {}


@dataclass
class _CollabSyncPlan:
    mode: str  # seed | sync | unavailable
    request_id: str | None
    responder: WebSocket | None = None
    attempted_responders: set[WebSocket] = field(default_factory=set)
    seed_update_seen: bool = False
    request_sent: bool = False
    response_forwarded: bool = False
    timed_out: bool = False
    timeout_phase: str | None = None  # request | response | ready
    timer_task: asyncio.Task[None] | None = None


_collab_plans: dict[WebSocket, _CollabSyncPlan] = {}


@dataclass
class _CollabSeedElection:
    """Room-level memory for one bounded pass through the current seed candidates."""

    attempted: set[WebSocket] = field(default_factory=set)
    retry_task: asyncio.Task[None] | None = None


_collab_seed_elections: dict[str, _CollabSeedElection] = {}
_COLLAB_CLIENT_MESSAGES = frozenset(("presence", "yjs", "ysync", "sync-ready"))
_COLLAB_SERVER_ONLY_MESSAGES = frozenset((
    "server", "room-state", "leave", "external-edit", "ownership", "authority", "sync-plan",
    "protocol-error",
))
_COLLAB_SEED_READY_TIMEOUT_SECONDS = 5.0
_COLLAB_SYNC_REQUEST_TIMEOUT_SECONDS = 5.0
_COLLAB_SYNC_RESPONSE_TIMEOUT_SECONDS = 5.0
_COLLAB_SYNC_READY_TIMEOUT_SECONDS = 5.0
_COLLAB_UNAVAILABLE_RETRY_SECONDS = 5.0
_COLLAB_SEND_TIMEOUT_SECONDS = 1.0
_COLLAB_ROLE_REVALIDATION_INTERVAL_SECONDS = 5.0
_COLLAB_ROLE_RECHECK_TIMEOUT_SECONDS = 1.0
_COLLAB_OUTBOX_CAPACITY = 256


def _retain_collab_room_lock(canvas_id: str) -> asyncio.Lock:
    """Pin one lock in the registry while a socket/task may wait on or hold it.

    The guard covers the no-await registry transition. Without the reference, a last-leaving socket
    could pop its lock after a new joiner captured it but before that joiner acquired it, allowing a
    third connection to create a second lock for the same canvas.
    """
    with _collab_room_lock_guard:
        lock = _collab_room_locks.setdefault(canvas_id, asyncio.Lock())
        _collab_room_lock_refs[canvas_id] = _collab_room_lock_refs.get(canvas_id, 0) + 1
        return lock


def _release_collab_room_lock(canvas_id: str, lock: asyncio.Lock) -> None:
    with _collab_room_lock_guard:
        if _collab_room_locks.get(canvas_id) is not lock:
            return
        remaining = _collab_room_lock_refs.get(canvas_id, 0) - 1
        if remaining > 0:
            _collab_room_lock_refs[canvas_id] = remaining
        else:
            _collab_room_lock_refs.pop(canvas_id, None)
            _collab_room_locks.pop(canvas_id, None)


async def _live_collab_role(ws: WebSocket, canvas_id: str) -> str | None:
    """Read the current session and role for a connected socket from the authorization store.

    Authenticated sessions and collaboration permissions are mutable while a socket is open. Reverify
    the exact token captured at admission, then read the current canvas role. The metadata DB is shared
    across web instances; running both lookups off-loop keeps revalidation from blocking peers.
    """
    session = _collab_sessions.get(ws)
    if session is None:
        return None
    if session.token is None:  # open mode is the existing trusted/single-user behavior
        return "editor"
    try:
        def _current_role() -> str | None:
            if auth.verify(session.token) != session.user_id:
                return None
            return metadb.canvas_role(canvas_id, session.user_id)

        return await asyncio.to_thread(_current_role)
    except Exception:  # noqa: BLE001 — fail closed if the authorization store is unavailable
        logging.getLogger("hub").warning("collab session/access revalidation failed", exc_info=True)
        return None


async def _bounded_live_collab_role(ws: WebSocket, canvas_id: str) -> str | None:
    """Fail closed when the mutable authorization store cannot answer promptly."""
    try:
        return await asyncio.wait_for(
            _live_collab_role(ws, canvas_id), timeout=_COLLAB_ROLE_RECHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logging.getLogger("hub").warning(
            "collab access revalidation timed out canvas=%s", canvas_id,
        )
        return None


async def _current_collab_role(ws: WebSocket, canvas_id: str) -> str | None:
    """Return admitted authorization while fresh, then share one fail-closed revalidation.

    The five-second monotonic interval bounds revocation and role-downgrade visibility for active
    sockets without multiplying metadata reads by frame rate and fanout recipients. Concurrent frames
    share ``revalidation_task``, so each connection performs at most one store lookup per interval.
    """
    session = _collab_sessions.get(ws)
    if session is None:
        return None
    if session.token is None:  # trusted open mode has no mutable session or per-user role store
        return session.role
    if _collab_now() - session.role_revalidated_at < _COLLAB_ROLE_REVALIDATION_INTERVAL_SECONDS:
        return session.role

    task = session.revalidation_task
    if task is None:
        async def revalidate() -> str | None:
            try:
                role = await _bounded_live_collab_role(ws, canvas_id)
                if role is not None and _collab_sessions.get(ws) is session:
                    session.role = role
                    session.role_revalidated_at = _collab_now()
                return role
            finally:
                if session.revalidation_task is asyncio.current_task():
                    session.revalidation_task = None

        task = asyncio.create_task(revalidate())
        session.revalidation_task = task
    # A cancelled frame must not cancel the shared lookup needed by another sender/recipient path.
    return await asyncio.shield(task)


def _ordered_collab_peers(room: set[WebSocket]) -> list[WebSocket]:
    return sorted(room, key=lambda peer: _collab_order.get(peer, 2**63))


def _queue_collab_locked(ws: WebSocket, payload: dict[str, object]) -> bool:
    """Append one frame without awaiting network I/O. Caller holds the room lock.

    A per-peer queue is the ordering boundary for the protocol: once a sync baseline is appended,
    later document deltas for that peer cannot overtake it. The finite queue also prevents a slow or
    malicious client from turning the relay into unbounded memory storage.
    """
    outbox = _collab_outboxes.get(ws)
    if outbox is None:
        return False
    try:
        outbox.put_nowait(payload)
    except asyncio.QueueFull:
        return False
    return True


def _detach_collab_peer_locked(
    ws: WebSocket, canvas_id: str, room: set[WebSocket], *, announce: bool = True,
) -> asyncio.Task[None] | None:
    """Remove all in-memory state for a peer without awaiting. Caller holds the room lock."""
    room.discard(ws)
    _collab_sessions.pop(ws, None)
    _collab_canvas.pop(ws, None)
    _collab_roles.pop(ws, None)
    _collab_synced.discard(ws)
    _collab_order.pop(ws, None)
    abandoned = _collab_plans.pop(ws, None)
    if abandoned is not None:
        _cancel_collab_plan_timer(abandoned)
    _collab_last_state.pop(ws, None)
    _collab_outboxes.pop(ws, None)
    sender_task = _collab_sender_tasks.pop(ws, None)
    if sender_task is not None and sender_task is not asyncio.current_task() and not sender_task.done():
        sender_task.cancel()
    client_id = _collab_ids.pop(ws, None)
    if announce and client_id:
        leave: dict[str, object] = {
            "type": "server", "event": "leave", "clientId": client_id,
        }
        # A full recipient queue already has a bounded sender that will fail and detach it. A leave
        # notification is advisory, so skipping it is safer than recursively mutating the room here.
        for peer in _ordered_collab_peers(room):
            _queue_collab_locked(peer, leave)
    if not room:
        _collab_rooms.pop(canvas_id, None)
        _clear_collab_seed_election(canvas_id)
    return sender_task


async def _close_collab_socket(ws: WebSocket, code: int) -> None:
    try:
        await asyncio.wait_for(ws.close(code=code), timeout=_COLLAB_SEND_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — already-disconnected or wedged sockets are fully detached
        pass


async def _finish_collab_removal(
    ws: WebSocket, sender_task: asyncio.Task[None] | None, *, close_code: int,
) -> None:
    if sender_task is not None and sender_task is not asyncio.current_task():
        try:
            await asyncio.wait_for(
                asyncio.gather(sender_task, return_exceptions=True),
                timeout=_COLLAB_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            pass
    await _close_collab_socket(ws, close_code)


async def _run_collab_sender(
    ws: WebSocket, canvas_id: str, outbox: asyncio.Queue[dict[str, object]],
) -> None:
    """Own all ordinary sends for one peer; slow peers cannot block the room or each other."""
    try:
        while True:
            payload = await outbox.get()
            await asyncio.wait_for(ws.send_json(payload), timeout=_COLLAB_SEND_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — timeout/disconnect both make this peer unusable
        logging.getLogger("hub").warning(
            "collab websocket sender failed canvas=%s", canvas_id, exc_info=True,
        )
        lock = _retain_collab_room_lock(canvas_id)
        try:
            async with lock:
                room = _collab_rooms.get(canvas_id)
                if room is not None and ws in room and _collab_outboxes.get(ws) is outbox:
                    _detach_collab_peer_locked(ws, canvas_id, room)
                    _replan_collab_room_locked(room, canvas_id)
        finally:
            _release_collab_room_lock(canvas_id, lock)
        await _close_collab_socket(ws, 1011)


def _new_collab_plan(
    mode: str, responder: WebSocket | None = None, *, attempted: set[WebSocket] | None = None,
) -> _CollabSyncPlan:
    request_id = secrets.token_urlsafe(18) if mode in ("seed", "sync") else None
    return _CollabSyncPlan(
        mode=mode, request_id=request_id, responder=responder,
        attempted_responders=set(attempted or ()),
    )


def _cancel_collab_plan_timer(plan: _CollabSyncPlan) -> None:
    task, plan.timer_task = plan.timer_task, None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


def _cancel_collab_seed_retry(election: _CollabSeedElection) -> None:
    task, election.retry_task = election.retry_task, None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


def _clear_collab_seed_election(canvas_id: str) -> None:
    election = _collab_seed_elections.pop(canvas_id, None)
    if election is not None:
        _cancel_collab_seed_retry(election)


def _ensure_collab_plan_deadline(
    canvas_id: str, peer: WebSocket, plan: _CollabSyncPlan,
) -> None:
    if plan.timer_task is not None:
        return
    if plan.mode == "seed":
        _schedule_collab_seed_deadline(canvas_id, peer, plan)
    elif plan.mode == "sync":
        phase = "request" if not plan.request_sent else "response" if not plan.response_forwarded else "ready"
        _schedule_collab_sync_phase_deadline(canvas_id, peer, plan, phase)


def _replan_collab_room_locked(room: set[WebSocket], canvas_id: str) -> None:
    """Recompute bootstrap ownership using cached roles and enqueue state transitions.

    The caller holds this canvas's room lock. This function deliberately contains no await: database
    revalidation happens before entering the critical section, and network delivery belongs to each
    peer's bounded sender task. Timers therefore observe their actual deadlines even when a role store
    or websocket is slow.
    """
    if not room:
        _clear_collab_seed_election(canvas_id)
        return
    while room:
        ready_writers = [
            peer for peer in _ordered_collab_peers(room)
            if peer in _collab_synced and _collab_roles.get(peer) in ("owner", "editor")
        ]
        unsynced = [peer for peer in _ordered_collab_peers(room) if peer not in _collab_synced]
        desired: dict[WebSocket, _CollabSyncPlan] = {}
        seed_exhausted = False
        if ready_writers:
            _clear_collab_seed_election(canvas_id)
            for peer in unsynced:
                previous = _collab_plans.get(peer)
                attempted = set(previous.attempted_responders) if previous is not None else set()
                if previous is not None and previous.mode == "sync" and previous.timed_out:
                    if previous.timeout_phase in ("request", "ready"):
                        # The authority did not fail in these phases. Fence the current authority set
                        # only to preserve the joiner's unavailable/backoff window; a newly ready writer
                        # may still trigger an immediate retry, and the scheduled retry clears the fence.
                        attempted.update(ready_writers)
                        desired[peer] = _new_collab_plan("unavailable", attempted=attempted)
                        continue
                    if previous.responder is not None:  # response timeout: rotate the silent authority
                        attempted.add(previous.responder)
                elif (
                    previous is not None and previous.mode == "sync"
                    and previous.responder in ready_writers
                ):
                    desired[peer] = previous
                    continue
                candidates = [responder for responder in ready_writers if responder not in attempted]
                if candidates:
                    desired[peer] = _new_collab_plan("sync", candidates[0], attempted=attempted)
                elif previous is not None and previous.mode == "unavailable":
                    desired[peer] = previous
                else:
                    desired[peer] = _new_collab_plan("unavailable", attempted=attempted)
        else:
            writers = [
                peer for peer in unsynced if _collab_roles.get(peer) in ("owner", "editor")
            ]
            if writers:
                election = _collab_seed_elections.setdefault(canvas_id, _CollabSeedElection())
                election.attempted.intersection_update(writers)
                active_seed: WebSocket | None = None
                for peer in writers:
                    previous = _collab_plans.get(peer)
                    if previous is None or previous.mode != "seed":
                        continue
                    if previous.timed_out:
                        election.attempted.add(peer)
                    else:
                        active_seed = peer
                        desired[peer] = previous
                    break

                if active_seed is None:
                    candidates = [peer for peer in writers if peer not in election.attempted]
                    if candidates:
                        _cancel_collab_seed_retry(election)
                        desired[candidates[0]] = _new_collab_plan("seed")
                    else:
                        seed_exhausted = True
                        if election.retry_task is None:
                            _schedule_collab_seed_retry(canvas_id, election)
            else:
                _clear_collab_seed_election(canvas_id)

        for peer in list(_collab_plans):
            if _collab_canvas.get(peer) == canvas_id and peer not in desired:
                removed = _collab_plans.pop(peer)
                _cancel_collab_plan_timer(removed)
        for peer, plan in desired.items():
            previous = _collab_plans.get(peer)
            if previous is not plan:
                if previous is not None:
                    _cancel_collab_plan_timer(previous)
                _collab_plans[peer] = plan
            if plan.mode == "unavailable" and plan.timer_task is None:
                _schedule_collab_unavailable_retry(canvas_id, peer, plan)

        send_failed: list[WebSocket] = []
        for peer in _ordered_collab_peers(room):
            plan = _collab_plans.get(peer)
            if peer in _collab_synced:
                state = ("ready", None)
            elif plan is not None:
                state = (plan.mode, plan.request_id if plan.mode != "unavailable" else None)
            elif seed_exhausted:
                state = ("unavailable", None)
            else:
                state = ("wait", None)
            if _collab_last_state.get(peer) == state:
                if plan is not None:
                    _ensure_collab_plan_deadline(canvas_id, peer, plan)
                continue
            payload: dict[str, str] = {"type": "server", "event": "room-state", "mode": state[0]}
            if state[1] is not None:
                payload["requestId"] = state[1]
            if _queue_collab_locked(peer, payload):
                _collab_last_state[peer] = state
                if plan is not None:
                    _ensure_collab_plan_deadline(canvas_id, peer, plan)
            else:
                send_failed.append(peer)
        if not send_failed:
            return
        for peer in send_failed:
            if peer in room:
                _detach_collab_peer_locked(peer, canvas_id, room)
                asyncio.create_task(_close_collab_socket(peer, 1013))


def _schedule_collab_seed_deadline(
    canvas_id: str, seed: WebSocket, plan: _CollabSyncPlan,
) -> None:
    """Lease the complete seed handshake; expiry rotates candidates but can never grant readiness."""
    lock = _retain_collab_room_lock(canvas_id)

    async def expire() -> None:
        await asyncio.sleep(_COLLAB_SEED_READY_TIMEOUT_SECONDS)
        async with lock:
            room = _collab_rooms.get(canvas_id)
            current = _collab_plans.get(seed)
            if (
                room is not None and seed in room and current is plan and plan.mode == "seed"
                and seed not in _collab_synced
            ):
                plan.timer_task = None
                plan.timed_out = True
                _collab_last_state.pop(seed, None)
                _replan_collab_room_locked(room, canvas_id)

    plan.timer_task = asyncio.create_task(expire())
    plan.timer_task.add_done_callback(lambda _task: _release_collab_room_lock(canvas_id, lock))


def _schedule_collab_sync_phase_deadline(
    canvas_id: str, joiner: WebSocket, plan: _CollabSyncPlan, phase: str,
) -> None:
    """Bound every sync phase; expiry only replans and can never grant readiness itself."""
    delay = {
        "request": _COLLAB_SYNC_REQUEST_TIMEOUT_SECONDS,
        "response": _COLLAB_SYNC_RESPONSE_TIMEOUT_SECONDS,
        "ready": _COLLAB_SYNC_READY_TIMEOUT_SECONDS,
    }[phase]
    lock = _retain_collab_room_lock(canvas_id)

    async def expire() -> None:
        await asyncio.sleep(delay)
        async with lock:
            room = _collab_rooms.get(canvas_id)
            current = _collab_plans.get(joiner)
            phase_still_pending = (
                (phase == "request" and not plan.request_sent)
                or (phase == "response" and plan.request_sent and not plan.response_forwarded)
                or (phase == "ready" and plan.response_forwarded)
            )
            if (
                room is not None and joiner in room and current is plan and plan.mode == "sync"
                and joiner not in _collab_synced and phase_still_pending
            ):
                plan.timer_task = None
                plan.timed_out = True
                plan.timeout_phase = phase
                _collab_last_state.pop(joiner, None)
                _replan_collab_room_locked(room, canvas_id)

    plan.timer_task = asyncio.create_task(expire())
    plan.timer_task.add_done_callback(lambda _task: _release_collab_room_lock(canvas_id, lock))


def _schedule_collab_unavailable_retry(
    canvas_id: str, joiner: WebSocket, plan: _CollabSyncPlan,
) -> None:
    """Surface unavailable first, then retry ready authorities without ever electing an empty peer."""
    lock = _retain_collab_room_lock(canvas_id)

    async def retry() -> None:
        await asyncio.sleep(_COLLAB_UNAVAILABLE_RETRY_SECONDS)
        async with lock:
            room = _collab_rooms.get(canvas_id)
            if room is not None and joiner in room and _collab_plans.get(joiner) is plan:
                plan.timer_task = None
                _collab_plans.pop(joiner, None)
                _collab_last_state.pop(joiner, None)
                _replan_collab_room_locked(room, canvas_id)

    plan.timer_task = asyncio.create_task(retry())
    plan.timer_task.add_done_callback(lambda _task: _release_collab_room_lock(canvas_id, lock))


def _schedule_collab_seed_retry(canvas_id: str, election: _CollabSeedElection) -> None:
    """Expose seed exhaustion, then begin a fresh bounded pass through connected writers."""
    lock = _retain_collab_room_lock(canvas_id)

    async def retry() -> None:
        await asyncio.sleep(_COLLAB_UNAVAILABLE_RETRY_SECONDS)
        async with lock:
            room = _collab_rooms.get(canvas_id)
            if room is not None and room and _collab_seed_elections.get(canvas_id) is election:
                election.retry_task = None
                election.attempted.clear()
                for peer in room:
                    _collab_last_state.pop(peer, None)
                _replan_collab_room_locked(room, canvas_id)

    election.retry_task = asyncio.create_task(retry())
    election.retry_task.add_done_callback(lambda _task: _release_collab_room_lock(canvas_id, lock))


async def _send_collab_protocol_error(ws: WebSocket, code: str, canvas_id: str) -> None:
    """Make a rejected client frame observable without echoing membership or document state."""
    logging.getLogger("hub").warning("collab protocol violation canvas=%s code=%s", canvas_id, code)
    try:
        await asyncio.wait_for(
            ws.send_json({"type": "server", "event": "protocol-error", "code": code}),
            timeout=_COLLAB_SEND_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — close still fences the offending socket
        pass
    await _close_collab_socket(ws, 1008)


async def _terminate_collab_peer(
    ws: WebSocket, canvas_id: str, lock: asyncio.Lock, code: str,
) -> None:
    """Fence an offender before doing the observable, bounded protocol-error I/O."""
    sender_task: asyncio.Task[None] | None = None
    async with lock:
        room = _collab_rooms.get(canvas_id)
        if room is not None and ws in room:
            sender_task = _detach_collab_peer_locked(ws, canvas_id, room)
            _replan_collab_room_locked(room, canvas_id)
    if sender_task is not None and sender_task is not asyncio.current_task():
        await asyncio.gather(sender_task, return_exceptions=True)
    await _send_collab_protocol_error(ws, code, canvas_id)


async def _refresh_collab_sender_role(
    ws: WebSocket, canvas_id: str, lock: asyncio.Lock,
) -> str | None:
    """Apply the sender's admitted or periodically revalidated role outside the room lock."""
    role = await _current_collab_role(ws, canvas_id)
    sender_task: asyncio.Task[None] | None = None
    revoked = False
    async with lock:
        room = _collab_rooms.get(canvas_id)
        if room is None or ws not in room:
            return None
        if role is None:
            sender_task = _detach_collab_peer_locked(ws, canvas_id, room)
            _replan_collab_room_locked(room, canvas_id)
            revoked = True
        else:
            previous = _collab_roles.get(ws)
            _collab_roles[ws] = role
            if previous != role:
                # A viewer promoted while connected must immediately enter seed/sync planning; a
                # downgraded authority must immediately stop serving joiners.
                _replan_collab_room_locked(room, canvas_id)
    if revoked:
        await _finish_collab_removal(ws, sender_task, close_code=1008)
        return None
    return role


def _collab_peer_has_ordered_baseline(peer: WebSocket) -> bool:
    if peer in _collab_synced:
        return True
    plan = _collab_plans.get(peer)
    # response_forwarded is set only after the baseline was appended to this peer's outbox. The
    # same FIFO therefore makes subsequent ordinary deltas safe before the ready ack reaches us.
    return plan is not None and plan.mode == "sync" and plan.response_forwarded


async def _fanout_collab(
    canvas_id: str,
    lock: asyncio.Lock,
    sender: WebSocket | None,
    payload: dict[str, object],
    *,
    baseline_required: bool = False,
) -> None:
    """Check cached recipient authorization, then append sanitized frames under the room lock."""
    async with lock:
        room = _collab_rooms.get(canvas_id)
        if room is None:
            return
        recipients = [peer for peer in _ordered_collab_peers(room) if peer is not sender]
    if not recipients:
        return

    async def recheck(peer: WebSocket) -> tuple[WebSocket, str | None]:
        return peer, await _current_collab_role(peer, canvas_id)

    checks = [asyncio.create_task(recheck(peer)) for peer in recipients]
    removals: list[asyncio.Task[None]] = []
    try:
        # Process fast authorization results immediately. A wedged role lookup for one peer cannot
        # delay a healthy recipient, and every lookup is independently bounded and fail-closed.
        for completed in asyncio.as_completed(checks):
            peer, role = await completed
            removed: tuple[asyncio.Task[None] | None, int] | None = None
            async with lock:
                room = _collab_rooms.get(canvas_id)
                if room is None or peer not in room:
                    continue
                if role is None:
                    removed = (_detach_collab_peer_locked(peer, canvas_id, room), 1008)
                    _replan_collab_room_locked(room, canvas_id)
                else:
                    if _collab_roles.get(peer) != role:
                        _collab_roles[peer] = role
                        _replan_collab_room_locked(room, canvas_id)
                    if baseline_required and not _collab_peer_has_ordered_baseline(peer):
                        continue
                    if not _queue_collab_locked(peer, payload):
                        removed = (_detach_collab_peer_locked(peer, canvas_id, room), 1013)
                        _replan_collab_room_locked(room, canvas_id)
            if removed is not None:
                removals.append(asyncio.create_task(
                    _finish_collab_removal(peer, removed[0], close_code=removed[1]),
                ))
    finally:
        for check in checks:
            if not check.done():
                check.cancel()
        await asyncio.gather(*checks, return_exceptions=True)
        if removals:
            await asyncio.gather(*removals)


@app.websocket("/ws/collab/{canvas_id}")
async def ws_collab(ws: WebSocket, canvas_id: str):
    global _collab_order_sequence
    # when auth is enabled, the collab channel is gated exactly like the HTTP canvas routes: a valid
    # signed session cookie + some role on this canvas. (Open mode: unauthenticated, like the rest.)
    if _cross_site_ws(ws):
        await ws.close(code=1008)  # cross-site origin — reject before touching the room
        return
    uid: str | None = None
    token: str | None = None
    admission_role = "editor"
    if auth.auth_enabled():
        token = ws.cookies.get("dp_session")
        uid = await asyncio.to_thread(auth.verify, token)
        try:
            admission_role = await asyncio.to_thread(metadb.canvas_role, canvas_id, uid) if uid else None
        except Exception:  # noqa: BLE001 — admission fails closed when the role store is unavailable
            logging.getLogger("hub").warning("collab admission role lookup failed", exc_info=True)
            admission_role = None
        if admission_role is None:
            await ws.close(code=1008)  # policy violation
            return
    await ws.accept()
    lock = _retain_collab_room_lock(canvas_id)
    try:
        # Registration belongs inside the lifecycle try: cancellation while waiting for this lock
        # must still release the retained lock and clean any state that became visible.
        async with lock:
            room = _collab_rooms.setdefault(canvas_id, set())
            outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue(
                maxsize=_COLLAB_OUTBOX_CAPACITY,
            )
            _collab_sessions[ws] = _CollabSession(
                uid, token, admission_role, _collab_now(),
            )
            _collab_canvas[ws] = canvas_id
            _collab_roles[ws] = admission_role
            _collab_outboxes[ws] = outbox
            _collab_order_sequence += 1
            _collab_order[ws] = _collab_order_sequence
            room.add(ws)
            _collab_sender_tasks[ws] = asyncio.create_task(
                _run_collab_sender(ws, canvas_id, outbox),
            )
            _replan_collab_room_locked(room, canvas_id)

        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:  # noqa: BLE001 — malformed JSON is a protocol violation, not a 500
                await _terminate_collab_peer(ws, canvas_id, lock, "malformed-json")
                break

            role = await _refresh_collab_sender_role(ws, canvas_id, lock)
            if role is None:
                break
            msg_type = msg.get("type") if isinstance(msg, dict) else None
            if msg_type in _COLLAB_SERVER_ONLY_MESSAGES:
                await _terminate_collab_peer(ws, canvas_id, lock, "server-frame-forgery")
                break
            if msg_type not in _COLLAB_CLIENT_MESSAGES:
                await _terminate_collab_peer(ws, canvas_id, lock, "unknown-frame-type")
                break

            if msg_type == "presence":
                client_id = msg.get("clientId")
                violation: str | None = None
                async with lock:
                    room = _collab_rooms.get(canvas_id)
                    if room is None or ws not in room:
                        break
                    if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
                        violation = "invalid-presence"
                    elif ws in _collab_ids and _collab_ids[ws] != client_id:
                        violation = "client-id-changed"
                    else:
                        _collab_ids[ws] = client_id
                if violation is not None:
                    await _terminate_collab_peer(ws, canvas_id, lock, violation)
                    break
                payload: dict[str, object] = {"type": "presence", "clientId": client_id}
                for field_name in ("name", "color"):
                    if isinstance(msg.get(field_name), str):
                        payload[field_name] = msg[field_name]
                cursor = msg.get("cursor")
                if isinstance(cursor, dict) and all(
                    isinstance(cursor.get(axis), (int, float)) for axis in ("x", "y")
                ):
                    payload["cursor"] = {"x": cursor["x"], "y": cursor["y"]}
                await _fanout_collab(canvas_id, lock, ws, payload)
                continue

            if msg_type == "ysync":
                request_id, state_vector = msg.get("requestId"), msg.get("sv")
                if not isinstance(request_id, str) or not isinstance(state_vector, str):
                    await _terminate_collab_peer(ws, canvas_id, lock, "invalid-sync-request")
                    break
                async with lock:
                    room = _collab_rooms.get(canvas_id)
                    plan = _collab_plans.get(ws)
                    responder = (
                        plan.responder
                        if room is not None and ws in room and plan is not None
                        and plan.mode == "sync" and plan.request_id == request_id
                        else None
                    )
                if responder is None:
                    continue
                responder_role = await _current_collab_role(responder, canvas_id)
                removed_responder: tuple[WebSocket, asyncio.Task[None] | None, int] | None = None
                async with lock:
                    room = _collab_rooms.get(canvas_id)
                    current = _collab_plans.get(ws)
                    if (
                        room is None or ws not in room or current is not plan
                        or plan.responder is not responder
                    ):
                        continue
                    if responder not in room or responder_role is None:
                        if responder in room:
                            removed_responder = (
                                responder,
                                _detach_collab_peer_locked(responder, canvas_id, room),
                                1008,
                            )
                        _replan_collab_room_locked(room, canvas_id)
                    else:
                        role_changed = _collab_roles.get(responder) != responder_role
                        _collab_roles[responder] = responder_role
                        if role_changed:
                            _replan_collab_room_locked(room, canvas_id)
                        current = _collab_plans.get(ws)
                        if (
                            current is plan and responder in _collab_synced
                            and responder_role in ("owner", "editor") and not plan.request_sent
                        ):
                            if _queue_collab_locked(responder, {
                                "type": "ysync", "requestId": request_id, "sv": state_vector,
                            }):
                                plan.request_sent = True
                                _cancel_collab_plan_timer(plan)
                                _schedule_collab_sync_phase_deadline(canvas_id, ws, plan, "response")
                            else:
                                removed_responder = (
                                    responder,
                                    _detach_collab_peer_locked(responder, canvas_id, room),
                                    1013,
                                )
                                _replan_collab_room_locked(room, canvas_id)
                if removed_responder is not None:
                    await _finish_collab_removal(
                        removed_responder[0], removed_responder[1], close_code=removed_responder[2],
                    )
                continue

            if msg_type == "yjs":
                update = msg.get("update")
                if not isinstance(update, str):
                    await _terminate_collab_peer(ws, canvas_id, lock, "invalid-yjs-update")
                    break
                if msg.get("seed") is True:
                    request_id = msg.get("requestId")
                    valid_seed = False
                    async with lock:
                        room = _collab_rooms.get(canvas_id)
                        plan = _collab_plans.get(ws)
                        valid_seed = (
                            room is not None and ws in room and role in ("owner", "editor")
                            and plan is not None and plan.mode == "seed"
                            and isinstance(request_id, str) and plan.request_id == request_id
                        )
                        if valid_seed:
                            plan.seed_update_seen = True
                    if not valid_seed:
                        await _terminate_collab_peer(ws, canvas_id, lock, "invalid-seed-update")
                        break
                    continue

                if msg.get("sync") is True:
                    reply_to = msg.get("replyTo")
                    if not isinstance(reply_to, str):
                        await _terminate_collab_peer(ws, canvas_id, lock, "invalid-sync-response")
                        break
                    async with lock:
                        room = _collab_rooms.get(canvas_id)
                        if room is None or ws not in room:
                            break
                        if role not in ("owner", "editor") or ws not in _collab_synced:
                            _replan_collab_room_locked(room, canvas_id)
                            target = None
                        else:
                            target = next((
                                (joiner, candidate) for joiner, candidate in _collab_plans.items()
                                if _collab_canvas.get(joiner) == canvas_id and candidate.mode == "sync"
                                and candidate.responder is ws and candidate.request_id == reply_to
                                and candidate.request_sent and not candidate.response_forwarded
                            ), None)
                    if target is None:
                        continue

                    joiner, plan = target
                    joiner_role = await _current_collab_role(joiner, canvas_id)
                    removed_joiner: tuple[WebSocket, asyncio.Task[None] | None, int] | None = None
                    async with lock:
                        room = _collab_rooms.get(canvas_id)
                        current = _collab_plans.get(joiner)
                        if (
                            room is None or ws not in room or joiner not in room
                            or current is not plan or plan.responder is not ws
                        ):
                            continue
                        if joiner_role is None:
                            removed_joiner = (
                                joiner,
                                _detach_collab_peer_locked(joiner, canvas_id, room),
                                1008,
                            )
                            _replan_collab_room_locked(room, canvas_id)
                        else:
                            if _collab_roles.get(joiner) != joiner_role:
                                _collab_roles[joiner] = joiner_role
                                _replan_collab_room_locked(room, canvas_id)
                            current = _collab_plans.get(joiner)
                            # Append the baseline before publishing response_forwarded. Every later
                            # delta uses the same outbox, so it cannot overtake this frame.
                            if current is plan and _queue_collab_locked(joiner, {
                                "type": "yjs", "sync": True,
                                "replyTo": reply_to, "update": update,
                            }):
                                plan.response_forwarded = True
                                _cancel_collab_plan_timer(plan)
                                _schedule_collab_sync_phase_deadline(canvas_id, joiner, plan, "ready")
                            elif current is plan:
                                removed_joiner = (
                                    joiner,
                                    _detach_collab_peer_locked(joiner, canvas_id, room),
                                    1013,
                                )
                                _replan_collab_room_locked(room, canvas_id)
                    if removed_joiner is not None:
                        await _finish_collab_removal(
                            removed_joiner[0], removed_joiner[1], close_code=removed_joiner[2],
                        )
                    continue

                if role not in ("owner", "editor"):
                    # A viewer remains a read replica, but its document writes never fan out.
                    continue
                async with lock:
                    room = _collab_rooms.get(canvas_id)
                    synchronized = room is not None and ws in room and ws in _collab_synced
                if not synchronized:
                    await _terminate_collab_peer(ws, canvas_id, lock, "update-before-sync")
                    break
                await _fanout_collab(
                    canvas_id, lock, ws, {"type": "yjs", "update": update},
                    baseline_required=True,
                )
                continue

            request_id = msg.get("requestId")  # sync-ready
            if not isinstance(request_id, str):
                await _terminate_collab_peer(ws, canvas_id, lock, "invalid-sync-ready")
                break
            premature = False
            async with lock:
                room = _collab_rooms.get(canvas_id)
                if room is None or ws not in room:
                    break
                plan = _collab_plans.get(ws)
                if plan is not None and plan.request_id == request_id:
                    seed_complete = (
                        plan.mode == "seed" and plan.seed_update_seen
                        and role in ("owner", "editor")
                    )
                    sync_complete = plan.mode == "sync" and plan.response_forwarded
                    if seed_complete or sync_complete:
                        _collab_synced.add(ws)
                        completed = _collab_plans.pop(ws, None)
                        if completed is not None:
                            _cancel_collab_plan_timer(completed)
                        _collab_last_state.pop(ws, None)
                        _replan_collab_room_locked(room, canvas_id)
                    else:
                        premature = True
            if premature:
                await _terminate_collab_peer(ws, canvas_id, lock, "premature-sync-ready")
                break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            async def cleanup() -> None:
                sender_task: asyncio.Task[None] | None = None
                async with lock:
                    room = _collab_rooms.get(canvas_id)
                    if room is not None and ws in room:
                        sender_task = _detach_collab_peer_locked(ws, canvas_id, room)
                        _replan_collab_room_locked(room, canvas_id)
                if sender_task is not None and sender_task is not asyncio.current_task():
                    await asyncio.gather(sender_task, return_exceptions=True)

            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # A second cancellation must not strand state after the registry lock is released.
                await cleanup_task
                raise
        finally:
            _release_collab_room_lock(canvas_id, lock)


async def _broadcast_external_edit(canvas_id: str) -> None:
    """Nudge every browser tab in a canvas's collab room that the doc changed out-of-band (an MCP
    client edited it). The tab refetches + applies. A plain relay message the collab client
    understands; carries no clientId so a peer's own self-filter can't drop it."""
    lock = _retain_collab_room_lock(canvas_id)
    try:
        await _fanout_collab(
            canvas_id,
            lock,
            None,
            {"type": "server", "event": "external-edit", "canvasId": canvas_id},
        )
    finally:
        _release_collab_room_lock(canvas_id, lock)


@asynccontextmanager
async def _idle_collab_room_edit(canvas_id: str):
    """Hold the room admission boundary while one out-of-band document edit commits.

    Checking the room and then releasing its lock before the metadata write leaves a window where a
    new editor can join with the old document and later overwrite the committed edit.  Keeping this
    lock across the off-loop write makes the empty-room decision and mutation one local boundary.
    """
    lock = _retain_collab_room_lock(canvas_id)
    try:
        async with lock:
            yield not bool(_collab_rooms.get(canvas_id))
    finally:
        _release_collab_room_lock(canvas_id, lock)


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


@app.get("/api/livez", responses=API_ERROR_RESPONSES)
def livez() -> dict:
    from hub.observability import MetricName, emit_metric
    emit_metric(MetricName.KERNEL_HEALTH, 1.0, labels={"probe": "livez", "ready": "true"})
    return {"ok": True}     # the process is up and serving — a pure liveness signal (no dep checks)


@app.get("/api/readyz", responses=API_ERROR_RESPONSES)
def readyz():
    # readiness = can this instance actually serve? Real dep checks (not a static ok): the metadata DB
    # answers, is still at this build's exact schema head, and the DuckDB engine is responsive (not
    # wedged). 503 makes a load balancer / k8s probe pull an unsafe instance out of rotation.
    from fastapi.responses import JSONResponse

    from hub import db, metadb
    from hub.observability import MetricName, emit_metric
    db_ok = metadb.ping()
    checks = {
        "db": db_ok,
        # Do not launch a second potentially blocking DB connection after ping has already timed out.
        "schema": metadb.schema_at_head() if db_ok else False,
        "engine": db.responsive(3.0),
    }
    ready = all(checks.values())
    emit_metric(MetricName.KERNEL_HEALTH, 1.0 if ready else 0.0,
                labels={"probe": "readyz", "ready": "true" if ready else "false"})
    body = {"ready": ready, "checks": checks}
    if not ready:
        body.update(
            detail="service is not ready",
            code=APIErrorCode.SERVICE_UNAVAILABLE,
            retryable=True,
        )
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/api/version", responses=API_ERROR_RESPONSES)
def version() -> dict:
    # deployment identity for operability — package version + sha + the pluggable-backend choices +
    # core lib versions. SECRETS ARE REDACTED: the DB is reported as its dialect only (never the
    # DP_DATABASE_URL creds).
    import platform
    from importlib.metadata import PackageNotFoundError, version as pkg_version

    import duckdb
    import pyarrow

    from hub import auth
    from hub.settings import settings
    try:
        package_version = pkg_version("data-playground")
    except PackageNotFoundError:  # bare source tree without an install — release builds always install
        package_version = "unknown"
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
        "version": package_version,
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
