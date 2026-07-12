"""Shared DuckDB connection — the default local data engine.

DuckDB reads Parquet/CSV, samples, counts, runs SQL views, and writes outputs. No external
service. A single `DuckDBPyConnection` is NOT safe for concurrent use by multiple threads, so
there are two access modes:

- **Base connection** (`conn()` outside a scope) + `db.lock()` — for quick shared metadata ops
  (catalog registration, object-store setup, schema fetch on the request thread). Serialized.
- **Per-run scope** (`with db.run_scope(): ...`) — a whole preview/run evaluation runs on its OWN
  cursor (a second connection to the same in-memory database: shared catalog/tables/secrets, but
  an INDEPENDENT transaction). Concurrent runs/previews therefore no longer serialize on one lock,
  and one run's failure (an aborted transaction) or its view cleanup can't wedge or clobber another
  run — each scope drops only the temp views IT minted. Cursor ops are thread-confined, so they
  need no lock. This is what stops a single long run from freezing every other user's preview/run.

Temp view names are unique across process lifetimes so names and derived spill files never collide.
"""

from __future__ import annotations

import itertools
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

import duckdb

_lock = threading.RLock()  # serializes access to the shared BASE connection (not per-run cursors)
_conn: duckdb.DuckDBPyConnection | None = None
_view_seq = itertools.count(1)
_view_namespace = uuid.uuid4().hex[:12]
_created_views: set[str] = set()
_local = threading.local()  # per-thread run scope: .con (the cursor) + .scope (the _Scope)


def lock() -> threading.RLock:
    """Acquire around a base-connection metadata op: `with db.lock(): ...`. Per-run/preview work
    should use `run_scope()` instead (its own cursor, no global serialization)."""
    return _lock


def _rollback_base() -> None:
    """Clear an ABORTED implicit transaction on the base connection. A statement that fails mid-work
    (a bad scan, a swallowed adapter probe, an interrupted query) leaves DuckDB's implicit transaction
    aborted, and every LATER base-connection op then fails with 'current transaction is aborted' — one
    shared connection would wedge the whole engine until restart. Roll back so a failure self-heals.
    Caller must hold `_lock`."""
    try:
        if _conn is not None:
            _conn.execute("ROLLBACK")
    except Exception:  # noqa: BLE001 — no active transaction to roll back
        pass


@contextmanager
def base_guard() -> Iterator[None]:
    """Serialize a base-connection EXECUTION and keep it un-wedged. A no-op inside a `run_scope()` —
    there `conn()` is a thread-confined cursor that needs no lock — but otherwise holds the
    base-connection lock for the whole op and ROLLS BACK on failure. Wrap adapter metadata ops
    (count/schema) that can run OFF the request thread: a catalog register fires on a runner /
    subprocess-watch DAEMON thread and probes the adapter, while request threads touch the base
    connection — and a bare DuckDBPyConnection is not safe for concurrent use (a lazy relation only
    executes when fetched, so the lock must span the fetch, not just the build). The rollback stops a
    single failed probe from leaving the shared connection's transaction aborted for everyone else."""
    if getattr(_local, "con", None) is not None:
        yield  # inside a run_scope: own cursor, already thread-confined
    else:
        with _lock:
            try:
                yield
            except BaseException:
                _rollback_base()  # a failed base-conn statement must not wedge later ops
                raise


def _spill_dir() -> str:
    """The on-disk spill location — where DuckDB writes external sort/hash/aggregate temp files and
    where the Python-transform spill lands. Operator-controllable via DP_SPILL_DIR."""
    d = os.environ.get("DP_SPILL_DIR") or os.path.join(tempfile.gettempdir(), "dataplay-spill")
    os.makedirs(d, exist_ok=True)
    return d


_BYTE_UNITS = {"": 1, "K": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12,
               "KI": 2 ** 10, "MI": 2 ** 20, "GI": 2 ** 30, "TI": 2 ** 40}


def _parse_bytes(s: str) -> int | None:
    """Parse a memory-size string ('300MB', '2GiB', '512') to bytes; None if unparseable."""
    import re
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT]?I?)B?\s*", str(s).upper())
    return int(float(m.group(1)) * _BYTE_UNITS.get(m.group(2), 1)) if m else None


