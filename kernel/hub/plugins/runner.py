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
from hub.run_outputs import (
    apply_cached_output,
    apply_cached_outputs,
    commit_output,
    committed_document_outputs,
    committed_output_snapshot,
    discard_unpublished_outputs,
    initialize_run_outputs,
    outputs_cache_document,
    preflight_output_table,
    preflight_run_output_target,
    settle_output,
    settle_uncommitted_outputs,
    sole_committed_document_output,
    sole_output,
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


def _catalog_publication_version(publication) -> str | None:
    """Read the exact version from unmanaged tables or a managed receipt's nested table."""
    version = (publication.get("version") if isinstance(publication, dict)
               else getattr(publication, "version", None))
    if version is None and isinstance(publication, dict):
        table = publication.get("table")
        version = table.get("version") if isinstance(table, dict) else getattr(table, "version", None)
    return None if version is None else str(version)


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
    """Idempotently establish a managed-result terminal owner after an unknown commit outcome.

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
                    "managed-result terminal publication remains uncertain (attempt %d): %s%s",
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


class _TerminalPublicationGate:
    """Linearize cancellation against one managed result's durable terminal owner.

    Cancellation wins while the gate is open. Once terminal publication starts, its exact
    commit/receipt retry owns the outcome and a concurrent late cancel must not delete an artifact
    whose RunState may already have committed.
    """

    def __init__(self) -> None:
        self._phase = "open"
        self._cancel_requested = False
        self._lock = threading.Lock()

    def request_cancel(self) -> bool:
        with self._lock:
            if self._phase != "open":
                return False
            self._cancel_requested = True
            return True

    def begin_publication(self) -> bool:
        with self._lock:
            if self._phase != "open" or self._cancel_requested:
                return False
            self._phase = "publishing"
            return True

    def mark_published(self) -> None:
        with self._lock:
            if self._phase != "publishing":
                raise RuntimeError("terminal publication gate is not publishing")
            self._phase = "published"


class _GuardSet:
    """One all-or-nothing cache ownership fence over every artifact in an output set."""

    def __init__(self, guards: list[object]):
        self.guards = list(guards)

    def check(self) -> None:
        for guard in self.guards:
            guard.check()

    def close(self) -> None:
        first_error: Exception | None = None
        for guard in reversed(self.guards):
            try:
                guard.close()
            except Exception as exc:  # retain every remaining fence before reporting uncertainty
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _committed_result_uris(status: RunStatus) -> list[str]:
    """Return the declaration-ordered durable result identities in one status candidate."""
    return [
        str(output.uri)
        for output in status.outputs
        if (output.publication_kind == "result"
            and output.outcome == "committed"
            and output.uri is not None)
    ]


def _single_committed_rows(status: RunStatus) -> int | None:
    """Project the collection row count only when the public scalar is unambiguous."""
    output = sole_output(status, committed=True)
    return output.rows if output is not None else None


class LocalRunner:
    name = "local-out-of-core"
    cancel_acknowledges_stop = True  # cancelled is set only after the adapter can no longer publish

    def supports_named_multi_output_runs(self) -> bool:
        return True

    @staticmethod
    def supports_selected_destination_credentials() -> bool:
        return True  # this process resolves the selected Cred against the authoritative metadata DB

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
        # A metadata-isolated worker receives one declaration-ordered exact local URI per result port.
        # ``None`` means this runner allocates its own results; an empty list is an invalid child contract.
        self.forced_results: list[dict] | None = None
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
        # Public readers never touch the execution-owned mutable RunStatus in ``runs``.  Writers publish
        # one validated deep snapshot at each coherent _emit boundary, and status()/cancel() copy only
        # that snapshot.  The live object remains available to the worker and synchronous callbacks.
        self._published_statuses: dict[str, RunStatus] = {}
        self._cancel: dict[str, _CancelToken] = {}
        self._terminal_publication_gates: dict[str, _TerminalPublicationGate] = {}
        self._scopes: dict[str, object] = {}  # run_id -> db._Scope, so cancel interrupts THIS run's cursor
        self._cache: dict[str, dict] = {}
        self._owned_result_uris: dict[str, set[str]] = {}
        self._owned_object_result_uris: dict[str, set[str]] = {}
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
        """Read and pin one complete cached output set, or release every member and miss."""
        doc = None
        guards: list[object] = []
        if self.result_acquire:
            try:
                doc, pin_ids = self.result_acquire(key, owner, ttl_seconds)
                if pin_ids:
                    from hub.handoff import ManagedResultCachePinGuard
                    guards.append(ManagedResultCachePinGuard(pin_ids, ttl_seconds))
            except Exception:  # noqa: BLE001 — an unavailable/stale cache safely recomputes
                return None, None
        else:
            doc = self._cache_get(key)

        def miss():
            if guards:
                try:
                    _GuardSet(guards).close()
                except Exception:  # retain best-effort cache semantics on release uncertainty
                    logging.getLogger("hub").exception(
                        "managed result cache miss cleanup failed")
            return None, None

        cached_outputs = committed_document_outputs(doc)
        if doc is not None and not cached_outputs:
            return miss()
        if cached_outputs:
            from hub import metadb
            object_outputs = [output for output in cached_outputs
                              if metadb.object_attempt_uri_shape(str(output.uri))]
            if object_outputs and not guards:
                return miss()  # managed reuse requires one atomic DB-backed pin set
            acquire_local = getattr(self.storage, "acquire_result_read", None)
            managed_local = getattr(self.storage, "requires_result_read", None)
            for index, output in enumerate(cached_outputs):
                uri = str(output.uri)
                managed_object = metadb.object_attempt_uri_shape(uri)
                try:
                    if (callable(acquire_local) and callable(managed_local)
                            and managed_local(uri)):
                        guards.append(acquire_local(
                            uri, f"cache:{run_id or owner}:{index}"))
                    # Managed object pins already attest a published immutable generation and its
                    # committed inventory. Probe ordinary/local artifacts only after their read fence;
                    # one missing member invalidates the whole group.
                    if not managed_object and not self._output_exists(uri):
                        return miss()
                except Exception:  # metadata/lock uncertainty is a safe cache miss
                    return miss()
        return doc, (_GuardSet(guards) if guards else None)

    def _cache_put(self, key: str, doc: dict) -> None:
        if self.result_put:
            try:
                self.result_put(key, doc)
            except Exception:  # noqa: BLE001 — never let caching break a successful run
                from hub import metadb
                outputs = committed_document_outputs(doc)
                if any(metadb.object_attempt_is_managed(output.uri) for output in outputs):
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
        output_target = preflight_run_output_target(plan, target_node_id)
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
                           target_node_id=output_target, request_id=request_id)
        initialize_run_outputs(status, graph, output_target, self.node_specs)
        with self._lock:
            self.runs[run_id] = status
            self._published_statuses[run_id] = RunStatus.model_validate(status.model_dump())
            self._cancel[run_id] = _CancelToken(cancel_check)
            self._terminal_publication_gates[run_id] = _TerminalPublicationGate()
            self._evict()
        self._emit(graph, status)  # persist 'queued' before the worker starts (pollable on any instance)
        threading.Thread(target=self._execute_guarded, args=(run_id, plan, graph, target_node_id), daemon=True).start()
        return self.status(run_id)

    def _execute_guarded(self, run_id: str, plan: CompilePlan, graph: Graph, target: str | None) -> None:
        """Worker-thread backstop. The daemon thread has no other terminalization, so any escape from
        `_execute` (run-scope/engine setup failing before the body's own boundary, or an unforeseen bug)
        fails the run cleanly instead of stranding every node in 'running'."""
        try:
            self._execute(run_id, plan, graph, target)
        except Exception as e:  # noqa: BLE001
            status = self.runs.get(run_id)
            with self._lock:
                self._cancel.pop(run_id, None)
                self._terminal_publication_gates.pop(run_id, None)
                self._scopes.pop(run_id, None)
            if status is None or status.status in ("done", "failed", "cancelled"):
                return
            status.status, status.error = "failed", f"{type(e).__name__}: {e}"
            settle_uncommitted_outputs(status, "failed", status.error)
            for p in status.per_node:
                if p.status not in ("done", "failed", "cancelled"):
                    p.status = "failed"
            with contextlib.suppress(Exception):
                self._emit(graph, status)
            with contextlib.suppress(Exception):
                self._complete(graph, target, status)

    def _publish_snapshot(self, status: RunStatus) -> None:
        snapshot = RunStatus.model_validate(status.model_dump())
        with self._lock:
            if status.run_id in self.runs:
                self._published_statuses[status.run_id] = snapshot

    def _emit(self, graph: Graph, status: RunStatus, *, strict: bool = False) -> None:
        """Fire the on_status hook (DB-backed live status); never let persistence break a run."""
        snapshot = RunStatus.model_validate(status.model_dump())
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise
        self._publish_snapshot(snapshot)

    def _complete(self, graph: Graph, target: str | None, status: RunStatus, *, strict: bool = False) -> None:
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise

    def _cleanup_new_result_artifacts(
            self, run_id: str, *, preserve: set[str] | None = None) -> None:
        """Abort only artifacts allocated by this run and not selected for terminal ownership."""
        keep = {str(uri).rstrip("/") for uri in (preserve or set())}
        with self._lock:
            local = set(self._owned_result_uris.get(run_id, set()))
            objects = set(self._owned_object_result_uris.get(run_id, set()))
        for uri in sorted(local - keep):
            try:
                self.storage.abort_result(uri, run_id)
            except Exception:  # metadata uncertainty retains the storage-owned writer fence
                logging.getLogger("hub").exception(
                    "managed local result cleanup failed after terminal decision")
                continue
            with self._lock:
                owned = self._owned_result_uris.get(run_id)
                if owned is not None:
                    owned.discard(uri)
                    if not owned:
                        self._owned_result_uris.pop(run_id, None)
        for uri in sorted(objects - keep):
            _safe_abandon_attempt(uri)
            with self._lock:
                owned = self._owned_object_result_uris.get(run_id)
                if owned is not None:
                    owned.discard(uri)
                    if not owned:
                        self._owned_object_result_uris.pop(run_id, None)

    def _release_published_result_writers(self, run_id: str) -> None:
        """Drop process writer fences after the exact terminal owner set is durable."""
        with self._lock:
            local = set(self._owned_result_uris.pop(run_id, set()))
            self._owned_object_result_uris.pop(run_id, None)
        for uri in sorted(local):
            try:
                if not self.storage.release_result(uri, run_id):
                    logging.getLogger("hub").error(
                        "managed local result writer release has no durable owner")
            except Exception:  # storage retains the exact pending release for maintenance retry
                logging.getLogger("hub").exception(
                    "managed local result writer release failed")

    def _evict(self) -> None:
        """Bound retained run/cancel/cache state (called under self._lock). Dicts keep insertion order.
        Evict only TERMINAL runs (oldest first) — never a queued/running one, so a long run submitted
        early isn't dropped mid-flight by 100 later submissions (which would 404 its status poll)."""
        _terminal = {"done", "failed", "cancelled"}
        while len(self.runs) > _MAX_RUNS:
            victim = next((rid for rid in self.runs
                           if (published := self._published_statuses.get(rid)) is not None
                           and published.status in _terminal), None)
            if victim is None:
                break  # everything retained is still in-flight — exceed the cap rather than drop a live run
            self.runs.pop(victim, None)
            self._published_statuses.pop(victim, None)
            self._cancel.pop(victim, None)
            self._terminal_publication_gates.pop(victim, None)
            self._scopes.pop(victim, None)
        while len(self._cache) > _MAX_RUNS:
            self._cache.pop(next(iter(self._cache)), None)

    def _execute(self, run_id: str, plan: CompilePlan, graph: Graph, target: str | None) -> None:
        status = self.runs[run_id]
        cancel = self._cancel[run_id]
        # `_execute` is also the narrow synchronous seam used by lifecycle tests and embedders. Keep
        # that path safe while normal `run()` still installs the gate before the first observable status.
        with self._lock:
            publication_gate = self._terminal_publication_gates.setdefault(
                run_id, _TerminalPublicationGate())
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
        read_leases_released = False
        cache_pin_released = False
        cache_pin_release_warned = False

        def release_read_leases() -> None:
            nonlocal read_leases_released
            if read_leases_released:
                return
            read_leases_released = True
            try:
                read_leases.close()
            except Exception:  # noqa: BLE001 — lease expiry is the safe fallback
                logging.getLogger("hub").exception(
                    "managed read lease cleanup failed after local run")

        def release_cache_pin() -> bool:
            nonlocal cache_pin_released, cache_pin_release_warned
            if cache_pin_released:
                return True
            if cache_pin is not None:
                try:
                    cache_pin.close()
                except Exception:  # retain its process fence on metadata uncertainty
                    if not cache_pin_release_warned:
                        logging.getLogger("hub").exception(
                            "managed result cache reader release failed; retrying before terminal state")
                        cache_pin_release_warned = True
                    return False
            cache_pin_released = True
            return True

        def release_cache_pin_before_terminal() -> None:
            while not release_cache_pin():
                # Cleanup is part of terminal acknowledgement: keep the shared status live while
                # metadata is unavailable, then retry the idempotent release without a hot loop.
                time.sleep(0.1)

        def release_guards() -> None:
            """Release all ownership exactly once when execution fails before run_scope opens."""
            release_read_leases()
            release_cache_pin_before_terminal()

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
            release_guards()
            status.status = "cancelled" if cancel.is_set() else "failed"
            status.error = None if cancel.is_set() else f"{type(exc).__name__}: {exc}"
            for item in status.per_node:
                item.status = status.status
            status.ms = int((time.time() - started) * 1000)
            status.total_rows = None
            settle_uncommitted_outputs(
                status, "cancelled" if cancel.is_set() else "failed", status.error)
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
                self._terminal_publication_gates.pop(run_id, None)
                self._scopes.pop(run_id, None)
            self._complete(graph, target, status)
            return
        terminal_persisted = False
        terminal_rejected = False
        provisional_result: RunStatus | None = None
        nm = g.node_map(graph)
        rows_seen = 0
        # Bind the write destination's object-store credential BEFORE the run scope opens, so the scope
        # cursor snapshots that secret (DuckDB freezes a cursor's secret view at transaction start). A
        # broken/ambiguous destination credential raises here — terminalize as a failed run instead of
        # letting it escape _execute, kill the worker thread, and strand every node in 'running'.
        try:
            run_object_store_cfg = self._run_object_store_cfg(plan, nm)
        except Exception as e:  # noqa: BLE001 — any resolution error is a clean run failure, not a strand
            release_guards()
            status.status, status.error = "failed", str(e)
            settle_uncommitted_outputs(status, "failed", status.error)
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
                self._terminal_publication_gates.pop(run_id, None)
                self._scopes.pop(run_id, None)
            self._complete(graph, target, status)
            return
        # Run on our OWN DuckDB cursor (db.run_scope), NOT the process-global lock: a long run no
        # longer serializes every other user's preview/sample/run, and a failure here can't wedge them.
        run_context = contextlib.ExitStack()
        try:
            run_context.enter_context(db.object_store_binding(run_object_store_cfg))
            scope = run_context.enter_context(db.run_scope())
        except Exception:
            # ``run_scope`` can fail before yielding, so the body/finally below never runs. Reset the
            # object-store binding and release every ownership guard before the thread backstop emits
            # the terminal failure.
            try:
                run_context.close()
            finally:
                release_guards()
            raise
        with run_context:
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
                        release_cache_pin_before_terminal()
                        status.status = "cancelled"
                        settle_uncommitted_outputs(status, "cancelled")
                        status.total_rows = None
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
                        # Build every declared output, but do not select one for a multi-output
                        # intermediate. Its downstream edge owns the explicit source-port choice.
                        engine.build(step.node_id)  # build (lazy) — cheap
                        self._check_schema(nm[step.node_id], engine)  # enforce a pinned contract (may raise)
                    if pn:
                        pn.status = "done"
                        pn.ms = int((time.time() - t0) * 1000)
                        pn.rows = rows_seen or None
                    status.rows_processed = rows_seen
                    status.progress = _step_progress(status)  # fraction of steps complete (deterministic)
                    self._emit(graph, status)  # per-node progress → DB (cross-instance polling sees it advance)

                # A direct non-sink target publishes every declared port. Assert still runs its quality
                # gate above first; when that gate permits completion, both pass and violations are durable
                # researcher-visible results rather than a hidden special case.
                if target and nm.get(target) and nm[target].type != "write":
                    # Keep the ready artifact binding private until its exact RunState owner commits.
                    # ``self.runs`` points at ``status`` and is polled concurrently, so materializing into
                    # that object would expose an unowned URI during a slow/unknown terminal commit.
                    provisional_result = status.model_copy(deep=True)
                    rows_seen = self._materialize_results(
                        target, engine, provisional_result, cached, phash, cancel)
                    status.rows_processed = rows_seen
                    last_step_published = False

                # A result artifact is still provisional until its exact terminal RunState owns the
                # complete committed set. Cancellation therefore wins here even when one or more files
                # have finished writing. A catalog sink keeps the older publication-wins rule because its
                # provider commit is already externally visible and cannot truthfully be relabelled.
                if cancel.is_set() and (provisional_result is not None
                                        or not last_step_published):
                    raise RuntimeError("run cancelled before completion")
                if not last_step_published:
                    if cache_pin is not None:
                        cache_pin.check()
                    for guard in read_guards:
                        guard.check()

                # set the final counts BEFORE flipping to 'done' — a client polls in another thread and
                # reads a terminal status eagerly; the finally sets these too late, so a poll could
                # otherwise observe status='done' with total_rows still None or ms still 0 (a flaky race).
                status.progress = 1.0
                status.stalled = False
                status.ms = int((time.time() - started) * 1000)
                from hub import metadb
                local_managed = getattr(self.storage, "is_managed_result_uri", None)
                result_binding = provisional_result or status
                result_uris = _committed_result_uris(result_binding)
                local_result_uris = [
                    uri for uri in result_uris
                    if callable(local_managed) and local_managed(uri)
                ]
                managed_result = bool(result_uris) and all(
                    metadb.object_attempt_uri_shape(uri)
                    or (callable(local_managed) and local_managed(uri))
                    for uri in result_uris)
                status.total_rows = _single_committed_rows(result_binding)
                if managed_result:
                    if self.on_status is None and self.forced_results is None:
                        raise RuntimeError(
                            "managed full results require authoritative RunState persistence")
                    if not publication_gate.begin_publication():
                        # The cancel request linearized first. Mirror it onto the execution token before
                        # raising so the terminal branch cannot race ahead of cancel() setting the token.
                        cancel.set()
                        raise RuntimeError("run cancelled before terminal publication")
                    # RunState is the primary owner for a full result. Publish it before the optional
                    # cache pointer, keep any cache-hit pin through best-effort history persistence, and
                    # never expose terminal done when the primary durable terminal write fails.
                    persisted = status.model_copy(deep=True)
                    persisted.outputs = [output.model_copy(deep=True)
                                         for output in result_binding.outputs]
                    persisted.status = "done"
                    persisted.total_rows = _single_committed_rows(persisted)
                    persisted_doc = persisted.model_dump()

                    def publication_retry(_attempt: int) -> None:
                        # Update only the in-memory observation.  Do not emit a different DB document
                        # while the exact done transaction's outcome is unresolved.
                        with self._lock:
                            status.status = "running"
                            status.stalled = True
                            status.error = "terminal publication is retrying"

                    if self.forced_results is not None and self.on_status is None:
                        # The isolated child writes a parent-owned exact URI. Its parent establishes the
                        # authoritative terminal owner after validating the child result document.
                        self._emit(graph, persisted, strict=True)
                    else:
                        def publication_receipt() -> bool:
                            if local_result_uris:
                                return self.storage.result_publication_receipt(
                                    local_result_uris, run_id, persisted_doc)
                            # RunState and its object-reference set commit in the same transaction. An
                            # exact document readback is therefore a sufficient object-only receipt.
                            return metadb.get_run_state(run_id) == persisted_doc

                        _persist_local_result_done(
                            lambda: self._emit(graph, persisted, strict=True),
                            publication_receipt,
                            on_retry=publication_retry,
                            wait=self.publication_retry_wait)
                    publication_gate.mark_published()
                    # A commit may have succeeded even when its response was lost. In that case the
                    # receipt, not _emit(), proves success, so install the same terminal snapshot here.
                    self._publish_snapshot(persisted)
                    terminal_persisted = True
                    # One locked object update makes done+binding visible together only after durable
                    # success/receipt.  ``persisted`` was never mutated across retries.
                    with self._lock:
                        status.__dict__.update(persisted.__dict__)
                    self._complete(graph, target, status)
                else:
                    if provisional_result is not None:
                        status.outputs = [output.model_copy(deep=True)
                                          for output in provisional_result.outputs]
                    status.status = "done"
                if cacheable:
                    try:
                        self._cache_put(phash, outputs_cache_document(status))
                    except Exception:  # the durable RunState/history owners already committed
                        pass
            except Exception as e:  # noqa: BLE001
                from hub import metadb
                terminal_rejected = isinstance(e, metadb.RunStatePublicationRejected)
                failed_binding = provisional_result or status
                if provisional_result is not None:
                    status.outputs = [output.model_copy(deep=True)
                                      for output in provisional_result.outputs]

                if cancel.is_set():
                    # Cancellation before terminal publication owns no result, even if earlier ports
                    # finished writing. Cleanup is limited to artifacts allocated by this run; cache-hit
                    # artifacts remain protected by their existing durable owners.
                    if provisional_result is not None:
                        self._cleanup_new_result_artifacts(run_id)
                        discard_unpublished_outputs(status, "cancelled")
                    else:
                        # A catalog provider may already have crossed its external commit point. Keep a
                        # committed sink visible and settle only work that never published.
                        settle_uncommitted_outputs(status, "cancelled")
                    status.status = "cancelled"  # an interrupted step is a cancel, not a failure
                    status.error = None
                    for p in status.per_node:  # settle interrupted + not-yet-started nodes too
                        if p.status != "done":
                            p.status = "cancelled"
                else:
                    # A partial result may be retained only while every source/cache ownership fence is
                    # still valid. Cache hits are an all-or-nothing reuse decision and are never exposed
                    # by a failed run after their temporary pins are released.
                    guard_error: Exception | None = None
                    try:
                        if cache_pin is not None:
                            cache_pin.check()
                        for guard in read_guards:
                            guard.check()
                    except Exception as exc:  # ownership uncertainty invalidates every provisional URI
                        guard_error = exc

                    cached_uris = {
                        str(output.uri) for output in committed_document_outputs(cached)
                    }
                    committed_uris = set(_committed_result_uris(failed_binding))
                    with self._lock:
                        newly_owned = {
                            *self._owned_result_uris.get(run_id, set()),
                            *self._owned_object_result_uris.get(run_id, set()),
                        }
                    retain_partial = (
                        not terminal_rejected and guard_error is None
                        and not (committed_uris & cached_uris)
                    )
                    preserve = committed_uris & newly_owned if retain_partial else set()
                    self._cleanup_new_result_artifacts(run_id, preserve=preserve)

                    status.status = "failed"
                    msg = f"{type(e).__name__}: {e}"
                    if guard_error is not None:
                        msg += ("; provisional outputs were discarded after an ownership fence "
                                f"failed: {type(guard_error).__name__}: {guard_error}")
                    if retain_partial:
                        settle_uncommitted_outputs(status, "failed", msg)
                    else:
                        discard_unpublished_outputs(status, "failed", msg)
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

                # A cache-hit reader pin is temporary ownership. Drop it before publishing any failed or
                # cancelled terminal state. Newly materialized partial outputs use RunState as their
                # primary durable owner and therefore do not depend on this pin.
                release_cache_pin_before_terminal()
                status.ms = int((time.time() - started) * 1000)
                status.total_rows = _single_committed_rows(status)

                if terminal_rejected:
                    # The durable owner was definitively deleted, so never call persistence/history
                    # hooks again. The accepting process may still answer an already-open status poll;
                    # publish only an in-memory terminal snapshot after temporary cache ownership is gone.
                    self._publish_snapshot(status)

                committed_after_failure = _committed_result_uris(status)
                local_managed = getattr(self.storage, "is_managed_result_uri", None)
                managed_partial = bool(committed_after_failure) and all(
                    metadb.object_attempt_uri_shape(uri)
                    or (callable(local_managed) and local_managed(uri))
                    for uri in committed_after_failure)
                if managed_partial and not terminal_rejected:
                    if not publication_gate.begin_publication():
                        cancel.set()
                        self._cleanup_new_result_artifacts(run_id)
                        discard_unpublished_outputs(status, "cancelled")
                        status.status = "cancelled"
                        status.error = None
                        status.total_rows = None
                        for item in status.per_node:
                            if item.status != "done":
                                item.status = "cancelled"
                        managed_partial = False
                if managed_partial and not terminal_rejected:
                    if self.on_status is None and self.forced_results is None:
                        raise RuntimeError(
                            "managed partial results require authoritative RunState persistence")
                    persisted = status.model_copy(deep=True)
                    persisted.total_rows = _single_committed_rows(persisted)
                    persisted_doc = persisted.model_dump()
                    local_result_uris = [
                        uri for uri in committed_after_failure
                        if callable(local_managed) and local_managed(uri)
                    ]

                    def failed_publication_retry(_attempt: int) -> None:
                        with self._lock:
                            status.stalled = True

                    def failed_publication_receipt() -> bool:
                        if local_result_uris:
                            return self.storage.result_publication_receipt(
                                local_result_uris, run_id, persisted_doc)
                        return metadb.get_run_state(run_id) == persisted_doc

                    try:
                        if self.forced_results is not None and self.on_status is None:
                            self._emit(graph, persisted, strict=True)
                        else:
                            _persist_local_result_done(
                                lambda: self._emit(graph, persisted, strict=True),
                                failed_publication_receipt,
                                on_retry=failed_publication_retry,
                                wait=self.publication_retry_wait)
                    except metadb.RunStatePublicationRejected as rejection:
                        # The partial-result owner was definitively deleted after the execution failure
                        # selected its committed prefix. This is symmetric with a rejected successful
                        # publication: abort every provisional artifact, hide every URI, and never re-emit
                        # terminal state/history from the finally block.
                        terminal_rejected = True
                        self._cleanup_new_result_artifacts(run_id)
                        rejected_error = f"{type(rejection).__name__}: {rejection}"
                        status.status = "failed"
                        status.error = rejected_error
                        status.total_rows = None
                        discard_unpublished_outputs(status, "failed", rejected_error)
                        self._publish_snapshot(status)
                    else:
                        publication_gate.mark_published()
                        self._publish_snapshot(persisted)
                        terminal_persisted = True
                        with self._lock:
                            status.__dict__.update(persisted.__dict__)
                        self._complete(graph, target, status)
            finally:
                release_read_leases()
                # scope exit (below) rolls back + drops this run's views on its own cursor
                for pth in engine.spill_files:  # GC temp parquet spilled this run (outputs already committed)
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
                status.ms = int((time.time() - started) * 1000)
                status.total_rows = _single_committed_rows(status)
                if not terminal_persisted and not terminal_rejected:
                    self._emit(graph, status)  # persist terminal state after all commit-point work
                with self._lock:
                    self._cancel.pop(run_id, None)  # done/failed/cancelled → drop the cancel Event
                    self._terminal_publication_gates.pop(run_id, None)
                    self._scopes.pop(run_id, None)
                if not terminal_persisted and not terminal_rejected:
                    self._complete(graph, target, status)
                release_cache_pin()
                if terminal_persisted:
                    self._release_published_result_writers(run_id)

    def _count(self, engine: BuildEngine, node_id: str, cached: dict | None) -> int:
        cached_output = sole_committed_document_output(cached)
        if cached_output is not None and cached_output.rows is not None:
            return cached_output.rows
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

    def _materialize_results(self, node_id: str, engine: BuildEngine, status: RunStatus,
                             cached: dict | None, phash: str, cancel: _CancelToken) -> int:
        """Materialize every direct target port serially in its declaration order.

        The supplied status is a private provisional copy. A caller publishes it only after the exact
        terminal RunState owns every committed artifact. If one port fails, earlier commits remain in
        the candidate, that port fails, and later ports are explicitly skipped.
        """
        cached_outputs = committed_document_outputs(cached)
        if (self.forced_results is None and cached_outputs
                and apply_cached_outputs(status, cached) is not None):
            return int(cached_outputs[0].rows or 0) if len(cached_outputs) == 1 else 0

        # This exact-key and declaration-order barrier happens before the first artifact allocation.
        relations = engine.relations(node_id)
        expected = [output.model_copy(deep=True) for output in status.outputs]
        if list(relations) != [output.port_id for output in expected]:
            raise RuntimeError("runtime output order does not match the declared run output set")
        for index, output in enumerate(expected):
            if cancel.is_set():
                raise RuntimeError("run cancelled before output materialization")
            try:
                uri, rows = self._materialize_result(
                    node_id, output.port_id, relations[output.port_id], status,
                    phash, index, cancel)
                commit_output(
                    status, port_id=output.port_id, uri=uri, rows=rows)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                settle_output(status, output.port_id, "failed", detail)
                for skipped in expected[index + 1:]:
                    settle_output(
                        status, skipped.port_id, "skipped",
                        f"not attempted after output port '{output.port_id}' failed")
                raise
        return int(status.outputs[0].rows or 0) if len(status.outputs) == 1 else 0

    def _materialize_result(
            self, node_id: str, port_id: str, rel, status: RunStatus,
            phash: str, slot: int, cancel: _CancelToken) -> tuple[str, int]:
        """Write one declared port to a fresh durable result without publishing its RunOutput."""
        forced_uri = self._forced_result_uri(node_id, port_id, status)
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
            uri = logical_uri = begin_local(f"{phash}:{slot}", status.run_id)
            with self._lock:
                self._owned_result_uris.setdefault(status.run_id, set()).add(uri)
            validate = getattr(self.storage, "validate_result_uri", None)
            if callable(validate):
                validate(uri)
        else:
            output_name = (f"__result_{phash}" if len(status.outputs) == 1
                           else f"__result_{phash}_{slot:02d}")
            logical_uri = forced_uri or self.storage.output_uri(
                output_name, ".parquet")
            uri = logical_uri
        committed_output_snapshot(
            status, port_id=port_id, uri=uri, rows=0)
        if is_object_uri(logical_uri):
            from hub.handoff import (
                allocate_attempt, is_attempt_uri, physical_attempt_uri, write_manifest)
            parent_owned = bool(forced_uri)
            if parent_owned:
                if not is_attempt_uri(logical_uri):
                    raise RuntimeError("parent-provided object result URI is not an exact attempt")
            else:
                handle = allocate_attempt(
                    logical_uri=logical_uri, kind="region", run_id=status.run_id,
                    allocation_key=f"full-result:{status.run_id}:{phash}:{slot}",
                    uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                        logical_uri, namespace, generation, attempt_id),
                )
                uri = handle["uri"]
                with self._lock:
                    self._owned_object_result_uris.setdefault(
                        status.run_id, set()).add(uri)
            committed_output_snapshot(
                status, port_id=port_id, uri=uri, rows=0)
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
                    with self._lock:
                        owned = self._owned_object_result_uris.get(status.run_id)
                        if owned is not None:
                            owned.discard(uri)
                            if not owned:
                                self._owned_object_result_uris.pop(status.run_id, None)
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
                        owned = self._owned_result_uris.get(status.run_id)
                        if owned is not None:
                            owned.discard(uri)
                            if not owned:
                                self._owned_result_uris.pop(status.run_id, None)
                except Exception:  # retain metadata/fd ownership when cleanup is uncertain
                    logging.getLogger("hub").exception(
                        "managed local result write cleanup failed")
                raise
        else:
            res = self._adapter_write(self.resolve_adapter(uri), uri, rel, "overwrite", cancel)
        rows = int(res.get("rows") or 0)
        return str(res.get("uri", uri)), rows

    def _forced_result_uri(
            self, node_id: str, port_id: str, status: RunStatus) -> str | None:
        """Return the one parent-reserved URI bound to this exact declared output."""
        if self.forced_results is None:
            return None
        expected = [(output.node_id, output.port_id) for output in status.outputs]
        actual: list[tuple[str, str]] = []
        uris: dict[tuple[str, str], str] = {}
        for item in self.forced_results:
            if not isinstance(item, dict):
                raise RuntimeError("isolated forced result contract is malformed")
            result_node = item.get("nodeId")
            result_port = item.get("portId")
            uri = item.get("uri")
            if (not isinstance(result_node, str) or not isinstance(result_port, str)
                    or not isinstance(uri, str) or not uri):
                raise RuntimeError("isolated forced result contract is malformed")
            key = (result_node, result_port)
            actual.append(key)
            uris[key] = uri
        if actual != expected or len(uris) != len(actual):
            raise RuntimeError("isolated forced result contract does not match declared outputs")
        return uris.get((node_id, port_id))

    def _check_assert(self, node, engine: BuildEngine) -> int:
        """A data-quality gate: the node's relation is the VIOLATING rows (predicate not TRUE). Count them;
        on severity='error' with any violation, raise so the run fails with an actionable message (the
        offending rows are inspectable by previewing the node). 'warn' just records the count."""
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        severity = cfg.get("severity")
        title = (node.data.get("title") if isinstance(node.data, dict) else None) or node.id
        try:
            # Assert exposes passing rows on ``pass`` and violations on ``out``. The quality gate is
            # specifically about violations, so never rely on a multi-output default.
            viol = int(engine.relation(node.id, "out").aggregate("count(*) AS n").fetchone()[0])
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
        """The single object-store credential this run's write sink(s) use, or None when no sink targets
        an object store. A run executes on ONE cursor whose secret is frozen at scope start, so it can
        carry only ONE object-store identity: if sinks target object stores with DIFFERENT credentials
        we reject the run here rather than silently binding the first (which would write to the wrong
        account). An object-store sink with ambient/instance-role creds resolves to {} — still bound
        (env), never skipped as if it were a local sink (None)."""
        from hub import destinations
        from hub.sinks import SinkSpec
        cfgs: list[dict] = []
        for step in plan.steps:
            if step.kind != "write":
                continue
            node = nm.get(step.node_id)
            if node is None:
                continue
            data = node.data if isinstance(node.data, dict) else {}
            spec = SinkSpec.from_config(data.get("config", {}), data.get("title"))
            cfg = destinations.object_store_cred_cfg(self.workspace, spec.destination_id)
            if cfg is not None:  # None = not an object store; {} = object store with ambient creds
                cfgs.append(cfg)
        if not cfgs:
            return None
        if any(c != cfgs[0] for c in cfgs[1:]):
            raise RuntimeError(
                "this run writes to object-store destinations with different credentials; a run uses a "
                "single object-store identity — split them into separate runs")
        return cfgs[0]

    def _commit_write(self, node, graph: Graph, engine: BuildEngine, status: RunStatus,
                      cached: dict | None, cancel: _CancelToken, pre_publish=None) -> int:
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        from hub import destinations
        from hub.sinks import (
            SinkCommit, SinkSpec, commit_sink, expected_sink_uri, preflight_sink,
        )
        spec = SinkSpec.from_config(cfg, node.data.get("title") if isinstance(node.data, dict) else None)
        if cancel.is_set():
            raise RuntimeError("run cancelled before output commit")
        # The table identity is known before destination resolution, allocation, or an adapter write.
        # Reject a snapshot that cannot enter the public contract before any irreversible sink effect.
        preflight_output_table(status, spec.name)
        parent_contract = self.forced_sink_targets is not None
        from hub import metadb
        parent_uris = metadb.catalog_lineage_parent_tokens(
            g.all_upstream_publication_uris(graph, node.id))
        from hub.plugins.catalog import lineage_for_output
        lineage = None if parent_contract else lineage_for_output(
            graph, status.run_id, node.id)
        # Bind this destination's credential at the object-store open (the scope cursor already
        # snapshotted it via the run-level binding; this makes the per-write credential explicit).
        os_cfg = destinations.object_store_cred_cfg(self.workspace, spec.destination_id)
        if os_cfg is not None:
            with db.lock():
                db.ensure_object_store(os_cfg)
        # content-addressed skip: an identical overwrite plan already wrote this, so re-running is a
        # no-op. append is NOT idempotent (it must add a part every run), so it never uses the cache.
        cached_output = sole_committed_document_output(cached)
        cached_binding = status.model_copy(deep=True)
        if (spec.mode != "append" and cached_output is not None and cached_output.table
                and cached_output.version is not None
                and self._output_exists(str(cached_output.uri))
                and apply_cached_output(cached_binding, cached) is not None):
            from hub.plugins.catalog import record_cached_output_lineage

            if lineage is None:  # pragma: no cover - subprocess children never read the parent cache
                raise RuntimeError("parent-owned sink cannot consume a local catalog cache entry")
            crossed = record_cached_output_lineage(
                self.catalog,
                name=cached_output.table,
                uri=str(cached_output.uri),
                version=cached_output.version,
                parents=parent_uris,
                lineage=lineage,
                pre_publish=(None if pre_publish is None
                             else lambda: pre_publish(check_cancel=True)),
            )
            if crossed:
                # Validation above used a copy so a stale/unsupported cache candidate cannot mutate the
                # public status. Apply only after its lineage fact has committed.
                if apply_cached_output(status, cached) is None:  # pragma: no cover - same frozen input
                    raise RuntimeError("validated cached output binding changed before publication")
                return int(cached_output.rows or 0)

        inc = g.incoming(graph, node.id)
        if not inc:
            return 0
        # route by the wired output PORT (source_handle) — a write off a multi-output node must
        # persist that port's data, not the default/first one. Mirrors BuildEngine._inputs.
        parent_rel = engine.relation(inc[0].source, inc[0].source_handle)
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
            try:
                # The immutable attempt is the published identity and is known before its writer
                # starts. Locally allocated attempts are discarded if even that identity is invalid.
                committed_output_snapshot(
                    status, uri=attempt_uri, table=spec.name, rows=0)
                physical_uri = (attempt_uri if spec.partition_by else
                                attempt_uri.rstrip("/") + "/part-00000.parquet")
                kwargs = {"partition_by": spec.partition_by} if spec.partition_by else {}
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
            # Built-in sink semantics have a deterministic published URI. Custom adapters may return
            # another URI, which is checked authoritatively immediately after their write below.
            committed_output_snapshot(
                status,
                uri=expected_sink_uri(spec, logical_uri, logical_adapter),
                table=spec.name,
                rows=0,
            )
            committed = commit_sink(
                spec, parent_rel, self.workspace, self.storage, self.resolve_adapter,
                target_uri=forced_target,
                write_adapter=lambda adapter, uri, rel, mode, **kwargs: self._adapter_write(
                    adapter, uri, rel, mode, cancel, **kwargs
                ),
            )

        # Adapter-returned identities are untrusted. Validate the exact public snapshot before the
        # cancellation/publication fence and, critically, before catalog registration.
        committed_snapshot = committed_output_snapshot(
            status, uri=committed.uri, table=committed.name, rows=committed.rows)

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
        if not (managed_parquet and parent_contract):
            if managed_parquet:
                publish = managed_publisher
            else:
                from hub.plugins.catalog import publish_unmanaged_output_attested
                publish = lambda **kwargs: publish_unmanaged_output_attested(  # noqa: E731
                    self.catalog, **kwargs)
            try:
                published = publish(
                    name=committed.name, uri=committed.uri, parents=parent_uris,
                    pipeline="canvas", lineage=lineage)  # content-addressed version
            except Exception as exc:
                logging.getLogger("hub").exception("sink publication failed")
                if managed_parquet and not parent_owned:
                    _safe_abandon_attempt(attempt_uri)
                raise RuntimeError("sink publication failed") from exc
            published_version = _catalog_publication_version(published)
            if published_version is not None:
                committed_snapshot = committed_output_snapshot(
                    status, uri=committed.uri, table=committed.name,
                    version=published_version, rows=committed.rows)
        status.outputs = [committed_snapshot]
        return committed.rows

    def _source_uri(self, nm_node: str, graph: Graph) -> str | None:
        for n in g.upstream_chain(graph, nm_node):
            if n.type == "source":
                cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
                return cfg.get("uri")
        return None

    def status(self, run_id: str) -> RunStatus:
        with self._lock:
            return self._published_statuses[run_id].model_copy(deep=True)

    def cancel(self, run_id: str) -> RunStatus:
        with self._lock:
            self.runs[run_id]  # preserve the existing KeyError contract for an unknown run
            published = self._published_statuses[run_id]
            # Request cancellation only. The worker publishes the terminal `cancelled` state after it has
            # unwound past every possible commit point, so a caller can treat terminal status as acknowledgement
            # instead of returning while this thread may still publish. Admission follows the coherent public
            # snapshot, not the execution-owned mutable status: a failed partial result remains externally
            # running until its durable terminal owner starts publication, and cancellation may still win that
            # gate during this interval.
            if published.status in ("queued", "running"):
                cancel = self._cancel.get(run_id)
                publication_gate = self._terminal_publication_gates.get(run_id)
                scope = self._scopes.get(run_id)
            else:
                cancel = None
                publication_gate = None
                scope = None
        admitted = publication_gate is None or publication_gate.request_cancel()
        if admitted and cancel is not None:
            cancel.set()
        if not admitted:
            scope = None
        if scope is not None:
            scope.interrupt()  # abort THIS run's cursor (base-conn interrupt wouldn't touch it)
        return self.status(run_id)
