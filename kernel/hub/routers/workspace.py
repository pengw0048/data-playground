"""Auth, users, canvases, and settings — the per-user metadata-DB routes.

Mixes the pre-login PUBLIC routes (auth status/login/logout + the login roster) with the authed
rest. main includes `public_router` WITHOUT the auth gate and `router` WITH it, preserving the
secure-default boundary.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any, Callable, Hashable, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select as _sa_select

from hub import auth, auth_admission, canvas_copy, metadb, native_canvas, workspace_providers
from hub.api_errors import APIError, APIErrorCode, APIErrorResponse
from hub.deps import get_deps
from hub.models import (
    CanvasTransformReference,
    CredUpsert,
    DurableTaskInboxItemView,
    DurableTaskInboxPage,
    DurableTaskInboxUnreadCount,
    ExecutionManifestDetail,
    Graph,
    RunHistoryRecord,
    RunStatus,
    WorkspaceRunPage,
    WorkspaceBrowsePage,
    WorkspaceResourceResolution,
    WorkspaceProviderRelinkRequest,
    WorkspaceProviderRelinkResult,
    WorkspaceSearchPage,
)
from hub.security import RequestIdentity, current_identity, current_user

router = APIRouter()
public_router = APIRouter()
_T = TypeVar("_T")
_NativeBodyT = TypeVar("_NativeBodyT", bound="NativeCanvasValidateBody")
MAX_AUTH_USER_ID_BYTES = 128
MAX_AUTH_USER_PROFILE_FIELD_BYTES = 1024
MAX_SETTING_KEY_BYTES = 512
MAX_SETTING_BATCH_CHANGES = 128
MAX_SETTING_BATCH_VALUE_BYTES = 1024 * 1024
SETTING_REVISION_UPPER_BOUND = 2**63


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
    # Consume only the peer identity supplied by the ASGI stack; this route never parses raw forwarded
    # headers. TrustedProxyHeadersMiddleware (and uvicorn when DP_TRUSTED_PROXIES is set) may already
    # have normalized request.client from X-Forwarded-For when the immediate peer is declared trusted.
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
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    if not auth.auth_enabled():
        # This endpoint is async so authenticated password work can use explicit admission. Preserve the
        # old sync endpoint's off-event-loop DB behavior for the open-mode identity lookup.
        user_id = await run_in_threadpool(metadb.resolve_user, body.user_id)
        emit_audit(AuditAction.AUTH_LOGIN, AuditOutcome.SUCCESS, principal_id=user_id,
                   resource_type="user", resource_id=user_id, attrs={"mode": "open"})
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
        emit_audit(AuditAction.AUTH_LOGIN, AuditOutcome.FAILURE, principal_id=uid,
                   resource_type="user", resource_id=uid, attrs={"mode": "auth"})
        raise HTTPException(401, "invalid user or password")
    # Secure flag opt-in for HTTPS deployments (default off so localhost http installs still work).
    # Shared mode refuses startup unless DP_AUTH_SECURE_COOKIE is set (see auth.reject_unsafe_transport).
    response.set_cookie("dp_session", auth.sign_at_epoch(uid, epoch), httponly=True, samesite="lax",
                        secure=auth.secure_cookie_enabled())
    auth_admission.login_attempts.reset(attempt_key)
    emit_audit(AuditAction.AUTH_LOGIN, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="user", resource_id=uid, attrs={"mode": "auth"})
    return {"ok": True, "userId": uid}


@router.post("/auth/password")
async def change_password(body: PasswordChangeBody, response: Response, request: Request,
                          identity: RequestIdentity = Depends(current_identity)) -> dict:
    """Set/rotate the CURRENT user's password. If one is already set, the old password must match."""
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    uid = identity.user_id
    attempt_key = _password_attempt_key(_client_host(request), uid)
    _admit_attempt(auth_admission.password_change_attempts, attempt_key)
    epoch = await _run_password_work(_change_password_work, identity, body.old_password, body.new_password)
    auth_admission.password_change_attempts.reset(attempt_key)
    if auth.auth_enabled():  # re-issue THIS session's cookie at the new epoch so the caller isn't logged out
        response.set_cookie("dp_session", auth.sign_at_epoch(uid, epoch), httponly=True, samesite="lax",
                            secure=auth.secure_cookie_enabled())
    emit_audit(AuditAction.AUTH_PASSWORD_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="user", resource_id=uid)
    return {"ok": True}


@public_router.post("/auth/logout")
def auth_logout(response: Response, dp_session: str | None = Cookie(default=None)) -> dict:
    # Sessions are stateless apart from the per-user epoch, so logout revokes every session issued
    # at the current epoch. Keep this route public so an expired/invalid cookie can still be cleared.
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    uid = auth.verify(dp_session) if auth.auth_enabled() else None
    if uid is not None:
        metadb.bump_token_epoch(uid)
        emit_audit(AuditAction.AUTH_LOGOUT, AuditOutcome.SUCCESS, principal_id=uid,
                   resource_type="user", resource_id=uid)
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


