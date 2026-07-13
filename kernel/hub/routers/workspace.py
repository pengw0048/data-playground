"""Auth, users, canvases, and settings — the per-user metadata-DB routes.

Mixes the pre-login PUBLIC routes (auth status/login/logout + the login roster) with the authed
rest. main includes `public_router` WITHOUT the auth gate and `router` WITH it, preserving the
secure-default boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Callable, Hashable, TypeVar

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select as _sa_select

from hub import auth, auth_admission, metadb
from hub.models import RunStatus
from hub.security import RequestIdentity, current_identity, current_user

router = APIRouter()
public_router = APIRouter()
_T = TypeVar("_T")
MAX_AUTH_USER_ID_BYTES = 128
MAX_AUTH_USER_PROFILE_FIELD_BYTES = 1024


class _StrictAuthBody(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )


def _bounded_password(value: str) -> str:
    auth.password_bytes_for_kdf(value)
    return value


def _bounded_database_text(value: str, *, label: str, max_bytes: int) -> str:
    """Reject text that cannot cross both SQLite and PostgreSQL boundaries safely."""
    # Reject obviously oversized values before allocating a second encoded copy. The request-body cap
    # remains the outer allocation bound; this narrower limit keeps auth metadata and DB work small.
    if len(value) > max_bytes:
        raise ValueError(f"{label} must be at most {max_bytes} UTF-8 bytes")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} must be at most {max_bytes} UTF-8 bytes")
    return value


def _bounded_user_id(value: str) -> str:
    # UUIDs and generated project IDs are far below this bound; the exact UTF-8 check keeps multibyte
    # identifiers deterministic and rejects PostgreSQL-incompatible NUL before hashing or DB access.
    return _bounded_database_text(value, label="user id", max_bytes=MAX_AUTH_USER_ID_BYTES)


class LoginBody(_StrictAuthBody):
    user_id: str
    password: str

    _user_id_is_bounded = field_validator("user_id")(_bounded_user_id)
    _password_is_bounded = field_validator("password")(_bounded_password)


class PasswordChangeBody(_StrictAuthBody):
    old_password: str = ""
    new_password: str

    _passwords_are_bounded = field_validator("old_password", "new_password")(_bounded_password)


def _client_host(request: Request) -> str:
    # Consume only the peer identity supplied by the ASGI server; this route never parses raw forwarded
    # headers. Uvicorn may already have normalized request.client from those headers when the immediate
    # peer is in its configured trusted-proxy set, which is the correct deployment-layer boundary.
    return request.client.host if request.client is not None else ""


def _password_attempt_key(client_host: str, user_id: str) -> bytes:
    return auth_admission.password_attempt_key(client_host, user_id)


def _admit_attempt(limiter: auth_admission.AttemptLimiter, key: Hashable) -> None:
    decision = limiter.consume(key)
    if not decision.allowed:
        raise HTTPException(
            429,
            "too many password attempts",
            headers={"Retry-After": str(decision.retry_after)},
        )


async def _run_password_work(function: Callable[..., _T], *args: object) -> _T:
    # Admission happens before submission to a dedicated, fixed-size executor, so password work can
    # neither wait inside AnyIO's shared worker pool nor starve unrelated sync endpoints.
    gate = auth_admission.password_work_gate
    if not gate.try_acquire():
        raise HTTPException(429, "password service is busy", headers={"Retry-After": "1"})
    try:
        future = auth_admission.password_work_executor.submit(function, *args)
    except BaseException:
        gate.release()
        raise

    # The request task may be cancelled while a Python worker continues. Tie the lease to the actual
    # concurrent Future, not the request lifetime; a queued cancellation also completes the Future and
    # releases exactly once. Capture this request's gate so test swaps/config changes cannot mis-release.
    future.add_done_callback(lambda _done: gate.release())
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.cancel()  # succeeds only while queued; running work retains its lease until completion
        raise


def _login_password_work(user_id: str, password: str) -> int | None:
    # PER-USER: the password must match THIS user's own credential — knowing the instance/bootstrap
    # password no longer lets you sign in as someone else.
    snapshot = metadb.user_auth_snapshot(user_id) if user_id else None
    if snapshot is None:
        return None
    password_hash, epoch = snapshot
    if not auth.verify_password(password, password_hash):
        return None
    # Scrypt runs without a DB lock. Reconfirm the exact hash+epoch snapshot afterwards, then sign
    # that original epoch: a concurrent rotation either fails this check or invalidates the old token.
    if not metadb.user_auth_snapshot_matches(user_id, password_hash, epoch):
        return None
    return epoch


def _change_password_work(identity: RequestIdentity, old_password: str, new_password: str) -> int:
    """Verify, hash, and CAS while one password-work lease remains held."""
    uid = identity.user_id
    snapshot = metadb.user_auth_snapshot(uid)
    if snapshot is None:
        raise HTTPException(409, "password was changed concurrently; sign in again")
    current, snapshot_epoch = snapshot
    if current is not None and not auth.verify_password(old_password, current):
        raise HTTPException(403, "current password is incorrect")
    if len(new_password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    # Both scrypt operations stay outside the DB transaction. The conditional update then proves that
    # the exact hash and admitted token epoch are still current, replaces the hash, and bumps the epoch.
    admission_epoch = identity.session_epoch if identity.session_epoch is not None else snapshot_epoch
    epoch = metadb.compare_and_set_user_password(
        uid,
        current,
        admission_epoch,
        auth.hash_password(new_password),
    )
    if epoch is None:
        raise HTTPException(409, "password was changed concurrently; sign in again")
    return epoch


@public_router.get("/auth/status")
def auth_status(dp_session: str | None = Cookie(default=None)) -> dict:
    if not auth.auth_enabled():
        return {"authEnabled": False, "userId": metadb.DEFAULT_USER_ID}
    return {"authEnabled": True, "userId": auth.verify(dp_session)}


@public_router.post("/auth/login")
async def auth_login(body: LoginBody, response: Response, request: Request) -> dict:
    if not auth.auth_enabled():
        # This endpoint is async so authenticated password work can use explicit admission. Preserve the
        # old sync endpoint's off-event-loop DB behavior for the open-mode identity lookup.
        user_id = await run_in_threadpool(metadb.resolve_user, body.user_id)
        return {"ok": True, "userId": user_id}
    uid = body.user_id
    client_host = _client_host(request)
    peer_key = auth_admission.login_peer_attempt_key(client_host)
    # NAT/proxy tradeoff: callers behind one trusted peer share this aggregate quota and may throttle
    # each other. A larger peer burst (100/minute) limits false positives while still keeping one peer
    # below the 4096 pair-table cap throughout the ten-minute entry lifetime, so random-ID sprays remain
    # bounded. Successful login resets only the pair bucket, never this peer bucket.
    _admit_attempt(auth_admission.login_peer_attempts, peer_key)
    attempt_key = _password_attempt_key(client_host, uid)
    _admit_attempt(auth_admission.login_attempts, attempt_key)
    epoch = await _run_password_work(_login_password_work, uid, body.password)
    if epoch is None:
        raise HTTPException(401, "invalid user or password")
    # Secure flag opt-in for HTTPS deployments (default off so internal http installs still work)
    response.set_cookie("dp_session", auth.sign_at_epoch(uid, epoch), httponly=True, samesite="lax",
                        secure=bool(os.environ.get("DP_AUTH_SECURE_COOKIE")))
    auth_admission.login_attempts.reset(attempt_key)
    return {"ok": True, "userId": uid}


@router.post("/auth/password")
async def change_password(body: PasswordChangeBody, response: Response, request: Request,
                          identity: RequestIdentity = Depends(current_identity)) -> dict:
    """Set/rotate the CURRENT user's password. If one is already set, the old password must match."""
    uid = identity.user_id
    attempt_key = _password_attempt_key(_client_host(request), uid)
    _admit_attempt(auth_admission.password_change_attempts, attempt_key)
    epoch = await _run_password_work(_change_password_work, identity, body.old_password, body.new_password)
    auth_admission.password_change_attempts.reset(attempt_key)
    if auth.auth_enabled():  # re-issue THIS session's cookie at the new epoch so the caller isn't logged out
        response.set_cookie("dp_session", auth.sign_at_epoch(uid, epoch), httponly=True, samesite="lax",
                            secure=bool(os.environ.get("DP_AUTH_SECURE_COOKIE")))
    return {"ok": True}


