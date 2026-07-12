"""Metadata store — users, canvases (per-user files), and settings.

A small SQLAlchemy layer, separate from `db.py` (which is the DuckDB data engine). Dev uses a
bundled SQLite file; deployment points DP_DATABASE_URL at Postgres. Only the connection string is
config; all metadata lives in this instance's DB. Per-user authentication is implemented in
`hub.auth` + `current_user` (signed session cookies gated by DP_AUTH_SECRET, verifying each
user's own scrypt password hash); with no secret set, an open X-DP-User dev mode defaults to a
seeded local user.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import uuid

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String,
    Text, UniqueConstraint, create_engine, func, or_, select, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from hub.settings import settings

DEFAULT_USER_ID = "local"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # per-user credential (auth mode)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # gates global settings + user management
    token_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # bump → revoke sessions
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Canvas(Base):
    __tablename__ = "canvases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String, default="untitled")
    version: Mapped[int] = mapped_column(Integer, default=1)
    doc: Mapped[str] = mapped_column(Text, default="{}")  # the full CanvasDoc as JSON
    visibility: Mapped[str] = mapped_column(String, default="private")  # 'private' | 'workspace' (edit) | 'workspace_view' (read-only)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CanvasShare(Base):
    """An explicit collaborator on a canvas (beyond the owner)."""
    __tablename__ = "canvas_shares"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String, default="editor")  # 'editor' | 'viewer' — never 'owner'
    # keep this in lockstep with migration 0015; ownership is by Canvas.owner_id alone, never a share
    __table_args__ = (
        UniqueConstraint("canvas_id", "user_id", name="uq_share"),
        CheckConstraint("role IN ('editor', 'viewer')", name="ck_share_role"),
    )


class RunRecord(Base):
    """A finished run, kept with its canvas (run history survives restarts). One row per run."""
    __tablename__ = "run_records"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    # The runner's real id links durable history back to the logical run. Nullable for records written
    # before migration 0018; `id` remains the history row's own primary key.
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_table: Mapped[str | None] = mapped_column(String, nullable=True)
    output_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    per_node: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: durable per-node breakdown
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CanvasVersion(Base):
    """A point-in-time snapshot of a canvas doc, for restore-after-a-bad-edit. Auto-captured (throttled)
    on save, plus explicit named snapshots. One row per snapshot; oldest auto-snapshots are pruned."""
    __tablename__ = "canvas_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    doc: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String, nullable=True)  # set for explicit named snapshots
    author_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RunState(Base):
    """Live / last-known status of a run, keyed by run_id, so GET /run/{id} + the status WebSocket are
    served from the shared DB: ANY (stateless) web instance can answer, and status survives a kernel
    restart instead of 404-ing. Distinct from RunRecord (the per-canvas run HISTORY). One row per run.
    (This is the run-state half of making the web tier stateless — see hub.deps.)"""
    __tablename__ = "run_states"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    canvas_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, index=True)  # queued | running | done | failed | cancelled
    doc: Mapped[str] = mapped_column(Text)  # the full RunStatus as JSON
    kernel_id: Mapped[str | None] = mapped_column(String, nullable=True)  # owning kernel (None = in-process/subprocess run, dies with the hub)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)  # the run's creator uid (durable owner, for authz)
    auth_canvas_id: Mapped[str | None] = mapped_column(String, nullable=True)  # the real canvas it was authorized against (None = ad-hoc)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class SchemaContract(Base):
    """A named, versioned schema contract — a workspace artifact multiple pipelines reference by name
    (a node's config.outputSchema = {"ref": name}) instead of each carrying a private inline copy.
    Saving under an existing name mints a new version, so drift is a diff between versions."""
    __tablename__ = "schema_contracts"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)  # JSON: [{name, type}, ...]
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Kernel(Base):
    """Lease for a per-canvas execution kernel (a long-lived process, or a pod). `canvas_id` PK makes
    the INSERT the single-spawner claim; `kernel_id` fences a replaced/zombie kernel out of
    heartbeating / dropping the row / writing run status — the guard the whole no-split-brain
    invariant rests on. Any hub reaches a kernel by resolving canvas_id → endpoint (no sticky LB)."""
    __tablename__ = "kernels"
    canvas_id: Mapped[str] = mapped_column(String, primary_key=True)
    kernel_id: Mapped[str] = mapped_column(String)
    endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    token: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)  # starting | ready
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CatalogEntry(Base):
    """A registered dataset / written output, shared across instances. The in-memory catalog write-throughs
    here on register and loads from here on read, so a dataset registered on one (stateless) web instance
    is visible to the others + survives a restart without re-probing. Keyed by uri; `doc` is the full
    CatalogTable (incl. probed schema) as JSON so no re-probe is needed to serve it.

    The `tbl_id`, `folder`, and `owner` columns are a filterable/sortable PROJECTION of `doc` (the doc
    stays authoritative). Promoting + indexing them is what lets browse/search/facet PUSH DOWN to the
    DB — the catalog answers a filtered page with an indexed query instead of loading every row into
    memory and filtering in Python (the old O(n)-per-read model that didn't scale past a few hundred)."""
    __tablename__ = "catalog_entries"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    doc: Mapped[str] = mapped_column(Text)  # the full CatalogTable as JSON
    tbl_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # CatalogTable.id (stable browse id)
    folder: Mapped[str] = mapped_column(String, default="", server_default="", index=True)  # browse-path namespace
    owner: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)  # promoted for sort/display
    usage: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)  # read-count popularity signal
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, index=True)


class CatalogTag(Base):
    """One (dataset uri → tag) membership. A join table (not a JSON blob on the entry) so a tag filter
    / a tag facet is an indexed query, and two instances tagging concurrently can't clobber each other."""
    __tablename__ = "catalog_tags"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    tag: Mapped[str] = mapped_column(String, primary_key=True)
    __table_args__ = (Index("ix_catalog_tags_tag", "tag"),)


class CatalogColumn(Base):
    """One (dataset uri → column name) pair, mirrored from the probed schema. Powers 'has column X'
    filtering + a column facet + full-text over column names without cracking every doc's JSON."""
    __tablename__ = "catalog_columns"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    column: Mapped[str] = mapped_column(String, primary_key=True)
    __table_args__ = (Index("ix_catalog_columns_column", "column"),)


class CatalogEmbedding(Base):
    """A dataset's semantic embedding (over name + description + columns), for semantic/hybrid search.
    Written only when an embedder is registered (reg.add_embedder) — the vector is opaque float32
    bytes + its dim, so the store stays engine-neutral and the search does cosine in the catalog."""
    __tablename__ = "catalog_embeddings"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String)
    dim: Mapped[int] = mapped_column(Integer)
    vec: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CatalogEdge(Base):
    """A lineage edge (parent uri → child uri), shared like CatalogEntry so lineage is cross-instance.
    `column` records column-level provenance when known (which output column derived from the input)."""
    __tablename__ = "catalog_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent: Mapped[str] = mapped_column(String, index=True)
    child: Mapped[str] = mapped_column(String, index=True)
    column: Mapped[str | None] = mapped_column(String, nullable=True)
    pipeline: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (UniqueConstraint("parent", "child", name="uq_catalog_edge"),)


class ResultCache(Base):
    """Content-addressed result index: a run plan's content hash → where its output landed (uri /
    table / rows / fmt, as JSON). Persisted + shared so a completed run's output is REUSED across
    kernel restarts AND across stateless web instances — the old in-process dict was per-process and
    lost on restart. Not authoritative data: a miss just recomputes, so it's safe to prune (newest N)."""
    __tablename__ = "result_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)  # {uri, table, rows, fmt}
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InstallationIdentity(Base):
    """One durable identity for every hub instance sharing this metadata database."""
    __tablename__ = "installation_identity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_token: Mapped[str] = mapped_column(String, nullable=False)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_installation_identity_singleton"),
        UniqueConstraint("owner_token", name="uq_installation_identity_owner_token"),
    )


class ObjectAttempt(Base):
    """Authoritative lifecycle registry for immutable object-store write attempts.

    Object storage holds only data and commit markers. Publication ownership, logical replacement, and
    bounded GC selection live here so every hub instance observes one transactionally ordered state.
    """
    __tablename__ = "object_attempts"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    logical_uri: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="writing", server_default="writing", index=True)
    reference_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gc_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("kind IN ('region', 'sink')", name="ck_object_attempt_kind"),
        CheckConstraint(
            "state IN ('writing', 'published', 'retiring', 'retired', 'discarding')",
            name="ck_object_attempt_state",
        ),
        Index("ix_object_attempts_gc", "state", "gc_attempted_at", "retired_at", "created_at", "uri"),
        Index("ix_object_attempts_sink_target", "kind", "logical_uri", "state"),
    )


class Setting(Base):
    __tablename__ = "settings"
    # scope 'global' (scope_id='') for system settings; scope 'user' (scope_id=user id) for prefs
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String, default="")
    key: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded
    __table_args__ = (UniqueConstraint("scope", "scope_id", "key", name="uq_setting"),)


class CatalogRelationship(Base):
    """An owner-declared join relationship, ONE ROW each (keyed by an orientation-insensitive rel_key)
    — not a single JSON blob, so two instances declaring different relationships can't clobber each
    other (each add/remove touches only its own row). `doc` is the full Relationship model_dump."""
    __tablename__ = "catalog_relationships"
    rel_key: Mapped[str] = mapped_column(String, primary_key=True)
    doc: Mapped[str] = mapped_column(Text)


