"""Shared DuckDB connection — the default local data engine.

DuckDB reads Parquet/CSV, samples, counts, runs SQL views, and writes outputs. No external
service. A `DuckDBPyConnection` is NOT safe for concurrent use, so all execution is serialized
under one reentrant lock (`with db.lock(): ...`) and temporary view names are process-globally
unique (concurrent evaluations would otherwise clobber each other's views).
"""

from __future__ import annotations

import itertools
import threading

import duckdb

_lock = threading.RLock()  # reentrant: serializes ALL DuckDB access; query()/execute() nest under it
_conn: duckdb.DuckDBPyConnection | None = None
_view_seq = itertools.count(1)
_created_views: set[str] = set()


def lock() -> threading.RLock:
    """Acquire around a whole preview/run evaluation: `with db.lock(): ...`."""
    return _lock


def conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = duckdb.connect(":memory:")
                _conn.execute("SET enable_progress_bar = false")
    return _conn


def unique_view(prefix: str = "v") -> str:
    """A process-globally-unique temp view name (never collides across engines/threads)."""
    with _lock:
        name = f"dp_{prefix}_{next(_view_seq)}"
        _created_views.add(name)
        return name


def drop_created_views() -> None:
    """Drop the temp views minted during an evaluation (call in a finally, under the lock)."""
    with _lock:
        for n in list(_created_views):
            try:
                conn().execute(f'DROP VIEW IF EXISTS "{n}"')
            except Exception:  # noqa: BLE001
                pass
        _created_views.clear()


def query(sql: str, params: list | None = None) -> list[dict]:
    with _lock:
        cur = conn().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_columns(sql: str, params: list | None = None) -> list[tuple[str, str]]:
    with _lock:
        cur = conn().execute(f"DESCRIBE {sql}", params or [])
        return [(r[0], r[1]) for r in cur.fetchall()]


def execute(sql: str, params: list | None = None) -> None:
    with _lock:
        conn().execute(sql, params or [])
