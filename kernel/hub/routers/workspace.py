"""Auth, users, canvases, and settings — the per-user metadata-DB routes.

Mixes the pre-login PUBLIC routes (auth status/login/logout + the login roster) with the authed
rest. main includes `public_router` WITHOUT the auth gate and `router` WITH it, preserving the
secure-default boundary.
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select as _sa_select

from hub import auth, metadb
from hub.models import RunStatus
from hub.security import current_user

router = APIRouter()
public_router = APIRouter()

@public_router.get("/auth/status")
def auth_status(dp_session: str | None = Cookie(default=None)) -> dict:
    if not auth.auth_enabled():
        return {"authEnabled": False, "userId": metadb.DEFAULT_USER_ID}
    return {"authEnabled": True, "userId": auth.verify(dp_session)}


@public_router.post("/auth/login")
def auth_login(body: dict, response: Response) -> dict:
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


@router.post("/auth/password")
def change_password(body: dict, uid: str = Depends(current_user)) -> dict:
    """Set/rotate the CURRENT user's password. If one is already set, the old password must match."""
    current = metadb.user_password_hash(uid)
    if current and not auth.verify_password(body.get("oldPassword", ""), current):
        raise HTTPException(403, "current password is incorrect")
    new = body.get("newPassword") or ""
    if len(new) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    metadb.set_user_password(uid, auth.hash_password(new))
    return {"ok": True}


@public_router.post("/auth/logout")
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


# public: the login screen needs the roster to populate its user picker BEFORE a session exists — so
# id + name only (no emails), and no other data.
@public_router.get("/users")
def list_users() -> list[dict]:
    with metadb.session() as s:
        return [{"id": u.id, "name": u.name} for u in s.scalars(_sa_select(metadb.User))]


def _require_admin(uid: str) -> None:
    """Instance-wide config (global settings, user management) is admin-only in multi-user mode.
    Open single-user mode has no privilege boundary — the single local user keeps full control."""
    if auth.auth_enabled() and not metadb.is_admin(uid):
        raise HTTPException(403, "admin only")


@router.post("/users")
def create_user(body: UserBody, uid: str = Depends(current_user)) -> dict:  # admin-only (auth mode)
    _require_admin(uid)
    with metadb.session() as s:
        u = metadb.User(name=body.name, email=body.email,
                        password_hash=auth.hash_password(body.password) if body.password else None)
        s.add(u)
        s.flush()
        return {"id": u.id, "name": u.name, "email": u.email}


@router.get("/me")
def whoami(uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        u = s.get(metadb.User, uid)
        return {"id": u.id, "name": u.name, "email": u.email}


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
        metadb.share_canvas(canvas_id, body["userId"], body.get("role", "editor"))
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
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    k = metadb.get_kernel(canvas_id)
    if not k or not k.get("endpoint"):
        return {"ok": True, "restarted": False}  # none live → next run spawns fresh anyway
    from hub import kernel_backend
    try:
        kernel_backend._post(k["endpoint"], "/shutdown", k["token"], {}, timeout=5.0)
    except Exception:  # noqa: BLE001 — unreachable = already dead; the lease reaps, next run respawns
        pass
    return {"ok": True, "restarted": True}


# Secrets never leave the kernel in plaintext. GET redacts them to a sentinel (fields are password
# inputs, so it just shows dots); PUT treats the sentinel as "unchanged" and preserves the stored value.
_REDACTED = "__redacted__"
_SECRET_SUBKEYS = ("accessKeyId", "secretAccessKey")  # within the objectStore setting


def _redact_global(key: str, value):
    if key == "agentApiKey":
        return _REDACTED if value else value
    if key == "objectStore" and isinstance(value, dict):
        return {k: (_REDACTED if k in _SECRET_SUBKEYS and v else v) for k, v in value.items()}
    return value


@router.get("/settings")
def get_settings(uid: str = Depends(current_user)) -> dict:
    with metadb.session() as s:
        rows = s.scalars(_sa_select(metadb.Setting))
        out: dict = {"global": {}, "user": {}}
        for r in rows:
            if r.scope == "global":
                out["global"][r.key] = _redact_global(r.key, json.loads(r.value))
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
        if body.key == "agentApiKey" and value == _REDACTED:
            value = stored
        elif body.key == "objectStore" and isinstance(value, dict) and isinstance(stored, dict):
            value = {**value, **{k: stored.get(k) for k in _SECRET_SUBKEYS if value.get(k) == _REDACTED}}
    metadb.set_setting(body.key, value, scope=body.scope, scope_id=scope_id)
    return {"ok": True}

