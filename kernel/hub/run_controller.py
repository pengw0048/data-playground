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

    def plan(self, graph: Graph, target: str | None, sizes: dict | None = None):
        if not target:
            return []
        return planner.plan_regions(graph, target, self.deps.node_specs, self.place_fn,
                                    extra_requires=self._cost_requires(graph, target, sizes))

    def plan_summary(self, graph: Graph, target: str) -> list[dict]:
        """A human-facing execution plan: the regions this run splits into, each with its backend, the
        storage tier its boundary materializes to, and its estimated output size. Powers the UI 'run
        plan' preview — so the cost-aware placement + tiering is something you can SEE before running.
        Mirrors run(): a single default region or a shape we can't safely split actually runs as ONE
        in-process whole-graph pass, so report THAT (not a plan the run won't use)."""
        from hub import estimate as est_mod, placement
        try:
            sizes = est_mod.estimate_sizes(graph, self.deps.resolve_adapter, target=target)  # once — reused by plan()
        except Exception:  # noqa: BLE001
            sizes = {}

        def _rows(nid):
            e = sizes.get(nid)
            return (e.rows if e else None, e.confidence if e else "unknown")

        def _req_str(r) -> str:  # a compact "4×a100 · 64GB · zone=eu" for the UI
            parts = []
            if getattr(r, "gpu", None) or getattr(r, "gpu_type", None):
                parts.append(f"{r.gpu}×{r.gpu_type or 'gpu'}" if getattr(r, "gpu", None) else (r.gpu_type or "gpu"))
            if getattr(r, "cpu", None):
                parts.append(f"{r.cpu} cpu")
            if getattr(r, "mem", None):
                parts.append(str(r.mem))
            if getattr(r, "labels", None):
                parts += [f"{k}={v}" for k, v in r.labels.items()]
            return " · ".join(parts)

        def _hard_req(r) -> bool:
            # a capability the local/default host cannot provide (gpu or a placement label). mem/cpu are
            # SOFT: the local out-of-core engine spills/time-shares, so they're a perf concern, not "no
            # backend provides it". Only a hard need forced onto the default backend is truly unsatisfied.
            return bool(getattr(r, "gpu", None) or getattr(r, "gpu_type", None) or getattr(r, "labels", None))

        avail = self._available_summary()  # what the registered placement backends actually advertise

        regions = self.plan(graph, target, sizes=sizes)
        collapses = (len(regions) <= 1 and (not regions or regions[0].backend == "default")) \
            or not self._safe_to_split(graph, target, regions)
        if collapses:  # what run() will actually do: one default whole-graph region
            rows, conf = _rows(target)
            cone = g.upstream_chain(graph, target)  # only the nodes THIS run executes — not the whole canvas
            greq = placement.graph_requires(graph, self.deps.node_specs, nodes=cone)
            unsat = _hard_req(greq)  # collapsing runs the whole cone on the local default → no gpu/labels there
            return [{"id": "r_all", "outputNode": target, "backend": "default", "worker": None,
                     "nodeIds": [n.id for n in cone], "tier": None,
                     "rows": rows, "confidence": conf,
                     "requires": _req_str(greq) if unsat else "", "unsatisfied": unsat,
                     "available": avail if unsat else "",
                     "preflight": self._source_warnings(graph, [n.id for n in cone])}]
        out: list[dict] = []
        for i, r in enumerate(regions):
            final = i == len(regions) - 1
            tier = None if final else self._boundary_tier(r, regions)  # the final region isn't materialized
            rows, conf = _rows(r.output_node)
            # a HARD requirement (gpu/labels) that fell back to the local default (which lacks it): flag it.
            # Same criterion as the collapse branch — mem/cpu are soft (local spills), so they don't count.
            unsat = _hard_req(r.requires) and r.backend == "default"
            out.append({"id": r.id, "outputNode": r.output_node, "backend": r.backend, "worker": r.worker,
                        "nodeIds": sorted(r.node_ids), "tier": (tier.name if tier else None),
                        "rows": rows, "confidence": conf,
                        "requires": _req_str(r.requires), "unsatisfied": unsat,
                        "available": avail if unsat else "",
                        "preflight": self._source_warnings(graph, r.node_ids)})
        return out

    def _source_warnings(self, graph: Graph, node_ids) -> list:
        """Pre-flight the source nodes in a region: huge fragment count / cold-tier objects → warnings the
        run-plan can show BEFORE a full run hangs or OOMs (see hub.preflight). Best-effort — never raises."""
        from hub import preflight
        nm = g.node_map(graph)
        out: list = []
        for nid in node_ids:
            n = nm.get(nid)
            if n is None or n.type != "source":
                continue
            cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
            ref = str(cfg.get("uri") or cfg.get("table") or "").strip()
            if not ref:
                continue
            try:
                uri = self.deps.catalog.resolve_ref(ref)
                out.extend(preflight.source_preflight(uri)["warnings"])
            except Exception:  # noqa: BLE001 — a preflight probe must never break the plan
                continue
        return out

    def _available_summary(self) -> str:
        """A compact summary of what the registered placement backends advertise (via workers()) — so an
        unsatisfied pre-flight can say WHAT is available, not just 'no backend provides it'."""
        caps = []
        for r in self.deps.runners:
            wf = getattr(r, "workers", None)
            if not callable(wf):
                continue
            try:
                caps.extend(w.capacity for w in wf())
            except Exception:  # noqa: BLE001 — a backend that can't report capacity just isn't counted
                continue
        if not caps:
            return "no placement backend registered"
        gpu = max((c.gpu or 0 for c in caps), default=0)
        gtypes = sorted({c.gpu_type for c in caps if c.gpu_type})
        labels = sorted({f"{k}={v}" for c in caps for k, v in (c.labels or {}).items()})
        parts = []
        if gpu:
            parts.append(f"{gpu}×{'/'.join(gtypes) or 'gpu'}")
        elif gtypes:
            parts.append("/".join(gtypes))
        if labels:
            parts.append(", ".join(labels))
        return f"backends advertise: {' · '.join(parts)}" if parts else "no GPU/label capacity advertised"

    def _cost_requires(self, graph: Graph, target: str, sizes: dict | None = None) -> dict:
        """Cost-based placement input: a BLOCKING region whose estimated working set exceeds the local
        memory budget 'wants' a backend with more memory → a `ResourceSpec(mem=…)` the planner folds in.
        A strict no-op when nothing exceeds the budget, or when no backend can satisfy it (place_fn then
        falls back to the local default). Never breaks a run — estimation failure → no extra requirement."""
        from hub import estimate as est_mod
        from hub.models import ResourceSpec
        if sizes is None:  # reuse a caller-computed estimate (plan_summary) to avoid a second source-count pass
            try:
                sizes = est_mod.estimate_sizes(graph, self.deps.resolve_adapter, target=target)
            except Exception:  # noqa: BLE001 — placement is best-effort; a bad estimate must not block the run
                return {}
        from hub import placement
        budget = getattr(self.deps, "local_mem_bytes", 4 << 30)
        extra: dict = {}
        for n in g.upstream_chain(graph, target):
            if not est_mod.is_blocking(n.type):
                continue
            manual = placement.node_requires(n, self.deps.node_specs)
            if manual is not None and manual.mem:  # a declared mem pin is AUTHORITATIVE — never override it
                continue
            ws = self._working_set_bytes(graph, n.id, sizes)
            if ws is None or ws > budget:  # unknown or over budget → route to a bigger backend if one exists
                need = ws if ws is not None else budget * 2  # unknown input size → assume it exceeds local
                extra[n.id] = ResourceSpec(mem=f"{max(1, (need + (1 << 30) - 1) >> 30)}GB")
        return extra

    @staticmethod
    def _working_set_bytes(graph: Graph, nid: str, sizes: dict) -> "int | None":
        """A blocking op's memory need ≈ the bytes it must hold = sum of its inputs' output bytes;
        None (unknown) if any input's size is unknown."""
        total = 0
        for e in g.incoming(graph, nid):
            s = sizes.get(e.source)
            if s is None or s.bytes is None:
                return None
            total += s.bytes
        return total

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
            self._evict()
        self._emit(graph, status)
        threading.Thread(target=self._orchestrate, args=(run_id, graph, target, regions), daemon=True).start()
        return status

    def _evict(self) -> None:
        """Bound retained distributed-run state (called under self._lock). The sibling in-process
        runners cap self.runs the same way; RunController did not, so a long-lived kernel accreted a
        RunStatus per distributed run forever. Evict only TERMINAL runs (oldest first) so an in-flight
        run submitted early isn't dropped by later submissions (which would 404 its status poll)."""
        from hub.plugins.runner import _MAX_RUNS
        _terminal = {"done", "failed", "cancelled"}
        while len(self.runs) > _MAX_RUNS:
            victim = next((rid for rid, st in self.runs.items() if st.status in _terminal), None)
            if victim is None:
                break  # everything retained is still in-flight — exceed the cap rather than drop a live run
            self.runs.pop(victim, None)
            self._cancel.pop(victim, None)
            self._sub.pop(victim, None)

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
                        # carry the sub-run's node-attributed error onto the logical per-node status, so the
                        # per-node failure diagnosis shows in the distributed path too (not just the banner).
                        by_id = {p.node_id: p for p in status.per_node}
                        for sp in sub.per_node:
                            if sp.error and sp.node_id in by_id:
                                by_id[sp.node_id].error = sp.error
                        return
                else:
                    ref_uri[region.output_node] = self._materialize(run_id, graph, region, ref_uri, regions)
                self._mark(status, region, "done")
                from hub.plugins.runner import _step_progress
                status.progress = _step_progress(status)
                self._emit(graph, status)
            status.total_rows = status.rows_processed  # set the count BEFORE 'done' (a poll reads terminal
            status.progress = 1.0                      # status eagerly; the finally would set it too late)
            status.stalled = False
            status.ms = int((time.time() - started) * 1000)
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
        import shutil
        try:
            files = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
            for f in files[:-keep]:
                try:  # a worker-direct handoff is a DIRECTORY of shards — os.remove can't delete a dir
                    shutil.rmtree(f) if os.path.isdir(f) else os.remove(f)
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

    def _boundary_tier(self, region, regions):
        """The tier to materialize `region`'s output on: the cheapest one reachable by BOTH its producer
        backend and every consuming region's backend (local for a local→local handoff, a shared object
        store when a remote backend is involved). None → no common tier (misconfiguration → caller warns)."""
        from hub import tiers as tier_mod
        prod = tier_mod.backend_reach(self._backend_runner(region), region.backend == "default")
        reach = [prod]
        for rc in regions:  # regions that read THIS boundary as a ref-source
            if any(ci[0] == region.output_node for ci in rc.cut_inputs):
                reach.append(tier_mod.backend_reach(self._backend_runner(rc), rc.backend == "default"))
        return tier_mod.pick_tier(tier_mod.tiers(self.deps.workspace), reach)

    def _move_tier(self, src_uri: str, dst_uri: str, dst_tier) -> None:
        """Copy a materialized region parquet across tiers (cheaper than recomputing). DuckDB streams it;
        httpfs handles an object-store endpoint on either side."""
        from hub.plugins.adapters import is_object_uri
        with db.run_scope():
            if dst_tier.is_object or is_object_uri(src_uri):
                db.ensure_object_store()      # register object-store creds for whichever side is s3/gs
            if not dst_tier.is_object:
                os.makedirs(dst_tier.prefix, exist_ok=True)  # DuckDB won't create a local file's parent dir
            # src may be a single file OR a directory of shards (a worker-direct write) — the adapter's
            # scan handles both (local isdir → dir-scan, object prefix → glob); consolidate into dst.
            self.deps.resolve_adapter(src_uri).scan(src_uri).write_parquet(dst_uri)

    def _materialize(self, run_id: str, graph: Graph, region, ref_uri: dict[str, str], regions=None) -> str:
        """Materialize an intermediate region's output to a durable, content-addressed parquet — reused
        across runs when the region's plan hash is unchanged. Runs in-process for a default region, or
        in the target worker's PROCESS for a placed region (C3). The output lands on the tier reachable
        by both producer and consumers (local, or a shared object store for a remote handoff — Phase C)."""
        from hub import tiers as tier_mod
        subg = self._subgraph(graph, region, ref_uri)
        key = self.base._plan_hash(subg, region.output_node)
        picked = self._boundary_tier(region, regions or [])
        if picked is None:
            # A remote handoff with NO shared tier: materializing to local would silently produce a result
            # the consuming (remote) backend cannot read — a wrong run dressed up as success. Fail fast with
            # the fix, instead of the old warn-and-use-local (which routed data to a dead end).
            raise RuntimeError(
                f"region '{region.output_node}' hands off to a backend with no storage tier reachable by "
                "both sides — a remote backend can't see a local file. Configure a shared object store "
                "(DP_STORAGE_URL=s3://…) or keep the producing and consuming nodes on one backend.")
        tier = picked
        ckey = f"{key}@{tier.name}"  # tier in the cache key: a local vs object copy are distinct entries
        cached = self.base._cache_get(ckey)
        if cached and cached.get("uri") and self.base._output_exists(cached["uri"]):
            return cached["uri"]  # reuse — the upstream region didn't change (on this tier)
        # C3 auto data-movement: a prior run materialized this exact region on ANOTHER tier → COPY it to
        # the tier this run needs (cheaper than recomputing), e.g. a local result now feeding a remote step.
        for other in tier_mod.tiers(self.deps.workspace).values():
            if other.name == tier.name:
                continue
            alt = self.base._cache_get(f"{key}@{other.name}")
            if alt and alt.get("uri") and self.base._output_exists(alt["uri"]):
                dst = tier.uri(f"{region.id}_{key}.parquet")
                self._move_tier(alt["uri"], dst, tier)
                self.base._cache_put(ckey, {"uri": dst, "table": region.id, "rows": None})
                return dst
        if tier.is_object:
            db.ensure_object_store()
        else:
            os.makedirs(tier.prefix, exist_ok=True)
            self._prune_regions(tier.prefix)  # bound the LOCAL handoff dir (coarse GC; TTL/refcount later)
        out_uri = tier.uri(f"{region.id}_{key}.parquet")
        backend = self._backend_runner(region)
        result_uri = out_uri
        if backend is self.base:
            with db.run_scope():
                eng = BuildEngine(subg, self.deps.resolve_adapter, self.deps.registry, full=True,
                                     node_builders=self.deps.node_builders, node_specs=self.deps.node_specs,
                                     pushdown=True, output_node=region.output_node)
                eng.relation(region.output_node).write_parquet(out_uri)
        else:
            # a placed backend may write a DIRECTORY of shards (worker-direct parallel write, e.g. Ray
            # Data) rather than the single file we suggested — honor the uri it actually produced. The
            # ref-read / _output_exists / _move_tier paths all accept a parts-dir as well as a file.
            sub = backend.run_unit(subg, region.output_node, out_uri, requires=region.requires)
            with self._lock:
                self._sub[run_id] = (backend, sub.run_id)
            s = self._await(backend, sub.run_id, cancel_run=run_id)
            if s.status != "done":
                raise RuntimeError(f"region {region.id} on {region.backend} {s.status}: {s.error}")
            result_uri = s.output_uri or out_uri
        self.base._cache_put(ckey, {"uri": result_uri, "table": region.id, "rows": None})
        return result_uri

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
        # carry the canvas's requirements so a region's cache key reflects a package-version edit
        # (a transform in a locally-run region can import them) — plan_hash folds them (P0-CACHE-01).
        return Graph(id="_region", version=1, nodes=nodes, edges=edges,
                     requirements=getattr(graph, "requirements", None) or [])

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
