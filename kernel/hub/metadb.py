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
import os
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
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
    role: Mapped[str] = mapped_column(String, default="editor")  # 'editor' | 'viewer'
    __table_args__ = (UniqueConstraint("canvas_id", "user_id", name="uq_share"),)


class RunRecord(Base):
    """A finished run, kept with its canvas (run history survives restarts). One row per run."""
    __tablename__ = "run_records"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    canvas_id: Mapped[str] = mapped_column(String, ForeignKey("canvases.id"), index=True)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_table: Mapped[str | None] = mapped_column(String, nullable=True)
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
    CatalogTable (incl. probed schema) as JSON so no re-probe is needed to serve it."""
    __tablename__ = "catalog_entries"
    uri: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    doc: Mapped[str] = mapped_column(Text)  # the full CatalogTable as JSON
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CatalogEdge(Base):
    """A lineage edge (parent uri → child uri), shared like CatalogEntry so lineage is cross-instance."""
    __tablename__ = "catalog_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent: Mapped[str] = mapped_column(String, index=True)
    child: Mapped[str] = mapped_column(String, index=True)
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
        if auth.auth_enabled() and not u.password_hash and auth.bootstrap_password():
            u.password_hash = auth.hash_password(auth.bootstrap_password())
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
        return True


def get_setting(key: str, scope: str = "global", scope_id: str = "", default=None):
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        return json.loads(row.value) if row else default


def canvas_role(canvas_id: str, uid: str) -> str | None:
    """The user's access to a canvas: 'owner' | 'editor' | 'viewer' | None."""
    with session() as s:
        c = s.get(Canvas, canvas_id)
        if c is None:
            return None
        if c.owner_id == uid:
            return "owner"
        if c.visibility == "workspace":
            return "editor"  # any user of this instance can edit a workspace-visible canvas
        sh = s.scalar(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id, CanvasShare.user_id == uid))
        if sh:
            return sh.role
        if c.visibility == "workspace_view":
            return "viewer"  # workspace-visible but read-only, unless explicitly shared as editor above
        return None


def share_canvas(canvas_id: str, user_id: str, role: str = "editor") -> None:
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
            out[c.id] = _canvas_row(c, "owner", False)
        for c, role in s.execute(select(Canvas, CanvasShare.role)
                                 .join(CanvasShare, CanvasShare.canvas_id == Canvas.id)
                                 .where(CanvasShare.user_id == uid)).all():
            out.setdefault(c.id, _canvas_row(c, role, True))
        for c in s.scalars(select(Canvas).where(Canvas.visibility == "workspace", Canvas.owner_id != uid)):
            out.setdefault(c.id, _canvas_row(c, "editor", True))
        for c in s.scalars(select(Canvas).where(Canvas.visibility == "workspace_view", Canvas.owner_id != uid)):
            out.setdefault(c.id, _canvas_row(c, "viewer", True))
        return sorted(out.values(), key=lambda r: r["updatedAt"] or "", reverse=True)


def record_run(canvas_id: str | None, target_node_id: str | None, status: str,
               rows: int | None = None, ms: int | None = None, error: str | None = None,
               output_table: str | None = None, per_node: list[dict] | None = None) -> bool:
    """Persist a finished run under its canvas. No-op (returns False) without a real canvas — an ad-hoc
    API run or an internal region sub-run (graph id '_region'). Returns True when a row was written."""
    if not canvas_id:
        return False
    with session() as s:
        if s.get(Canvas, canvas_id) is None:
            return False  # ad-hoc / unsaved-canvas / internal region run → don't dangle a run row
        s.add(RunRecord(canvas_id=canvas_id, target_node_id=target_node_id, status=status,
                        rows=rows, ms=ms, error=error, output_table=output_table,
                        per_node=json.dumps(per_node, default=str) if per_node else None))
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
        c = s.get(Canvas, canvas_id)
        if c:
            s.delete(c)


