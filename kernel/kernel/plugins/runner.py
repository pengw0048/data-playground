"""Default runner (PRD §8.3) — the local out-of-core engine.

Lowers the graph to a DuckDB relation plan and executes it out-of-core (DuckDB streams and
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

from kernel import db, graph as g
from kernel.executors.engine import LoweringEngine
from kernel.models import (
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

    def __init__(self, resolve_adapter, registry, catalog, workspace: str, node_lowerings=None,
                 node_specs=None, storage=None):
        self.resolve_adapter = resolve_adapter
        self.registry = registry
        self.catalog = catalog
        self.workspace = workspace
        # where committed outputs go (pluggable; local dir by default). Falls back to workspace/outputs.
        from kernel.storage import make_storage
        self.storage = storage if storage is not None else make_storage(workspace)
        self.on_complete = None  # optional (graph, target, status) hook — Deps wires it to run-history persistence
        # keep the SAME dict object deps passes (plugins fill it AFTER construction) — an
        # empty {} is falsy, so `or {}` would rebind a new dict and drop plugin lowerings.
        self.node_lowerings = node_lowerings if node_lowerings is not None else {}
        self.node_specs = node_specs if node_specs is not None else {}
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}
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

    # -- plan hash (content addressing) ------------------------------------ #
    def _plan_hash(self, graph: Graph, target: str | None) -> str:
        chain = g.upstream_chain(graph, target) if target else g.topo_order(graph)
        parts = []
        for n in chain:
            cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
            parts.append(f"{n.id}:{n.type}:{json.dumps(cfg, sort_keys=True, default=str)}")
            if n.type == "source":
                uri = cfg.get("uri") or cfg.get("table")
                if uri:
                    try:
                        parts.append(f"fp:{self.resolve_adapter(uri).fingerprint(uri)}")
                    except Exception:  # noqa: BLE001
                        pass
        # edges + their handles are part of the plan: re-routing an edge to a different output port
        # (same node configs) must invalidate the cache, else it returns the old port's result.
        ids = {n.id for n in chain}
        parts += sorted(f"e:{e.source}:{e.source_handle}:{e.target}:{e.target_handle}"
                        for e in graph.edges if e.source in ids and e.target in ids)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    # -- run --------------------------------------------------------------- #
    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement) -> RunStatus:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        per_node = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        status = RunStatus(run_id=run_id, status="queued", placement=placement, per_node=per_node)
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
            self._evict()
        threading.Thread(target=self._execute, args=(run_id, plan, graph, target_node_id), daemon=True).start()
        return status

    def _evict(self) -> None:
        """Bound retained run/cancel/cache state (called under self._lock). Dicts keep insertion order."""
        while len(self.runs) > _MAX_RUNS:
            old = next(iter(self.runs))
            self.runs.pop(old, None)
            self._cancel.pop(old, None)
        while len(self._cache) > _MAX_RUNS:
            self._cache.pop(next(iter(self._cache)), None)

    def _execute(self, run_id: str, plan: CompilePlan, graph: Graph, target: str | None) -> None:
        status = self.runs[run_id]
        cancel = self._cancel[run_id]
        started = time.time()
        status.status = "running"
        phash = self._plan_hash(graph, target)
        with self._lock:
            cached = self._cache.get(phash)
        engine = LoweringEngine(graph, self.resolve_adapter, self.registry, full=True,
                                node_lowerings=self.node_lowerings, node_specs=self.node_specs)
        nm = g.node_map(graph)
        rows_seen = 0
        db.lock().acquire()  # serialize all DuckDB access for the whole run
        try:
            for step in plan.steps:
                if cancel.is_set():
                    status.status = "cancelled"
                    return
                pn = next((p for p in status.per_node if p.node_id == step.node_id), None)
                if pn:
                    pn.status = "running"
                t0 = time.time()
                if step.kind == "error_gate":
                    time.sleep(0.02)
                elif step.kind == "write":
                    rows_seen = self._commit_write(nm[step.node_id], graph, engine, status, cached)
                else:
                    engine.relation(step.node_id)  # lower (lazy) — cheap
                if pn:
                    pn.status = "done"
                    pn.ms = int((time.time() - t0) * 1000)
                    pn.rows = rows_seen or None
                status.rows_processed = rows_seen

            # if the target is not a sink, force execution to a real row count
            if target and nm.get(target) and nm[target].type not in ("write",):
                rows_seen = self._count(engine, target, cached)
                status.rows_processed = rows_seen

            status.status = "done"
            with self._lock:
                self._cache[phash] = {"rows": rows_seen, "uri": status.output_uri, "table": status.output_table}
        except Exception as e:  # noqa: BLE001
            if cancel.is_set():
                status.status = "cancelled"  # an interrupted step is a cancel, not a failure
            else:
                status.status = "failed"
                status.error = f"{type(e).__name__}: {e}"
                for p in status.per_node:
                    if p.status == "running":
                        p.status = "failed"
        finally:
            db.drop_created_views()
            for pth in engine.spill_files:  # GC temp parquet spilled this run (outputs already committed)
                try:
                    os.remove(pth)
                except OSError:
                    pass
            db.lock().release()
            status.ms = int((time.time() - started) * 1000)
            status.total_rows = rows_seen
            self._cancel.pop(run_id, None)  # done/failed/cancelled → drop the cancel Event
            if self.on_complete:  # persist the finished run (run history); never let it break the run
                try:
                    self.on_complete(graph, target, status)
                except Exception:  # noqa: BLE001
                    pass

    def _count(self, engine: LoweringEngine, node_id: str, cached: dict | None) -> int:
        if cached and cached.get("rows") is not None:
            return cached["rows"]
        return int(engine.relation(node_id).aggregate("count(*) AS n").fetchone()[0])

    def _commit_write(self, node, graph: Graph, engine: LoweringEngine, status: RunStatus,
                      cached: dict | None) -> int:
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        mode = cfg.get("writeMode", "overwrite")
        # content-addressed skip: an identical overwrite plan already wrote this, so re-running is a
        # no-op. append is NOT idempotent (it must add a part every run), so it never uses the cache.
        if mode != "append" and cached and cached.get("table") and cached.get("uri") and os.path.exists(cached["uri"]):
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
            from kernel import destinations
            uri = destinations.target_uri(self.workspace, dest_id, cfg.get("destPath", ""), fname)
        else:
            uri = self.storage.output_uri(base, ext)

        inc = g.incoming(graph, node.id)
        if not inc:
            return 0
        # route by the wired output PORT (source_handle) — a write off a multi-output node must
        # persist that port's data, not the default/first one. Mirrors LoweringEngine._inputs.
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
            db.interrupt()  # abort the in-flight DuckDB step so cancel actually stops work + frees the lock
            st.status = "cancelled"
        return st
