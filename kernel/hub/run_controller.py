"""RunController — owner of a logical run across regions.

The common case — a plain graph that plans to a single default region — is NOT touched: run() returns
None and the caller uses the base runner exactly as before (zero regression). A run that genuinely
splits (a placed node, a fan-out, or a `checkpoint`) takes the multi-region path: run each region in
topological order in a background thread, materialize each intermediate region's output to a durable
per-region-hashed parquet (content-addressed → reused across runs, so editing a downstream region
recomputes only it), and run the final region via the base runner (write-commit / catalog / status as
usual) over a reduced graph whose upstream regions are replaced by ref-sources.

Per-region execution is in-process here (Phase C2); dispatching a region to its target worker's
process (real cross-worker placement, GPU isolation) is Phase C3 — the region abstraction is the seam.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from hub import compiler, db
from hub import graph as g
from hub import planner
from hub.executors.engine import BuildEngine
from hub.models import (Graph, GraphEdge, GraphEdgeData, GraphNode, PerNodeStatus, Position, RunStatus)


class RunController:
    name = "run-controller"

    def __init__(self, deps, base, place_fn):
        self.deps = deps
        self.base = base                 # the in-process LocalRunner (does the real per-region work)
        self.place_fn = place_fn         # requires -> (backend, worker) | None
        self.on_status = None
        self.on_complete = None
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._sub: dict[str, tuple] = {}  # overall run_id -> (backend, sub_run_id) currently executing
        self._lock = threading.Lock()

    def plan(self, graph: Graph, target: str | None):
        return planner.plan_regions(graph, target, self.deps.node_specs, self.place_fn) if target else []

    # -- orchestration ----------------------------------------------------- #
    def run(self, graph: Graph, target: str | None) -> RunStatus | None:
        """Start a multi-region run; return None if the graph is a single default region (caller uses
        the base runner unchanged)."""
        regions = self.plan(graph, target)
        if len(regions) <= 1 and (not regions or regions[0].backend == "default"):
            return None
        if not self._safe_to_split(graph, target, regions):
            return None  # a shape the region machinery can't yet materialize correctly → run whole, in-process
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        nm = g.node_map(graph)
        per = [PerNodeStatus(node_id=nid, status="queued", label=nm[nid].type)
               for r in regions for nid in r.node_ids if nid in nm]
        status = RunStatus(run_id=run_id, status="queued", placement="distributed", per_node=per,
                           target_node_id=target)
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
        self._emit(graph, status)
        threading.Thread(target=self._orchestrate, args=(run_id, graph, target, regions), daemon=True).start()
        return status

    def _orchestrate(self, run_id: str, graph: Graph, target: str, regions) -> None:
        status = self.runs[run_id]
        cancel = self._cancel[run_id]
        status.status = "running"
        self._emit(graph, status)
        started = time.time()
        ref_uri: dict[str, str] = {}
        try:
            for i, region in enumerate(regions):
                if cancel.is_set():
                    status.status = "cancelled"
                    return
                final = i == len(regions) - 1
                self._mark(status, region, "running")
                if final:
                    sub = self._run_final(run_id, graph, region, ref_uri)
                    status.output_uri, status.output_table = sub.output_uri, sub.output_table
                    status.rows_processed = sub.rows_processed
                    if sub.status != "done":
                        status.status = sub.status
                        status.error = sub.error
                        self._mark(status, region, "failed")
                        return
                else:
                    ref_uri[region.output_node] = self._materialize(run_id, graph, region, ref_uri)
                self._mark(status, region, "done")
                self._emit(graph, status)
            status.status = "done"
        except Exception as e:  # noqa: BLE001
            status.status = "cancelled" if cancel.is_set() else "failed"
            if status.status == "failed":
                status.error = f"{type(e).__name__}: {e}"
        finally:
            if status.status in ("failed", "cancelled"):  # don't leave earlier/other regions stuck
                for p in status.per_node:
                    if p.status != "done":
                        p.status = status.status
            status.ms = int((time.time() - started) * 1000)
            status.total_rows = status.rows_processed
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
                self._sub.pop(run_id, None)
            if self.on_complete:
                try:
                    self.on_complete(graph, target, status)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _prune_regions(d: str, keep: int = 500) -> None:
        try:
            files = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
            for f in files[:-keep]:
                try:
                    os.remove(f)
                except OSError:
                    pass
        except OSError:
            pass

    def _safe_to_split(self, graph: Graph, target: str, regions) -> bool:
        """Refuse to split (→ run the whole graph in-process, correct but unplaced) for shapes the
        single-port parquet handoff can't represent yet, rather than silently corrupt data:
        - a cross-region cut off a NON-default output port (a multi-output node / section named port);
        - an intermediate `write` (materializing it would drop its commit + catalog side-effect)."""
        nm = g.node_map(graph)
        if any(sh not in (None, "out") for r in regions for (_u, sh, _i, _t) in r.cut_inputs):
            return False
        return not any(nm[nid].type == "write" and nid != target for r in regions for nid in r.node_ids)

    def _backend_runner(self, region):
        """The runner that executes a region: the in-process base for 'default', else the named backend
        (a pool / pod / Ray runner) — so a PLACED region physically runs on its worker (C3)."""
        if region.backend == "default":
            return self.base
        return next((r for r in self.deps.runners if r.name == region.backend), self.base)

    def _await(self, backend, sub_id: str, cancel_run: str | None = None) -> RunStatus:
        while True:
            s = backend.status(sub_id)
            if s.status in ("done", "failed", "cancelled"):
                return s
            if cancel_run and self._cancel[cancel_run].is_set():
                backend.cancel(sub_id)
                return backend.status(sub_id)
            time.sleep(0.1)

    def _materialize(self, run_id: str, graph: Graph, region, ref_uri: dict[str, str]) -> str:
        """Materialize an intermediate region's output to a durable, content-addressed parquet — reused
        across runs when the region's plan hash is unchanged. Runs in-process for a default region, or
        in the target worker's PROCESS for a placed region (C3)."""
        subg = self._subgraph(graph, region, ref_uri)
        key = self.base._plan_hash(subg, region.output_node)
        cached = self.base._cache_get(key)
        if cached and cached.get("uri") and self.base._output_exists(cached["uri"]):
            return cached["uri"]  # reuse — the upstream region didn't change
        out_dir = os.path.join(self.deps.workspace, "regions")
        os.makedirs(out_dir, exist_ok=True)
        self._prune_regions(out_dir)  # bound the handoff dir (a coarse GC; TTL/refcount is a later refinement)
        out_uri = os.path.join(out_dir, f"{region.id}_{key}.parquet")
        backend = self._backend_runner(region)
        if backend is self.base:
            with db.run_scope():
                eng = BuildEngine(subg, self.deps.resolve_adapter, self.deps.registry, full=True,
                                     node_builders=self.deps.node_builders, node_specs=self.deps.node_specs,
                                     pushdown=True, output_node=region.output_node)
                eng.relation(region.output_node).write_parquet(out_uri)
        else:
            sub = backend.run_unit(subg, region.output_node, out_uri)
            with self._lock:
                self._sub[run_id] = (backend, sub.run_id)
            s = self._await(backend, sub.run_id, cancel_run=run_id)
            if s.status != "done":
                raise RuntimeError(f"region {region.id} on {region.backend} {s.status}: {s.error}")
        self.base._cache_put(key, {"uri": out_uri, "table": region.id, "rows": None})
        return out_uri

    def _run_final(self, run_id: str, graph: Graph, region, ref_uri: dict[str, str]) -> RunStatus:
        """Run the final (target) region over the reduced graph, waiting for it — on the base runner
        (default) or on the region's target backend (a placed final region), so writes commit normally."""
        subg = self._subgraph(graph, region, ref_uri)
        plan = compiler.compile_plan(subg, region.output_node, self.deps.registry, self.deps.node_specs, self.deps.node_ir)
        backend = self._backend_runner(region)
        sub = backend.run(plan, subg, region.output_node, "local")
        with self._lock:
            self._sub[run_id] = (backend, sub.run_id)
        return self._await(backend, sub.run_id, cancel_run=run_id)

    def _subgraph(self, graph: Graph, region, ref_uri: dict[str, str]) -> Graph:
        # Rebuild the region as a graph, preserving each node's ORIGINAL incoming-edge order (the engine
        # feeds multi-input nodes like join positionally, so a swapped operand order silently corrupts
        # results). A cut input (source outside the region = an upstream materialized region) is replaced
        # IN PLACE by a ref-source reading that region's parquet.
        nm = g.node_map(graph)
        nodes = [nm[nid] for nid in region.node_ids if nid in nm]
        have = {n.id for n in nodes}
        edges: list[GraphEdge] = []
        for nid in region.node_ids:
            for e in g.incoming(graph, nid):  # original order, per node
                if e.source in region.node_ids:
                    edges.append(e)  # intra-region edge, unchanged
                else:  # a cut → read the upstream region's ref at THIS operand position
                    rid = f"__ref_{e.source}"
                    if rid not in have:
                        nodes.append(GraphNode(id=rid, type="source", position=Position(x=0, y=0),
                                               data={"config": {"uri": ref_uri[e.source]}}))
                        have.add(rid)
                    edges.append(GraphEdge(id=f"__e_{rid}_{nid}_{e.target_handle or 'in'}", source=rid,
                                           target=nid, source_handle=None, target_handle=e.target_handle,
                                           data=GraphEdgeData()))
        return Graph(id="_region", version=1, nodes=nodes, edges=edges)

    # -- status / cancel (a logical run, keyed by the overall run_id) ------- #
    def _mark(self, status: RunStatus, region, state: str) -> None:
        for p in status.per_node:
            if p.node_id in region.node_ids:
                p.status = state

    def _emit(self, graph: Graph, status: RunStatus) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                pass

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel(self, run_id: str) -> RunStatus:
        ev = self._cancel.get(run_id)
        if ev:
            ev.set()
        pair = self._sub.get(run_id)
        if pair:
            backend, sub_id = pair  # cancel on the backend that OWNS the in-flight region (pool, not base)
            try:
                backend.cancel(sub_id)
            except Exception:  # noqa: BLE001
                pass
        st = self.runs.get(run_id)
        if st and st.status in ("queued", "running"):
            st.status = "cancelled"
        return st