class CatalogDeclaredKey(Base):
    """An owner-declared primary key, ONE ROW per dataset uri (columns as a JSON list) — same
    per-row isolation as CatalogRelationship (no shared-blob lost update)."""
    __tablename__ = "catalog_declared_keys"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    columns: Mapped[str] = mapped_column(Text)  # JSON list of column names


_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = settings.database_url
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            # The bundled default is a local SQLite file, but run status is upserted from daemon runner
            # threads on every per-node step, concurrent with autosave PUTs, catalog writes, and fast
            # GET-run polling. With the default rollback journal + busy_timeout=0 that intermittently
            # raises SQLITE_BUSY ("database is locked") → lost status updates / 500s. WAL lets readers
            # and one writer proceed without blocking each other; busy_timeout makes a contended writer
            # wait-and-retry instead of failing immediately. (No-op for the Postgres deployment path.)
            from sqlalchemy import event

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA busy_timeout=5000")  # ms
                    cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; avoids fsync per commit
                finally:
                    cur.close()
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def _alembic_cfg():
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    return cfg


def init_db() -> None:
    """Bring the metadata schema to head via Alembic, then seed the default local user.

    Alembic is the source of truth for schema. A pre-Alembic DB (tables created by the old
    create_all) is adopted by stamping the baseline before upgrading, so existing installs migrate
    cleanly instead of erroring on already-present tables."""
    from alembic import command
    from sqlalchemy import inspect

    names = set(inspect(engine()).get_table_names())
    cfg = _alembic_cfg()
    if "users" in names and "alembic_version" not in names:
        command.stamp(cfg, "0001_baseline")  # legacy DB → adopt the baseline without recreating tables
    command.upgrade(cfg, "head")
    with session() as s:
        u = s.get(User, DEFAULT_USER_ID)
        if u is None:
            u = User(id=DEFAULT_USER_ID, name="Local", is_admin=True)  # the seeded/bootstrap user is the admin
            s.add(u)
        elif not u.is_admin and s.query(User).filter(User.is_admin).count() == 0:
            u.is_admin = True  # no admin exists yet (upgraded DB) → the default user becomes admin
        # bootstrap: seed the default user's credential from DP_AUTH_PASSWORD (once) so an existing
        # shared-password deployment keeps working after upgrading to per-user auth
        from hub import auth
        bootstrap = auth.bootstrap_password()
        if auth.auth_enabled() and not u.password_hash and bootstrap:
            u.password_hash = auth.hash_password(bootstrap)
    # The cleartext value is bootstrap input, not runtime configuration. Consume it after the DB commit
    # whether or not this restart needed to seed a hash, so no subsequently spawned workload inherits it.
    if bootstrap:
        os.environ.pop("DP_AUTH_PASSWORD", None)
    reap_kernels()        # drop leases whose kernel is dead (stale heartbeat)
    reap_orphaned_runs()  # fail in-flight runs whose owning kernel is gone; live-kernel runs survive (reattach)


@contextlib.contextmanager
def session():
    engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ping(timeout_s: float = 3.0) -> bool:
    """True if the metadata DB answers a trivial query within the budget — the readiness probe's DB check.
    A timeout/failure (DB down, connection pool exhausted, migrations never ran) returns False."""
    import threading
    from sqlalchemy import text
    ok: list[bool] = []

    def _q() -> None:
        try:
            with session() as s:
                s.execute(text("SELECT 1"))
            ok.append(True)
        except Exception:  # noqa: BLE001 — any failure = not-ready
            pass
    t = threading.Thread(target=_q, daemon=True)
    t.start()
    t.join(timeout_s)
    return bool(ok)


def is_admin(user_id: str | None) -> bool:
    """Whether this user may change instance-wide config (global settings, user management)."""
    if not user_id:
        return False
    with session() as s:
        u = s.get(User, user_id)
        return bool(u and u.is_admin)


def resolve_user(user_id: str | None) -> str:
    """Return a valid user id for the request — the header's user if it exists, else the default
    local user (created on demand). Light by design; a real auth layer replaces this later."""
    with session() as s:
        if user_id and s.get(User, user_id) is not None:
            return user_id
        if s.get(User, DEFAULT_USER_ID) is None:
            s.add(User(id=DEFAULT_USER_ID, name="Local"))
        return DEFAULT_USER_ID


def user_password_hash(user_id: str) -> str | None:
    with session() as s:
        u = s.get(User, user_id)
        return u.password_hash if u else None


def set_user_password(user_id: str, pw_hash: str | None) -> bool:
    with session() as s:
        u = s.get(User, user_id)
        if u is None:
            return False
        u.password_hash = pw_hash
        u.token_epoch = (u.token_epoch or 0) + 1  # revoke every outstanding session on a password change
        return True


def user_token_epoch(user_id: str) -> int | None:
    """The user's current session epoch, or None if the user doesn't exist (→ a token for a deleted /
    unknown user fails to verify). Read on each authed request by auth.verify."""
    with session() as s:
        u = s.get(User, user_id)
        return (u.token_epoch or 0) if u is not None else None


def bump_token_epoch(user_id: str) -> None:
    """Invalidate all outstanding sessions for a user (call on disable / delete / forced logout)."""
    with session() as s:
        u = s.get(User, user_id)
        if u is not None:
            u.token_epoch = (u.token_epoch or 0) + 1


def get_setting(key: str, scope: str = "global", scope_id: str = "", default=None):
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        return json.loads(row.value) if row else default


SHARE_ROLES = ("editor", "viewer")  # the ONLY roles a share may grant; ownership is by owner_id alone


def _effective_canvas_role(c: Canvas, uid: str, explicit_role: str | None = None) -> str | None:
    """Resolve one role consistently: owner > explicit share > workspace visibility baseline."""
    if c.owner_id == uid:
        return "owner"
    if explicit_role is not None:
        # A share can NEVER confer ownership. Clamp a legacy/out-of-band value rather than letting it
        # override Canvas.owner_id; this also keeps list_canvases_for aligned with canvas_role.
        return explicit_role if explicit_role in SHARE_ROLES else "viewer"
    if c.visibility == "workspace":
        return "editor"
    if c.visibility == "workspace_view":
        return "viewer"
    return None


def canvas_role(canvas_id: str, uid: str) -> str | None:
    """The user's access to a canvas: 'owner' | 'editor' | 'viewer' | None."""
    with session() as s:
        c = s.get(Canvas, canvas_id)
        if c is None:
            return None
        explicit_role = s.scalar(
            select(CanvasShare.role).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == uid)
        )
        return _effective_canvas_role(c, uid, explicit_role)


def canvas_exists(canvas_id: str) -> bool:
    """True if a canvas row exists (regardless of who can reach it). Used to tell an authorized run
    against a real canvas apart from an ad-hoc, never-saved graph id."""
    with session() as s:
        return s.get(Canvas, canvas_id) is not None


def run_canvas_id(run_id: str) -> str | None:
    """The canvas a run was launched against (from its persisted run_state), or None if the run is
    unknown / was an ad-hoc graph. This is the DB-backed owner signal for run-object authorization."""
    with session() as s:
        r = s.get(RunState, run_id)
        return r.canvas_id if r else None


def bind_run_owner(run_id: str, uid: str, auth_canvas_id: str | None) -> None:
    """Persist a run's creator (authoritative, unspoofable owner) and the real canvas it was authorized
    against (None for an ad-hoc graph). Upserts so it works whether the run_state row exists yet."""
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None:
            s.add(RunState(run_id=run_id, canvas_id=auth_canvas_id, status="queued", doc="{}",
                           created_by=uid, auth_canvas_id=auth_canvas_id))
        else:
            r.created_by = uid
            r.auth_canvas_id = auth_canvas_id


def run_auth(run_id: str) -> tuple[str | None, str | None]:
    """(creator uid, authorized real-canvas id) for a run, or (None, None) if unknown / a legacy run
    persisted before these columns existed."""
    with session() as s:
        r = s.get(RunState, run_id)
        return (r.created_by, r.auth_canvas_id) if r else (None, None)


def share_canvas(canvas_id: str, user_id: str, role: str = "editor") -> None:
    if role not in SHARE_ROLES:  # reject 'owner' or any junk at the write boundary, not just the API layer
        raise ValueError(f"invalid share role {role!r}; must be one of {SHARE_ROLES}")
    with session() as s:
        sh = s.scalar(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == user_id))
        if sh:
            sh.role = role
        else:
            s.add(CanvasShare(canvas_id=canvas_id, user_id=user_id, role=role))


def unshare_canvas(canvas_id: str, user_id: str) -> None:
    with session() as s:
        sh = s.scalar(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == user_id))
        if sh:
            s.delete(sh)


def list_shares(canvas_id: str) -> list[dict]:
    with session() as s:
        rows = s.execute(
            select(CanvasShare, User.name).join(User, User.id == CanvasShare.user_id)
            .where(CanvasShare.canvas_id == canvas_id)
        ).all()
        return [{"userId": sh.user_id, "name": name, "role": sh.role} for sh, name in rows]


def set_visibility(canvas_id: str, visibility: str) -> None:
    with session() as s:
        c = s.get(Canvas, canvas_id)
        if c:
            c.visibility = visibility


def _canvas_row(c: "Canvas", role: str, shared: bool) -> dict:
    return {"id": c.id, "name": c.name, "version": c.version, "role": role, "shared": shared,
            "visibility": c.visibility, "updatedAt": c.updated_at.isoformat() if c.updated_at else None}


