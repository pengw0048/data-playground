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

import contextvars
import itertools
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

import duckdb

_lock = threading.RLock()  # serializes access to the shared BASE connection (not per-run cursors)

# The resolved object-store config this run/request should use (a destination's credential), carried
# from the write/browse caller down to the object-store open. Set before a run scope opens so the
# scope cursor snapshots the right secret (DuckDB freezes a cursor's secret view at transaction start).
_bound_object_store_cfg: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "dp_object_store_cfg", default=None)
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
    # A SQL identifier must resolve only against DuckDB's catalog.  Python replacement scans can turn
    # an otherwise missing table name into an in-process Python object, bypassing the SQL input policy.
    c.execute("SET python_enable_replacements = false")
    # Keep unqualified relation/function resolution deterministic.  `main` still exposes the run's
    # generated views; arbitrary attached/custom schemas cannot silently enter the search path.
    c.execute("SET search_path = 'main'")
    # Do NOT auto-install/auto-load extensions: unknown schemes must not silently add network access.
    # Object-store access loads httpfs explicitly in ensure_object_store(), so authenticated sessions
    # also disable its direct HTTP(S) and Hugging Face filesystems. S3FileSystem remains available,
    # while user-authored SQL and dataset URLs cannot turn trusted extension loading into arbitrary
    # network egress after httpfs is loaded.
    # Re-asserted on every per-run cursor (below) because these are security settings.
    c.execute("SET autoinstall_known_extensions = false")
    c.execute("SET autoload_known_extensions = false")
    from hub import auth
    if auth.auth_enabled():
        c.execute("SET disabled_filesystems = 'HTTPFileSystem,HuggingFaceFileSystem'")
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
        # an object store may be configured via the default Cred, the DP_STORAGE_URL
        # env var, OR a per-destination object-store credential — ALL need external access on, or
        # httpfs/s3 fails closed (P0-STOR-01). A destination-only S3 install must not stay sandboxed.
        storage_url = (os.environ.get("DP_STORAGE_URL") or "").strip()
        try:
            has_default_object_store = bool(metadb.cred_object_store_config(None))
        except metadb.CredResolutionError:
            has_default_object_store = True  # configured-but-broken default still means an object store is intended
        dests = metadb.get_setting("destinations", "global", default=[]) or []
        has_object_store_dest = any(isinstance(d, dict) and d.get("backend") in ("s3", "gs") for d in dests)
        if has_default_object_store or is_object_uri(storage_url) or has_object_store_dest:
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
    _prime_object_store_before_scope()
    with _lock:
        cur = _base_conn().cursor()  # cursor creation touches the base connection — serialize it too
    try:
        _apply_session(cur)  # re-assert the SSRF-safe extension policy on the cursor (defensive)
    except Exception:  # noqa: BLE001
        pass
    # Keep one catalog snapshot from policy validation through lazy relation execution.  DuckDBPyRelation
    # binds at fetch/write time, not at con.sql(), so without this fence a concurrent catalog mutation
    # could insert a macro/UDF after validation and change which function the relation executes.  Runs do
    # not publish DuckDB catalog state; rollback is already their cleanup contract.
    cur.execute("BEGIN TRANSACTION")
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
_obj_store_aws_loaded = False
_obj_store_secret_config: tuple | None = None

_CREDENTIAL_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_EC2_METADATA_DISABLED",
)


def _sql_str(s: str) -> str:
    return str(s).replace("'", "''")


def _object_store_fingerprint(cfg: dict) -> tuple:
    config = tuple(cfg.get(key) for key in (
        "accessKeyId", "secretAccessKey", "sessionToken", "region", "endpoint", "useSsl",
    ))
    explicit_keys = bool(cfg.get("accessKeyId") and cfg.get("secretAccessKey"))
    environment = () if explicit_keys else tuple(os.environ.get(key) for key in _CREDENTIAL_ENV_KEYS)
    return (("config" if explicit_keys else "credential_chain"), *config, *environment)


def _publish_object_store(cfg: dict) -> None:
    """Publish one fingerprinted object-store configuration under the base lock."""
    global _obj_store_loaded, _obj_store_aws_loaded, _obj_store_secret_config
    fingerprint = _object_store_fingerprint(cfg)
    with _lock:
        c = _base_conn()
        if not _obj_store_loaded:
            c.execute("INSTALL httpfs")  # bundled on some platforms, downloaded on others (needs net once)
            c.execute("LOAD httpfs")
            _obj_store_loaded = True
            _obj_store_secret_config = None
        if fingerprint == _obj_store_secret_config:
            return
        explicit_keys = fingerprint[0] == "config"
        if not explicit_keys and not _obj_store_aws_loaded:
            # credential_chain is registered by aws (not httpfs) on DuckDB 1.5.x. Explicit credentials
            # remain httpfs-only; chain users pay this installation/load cost once.
            c.execute("INSTALL aws")
            c.execute("LOAD aws")
            _obj_store_aws_loaded = True
        for kind, secret in (("s3", "dp_s3"), ("gcs", "dp_gcs")):
            if explicit_keys:
                parts = [f"TYPE {kind}", f"KEY_ID '{_sql_str(cfg['accessKeyId'])}'",
                         f"SECRET '{_sql_str(cfg['secretAccessKey'])}'"]
                if kind == "s3" and cfg.get("sessionToken"):
                    parts.append(f"SESSION_TOKEN '{_sql_str(cfg['sessionToken'])}'")
                if cfg.get("region"):
                    parts.append(f"REGION '{_sql_str(cfg['region'])}'")
                endpoint = str(cfg.get("endpoint") or "").strip()
                if kind == "s3" and endpoint:
                    # DuckDB wants host[:port] with no scheme; the scheme decides USE_SSL
                    use_ssl = not endpoint.startswith("http://") if cfg.get("useSsl") is None else bool(cfg.get("useSsl"))
                    host = endpoint.split("://", 1)[-1].rstrip("/")
                    parts += [f"ENDPOINT '{_sql_str(host)}'", "URL_STYLE 'path'",
                              f"USE_SSL {'true' if use_ssl else 'false'}"]
                c.execute(f"CREATE OR REPLACE TEMPORARY SECRET {secret} ({', '.join(parts)})")
            else:
                try:
                    c.execute(
                        f"CREATE OR REPLACE TEMPORARY SECRET {secret} "
                        f"(TYPE {kind}, PROVIDER credential_chain, REFRESH auto)"
                    )
                except duckdb.Error as exc:
                    # An empty chain preserves anonymous/default access. Never hide catalog conflicts,
                    # extension errors, or an invalid secret implementation.
                    if "Secret Validation Failure" not in str(exc):
                        raise
                    c.execute(f"DROP SECRET IF EXISTS {secret}")
        # A successfully created chain refreshes expiring role/container credentials itself. Static env
        # key/session/profile changes are in the fingerprint and force replacement before the next cursor.
        # If no chain credentials exist, cache the attempted fingerprint as an anonymous/no-secret
        # state. A later static env/session/profile change invalidates it; an established role/container
        # chain uses REFRESH auto for expiry-driven rotation.
        _obj_store_secret_config = fingerprint