class SettingBody(_StrictAuthBody):
    scope: Literal["global", "user"] = "global"
    key: str
    value: object = None

    @field_validator("key")
    @classmethod
    def _setting_key_is_bounded(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("setting key must not be blank")
        return _bounded_database_text(
            value, label="setting key", max_bytes=MAX_SETTING_KEY_BYTES)


class SettingsRevision(_StrictAuthBody):
    global_: int = Field(alias="global", ge=0, lt=SETTING_REVISION_UPPER_BOUND)
    user: int = Field(ge=0, lt=SETTING_REVISION_UPPER_BOUND)

    def as_dict(self) -> dict[str, int]:
        return {"global": self.global_, "user": self.user}


class SettingsSnapshot(_StrictAuthBody):
    global_: dict[str, object] = Field(alias="global")
    user: dict[str, object]
    revision: SettingsRevision


class SettingsBatchBody(_StrictAuthBody):
    expected_revision: SettingsRevision
    changes: list[SettingBody] = Field(max_length=MAX_SETTING_BATCH_CHANGES)

    @model_validator(mode="after")
    def _scope_keys_are_unique(self) -> "SettingsBatchBody":
        keys = [(change.scope, change.key) for change in self.changes]
        if len(keys) != len(set(keys)):
            raise ValueError("settings batch contains a duplicate scope and key")
        return self


class SettingsBatchResult(_StrictAuthBody):
    ok: bool = True
    revision: SettingsRevision


class SettingsBatchConflict(APIErrorResponse):
    revision: SettingsRevision


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
        s.add(metadb.SettingRevision(scope="user", scope_id=u.id, revision=0))
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


class WorkspaceCreateCanvasBody(_StrictAuthBody):
    container_id: str
    expected_container_version: int = Field(ge=1)
    name: str = "untitled"
    dataset_ids: list[str] = Field(default_factory=list, max_length=50)
    provider_dataset_refs: list[str] = Field(default_factory=list, max_length=1)
    transform_id: str | None = Field(default=None, min_length=1, max_length=256)
    transform_version: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _bounded_dataset_selection(self) -> "WorkspaceCreateCanvasBody":
        if len(self.dataset_ids) + len(self.provider_dataset_refs) > 50:
            raise ValueError("dataset selection is limited to 50 sources")
        if (self.transform_id is None) != (self.transform_version is None):
            raise ValueError("Transform id and version must be supplied together")
        if self.transform_id is not None and (self.dataset_ids or self.provider_dataset_refs):
            raise ValueError("a new Canvas may start with datasets or one Transform, not both")
        return self


class WorkspaceAddDatasetBody(_StrictAuthBody):
    dataset_ids: list[str] = Field(default_factory=list, max_length=50)
    provider_dataset_refs: list[str] = Field(default_factory=list, max_length=1)
    expected_canvas_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _bounded_dataset_selection(self) -> "WorkspaceAddDatasetBody":
        count = len(self.dataset_ids) + len(self.provider_dataset_refs)
        if count < 1 or count > 50:
            raise ValueError("dataset selection must contain between 1 and 50 sources")
        return self


class WorkspaceAddTransformBody(_StrictAuthBody):
    transform_id: str = Field(min_length=1, max_length=256)
    transform_version: str = Field(min_length=1, max_length=64)
    expected_canvas_version: int = Field(ge=1)
    replace_node_id: str | None = Field(default=None, min_length=1, max_length=256)


class WorkspaceMoveCanvasBody(_StrictAuthBody):
    container_id: str
    expected_container_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class NativeCanvasValidateBody(_StrictAuthBody):
    filename: str = Field(min_length=1, max_length=256)
    import_id: UUID = Field(alias="importId", strict=False)
    envelope: dict[str, Any]


class NativeCanvasImportBody(NativeCanvasValidateBody):
    validation_digest: str = Field(
        alias="validationDigest", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    confirm_warnings: bool = Field(alias="confirmWarnings")


class NativeCanvasDescriptorSnapshot(_StrictAuthBody):
    core: dict[str, Any]
    nodes: list[dict[str, Any]]
    plugins: list[dict[str, Any]]


class NativeCanvasDataReference(_StrictAuthBody):
    node_id: str = Field(alias="nodeId")
    intent: dict[str, Any]


class NativeCanvasLibraryProcessor(_StrictAuthBody):
    node_id: str = Field(alias="nodeId")
    processor: str
    version: str
    descriptor: dict[str, Any]


class NativeCanvasEnvelopeResponse(_StrictAuthBody):
    format: Literal["dataplay.native-canvas"]
    version: Literal[1]
    canvas: dict[str, Any]
    descriptors: NativeCanvasDescriptorSnapshot
    data_references: list[NativeCanvasDataReference] = Field(alias="dataReferences")
    library_processors: list[NativeCanvasLibraryProcessor] = Field(alias="libraryProcessors")


class NativeCanvasValidateRequestSchema(_StrictAuthBody):
    filename: str = Field(min_length=1, max_length=256)
    import_id: UUID = Field(alias="importId", strict=False)
    envelope: NativeCanvasEnvelopeResponse


class NativeCanvasImportRequestSchema(NativeCanvasValidateRequestSchema):
    validation_digest: str = Field(
        alias="validationDigest", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    confirm_warnings: bool = Field(alias="confirmWarnings")


class NativeCanvasDiagnosticResponse(_StrictAuthBody):
    code: str
    severity: Literal["error", "warning"]
    message: str
    path: str | None = None


class NativeCanvasValidationResponse(_StrictAuthBody):
    name: str
    node_count: int = Field(alias="nodeCount", ge=0)
    edge_count: int = Field(alias="edgeCount", ge=0)
    requirements: list[str]
    parameters: list[dict[str, Any]]
    diagnostics: list[NativeCanvasDiagnosticResponse]
    can_import: bool = Field(alias="canImport")
    requires_confirmation: bool = Field(alias="requiresConfirmation")
    validation_digest: str = Field(
        alias="validationDigest", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class NativeCanvasImportResponse(_StrictAuthBody):
    ok: Literal[True]
    id: str
    created: bool
    replayed: bool


class CanvasCopyValidateBody(_StrictAuthBody):
    copy_id: UUID = Field(alias="copyId", strict=False)
    source_canvas_id: str = Field(alias="sourceCanvasId", min_length=1, max_length=512)
    source_canvas_version: int | None = Field(
        default=None, alias="sourceCanvasVersion", ge=1)
    source_subject_id: str | None = Field(
        default=None, alias="sourceSubjectId", min_length=1, max_length=1024)
    container_id: str = Field(alias="containerId", min_length=1, max_length=512)
    expected_container_version: int = Field(alias="expectedContainerVersion", ge=1)
    name: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _one_copy_source(self) -> "CanvasCopyValidateBody":
        if (self.source_canvas_version is None) == (self.source_subject_id is None):
            raise ValueError("provide exactly one source Canvas version or retained manifest subject")
        return self


class CanvasCopyCreateBody(CanvasCopyValidateBody):
    copy_intent_digest: str = Field(
        alias="copyIntentDigest", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    validation_digest: str = Field(
        alias="validationDigest", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    confirm_warnings: bool = Field(alias="confirmWarnings")


class CanvasCopyValidationResponse(NativeCanvasValidationResponse):
    copy_intent_digest: str = Field(
        alias="copyIntentDigest", min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def _native_request_openapi(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        },
    }


def _native_import_body(
        raw: bytes, model: type[_NativeBodyT]) -> _NativeBodyT:
    # JSON is decoded only after this cap.  The envelope cap below is separate so wrapper fields
    # cannot use the import endpoint as an oversized generic JSON ingress.
    if len(raw) > native_canvas.MAX_REQUEST_BYTES:
        raise APIError(413, "native Canvas file exceeds the 2 MiB limit",
                       code=APIErrorCode.PAYLOAD_TOO_LARGE, retryable=False)
    try:
        value = json.loads(raw)
        body = model.model_validate(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise APIError(422, "native Canvas request is invalid",
                       code=APIErrorCode.VALIDATION_ERROR, retryable=False) from exc
    if native_canvas.canonical_size(body.envelope) > native_canvas.MAX_BYTES:
        raise APIError(413, "native Canvas file exceeds the 2 MiB limit",
                       code=APIErrorCode.PAYLOAD_TOO_LARGE, retryable=False)
    return body


def _native_parse(
        body: NativeCanvasValidateBody, uid: str,
) -> tuple[dict, list[native_canvas.Diagnostic]]:
    try:
        parsed = native_canvas.parse_envelope(body.envelope, filename=body.filename)
    except native_canvas.NativeCanvasError as exc:
        raise APIError(422, str(exc), code=APIErrorCode.VALIDATION_ERROR, retryable=False) from exc
    deps = get_deps()
    return parsed, native_canvas.diagnostics(parsed, deps, uid)


def _workspace_action_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, metadb.WorkspaceVersionConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, metadb.WorkspaceTransformCompatibilityConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(422, str(exc)) from exc
    raise exc


def _prepare_canvas_copy(body: CanvasCopyValidateBody, uid: str) -> tuple[
        dict, list[native_canvas.Diagnostic], str, str | None]:
    name = metadb._workspace_name(body.name)
    with metadb.session() as s:
        metadb._workspace_container_at_version(
            s, body.container_id, body.expected_container_version)
        source = s.get(metadb.Canvas, body.source_canvas_id)
        if source is None or metadb._workspace_canvas_role_in_session(s, source, uid) is None:
            raise KeyError(f"canvas '{body.source_canvas_id}' not found")
        if body.source_canvas_version is not None:
            if source.version != body.source_canvas_version:
                raise metadb.WorkspaceVersionConflict(
                    f"canvas '{body.source_canvas_id}' changed from expected version {body.source_canvas_version}")
            document = json.loads(source.doc)
            canvas, stripped = canvas_copy.prepare_current(document, name)
            lineage = {
                "kind": "canvas", "canvasId": source.id, "canvasVersion": source.version,
            }
            descriptors, manifest_sha256 = None, None
            retained_parameters = False
        else:
            detail = metadb.execution_manifest_detail_for_subject(
                uid, body.source_canvas_id, str(body.source_subject_id))
            if detail is None or detail["availability"] != "available" or detail["document"] is None:
                raise metadb.WorkspaceVersionConflict(
                    "retained execution manifest is unavailable; live Canvas state was not substituted")
            canvas, stripped = canvas_copy.prepare_manifest(detail["document"], name)
            manifest_sha256 = str(detail["sha256"])
            lineage = {
                "kind": "executionManifest", "canvasId": source.id,
                "subjectId": body.source_subject_id, "sha256": manifest_sha256,
            }
            descriptors = detail["document"].get("descriptors")
            retained_parameters = bool(detail["document"].get("parameters"))
    canvas["_copiedFrom"] = lineage
    if manifest_sha256 is None:
        metadb.require_promoted_transform_use(
            uid, canvas, canvas_id=body.source_canvas_id)
    else:
        metadb.require_retained_execution_manifest_transform_use(
            uid, body.source_canvas_id, str(body.source_subject_id), manifest_sha256, canvas)
    items = canvas_copy.diagnostics(
        canvas, get_deps(), uid, descriptors=descriptors,
        stripped_credentials=stripped, retained_parameters=retained_parameters)
    source_intent = {
        "canvasId": body.source_canvas_id,
        "canvasVersion": body.source_canvas_version,
        "subjectId": body.source_subject_id,
        "manifestSha256": manifest_sha256,
    }
    destination = {
        "containerId": body.container_id,
        "containerVersion": body.expected_container_version,
    }
    return canvas, items, canvas_copy.intent_digest(source_intent, destination, canvas), manifest_sha256


def _provider_dataset_sources(refs: list[str], uid: str) -> list[dict]:
    if len(refs) != len(set(refs)):
        raise ValueError("provider dataset selection contains a duplicate identity")
    from hub.deps import get_deps
    deps = get_deps()
    return [workspace_providers.provider_dataset_source(
        ref, uid=uid, resolve_physical=deps.resolve_physical_adapter)
        for ref in refs]


def _provider_dataset_action_error(exc: Exception) -> None:
    if isinstance(exc, workspace_providers.ProviderDatasetGone):
        raise APIError(
            410, "provider dataset was deleted; relink it explicitly",
            code=APIErrorCode.RESOURCE_GONE, retryable=False,
        ) from exc
    if isinstance(exc, workspace_providers.ProviderDatasetOffline):
        raise APIError(
            503, "provider dataset is offline",
            code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True,
        ) from exc
    if isinstance(exc, workspace_providers.ProviderDatasetUnavailable):
        raise APIError(
            409, ("provider dataset binding is unavailable; install or restore a compatible "
                  "provider and dataset adapter"),
            code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED, retryable=False,
        ) from exc
    _workspace_action_error(exc)


def _exact_transform_descriptor(processor_id: str, version: str) -> dict:
    try:
        return get_deps().registry.get(processor_id, version).descriptor().model_dump(mode="python")
    except KeyError as exc:
        raise HTTPException(
            409, f"Transform {processor_id}@{version} is unavailable") from exc


@router.get("/workspace/containers/{container_id}", response_model=WorkspaceBrowsePage)
def browse_workspace_container(container_id: str, limit: int = 50, cursor: str | None = None,
                               uid: str = Depends(current_user)) -> dict:
    """One bounded mixed Workspace page across local and configured read-only provider sources."""
    try:
        return workspace_providers.browse(
            container_id, uid=uid, limit=limit, cursor=cursor)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/workspace/search", response_model=WorkspaceSearchPage)
def search_workspace(q: str, limit: int = 25, cursor: str | None = None,
                     uid: str = Depends(current_user)) -> dict:
    """One bounded lexical page grouped by local and mounted provider source."""
    try:
        return workspace_providers.search(q, uid=uid, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/workspace/resources/{resource_id}", response_model=WorkspaceResourceResolution)
def resolve_workspace_resource(resource_id: str, uid: str = Depends(current_user)) -> dict:
    """Resolve a stable local/provider reference plus its bounded navigation ancestors."""
    try:
        return workspace_providers.resolve(resource_id, uid=uid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/workspace/resources/{resource_id}/relink",
    response_model=WorkspaceProviderRelinkResult,
)
def relink_workspace_resource(
    resource_id: str,
    body: WorkspaceProviderRelinkRequest,
    uid: str = Depends(current_user),
) -> dict:
    """Mint a new external binding from one explicit mount/resource selection."""
    from hub.observability import AuditAction, AuditOutcome, emit_audit

    try:
        result = workspace_providers.relink(
            resource_id, uid=uid, mount_id=body.mount_id,
            resource_id=body.resource_id,
        )
    except KeyError as exc:
        emit_audit(
            AuditAction.WORKSPACE_RELINK, AuditOutcome.FAILURE,
            principal_id=uid, resource_type="workspace_provider_binding",
        )
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        emit_audit(
            AuditAction.WORKSPACE_RELINK, AuditOutcome.DENIED,
            principal_id=uid, resource_type="workspace_provider_binding",
        )
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        emit_audit(
            AuditAction.WORKSPACE_RELINK, AuditOutcome.FAILURE,
            principal_id=uid, resource_type="workspace_provider_binding",
        )
        raise HTTPException(422, str(exc)) from exc
    except workspace_providers.ProviderRelinkUnavailable as exc:
        emit_audit(
            AuditAction.WORKSPACE_RELINK, AuditOutcome.FAILURE,
            principal_id=uid, resource_type="workspace_provider_binding",
        )
        raise HTTPException(503, str(exc)) from exc
    emit_audit(
        AuditAction.WORKSPACE_RELINK, AuditOutcome.SUCCESS,
        principal_id=uid, resource_type="workspace_provider_binding",
        resource_id=result["resource"]["bindingId"],
        attrs={"operation": "explicit_relink"},
    )
    return result


@router.post("/workspace/canvases")
def create_workspace_canvas(body: WorkspaceCreateCanvasBody,
                            uid: str = Depends(current_user)) -> dict:
    """Create at one exact local destination; an optional dataset is resolved by stable identity."""
    try:
        provider_sources = _provider_dataset_sources(body.provider_dataset_refs, uid)
        transform = (_exact_transform_descriptor(body.transform_id, body.transform_version)
                     if body.transform_id is not None and body.transform_version is not None else None)
        return metadb.workspace_create_canvas_action(
            uid=uid, container_id=body.container_id,
            expected_container_version=body.expected_container_version,
            name=body.name, dataset_ids=body.dataset_ids,
            provider_sources=provider_sources, transform=transform)
    except HTTPException:
        raise
    except metadb.WorkspaceTransformUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except (KeyError, PermissionError, metadb.WorkspaceVersionConflict, ValueError,
            workspace_providers.ProviderDatasetUnavailable) as exc:
        _provider_dataset_action_error(exc)


@router.post("/workspace/canvases/{canvas_id}/datasets")
async def add_workspace_dataset_to_canvas(canvas_id: str, body: WorkspaceAddDatasetBody,
                                          uid: str = Depends(current_user)) -> dict:
    """Add one exact local dataset to one explicitly named editable canvas."""
    from hub.main import _broadcast_external_edit, _idle_collab_room_edit
    try:
        provider_sources = await run_in_threadpool(
            _provider_dataset_sources, body.provider_dataset_refs, uid)
    except (KeyError, PermissionError, ValueError,
            workspace_providers.ProviderDatasetUnavailable) as exc:
        _provider_dataset_action_error(exc)
    async with _idle_collab_room_edit(canvas_id) as idle:
        if not idle:
            raise HTTPException(
                409,
                "target canvas is currently open; close active editors and retry so their unsaved work is not replaced",
            )
        try:
            result = await run_in_threadpool(
                metadb.workspace_add_datasets_action,
                uid=uid, canvas_id=canvas_id,
                expected_canvas_version=body.expected_canvas_version,
                dataset_ids=body.dataset_ids, provider_sources=provider_sources)
        except (KeyError, PermissionError, metadb.WorkspaceVersionConflict, ValueError) as exc:
            _workspace_action_error(exc)
    # This is an out-of-band document edit, like MCP. Nudge any currently open collab room to refetch
    # the committed snapshot so a stale tab cannot later autosave over the appended source.
    await _broadcast_external_edit(canvas_id)
    return result


@router.post("/workspace/canvases/{canvas_id}/transforms")
async def add_workspace_transform_to_canvas(
        canvas_id: str, body: WorkspaceAddTransformBody,
        uid: str = Depends(current_user)) -> dict:
    """Atomically add or explicitly upgrade one exact Transform in one selected editable Canvas."""
    from hub.main import _broadcast_external_edit, _idle_collab_room_edit

    transform = _exact_transform_descriptor(body.transform_id, body.transform_version)
    async with _idle_collab_room_edit(canvas_id) as idle:
        if not idle:
            raise HTTPException(
                409,
                "target canvas is currently open; close active editors and retry so their unsaved work is not replaced",
            )
        try:
            result = await run_in_threadpool(
                metadb.workspace_add_transform_action,
                uid=uid, canvas_id=canvas_id,
                expected_canvas_version=body.expected_canvas_version,
                transform=transform, replace_node_id=body.replace_node_id)
        except metadb.WorkspaceTransformUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, PermissionError, metadb.WorkspaceVersionConflict,
                metadb.WorkspaceTransformCompatibilityConflict, ValueError) as exc:
            _workspace_action_error(exc)
    await _broadcast_external_edit(canvas_id)
    return result


@router.get(
    "/canvas/{canvas_id}/transform-references",
    response_model=list[CanvasTransformReference],
)
def list_canvas_transform_references(
        canvas_id: str, uid: str = Depends(current_user)) -> list[dict]:
    """Return safe exact metadata only for library refs already present in a readable Canvas."""
    try:
        items = metadb.canvas_transform_references(uid, canvas_id)
    except (KeyError, PermissionError, ValueError) as exc:
        _workspace_action_error(exc)
    for item in items:
        if item.get("descriptor") is not None:
            descriptor = item["descriptor"]
            descriptor["provenance"] = "promoted"
            continue
        if str(item["id"]).startswith("tr_"):
            continue
        try:
            item["descriptor"] = get_deps().registry.get(
                str(item["id"]), str(item["version"])).descriptor().model_dump(mode="python")
            item["availability"] = "active"
        except KeyError:
            item["availability"] = "missing"
    return items


@router.put("/workspace/placements/{placement_id}/canvas")
def move_workspace_canvas(placement_id: str, body: WorkspaceMoveCanvasBody,
                          uid: str = Depends(current_user)) -> dict:
    """Move only local canvas placement with placement and destination CAS preconditions."""
    try:
        return metadb.workspace_move_canvas_action(
            uid=uid, placement_id=placement_id, expected_version=body.expected_version,
            container_id=body.container_id,
            expected_container_version=body.expected_container_version)
    except (KeyError, PermissionError, metadb.WorkspaceVersionConflict, ValueError) as exc:
        _workspace_action_error(exc)


@router.get("/canvas")
def list_canvases(uid: str = Depends(current_user)) -> list[dict]:
    return metadb.list_canvases_for(uid)  # owned + shared + workspace-visible


@router.get(
    "/canvas/{canvas_id}/native-export",
    response_model=NativeCanvasEnvelopeResponse,
)
def export_native_canvas(canvas_id: str, uid: str = Depends(current_user)) -> JSONResponse:
    """Download a readable, bounded native Canvas document.  Viewers may export what they can read."""
    if metadb.canvas_role(canvas_id, uid) is None:
        raise APIError(404, f"canvas '{canvas_id}' not found", code=APIErrorCode.CANVAS_NOT_FOUND,
                       retryable=False)
    with metadb.session() as s:
        row = s.get(metadb.Canvas, canvas_id)
        if row is None:  # role was revoked/deleted after the first check
            raise APIError(404, f"canvas '{canvas_id}' not found", code=APIErrorCode.CANVAS_NOT_FOUND,
                           retryable=False)
        try:
            envelope = native_canvas.export_envelope(json.loads(row.doc), get_deps())
        except native_canvas.NativeCanvasError as exc:
            raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    filename = native_canvas.filename_for(str(envelope["canvas"]["name"]))
    return JSONResponse(
        content=envelope,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/canvas/native-import/validate",
    response_model=NativeCanvasValidationResponse,
    openapi_extra=_native_request_openapi(NativeCanvasValidateRequestSchema),
)
async def validate_native_canvas_import(request: Request,
                                        uid: str = Depends(current_user)) -> dict:
    """Parse and diagnose a selected native file without creating a Canvas."""
    body = _native_import_body(await request.body(), NativeCanvasValidateBody)
    parsed, diagnostics = _native_parse(body, uid)
    return native_canvas.summary(parsed, diagnostics)


@router.post(
    "/canvas/native-import",
    response_model=NativeCanvasImportResponse,
    openapi_extra=_native_request_openapi(NativeCanvasImportRequestSchema),
)
async def import_native_canvas(request: Request, uid: str = Depends(current_user)) -> dict:
    """Atomically create one new owner Canvas, with a deterministic replay identity per import UUID."""
    body = _native_import_body(await request.body(), NativeCanvasImportBody)
    parsed, diagnostics = _native_parse(body, uid)
    if not native_canvas.validation_digest_matches(
            body.validation_digest, parsed, diagnostics):
        raise APIError(
            409, "native Canvas validation digest does not match; validate this exact file again",
            code=APIErrorCode.CONFLICT, retryable=False)
    if any(item.severity == "error" for item in diagnostics):
        raise APIError(409, "native Canvas has blocking compatibility diagnostics; validate and fix them first",
                       code=APIErrorCode.CONFLICT, retryable=False)
    if any(item.severity == "warning" for item in diagnostics) and not body.confirm_warnings:
        raise APIError(409, "native Canvas has warnings; confirm them before creating a new Canvas",
                       code=APIErrorCode.CONFLICT, retryable=False)
    canvas_id = native_canvas.import_canvas_id(uid, str(body.import_id))
    doc = {**parsed["canvas"], "id": canvas_id, "version": 1}
    try:
        metadb.require_promoted_transform_use(uid, doc)
    except PermissionError as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    try:
        created = metadb.import_native_canvas(
            uid=uid, canvas_id=canvas_id, doc=doc,
            intent_digest=native_canvas.import_intent_digest(parsed))
    except metadb.NativeCanvasImportConflict as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    return {"ok": True, "id": canvas_id, "created": created, "replayed": not created}


@router.post("/canvas/copy/validate", response_model=CanvasCopyValidationResponse)
def validate_canvas_copy(body: CanvasCopyValidateBody,
                         uid: str = Depends(current_user)) -> dict:
    """Validate one exact source snapshot and Workspace destination without creating it."""
    try:
        canvas, items, intent, _manifest = _prepare_canvas_copy(body, uid)
    except (KeyError, PermissionError, metadb.WorkspaceVersionConflict, ValueError) as exc:
        _workspace_action_error(exc)
    summary = native_canvas.summary({
        "canvas": canvas, "descriptors": {}, "dataReferences": [], "libraryProcessors": [],
    }, items)
    summary["validationDigest"] = canvas_copy.validation_digest(intent, items)
    summary["copyIntentDigest"] = intent
    return summary


@router.post("/canvas/copy", response_model=NativeCanvasImportResponse)
def create_canvas_copy(body: CanvasCopyCreateBody,
                       uid: str = Depends(current_user)) -> dict:
    """Create or replay one owned, detached Canvas copy."""
    created_id = canvas_copy.canvas_id(uid, str(body.copy_id))
    request_digest = canvas_copy.request_digest(
        source_canvas_id=body.source_canvas_id,
        source_canvas_version=body.source_canvas_version,
        source_subject_id=body.source_subject_id,
        container_id=body.container_id,
        container_version=body.expected_container_version,
        name=body.name)
    try:
        if metadb.canvas_copy_replay(
                uid, created_id, body.copy_intent_digest, request_digest):
            return {"ok": True, "id": created_id, "created": False, "replayed": True}
        canvas, items, intent, manifest_sha256 = _prepare_canvas_copy(body, uid)
        if intent != body.copy_intent_digest or not canvas_copy.validation_matches(
                body.validation_digest, intent, items):
            raise metadb.WorkspaceVersionConflict(
                "Canvas copy validation changed; validate this exact source and destination again")
        if any(item.severity == "error" for item in items):
            raise ValueError("Canvas copy has blocking validation errors")
        if any(item.severity == "warning" for item in items) and not body.confirm_warnings:
            raise metadb.WorkspaceVersionConflict(
                "Canvas copy has warnings; confirm them before creating it")
        doc = {**canvas, "id": created_id, "version": 1}
        created = metadb.create_canvas_copy(
            uid=uid, canvas_id=created_id, doc=doc, intent_digest=intent,
            request_digest=request_digest,
            container_id=body.container_id,
            expected_container_version=body.expected_container_version,
            source_canvas_id=body.source_canvas_id,
            source_canvas_version=body.source_canvas_version,
            source_subject_id=body.source_subject_id,
            source_manifest_sha256=manifest_sha256)
    except metadb.CanvasCopyConflict as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    except (KeyError, PermissionError, metadb.WorkspaceVersionConflict, ValueError) as exc:
        _workspace_action_error(exc)
    return {"ok": True, "id": created_id, "created": created, "replayed": not created}


def _validate_canvas_execution_contract(doc: dict) -> None:
    try:
        Graph.model_validate(doc)
    except ValueError as exc:
        raise APIError(
            422, "canvas has an invalid execution contract",
            code=APIErrorCode.VALIDATION_ERROR, retryable=False,
        ) from exc


@router.post("/canvas")
def create_canvas(doc: dict, uid: str = Depends(current_user)) -> dict:
    _validate_canvas_execution_contract(doc)
    try:
        metadb.require_promoted_transform_use(uid, doc)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    with metadb.session() as s:
        # honor the client's id so the canvas exists under it immediately (no orphan row, and
        # sharing/opening works without waiting for the first autosave to PUT it).
        cid = doc.get("id") or metadb._uid()
        values = {
            "id": cid,
            "owner_id": uid,
            "name": doc.get("name") or "untitled",
            "version": doc.get("version", 1),
            "doc": json.dumps(doc),
        }
        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:  # pragma: no cover - supported deployments use SQLite or PostgreSQL
            raise RuntimeError(f"unsupported metadata database dialect: {dialect}")
        # The insert itself is the ownership decision. A prior read plus INSERT would race another
        # creator, while RETURNING proves this transaction inserted the row. Clients may only clean up
        # a cancelled import after receiving this positive evidence; an existing ID is never theirs.
        inserted_id = s.scalar(dialect_insert(metadb.Canvas).values(**values)
                               .on_conflict_do_nothing(index_elements=[metadb.Canvas.id])
                               .returning(metadb.Canvas.id))
        created = inserted_id is not None
        if created:
            # Materialize the durable owner row before the local-result registry lock.  Autoflush is
            # deliberately disabled inside that lock so every ownership path has one global order.
            s.flush()
            metadb.sync_local_result_owner(s, "canvas", cid, doc)
            metadb._replace_promoted_transform_refs(s, "canvas", cid, doc)
        metadb._workspace_ensure_root_placement_in_session(
            s, target_kind="canvas", target_id=cid, name=values["name"])
        return {"ok": True, "id": cid, "created": created}


@router.get("/canvas/{canvas_id}")
def get_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) is None:  # owner, shared, or workspace-visible
        raise APIError(
            404,
            f"canvas '{canvas_id}' not found",
            code=APIErrorCode.CANVAS_NOT_FOUND,
            retryable=False,
        )
    with metadb.session() as s:
        return json.loads(s.get(metadb.Canvas, canvas_id).doc)


@router.put("/canvas/{canvas_id}")
def put_canvas(canvas_id: str, doc: dict,
               expected_version: int | None = Query(None, alias="expectedVersion", ge=1),
               uid: str = Depends(current_user)) -> dict:
    _validate_canvas_execution_contract({**doc, "id": canvas_id})
    role = metadb.canvas_role(canvas_id, uid)  # None if the canvas doesn't exist yet
    try:
        metadb.require_promoted_transform_use(
            uid, doc, canvas_id=canvas_id if role is not None else None)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_id, with_for_update=True)
        previous_name = c.name if c is not None else None
        if c and role not in ("owner", "editor"):
            raise HTTPException(403, "you don't have edit access to this canvas")
        if expected_version is not None:
            if c is None:
                raise APIError(
                    409,
                    f"canvas '{canvas_id}' was deleted before this draft could sync",
                    code=APIErrorCode.CONFLICT,
                    retryable=False,
                )
            if c.version != expected_version:
                raise APIError(
                    409,
                    f"canvas '{canvas_id}' changed from expected version {expected_version}",
                    code=APIErrorCode.CONFLICT,
                    retryable=False,
                )
        if not c:
            c = metadb.Canvas(id=canvas_id, owner_id=uid)  # first save → the creator owns it
            s.add(c)
        version = expected_version + 1 if expected_version is not None else doc.get("version", 1)
        persisted_doc = {**doc, "id": canvas_id, "version": version}
        doc_json = json.dumps(persisted_doc)
        c.name = doc.get("name") or c.name or "untitled"
        c.version = version
        c.doc = doc_json
        s.flush()  # settle a newly-created owner row before the local-result registry lock
        metadb.sync_local_result_owner(s, "canvas", canvas_id, persisted_doc)
        metadb._replace_promoted_transform_refs(
            s, "canvas", canvas_id, persisted_doc)
        metadb._workspace_ensure_root_placement_in_session(
            s, target_kind="canvas", target_id=canvas_id, name=c.name)
        if previous_name is not None:
            metadb._workspace_follow_target_name_in_session(
                s, target_kind="canvas", target_id=canvas_id,
                previous_name=previous_name, name=c.name)
    # keep a throttled snapshot history so a bad edit is recoverable (autosave fires ~every 400ms; the
    # snapshotter dedups + rate-limits so it doesn't store every keystroke)
    metadb.snapshot_canvas(canvas_id, doc_json, version, author_id=uid)
    return {"ok": True, "id": canvas_id, "version": version}


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
        c = s.get(metadb.Canvas, canvas_id, with_for_update=True)
        if c is None:
            raise HTTPException(404, "not found")
        # snapshot the CURRENT state first so a restore is itself undoable, then swap in the old doc
        metadb._snapshot_canvas_in_session(
            s, c, c.doc, c.version, author_id=uid, label="before restore")
        c.doc = doc
        c.version = (c.version or 1) + 1
        restored_doc = json.loads(doc)
        metadb.sync_local_result_owner(s, "canvas", canvas_id, restored_doc)
        metadb._replace_promoted_transform_refs(
            s, "canvas", canvas_id, restored_doc)
    return {"ok": True, "id": canvas_id, "doc": json.loads(doc)}


@router.delete("/canvas/{canvas_id}")
def delete_canvas(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    if metadb.canvas_role(canvas_id, uid) == "owner":  # only the owner can delete
        try:
            metadb.delete_canvas_cascade(canvas_id)  # also drop shares + run history + versions (no FK cascade)
        except metadb.ActiveBackendJobsError as e:
            raise HTTPException(409, str(e))
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
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    if metadb.canvas_role(canvas_id, uid) != "owner":
        emit_audit(AuditAction.SHARING_CHANGE, AuditOutcome.DENIED, principal_id=uid,
                   resource_type="canvas", resource_id=canvas_id, attrs={"op": "share"})
        raise HTTPException(403, "only the owner can share")
    if "visibility" in body:
        if body["visibility"] not in ("private", "workspace", "workspace_view"):
            raise HTTPException(400, "invalid visibility")
        metadb.set_visibility(canvas_id, body["visibility"])
        emit_audit(AuditAction.SHARING_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
                   resource_type="canvas", resource_id=canvas_id, attrs={"op": "visibility"})
    if body.get("userId"):
        role = body.get("role", "editor")
        if role not in metadb.SHARE_ROLES:  # never let a share grant 'owner' — that's a privilege escalation
            raise HTTPException(422, f"invalid role {role!r}; must be one of {list(metadb.SHARE_ROLES)}")
        metadb.share_canvas(canvas_id, body["userId"], role)
        emit_audit(AuditAction.SHARING_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
                   resource_type="canvas", resource_id=canvas_id, attrs={"op": "share"})
    return {"ok": True}


@router.delete("/canvas/{canvas_id}/share/{user_id}")
def remove_share(canvas_id: str, user_id: str, uid: str = Depends(current_user)) -> dict:
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    if metadb.canvas_role(canvas_id, uid) != "owner":
        emit_audit(AuditAction.SHARING_CHANGE, AuditOutcome.DENIED, principal_id=uid,
                   resource_type="canvas", resource_id=canvas_id, attrs={"op": "unshare"})
        raise HTTPException(403, "only the owner can unshare")
    metadb.unshare_canvas(canvas_id, user_id)
    emit_audit(AuditAction.SHARING_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="canvas", resource_id=canvas_id, attrs={"op": "unshare"})
    return {"ok": True}


@router.get("/canvas/{canvas_id}/runs", response_model=list[RunHistoryRecord])
def canvas_runs(canvas_id: str, uid: str = Depends(current_user)) -> list[RunHistoryRecord]:
    """Run history for a canvas (persisted, survives restarts)."""
    if metadb.canvas_role(canvas_id, uid) is None:  # same authz as the other canvas endpoints
        raise HTTPException(404, "not found")
    return [RunHistoryRecord.model_validate(record) for record in metadb.list_runs(canvas_id)]


@router.get(
    "/canvas/{canvas_id}/runs/{subject_id}/manifest",
    response_model=ExecutionManifestDetail,
)
def execution_manifest_detail(
        canvas_id: str = Path(min_length=1, max_length=512),
        subject_id: str = Path(min_length=1, max_length=1024),
        uid: str = Depends(current_user)) -> ExecutionManifestDetail:
    """Inspect one immutable History/Jobs manifest under current Canvas visibility."""
    detail = metadb.execution_manifest_detail_for_subject(uid, canvas_id, subject_id)
    if detail is None:
        # Match the existing Canvas/run read boundary: revoked and unknown Canvases are not enumerable.
        raise HTTPException(404, "not found")
    return ExecutionManifestDetail.model_validate(detail)


@router.get("/jobs", response_model=WorkspaceRunPage)
def workspace_jobs(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=4096),
        status: Literal["queued", "running", "done", "failed", "cancelled"] | None = None,
        canvas_id: str | None = Query(default=None, max_length=512),
        node_id: str | None = Query(default=None, max_length=256),
        run_id: str | None = Query(default=None, max_length=512),
        backend: str | None = Query(default=None, max_length=256),
        after: datetime.datetime | None = None,
        before: datetime.datetime | None = None,
        q: str | None = Query(default=None, max_length=256),
        uid: str = Depends(current_user)) -> WorkspaceRunPage:
    """Bounded read-only history across every canvas the caller can currently access."""
    if after is not None and (after.tzinfo is None or after.utcoffset() is None):
        raise HTTPException(422, "after must include a timezone")
    if before is not None and (before.tzinfo is None or before.utcoffset() is None):
        raise HTTPException(422, "before must include a timezone")
    if after is not None and before is not None and after > before:
        raise HTTPException(422, "after must not be later than before")
    try:
        page = metadb.list_workspace_runs(
            uid, limit=limit, cursor=cursor, status=status,
            canvas_id=canvas_id, node_id=node_id, run_id=run_id, backend=backend,
            recorded_after=after, recorded_before=before, text=q,
        )
        return WorkspaceRunPage.model_validate(page)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/inbox", response_model=DurableTaskInboxPage)
def workspace_inbox(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=4096),
        filter: Literal["unread", "all"] = Query(default="all"),
        uid: str = Depends(current_user)) -> DurableTaskInboxPage:
    """Owner-scoped durable Task outcomes. Shared-canvas peers never see another owner's items."""
    try:
        page = metadb.list_durable_task_inbox_items(
            uid, limit=limit, cursor=cursor, unread_only=(filter == "unread"))
        return DurableTaskInboxPage.model_validate(page)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/inbox/unread-count", response_model=DurableTaskInboxUnreadCount)
def workspace_inbox_unread_count(
        uid: str = Depends(current_user)) -> DurableTaskInboxUnreadCount:
    return DurableTaskInboxUnreadCount(count=metadb.count_durable_task_inbox_unread(uid))


@router.post("/inbox/{item_id}/read", response_model=DurableTaskInboxItemView)
def workspace_inbox_mark_read(
        item_id: str, uid: str = Depends(current_user)) -> DurableTaskInboxItemView:
    """Idempotent mark-one-read. Missing or cross-owner items are unavailable."""
    item = metadb.mark_durable_task_inbox_item_read(uid, item_id)
    if item is None:
        raise HTTPException(404, "not found")
    return DurableTaskInboxItemView.model_validate(item)


@router.get("/canvas/{canvas_id}/active-runs", response_model=list[RunStatus])
def canvas_active_runs(canvas_id: str, uid: str = Depends(current_user)) -> list[RunStatus]:
    """In-flight runs for a canvas, so a reopened canvas re-subscribes to a run that survived a hub
    restart on its kernel (rather than the run silently vanishing from the UI). Rebuilt into RunStatus
    so it serializes with the same camelCase wire shape as GET /run/{id}."""
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    return [RunStatus(**d) for d in metadb.active_runs(canvas_id)]


@router.get("/canvas/{canvas_id}/profile-jobs", response_model=list[RunStatus])
def canvas_profile_jobs(canvas_id: str, uid: str = Depends(current_user)) -> list[RunStatus]:
    """Latest durable profile attempt for each node/plan identity, including terminal attempts.

    Unlike ``active-runs``, this bounded recovery surface lets a profile that finished while the canvas
    was closed reappear on reopen. The client still verifies the fixed plan digest against its current
    graph identity before presenting the result.
    """
    if metadb.canvas_role(canvas_id, uid) is None:
        raise HTTPException(404, "not found")
    return [RunStatus(**doc) for doc in metadb.latest_profile_jobs(canvas_id)]


@router.get("/canvas/{canvas_id}/kernel")
def canvas_kernel(canvas_id: str, uid: str = Depends(current_user)) -> dict:
    """The per-canvas execution kernel's live state (Jupyter-style), or {exists:false} if none is
    running. Token/endpoint are internal — only lease state + the kernel's own /status are surfaced.
    Read-only: this NEVER spawns a kernel, and the /status proxy fast-fails so a dead kernel can't
    stall the request."""
    if metadb.canvas_role(canvas_id, uid) is None:
        # unknown/inaccessible canvas → "no kernel" (kernel liveness is not sensitive, and a 404 here
        # would log a browser console error while a just-created canvas is not yet persisted). The
        # mutating restart endpoint below stays role-gated.
        return {"exists": False}
    k = metadb.get_kernel(canvas_id)
    if k is None:
        return {"exists": False}
    out: dict = {"exists": True, "state": k["state"], "stale": k["stale"]}
    if k.get("endpoint") and k.get("token") and not k["stale"]:
        from hub import kernel_backend
        try:
            out.update(kernel_backend._get(k["endpoint"], "/status", k["token"],
                                            timeout=2.0, connect_retries=0))
            out["reachable"] = True
        except Exception:  # noqa: BLE001 — a live lease whose HTTP status can't be reached is NOT healthy;
            out["reachable"] = False  # surface it so the badge shows degraded, not warm/green
    return out


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


# Plugin settings declared ``secret`` store references (env:VAR / file:/path), never material values.
# Agent and object-store credentials are first-class Cred entities below; the removed pre-1.0 setting
# keys are neither returned nor writable through this generic settings API.


_REMOVED_CREDENTIAL_SETTINGS = {
    "agentApiKey": (
        "agentApiKey is no longer supported; create an agent Cred via /api/creds and bind it with "
        "agentCredId"
    ),
    "objectStore": (
        "objectStore is no longer supported; create an object_store Cred via /api/creds and bind it "
        "with defaultObjectStoreCredId or a destination credId"
    ),
}


def _plugin_secret_keys() -> set[str]:
    """Setting keys `plugin.<pack>.<field>` whose declared [[config]] field is `secret`."""
    from hub.secrets import plugin_secret_setting_keys
    return plugin_secret_setting_keys()


def _prepare_setting_changes(
        changes: list[SettingBody], plugin_secrets: set[str]) \
        -> tuple[list[tuple[str, str, object]], bool]:
    from hub.secrets import validate_secret_reference

    prepared: list[tuple[str, str, object]] = []
    sensitive = False
    total_bytes = 0
    for change in changes:
        if change.key in _REMOVED_CREDENTIAL_SETTINGS:
            raise HTTPException(400, _REMOVED_CREDENTIAL_SETTINGS[change.key])
        value = change.value
        is_secret = change.scope == "global" and change.key in plugin_secrets
        if is_secret:
            try:
                value = validate_secret_reference(value, field=change.key)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            value_bytes = len(encoded.encode("utf-8"))
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise HTTPException(400, f"setting '{change.key}' must be valid JSON") from exc
        total_bytes += value_bytes
        if total_bytes > MAX_SETTING_BATCH_VALUE_BYTES:
            raise HTTPException(
                400,
                f"settings batch values must total at most {MAX_SETTING_BATCH_VALUE_BYTES} bytes",
            )
        sensitive = sensitive or is_secret
        prepared.append((change.scope, change.key, value))
    return prepared, sensitive


def _settings_batch_audit(
        outcome, *, uid: str, changes: list[SettingBody], sensitive: bool = False):
    from hub.observability import AuditAction, emit_audit

    scopes = ",".join(sorted({change.scope for change in changes})) or "none"
    return emit_audit(
        AuditAction.ADMIN_SETTINGS_CHANGE, outcome, principal_id=uid,
        resource_type="settings_batch", resource_id="batch",
        attrs={
            "scopes": scopes,
            "change_count": str(len(changes)),
            "sensitive": "true" if sensitive else "false",
        },
    )


@router.get("/settings", response_model=SettingsSnapshot)
def get_settings(uid: str = Depends(current_user)) -> SettingsSnapshot:
    from hub.secrets import redact_secret_for_display
    plugin_secrets = _plugin_secret_keys()
    rows, revision = metadb.settings_snapshot(uid)
    out: dict[str, dict[str, object]] = {"global": {}, "user": {}}
    for scope, key, encoded in rows:
        if key in _REMOVED_CREDENTIAL_SETTINGS:
            continue
        value = json.loads(encoded)
        out[scope][key] = (
            redact_secret_for_display(value)
            if scope == "global" and key in plugin_secrets else value
        )
    return SettingsSnapshot.model_validate({**out, "revision": revision})


@router.put("/settings")
def put_setting(body: SettingBody, uid: str = Depends(current_user)) -> dict:
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    if body.scope == "global":
        try:
            _require_admin(uid)  # instance-wide settings (object-store creds, agent key, destinations) — admin only
        except HTTPException:
            emit_audit(AuditAction.ADMIN_SETTINGS_CHANGE, AuditOutcome.DENIED, principal_id=uid,
                       resource_type="setting", resource_id=body.key, attrs={"scope": body.scope})
            raise
    plugin_secrets = _plugin_secret_keys()
    prepared, _sensitive = _prepare_setting_changes([body], plugin_secrets)
    _scope, _key, value = prepared[0]
    scope_id = uid if body.scope == "user" else ""
    metadb.set_setting(body.key, value, scope=body.scope, scope_id=scope_id)
    is_secret = body.key in plugin_secrets
    # attr key must avoid validate_audit_attrs' secret-name regex, else this event is rejected + dropped.
    emit_audit(AuditAction.ADMIN_SETTINGS_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="setting", resource_id=body.key,
               attrs={"scope": body.scope, "sensitive": "true" if is_secret else "false"})
    return {"ok": True}


@router.put(
    "/settings/batch",
    response_model=SettingsBatchResult,
    responses={409: {"model": SettingsBatchConflict}},
)
def put_settings_batch(
        body: SettingsBatchBody, uid: str = Depends(current_user)) \
        -> SettingsBatchResult | JSONResponse:
    from hub.observability import AuditOutcome

    # Authorize the whole requested scope set before validating or touching any value. In particular,
    # a user change placed before an unauthorized global change must never be partially committed.
    if any(change.scope == "global" for change in body.changes) and not _can_manage_global(uid):
        _settings_batch_audit(AuditOutcome.DENIED, uid=uid, changes=body.changes)
        raise HTTPException(403, "admin only")
    prepared, sensitive = _prepare_setting_changes(body.changes, _plugin_secret_keys())
    try:
        revision = metadb.set_settings_batch(
            prepared, body.expected_revision.as_dict(), uid)
    except metadb.SettingsRevisionConflict as exc:
        conflict = SettingsBatchConflict(
            detail=str(exc), code=APIErrorCode.CONFLICT, retryable=False,
            revision=SettingsRevision.model_validate(exc.current_revision),
        )
        return JSONResponse(
            status_code=409,
            content=conflict.model_dump(mode="json", by_alias=True),
        )
    if body.changes:
        _settings_batch_audit(
            AuditOutcome.SUCCESS, uid=uid, changes=body.changes, sensitive=sensitive)
    return SettingsBatchResult.model_validate({"ok": True, "revision": revision})


# Credentials (issue #156) — a Cred entity is admin-only instance-wide config, like global settings.
# Fields are secret REFERENCES (env:/file:), never raw bytes; cred_upsert rejects a raw secret. A
# defense-in-depth redaction still masks any residual plaintext before a cred leaves an API response.


def _redact_cred(cred: dict) -> dict:
    from hub.secrets import OBJECT_STORE_SECRET_SUBKEYS, redact_secret_for_display
    fields = dict(cred.get("fields") or {})
    secret_fields = OBJECT_STORE_SECRET_SUBKEYS if cred.get("kind") == "object_store" else ("apiKey",)
    for field in secret_fields:
        if field in fields:
            fields[field] = redact_secret_for_display(fields[field])
    return {**cred, "fields": fields}


@router.get("/creds")
def list_creds(uid: str = Depends(current_user)) -> list[dict]:
    _require_admin(uid)
    return [_redact_cred(c) for c in metadb.creds_list()]


@router.post("/creds")
def create_cred(body: CredUpsert, uid: str = Depends(current_user)) -> dict:
    return _upsert_cred(None, body, uid)


@router.put("/creds/{cred_id}")
def update_cred(cred_id: str, body: CredUpsert, uid: str = Depends(current_user)) -> dict:
    return _upsert_cred(cred_id, body, uid)


def _upsert_cred(cred_id: str | None, body: CredUpsert, uid: str) -> dict:
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    try:
        _require_admin(uid)
    except HTTPException:
        emit_audit(AuditAction.ADMIN_SETTINGS_CHANGE, AuditOutcome.DENIED, principal_id=uid,
                   resource_type="cred", resource_id=cred_id or "new")
        raise
    try:
        cred = metadb.cred_upsert(cred_id, body.name, body.kind, body.fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    emit_audit(AuditAction.ADMIN_SETTINGS_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="cred", resource_id=cred["id"], attrs={"sensitive": "true"})
    return _redact_cred(cred)


@router.delete("/creds/{cred_id}")
def delete_cred(cred_id: str, uid: str = Depends(current_user)) -> dict:
    from hub.observability import AuditAction, AuditOutcome, emit_audit
    _require_admin(uid)
    try:
        metadb.cred_delete(cred_id)
    except ValueError as exc:  # still bound → 409, don't strand a reference that would fail open
        raise HTTPException(409, str(exc)) from exc
    emit_audit(AuditAction.ADMIN_SETTINGS_CHANGE, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="cred", resource_id=cred_id)
    return {"ok": True}
