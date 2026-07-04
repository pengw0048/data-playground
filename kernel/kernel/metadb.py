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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


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


def set_setting(key: str, value, scope: str = "global", scope_id: str = "") -> None:
    with session() as s:
        row = s.scalar(select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id, Setting.key == key))
        if row:
            row.value = json.dumps(value)
        else:
            s.add(Setting(scope=scope, scope_id=scope_id, key=key, value=json.dumps(value)))
