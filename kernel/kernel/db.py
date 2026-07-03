"""Shared DuckDB connection — the default local data engine.

DuckDB is the one heavyweight dependency of the default bundle: it reads Parquet/CSV,
samples, counts, runs SQL views, and writes Parquet outputs. No external service.
"""

from __future__ import annotations

import threading

import duckdb

_lock = threading.RLock()  # reentrant: query()/execute() hold it while calling conn()
_conn: duckdb.DuckDBPyConnection | None = None


def conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = duckdb.connect(":memory:")
                _conn.execute("SET enable_progress_bar = false")
    return _conn


def query(sql: str, params: list | None = None) -> list[dict]:
    """Run a query, return rows as list[dict]."""
    with _lock:
        cur = conn().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_columns(sql: str, params: list | None = None) -> list[tuple[str, str]]:
    """Return (name, duckdb_type) for the columns a query would produce."""
    with _lock:
        cur = conn().execute(f"DESCRIBE {sql}", params or [])
        return [(r[0], r[1]) for r in cur.fetchall()]


def execute(sql: str, params: list | None = None) -> None:
    with _lock:
        conn().execute(sql, params or [])