@public_router.post("/auth/logout")
def auth_logout(response: Response) -> dict:
    response.delete_cookie("dp_session")
    return {"ok": True}


class UserBody(_StrictAuthBody):
    name: str
    email: str | None = None
    password: str | None = None  # set the new user's credential (required for login when auth is on)

    @field_validator("name", "email")
    @classmethod
    def _profile_text_is_database_safe(cls, value: str | None) -> str | None:
        if value is not None:
            _bounded_database_text(
                value,
                label="user profile field",
                max_bytes=MAX_AUTH_USER_PROFILE_FIELD_BYTES,
            )
        return value

    @field_validator("password")
    @classmethod
    def _optional_password_is_bounded(cls, value: str | None) -> str | None:
        if value is not None:
            _bounded_password(value)
        return value


class SettingBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    scope: str = "global"   # 'global' | 'user'
    key: str
    value: object = None


# public: the login screen needs the roster to populate its user picker BEFORE a session exists — so
# id + name only (no emails), and no other data.
@public_router.get("/users")
def list_users() -> list[dict]:
    with metadb.session() as s:
        return [{"id": u.id, "name": u.name} for u in s.scalars(_sa_select(metadb.User))]


def _can_manage_global(uid: str) -> bool:
    """Whether uid may write instance-wide config (global settings, user management). Open single-user
    mode has no privilege boundary; auth mode requires admin."""
    return (not auth.auth_enabled()) or metadb.is_admin(uid)