def list_canvases_for(uid: str) -> list[dict]:
    """Canvases a user can see: owned + explicitly shared + workspace-visible (deduped)."""
    with session() as s:
        out: dict[str, dict] = {}
        for c in s.scalars(select(Canvas).where(Canvas.owner_id == uid)):
            out[c.id] = _canvas_row(c, _effective_canvas_role(c, uid), False)
        for c, explicit_role in s.execute(select(Canvas, CanvasShare.role)
                                          .join(CanvasShare, CanvasShare.canvas_id == Canvas.id)
                                          .where(CanvasShare.user_id == uid)).all():
            out.setdefault(c.id, _canvas_row(c, _effective_canvas_role(c, uid, explicit_role), True))
        for c in s.scalars(select(Canvas).where(Canvas.visibility == "workspace", Canvas.owner_id != uid)):
            out.setdefault(c.id, _canvas_row(c, _effective_canvas_role(c, uid), True))
        for c in s.scalars(select(Canvas).where(Canvas.visibility == "workspace_view", Canvas.owner_id != uid)):
            out.setdefault(c.id, _canvas_row(c, _effective_canvas_role(c, uid), True))
        return sorted(out.values(), key=lambda r: r["updatedAt"] or "", reverse=True)


_RUN_HISTORY_MAX = 500  # per-canvas run_records cap — bound the local DB (older history is pruned)


def record_run(canvas_id: str | None, target_node_id: str | None, status: str,
               rows: int | None = None, ms: int | None = None, error: str | None = None,
               output_table: str | None = None, per_node: list[dict] | None = None,
               run_id: str | None = None, output_uri: str | None = None) -> bool:
    """Persist a finished run under its canvas. No-op (returns False) without a real canvas — an ad-hoc
    API run or an internal region sub-run (graph id '_region'). Returns True when a row was written.
    Prunes this canvas's history to the newest _RUN_HISTORY_MAX rows so the local DB can't grow without
    bound (one row per run, forever) — mirrors the ResultCache / CanvasVersion caps."""
    if not canvas_id:
        return False
    with session() as s:
        if s.get(Canvas, canvas_id) is None:
            return False  # ad-hoc / unsaved-canvas / internal region run → don't dangle a run row
        s.add(RunRecord(canvas_id=canvas_id, run_id=run_id, target_node_id=target_node_id, status=status,
                        rows=rows, ms=ms, error=error, output_table=output_table,
                        output_uri=output_uri,
                        per_node=json.dumps(per_node, default=str) if per_node else None))
        s.flush()
        stale = s.scalars(select(RunRecord.id).where(RunRecord.canvas_id == canvas_id)
                          .order_by(RunRecord.created_at.desc()).offset(_RUN_HISTORY_MAX)).all()
        for rid in stale:
            obj = s.get(RunRecord, rid)
            if obj:
                s.delete(obj)
        return True


def delete_canvas_cascade(canvas_id: str) -> None:
    """Delete a canvas and its children (shares, run history) — FKs don't cascade (SQLite FK off,
    Postgres would error), so clean them explicitly."""
    with session() as s:
        for sh in s.scalars(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id)):
            s.delete(sh)
        for r in s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id)):
            s.delete(r)
        for v in s.scalars(select(CanvasVersion).where(CanvasVersion.canvas_id == canvas_id)):
            s.delete(v)
        # also drop this canvas's run_states — else auth_canvas_id/canvas_id dangle into a reusable id
        # namespace and a later canvas re-created under the same id could re-grant its old runs (P0-AUTH-02)
        for rs in s.scalars(select(RunState).where(
                (RunState.canvas_id == canvas_id) | (RunState.auth_canvas_id == canvas_id))):
            s.delete(rs)
        c = s.get(Canvas, canvas_id)
        if c:
            s.delete(c)


def latest_actuals(canvas_id: str | None) -> dict[str, int]:
    """Node measured row counts from recent SUCCESSFUL runs of this canvas — feeds the size estimator (as
    `actuals`) so nodes whose output is statically unknowable (join / aggregate / sql / code) carry a real
    count on the next estimate instead of 'unknown'. The primary source is each run's TARGET count
    (RunRecord.rows — the per_node breakdown leaves a lazy relation's own rows null); any per_node entry
    that DID capture a count (a materialized checkpoint) is folded in too. Scans the last several runs so
    different targets are covered, most-recent wins. The caller guards staleness (only nodes still
    'latest')."""
    if not canvas_id:
        return {}
    out: dict[str, int] = {}
    with session() as s:
        recs = s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id, RunRecord.status == "done")
                         .order_by(RunRecord.created_at.desc()).limit(20)).all()
        for r in recs:  # most-recent first — never let an older run overwrite a fresher measurement
            if r.target_node_id and r.rows is not None and r.target_node_id not in out:
                out[r.target_node_id] = int(r.rows)
            if r.per_node:
                try:
                    pn = json.loads(r.per_node)
                except (ValueError, TypeError):
                    pn = []
                for p in pn:
                    if isinstance(p, dict) and p.get("node_id") and p.get("rows") is not None and p["node_id"] not in out:
                        out[p["node_id"]] = int(p["rows"])
    return out


def list_runs(canvas_id: str, limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id)
                         .order_by(RunRecord.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "runId": r.run_id, "status": r.status,
                 "targetNodeId": r.target_node_id, "rows": r.rows,
                 "ms": r.ms, "error": r.error, "outputTable": r.output_table,
                 "outputUri": r.output_uri,
                 "perNode": json.loads(r.per_node) if r.per_node else None,
                 "createdAt": r.created_at.isoformat() if r.created_at else None} for r in rows]


_RUN_STATE_MAX = 2000  # cap on TERMINAL run_states — live (queued/running) rows are never pruned
_TERMINAL_RUN = ("done", "failed", "cancelled")


def save_run_state(run_id: str, status: dict, canvas_id: str | None = None, kernel_id: str | None = None) -> None:
    """Upsert a run's live status (the runner calls this on each transition). `status` is a RunStatus
    model_dump; stored whole as JSON so GET /run/{id} can rebuild it on any instance. `kernel_id`
    stamps the owning kernel so the boot-time reaper fails a run only when its kernel is gone. When a run
    reaches a terminal status, prunes finished run_states to the newest _RUN_STATE_MAX (each row holds a
    full RunStatus JSON, so unbounded growth is a real local-DB leak) — live rows are never touched, so
    the reaper and in-flight status lookups are unaffected; an evicted OLD run just 404s on GET /run/{id}
    (its durable per-canvas history lives in run_records)."""
    with session() as s:
        r = s.get(RunState, run_id)
        st = str(status.get("status", "running"))
        payload = json.dumps(status, default=str)
        if r is None:
            s.add(RunState(run_id=run_id, canvas_id=canvas_id, status=st, doc=payload, kernel_id=kernel_id))
        else:
            r.status = st
            r.doc = payload
            if canvas_id and not r.canvas_id:
                r.canvas_id = canvas_id
            if kernel_id and not r.kernel_id:
                r.kernel_id = kernel_id
        if st in _TERMINAL_RUN:  # once per run at completion (not every transition) → prune finished rows
            s.flush()
            stale = s.scalars(select(RunState.run_id).where(RunState.status.in_(_TERMINAL_RUN))
                              .order_by(RunState.updated_at.desc()).offset(_RUN_STATE_MAX)).all()
            for rid in stale:
                obj = s.get(RunState, rid)
                if obj:
                    s.delete(obj)


def get_run_state(run_id: str) -> dict | None:
    """The last-persisted RunStatus dict for a run, or None if unknown to this instance's DB."""
    with session() as s:
        r = s.get(RunState, run_id)
        return json.loads(r.doc) if r else None


def run_stalled(run_id: str, threshold_s: float) -> bool:
    """True if a run's last status update (run_states.updated_at, bumped on every step transition) is
    older than threshold_s — a soft 'stuck?' hint for a still-running run. A long single step can trip
    it (no step completed recently ≠ dead), so it's advisory; a genuinely dead kernel is caught by the
    heartbeat reaper, not this."""
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None or r.updated_at is None:
            return False
        return _stale_secs(r.updated_at) > threshold_s  # _stale_secs normalizes SQLite's naive datetimes


def save_schema_contract(name: str, columns: list[dict]) -> int:
    """Save a named schema contract as a NEW version (max existing + 1). `columns` = [{name, type}, ...].
    Returns the new version number. The max+1 read-then-insert isn't atomic, so a concurrent save of the
    SAME name can collide on the (name, version) PK — retry a few times (each recomputes the next version)."""
    from sqlalchemy import func
    from sqlalchemy.exc import IntegrityError
    doc = json.dumps([{"name": c["name"], "type": c.get("type", "")} for c in columns])
    for _ in range(5):
        try:
            with session() as s:
                cur = s.query(func.max(SchemaContract.version)).filter(SchemaContract.name == name).scalar()
                version = (cur or 0) + 1
                s.add(SchemaContract(name=name, version=version, doc=doc))
            return version
        except IntegrityError:
            continue  # someone else grabbed this version — recompute and retry
    raise RuntimeError(f"could not save schema contract '{name}' after retries (version contention)")


def get_schema_contract(name: str, version: int | None = None) -> dict | None:
    """A contract by name — the latest version, or a specific one. None if unknown."""
    with session() as s:
        q = select(SchemaContract).where(SchemaContract.name == name)
        q = q.where(SchemaContract.version == version) if version is not None \
            else q.order_by(SchemaContract.version.desc()).limit(1)
        r = s.scalars(q).first()
        return {"name": r.name, "version": r.version, "columns": json.loads(r.doc)} if r else None