@contextmanager
def object_store_binding(cfg: dict | None) -> Iterator[None]:
    """Bind the resolved object-store credentials this run/request should use, so a later
    ``ensure_object_store()`` (with no explicit cfg) and the pre-scope prime both adopt it. Reset on
    exit — the binding is per-context, never a stale process-global secret."""
    token = _bound_object_store_cfg.set(cfg)
    try:
        yield
    finally:
        _bound_object_store_cfg.reset(token)


def _default_object_store_cfg() -> dict:
    """Resolved default object-store Cred fields, or an empty config for the ambient SDK chain."""
    from hub import metadb
    from hub.secrets import resolve_object_store
    return resolve_object_store(metadb.cred_object_store_config(None))


def ensure_object_store(cfg: dict | None = None) -> None:
    """Publish httpfs + credentials on the base connection for object-store consumers.

    ``cfg`` is a resolved object-store config (a destination's credential). With no cfg, adopt the
    context binding if one is set (a write/browse in progress), else the default Cred or deliberate
    ambient SDK chain — so a default Cred configured in Settings still reaches generic reads.

    A run scope owns a long rollback-only transaction.  Creating its secret there would make two
    concurrent runs conflict on the shared catalog even though both use the same credentials.  The
    base connection instead performs one short, serialized autocommit publication per config; scoped
    cursors consume that committed secret without writing the catalog themselves. Secret subkeys are
    references; material values are resolved before they reach here.
    """
    if cfg is None:
        cfg = _bound_object_store_cfg.get()
    if cfg is None:
        cfg = _default_object_store_cfg()
    _publish_object_store(cfg or {})


def _prime_object_store_before_scope() -> None:
    """Best-effort publish before a cursor takes its credential snapshot.

    A cursor that began while an older fixed secret existed keeps that old version even after the base
    connection replaces it.  Prime configured/previously-used object storage before creating the cursor;
    a later real object operation still calls ``ensure_object_store`` and surfaces any setup error.
    """
    try:
        # A write/browse bound its destination credential for this context — prime THAT before the
        # cursor snapshots its secret, so the run's object-store writes use the destination's cred.
        bound = _bound_object_store_cfg.get()
        if bound is not None:
            _publish_object_store(bound)
            return
        scheme = (os.environ.get("DP_STORAGE_URL") or "").split(":", 1)[0].lower()
        # A process that has never published a secret cannot have a stale cursor snapshot. This fast
        # path keeps purely local previews free of metadata/provider work.
        if (not _obj_store_loaded and _obj_store_secret_config is None
                and scheme not in ("s3", "s3a", "s3n", "gs", "gcs")):
            return
        from hub import metadb
        from hub.secrets import resolve_object_store
        cfg = metadb.cred_object_store_config(None)  # default Cred, else deliberate ambient chain
        if not cfg and scheme in ("s3", "s3a", "s3n", "gs", "gcs"):
            # Only a one-shot workload (subrun / Ray driver) with no hub settings DB reconstructs its
            # config from the allowlisted data-plane environment; a hub with an empty setting keeps its
            # ambient credential chain instead of adopting the workers' data-plane keys.
            from hub.workload_env import (data_plane_object_store_config,
                                          is_ephemeral_workload)
            if is_ephemeral_workload():
                cfg = data_plane_object_store_config(scheme=scheme)
        cfg = resolve_object_store(cfg)  # Cred fields hold SecretRefs; resolve in-process
        if not _obj_store_loaded or _object_store_fingerprint(cfg) != _obj_store_secret_config:
            _publish_object_store(cfg)
    except Exception:  # noqa: BLE001 — defer setup failure until an object-store operation actually runs
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
            # A readiness probe must not call run_scope(): that path primes object-store extensions
            # and credentials before opening its cursor, which can perform provider/network I/O and
            # make a healthy engine look wedged. Probe the shared engine and its serialization lock
            # directly; a lock that cannot be acquired within the deadline is itself not ready.
            with base_guard():
                conn().execute("SELECT 1").fetchone()
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