def latest_actuals(canvas_id: str | None) -> dict[str, int]:
    """Per-node measured row counts from the most recent SUCCESSFUL run of this canvas — feeds the size
    estimator (as `actuals`) so nodes whose output is statically unknowable (join / aggregate / sql /
    code) carry a real count on the next estimate instead of 'unknown'. The caller guards staleness (an
    edited node's status is no longer 'latest')."""
    if not canvas_id:
        return {}
    with session() as s:
        r = s.scalar(select(RunRecord).where(RunRecord.canvas_id == canvas_id, RunRecord.status == "done")
                     .order_by(RunRecord.created_at.desc()).limit(1))
        if not r or not r.per_node:
            return {}
        try:
            pn = json.loads(r.per_node)
        except (ValueError, TypeError):
            return {}
        return {p["node_id"]: int(p["rows"]) for p in pn
                if isinstance(p, dict) and p.get("node_id") and p.get("rows") is not None}


def list_runs(canvas_id: str, limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id)
                         .order_by(RunRecord.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "status": r.status, "targetNodeId": r.target_node_id, "rows": r.rows,
                 "ms": r.ms, "error": r.error, "outputTable": r.output_table,
                 "perNode": json.loads(r.per_node) if r.per_node else None,
                 "createdAt": r.created_at.isoformat() if r.created_at else None} for r in rows]


def save_run_state(run_id: str, status: dict, canvas_id: str | None = None, kernel_id: str | None = None) -> None:
    """Upsert a run's live status (the runner calls this on each transition). `status` is a RunStatus
    model_dump; stored whole as JSON so GET /run/{id} can rebuild it on any instance. `kernel_id`
    stamps the owning kernel so the boot-time reaper fails a run only when its kernel is gone."""
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


def get_result(key: str) -> dict | None:
    """The stored result pointer ({uri, table, rows, fmt}) for a plan's content hash, or None."""
    with session() as s:
        r = s.get(ResultCache, key)
        return json.loads(r.doc) if r else None


def put_result(key: str, doc: dict) -> None:
    """Upsert a completed run's result pointer, then prune to the newest N (safe: a miss recomputes)."""
    with session() as s:
        r = s.get(ResultCache, key)
        payload = json.dumps(doc, default=str)
        if r is None:
            s.add(ResultCache(key=key, doc=payload))
        else:
            r.doc = payload
        s.flush()
        stale = s.scalars(select(ResultCache.key).order_by(ResultCache.created_at.desc())
                          .offset(_RESULT_CACHE_MAX)).all()
        for k in stale:
            obj = s.get(ResultCache, k)
            if obj:
                s.delete(obj)


def catalog_upsert_entry(uri: str, name: str, doc: dict) -> None:
    """Write-through a catalog entry (registered dataset / written output) to the shared DB, keyed by
    uri, so other instances + a restart see it. `doc` is the full CatalogTable model_dump."""
    with session() as s:
        r = s.get(CatalogEntry, uri)
        payload = json.dumps(doc, default=str)
        if r is None:
            s.add(CatalogEntry(uri=uri, name=name, doc=payload))
        else:
            r.name = name
            r.doc = payload


def catalog_add_edge(parent: str, child: str, pipeline: str | None = None) -> None:
    """Write-through a lineage edge; one row per (parent, child)."""
    if parent == child:
        return
    with session() as s:
        exists = s.scalars(select(CatalogEdge).where(CatalogEdge.parent == parent, CatalogEdge.child == child)).first()
        if exists is None:
            s.add(CatalogEdge(parent=parent, child=child, pipeline=pipeline))


def catalog_entries() -> list[dict]:
    """Every persisted catalog entry, as CatalogTable-shaped dicts (for the in-memory catalog to load)."""
    with session() as s:
        return [json.loads(r.doc) for r in s.scalars(select(CatalogEntry))]


def catalog_delete_entry(uri: str) -> None:
    """Remove a catalog entry (unregister) from the shared store."""
    with session() as s:
        r = s.get(CatalogEntry, uri)
        if r is not None:
            s.delete(r)


def catalog_edges() -> list[dict]:
    with session() as s:
        return [{"parent": r.parent, "child": r.child, "pipeline": r.pipeline}
                for r in s.scalars(select(CatalogEdge))]


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


def catalog_declared_keys() -> dict[str, list]:
    """{uri: [column, ...]} for every declared primary key."""
    with session() as s:
        return {r.uri: json.loads(r.columns) for r in s.scalars(select(CatalogDeclaredKey))}


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