def list_schema_contracts() -> list[dict]:
    """Every contract's LATEST version (name/version/columns), for the registry view + a reference picker."""
    with session() as s:
        rows = s.scalars(select(SchemaContract).order_by(SchemaContract.name, SchemaContract.version)).all()
        latest: dict[str, dict] = {}
        for r in rows:  # ordered ascending → the last seen per name is the latest
            latest[r.name] = {"name": r.name, "version": r.version, "columns": json.loads(r.doc)}
        return list(latest.values())


def schema_contract_versions(name: str) -> list[int]:
    with session() as s:
        return sorted(v for (v,) in s.query(SchemaContract.version).filter(SchemaContract.name == name).all())


def diff_columns(a: list[dict], b: list[dict]) -> dict:
    """Structural diff of two column lists (contract vs contract, or contract vs actual). Reports columns
    added / removed / whose type changed, going a → b (b is the newer / actual)."""
    am = {c["name"]: str(c.get("type", "")) for c in a}
    bm = {c["name"]: str(c.get("type", "")) for c in b}
    added = [n for n in bm if n not in am]
    removed = [n for n in am if n not in bm]
    changed = [{"name": n, "from": am[n], "to": bm[n]} for n in am if n in bm and am[n] != bm[n]]
    return {"added": added, "removed": removed, "changed": changed,
            "match": not (added or removed or changed)}


_RESULT_CACHE_MAX = 1000  # persistent equivalent of the old in-process _MAX_RUNS cache cap
_INSTALLATION_ID = 1
_OBJECT_ATTEMPT_KINDS = ("region", "sink")


def get_result(key: str) -> dict | None:
    """The stored result pointer ({uri, table, rows, fmt}) for a plan's content hash, or None."""
    with session() as s:
        r = s.get(ResultCache, key)
        return json.loads(r.doc) if r else None


def _db_now(s) -> datetime.datetime:
    """The database server's transaction clock, normalized for SQLite's string result."""
    value = s.scalar(select(func.now()))
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime.datetime):
        raise RuntimeError("metadata database did not return a timestamp")
    return value


def _lock_object_attempt_registry(s) -> InstallationIdentity:
    """Serialize lifecycle pointer swaps across hub instances, including SQLite's no-op FOR UPDATE."""
    result = s.execute(
        update(InstallationIdentity)
        .where(InstallationIdentity.id == _INSTALLATION_ID)
        .values(owner_token=InstallationIdentity.owner_token)
    )
    if result.rowcount != 1:
        raise RuntimeError("object-attempt installation identity is missing")
    row = s.get(InstallationIdentity, _INSTALLATION_ID, with_for_update=True)
    if row is None:
        raise RuntimeError("object-attempt installation identity is missing")
    return row


def object_attempt_owner_id() -> str:
    """The durable non-secret owner token shared by every hub using this metadata database."""
    with session() as s:
        row = s.get(InstallationIdentity, _INSTALLATION_ID)
        if row is None or not row.owner_token:
            raise RuntimeError("object-attempt installation identity is missing")
        return row.owner_token


def _validate_object_attempt_identity(row: ObjectAttempt, *, logical_uri: str, kind: str,
                                      run_id: str) -> None:
    if (row.logical_uri, row.kind, row.run_id) != (logical_uri, kind, run_id):
        raise RuntimeError("object attempt URI is already claimed by a different logical write")


def claim_object_attempt(uri: str, logical_uri: str, kind: str, run_id: str) -> None:
    """Idempotently register one immutable object attempt before its first shard is written."""
    uri, logical_uri, run_id = str(uri).rstrip("/"), str(logical_uri).rstrip("/"), str(run_id)
    if not uri or not logical_uri or not run_id or kind not in _OBJECT_ATTEMPT_KINDS:
        raise ValueError("object attempt claim requires URI, logical URI, run ID, and region/sink kind")
    with session() as s:
        _lock_object_attempt_registry(s)
        row = s.get(ObjectAttempt, uri, with_for_update=True)
        if row is not None:
            _validate_object_attempt_identity(row, logical_uri=logical_uri, kind=kind, run_id=run_id)
            if row.state in ("retiring", "retired", "discarding"):
                raise RuntimeError(f"cannot reclaim immutable object attempt in state {row.state!r}")
            return
        s.add(ObjectAttempt(uri=uri, logical_uri=logical_uri, kind=kind, run_id=run_id, state="writing"))


def _publish_object_attempt_in_session(s, row: ObjectAttempt, now: datetime.datetime,
                                       reference_key: str | None = None) -> list[str]:
    if row.state in ("retiring", "retired", "discarding"):
        raise RuntimeError(f"cannot publish object attempt in state {row.state!r}")
    if row.state == "writing":
        row.state, row.published_at = "published", now
    elif row.published_at is None:
        row.published_at = now
    if reference_key is not None:
        row.reference_key = str(reference_key)
    s.flush()
    retired: list[str] = []
    if row.kind == "sink":
        prior = list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.kind == "sink",
                ObjectAttempt.logical_uri == row.logical_uri,
                ObjectAttempt.state == "published",
                ObjectAttempt.uri != row.uri,
            ).order_by(ObjectAttempt.published_at.asc(), ObjectAttempt.uri.asc()).with_for_update()
        ))
        for old in prior:
            old.state, old.gc_attempted_at = "retiring", None
            retired.append(old.uri)
    return retired


def publish_object_attempt(uri: str, reference_key: str | None = None) -> list[str]:
    """Publish one attempt and atomically fence superseded siblings of a logical sink."""
    uri = str(uri).rstrip("/")
    with session() as s:
        _lock_object_attempt_registry(s)
        now = _db_now(s)
        row = s.get(ObjectAttempt, uri, with_for_update=True)
        if row is None:
            raise KeyError(uri)
        return _publish_object_attempt_in_session(s, row, now, reference_key)


def object_attempt_catalog_prior(uri: str) -> dict | None:
    """Catalog identity/organization of the current published version of this sink target."""
    uri = str(uri).rstrip("/")
    with session() as s:
        row = s.get(ObjectAttempt, uri)
        if row is None or row.kind != "sink":
            return None
        prior = s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.kind == "sink",
                ObjectAttempt.logical_uri == row.logical_uri,
                ObjectAttempt.state == "published",
                ObjectAttempt.uri != uri,
            ).order_by(ObjectAttempt.published_at.desc(), ObjectAttempt.uri.desc()).limit(1)
        ).first()
        if prior is None:
            return None
        entry = s.get(CatalogEntry, prior.uri)
        return _row_to_doc(entry, _tags_for(s, [prior.uri]).get(prior.uri, [])) if entry else None


def retire_object_attempts(uris: list[str]) -> list[str]:
    """Fence exact published attempts from new readers; physical retirement happens out of transaction."""
    ordered = list(dict.fromkeys(str(uri).rstrip("/") for uri in uris if str(uri).strip()))
    if not ordered:
        return []
    retired: list[str] = []
    with session() as s:
        _lock_object_attempt_registry(s)
        rows = {row.uri: row for row in s.scalars(
            select(ObjectAttempt).where(ObjectAttempt.uri.in_(ordered)).with_for_update()
        )}
        for uri in ordered:
            row = rows.get(uri)
            if row is not None and row.state == "published":
                row.state = "retiring"
                row.gc_attempted_at = None
                retired.append(uri)
    return retired


def mark_object_attempt_retired(uri: str) -> None:
    """Acknowledge that catalog/cache admission and the commit marker have been retired."""
    uri = str(uri).rstrip("/")
    with session() as s:
        _lock_object_attempt_registry(s)
        row = s.get(ObjectAttempt, uri, with_for_update=True)
        if row is None:
            return
        if row.state == "retired":
            return
        if row.state != "retiring":
            raise RuntimeError(f"cannot mark object attempt retired from state {row.state!r}")
        row.state, row.retired_at, row.gc_attempted_at = "retired", _db_now(s), None


def begin_discard_object_attempt(uri: str) -> bool:
    """Fence one unpublished attempt before physical deletion.

    Registered attempts must atomically move from ``writing`` to ``discarding``; publication refuses
    that state. Missing object attempts are not safe to delete because there is no durable tombstone to
    stop a concurrent claimant; legacy prefixes remain provider-lifecycle work.
    """
    uri = str(uri).rstrip("/")
    with session() as s:
        _lock_object_attempt_registry(s)
        row = s.get(ObjectAttempt, uri, with_for_update=True)
        if row is None:
            return False
        if row.state == "discarding":
            return True
        if row.state != "writing":
            return False
        row.state, row.gc_attempted_at = "discarding", None
        return True


def delete_object_attempt(uri: str) -> None:
    """Forget an exact attempt after its fenced-discard or grace-expired objects were removed."""
    uri = str(uri).rstrip("/")
    with session() as s:
        _lock_object_attempt_registry(s)
        row = s.get(ObjectAttempt, uri, with_for_update=True)
        if row is None:
            return
        if row.state not in ("discarding", "retired"):
            raise RuntimeError(f"cannot delete object attempt in state {row.state!r}")
        s.delete(row)


