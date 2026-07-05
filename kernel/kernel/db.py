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
                # Do NOT auto-install/auto-load extensions: that let ANY uri (e.g. https://evil/x.parquet)
                # silently pull in httpfs and fetch it (SSRF). Object-store access loads httpfs
                # EXPLICITLY in ensure_object_store(), so s3://gs:// still work; other schemes now fail
                # closed instead of reaching out.
                _conn.execute("SET autoinstall_known_extensions = false")
                _conn.execute("SET autoload_known_extensions = false")
    return _conn


_obj_store_loaded = False


def _sql_str(s: str) -> str:
    return str(s).replace("'", "''")


def ensure_object_store() -> None:
    """Prepare the connection for object storage (s3://, gs://): load httpfs and (re)register
    credentials. Called before an object-store read/write. Credentials come from the `objectStore`
    setting (explicit keys — for AWS, MinIO, R2, or any S3-compatible endpoint) or, when none are
    set, the standard AWS credential chain (env vars / ~/.aws / instance role)."""
    global _obj_store_loaded
    with _lock:
        c = conn()
        if not _obj_store_loaded:
            c.execute("INSTALL httpfs")  # bundled on some platforms, downloaded on others (needs net once)
            c.execute("LOAD httpfs")
            _obj_store_loaded = True
        from kernel import metadb
        cfg = metadb.get_setting("objectStore", "global", default={}) or {}
        for kind, secret in (("s3", "dp_s3"), ("gcs", "dp_gcs")):
            try:
                if cfg.get("accessKeyId") and cfg.get("secretAccessKey"):
                    parts = [f"TYPE {kind}", f"KEY_ID '{_sql_str(cfg['accessKeyId'])}'",
                             f"SECRET '{_sql_str(cfg['secretAccessKey'])}'"]
                    if cfg.get("region"):
                        parts.append(f"REGION '{_sql_str(cfg['region'])}'")
                    endpoint = str(cfg.get("endpoint") or "").strip()
                    if kind == "s3" and endpoint:
                        # DuckDB wants host[:port] with no scheme; the scheme decides USE_SSL
                        use_ssl = not endpoint.startswith("http://") if cfg.get("useSsl") is None else bool(cfg.get("useSsl"))
                        host = endpoint.split("://", 1)[-1].rstrip("/")
                        parts += [f"ENDPOINT '{_sql_str(host)}'", "URL_STYLE 'path'",
                                  f"USE_SSL {'true' if use_ssl else 'false'}"]
                    c.execute(f"CREATE OR REPLACE SECRET {secret} ({', '.join(parts)})")
                else:
                    c.execute(f"CREATE OR REPLACE SECRET {secret} (TYPE {kind}, PROVIDER credential_chain)")
            except Exception:  # noqa: BLE001 — a secret type may be unavailable; the other still helps
                pass


def interrupt() -> None:
    """Abort the in-flight DuckDB query. Safe to call from ANOTHER thread (that's the point): it lets
    a cancel or a preview timeout actually stop a long-running query so the worker thread unwinds and
    releases the process-global lock, instead of pinning the whole engine until the kernel restarts.
    (A pure-Python runaway inside a transform can't be stopped this way — use the subprocess backend
    for real isolation.)"""
    c = _conn
    if c is not None:
        try:
            c.interrupt()
        except Exception:  # noqa: BLE001 — nothing running, or already finished
            pass


def unique_view(prefix: str = "v") -> str:
    """A process-globally-unique temp view name (never collides across engines/threads)."""
    with _lock:
        name = f"dp_{prefix}_{next(_view_seq)}"
        _created_views.add(name)
        return name


def drop_created_views() -> None:
    """Cleanup after an evaluation (call in a finally, under the lock): first roll back any aborted
    transaction, then drop the temp views minted during the eval.

    A failed statement (e.g. scanning a missing file) leaves the shared connection's implicit
    transaction ABORTED — DuckDB then rejects every later query with "current transaction is aborted
    (please ROLLBACK)". Since one connection is reused across all previews/runs, that would wedge the
    whole engine until restart. ROLLBACK clears it; it's a harmless no-op when nothing is aborted."""
    with _lock:
        try:
            conn().execute("ROLLBACK")
        except Exception:  # noqa: BLE001 — no active transaction to roll back
            pass
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
