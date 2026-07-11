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

_CONFIRM_ROWS = 5_000_000   # fallback gate when byte size is unknown but the row count is known
_CONFIRM_BYTES = 2 << 30    # 2 GiB — the primary confirm signal: a full pass moving this much data


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
    if "referenced column" in m or "not found in from" in m:  # specifically an unknown COLUMN
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
                    rows_seen = self._materialize_result(target, engine, status, cached, phash)
                    status.rows_processed = rows_seen

                # set the final counts BEFORE flipping to 'done' — a client polls in another thread and
                # reads a terminal status eagerly; the finally sets these too late, so a poll could
                # otherwise observe status='done' with total_rows still None or ms still 0 (a flaky race).
                status.total_rows = rows_seen
                status.progress = 1.0
                status.stalled = False
                status.ms = int((time.time() - started) * 1000)
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

    def _materialize_result(self, node_id: str, engine: BuildEngine, status: RunStatus,
                            cached: dict | None, phash: str) -> int:
        """A non-write target's full result → a durable parquet artifact so the UI can page the exact
        rows a full pass produced (an aggregate/sort a sample can't preview) and they survive a restart
        (P0-UX-01). CONTENT-ADDRESSED by the plan hash (`__result_<phash>`): identical content shares one
        artifact and re-running reuses it, while different content (another canvas, an edit) gets its own
        file — so a stable per-node path can't collide across canvases or go stale after an edit. NOT
        registered in the catalog — it's an ephemeral run result, not a user-published dataset.
        Best-effort: a relation that can't serialize (a section/opaque) falls back to a plain count so the
        run still reports a truthful total instead of failing."""
        if cached and cached.get("uri") and self._output_exists(cached["uri"]):
            status.output_uri = cached["uri"]
            status.output_table = cached.get("table")
            return int(cached.get("rows") or 0)
        try:
            rel = engine.relation(node_id)
            uri = self.storage.output_uri(f"__result_{phash}", ".parquet")  # content-addressed run result
            res = self.resolve_adapter(uri).write(uri, rel, "overwrite")
            status.output_uri = res.get("uri", uri)
            status.output_table = None  # a run result, not a catalog table (don't clutter the Tables view)
            prune = getattr(self.storage, "prune_results", None)  # coarse newest-N GC so results don't pile up
            if callable(prune):
                prune()
            return int(res.get("rows") or 0)
        except Exception:  # noqa: BLE001 — can't serialize this relation → still report a truthful count
            return self._count(engine, node_id, cached)

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
        self.catalog.register_output(name=name, uri=out_uri, parents=parent_uris, pipeline="canvas")  # content-addressed version
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
