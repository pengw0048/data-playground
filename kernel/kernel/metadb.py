"""Metadata store — users, canvases (per-user files), and settings.

A small SQLAlchemy layer, separate from `db.py` (which is the DuckDB data engine). Dev uses a
bundled SQLite file; deployment points DP_DATABASE_URL at Postgres. Only the connection string is
config; all metadata lives in this instance's DB. Auth is intentionally light (internal-tool grade):
the current user is carried in an `X-DP-User` header and defaults to a seeded local user — real
authentication is a later, separable layer.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from kernel.settings import settings

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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Canvas(Base):
    __tablename__ = "canvases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String, default="untitled")
    version: Mapped[int] = mapped_column(Integer, default=1)
    doc: Mapped[str] = mapped_column(Text, default="{}")  # the full CanvasDoc as JSON
    visibility: Mapped[str] = mapped_column(String, default="private")  # 'private' | 'workspace'
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


_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = settings.database_url
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        _engine = create_engine(url, **kw)
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
        if s.get(User, DEFAULT_USER_ID) is None:
            s.add(User(id=DEFAULT_USER_ID, name="Local"))


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


def resolve_user(user_id: str | None) -> str:
    """Return a valid user id for the request — the header's user if it exists, else the default
    local user (created on demand). Light by design; a real auth layer replaces this later."""
    with session() as s:
        if user_id and s.get(User, user_id) is not None:
            return user_id
        if s.get(User, DEFAULT_USER_ID) is None:
            s.add(User(id=DEFAULT_USER_ID, name="Local"))
        return DEFAULT_USER_ID


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
        return sh.role if sh else None


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
        return sorted(out.values(), key=lambda r: r["updatedAt"] or "", reverse=True)


def record_run(canvas_id: str | None, target_node_id: str | None, status: str,
               rows: int | None = None, ms: int | None = None, error: str | None = None,
               output_table: str | None = None) -> None:
    """Persist a finished run under its canvas. No-op without a canvas id (e.g. ad-hoc API runs)."""
    if not canvas_id:
        return
    with session() as s:
        if s.get(Canvas, canvas_id) is None:
            return  # ad-hoc / unsaved-canvas run → don't write a run row dangling off a missing canvas
        s.add(RunRecord(canvas_id=canvas_id, target_node_id=target_node_id, status=status,
                        rows=rows, ms=ms, error=error, output_table=output_table))


def delete_canvas_cascade(canvas_id: str) -> None:
    """Delete a canvas and its children (shares, run history) — FKs don't cascade (SQLite FK off,
    Postgres would error), so clean them explicitly."""
    with session() as s:
        for sh in s.scalars(select(CanvasShare).where(CanvasShare.canvas_id == canvas_id)):
            s.delete(sh)
        for r in s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id)):
            s.delete(r)
        c = s.get(Canvas, canvas_id)
        if c:
            s.delete(c)


def list_runs(canvas_id: str, limit: int = 50) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(RunRecord).where(RunRecord.canvas_id == canvas_id)
                         .order_by(RunRecord.created_at.desc()).limit(limit)).all()
        return [{"id": r.id, "status": r.status, "targetNodeId": r.target_node_id, "rows": r.rows,
                 "ms": r.ms, "error": r.error, "outputTable": r.output_table,
                 "createdAt": r.created_at.isoformat() if r.created_at else None} for r in rows]


def set_setting(key: str, value, scope: str = "global", scope_id: str = "") -> None:
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        if row:
            row.value = json.dumps(value)
        else:
            s.add(Setting(scope=scope, scope_id=scope_id, key=key, value=json.dumps(value)))