def _apply_session(c: duckdb.DuckDBPyConnection) -> None:
    c.execute("SET enable_progress_bar = false")
    # Do NOT auto-install/auto-load extensions: that let ANY uri (e.g. https://evil/x.parquet)
    # silently pull in httpfs and fetch it (SSRF). Object-store access loads httpfs EXPLICITLY in
    # ensure_object_store(), so s3://gs:// still work; other schemes now fail closed instead of
    # reaching out. Re-asserted on every per-run cursor (below) since it's a security setting.
    c.execute("SET autoinstall_known_extensions = false")
    c.execute("SET autoload_known_extensions = false")
    # Out-of-core: point DuckDB's temp files at an explicit, operator-controllable dir so large
    # sorts/joins/aggregates spill to disk instead of failing (robust across versions + lets a deploy
    # put spill on fast/roomy disk). DP_MEMORY_LIMIT optionally caps per-kernel RAM (multi-tenant).
    try:
        c.execute("SET temp_directory = ?", [_spill_dir()])
        ml = os.environ.get("DP_MEMORY_LIMIT")
        if ml:
            c.execute("SET memory_limit = ?", [ml])
            # Cap threads to a sane memory-per-thread ratio. The query pipeline spills, but the
            # order-preserving write/COPY buffers per thread, so at a tight limit the default thread
            # count (all cores) OOMs the write even though the sort/aggregate completes. We only LOWER
            # threads (never raise RAM above the operator's cap — the cap is a hard multi-tenant limit).
            mb = _parse_bytes(ml)
            floor = int(os.environ.get("DP_MIN_MEM_PER_THREAD_MB", "96")) * 2 ** 20
            if mb and floor:
                want = max(1, mb // floor)
                cur = int(c.execute("SELECT current_setting('threads')").fetchone()[0])
                if cur > want:
                    c.execute("SET threads = ?", [want])
    except Exception:  # noqa: BLE001 — never let a tuning knob block the connection
        pass


def _maybe_sandbox_fs(c: duckdb.DuckDBPyConnection) -> None:
    """Confine DuckDB's filesystem to the allowed dataset roots (+ spill dir) and disable external
    access — so a `sql` node's read_csv/read_text/COPY can't reach arbitrary local files or the
    network, closing the one data-confinement gap the per-node ensure_local_uri_allowed check misses.

    Applied ONCE on the base connection (DuckDB's enable_external_access is a process-wide, one-way
    switch — it also propagates to cursors), and only when: auth is enabled (multi-user; open
    single-user mode is a trusted local tool) AND no object store is configured. The two are genuinely
    mutually exclusive in DuckDB, not just by our choice: `enable_external_access` is the MASTER switch
    for all off-database access (network AND local files beyond the DB), and `allowed_directories` only
    takes effect while it is FALSE — with it TRUE (required for httpfs/s3) a `read_csv('/etc/passwd')`
    is NOT confined by allowed_directories (verified). So when an object store is configured we cannot
    also FS-sandbox; we WARN so the widening is never silent (real local-FS isolation alongside object
    storage needs OS-level isolation — the pod/subprocess runner, see README)."""
    try:
        from hub import auth
        if not auth.auth_enabled():
            return
        from hub import metadb
        from hub.plugins.adapters import is_object_uri
        # an object store may be configured EITHER via the DB setting OR the DP_STORAGE_URL env var
        # (creds then from the AWS chain) — both need external access on, or httpfs/s3 fails closed (P0-STOR-01)
        storage_url = (os.environ.get("DP_STORAGE_URL") or "").strip()
        if metadb.get_setting("objectStore", "global", default={}) or is_object_uri(storage_url):
            import logging
            logging.getLogger("hub").warning(
                "FS sandbox DISABLED: an object store is configured, so DuckDB runs with external access "
                "enabled — a `sql` node can read arbitrary LOCAL files (the soft sandbox can't confine "
                "local reads while network access is on). Run with OS-level isolation (the pod runner) "
                "for untrusted multi-user use.")
            return
        from hub import paths
        dirs = sorted({d for d in (*paths.allowed_roots(), _spill_dir()) if d})
        c.execute("SET allowed_directories = ?", [dirs])
        c.execute("SET enable_external_access = false")
    except Exception:  # noqa: BLE001 — never block connection creation on the sandbox
        pass


def _base_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = duckdb.connect(":memory:")
                _apply_session(_conn)
                _maybe_sandbox_fs(_conn)  # multi-user + no object store → confine the FS (sql node too)
    return _conn


def conn() -> duckdb.DuckDBPyConnection:
    """The DuckDB connection for the caller: the current thread's per-run CURSOR when inside a
    `run_scope()`, else the shared base connection. Engine/adapter code calls this unchanged."""
    c = getattr(_local, "con", None)
    return c if c is not None else _base_conn()


def is_run_scoped() -> bool:
    """Whether this thread already owns an isolated run cursor."""
    return getattr(_local, "con", None) is not None


class _Scope:
    """One run/preview's isolated DuckDB cursor + the temp views it minted."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.views: set[str] = set()

    def interrupt(self) -> None:
        """Abort this scope's in-flight query. Safe to call from ANOTHER thread (cancel / preview
        timeout) — interrupting the BASE connection would NOT stop a query running on this cursor."""
        try:
            self.con.interrupt()
        except Exception:  # noqa: BLE001 — nothing running / already finished
            pass


@contextmanager
def run_scope() -> Iterator[_Scope]:
    """Give this thread its own DuckDB cursor for the duration of a run/preview, so it doesn't
    serialize on (or get wedged by) any other run. Yields a `_Scope` whose `.interrupt()` a canceller
    can call from another thread. On exit, rolls back and drops only the views this scope created."""
    with _lock:
        cur = _base_conn().cursor()  # cursor creation touches the base connection — serialize it too
    try:
        _apply_session(cur)  # re-assert the SSRF-safe extension policy on the cursor (defensive)
    except Exception:  # noqa: BLE001
        pass
    scope = _Scope(cur)
    _local.con = cur
    _local.scope = scope
    try:
        yield scope
    finally:
        _local.con = None
        _local.scope = None
        try:
            cur.execute("ROLLBACK")  # clear any aborted transaction before dropping views
        except Exception:  # noqa: BLE001 — no active transaction
            pass
        for n in list(scope.views):
            try:
                cur.execute(f'DROP VIEW IF EXISTS "{n}"')
            except Exception:  # noqa: BLE001
                pass
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


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
        from hub import metadb
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


def responsive(timeout_s: float = 5.0) -> bool:
    """True if the engine can complete a trivial query within `timeout_s`. A wedged process — the base
    lock held forever, a deadlocked base connection, GIL starvation — can't. The kernel watchdog uses
    this to recycle a wedged kernel: since runs now execute in child processes, a healthy warm kernel
    ALWAYS passes quickly, so a persistent timeout means it's genuinely stuck. An ERROR still counts as
    responsive (the engine answered); only a HANG (no result within the budget) is a wedge."""
    ok: list[bool] = []

    def _probe() -> None:
        try:
            with run_scope() as sc:
                sc.con.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001 — an error is still a live, responsive engine
            pass
        ok.append(True)

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout_s)
    return bool(ok)


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
    """A temp view name unique across process lifetimes, engines, and threads. Inside a run_scope
    the name is tracked on the SCOPE (dropped when the scope exits, on its own cursor); otherwise on
    the global set (dropped by drop_created_views under the base connection). The process namespace
    also makes derived spill filenames safe when independent kernels share DP_SPILL_DIR."""
    # The PID separates forked processes; the nonce prevents stale files from a previous process with
    # a reused PID from colliding. itertools.count.__next__ is atomic under the GIL within a process.
    name = f"dp_{prefix}_{os.getpid()}_{_view_namespace}_{next(_view_seq)}"
    scope = getattr(_local, "scope", None)
    if scope is not None:
        scope.views.add(name)
    else:
        with _lock:
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
    with base_guard():  # serialize + roll back on failure so bad SQL can't wedge the base connection
        cur = conn().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_columns(sql: str, params: list | None = None) -> list[tuple[str, str]]:
    with base_guard():
        cur = conn().execute(f"DESCRIBE {sql}", params or [])
        return [(r[0], r[1]) for r in cur.fetchall()]


def execute(sql: str, params: list | None = None) -> None:
    with base_guard():
        conn().execute(sql, params or [])
