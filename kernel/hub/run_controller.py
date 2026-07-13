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

import concurrent.futures as cf
import contextlib
import logging
import os
import threading
import time
import uuid

from hub import compiler, db
from hub import graph as g
from hub import planner
from hub.executors.engine import BuildEngine
from hub.models import (Graph, GraphEdge, GraphEdgeData, GraphNode, PerNodeStatus, Position, RunStatus)


def _region_concurrency() -> int:
    """Max intermediate regions materialized concurrently (DP_REGION_CONCURRENCY, default 4)."""
    try:
        return max(1, int(os.environ.get("DP_REGION_CONCURRENCY", "4")))
    except ValueError:
        return 4


class _RegionMaterialization(str):
    """A region URI plus the temporary cache owner protecting it for the consuming run."""

    def __new__(cls, uri: str, cache_pin=None):
        value = super().__new__(cls, uri)
        value.cache_pin = cache_pin
        return value


class RunController:
    cancel_acknowledges_stop = True  # cancelled is published only after in-flight region tasks unwind

    name = "run-controller"

    def __init__(self, deps, base, place_fn):
        self.deps = deps
        self.base = base                 # the in-process LocalRunner (does the real per-region work)
        self.place_fn = place_fn         # requires -> (backend, worker) | None
        self.on_status = None
        self.on_complete = None
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}  # user intent only
        self._stop: dict[str, threading.Event] = {}    # user cancel OR sibling-failure execution stop
        self._sub: dict[str, dict] = {}  # overall run_id -> {sub_run_id: backend} for ALL concurrent sub-runs
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
            # schema-aware (measured vector/decimal widths), matching the real run's placement (start_run
            # hands the same schema-aware estimate to run()); best-effort — coarse widths on failure.
            try:
                from hub.executors.schema import schema_for_graph
                schemas = schema_for_graph(graph, self.deps.resolve_adapter, self.deps.registry,
                                           self.deps.node_builders, self.deps.node_specs)
            except Exception:  # noqa: BLE001
                schemas = None
            sizes = est_mod.estimate_sizes(graph, self.deps.resolve_adapter, target=target, schemas=schemas)  # once — reused by plan()
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
    def run(self, graph: Graph, target: str | None, uid: str | None = None,
            sizes: dict | None = None) -> RunStatus | None:
        """Start a multi-region run; return None if the graph is a single default region (caller uses
        the base runner unchanged). `uid` carries the caller so a per-user backend preference routes
        default regions to the isolated child too (P0-EXEC-01), not just the global default. `sizes` is
        the caller's already-computed schema+actual-aware estimate — reused for cost-based placement so
        it routes on the SAME measured widths the confirm-gate saw (else placement re-estimates coarse)."""
        regions = self.plan(graph, target, sizes=sizes)
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
            self._stop[run_id] = threading.Event()
            self._evict()
        self._emit(graph, status)
        threading.Thread(target=self._orchestrate, args=(run_id, graph, target, regions, uid), daemon=True).start()
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
            self._stop.pop(victim, None)
            self._sub.pop(victim, None)

    def _orchestrate(self, run_id: str, graph: Graph, target: str, regions, uid: str | None = None) -> None:
        status = self.runs[run_id]
        cancel = self._cancel[run_id]
        stop = self._stop.setdefault(run_id, threading.Event())
        if cancel.is_set():
            stop.set()
        status.status = "running"
        self._emit(graph, status)
        started = time.time()
        ref_uri: dict[str, str] = {}
        ref_lock = threading.Lock()
        region_cache_pins: list = []
        failure: Exception | None = None
        from hub.plugins.runner import _step_progress
        try:
            # Materialize the INTERMEDIATE regions concurrently, respecting the region DAG: a region is
            # ready once every region it reads (via cut_inputs) has materialized. Independent regions run
            # in parallel — a wave scheduler where no task ever blocks on another, so the pool can't
            # deadlock. `regions` is topo-ordered; the last is the target's (final) region, run afterward.
            intermediates, final_region = list(regions[:-1]), regions[-1]
            interm_outs = {r.output_node for r in intermediates}

            def _region_deps(r):  # the intermediate-region output_nodes this region reads (O(1) membership)
                return {ci[0] for ci in r.cut_inputs if ci[0] in interm_outs and ci[0] != r.output_node}

            done_nodes: set[str] = set()
            pending = list(intermediates)
            cap = max(1, min(len(pending) or 1, _region_concurrency()))
            inflight: dict = {}
            # Explicit lifecycle (not `with`) lets us publish a nonterminal stalled reconciliation state
            # before waiting for every sibling writer to acknowledge stop.
            ex = cf.ThreadPoolExecutor(max_workers=cap, thread_name_prefix="dp-region")
            try:
                try:
                    while (pending or inflight) and not stop.is_set():
                        for r in [r for r in pending if _region_deps(r) <= done_nodes]:
                            pending.remove(r)
                            self._mark(status, r, "running")  # status mutated only on THIS thread
                            with ref_lock:
                                snap = dict(ref_uri)  # per-region snapshot; no concurrent dict read/write
                            future = ex.submit(
                                self._materialize, run_id, graph, r, snap, regions, uid)
                            inflight[future] = r
                        if not inflight:
                            break  # unsatisfiable dependency (defensive; topo planning rules it out)
                        for fut in cf.wait(
                                list(inflight), return_when=cf.FIRST_COMPLETED).done:
                            r = inflight.pop(fut)
                            materialized = fut.result()
                            uri = str(materialized)
                            pin = getattr(materialized, "cache_pin", None)
                            if pin is not None:
                                try:
                                    pin.check()
                                except Exception:
                                    self._close_region_pin(pin)
                                    raise
                                region_cache_pins.append(pin)
                            with ref_lock:
                                ref_uri[r.output_node] = uri
                            done_nodes.add(r.output_node)
                            self._mark(status, r, "done")
                            status.progress = _step_progress(status)
                            self._emit(graph, status)
                except Exception as exc:  # a failed region must stop every still-live sibling first
                    self._signal_execution_stop(run_id)
                    if not cancel.is_set():
                        failure = exc
                        status.status = "running"
                        status.stalled = True
                        status.error = "region failure is waiting for sibling stop acknowledgement"
                        self._emit(graph, status)
            finally:
                # Futures left in this map were never consumed by the scheduler (a sibling failed or the
                # overall run was cancelled). If one finishes after this thread moves on, release the
                # result-reader owner it acquired rather than waiting for lease expiry.
                def _release_unconsumed(future) -> None:
                    try:
                        result = future.result()
                        pin = getattr(result, "cache_pin", None)
                        if pin is not None:
                            self._close_region_pin(pin)
                    except Exception:  # noqa: BLE001 — a failed future owns no returned cache pin
                        pass

                for future in inflight:
                    future.add_done_callback(_release_unconsumed)
                # User cancellation and sibling failure share the execution-stop signal, but not outcome
                # semantics. In both cases terminal publication waits until local futures unwind and every
                # backend _await observes a truthful stop acknowledgement. If one never does, this thread
                # remains here and the durable status stays running+stalled with tracking intact.
                ex.shutdown(wait=stop.is_set(), cancel_futures=True)
            if failure is not None:
                status.stalled = False
                raise failure
            if cancel.is_set():
                status.stalled = False
                status.status = "cancelled"
                return
            # the FINAL (target) region, now that every upstream region it reads is materialized
            region = final_region
            for pin in region_cache_pins:
                pin.check()
            self._mark(status, region, "running")
            sub = self._run_final(run_id, graph, region, ref_uri, uid)
            status.output_uri, status.output_table = sub.output_uri, sub.output_table
            status.rows_processed = sub.rows_processed
            if sub.status != "done":
                status.status = sub.status
                status.error = sub.error
                self._mark(status, region, "failed")
                by_id = {p.node_id: p for p in status.per_node}
                for sp in sub.per_node:
                    if sp.error and sp.node_id in by_id:
                        by_id[sp.node_id].error = sp.error
                return
            self._mark(status, region, "done")
            status.progress = _step_progress(status)
            self._emit(graph, status)
            status.total_rows = status.rows_processed  # set the count BEFORE 'done' (a poll reads terminal
            status.progress = 1.0                      # status eagerly; the finally would set it too late)
            status.stalled = False
            status.ms = int((time.time() - started) * 1000)
            status.status = "done"
        except Exception as e:  # noqa: BLE001
            # Once a region has failed, a later user cancel may re-send stop requests but must not erase
            # the primary outcome. Before a failure is observed, user intent still settles as cancelled.
            status.status = "failed" if failure is not None else (
                "cancelled" if cancel.is_set() else "failed")
            status.stalled = False
            if status.status == "failed":
                status.error = f"{type(e).__name__}: {e}"
            else:
                status.error = None
        finally:
            # Cache refs protect intermediate managed attempts from a concurrent same-hash replacement
            # until the consuming final region has stopped. Release only after _run_final returns/fails.
            for pin in reversed(region_cache_pins):
                self._close_region_pin(pin)
            if status.status in ("failed", "cancelled"):  # don't leave earlier/other regions stuck
                for p in status.per_node:
                    if p.status != "done":
                        p.status = status.status
            status.ms = int((time.time() - started) * 1000)
            status.total_rows = status.rows_processed
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
                self._stop.pop(run_id, None)
                self._sub.pop(run_id, None)
            if self.on_complete:
                try:
                    self.on_complete(graph, target, status)
                except Exception:  # noqa: BLE001
                    pass

    def _region_output_exists(self, uri: str) -> bool:
        """A Ray attempt is reusable only when its last-written commit manifest is valid.

        Stable/legacy handoffs keep the runner's ordinary existence contract. This scopes the manifest
        requirement to immutable attempt prefixes, so other placed backends remain compatible.
        """
        from hub.handoff import (is_attempt_uri, managed_read_lease, prepare_attempt_commit,
                                 read_manifest, validate_shards)
        if is_attempt_uri(uri):
            try:
                prepare_attempt_commit(uri)
                with managed_read_lease(
                        uri, owner="region-validation", allow_committed=True):
                    manifest = read_manifest(uri)
                    if manifest is None or not validate_shards(uri, manifest):
                        return False
                    return self.base._output_exists(uri)
            except (FileNotFoundError, RuntimeError):
                return False
        return self.base._output_exists(uri)

    def _safe_to_split(self, graph: Graph, target: str, regions) -> bool:
        """Refuse to split (→ run the whole graph in-process, correct but unplaced) for shapes the
        single-port parquet handoff can't represent yet, rather than silently corrupt data:
        - a cross-region cut off a NON-default output port (a multi-output node / section named port);
        - an intermediate `write` (materializing it would drop its commit + catalog side-effect)."""
        nm = g.node_map(graph)
        if any(sh not in (None, "out") for r in regions for (_u, sh, _i, _t) in r.cut_inputs):
            return False
        return not any(nm[nid].type == "write" and nid != target for r in regions for nid in r.node_ids)

    def _backend_runner(self, region, uid: str | None = None):
        """The runner that executes a region: for a 'default' (unplaced) region, the isolated child the
        kernel uses when the kernel is the selected backend — else the in-process base; for a named
        region, that backend (pool / pod / Ray) so a PLACED region physically runs on its worker (C3).
        `uid` honors a per-user backend preference on the execution path (tier callers pass none = the
        global/default selection, which is fine — tier reachability is identical for base vs subprocess)."""
        if region.backend == "default":
            # P0-EXEC-01: don't run a default region's user code / heavy ops in the HUB pid when the
            # per-canvas kernel is selected — route it to the SAME killable, deadline-bounded, sandboxed
            # child (local-subprocess) a single-region kernel run uses. An explicit in-process selection
            # (local-out-of-core) or a pool/Ray default keeps the base, unchanged.
            if self.deps.chosen_backend(uid) == "kernel":
                return next((r for r in self.deps.runners if r.name == "local-subprocess"), self.base)
            return self.base
        return next((r for r in self.deps.runners if r.name == region.backend), self.base)

    def _track_sub(self, run_id: str, backend, sub_id: str) -> None:
        with self._lock:
            self._sub.setdefault(run_id, {})[sub_id] = backend  # ALL concurrent sub-runs, so cancel() reaches each

    def _untrack_sub(self, run_id: str, sub_id: str) -> None:
        with self._lock:
            self._sub.get(run_id, {}).pop(sub_id, None)

    def _signal_execution_stop(self, run_id: str) -> None:
        """Stop every execution owned by a logical run without deciding its terminal outcome.

        User cancellation and sibling failure both use this signal. The former sets ``_cancel`` first;
        the latter deliberately does not, so a primary region failure remains ``failed`` after every
        sibling has acknowledged stop. Snapshot under the controller lock, but never call a backend
        while holding it: a backend may synchronously complete and untrack its sub-run from another
        thread.
        """
        with self._lock:
            stop = self._stop.setdefault(run_id, threading.Event())
            stop.set()
            subs = list(self._sub.get(run_id, {}).items())
        for sub_id, backend in subs:
            try:
                backend.cancel(sub_id)
            except Exception:  # noqa: BLE001 — _await keeps polling/retrying until stop is proven
                logging.getLogger("hub").exception(
                    "region backend stop request failed; awaiting terminal acknowledgement")

    def _await(self, backend, sub_id: str, cancel_run: str | None = None) -> RunStatus:
        # Capture the execution-stop Event ONCE (don't subscript a tracking dict each poll). It covers
        # both user cancellation and sibling-failure reconciliation, while _cancel remains user intent
        # only and therefore cannot accidentally turn a genuine region failure into "cancelled".
        ev = ((self._stop.get(cancel_run) or self._cancel.get(cancel_run))
              if cancel_run else None)
        cancel_sent = False
        from hub.backends import stop_acknowledged
        while True:
            s = backend.status(sub_id)
            if stop_acknowledged(backend, s):
                return s
            if ev is not None and ev.is_set() and not cancel_sent:
                try:
                    backend.cancel(sub_id)
                except Exception:  # noqa: BLE001 — a request error is not proof the worker stopped
                    logging.getLogger("hub").exception(
                        "region backend stop request failed; awaiting terminal acknowledgement")
                else:
                    cancel_sent = True
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

    @staticmethod
    def _region_pin_ttl() -> float:
        """Keep a cache-reader owner beyond the longest configured run deadline."""
        try:
            deadline = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
        except ValueError:
            deadline = 3600.0
        return max(300.0, deadline + 300.0)

    @staticmethod
    def _close_region_pin(pin) -> None:
        if pin is not None:
            try:
                pin.close()
            except Exception:  # noqa: BLE001 — a stale cache hit is safe to ignore
                logging.getLogger("hub").exception(
                    "region result-cache pin cleanup failed")

    @contextlib.contextmanager
    def _region_source_lease_scope(self, graph: Graph, target: str, *,
                                   run_id: str, region_id: str):
        """Pin every exact source generation while this parent-owned region actually reads it."""
        from hub.handoff import managed_read_lease

        stack = contextlib.ExitStack()
        guards = []
        try:
            try:
                for uri in g.all_upstream_source_uris(graph, target):
                    guards.append(stack.enter_context(managed_read_lease(
                        uri, owner=f"region-source:{run_id}:{region_id}",
                        ttl_seconds=self._region_pin_ttl())))
            except Exception:  # noqa: BLE001 — ownership details remain in server logs
                logging.getLogger("hub").exception(
                    "region source ownership lease acquisition failed")
                raise RuntimeError("region source ownership lease unavailable") from None
            yield guards
        finally:
            # A failed release retains a conservative lease until DB expiry. It must not replace a
            # primary execution error or retroactively invalidate output after the explicit fence below.
            try:
                stack.close()
            except Exception:  # noqa: BLE001
                logging.getLogger("hub").exception(
                    "region source ownership lease cleanup failed")

    @staticmethod
    def _check_region_source_leases(guards) -> None:
        try:
            for guard in guards:
                guard.check()
        except Exception:  # noqa: BLE001 — fail closed before any cache/pointer publication
            logging.getLogger("hub").exception(
                "region source ownership lease was lost during execution")
            raise RuntimeError("region source ownership lease was lost") from None

    @staticmethod
    def _assert_region_attempt(uri: str, logical_uri: str, *,
                               expected_run_id: str | None = None,
                               allowed_states: tuple[str, ...] = (
                                   "allocated", "writing", "committed", "published")) -> dict:
        """Attest a placed backend's returned object against the parent ownership registry."""
        from hub import metadb
        from hub.handoff import is_attempt_uri
        from hub.plugins.adapters import is_object_uri

        normalized = str(uri or "").rstrip("/")
        logical = str(logical_uri).rstrip("/")
        if not is_object_uri(normalized) or not is_attempt_uri(normalized):
            raise RuntimeError("placed backend did not return an exact managed region attempt")
        return metadb.attest_object_attempt(
            normalized, logical_uri=logical, kind="region",
            expected_run_id=expected_run_id,
            allowed_states=allowed_states,
        )

    def _allocate_region_attempt(self, *, logical_uri: str, run_id: str,
                                 region_id: str, cache_key: str) -> str:
        """Allocate one parent-owned generation before a local producer/copy can write."""
        try:
            from hub.handoff import allocate_attempt, physical_attempt_uri
            invocation = uuid.uuid4().hex
            handle = allocate_attempt(
                logical_uri=logical_uri, kind="region", run_id=run_id,
                # One writer invocation owns one generation. A concurrent retry of the same logical
                # run/region must not recover and overwrite a still-live writer's physical prefix.
                allocation_key=(
                    f"region-handoff:{run_id}:{region_id}:{cache_key}:{invocation}"),
                uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                    logical_uri, namespace, generation, attempt_id),
            )
            self._assert_region_attempt(
                handle["uri"], logical_uri, expected_run_id=run_id)
            return handle["uri"]
        except Exception:  # noqa: BLE001 — provider/DB details stay in server logs
            logging.getLogger("hub").exception(
                "region object-attempt allocation failed")
            raise RuntimeError("region object lifecycle allocation failed") from None

    @staticmethod
    def _abandon_region_attempt(uri: str) -> None:
        """Terminalize a stopped, unpublished writer without deleting provider objects inline."""
        from hub import metadb
        from hub.handoff import discard_attempt

        try:
            abandoned = metadb.abandon_committed_object_attempt(uri)
            if abandoned:
                return
        except Exception:  # noqa: BLE001 — uncertain ownership must retain provider data
            logging.getLogger("hub").exception(
                "committed region object-attempt abandon failed")
            return
        discard_attempt(uri)

    def _acquire_region_cache(self, cache_key: str, *, owner: str,
                              logical_uri: str | None = None) -> _RegionMaterialization | None:
        """Atomically read and temporarily own one reusable region-cache generation."""
        # SQLite cannot enforce the cache-row FOR UPDATE lock used by PostgreSQL. A concurrent pointer
        # replacement can therefore retire the generation observed by one acquire before its follow-up
        # identity check. Release that stale pin and retry the atomic read; never consume it unowned.
        for _attempt in range(3):
            doc, pin = self.base._cache_acquire(
                cache_key, owner, self._region_pin_ttl())
            uri = doc.get("uri") if isinstance(doc, dict) else None
            keep_pin = False
            if not uri:
                self._close_region_pin(pin)
                return None
            try:
                if logical_uri is not None:
                    if pin is None:
                        return None  # object attempts require the DB-backed result_reader ref
                    self._assert_region_attempt(
                        uri, logical_uri, allowed_states=("published",))
                if not self._region_output_exists(uri):
                    continue
                if pin is not None:
                    pin.check()
                result = _RegionMaterialization(str(uri), pin)
                keep_pin = True
                return result
            except Exception:  # noqa: BLE001 — retry a concurrently replaced/unattested pointer
                logging.getLogger("hub").warning(
                    "region result-cache attestation raced a pointer replacement",
                    exc_info=True)
            finally:
                # Ownership transfers only with the returned wrapper. Every miss releases immediately.
                if not keep_pin:
                    self._close_region_pin(pin)
        return None

    def _publish_region_attempt(self, *, cache_key: str, uri: str, logical_uri: str,
                                table: str, run_id: str, cancel,
                                expected_attempt_run_id: str) -> _RegionMaterialization:
        """Commit inventory, publish the cache owner, then pin the exact current generation."""
        try:
            self._assert_region_attempt(
                uri, logical_uri, expected_run_id=expected_attempt_run_id)
            from hub.handoff import prepare_attempt_commit
            prepare_attempt_commit(uri)
        except Exception:  # noqa: BLE001 — provider/DB details stay in server logs
            self._abandon_region_attempt(uri)
            logging.getLogger("hub").exception(
                "region object-attempt commit preparation failed")
            raise RuntimeError("region object lifecycle publication failed") from None
        if cancel.is_set():
            self._abandon_region_attempt(uri)
            raise RuntimeError("run cancelled before region publication")
        try:
            self.base._cache_put(
                cache_key, {"uri": uri, "table": table, "rows": None})
        except Exception:  # noqa: BLE001 — commit outcome may be uncertain after a transport error
            logging.getLogger("hub").exception(
                "region result-cache publication reported failure")
            # A transaction may have committed before its caller observed an error. Read-back plus a
            # result_reader ref is the durable receipt; never report failure while that owner exists.
            current = self._acquire_region_cache(
                cache_key, owner=f"region:{run_id}:{table}", logical_uri=logical_uri)
            if current is not None:
                if str(current) != uri:
                    self._abandon_region_attempt(uri)
                return current
            self._abandon_region_attempt(uri)
            raise RuntimeError("region object lifecycle publication failed") from None
        current = self._acquire_region_cache(
            cache_key, owner=f"region:{run_id}:{table}", logical_uri=logical_uri)
        if current is None:
            self._abandon_region_attempt(uri)
            raise RuntimeError("region object lifecycle publication failed") from None
        # A concurrent same-hash publisher may replace our generation between put and acquire. The
        # atomically pinned winner is equivalent content and is the only safe URI to consume.
        if str(current) != uri:
            self._abandon_region_attempt(uri)
        return current

    def _move_tier(self, src_uri: str, dst_uri: str, dst_tier, cancel,
                   run_id: str | None = None) -> None:
        """Copy a materialized region parquet across tiers (cheaper than recomputing). DuckDB streams it;
        httpfs handles an object-store endpoint on either side."""
        from hub.handoff import is_attempt_uri
        from hub.plugins.adapters import is_object_uri
        with db.run_scope():
            if dst_tier.is_object or is_object_uri(src_uri):
                try:
                    db.ensure_object_store()  # register credentials for whichever side is s3/gs
                except Exception:  # noqa: BLE001 — provider details stay in server logs
                    logging.getLogger("hub").exception(
                        "region tier-copy object-store setup failed")
                    raise RuntimeError("region object lifecycle setup failed") from None
            if not dst_tier.is_object:
                os.makedirs(dst_tier.prefix, exist_ok=True)  # DuckDB won't create a local file's parent dir
            # src may be a single file OR a directory of shards (a worker-direct write) — the adapter's
            # scan handles both (local isdir → dir-scan, object prefix → glob); consolidate into dst.
            rel = self.deps.resolve_adapter(src_uri).scan(src_uri)
            exact_attempt = is_attempt_uri(dst_uri)
            write_uri = (dst_uri.rstrip("/") + "/part-00000.parquet"
                         if exact_attempt else dst_uri)
            result = self.base._adapter_write(
                self.deps.resolve_adapter(write_uri), write_uri, rel, "overwrite", cancel)
            if exact_attempt:
                if cancel.is_set():
                    raise RuntimeError("run cancelled before region manifest publication")
                try:
                    from hub.handoff import write_manifest
                    schema = list(zip(rel.columns, (str(t) for t in rel.types)))
                    write_manifest(
                        dst_uri, run_id=run_id or f"region-copy-{uuid.uuid4().hex}",
                        rows=int(result.get("rows") or 0), schema=schema)
                except Exception:  # noqa: BLE001 — provider details stay in server logs
                    logging.getLogger("hub").exception(
                        "region object-attempt manifest write failed")
                    raise RuntimeError("region object lifecycle manifest failed") from None

    def _materialize(self, run_id: str, graph: Graph, region, ref_uri: dict[str, str],
                     regions=None, uid: str | None = None) -> str:
        """Materialize an intermediate region's output to a durable, content-addressed parquet — reused
        across runs when the region's plan hash is unchanged. Runs in-process for a default region, or
        in the target worker's PROCESS for a placed region (C3). The output lands on the tier reachable
        by both producer and consumers (local, or a shared object store for a remote handoff — Phase C)."""
        from hub import tiers as tier_mod
        cancel = (self._stop.get(run_id) or self._cancel.get(run_id)
                  or threading.Event())
        if cancel.is_set():
            raise RuntimeError("run cancelled before region materialization")
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
        logical_uri = tier.uri(f"{region.id}_{key}.parquet")
        cached = self._acquire_region_cache(
            ckey, owner=f"region:{run_id}:{region.id}",
            logical_uri=logical_uri if tier.is_object else None)
        if cached is not None:
            return cached  # reuse — pin survives until this run's final region has stopped
        # C3 auto data-movement: a prior run materialized this exact region on ANOTHER tier → COPY it to
        # the tier this run needs (cheaper than recomputing), e.g. a local result now feeding a remote step.
        for other in tier_mod.tiers(self.deps.workspace).values():
            if other.name == tier.name:
                continue
            other_logical = other.uri(f"{region.id}_{key}.parquet")
            alt = self._acquire_region_cache(
                f"{key}@{other.name}", owner=f"region-copy:{run_id}:{region.id}",
                logical_uri=other_logical if other.is_object else None)
            if alt is None:
                continue
            alt_pin = getattr(alt, "cache_pin", None)
            try:
                if cancel.is_set():
                    raise RuntimeError("run cancelled before region tier copy")
                if tier.is_object:
                    dst = self._allocate_region_attempt(
                        logical_uri=logical_uri, run_id=run_id,
                        region_id=region.id, cache_key=ckey)
                    try:
                        self._move_tier(str(alt), dst, tier, cancel, run_id=run_id)
                        if alt_pin is not None:
                            alt_pin.check()
                        return self._publish_region_attempt(
                            cache_key=ckey, uri=dst, logical_uri=logical_uri,
                            table=region.id, run_id=run_id, cancel=cancel,
                            expected_attempt_run_id=run_id)
                    except Exception:
                        self._abandon_region_attempt(dst)  # copy is synchronous; writer has stopped
                        raise
                self._move_tier(str(alt), logical_uri, tier, cancel, run_id=run_id)
                if alt_pin is not None:
                    alt_pin.check()
                self.base._cache_put(
                    ckey, {"uri": logical_uri, "table": region.id, "rows": None})
                return logical_uri
            finally:
                self._close_region_pin(alt_pin)
        if tier.is_object:
            try:
                db.ensure_object_store()
            except Exception:  # noqa: BLE001 — provider details stay in server logs
                logging.getLogger("hub").exception(
                    "region object-store setup failed")
                raise RuntimeError("region object lifecycle setup failed") from None
        else:
            os.makedirs(tier.prefix, exist_ok=True)
        backend = self._backend_runner(region, uid)
        if backend is self.base:
            try:
                result_uri = logical_uri
                if tier.is_object:
                    result_uri = self._allocate_region_attempt(
                        logical_uri=logical_uri, run_id=run_id,
                        region_id=region.id, cache_key=ckey)
                with self._region_source_lease_scope(
                        subg, region.output_node, run_id=run_id,
                        region_id=region.id) as source_guards:
                    with db.run_scope():
                        eng = BuildEngine(
                            subg, self.deps.resolve_adapter, self.deps.registry, full=True,
                            node_builders=self.deps.node_builders, node_specs=self.deps.node_specs,
                            pushdown=True, output_node=region.output_node)
                        rel = eng.relation(region.output_node)
                        write_uri = (result_uri.rstrip("/") + "/part-00000.parquet"
                                     if tier.is_object else result_uri)
                        result = self.base._adapter_write(
                            self.deps.resolve_adapter(write_uri), write_uri,
                            rel, "overwrite", cancel)
                        rows = int(result.get("rows") or 0)
                        schema = list(zip(rel.columns, (str(t) for t in rel.types)))
                        # The shard write is the point at which lazy source scans have completed. Fence
                        # every exact source generation before writing a commit manifest or cache pointer.
                        self._check_region_source_leases(source_guards)
                if tier.is_object:
                    if cancel.is_set():
                        raise RuntimeError("run cancelled before region manifest publication")
                    try:
                        from hub.handoff import write_manifest
                        write_manifest(
                            result_uri, run_id=run_id, rows=rows, schema=schema)
                    except Exception:  # noqa: BLE001 — provider details stay in server logs
                        logging.getLogger("hub").exception(
                            "region object-attempt manifest write failed")
                        raise RuntimeError(
                            "region object lifecycle manifest failed") from None
                if tier.is_object:
                    return self._publish_region_attempt(
                        cache_key=ckey, uri=result_uri, logical_uri=logical_uri,
                        table=region.id, run_id=run_id, cancel=cancel,
                        expected_attempt_run_id=run_id)
                if not self._region_output_exists(result_uri):
                    raise RuntimeError(
                        f"region {region.id} on {region.backend} returned an unreadable handoff")
                self.base._cache_put(
                    ckey, {"uri": result_uri, "table": region.id, "rows": None})
                return result_uri
            except Exception:
                if tier.is_object and "result_uri" in locals():
                    self._abandon_region_attempt(result_uri)  # in-process writer has stopped
                raise

        # A placed backend owns its writer lifecycle. It receives only the stable logical target and must
        # return one exact, parent-registered region attempt after its worker can no longer mutate it.
        # Backends that own and renew exact parent-side source leases fence them through worker reaping
        # themselves. Other backends remain protected by the controller's generic outer lease scope.
        source_scope = (contextlib.nullcontext([])
                        if getattr(backend, "manages_source_leases", False)
                        else self._region_source_lease_scope(
                            subg, region.output_node, run_id=run_id,
                            region_id=region.id))
        with source_scope as source_guards:
            sub = backend.run_unit(
                subg, region.output_node, logical_uri, requires=region.requires)
            self._track_sub(run_id, backend, sub.run_id)
            try:
                s = self._await(backend, sub.run_id, cancel_run=run_id)
            finally:
                self._untrack_sub(run_id, sub.run_id)
            if s.status != "done":
                raise RuntimeError(
                    f"region {region.id} on {region.backend} {s.status}: {s.error}")
            if source_guards:
                self._check_region_source_leases(source_guards)
        result_uri = s.output_uri or logical_uri
        if tier.is_object:
            try:
                self._assert_region_attempt(
                    result_uri, logical_uri, expected_run_id=sub.run_id)
            except Exception:  # noqa: BLE001 — backend/metadata details stay in server logs
                logging.getLogger("hub").exception(
                    "placed backend returned an unattested region object attempt")
                raise RuntimeError("region object lifecycle attestation failed") from None
            return self._publish_region_attempt(
                cache_key=ckey, uri=result_uri, logical_uri=logical_uri,
                table=region.id, run_id=run_id, cancel=cancel,
                expected_attempt_run_id=sub.run_id)
        if not self._region_output_exists(result_uri):
            raise RuntimeError(
                f"region {region.id} on {region.backend} returned an unreadable handoff")
        self.base._cache_put(
            ckey, {"uri": result_uri, "table": region.id, "rows": None})
        return result_uri

    def _run_final(self, run_id: str, graph: Graph, region, ref_uri: dict[str, str], uid: str | None = None) -> RunStatus:
        """Run the final (target) region over the reduced graph, waiting for it — on the base runner
        (default) or on the region's target backend (a placed final region), so writes commit normally."""
        subg = self._subgraph(graph, region, ref_uri)
        plan = compiler.compile_plan(subg, region.output_node, self.deps.registry, self.deps.node_specs, self.deps.node_ir)
        backend = self._backend_runner(region, uid)
        sub = backend.run(plan, subg, region.output_node, "local")
        self._track_sub(run_id, backend, sub.run_id)
        try:
            return self._await(backend, sub.run_id, cancel_run=run_id)
        finally:
            self._untrack_sub(run_id, sub.run_id)

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
            self._signal_execution_stop(run_id)
        # _orchestrate publishes the terminal state only after all locally-owned sub-runs have acknowledged
        # cancellation. Until then the logical run remains non-terminal, so clients do not mistake a request
        # for completion while a region may still publish.
        return self.runs.get(run_id)
