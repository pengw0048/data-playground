"""Default runner — the local out-of-core engine.

Builds the graph into a DuckDB relation plan and executes it out-of-core (DuckDB streams and
spills, so bigger-than-RAM is fine). Estimates cost coarsely and picks placement by a threshold
— no resource knobs (P4). A runner plugin (Ray/Dask) would bind the SAME plan to a cluster.
Content-addressed: an unchanged plan (by node config + source fingerprint) is served from cache
(FR-6.3/6.4).
"""

from __future__ import annotations

import hashlib
import json
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

_CONFIRM_ROWS = 5_000_000   # a full pass over this many rows is worth a heads-up before it runs
_MAX_RUNS = 100          # cap retained run history / cache so a long-lived kernel doesn't grow forever


class LocalRunner:
    name = "local-out-of-core"

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
        self.result_put = None   # (key, doc) -> None
        # keep the SAME dict object deps passes (plugins fill it AFTER construction) — an
        # empty {} is falsy, so `or {}` would rebind a new dict and drop plugin lowerings.
        self.node_builders = node_builders if node_builders is not None else {}
        self.node_specs = node_specs if node_specs is not None else {}
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._scopes: dict[str, object] = {}  # run_id -> db._Scope, so cancel interrupts THIS run's cursor
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def can_run(self, plan: CompilePlan) -> bool:
        return plan.acyclic

    # -- estimate ---------------------------------------------------------- #
    def estimate(self, plan: CompilePlan, rows: int | None) -> RunEstimate:
        # No fabricated ETA — a per-op seconds guess is uncalibrated and misleadingly precise. Report
        # the real source-row count, or "unknown" (rather than the old rows=1000 that also slipped the
        # confirm gate). Unknown means the source couldn't be counted, which for the built-in adapters
        # means it can't be scanned either — the run will fail fast — so it needs no confirm gate; only
        # a genuinely large, countable pass does.
        placement: Placement = "local"  # the only backend today; a cluster runner (plugin) sets its own
        if rows is None:
            return RunEstimate(rows=None, placement=placement, needs_confirm=False,
                               breakdown=f"size unknown · {len(plan.steps)} steps · out-of-core")
        return RunEstimate(rows=rows, placement=placement, needs_confirm=rows >= _CONFIRM_ROWS,
                           breakdown=f"{rows:,} rows · {len(plan.steps)} steps · out-of-core")

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

    def _cache_put(self, key: str, doc: dict) -> None:
        if self.result_put:
            try:
                self.result_put(key, doc)
            except Exception:  # noqa: BLE001 — never let caching break a successful run
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
            return os.path.exists(p)
        try:
            self.resolve_adapter(uri).schema(uri)  # reads object metadata; missing → raises → recompute
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- run --------------------------------------------------------------- #
    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement, run_id: str | None = None) -> RunStatus:
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"  # a kernel passes the hub-minted id (authoritative)
        per_node = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        status = RunStatus(run_id=run_id, status="queued", placement=placement, per_node=per_node,
                           target_node_id=target_node_id)
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
            self._evict()
        self._emit(graph, status)  # persist 'queued' before the worker starts (pollable on any instance)
        threading.Thread(target=self._execute, args=(run_id, plan, graph, target_node_id), daemon=True).start()
        return status

    def _emit(self, graph: Graph, status: RunStatus) -> None:
        """Fire the on_status hook (DB-backed live status); never let persistence break a run."""
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                pass

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
        phash = self._plan_hash(graph, target)
        cacheable = self._plan_cacheable(graph, target)
        cached = self._cache_get(phash) if cacheable else None
        engine = BuildEngine(graph, self.resolve_adapter, self.registry, full=True,
                                node_builders=self.node_builders, node_specs=self.node_specs,
                                pushdown=True, output_node=target)
        nm = g.node_map(graph)
        rows_seen = 0
        # Run on our OWN DuckDB cursor (db.run_scope), NOT the process-global lock: a long run no
        # longer serializes every other user's preview/sample/run, and a failure here can't wedge them.
        with db.run_scope() as scope:
            with self._lock:
                self._scopes[run_id] = scope  # cancel() interrupts this scope's cursor
            try:
                for step in plan.steps:
                    if cancel.is_set():
                        status.status = "cancelled"
                        return
                    pn = next((p for p in status.per_node if p.node_id == step.node_id), None)
                    if pn:
                        pn.status = "running"
                    t0 = time.time()
                    if step.kind == "write":
                        rows_seen = self._commit_write(nm[step.node_id], graph, engine, status, cached)
                    elif step.kind == "assert":
                        rows_seen = self._check_assert(nm[step.node_id], engine)  # violation count; may raise
                    else:
                        engine.relation(step.node_id)  # build (lazy) — cheap
                    if pn:
                        pn.status = "done"
                        pn.ms = int((time.time() - t0) * 1000)
                        pn.rows = rows_seen or None
                    status.rows_processed = rows_seen
                    self._emit(graph, status)  # per-node progress → DB (cross-instance polling sees it advance)

                # if the target is not a sink, force execution to a real row count
                if target and nm.get(target) and nm[target].type not in ("write",):
                    rows_seen = self._count(engine, target, cached)
                    status.rows_processed = rows_seen

                status.status = "done"
                if cacheable:
                    self._cache_put(phash, {"rows": rows_seen, "uri": status.output_uri, "table": status.output_table})
            except Exception as e:  # noqa: BLE001
                if cancel.is_set():
                    status.status = "cancelled"  # an interrupted step is a cancel, not a failure
                    for p in status.per_node:  # settle the interrupted node too (else it animates forever)
                        if p.status == "running":
                            p.status = "cancelled"
                else:
                    status.status = "failed"
                    status.error = f"{type(e).__name__}: {e}"
                    for p in status.per_node:
                        if p.status == "running":
                            p.status = "failed"
            finally:
                # scope exit (below) rolls back + drops this run's views on its own cursor
                for pth in engine.spill_files:  # GC temp parquet spilled this run (outputs already committed)
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
                status.ms = int((time.time() - started) * 1000)
                status.total_rows = rows_seen
                self._emit(graph, status)  # persist the terminal status (done/failed/cancelled) to the DB
                with self._lock:
                    self._cancel.pop(run_id, None)  # done/failed/cancelled → drop the cancel Event
                    self._scopes.pop(run_id, None)
                if self.on_complete:  # persist the finished run (run history); never let it break the run
                    try:
                        self.on_complete(graph, target, status)
                    except Exception:  # noqa: BLE001
                        pass

    def _count(self, engine: BuildEngine, node_id: str, cached: dict | None) -> int:
        if cached and cached.get("rows") is not None:
            return cached["rows"]
        return int(engine.relation(node_id).aggregate("count(*) AS n").fetchone()[0])

    def _check_assert(self, node, engine: BuildEngine) -> int:
        """A data-quality gate: the node's relation is the VIOLATING rows (predicate not TRUE). Count them;
        on severity='error' with any violation, raise so the run fails with an actionable message (the
        offending rows are inspectable by previewing the node). 'warn' just records the count."""
        viol = int(engine.relation(node.id).aggregate("count(*) AS n").fetchone()[0])
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        if cfg.get("severity") == "error" and viol > 0:
            title = (node.data.get("title") if isinstance(node.data, dict) else None) or node.id
            raise RuntimeError(f"assert '{title}' failed: {viol} row(s) violate the check")
        return viol

    def _commit_write(self, node, graph: Graph, engine: BuildEngine, status: RunStatus,
                      cached: dict | None) -> int:
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        mode = cfg.get("writeMode", "overwrite")
        # content-addressed skip: an identical overwrite plan already wrote this, so re-running is a
        # no-op. append is NOT idempotent (it must add a part every run), so it never uses the cache.
        if mode != "append" and cached and cached.get("table") and cached.get("uri") and self._output_exists(cached["uri"]):
            status.output_uri, status.output_table = cached["uri"], cached["table"]
            return int(cached.get("rows") or 0)
        # the output file name (the extension picks the format); `name` (its base) is the catalog table.
        raw = cfg.get("filename") or cfg.get("name") or node.data.get("title") or "output"
        fname = "".join(c if c.isalnum() or c in "_-." else "_" for c in str(raw)).strip(".") or "output"
        base, ext = os.path.splitext(fname)
        _KNOWN = (".parquet", ".pq", ".csv", ".tsv", ".arrow", ".feather", ".ipc", ".json", ".lance")
        if ext.lower() not in _KNOWN:  # no/unknown extension → apply the format (default parquet)
            ext = {"parquet": ".parquet", "csv": ".csv", "lance": ".lance"}.get((cfg.get("format") or "parquet").lower(), ".parquet")
            base, fname = fname, f"{fname}{ext}"
        name = base
        # a write node may target a chosen destination (a preset place — local dir / object-store
        # prefix); otherwise fall back to the default local outputs storage.
        dest_id = cfg.get("destId")
        if dest_id:
            from hub import destinations
            uri = destinations.target_uri(self.workspace, dest_id, cfg.get("destPath", ""), fname)
        else:
            uri = self.storage.output_uri(base, ext)

        inc = g.incoming(graph, node.id)
        if not inc:
            return 0
        # route by the wired output PORT (source_handle) — a write off a multi-output node must
        # persist that port's data, not the default/first one. Mirrors BuildEngine._inputs.
        parent_rel = engine.relation(inc[0].source, inc[0].source_handle)
        adapter = self.resolve_adapter(uri)
        res = adapter.write(uri, parent_rel, mode)
        rows = int(res.get("rows") or 0)
        out_uri = res.get("uri", uri)  # append writes into a directory of parts — register THAT

        parent_uris = [u for e in inc for u in [self._source_uri(nm_node=e.source, graph=graph)] if u]
        self.catalog.register_output(name=name, uri=out_uri, version="v1", parents=parent_uris, pipeline="canvas")
        status.output_uri = out_uri
        status.output_table = name
        return rows

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
        # only cancel an in-flight run — never relabel a finished/failed one
        if st.status in ("queued", "running"):
            if run_id in self._cancel:
                self._cancel[run_id].set()
            scope = self._scopes.get(run_id)
            if scope is not None:
                scope.interrupt()  # abort THIS run's cursor (base-conn interrupt wouldn't touch it)
            st.status = "cancelled"
        return st
