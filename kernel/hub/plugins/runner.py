"""Default runner — the local out-of-core engine.

Builds the graph into a DuckDB relation plan and executes it out-of-core (DuckDB streams and
spills, so bigger-than-RAM is fine). Estimates cost coarsely and picks placement by a threshold
— no resource knobs (P4). A runner plugin (Ray/Dask) would bind the SAME plan to a cluster.
Content-addressed: an unchanged plan (by node config + source fingerprint) is served from cache
(FR-6.3/6.4).
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import os
import threading
import time
import uuid

from hub import db, graph as g
from hub.executors.engine import BuildEngine
from hub.models import (
    CompilePlan,
    Graph,
    PerNodeStatus,
    Placement,
    RunEstimate,
    RunStatus,
)

_CONFIRM_ROWS = 5_000_000   # fallback gate when byte size is unknown but the row count is known
_CONFIRM_BYTES = 2 << 30    # 2 GiB — the primary confirm signal: a full pass moving this much data


def _safe_abandon_attempt(uri: str) -> None:
    """Best-effort lifecycle cleanup after this process has proved the writer is stopped."""
    from hub import metadb
    from hub.handoff import discard_attempt

    try:
        abandoned = metadb.abandon_committed_object_attempt(uri)
    except Exception:  # noqa: BLE001 — cleanup must not replace the terminal run result
        logging.getLogger("hub").exception("managed attempt abandon failed during run cleanup")
        return  # metadata uncertainty: retaining data is safer than deleting an owned object
    if not abandoned:
        discard_attempt(uri)


def _is_core_managed_sink(spec, uri: str, adapter) -> bool:
    """The one sink shape whose immutable attempt lifecycle is owned by the core control plane."""
    from hub.plugins.adapters import is_object_uri

    return (
        is_object_uri(uri) and spec.mode == "overwrite"
        and spec.extension.lower() in (".parquet", ".pq")
        and adapter.__class__.__module__ == "hub.plugins.adapters"
    )


def _fmt_bytes(n: int) -> str:
    for unit, scale in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"~{n / scale:.1f} {unit}"
    return f"{n} B"


def _step_progress(status) -> "float | None":
    """0..1 fraction of the run's steps that have finished — a deterministic progress signal any backend
    can report from its per-node states (no reliance on row counts, which many ops can't predict)."""
    pn = status.per_node
    if not pn:
        return None
    return sum(1 for p in pn if p.status in ("done", "failed")) / len(pn)


def _diagnose(msg: str) -> str | None:
    """Map a common engine error to ONE actionable hint (the run failure's 'how to fix'). Honest: only
    recognized patterns get a hint; anything else shows the raw error alone (never fabricate a cause)."""
    m = msg.lower()
    if ("referenced column" in m or "not found in from" in m
            or "not present in the input schema" in m):  # specifically an unknown COLUMN
        return "a column name doesn't match this step's input — check its column references (see the amber ⚠ hints)"
    if "conversion error" in m or "could not convert" in m or "cannot cast" in m or "type mismatch" in m:
        return "a value doesn't fit the column type — check the types or add an explicit cast"
    if "catalog error" in m or "does not exist" in m:
        return "a table or function name isn't recognized here — check the source/name"
    if "parser error" in m or "syntax error" in m:
        return "a SQL / expression syntax error — check this step's expression"
    if "binder error" in m:  # a bind failure that ISN'T a plain unknown-column (function/arg-type resolution)
        return "an expression didn't resolve — check the column names, functions, and argument types used here"
    return None
_MAX_RUNS = 100          # cap retained run history / cache so a long-lived kernel doesn't grow forever
_PUBLICATION_RETRY_INITIAL_S = 0.05
_PUBLICATION_RETRY_MAX_S = 1.0


def _persist_local_result_done(persist, receipt, *, on_retry=None, wait=time.sleep) -> None:
    """Idempotently establish a local full-result terminal owner after an unknown commit outcome.

    Once ``persist`` raises, neither a negative read nor another connection error proves rollback: the
    server may still finish/advertise the commit.  Keep the writer fence and replay the *same* done
    document until either the write returns or the exact receipt proves it committed.  This deliberately
    has no attempt limit; process death leaves the durable lock for normal reconciliation.
    """
    attempt = 0
    delay = _PUBLICATION_RETRY_INITIAL_S
    from hub.metadb import RunStatePublicationRejected
    while True:
        try:
            persist()
            return
        except RunStatePublicationRejected:
            # The owner row was definitively deleted (for example by a winning canvas cascade). This is
            # not commit-unknown: replay must not resurrect it, and the stopped writer can be aborted.
            raise
        except Exception as persist_error:  # noqa: BLE001 - commit outcome is intentionally unknown
            try:
                if receipt():
                    return
                receipt_error = None
            except Exception as exc:  # noqa: BLE001 - database unavailability is also unknown
                receipt_error = exc
            attempt += 1
            if on_retry is not None:
                on_retry(attempt)
            # Log on the first and exponentially-spaced attempts: persistent invariant failures remain
            # visible without flooding production logs during an outage.
            if attempt == 1 or attempt & (attempt - 1) == 0:
                detail = (f"; receipt failed: {receipt_error}"
                          if receipt_error is not None else "; receipt not yet visible")
                logging.getLogger("hub").warning(
                    "local full-result terminal publication remains uncertain (attempt %d): %s%s",
                    attempt, persist_error, detail)
            wait(delay)
            delay = min(_PUBLICATION_RETRY_MAX_S, delay * 2)


class _CancelToken:
    """Internal cancel Event plus an optional external predicate (the subprocess cancel-request file)."""

    def __init__(self, external=None):
        self._event = threading.Event()
        self._external = external

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        try:
            return bool(self._external and self._external())
        except Exception:  # noqa: BLE001 — a broken external probe must not fail the run
            return False


class LocalRunner:
    name = "local-out-of-core"
    cancel_acknowledges_stop = True  # cancelled is set only after the adapter can no longer publish

    def __init__(self, resolve_adapter, registry, catalog, workspace: str, node_builders=None,
                 node_specs=None, storage=None):
        self.resolve_adapter = resolve_adapter
        self.registry = registry
        self.catalog = catalog
        self.workspace = workspace
        # where committed outputs go (pluggable; local dir by default). Falls back to workspace/outputs.
        from hub.storage import make_storage
        self.storage = storage if storage is not None else make_storage(workspace)
        self.on_complete = None  # optional (graph, target, status) hook — Deps wires it to run-history persistence
        self.on_status = None    # optional (graph, status) hook fired on each transition — DB-backed live status
        # optional result-cache hooks (Deps wires them to the DB-backed content-addressed store, so
        # reuse survives restart + is shared across instances). Absent → fall back to the in-process dict.
        self.result_get = None   # (key) -> {uri, table, rows} | None
        self.result_acquire = None  # (key, owner, ttl) -> (doc, temporary ownership pin)
        self.result_put = None   # (key, doc) -> None
        self.forced_result_uri = None  # parent-owned exact attempt URI for metadata-isolated workers
        self.forced_result_namespace_identity: tuple[int, int] | None = None
        # A metadata-isolated worker receives every logical sink target from its parent. Managed object
        # sinks additionally receive the exact parent-allocated physical attempt; the child may write that
        # attempt, but must never allocate or publish lifecycle state in its disposable metadata DB.
        # None means a normal in-process runner. An empty dict is an authoritative "no write sinks" contract.
        self.forced_sink_targets: dict[str, str] | None = None
        self.forced_sink_attempts: dict[str, str] = {}
        # A metadata-isolated child cannot validate a parent-owned managed source against its disposable
        # database. Its parent holds the renewable read leases and passes only the exact physical URIs it
        # attested before dispatch. None means this is a normal in-process runner; an empty set is an
        # authoritative child contract with no managed sources.
        self.parent_attested_source_uris: frozenset[str] | None = None
        # keep the SAME dict object deps passes (plugins fill it AFTER construction) — an
        # empty {} is falsy, so `or {}` would rebind a new dict and drop plugin lowerings.
        self.node_builders = node_builders if node_builders is not None else {}
        self.node_specs = node_specs if node_specs is not None else {}
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, _CancelToken] = {}
        self._scopes: dict[str, object] = {}  # run_id -> db._Scope, so cancel interrupts THIS run's cursor
        self._cache: dict[str, dict] = {}
        self._owned_result_uris: dict[str, str] = {}
        self._lock = threading.Lock()
        # Injectable for deterministic commit-unknown tests; production uses bounded exponential sleeps.
        self.publication_retry_wait = time.sleep

    def can_run(self, plan: CompilePlan) -> bool:
        return plan.acyclic

    # -- estimate ---------------------------------------------------------- #
    def estimate(self, plan: CompilePlan, rows: int | None, byts: int | None = None) -> RunEstimate:
        # No fabricated ETA — a per-op seconds guess is uncalibrated and misleadingly precise. The honest
        # cost signal is DATA VOLUME: confirm on estimated bytes (primary), because a 5M-row pass over one
        # int (~20 MB) is trivial while 200k wide rows can be gigabytes — the old pure row gate misfired on
        # both. Fall back to the row count when byte size is unknown. Unknown-and-uncountable means the
        # source can't be scanned either → the run fails fast → no confirm gate.
        placement: Placement = "local"  # the only backend today; a cluster runner (plugin) sets its own
        if rows is None and byts is None:
            return RunEstimate(rows=None, bytes=None, placement=placement, needs_confirm=False,
                               breakdown=f"size unknown · {len(plan.steps)} steps · out-of-core")
        # EITHER signal trips the gate: large estimated bytes (catches few-but-WIDE rows the row count
        # misses) OR a large row count (the width estimate under-counts variable-length strings/blobs, so
        # the row threshold stays a floor). Neither subsumes the other.
        needs = (byts is not None and byts >= _CONFIRM_BYTES) or (rows is not None and rows >= _CONFIRM_ROWS)
        size = _fmt_bytes(byts) if byts is not None else "size unknown"
        rowstr = f"{rows:,} rows" if rows is not None else "unknown rows"
        return RunEstimate(rows=rows, bytes=byts, placement=placement, needs_confirm=needs,
                           breakdown=f"{size} · {rowstr} · {len(plan.steps)} steps · out-of-core")

    # -- plan hash (content addressing) — shared logic in hub.plan_key ------ #
    def _plan_hash(self, graph: Graph, target: str | None) -> str:
        from hub.plan_key import plan_hash
        return plan_hash(graph, target, self.resolve_adapter)

    def _plan_cacheable(self, graph: Graph, target: str | None) -> bool:
        from hub.plan_key import plan_cacheable
        return plan_cacheable(graph, target, self.node_builders)

    def _cache_get(self, key: str) -> dict | None:
        if self.result_get:  # DB-backed shared/persistent store (Deps-wired)
            try:
                return self.result_get(key)
            except Exception:  # noqa: BLE001 — a cache miss is always safe (recompute)
                return None
        with self._lock:
            return self._cache.get(key)

    def _cache_acquire(
            self, key: str, owner: str, ttl_seconds: float, run_id: str | None = None):
        """Read a cache hit with a temporary durable owner when it points at a managed attempt."""
        doc = None
        cache_pin = None
        if self.result_acquire:
            try:
                doc, pin_id = self.result_acquire(key, owner, ttl_seconds)
                if pin_id:
                    from hub.handoff import ManagedResultCachePinGuard
                    cache_pin = ManagedResultCachePinGuard(pin_id, ttl_seconds)
            except Exception:  # noqa: BLE001 — an unavailable/stale cache safely recomputes
                return None, None
        else:
            doc = self._cache_get(key)
        if doc and doc.get("uri"):
            from hub import metadb
            if metadb.object_attempt_uri_shape(doc["uri"]):
                if cache_pin is None:
                    return None, None  # managed reuse requires an atomic DB-backed ownership pin
            acquire_local = getattr(self.storage, "acquire_result_read", None)
            managed_local = getattr(self.storage, "requires_result_read", None)
            if (callable(acquire_local) and callable(managed_local)
                    and managed_local(doc["uri"])):
                local_guard = None
                try:
                    local_guard = acquire_local(doc["uri"], f"cache:{run_id or owner}")
                    # The ephemeral reader is acquired before this first existence check. A ready
                    # registry row with a missing file is a cache miss, not a lease that survives forever.
                    if not self._output_exists(doc["uri"]):
                        local_guard.close()
                        if cache_pin is not None:
                            cache_pin.close()
                        return None, None
                except Exception:  # metadata/lock uncertainty is a safe cache miss
                    if local_guard is not None:
                        try:
                            local_guard.close()
                        except Exception:  # retain its lock on metadata uncertainty
                            pass
                    if cache_pin is not None:
                        cache_pin.close()
                    return None, None
                cache_pin = local_guard
        return doc, cache_pin

    def _cache_put(self, key: str, doc: dict) -> None:
        if self.result_put:
            try:
                self.result_put(key, doc)
            except Exception:  # noqa: BLE001 — never let caching break a successful run
                from hub import metadb
                if doc.get("uri") and metadb.object_attempt_is_managed(doc["uri"]):
                    raise
                pass
            return
        with self._lock:
            self._cache[key] = doc

    def _output_exists(self, uri: str) -> bool:
        """Store-aware existence: a persisted result pointer is only reusable if its artifact is still
        there. os.path.exists is wrong for object stores (s3/gs) — probe via the adapter instead. On
        ANY uncertainty return False, so we RECOMPUTE rather than serve a missing/stale artifact."""
        if not uri:
            return False
        if "://" not in uri or uri.startswith("file://"):
            p = uri[len("file://"):] if uri.startswith("file://") else uri
            if os.path.isdir(p):  # a worker-direct write lands a DIRECTORY of shards — exists iff non-empty
                import glob as _glob
                return bool(_glob.glob(os.path.join(p, "**", "*.parquet"), recursive=True))
            return os.path.exists(p)
        try:
            self.resolve_adapter(uri).schema(uri)  # reads object metadata; missing → raises → recompute
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- run --------------------------------------------------------------- #
    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement, run_id: str | None = None, cancel_check=None,
            request_id: str | None = None, attempt_id: str | None = None) -> RunStatus:
        if sum(step.kind == "write" for step in plan.steps) > 1:
            raise RuntimeError(
                "local runs support one write sink until atomic multi-sink publication is enabled")
        if any(step.kind == "write" for step in plan.steps):
            from hub.plugins.catalog import unmanaged_publication_supported
            if not unmanaged_publication_supported(self.catalog):
                raise RuntimeError(
                    "local write sinks require catalog registration with read-back support")
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"  # a kernel passes the hub-minted id (authoritative)
        per_node = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        # attempt_id is accepted for OPS-01 port parity (managed publication stamps its own attempts).
        _ = attempt_id
        status = RunStatus(run_id=run_id, status="queued", placement=placement, per_node=per_node,
                           target_node_id=target_node_id, request_id=request_id)
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = _CancelToken(cancel_check)
            self._evict()
        self._emit(graph, status)  # persist 'queued' before the worker starts (pollable on any instance)
        threading.Thread(target=self._execute, args=(run_id, plan, graph, target_node_id), daemon=True).start()
        return status

    def _emit(self, graph: Graph, status: RunStatus, *, strict: bool = False) -> None:
        """Fire the on_status hook (DB-backed live status); never let persistence break a run."""
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise

    def _complete(self, graph: Graph, target: str | None, status: RunStatus, *, strict: bool = False) -> None:
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise

    def _evict(self) -> None:
        """Bound retained run/cancel/cache state (called under self._lock). Dicts keep insertion order.
        Evict only TERMINAL runs (oldest first) — never a queued/running one, so a long run submitted
        early isn't dropped mid-flight by 100 later submissions (which would 404 its status poll)."""
        _terminal = {"done", "failed", "cancelled"}
        while len(self.runs) > _MAX_RUNS:
            victim = next((rid for rid, st in self.runs.items() if st.status in _terminal), None)
            if victim is None:
                break  # everything retained is still in-flight — exceed the cap rather than drop a live run
            self.runs.pop(victim, None)
            self._cancel.pop(victim, None)
            self._scopes.pop(victim, None)
        while len(self._cache) > _MAX_RUNS:
            self._cache.pop(next(iter(self._cache)), None)

    def _execute(self, run_id: str, plan: CompilePlan, graph: Graph, target: str | None) -> None:
        status = self.runs[run_id]
        cancel = self._cancel[run_id]
        started = time.time()
        status.status = "running"
        self._emit(graph, status)
        try:
            deadline = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
        except ValueError:
            deadline = 3600.0
        ttl = max(300.0, deadline + 300.0)
        read_leases = contextlib.ExitStack()
        read_guards = []
        cache_pin = None
        try:
            # Fingerprinting for the plan hash is already a real source read. Acquire every exact source
            # guard before it, then retain the guards through the final lazy scan and publication fence.
            from hub.handoff import (
                has_attempt_path_component, is_attempt_uri, managed_read_lease)
            from hub.storage import preflight_managed_execution_sources
            source_uris = preflight_managed_execution_sources(
                self.storage, g.execution_source_uris(graph, target))
            for uri in source_uris:
                normalized = str(uri).rstrip("/")
                canonical = (normalized[len("file://"):]
                             if normalized.startswith("file://") else normalized)
                if (self.parent_attested_source_uris is not None
                        and (normalized in self.parent_attested_source_uris
                             or canonical in self.parent_attested_source_uris)):
                    continue
                if (has_attempt_path_component(normalized)
                        and not is_attempt_uri(normalized)):
                    raise FileNotFoundError(
                        "managed source must reference the exact attempt root")
                acquire_local = getattr(self.storage, "acquire_result_read", None)
                managed_local = getattr(self.storage, "requires_result_read", None)
                if (callable(acquire_local) and callable(managed_local)
                        and managed_local(normalized)):
                    read_guards.append(read_leases.enter_context(
                        acquire_local(normalized, f"run-source:{run_id}")))
                else:
                    read_guards.append(read_leases.enter_context(managed_read_lease(
                        normalized, owner=f"run:{run_id}", ttl_seconds=ttl)))
            phash = self._plan_hash(graph, target)
            cacheable = self._plan_cacheable(graph, target)
            cached, cache_pin = self._cache_acquire(
                phash, f"run:{run_id}", ttl, run_id) if cacheable else (None, None)
            engine = BuildEngine(graph, self.resolve_adapter, self.registry, full=True,
                                 node_builders=self.node_builders, node_specs=self.node_specs,
                                 pushdown=True, output_node=target)
        except Exception as exc:  # source ownership/fingerprint failure precedes the main execution scope
            with contextlib.suppress(Exception):
                read_leases.close()
            if cache_pin is not None:
                with contextlib.suppress(Exception):
                    cache_pin.close()
            status.status = "cancelled" if cancel.is_set() else "failed"
            status.error = None if cancel.is_set() else f"{type(exc).__name__}: {exc}"
            for item in status.per_node:
                item.status = status.status
            status.ms = int((time.time() - started) * 1000)
            status.total_rows = 0
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
                self._scopes.pop(run_id, None)
            self._complete(graph, target, status)
            return
        terminal_persisted = False
        terminal_rejected = False
        provisional_result: RunStatus | None = None
        nm = g.node_map(graph)
        rows_seen = 0
        # Bind the write destination's object-store credential BEFORE the run scope opens, so the scope
        # cursor snapshots that secret (DuckDB freezes a cursor's secret view at transaction start).
        run_object_store_cfg = self._run_object_store_cfg(plan, nm)
        # Run on our OWN DuckDB cursor (db.run_scope), NOT the process-global lock: a long run no
        # longer serializes every other user's preview/sample/run, and a failure here can't wedge them.
        with db.object_store_binding(run_object_store_cfg), db.run_scope() as scope:
            with self._lock:
                self._scopes[run_id] = scope  # cancel() interrupts this scope's cursor
            try:
                last_step_published = False
                for step in plan.steps:
                    last_step_published = False
                    if cache_pin is not None:
                        cache_pin.check()
                    for guard in read_guards:
                        guard.check()
                    if cancel.is_set():
                        status.status = "cancelled"
                        for p in status.per_node:
                            if p.status != "done":
                                p.status = "cancelled"
                        return
                    pn = next((p for p in status.per_node if p.node_id == step.node_id), None)
                    if pn:
                        pn.status = "running"
                    t0 = time.time()
                    if step.kind == "write":
                        write_fence_passed = False

                        def pre_publish(*, check_cancel: bool) -> None:
                            nonlocal write_fence_passed
                            if cache_pin is not None:
                                cache_pin.check()
                            for guard in read_guards:
                                guard.check()
                            if check_cancel and cancel.is_set():
                                raise RuntimeError("run cancelled before output publication")
                            write_fence_passed = True

                        rows_seen = self._commit_write(
                            nm[step.node_id], graph, engine, status, cached, cancel,
                            pre_publish=pre_publish)
                        last_step_published = write_fence_passed
                    elif step.kind == "assert":
                        rows_seen = self._check_assert(nm[step.node_id], engine)  # violation count; may raise
                    else:
                        engine.relation(step.node_id)  # build (lazy) — cheap
                        self._check_schema(nm[step.node_id], engine)  # enforce a pinned contract (may raise)
                    if pn:
                        pn.status = "done"
                        pn.ms = int((time.time() - t0) * 1000)
                        pn.rows = rows_seen or None
                    status.rows_processed = rows_seen
                    status.progress = _step_progress(status)  # fraction of steps complete (deterministic)
                    self._emit(graph, status)  # per-node progress → DB (cross-instance polling sees it advance)

                # if the target is not a sink, MATERIALIZE its full result to a durable artifact (not just
                # count it) so the UI can page the exact rows a full pass produced — the aggregate/sort a
                # sample can't preview — and it survives a restart (P0-UX-01). `assert` is excluded: its
                # "rows" is the violation count from _check_assert (already set), and re-counting would
                # rebuild — and re-raise — a warn-severity assert's un-evaluable predicate (defeating warn).
                if target and nm.get(target) and nm[target].type not in ("write", "assert"):
                    # Keep the ready artifact binding private until its exact RunState owner commits.
                    # ``self.runs`` points at ``status`` and is polled concurrently, so materializing into
                    # that object would expose an unowned URI during a slow/unknown terminal commit.
                    provisional_result = status.model_copy(deep=True)
                    rows_seen = self._materialize_result(
                        target, engine, provisional_result, cached, phash, cancel)
                    status.rows_processed = rows_seen
                    last_step_published = False

                # Resolve the last-instruction race explicitly. If no output crossed its commit point,
                # cancellation wins before `done`. Once output_uri is set, publication/reuse already won;
                # report done truthfully (the CLI still exits 124/130 and includes that terminal state)
                # rather than claim cancelled while a committed artifact exists.
                if cancel.is_set():
                    from hub import metadb
                    local_managed = getattr(self.storage, "is_managed_result_uri", None)
                    provisional_uri = (provisional_result.output_uri
                                       if provisional_result is not None else status.output_uri)
                    managed_full_result = bool(
                        target and nm.get(target) and nm[target].type not in ("write", "assert")
                        and provisional_uri
                        and (metadb.object_attempt_uri_shape(provisional_uri)
                             or (callable(local_managed) and local_managed(provisional_uri))))
                    if managed_full_result:
                        with self._lock:
                            owned_local = self._owned_result_uris.get(run_id)
                        if owned_local == provisional_uri:
                            try:
                                self.storage.abort_result(owned_local, run_id)
                                with self._lock:
                                    self._owned_result_uris.pop(run_id, None)
                            except Exception:  # retain the writer fence for later reconciliation
                                logging.getLogger("hub").exception(
                                    "managed local result cancellation cleanup failed")
                        elif metadb.object_attempt_uri_shape(provisional_uri):
                            _safe_abandon_attempt(provisional_uri)
                        if provisional_result is not None:
                            provisional_result.output_uri = provisional_result.output_table = None
                        status.output_uri = status.output_table = None
                    if managed_full_result or not provisional_uri:
                        raise RuntimeError("run cancelled before completion")
                if not last_step_published:
                    if cache_pin is not None:
                        cache_pin.check()
                    for guard in read_guards:
                        guard.check()

                # set the final counts BEFORE flipping to 'done' — a client polls in another thread and
                # reads a terminal status eagerly; the finally sets these too late, so a poll could
                # otherwise observe status='done' with total_rows still None or ms still 0 (a flaky race).
                status.total_rows = rows_seen
                status.progress = 1.0
                status.stalled = False
                status.ms = int((time.time() - started) * 1000)
                from hub import metadb
                local_managed = getattr(self.storage, "is_managed_result_uri", None)
                result_binding = provisional_result or status
                result_uri = result_binding.output_uri
                managed_result = bool(
                    target and nm.get(target) and nm[target].type not in ("write", "assert")
                    and result_uri
                    and (metadb.object_attempt_uri_shape(result_uri)
                         or (callable(local_managed) and local_managed(result_uri))))
                if managed_result:
                    if self.on_status is None and not self.forced_result_uri:
                        raise RuntimeError(
                            "managed full results require authoritative RunState persistence")
                    # RunState is the primary owner for a full result. Publish it before the optional
                    # cache pointer, keep any cache-hit pin through best-effort history persistence, and
                    # never expose terminal done when the primary durable terminal write fails.
                    persisted = status.model_copy(deep=True)
                    persisted.output_uri = result_binding.output_uri
                    persisted.output_table = result_binding.output_table
                    persisted.status = "done"
                    persisted_doc = persisted.model_dump()
                    local_result = bool(
                        callable(local_managed) and local_managed(result_uri))

                    def publication_retry(_attempt: int) -> None:
                        # Update only the in-memory observation.  Do not emit a different DB document
                        # while the exact done transaction's outcome is unresolved.
                        with self._lock:
                            status.status = "running"
                            status.stalled = True
                            status.error = "terminal publication is retrying"
                            status.output_uri = status.output_table = None

                    if local_result:
                        _persist_local_result_done(
                            lambda: self._emit(graph, persisted, strict=True),
                            lambda: self.storage.result_publication_receipt(
                                result_uri, run_id, persisted_doc),
                            on_retry=publication_retry,
                            wait=self.publication_retry_wait)
                    else:
                        try:
                            self._emit(graph, persisted, strict=True)
                        except metadb.RunStatePublicationRejected:
                            raise
                        except Exception as exc:
                            logging.getLogger("hub").exception(
                                "managed full result publication failed")
                            raise RuntimeError("full result publication failed") from exc
                    terminal_persisted = True
                    # One locked object update makes done+binding visible together only after durable
                    # success/receipt.  ``persisted`` was never mutated across retries.
                    with self._lock:
                        status.__dict__.update(persisted.__dict__)
                    self._complete(graph, target, status)
                else:
                    if provisional_result is not None:
                        status.output_uri = provisional_result.output_uri
                        status.output_table = provisional_result.output_table
                    status.status = "done"
                if cacheable:
                    try:
                        self._cache_put(phash, {
                            "rows": rows_seen, "uri": status.output_uri,
                            "table": status.output_table})
                    except Exception:  # the durable RunState/history owners already committed
                        pass
            except Exception as e:  # noqa: BLE001
                from hub import metadb
                terminal_rejected = isinstance(e, metadb.RunStatePublicationRejected)
                with self._lock:
                    owned_local = self._owned_result_uris.get(run_id)
                if owned_local is not None:
                    try:
                        self.storage.abort_result(owned_local, run_id)
                        with self._lock:
                            self._owned_result_uris.pop(run_id, None)
                    except Exception:  # metadata uncertainty retains the process/file fence
                        logging.getLogger("hub").exception(
                            "managed local result abort failed after run error")
                    if provisional_result is not None and provisional_result.output_uri == owned_local:
                        provisional_result.output_uri = provisional_result.output_table = None
                    if status.output_uri == owned_local:
                        status.output_uri = status.output_table = None
                failed_binding = provisional_result or status
                cached_local_uri = getattr(cache_pin, "uri", None)
                status_local_uri = (failed_binding.output_uri[len("file://"):]
                                    if (failed_binding.output_uri
                                        and failed_binding.output_uri.startswith("file://"))
                                    else failed_binding.output_uri)
                if cached_local_uri is not None and status_local_uri == cached_local_uri:
                    failed_binding.output_uri = failed_binding.output_table = None
                    status.output_uri = status.output_table = None
                if (failed_binding.output_uri
                        and metadb.object_attempt_uri_shape(failed_binding.output_uri)):
                    _safe_abandon_attempt(failed_binding.output_uri)
                    failed_binding.output_uri = failed_binding.output_table = None
                    status.output_uri = status.output_table = None
                if cancel.is_set():
                    status.status = "cancelled"  # an interrupted step is a cancel, not a failure
                    for p in status.per_node:  # settle interrupted + not-yet-started nodes too
                        if p.status != "done":
                            p.status = "cancelled"
                else:
                    status.status = "failed"
                    msg = f"{type(e).__name__}: {e}"
                    hint = _diagnose(str(e))
                    tail = f"\nHint: {hint}" if hint else ""
                    # steps run sequentially, so the one still 'running' is exactly where it broke — attribute
                    # the error THERE (the card + per-node list show which node failed and why), not just a
                    # global banner. Relational ops build lazily, so a bad-column/type error can instead
                    # surface at the final forced count (all steps already 'done') → attribute to the target.
                    # A diagnostic hint maps common error classes to an actionable next step.
                    failed = next((p for p in status.per_node if p.status == "running"), None)
                    if failed is None and target:
                        failed = next((p for p in status.per_node if p.node_id == target), None)
                    if failed is not None:
                        failed.status = "failed"
                        failed.error = msg + tail
                        status.error = f"at '{failed.label or failed.node_id}': {msg}{tail}"
                    else:
                        status.error = msg + tail
                    for p in status.per_node:
                        if p.status == "running":
                            p.status = "failed"
            finally:
                try:
                    read_leases.close()
                except Exception:  # noqa: BLE001 — lease expiry is the safe fallback
                    logging.getLogger("hub").exception(
                        "managed read lease cleanup failed after local run")
                # scope exit (below) rolls back + drops this run's views on its own cursor
                for pth in engine.spill_files:  # GC temp parquet spilled this run (outputs already committed)
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
                status.ms = int((time.time() - started) * 1000)
                status.total_rows = rows_seen
                if not terminal_persisted and not terminal_rejected:
                    self._emit(graph, status)  # persist terminal state after all commit-point work
                with self._lock:
                    self._cancel.pop(run_id, None)  # done/failed/cancelled → drop the cancel Event
                    self._scopes.pop(run_id, None)
                if not terminal_persisted:
                    self._complete(graph, target, status)
                if cache_pin is not None:
                    try:
                        cache_pin.close()
                    except Exception:  # retain its process fence on metadata uncertainty
                        logging.getLogger("hub").exception(
                            "managed result cache reader release failed")
                with self._lock:
                    owned_local = self._owned_result_uris.get(run_id)
                if terminal_persisted and owned_local is not None:
                    try:
                        if self.storage.release_result(owned_local, run_id):
                            with self._lock:
                                self._owned_result_uris.pop(run_id, None)
                    except Exception:  # retain the writer fd on publication uncertainty
                        logging.getLogger("hub").exception(
                            "managed local result writer release failed")

    def _count(self, engine: BuildEngine, node_id: str, cached: dict | None) -> int:
        if cached and cached.get("rows") is not None:
            return cached["rows"]
        return int(engine.relation(node_id).aggregate("count(*) AS n").fetchone()[0])

    @staticmethod
    def _adapter_write(adapter, uri: str, rel, mode: str, cancel: _CancelToken, **kwargs) -> dict:
        """Invoke an adapter with the optional cancellation fence when it supports that seam.

        Older/plugin adapters keep their existing signature. They still receive a pre-call cancellation
        check, but only adapters accepting ``cancelled`` can fence a long write at its publish point.
        """
        if cancel.is_set():
            raise RuntimeError("run cancelled before output commit")
        try:
            params = inspect.signature(adapter.write).parameters.values()
            supports_fence = any(p.name == "cancelled" or p.kind == inspect.Parameter.VAR_KEYWORD
                                 for p in params)
        except (TypeError, ValueError):
            supports_fence = False
        if supports_fence:
            kwargs["cancelled"] = cancel.is_set
        return adapter.write(uri, rel, mode, **kwargs)

    def _materialize_result(self, node_id: str, engine: BuildEngine, status: RunStatus,
                            cached: dict | None, phash: str, cancel: _CancelToken) -> int:
        """A non-write target's full result → a durable parquet artifact so the UI can page the exact
        rows a full pass produced (an aggregate/sort a sample can't preview) and they survive a restart
        (P0-UX-01). CONTENT-ADDRESSED by the plan hash (`__result_<phash>`): identical content shares one
        cache pointer is content-addressed, while every cache miss writes a unique immutable physical
        artifact. NOT registered in the catalog — it's a run result, not a user-published dataset.
        Materialization is part of the run contract: a write/commit failure propagates and fails the run
        instead of reporting `done` with an artifact the UI cannot reopen."""
        forced_uri = self.forced_result_uri
        if not forced_uri and cached and cached.get("uri") and self._output_exists(cached["uri"]):
            status.output_uri = cached["uri"]
            status.output_table = cached.get("table")
            return int(cached.get("rows") or 0)
        rel = engine.relation(node_id)
        from hub.plugins.adapters import is_object_uri
        begin_local = getattr(self.storage, "begin_result", None)
        managed_local = getattr(self.storage, "requires_result_read", None)
        parent_owned_local = bool(
            forced_uri and callable(managed_local) and managed_local(forced_uri))
        if parent_owned_local:
            validate = getattr(self.storage, "validate_result_uri", None)
            if not callable(validate):
                raise RuntimeError("parent-provided local result has no namespace validator")
            validate(forced_uri, self.forced_result_namespace_identity)
            logical_uri = uri = forced_uri
        elif not forced_uri and callable(begin_local):
            uri = logical_uri = begin_local(phash, status.run_id)
            with self._lock:
                self._owned_result_uris[status.run_id] = uri
            validate = getattr(self.storage, "validate_result_uri", None)
            if callable(validate):
                validate(uri)
        else:
            logical_uri = forced_uri or self.storage.output_uri(
                f"__result_{phash}", ".parquet")
            uri = logical_uri
        if is_object_uri(logical_uri):
            from hub.handoff import (allocate_attempt, is_attempt_uri, physical_attempt_uri,
                                     write_manifest)
            parent_owned = bool(forced_uri)
            if parent_owned:
                if not is_attempt_uri(logical_uri):
                    raise RuntimeError("parent-provided object result URI is not an exact attempt")
            else:
                handle = allocate_attempt(
                    logical_uri=logical_uri, kind="region", run_id=status.run_id,
                    allocation_key=f"full-result:{status.run_id}:{phash}",
                    uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                        logical_uri, namespace, generation, attempt_id),
                )
                uri = handle["uri"]
            data_uri = uri.rstrip("/") + "/part-00000.parquet"
            try:
                res = self._adapter_write(
                    self.resolve_adapter(data_uri), data_uri, rel, "overwrite", cancel)
                schema = list(zip(rel.columns, (str(t) for t in rel.types)))
                write_manifest(
                    uri, run_id=status.run_id, rows=int(res.get("rows") or 0), schema=schema)
                if not parent_owned:
                    from hub.handoff import prepare_attempt_commit
                    prepare_attempt_commit(uri)
                res = {**res, "uri": uri}
            except Exception:
                if not parent_owned:
                    from hub.handoff import discard_attempt
                    discard_attempt(uri)  # this synchronous writer has stopped; terminal proof is valid
                raise
        elif parent_owned_local:
            res = self._adapter_write(
                self.resolve_adapter(uri), uri, rel, "overwrite", cancel)
            if res.get("uri", uri) != uri:
                raise RuntimeError("adapter did not write the exact parent-reserved local result")
        elif callable(begin_local) and callable(managed_local) and managed_local(uri):
            try:
                res = self._adapter_write(
                    self.resolve_adapter(uri), uri, rel, "overwrite", cancel)
                if res.get("uri", uri) != uri:
                    raise RuntimeError("adapter did not write the exact reserved local result")
                self.storage.commit_result(uri, status.run_id)
            except Exception:
                try:
                    self.storage.abort_result(uri, status.run_id)
                    with self._lock:
                        self._owned_result_uris.pop(status.run_id, None)
                except Exception:  # retain metadata/fd ownership when cleanup is uncertain
                    logging.getLogger("hub").exception(
                        "managed local result write cleanup failed")
                raise
        else:
            res = self._adapter_write(self.resolve_adapter(uri), uri, rel, "overwrite", cancel)
        status.output_uri = res.get("uri", uri)
        status.output_table = None  # a run result, not a catalog table (don't clutter the Tables view)
        return int(res.get("rows") or 0)

    def _check_assert(self, node, engine: BuildEngine) -> int:
        """A data-quality gate: the node's relation is the VIOLATING rows (predicate not TRUE). Count them;
        on severity='error' with any violation, raise so the run fails with an actionable message (the
        offending rows are inspectable by previewing the node). 'warn' just records the count."""
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        severity = cfg.get("severity")
        title = (node.data.get("title") if isinstance(node.data, dict) else None) or node.id
        try:
            viol = int(engine.relation(node.id).aggregate("count(*) AS n").fetchone()[0])
        except Exception as e:  # noqa: BLE001 — a predicate that isn't a per-row boolean (missing column,
            # non-castable value, aggregate) can't evaluate. 'warn' is non-blocking, so it must NOT fail the
            # run (the non-enforcing column-reference warning already flags a bad column on the card).
            if severity == "error":
                raise RuntimeError(f"assert '{title}' could not evaluate its predicate: {e}") from e
            return 0
        if severity == "error" and viol > 0:
            raise RuntimeError(f"assert '{title}' failed: {viol} row(s) violate the check")
        return viol

    def _check_schema(self, node, engine: BuildEngine) -> None:
        """Enforce a pinned schema contract: when config.enforceSchema is set, compare the built relation's
        ACTUAL columns to the declared/referenced contract and FAIL the run on drift (missing / unexpected
        columns, or a type the actual doesn't satisfy) — turning silent schema drift into an actionable
        error. Names are compared strictly; types are compared with the contract's own specificity via
        type_satisfies (a coarse contract stays lenient; a precise one — decimal(p,s), timestamp unit/tz,
        list/struct/map element types — is enforced faithfully). A node with no enforce flag / no
        contract is a no-op."""
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        if not cfg.get("enforceSchema"):
            return
        from hub.executors.engine import canonical_type, declared_schema, type_satisfies
        contract = declared_schema(node)
        title = (node.data.get("title") if isinstance(node.data, dict) else None) or node.id
        if not contract:
            # enforce is ON but there's no resolvable contract (a deleted/renamed ref, or none declared) —
            # do NOT silently pass: a safety gate that quietly turns itself off is worse than no gate.
            osc = cfg.get("outputSchema")
            ref = osc.get("ref") if isinstance(osc, dict) else None
            why = f"referenced contract '{ref}' not found" if ref else "no contract is declared"
            raise RuntimeError(f"schema contract on '{title}' can't be enforced — {why}")
        rel = engine.relation(node.id)
        actual = {n: str(t) for n, t in zip(rel.columns, rel.types)}
        declared = {c["name"] for c in contract}
        missing = [c["name"] for c in contract if c["name"] not in actual]
        unexpected = [n for n in actual if n not in declared]
        changed = [{"name": c["name"], "from": str(c.get("type", "")), "to": actual[c["name"]]}
                   for c in contract if c["name"] in actual
                   and not type_satisfies(canonical_type(c.get("type", "")), canonical_type(actual[c["name"]]))]
        if missing or unexpected or changed:
            parts = []
            if missing:
                parts.append(f"missing {missing}")
            if unexpected:
                parts.append(f"unexpected {unexpected}")
            if changed:
                parts.append("type-changed " + str([f"{c['name']}:{c['from']}→{c['to']}" for c in changed]))
            raise RuntimeError(f"schema contract on '{title}' violated — {'; '.join(parts)}")

    def _run_object_store_cfg(self, plan: CompilePlan, nm: dict) -> dict | None:
        """Resolved object-store credentials for this run's write destination (the first object-store
        sink with a resolvable cred), or None. Bound before the scope so writes use that credential."""
        from hub import destinations
        from hub.sinks import SinkSpec
        for step in plan.steps:
            if step.kind != "write":
                continue
            node = nm.get(step.node_id)
            if node is None:
                continue
            data = node.data if isinstance(node.data, dict) else {}
            spec = SinkSpec.from_config(data.get("config", {}), data.get("title"))
            cfg = destinations.object_store_cred_cfg(self.workspace, spec.destination_id)
            if cfg:
                return cfg
        return None

    def _commit_write(self, node, graph: Graph, engine: BuildEngine, status: RunStatus,
                      cached: dict | None, cancel: _CancelToken, pre_publish=None) -> int:
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        from hub import destinations
        from hub.sinks import SinkCommit, SinkSpec, commit_sink, preflight_sink
        spec = SinkSpec.from_config(cfg, node.data.get("title") if isinstance(node.data, dict) else None)
        if cancel.is_set():
            raise RuntimeError("run cancelled before output commit")
        # Bind this destination's credential at the object-store open (the scope cursor already
        # snapshotted it via the run-level binding; this makes the per-write credential explicit).
        os_cfg = destinations.object_store_cred_cfg(self.workspace, spec.destination_id)
        if os_cfg is not None:
            with db.lock():
                db.ensure_object_store(os_cfg)
        # content-addressed skip: an identical overwrite plan already wrote this, so re-running is a
        # no-op. append is NOT idempotent (it must add a part every run), so it never uses the cache.
        if spec.mode != "append" and cached and cached.get("table") and cached.get("uri") and self._output_exists(cached["uri"]):
            status.output_uri, status.output_table = cached["uri"], cached["table"]
            return int(cached.get("rows") or 0)

        inc = g.incoming(graph, node.id)
        if not inc:
            return 0
        # route by the wired output PORT (source_handle) — a write off a multi-output node must
        # persist that port's data, not the default/first one. Mirrors BuildEngine._inputs.
        parent_rel = engine.relation(inc[0].source, inc[0].source_handle)
        parent_contract = self.forced_sink_targets is not None
        if parent_contract and node.id not in self.forced_sink_targets:
            raise RuntimeError(f"parent did not authorize sink '{node.id}'")
        forced_target = self.forced_sink_targets.get(node.id) if parent_contract else None
        if parent_contract and (not isinstance(forced_target, str) or not forced_target):
            raise RuntimeError(f"parent supplied an invalid target for sink '{node.id}'")
        logical_uri = preflight_sink(
            spec, self.workspace, self.storage, self.resolve_adapter,
            target_uri=forced_target)
        logical_adapter = self.resolve_adapter(logical_uri)
        managed_parquet = _is_core_managed_sink(spec, logical_uri, logical_adapter)
        parent_assigned_attempt = self.forced_sink_attempts.get(node.id)
        if parent_assigned_attempt and not managed_parquet:
            raise RuntimeError(
                f"parent and child disagree on managed sink '{node.id}'")
        if managed_parquet:
            from hub.handoff import (allocate_attempt, discard_attempt, is_attempt_uri,
                                     physical_attempt_uri, write_manifest)
            parent_owned = parent_contract
            if parent_owned:
                attempt_uri = parent_assigned_attempt
                if not attempt_uri or not is_attempt_uri(attempt_uri):
                    raise RuntimeError(
                        f"parent did not assign an exact managed attempt for sink '{node.id}'")
            else:
                from hub.plugins.catalog import core_managed_publisher
                managed_publisher = core_managed_publisher(self.catalog)
                if managed_publisher is None:
                    raise RuntimeError(
                        "managed object writes require the core transactional catalog publisher")
                handle = allocate_attempt(
                    logical_uri=logical_uri, kind="sink", run_id=status.run_id,
                    allocation_key=f"local-sink:{status.run_id}:{node.id}:{logical_uri}",
                    catalog_key_base=f"tbl_{spec.name}",
                    uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                        logical_uri, namespace, generation, attempt_id),
                )
                attempt_uri = handle["uri"]
            physical_uri = (attempt_uri if spec.partition_by else
                            attempt_uri.rstrip("/") + "/part-00000.parquet")
            kwargs = {"partition_by": spec.partition_by} if spec.partition_by else {}
            try:
                result = self._adapter_write(
                    logical_adapter, physical_uri, parent_rel, spec.mode, cancel, **kwargs)
                rows = int(result.get("rows") or 0)
                schema = list(zip(parent_rel.columns, (str(t) for t in parent_rel.types)))
                write_manifest(
                    attempt_uri, run_id=status.run_id, rows=rows, schema=schema)
                committed = SinkCommit(name=spec.name, uri=attempt_uri, rows=rows)
            except Exception:
                if not parent_owned:
                    discard_attempt(attempt_uri)
                raise
        else:
            committed = commit_sink(
                spec, parent_rel, self.workspace, self.storage, self.resolve_adapter,
                target_uri=forced_target,
                write_adapter=lambda adapter, uri, rel, mode, **kwargs: self._adapter_write(
                    adapter, uri, rel, mode, cancel, **kwargs
                ),
            )

        try:
            if pre_publish is not None:
                # An unmanaged adapter's successful return is already an externally visible mutation;
                # publication wins from that point. Immutable managed attempts can still let cancel win
                # before their pointer is published.
                pre_publish(check_cancel=managed_parquet)
        except Exception:
            if managed_parquet and not parent_owned:
                _safe_abandon_attempt(attempt_uri)
            raise
        parent_uris = g.all_upstream_publication_uris(graph, node.id)
        if not (managed_parquet and parent_contract):
            if managed_parquet:
                publish = managed_publisher
            else:
                from hub.plugins.catalog import publish_unmanaged_output_attested
                publish = lambda **kwargs: publish_unmanaged_output_attested(  # noqa: E731
                    self.catalog, **kwargs)
            try:
                publish(name=committed.name, uri=committed.uri, parents=parent_uris,
                        pipeline="canvas")  # content-addressed version
            except Exception as exc:
                logging.getLogger("hub").exception("sink publication failed")
                if managed_parquet and not parent_owned:
                    _safe_abandon_attempt(attempt_uri)
                raise RuntimeError("sink publication failed") from exc
        status.output_uri = committed.uri
        status.output_table = committed.name
        return committed.rows

    def _source_uri(self, nm_node: str, graph: Graph) -> str | None:
        for n in g.upstream_chain(graph, nm_node):
            if n.type == "source":
                cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
                return cfg.get("uri") or cfg.get("table")
        return None

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel(self, run_id: str) -> RunStatus:
        st = self.runs[run_id]
        # Request cancellation only. The worker publishes the terminal `cancelled` state after it has
        # unwound past every possible commit point, so a caller can treat terminal status as acknowledgement
        # instead of returning while this thread may still publish.
        if st.status in ("queued", "running"):
            if run_id in self._cancel:
                self._cancel[run_id].set()
            scope = self._scopes.get(run_id)
            if scope is not None:
                scope.interrupt()  # abort THIS run's cursor (base-conn interrupt wouldn't touch it)
        return st