def _gc_seconds(value: float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def _object_attempt_action(row: ObjectAttempt, action: str) -> dict:
    return {
        "action": action,
        "uri": row.uri,
        "logical_uri": row.logical_uri,
        "kind": row.kind,
        "run_id": row.run_id,
        "reference_key": row.reference_key,
    }


def object_attempt_gc_batch(retention_seconds: float, grace_seconds: float,
                            limit: int = 100) -> list[dict]:
    """Select one bounded, transactionally ordered batch of safe object-lifecycle actions.

    The caller performs storage/catalog I/O after this short transaction, then acknowledges ``retire``
    with ``mark_object_attempt_retired`` and ``discard``/``delete`` with ``delete_object_attempt``.
    Actions are idempotent, so another hub selecting the same pending row is harmless.
    """
    # Retained as a validated compatibility knob. Age is not proof that an independent driver or a
    # durable Ray Job stopped writing, so unpublished attempts are never selected from it.
    _gc_seconds(retention_seconds, "retention_seconds")
    grace = _gc_seconds(grace_seconds, "grace_seconds")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("object attempt GC limit must be a positive integer")
    actions: list[dict] = []
    with session() as s:
        _lock_object_attempt_registry(s)
        now = _db_now(s)

        def remaining() -> int:
            return limit - len(actions)

        retry_cutoff = now - datetime.timedelta(seconds=60)
        claimable = or_(ObjectAttempt.gc_attempted_at.is_(None),
                        ObjectAttempt.gc_attempted_at <= retry_cutoff)

        retiring = list(s.scalars(
            select(ObjectAttempt).where(ObjectAttempt.state == "retiring", claimable)
            .order_by(ObjectAttempt.created_at.asc(), ObjectAttempt.uri.asc())
            .limit(remaining()).with_for_update()
        ))
        for row in retiring:
            row.gc_attempted_at = now
        actions.extend(_object_attempt_action(row, "retire") for row in retiring)
        if remaining() <= 0:
            return actions

        retired_cutoff = now - datetime.timedelta(seconds=grace)
        expired = list(s.scalars(
            select(ObjectAttempt).where(
                ObjectAttempt.state == "retired",
                ObjectAttempt.retired_at.is_not(None),
                ObjectAttempt.retired_at <= retired_cutoff,
                claimable,
            ).order_by(ObjectAttempt.retired_at.asc(), ObjectAttempt.uri.asc())
            .limit(remaining()).with_for_update()
        ))
        for row in expired:
            row.gc_attempted_at = now
        actions.extend(_object_attempt_action(row, "delete") for row in expired)
        if remaining() <= 0:
            return actions

        discarding = list(s.scalars(
            select(ObjectAttempt).where(ObjectAttempt.state == "discarding", claimable)
            .order_by(ObjectAttempt.created_at.asc(), ObjectAttempt.uri.asc())
            .limit(remaining()).with_for_update()
        ))
        for row in discarding:
            row.gc_attempted_at = now
        actions.extend(_object_attempt_action(row, "discard") for row in discarding)
    return actions


def _result_doc_uri(raw: str | dict | None) -> str | None:
    try:
        doc = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    uri = doc.get("uri") if isinstance(doc, dict) else None
    return str(uri).rstrip("/") if uri else None


def put_result(key: str, doc: dict) -> list[str]:
    """Atomically publish a region result pointer and return exact attempt URIs it superseded/evicted."""
    payload = json.dumps(doc, default=str)
    new_uri = _result_doc_uri(doc)
    old_refs: list[tuple[str, str]] = []
    retired: list[str] = []
    with session() as s:
        _lock_object_attempt_registry(s)
        now = _db_now(s)
        row = s.get(ResultCache, key, with_for_update=True)
        if row is None:
            s.add(ResultCache(key=key, doc=payload, created_at=now))
        else:
            old_uri = _result_doc_uri(row.doc)
            if old_uri and old_uri != new_uri:
                old_refs.append((old_uri, key))
            row.doc, row.created_at = payload, now

        if new_uri:
            attempt = s.get(ObjectAttempt, new_uri, with_for_update=True)
            if attempt is not None and attempt.kind == "region":
                if attempt.state in ("retiring", "retired", "discarding"):
                    raise RuntimeError(f"cannot publish region attempt in state {attempt.state!r}")
                if attempt.state == "writing":
                    attempt.state, attempt.published_at = "published", now
                elif attempt.published_at is None:
                    attempt.published_at = now
                attempt.reference_key = key

        s.flush()
        stale = list(s.scalars(
            select(ResultCache).order_by(ResultCache.created_at.desc(), ResultCache.key.desc())
            .offset(_RESULT_CACHE_MAX).with_for_update()
        ))
        for stale_row in stale:
            old_uri = _result_doc_uri(stale_row.doc)
            if old_uri and old_uri != new_uri:
                old_refs.append((old_uri, stale_row.key))
            s.delete(stale_row)

        for old_uri, reference_key in old_refs:
            if old_uri == new_uri or old_uri in retired:
                continue
            attempt = s.get(ObjectAttempt, old_uri, with_for_update=True)
            if (attempt is not None and attempt.kind == "region" and attempt.state == "published"
                    and attempt.reference_key == reference_key):
                attempt.state = "retiring"
                attempt.gc_attempted_at = None
                retired.append(old_uri)
    return retired


def _doc_org(doc: dict) -> tuple[str, str, str | None, str | None, int | None, list[str], list[str]]:
    """Pull the projectable/indexable fields out of a CatalogTable doc: (tbl_id, folder, owner,
    description, row_count, tags, column-names). The doc stays authoritative; these are its mirror."""
    tbl_id = doc.get("id")
    folder = (doc.get("folder") or "").strip("/")
    owner = doc.get("owner") or None
    description = doc.get("description") or None
    rows = doc.get("rowCount")
    if rows is None:
        rows = doc.get("row_count")
    tags = [str(t) for t in (doc.get("tags") or []) if str(t).strip()]
    cols = [c.get("name") for c in (doc.get("columns") or []) if isinstance(c, dict) and c.get("name")]
    return tbl_id, folder, owner, description, rows, tags, cols


def _sync_children(s, uri: str, tags: list[str], cols: list[str]) -> None:
    """Replace a uri's tag + column rows (the indexable projections) in one transaction."""
    for r in s.scalars(select(CatalogTag).where(CatalogTag.uri == uri)):
        s.delete(r)
    for r in s.scalars(select(CatalogColumn).where(CatalogColumn.uri == uri)):
        s.delete(r)
    s.flush()
    for t in dict.fromkeys(tags):  # de-dupe, preserve order
        s.add(CatalogTag(uri=uri, tag=t))
    for c in dict.fromkeys(cols):
        s.add(CatalogColumn(uri=uri, column=c))


def catalog_upsert_entry(uri: str, name: str, doc: dict) -> None:
    """Write-through a catalog entry (registered dataset / written output) to the shared DB, keyed by
    uri, so other instances + a restart see it. `doc` is the full CatalogTable model_dump; its folder /
    owner / description / row_count / tags / column-names are mirrored to indexed columns + join tables
    so browse/search/facet push down to the DB. `usage` (popularity) is owned by the column and NOT
    overwritten from the doc — it's bumped independently on reads."""
    with session() as s:
        attempt = s.get(ObjectAttempt, uri)
        if attempt is not None and attempt.kind == "sink":
            _lock_object_attempt_registry(s)
            attempt = s.get(ObjectAttempt, uri, with_for_update=True)
            if attempt is None or attempt.kind != "sink":
                raise RuntimeError("object sink attempt disappeared during catalog publication")
        tbl_id, folder, owner, description, rows, tags, cols = _doc_org(doc)
        r = s.get(CatalogEntry, uri)
        payload = json.dumps(doc, default=str)
        if r is None:
            s.add(CatalogEntry(uri=uri, name=name, doc=payload, tbl_id=tbl_id, folder=folder,
                               owner=owner, description=description, row_count=rows))
        else:
            r.name, r.doc, r.tbl_id = name, payload, tbl_id
            r.folder, r.owner, r.description, r.row_count = folder, owner, description, rows
        _sync_children(s, uri, tags, cols)
        if attempt is not None:
            retired = _publish_object_attempt_in_session(s, attempt, _db_now(s))
            if retired:
                _delete_catalog_children(s, retired)
                for old_uri in retired:
                    old = s.get(CatalogEntry, old_uri)
                    if old is not None:
                        s.delete(old)


def catalog_set_metadata(uri: str, folder: str, owner: str | None, description: str | None,
                         tags: list[str]) -> None:
    """Update ONLY the organization fields of an entry (folder/owner/description/tags) — both the
    indexed columns AND the mirrored fields inside the stored doc, so a re-read is consistent without
    re-probing the dataset. No-op if the uri isn't registered."""
    with session() as s:
        r = s.get(CatalogEntry, uri)
        if r is None:
            return
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            doc = {}
        doc["folder"], doc["owner"], doc["description"], doc["tags"] = folder, owner, description, list(tags)
        r.folder, r.owner, r.description, r.doc = folder, owner, description, json.dumps(doc, default=str)
        cols = [c.get("name") for c in doc.get("columns", []) if isinstance(c, dict) and c.get("name")]
        _sync_children(s, uri, tags, cols)


def catalog_bump_usage(uri: str, n: int = 1) -> None:
    """Increment a dataset's read-count popularity (called best-effort when it's sampled / read in a
    run). An atomic `usage = usage + n` (concurrent bumps can't lose increments) that explicitly
    carries updated_at, so a READ never masquerades as an update in the 'Recently updated' sort."""
    with session() as s:
        s.execute(update(CatalogEntry).where(CatalogEntry.uri == uri)
                  .values(usage=CatalogEntry.usage + n, updated_at=CatalogEntry.updated_at))


def catalog_add_edge(parent: str, child: str, pipeline: str | None = None, column: str | None = None) -> None:
    """Write-through a lineage edge; one row per (parent, child). `column` records column-level
    provenance when known."""
    if parent == child:
        return
    with session() as s:
        exists = s.scalars(select(CatalogEdge).where(CatalogEdge.parent == parent, CatalogEdge.child == child)).first()
        if exists is None:
            s.add(CatalogEdge(parent=parent, child=child, pipeline=pipeline, column=column))
        elif column and not exists.column:
            exists.column = column


def _row_to_doc(r: "CatalogEntry", tags: list[str]) -> dict:
    """Materialize a CatalogTable-shaped dict from a row, overlaying the authoritative indexed
    columns (id/folder/owner/description/usage) + the tag rows onto the stored doc."""
    try:
        d = json.loads(r.doc)
    except (ValueError, TypeError):
        d = {"id": r.tbl_id or f"tbl_{r.name}", "name": r.name, "uri": r.uri}
    d["id"] = r.tbl_id or d.get("id") or f"tbl_{r.name}"
    d["folder"] = r.folder or ""
    d["owner"] = r.owner
    d["description"] = r.description
    d["usage"] = r.usage or 0
    d["tags"] = tags
    return d


def _tags_for(s, uris: list[str]) -> dict[str, list[str]]:
    """{uri: [tag, ...]} for a batch of uris — one query, so a page of N rows costs one round-trip."""
    out: dict[str, list[str]] = {u: [] for u in uris}
    if not uris:
        return out
    for row in s.scalars(select(CatalogTag).where(CatalogTag.uri.in_(uris))):
        out.setdefault(row.uri, []).append(row.tag)
    return out


def _like_escape(s: str) -> str:
    """Escape LIKE metacharacters in user input so a literal % / _ matches itself (used with ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _catalog_filters(q: str | None, folder: str | None, tags: list[str] | None,
                     owner: str | None, has_columns: list[str] | None,
                     uris: list[str] | None = None) -> list:
    """The WHERE terms shared by catalog_query + catalog_facets, so a page and its facet counts always
    describe the SAME filtered set."""
    from sqlalchemy import exists
    terms: list = []
    # every whitespace token must match SOMEWHERE (name/uri/folder/description/column/tag), so
    # "curated images" finds demo/images/curated even though the words never appear adjacent
    for token in (q or "").lower().split():
        like = f"%{_like_escape(token)}%"
        terms.append(or_(
            func.lower(CatalogEntry.name).like(like, escape="\\"),
            func.lower(CatalogEntry.uri).like(like, escape="\\"),
            func.lower(CatalogEntry.folder).like(like, escape="\\"),
            func.lower(func.coalesce(CatalogEntry.description, "")).like(like, escape="\\"),
            exists().where(CatalogColumn.uri == CatalogEntry.uri,
                           func.lower(CatalogColumn.column).like(like, escape="\\")),
            exists().where(CatalogTag.uri == CatalogEntry.uri,
                           func.lower(CatalogTag.tag).like(like, escape="\\")),
        ))
    if uris:
        terms.append(CatalogEntry.uri.in_(list(uris)))
    if folder:
        f = folder.strip("/")
        terms.append(or_(CatalogEntry.folder == f,
                         CatalogEntry.folder.like(_like_escape(f) + "/%", escape="\\")))
    for t in (tags or []):
        terms.append(exists().where(CatalogTag.uri == CatalogEntry.uri, CatalogTag.tag == t))
    if owner:
        terms.append(CatalogEntry.owner == owner)
    for c in (has_columns or []):
        terms.append(exists().where(CatalogColumn.uri == CatalogEntry.uri, CatalogColumn.column == c))
    return terms


_SORT_COLS = {"name": CatalogEntry.name, "rows": CatalogEntry.row_count,
              "updated": CatalogEntry.updated_at, "usage": CatalogEntry.usage,
              "folder": CatalogEntry.folder}


def catalog_query(q: str | None = None, folder: str | None = None, tags: list[str] | None = None,
                  owner: str | None = None, has_columns: list[str] | None = None,
                  uris: list[str] | None = None,
                  sort: str = "name", order: str = "asc", limit: int = 50, offset: int = 0,
                  ) -> tuple[list[dict], int]:
    """A filtered, sorted, paginated window over the catalog PUSHED DOWN to the DB (indexed columns +
    EXISTS subqueries), plus the total match count. Returns (docs, total). This is the scalable core:
    memory + wire cost are bounded by `limit`, not by the catalog size."""
    terms = _catalog_filters(q, folder, tags, owner, has_columns, uris)
    col = _SORT_COLS.get(sort, CatalogEntry.name)
    with session() as s:
        total = s.scalar(select(func.count()).select_from(CatalogEntry).where(*terms)) or 0
        stmt = select(CatalogEntry).where(*terms)
        primary = col.desc() if order == "desc" else col.asc()
        # a stable tiebreak (name, then uri) keeps paging deterministic when many rows share a sort key
        stmt = stmt.order_by(primary, CatalogEntry.name.asc(), CatalogEntry.uri.asc())
        rows = list(s.scalars(stmt.limit(max(0, limit)).offset(max(0, offset))))
        tag_map = _tags_for(s, [r.uri for r in rows])
        docs = [_row_to_doc(r, tag_map.get(r.uri, [])) for r in rows]
    return docs, int(total)


def catalog_filter_uris(folder: str | None = None, tags: list[str] | None = None,
                        owner: str | None = None, has_columns: list[str] | None = None,
                        uris: list[str] | None = None) -> set[str] | None:
    """URIs allowed by structured catalog filters, or ``None`` when there are no such filters.

    Semantic search already scores the complete embedding matrix.  Restricting that matrix with one
    URI-only DB query keeps ranking exact without loading catalog documents or applying filters after
    the top-k cutoff (which can incorrectly return too few matches).
    """
    if not (folder or tags or owner or has_columns or uris):
        return None
    terms = _catalog_filters(None, folder, tags, owner, has_columns, uris)
    with session() as s:
        return set(s.scalars(select(CatalogEntry.uri).where(*terms)))


def catalog_facets(q: str | None = None, folder: str | None = None, tags: list[str] | None = None,
                   owner: str | None = None, has_columns: list[str] | None = None, top: int = 100,
                   ) -> dict[str, list[tuple[str, int]]]:
    """Distinct values + counts for the folder / tag / owner dimensions over the ACTIVE filter set
    (drill-down semantics). Each list is capped to the `top` most common. Powers the facet rail."""
    terms = _catalog_filters(q, folder, tags, owner, has_columns)
    with session() as s:
        folders = [(v or "", int(c)) for v, c in s.execute(
            select(CatalogEntry.folder, func.count()).where(*terms, CatalogEntry.folder != "")
            .group_by(CatalogEntry.folder).order_by(func.count().desc()).limit(top)).all()]
        owners = [(v, int(c)) for v, c in s.execute(
            select(CatalogEntry.owner, func.count()).where(*terms, CatalogEntry.owner.is_not(None))
            .group_by(CatalogEntry.owner).order_by(func.count().desc()).limit(top)).all()]
        # tags live in a join table → count distinct entries per tag within the filtered set
        uri_sq = select(CatalogEntry.uri).where(*terms).subquery()
        tag_rows = s.execute(
            select(CatalogTag.tag, func.count()).where(CatalogTag.uri.in_(select(uri_sq.c.uri)))
            .group_by(CatalogTag.tag).order_by(func.count().desc()).limit(top)).all()
        tag_counts = [(v, int(c)) for v, c in tag_rows]
    return {"folders": folders, "tags": tag_counts, "owners": owners}


def catalog_tree(prefix: str = "", table_limit: int = 100
                 ) -> tuple[list[tuple[str, str, int]], list[dict], int]:
    """One level of the browse tree at `prefix`: (child_folders, direct_tables, direct_total).
    child_folders is a list of (name, path, subtree_table_count) for the immediate sub-folders;
    direct_tables are the first `table_limit` tables filed exactly at `prefix` (direct_total says how
    many exist, so a caller can signal truncation). Folder aggregation is over the (small) set of
    distinct folders, so this scales with the number of FOLDERS, not tables."""
    p = (prefix or "").strip("/")
    depth = 0 if not p else p.count("/") + 1
    with session() as s:
        folder_counts = s.execute(
            select(CatalogEntry.folder, func.count()).where(CatalogEntry.folder != "")
            .group_by(CatalogEntry.folder)).all()
        children: dict[str, int] = {}
        for folder, cnt in folder_counts:
            f = (folder or "").strip("/")
            if p:
                if f != p and not f.startswith(p + "/"):
                    continue
            segs = f.split("/")
            if len(segs) <= depth:
                continue  # this folder is AT the prefix (no deeper child segment)
            child_path = "/".join(segs[: depth + 1])
            children[child_path] = children.get(child_path, 0) + int(cnt)
        child_list = sorted(
            ((cp.split("/")[-1], cp, n) for cp, n in children.items()), key=lambda x: x[0].lower())
        direct_total = int(s.scalar(select(func.count()).select_from(CatalogEntry)
                                    .where(CatalogEntry.folder == p)) or 0)
        direct_rows = list(s.scalars(
            select(CatalogEntry).where(CatalogEntry.folder == p).order_by(CatalogEntry.name).limit(table_limit)))
        tag_map = _tags_for(s, [r.uri for r in direct_rows])
        tables = [_row_to_doc(r, tag_map.get(r.uri, [])) for r in direct_rows]
    return child_list, tables, direct_total


def catalog_get(token: str) -> dict | None:
    """A single entry by uri (PK), then by tbl_id, then by name — all indexed. None if unknown.
    Replaces the old 'load the whole catalog then look it up' path, so get_table is O(1), not O(n)."""
    with session() as s:
        r = s.get(CatalogEntry, token)
        if r is None:
            r = s.scalars(select(CatalogEntry).where(CatalogEntry.tbl_id == token).limit(1)).first()
        if r is None:
            r = s.scalars(select(CatalogEntry).where(CatalogEntry.name == token).limit(1)).first()
        if r is None:
            return None
        return _row_to_doc(r, [t.tag for t in s.scalars(select(CatalogTag).where(CatalogTag.uri == r.uri))])


def catalog_get_many(uris: list[str]) -> dict[str, dict]:
    """{uri: doc} for a batch of uris — used to name lineage-graph nodes in one round-trip."""
    if not uris:
        return {}
    with session() as s:
        rows = list(s.scalars(select(CatalogEntry).where(CatalogEntry.uri.in_(uris))))
        tag_map = _tags_for(s, [r.uri for r in rows])
        return {r.uri: _row_to_doc(r, tag_map.get(r.uri, [])) for r in rows}


def catalog_entries() -> list[dict]:
    """Every persisted catalog entry, as CatalogTable-shaped dicts. Retained for tooling/back-compat;
    the browse path uses catalog_query (bounded), not this (unbounded) scan."""
    with session() as s:
        rows = list(s.scalars(select(CatalogEntry)))
        tag_map = _tags_for(s, [r.uri for r in rows])
        return [_row_to_doc(r, tag_map.get(r.uri, [])) for r in rows]


def _delete_catalog_children(s, uris: list[str]) -> None:
    """Remove EVERY row keyed to `uris` alongside the entries themselves — tags/columns/embeddings,
    lineage edges (either endpoint), declared keys, and relationships. Otherwise a deleted table
    haunts lineage/ER as a ghost node, and a NEW dataset re-registered at the same uri silently
    inherits the old declared key + parents."""
    for model in (CatalogTag, CatalogColumn, CatalogEmbedding, CatalogDeclaredKey):
        for r in s.scalars(select(model).where(model.uri.in_(uris))):
            s.delete(r)
    for r in s.scalars(select(CatalogEdge).where(
            or_(CatalogEdge.parent.in_(uris), CatalogEdge.child.in_(uris)))):
        s.delete(r)
    # relationship endpoints live inside the JSON doc — relationships are curated (small), so scan them
    gone = set(uris)
    for r in s.scalars(select(CatalogRelationship)):
        try:
            doc = json.loads(r.doc)
        except (ValueError, TypeError):
            continue
        if doc.get("leftUri") in gone or doc.get("rightUri") in gone \
                or doc.get("left_uri") in gone or doc.get("right_uri") in gone:
            s.delete(r)


def catalog_delete_entry(uri: str) -> None:
    """Remove a catalog entry (unregister) + everything keyed to it (tags/columns/embedding/edges/
    declared key/relationships)."""
    with session() as s:
        _delete_catalog_children(s, [uri])
        r = s.get(CatalogEntry, uri)
        if r is not None:
            s.delete(r)


def catalog_delete_prefix(uri_prefix: str) -> int:
    """Delete every entry (+ everything keyed to it) whose uri starts with `uri_prefix`. Returns the
    count removed. For bulk teardown of demo/scale entries; a no-op for a prefix that matches none."""
    like = _like_escape(uri_prefix) + "%"
    with session() as s:
        uris = [u for (u,) in s.execute(select(CatalogEntry.uri).where(
            CatalogEntry.uri.like(like, escape="\\"))).all()]
        if not uris:
            return 0
        _delete_catalog_children(s, uris)
        for r in s.scalars(select(CatalogEntry).where(CatalogEntry.uri.in_(uris))):
            s.delete(r)
    return len(uris)


def catalog_edges() -> list[dict]:
    with session() as s:
        return [{"parent": r.parent, "child": r.child, "column": r.column, "pipeline": r.pipeline}
                for r in s.scalars(select(CatalogEdge))]


def catalog_edges_touching(uris: list[str], limit: int | None = None) -> list[dict]:
    """Every edge with an endpoint in `uris` — the frontier expansion step of a bounded lineage BFS
    (so lineage never loads the whole edge table). `limit` caps a pathologically-connected frontier
    (a hub node with 100k children) so one expansion can't load unbounded rows; the caller treats a
    full batch as truncation."""
    if not uris:
        return []
    with session() as s:
        stmt = select(CatalogEdge).where(
            or_(CatalogEdge.parent.in_(uris), CatalogEdge.child.in_(uris)))
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = s.scalars(stmt)
        return [{"parent": r.parent, "child": r.child, "column": r.column, "pipeline": r.pipeline} for r in rows]


def catalog_edges_page(limit: int = 500, offset: int = 0) -> tuple[list[dict], int]:
    """One page of the whole lineage edge set + the total count — the bulk-export surface an external
    lineage store (e.g. an OpenLineage bridge plugin) syncs from."""
    with session() as s:
        total = s.scalar(select(func.count()).select_from(CatalogEdge)) or 0
        rows = s.scalars(select(CatalogEdge).order_by(CatalogEdge.id.asc())
                         .limit(max(0, limit)).offset(max(0, offset)))
        return ([{"parent": r.parent, "child": r.child, "column": r.column, "pipeline": r.pipeline}
                 for r in rows], int(total))


# -- semantic search (opt-in: only populated when an embedder is registered) ----------------------- #
def catalog_set_embedding(uri: str, model: str, dim: int, vec: bytes) -> None:
    with session() as s:
        r = s.get(CatalogEmbedding, uri)
        if r is None:
            s.add(CatalogEmbedding(uri=uri, model=model, dim=dim, vec=vec))
        else:
            r.model, r.dim, r.vec = model, dim, vec


def catalog_embeddings_for(model: str) -> list[tuple[str, bytes]]:
    """(uri, vec-bytes) for every embedding under `model` — the candidate set semantic search scores."""
    with session() as s:
        return [(r.uri, r.vec) for r in s.scalars(select(CatalogEmbedding).where(CatalogEmbedding.model == model))]


def catalog_bulk_seed(entries: list[dict]) -> int:
    """Register many synthetic/pre-built entries in one transaction (skipping uris already present) —
    for demos + the scale acceptance test. Each entry: {uri, name, doc, folder?, tags?, owner?,
    description?, rowCount?}. Returns how many were inserted."""
    n = 0
    with session() as s:
        existing = {u for (u,) in s.execute(select(CatalogEntry.uri).where(
            CatalogEntry.uri.in_([e["uri"] for e in entries]))).all()}
        for e in entries:
            uri = e["uri"]
            if uri in existing:
                continue
            doc = e.get("doc") or {}
            tbl_id, folder, owner, description, rows, tags, cols = _doc_org(doc)
            s.add(CatalogEntry(uri=uri, name=e["name"], doc=json.dumps(doc, default=str), tbl_id=tbl_id,
                               folder=folder, owner=owner, description=description, row_count=rows))
            for t in dict.fromkeys(tags):
                s.add(CatalogTag(uri=uri, tag=t))
            for c in dict.fromkeys(cols):
                s.add(CatalogColumn(uri=uri, column=c))
            n += 1
    return n


def catalog_relationships() -> list[dict]:
    """Every declared relationship as a Relationship-shaped dict."""
    with session() as s:
        return [json.loads(r.doc) for r in s.scalars(select(CatalogRelationship))]


def catalog_upsert_relationship(rel_key: str, doc: dict) -> None:
    """Insert or replace ONE relationship row (keyed by rel_key) — no read-modify-write of a shared
    blob, so a concurrent declare of a DIFFERENT relationship on another instance can't be lost."""
    with session() as s:
        r = s.get(CatalogRelationship, rel_key)
        payload = json.dumps(doc, default=str)
        if r is None:
            s.add(CatalogRelationship(rel_key=rel_key, doc=payload))
        else:
            r.doc = payload


def catalog_delete_relationship(rel_key: str) -> None:
    with session() as s:
        r = s.get(CatalogRelationship, rel_key)
        if r is not None:
            s.delete(r)


def catalog_declared_keys(uris: list[str] | None = None) -> dict[str, list]:
    """{uri: [column, ...]} for the declared primary keys of `uris` (an indexed PK batch lookup — the
    read path passes the page's uris so this stays O(page), never O(catalog)). None → all keys."""
    with session() as s:
        stmt = select(CatalogDeclaredKey)
        if uris is not None:
            if not uris:
                return {}
            stmt = stmt.where(CatalogDeclaredKey.uri.in_(uris))
        return {r.uri: json.loads(r.columns) for r in s.scalars(stmt)}


def catalog_set_declared_key(uri: str, columns: list) -> None:
    """Set (columns non-empty) or clear (empty) ONE dataset's declared key — a single row, so it
    can't clobber another dataset's key set concurrently on another instance."""
    with session() as s:
        r = s.get(CatalogDeclaredKey, uri)
        if columns:
            payload = json.dumps(list(columns))
            if r is None:
                s.add(CatalogDeclaredKey(uri=uri, columns=payload))
            else:
                r.columns = payload
        elif r is not None:
            s.delete(r)


# --------------------------------------------------------------------------- #
# Kernels — the per-canvas execution-kernel lease (Phase 1 of the session kernel)
# --------------------------------------------------------------------------- #
KERNEL_STALE_S = 30  # a kernel whose heartbeat is older than this is presumed dead


def _stale_secs(dt: "datetime.datetime | None") -> float:
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:  # SQLite reads DateTime back naive — treat stored times as UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (_now() - dt).total_seconds()


def _kernel_stale(r: "Kernel") -> bool:
    return _stale_secs(r.heartbeat_at) >= KERNEL_STALE_S


def claim_kernel(canvas_id: str, kernel_id: str, token: str) -> dict:
    """Atomically claim the right to spawn THE kernel for a canvas — the single-spawner guard the
    no-split-brain invariant rests on. Returns {won, endpoint, state, kernel_id}: won=True → this
    caller holds the lease under `kernel_id` and should spawn; won=False → a live kernel already
    exists, use its `endpoint`. A stale/dead lease is taken over (rebinding fences the old kernel_id)."""
    now = _now()
    with session() as s:
        # row-lock the read so a concurrent stale-lease takeover serializes: the second claimer blocks,
        # then re-reads the freshly-rebound (no-longer-stale) row and loses (won=False). Without it two
        # hubs could both read the same stale row, both pass the staleness check, and both spawn.
        # (SELECT … FOR UPDATE on Postgres — the prod DB; a no-op on single-process SQLite dev.)
        r = s.get(Kernel, canvas_id, with_for_update=True)
        if r is not None:
            if not _kernel_stale(r):
                return {"won": False, "endpoint": r.endpoint, "state": r.state, "kernel_id": r.kernel_id}
            r.kernel_id, r.token, r.state = kernel_id, token, "starting"  # take over (fences the old id)
            r.endpoint, r.heartbeat_at, r.started_at = None, now, now
            return {"won": True, "endpoint": None, "state": "starting", "kernel_id": kernel_id}
        s.add(Kernel(canvas_id=canvas_id, kernel_id=kernel_id, token=token, state="starting",
                     heartbeat_at=now, started_at=now))
        try:
            s.flush()  # a concurrent creator makes the PK insert fail → we lost the race
        except IntegrityError:
            s.rollback()
            r = s.get(Kernel, canvas_id)
            return {"won": False, "endpoint": r.endpoint if r else None,
                    "state": r.state if r else "starting", "kernel_id": r.kernel_id if r else ""}
        return {"won": True, "endpoint": None, "state": "starting", "kernel_id": kernel_id}


def _fenced(s, canvas_id: str, kernel_id: str) -> "Kernel | None":
    """The lease row IFF it's still ours — a kernel fenced out (replaced by a newer one) sees None."""
    r = s.get(Kernel, canvas_id)
    return r if (r is not None and r.kernel_id == kernel_id) else None


def mark_kernel_ready(canvas_id: str, kernel_id: str, endpoint: str) -> bool:
    with session() as s:
        r = _fenced(s, canvas_id, kernel_id)
        if r is None:
            return False
        r.state, r.endpoint, r.heartbeat_at = "ready", endpoint, _now()
        return True


def heartbeat_kernel(canvas_id: str, kernel_id: str) -> bool:
    """Touch the lease. False if we've been fenced out (a newer kernel took over) → the kernel exits."""
    with session() as s:
        r = _fenced(s, canvas_id, kernel_id)
        if r is None:
            return False
        r.heartbeat_at = _now()
        return True


def drop_kernel(canvas_id: str, kernel_id: str) -> None:
    """Release our lease (idle-exit / explicit shutdown). Fenced: a zombie can't delete the new owner."""
    with session() as s:
        r = _fenced(s, canvas_id, kernel_id)
        if r is not None:
            s.delete(r)


def get_kernel(canvas_id: str) -> dict | None:
    with session() as s:
        r = s.get(Kernel, canvas_id)
        if r is None:
            return None
        return {"canvas_id": r.canvas_id, "kernel_id": r.kernel_id, "endpoint": r.endpoint,
                "token": r.token, "state": r.state, "stale": _kernel_stale(r)}


def active_runs(canvas_id: str) -> list[dict]:
    """In-flight runs (queued/running) for a canvas — so a reopened canvas re-subscribes to a run that
    outlived a hub restart (its kernel kept it alive). Returns each run's last-known RunStatus dict."""
    out = []
    with session() as s:
        for r in s.scalars(select(RunState).where(RunState.canvas_id == canvas_id,
                                                   RunState.status.in_(("queued", "running")))):
            try:
                out.append(json.loads(r.doc))
            except Exception:  # noqa: BLE001
                out.append({"run_id": r.run_id, "status": r.status})
    return out


def kernel_for_run(run_id: str) -> dict | None:
    """The kernel that OWNS a run (endpoint + token) — for routing cancel to it. None if the run's owning
    kernel no longer holds the canvas lease (fenced / replaced / gone): routing /cancel to whatever kernel
    now holds the canvas would hit one that never knew this run (an HTTP error), so the caller instead
    falls back to the last-known persisted status."""
    with session() as s:
        r = s.get(RunState, run_id)
        if r is None or not r.canvas_id:
            return None
        k = s.get(Kernel, r.canvas_id)
        if k is None or (r.kernel_id and k.kernel_id != r.kernel_id):
            return None
        return {"endpoint": k.endpoint, "token": k.token, "kernel_id": k.kernel_id}


def reap_kernels() -> list[tuple[str, str]]:
    """Delete leases whose kernel is presumed dead (stale heartbeat). Any hub, on boot + on a timer.
    Returns the reaped (canvas_id, kernel_id) pairs so the caller can also tear down the substrate
    (delete the pod + service) — otherwise a crashed/fenced pod's k8s objects accumulate as orphans."""
    reaped: list[tuple[str, str]] = []
    with session() as s:
        for r in s.scalars(select(Kernel)):
            if _kernel_stale(r):
                reaped.append((r.canvas_id, r.kernel_id))
                s.delete(r)
    return reaped


def reap_orphaned_runs(only_kernel_runs: bool = False) -> int:
    """Fail a non-terminal run whose owning kernel is gone/stale. A run owned by a still-live kernel is
    LEFT running so the client reattaches — replacing the old blanket "fail every run on restart".

    `only_kernel_runs` distinguishes the two callers: on hub BOOT (default False) a kernel-less run —
    an in-process / subprocess run — belonged to the now-dead previous hub, so it is reaped too. On the
    PERIODIC path (True) a kernel-less run belongs to THIS live hub process (or, across instances, to
    another live one) and must NOT be reaped mid-flight — only its dead-kernel runs are."""
    n = 0
    with session() as s:
        live = {k.kernel_id for k in s.scalars(select(Kernel)) if not _kernel_stale(k)}
        for r in s.scalars(select(RunState).where(RunState.status.in_(("queued", "running")))):
            if r.kernel_id and r.kernel_id in live:
                continue  # owning kernel is alive → leave it running (reattach)
            if only_kernel_runs and not r.kernel_id:
                continue  # periodic path: a kernel-less run belongs to a live hub process, not us to reap
            try:
                d = json.loads(r.doc)
            except Exception:  # noqa: BLE001
                d = {"run_id": r.run_id}
            d["status"] = "failed"
            d["error"] = "interrupted — the run's kernel is gone (hub restarted with no live kernel)"
            r.status = "failed"
            r.doc = json.dumps(d, default=str)
            n += 1
    return n


def snapshot_canvas(canvas_id: str, doc_json: str, version: int, author_id: str | None = None,
                    label: str | None = None, throttle_seconds: int = 90, keep: int = 30) -> bool:
    """Save a snapshot of a canvas doc for later restore. Auto-snapshots (label=None) are throttled —
    skipped if a recent one exists or the doc is unchanged — and pruned to the newest `keep`; named
    snapshots are always kept. Returns True if a row was written."""
    with session() as s:
        if s.get(Canvas, canvas_id) is None:
            return False
        if label is None:
            last = s.scalars(select(CanvasVersion).where(CanvasVersion.canvas_id == canvas_id, CanvasVersion.label.is_(None))
                             .order_by(CanvasVersion.created_at.desc()).limit(1)).first()
            if last:
                if last.doc == doc_json:
                    return False  # nothing changed since the last auto-snapshot
                lc = last.created_at
                if lc is not None and lc.tzinfo is None:
                    lc = lc.replace(tzinfo=datetime.timezone.utc)  # SQLite may hand back naive
                if lc is not None and (_now() - lc).total_seconds() < throttle_seconds:
                    return False  # too soon — don't snapshot every 400ms autosave
        s.add(CanvasVersion(canvas_id=canvas_id, version=version, doc=doc_json, label=label, author_id=author_id))
        s.flush()
        autos = s.scalars(select(CanvasVersion).where(CanvasVersion.canvas_id == canvas_id, CanvasVersion.label.is_(None))
                          .order_by(CanvasVersion.created_at.desc())).all()
        for old in autos[keep:]:  # prune the oldest auto-snapshots; named ones are retained
            s.delete(old)
    return True


def list_versions(canvas_id: str, limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(CanvasVersion).where(CanvasVersion.canvas_id == canvas_id)
                         .order_by(CanvasVersion.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "version": r.version, "label": r.label, "authorId": r.author_id,
                 "createdAt": r.created_at.isoformat() if r.created_at else None} for r in rows]


def get_version_doc(canvas_id: str, version_id: str) -> str | None:
    with session() as s:
        v = s.get(CanvasVersion, version_id)
        return v.doc if v and v.canvas_id == canvas_id else None


def set_setting(key: str, value, scope: str = "global", scope_id: str = "") -> None:
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        if row:
            row.value = json.dumps(value)
        else:
            s.add(Setting(scope=scope, scope_id=scope_id, key=key, value=json.dumps(value)))