def _require_admin(uid: str) -> None:
    """Instance-wide config (global settings, user management) is admin-only in multi-user mode.
    Open single-user mode has no privilege boundary — the single local user keeps full control."""
    if not _can_manage_global(uid):
        raise HTTPException(403, "admin only")


def _create_user_work(body: UserBody, uid: str) -> dict:
    _require_admin(uid)
    if body.password is not None and len(body.password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    password_hash = auth.hash_password(body.password) if body.password is not None else None
    with metadb.session() as s:
        u = metadb.User(name=body.name, email=body.email, password_hash=password_hash)
        s.add(u)
        s.flush()
        return {"id": u.id, "name": u.name, "email": u.email}


@router.post("/users")
async def create_user(body: UserBody, uid: str = Depends(current_user)) -> dict:  # admin-only (auth mode)
    if body.password is None:
        return await run_in_threadpool(_create_user_work, body, uid)
    return await _run_password_work(_create_user_work, body, uid)


@router.get("/me")
def whoami(uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        u = s.get(metadb.User, uid)
        # capabilities let the UI hide/disable what this user can't do (e.g. global settings), instead
        # of showing controls that then fail — so the client never lies about a doomed action (UX-01).
        caps = ["global_settings"] if _can_manage_global(uid) else []
        return {"id": u.id, "name": u.name, "email": u.email, "capabilities": caps}


@router.get("/canvas")
def list_canvases(uid: str = Depends(current_user)) -> list[dict]:
    return metadb.list_canvases_for(uid)  # owned + shared + workspace-visible


@router.post("/canvas")
def create_canvas(doc: dict, uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        # honor the client's id so the canvas exists under it immediately (no orphan row, and
        # sharing/opening works without waiting for the first autosave to PUT it).
        cid = doc.get("id") or metadb._uid()
        if s.get(metadb.Canvas, cid) is None:
            s.add(metadb.Canvas(id=cid, owner_id=uid, name=doc.get("name") or "untitled",
                                version=doc.get("version", 1), doc=json.dumps(doc)))
        return {"ok": True, "id": cid}


@router.get("/canvas/{canvas_id}")
def get_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) is None:  # owner, shared, or workspace-visible
        raise HTTPException(404, f"canvas '{canvas_id}' not found")
    with metadb.session() as s:
        return json.loads(s.get(metadb.Canvas, canvas_id).doc)


@router.put("/canvas/{canvas_id}")
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


@router.get("/canvas/{canvas_id}/versions")
def get_canvas_versions(canvas_id: str, uid: str = Depends(current_user)) -> list[dict]:
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    return metadb.list_versions(canvas_id)


class RestoreRequest(BaseModel):
    version_id: str
    label: str | None = None  # optional name for the safety snapshot taken of the pre-restore state


@router.post("/canvas/{canvas_id}/restore")
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


@router.delete("/canvas/{canvas_id}")
def delete_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) == "owner":  # only the owner can delete
        metadb.delete_canvas_cascade(canvas_id)  # also drop shares + run history + versions (no FK cascade)
    return {"ok": True}


@router.get("/canvas/{canvas_id}/shares")
def get_shares(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id)
        vis = c.visibility if c else "private"
    return {"visibility": vis, "shares": metadb.list_shares(canvas_id)}


@router.post("/canvas/{canvas_id}/share")
def add_share(canvas_id: str, body: dict, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) != "owner":
        raise HTTPException(403, "only the owner can share")
    if "visibility" in body:
        if body["visibility"] not in ("private", "workspace", "workspace_view"):
            raise HTTPException(400, "invalid visibility")
        metadb.set_visibility(canvas_id, body["visibility"])
    if body.get("userId"):
        role = body.get("role", "editor")
        if role not in metadb.SHARE_ROLES:  # never let a share grant 'owner' — that's a privilege escalation
            raise HTTPException(422, f"invalid role {role!r}; must be one of {list(metadb.SHARE_ROLES)}")
        metadb.share_canvas(canvas_id, body["userId"], role)
    return {"ok": True}


@router.delete("/canvas/{canvas_id}/share/{user_id}")
def remove_share(canvas_id: str, user_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) != "owner":
        raise HTTPException(403, "only the owner can unshare")
    metadb.unshare_canvas(canvas_id, user_id)
    return {"ok": True}


@router.get("/canvas/{canvas_id}/runs")
def canvas_runs(canvas_id: str, uid: str = Depends(current_user)) -> list[dict]:
    """Run history for a canvas (persisted, survives restarts)."""
    if metadb.canvas_role(canvas_id, uid) is None:  # same authz as the other canvas endpoints
        raise HTTPException(404, "not found")
    return metadb.list_runs(canvas_id)


@router.get("/canvas/{canvas_id}/active-runs", response_model=list[RunStatus])
def canvas_active_runs(canvas_id: str, uid: str = Depends(current_user)) -> list[RunStatus]:
    """In-flight runs for a canvas, so a reopened canvas re-subscribes to a run that survived a hub
    restart on its kernel (rather than the run silently vanishing from the UI). Rebuilt into RunStatus
    so it serializes with the same camelCase wire shape as GET /run/{id}."""
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    return [RunStatus(**d) for d in metadb.active_runs(canvas_id)]


@router.get("/canvas/{canvas_id}/kernel")
def canvas_kernel(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    """The per-canvas execution kernel's state (Jupyter-style), or {exists:false} if none is running.
    Token/endpoint are internal — only state + staleness are surfaced."""
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    k = metadb.get_kernel(canvas_id)
    return {"exists": False} if k is None else {"exists": True, "state": k["state"], "stale": k["stale"]}


@router.post("/canvas/{canvas_id}/kernel/restart")
def canvas_kernel_restart(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    """Restart the canvas's kernel (Jupyter's 'Restart kernel'): shut the current one down; the next
    run/preview spawns a fresh one. A wedged transform or a stale warm state is cleared this way."""
    role = metadb.canvas_role(canvas_id, uid)
    if role is None:
        raise HTTPException(404, "not found")
    if role not in ("owner", "editor"):  # a viewer must not be able to kill a shared canvas's kernel
        raise HTTPException(403, "restart requires edit access")
    k = metadb.get_kernel(canvas_id)
    if not k or not k.get("endpoint"):
        return {"ok": True, "restarted": False}  # none live → next run spawns fresh anyway
    from hub import kernel_backend
    from hub.deps import get_deps
    try:
        kernel_backend._post(k["endpoint"], "/shutdown", k["token"], {}, timeout=5.0, connect_retries=0)  # graceful; dead → fast-fail to kill
    except Exception:  # noqa: BLE001 — unreachable = already dead; the lease reaps, next run respawns
        pass
    kb = get_deps().kernel_backend()  # force-remove the substrate too (deletes the pod; no-op for local)
    if kb is not None:
        kb.kill(canvas_id, k["kernel_id"])
    # authoritative: clear the lease ourselves even if the kernel was unreachable and couldn't drop it —
    # else the canvas stays bound to a dead endpoint until the reaper fires. Fenced by kernel_id, so it
    # never deletes a newer kernel that already took over the canvas.
    metadb.drop_kernel(canvas_id, k["kernel_id"])
    return {"ok": True, "restarted": True}


# Secrets never leave the kernel in plaintext. GET redacts them to a sentinel (fields are password
# inputs, so it just shows dots); PUT treats the sentinel as "unchanged" and preserves the stored value.
_REDACTED = "__redacted__"
_SECRET_SUBKEYS = ("accessKeyId", "secretAccessKey")  # within the objectStore setting


def _plugin_secret_keys() -> set[str]:
    """Setting keys `plugin.<pack>.<field>` whose declared [[config]] field is `secret` — so GET redacts
    them and PUT treats the redaction sentinel as 'unchanged', exactly like agentApiKey. A plugin's secret
    (an API token / DB password) must not be readable by a non-admin via GET /settings the way /api/plugins
    already avoids. Sourced from the loaded plugins' schemas; never crashes settings."""
    out: set[str] = set()
    try:
        from hub.deps import get_deps
        for p in get_deps().plugins:
            for f in (p.get("config") or []):
                if isinstance(f, dict) and f.get("secret") and f.get("key"):
                    out.add(f"plugin.{p['name']}.{f['key']}")
    except Exception:  # noqa: BLE001
        pass
    return out


def _redact_global(key: str, value, secret_keys: set[str] = frozenset()):
    if key == "agentApiKey" or key in secret_keys:
        return _REDACTED if value else value
    if key == "objectStore" and isinstance(value, dict):
        return {k: (_REDACTED if k in _SECRET_SUBKEYS and v else v) for k, v in value.items()}
    return value


@router.get("/settings")
def get_settings(uid: str = Depends(current_user)) -> dict:
    secret_keys = _plugin_secret_keys()
    with metadb.session() as s:
        rows = s.scalars(_sa_select(metadb.Setting))
        out: dict = {"global": {}, "user": {}}
        for r in rows:
            if r.scope == "global":
                out["global"][r.key] = _redact_global(r.key, json.loads(r.value), secret_keys)
            elif r.scope == "user" and r.scope_id == uid:
                out["user"][r.key] = json.loads(r.value)
        return out


@router.put("/settings")
def put_setting(body: SettingBody, uid: str = Depends(current_user)) -> dict:
    if body.scope == "global":
        _require_admin(uid)  # instance-wide settings (object-store creds, agent key, destinations) — admin only
    scope_id = uid if body.scope == "user" else ""
    value = body.value
    if body.scope == "global":  # a redaction sentinel means "keep what's stored" — never overwrite a secret with dots
        stored = metadb.get_setting(body.key, "global", default=None)
        if value == _REDACTED and (body.key == "agentApiKey" or body.key in _plugin_secret_keys()):
            value = stored  # never overwrite a secret with the dots sentinel echoed by GET
        elif body.key == "objectStore" and isinstance(value, dict) and isinstance(stored, dict):
            value = {**value, **{k: stored.get(k) for k in _SECRET_SUBKEYS if value.get(k) == _REDACTED}}
    metadb.set_setting(body.key, value, scope=body.scope, scope_id=scope_id)
    return {"ok": True}
