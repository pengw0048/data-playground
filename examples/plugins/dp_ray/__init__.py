"""Reference plugin — a **Ray Data execution backend** that runs a canvas on Ray, straight from the
engine-neutral IR (`hub.ir`).

This is the proof that the IR is a real engine-neutral contract: a SECOND engine (Ray Data, not DuckDB)
executes the graph WITHOUT re-reading node configs or re-implementing lowering. It runs the **clean
subset** — `read → per-row/-batch map/filter/flat_map/map_batches → write` — where the transform
operator is the SAME `sandbox.compile_operator` the default engine runs, so results match byte-for-byte.
Anything relational (`sql`/`join`/`aggregate`/`sort`/`dedup`), reducing (`metric`/`chart`), or opaque
(`section`, plugin kinds) makes `can_run` return False → the kernel falls back to the DuckDB engine,
which runs it correctly. `run()` re-checks and delegates too, so a mis-route degrades safely.

Honest scope (a reference, not a tuned production backend): the map/filter/flat_map/map_batches stages
run **distributed on Ray Data** — that's the point being proven. Source reads go through the driver's
adapter (→ Arrow → `ray.data.from_arrow`) so EVERY source works (Lance/HF/Iceberg/plugin), and the sink
collects to the driver and writes via the adapter; cancellation is cooperative between IR steps (Ray has
no cheap mid-Dataset abort).

EXECUTION MODEL — an isolated subprocess driver. Running Ray inline in the kernel process deadlocks: the
source read / sink write go through a DuckDB base connection, and a materialization on the hub's
pre-existing connection wedges once `ray.init()` has run in the same process. So `run()` spawns a fresh
subprocess (`_driver.py`) whose OWN process holds its DuckDB + Ray (`ray.init` before any DuckDB). The hub
resolves logical destinations before dispatch and owns catalog registration after the driver returns;
the driver receives physical sink URIs and never reads that control-plane state. This is the
production-correct isolation shape (the same boundary the built-in SubprocessRunner uses).

The `uv` fix. If the kernel is launched via `uv run` (common), Ray's default behavior
(`RAY_ENABLE_UV_RUN_RUNTIME_ENV`) re-launches its WORKERS through `uv` too — which builds a fresh,
ray-less `.venv`, so workers can't `import ray`, the raylet dies, and the run hangs. The driver sets
`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (before `import ray`) so workers use its own interpreter (which has
ray); `_supervise` also strips uv/`VIRTUAL_ENV` markers and runs the child off the repo's pyproject.
With that, the live differential (`test_ray_backend_live_differential`) passes on macOS AND Linux — it's
opt-in only because it needs the `[ray]` extra + is slow. `DP_RAY_NUM_CPUS` optionally caps the worker
pool. The Part B mechanism (a plugin node's `ir` hook → clean op → routed here) is also covered
cluster-free by `test_plugin_node_ir_hook_runs_on_duckdb_and_ray`.

Opt-in: `uv pip install -e 'kernel[ray]'`, drop this folder in `<workspace>/plugins/`, and select it
via Settings → Execution or `DP_EXECUTION=ray-data`. It never becomes the default (the kernel is), so a
small graph won't spin up Ray unless you ask.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid

from hub import db, graph as g
from hub.sinks import SinkSpec, commit_sink, preflight_sink
from hub.sqlanalyze import agg_has_order_sensitive, window_needs_order  # AST (DuckDB's own parser), shared
from hub.ir import (CLEAN_TRANSFORM_MODES, lower_to_ir, parse_group_keys, parse_sort_keys,
                    plan_is_clean, plan_is_distributable)
from hub.models import PerNodeStatus, ResourceSpec, RunStatus, WorkerInfo

# the relational ops THIS backend claims beyond the map-style clean subset (ARC3). The engine does NOT
# reimplement these on Ray operators — it lets RAY do only the SHUFFLE (hash-partition rows by the op's
# key) and lets DUCKDB do the compute on each COMPLETE partition, running the SAME SQL the single-node
# engine runs. So the result is byte-identical BY CONSTRUCTION (it's DuckDB, on partitions holding every
# row of their key-groups — nothing combined across partitions), MOST DuckDB aggregates work, the output
# carries DuckDB's exact schema, and the only thing parsed is the shuffle KEY (bare columns). `aggregate`
# = a GROUPED aggregate; a global aggregate (no key) is cheap + falls back to the single-node engine. An
# ORDER-SENSITIVE aggregate (list/string_agg/first/last/any_value/arg_max/…) depends on intra-group row
# order, which the hash-shuffle does not preserve, so it falls back — detected by name via the shared
# AST analyzer (hub.sqlanalyze.agg_has_order_sensitive), conservatively including an ORDER-BY'd form like
# `list(x ORDER BY x)` (DuckDB rewrites the ORDER BY out of the parsed AST, so we can't prove it safe).
# `window` = a PARTITION BY window (shuffle by the
# partition key → DuckDB window per complete partition); requires a non-empty ORDER BY (a no-ORDER-BY
# window like row_number is intra-partition-order-dependent → falls back), and is exact up to ORDER BY
# ties (the same inherent tie-ceiling as sort — single-node is itself unstable there). `dedup` = full-row
# DISTINCT (shuffle by ALL columns → DuckDB DISTINCT; identical rows colocate). A keyed DISTINCT ON keeps
# the first row in an arbitrary order (non-deterministic even single-node) → needs an explicit order key,
# so it falls back; and a dedup whose schema has any FLOAT/DOUBLE column falls back too, because the
# shuffle's raw-byte equality distinguishes -0.0/0.0 and NaN payloads that DuckDB DISTINCT coalesces.
# `join` = a BROADCAST join: collect the RIGHT side to the driver + broadcast it, then DuckDB-join each
# LEFT block against the full right per worker (the SAME join_sql the single-node engine uses → identical
# output). inner/left/cross are correct block-by-block; right/full (need unmatched-right rows) fall back,
# and — like Spark's broadcast hint — the right is assumed small enough to broadcast (a large-large join
# should not pin engine=ray).
# `sort` = a native Ray range-shuffle sort on plain-column keys, then repartition(1) so the ordered output
# is a SINGLE file (matching the single-node engine's single ordered writer — a sharded read wouldn't
# preserve global order). FAITHFULNESS is exact only for a TOTAL (unique) key: the sequence then equals
# DuckDB's, incl. NULL placement (both NULLS LAST on DuckDB 1.5.x). For a NON-unique key, ties are
# unstable in BOTH engines → correctly sorted but tie-order may differ from single-node (not
# byte-identical); a float/double DESC with NaN also differs (Ray puts NaN last, DuckDB treats it as
# largest). SCALE: repartition(1) gathers the whole sorted set onto ONE worker — fine for this reference
# backend, but a sort exceeding one node's memory should not pin engine=ray (a production backend would
# write ordered shards + stitch on read).
RAY_RELATIONAL = frozenset({"aggregate", "window", "dedup", "join", "sort"})


def _ray_opts(requires: dict | None) -> dict:
    """Map the region's resolved resource need (the planner's `requires`) to per-Ray-task placement
    options, so a Ray cluster schedules the region's map tasks onto a worker that has the resource:
    `gpu` → num_gpus (each map task needs a GPU); a non-`engine` label `k=v` → a custom resource named
    `v` (fractional so many tasks share one node — declare it on the node via `ray start --resources`).
    cpu/mem are omitted: they're per-REGION aggregates, not the per-TASK cost Ray schedules on."""
    if not requires:
        return {}
    opts: dict = {}
    if requires.get("gpu"):
        opts["num_gpus"] = float(requires["gpu"])
    res = {str(v): 0.001 for k, v in (requires.get("labels") or {}).items() if k != "engine" and v}
    if res:
        opts["resources"] = res
    return opts


def _make_mapper(config: dict):
    """A Ray Data batch UDF that reuses the DuckDB engine's EXACT operator — so a transform produces the
    same rows on Ray as locally. Captures only plain strings, so it cloudpickles to Ray workers."""
    code, mode, on_error = config.get("code"), config["mode"], config.get("onError", "raise")
    fmt = config.get("batchFormat", "rows") if mode == "map_batches" else "rows"

    def _op(table):  # a pyarrow.Table block
        import pyarrow as pa

        from hub import sandbox
        from hub.executors.engine import _apply_batch, _apply_fn

        fn = sandbox.compile_operator(code, mode)
        if fmt in ("pandas", "arrow"):  # whole-batch pandas/arrow UDF — SAME arrow-native path as local
            res = _apply_batch(fn, table, fmt, on_error, None)
            return res if res is not None else table.slice(0, 0)  # skip → empty block (Ray needs a table)
        rows: list[dict] = []
        for batch in table.to_batches():
            rows.extend(_apply_fn(fn, batch, mode, on_error, None))
        return pa.Table.from_pylist(rows) if rows else table.slice(0, 0)  # keep schema when a batch empties

    return _op


class RayRunner:
    name = "ray-data"

    def __init__(self, deps):
        self.deps = deps
        self.base = deps.runner            # the local out-of-core runner — estimate, fallback, lineage reuse
        self.resolve_adapter = deps.resolve_adapter
        self.catalog = deps.catalog
        self.node_specs = deps.node_specs
        # mirror the hub-wired status/history hooks so Ray runs are just as visible cross-instance
        self.on_status = getattr(self.base, "on_status", None)
        self.on_complete = getattr(self.base, "on_complete", None)
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # gate on the clean subset from the CompilePlan alone (what the kernel hands can_run) — else the
    # DuckDB LocalRunner handles it. Conservative: unsure ⇒ fall back (always correct).
    def can_run(self, plan) -> bool:
        return plan_is_clean(plan) or plan_is_distributable(plan, RAY_RELATIONAL)

    def estimate(self, plan, rows, byts=None):
        return self.base.estimate(plan, rows, byts)  # reuse the hub-side confirm gate verbatim

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel(self, run_id: str) -> RunStatus:
        st = self.runs.get(run_id)
        if st and st.status in ("queued", "running"):
            ev = self._cancel.get(run_id)
            if ev:
                ev.set()  # cooperative — checked between IR steps (Ray has no cheap mid-Dataset abort)
            st.status = "cancelled"
        return st

    # -- PlaceableBackend (region dispatch, Phase C3) ---------------------- #
    # dp_ray advertises ONE synthetic worker labelled engine=ray. place() claims a region ONLY when it
    # explicitly asks for engine=ray (config.requires.labels) — so the cost-based mem policy never
    # silently routes here; a user opts a node into Ray deliberately. reachable_tiers: local Ray shares
    # the fs and can read object storage, so both (a real remote cluster would declare object-only).
    def workers(self) -> list:
        # The hub can't query a live Ray cluster (the driver runs in an isolated subprocess — see the
        # DuckDB×Ray deadlock note), so an operator declares the cluster's shape via env: DP_RAY_GPUS /
        # DP_RAY_GPU_TYPE / DP_RAY_MEM. That advertised capacity feeds the topology view + the run-plan
        # pre-flight ("needs 4×a100 — backends advertise: 8×a100"). Defaults keep the engine=ray label.
        import os
        try:
            gpu = int(os.environ.get("DP_RAY_GPUS", "0") or 0)
        except ValueError:
            gpu = 0  # a mistyped count shouldn't silently drop the whole capacity report
        cap = ResourceSpec(mem=os.environ.get("DP_RAY_MEM", "1000GB"),
                           gpu=gpu or None, gpu_type=(os.environ.get("DP_RAY_GPU_TYPE") or None) if gpu else None,
                           labels={"engine": "ray"})
        return [WorkerInfo(id="ray", capacity=cap, state="idle")]

    def place(self, requires) -> "str | None":
        labels = getattr(requires, "labels", None) or {}
        return "ray" if labels.get("engine") == "ray" else None

    def reachable_tiers(self):
        # A same-host reference cluster (worker-direct LOCAL reads) reaches local + object. But an
        # OFF-HOST cluster's workers can't read the hub's local disk — declaring local there would let the
        # controller route a region handoff to local and silently produce a result the remote workers
        # can't read. So when the operator marks the cluster remote (DP_RAY_REMOTE), reach is object-only,
        # and the controller correctly refuses a handoff with no shared object store.
        remote = os.environ.get("DP_RAY_REMOTE", "").strip().lower() in ("1", "true", "yes", "on")
        return ("object",) if remote else ("local", "object")

    def run_unit(self, graph, output_node, output_uri, requires=None, run_id=None) -> RunStatus:
        """Run ONE region's subgraph on Ray and materialize output_node → output_uri (the RunController
        handoff contract). A clean region runs distributed on Ray: reads AND writes worker-direct (each
        block written as its own parquet shard, no driver funnel — output_uri becomes a DIRECTORY of
        shards). `requires` (the planner's resolved region need) is passed to Ray so its map tasks are
        scheduled onto a matching worker. Anything non-clean falls back to the base subprocess-materialize."""
        ir = lower_to_ir(graph, output_node, self.node_specs, self.deps.node_ir)
        if not self._ray_runnable(ir) or self._dedup_needs_single_node(graph, ir):
            return self._materialize_local(graph, output_node, output_uri, run_id)  # non-clean → local engine
        run_id = run_id or f"unit_{uuid.uuid4().hex[:10]}"
        status = RunStatus(run_id=run_id, status="queued", placement="distributed", target_node_id=output_node,
                           per_node=[PerNodeStatus(node_id=output_node, status="queued", label=output_node)])
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
        req = requires.model_dump() if hasattr(requires, "model_dump") else requires
        threading.Thread(target=self._supervise, args=(run_id, graph, output_node, status),
                         kwargs={"materialize_uri": output_uri, "requires": req}, daemon=True).start()
        return status

    def _materialize_local(self, graph, output_node, output_uri, run_id=None) -> RunStatus:
        """Non-clean region fallback: materialize it with the LOCAL DuckDB engine (correct, just not
        distributed). Synchronous — returns a terminal status the RunController polls. Uses DuckDB only
        (no Ray in this process), so no init-order deadlock. self.base (LocalRunner) has no run_unit."""
        from hub.executors.engine import BuildEngine
        from hub.plugins.adapters import is_object_uri
        run_id = run_id or f"unit_{uuid.uuid4().hex[:10]}"
        status = RunStatus(run_id=run_id, status="running", placement="local", target_node_id=output_node,
                           per_node=[PerNodeStatus(node_id=output_node, status="running", label=output_node)])
        with self._lock:
            self.runs[run_id] = status
        try:
            with db.run_scope():
                if is_object_uri(output_uri):
                    db.ensure_object_store()
                else:
                    os.makedirs(os.path.dirname(output_uri) or ".", exist_ok=True)
                eng = BuildEngine(graph, self.resolve_adapter, self.deps.registry, full=True,
                                  node_builders=self.deps.node_builders, node_specs=self.node_specs,
                                  output_node=output_node)
                eng.relation(output_node).write_parquet(output_uri)
            status.status, status.output_uri = "done", output_uri
        except Exception as e:  # noqa: BLE001
            status.status, status.error = "failed", f"{type(e).__name__}: {e}"
        for p in status.per_node:
            p.status = status.status
        return status

    def _ray_runnable(self, ir) -> bool:
        # (1) every step is clean OR a claimed relational op; (2) every clean transform carries inlined
        # code (a Ray worker has no access to the driver's processor registry); (3) every aggregate has a
        # GROUPED, bare-column key we can hash-shuffle on (a global aggregate — empty keys — or an
        # expression key has no shuffle key → DuckDB single-node, which is cheap for a global reduce).
        if not ir.is_distributable(RAY_RELATIONAL):
            return False
        if not all(s.config.get("code") for s in ir.steps if s.op in CLEAN_TRANSFORM_MODES):
            return False
        for s in ir.steps:
            if s.op == "write":
                try:
                    SinkSpec.from_config(s.config, s.config.get("title"))
                except (TypeError, ValueError):
                    return False  # invalid sink semantics must not be silently ignored by Ray
            if s.op == "aggregate":
                if not parse_group_keys(s.config.get("groupBy", "")):
                    return False  # None (expression key) or [] (global) → not shuffle-distributable
                # pass the EFFECTIVE aggs (default matches _build_aggregate) so a node with no aggs isn't
                # spuriously rejected by the empty-fragment conservative default.
                if agg_has_order_sensitive(s.config.get("aggs") or "count(*) AS n"):
                    return False  # list/string_agg/first/any_value/arg_max/mode/argmax → intra-group order
                                  # matters, the hash-shuffle doesn't preserve it → single-node (conservative:
                                  # even an ORDER-BY'd list, whose ORDER BY DuckDB rewrites out of the AST)
            if s.op == "window":
                if not parse_group_keys(s.config.get("partitionBy", "")):
                    return False  # a window needs a bare-column PARTITION BY to be the shuffle key
                expr = s.config.get("expr", "")
                if agg_has_order_sensitive(expr):
                    return False  # list/string_agg/array_agg/arg_max/mode OVER (PARTITION BY k) is
                                  # intra-partition-ORDER-dependent even though its window AST type is
                                  # WINDOW_AGGREGATE → the shuffle scrambles the partition → single-node.
                if window_needs_order(expr) and not (s.config.get("orderBy") or "").strip():
                    return False  # a RANKING/OFFSET window (row_number/rank/lag/first_value/…) is
                                  # intra-partition-order-dependent → needs a total ORDER BY (exact up to
                                  # ties — the sort tie-ceiling); a pure-AGGREGATE window (sum/count OVER
                                  # (PARTITION BY k)) is whole-partition → byte-identical with no ORDER BY.
            if s.op == "dedup" and (s.config.get("on") or "").strip():
                return False  # only full-row DISTINCT distributes; keyed DISTINCT ON is order-dependent
            if s.op == "join":
                from hub.executors.engine import normalize_how
                if normalize_how(s.config.get("how", "")) not in ("inner", "left", "cross"):
                    return False  # right/full must emit unmatched-RIGHT rows → broadcast-right can't → DuckDB
            if s.op == "sort" and parse_sort_keys(s.config.get("by", "")) is None:
                return False  # only a bare-column ORDER BY is distributable; expressions → DuckDB
        return True

    def _resolve_sink_targets(self, ir) -> dict[str, str]:
        """Resolve and validate logical sinks on the hub, before the isolated driver is dispatched."""
        targets: dict[str, str] = {}
        for step in ir.steps:
            if step.op == "write":
                spec = SinkSpec.from_config(step.config, step.config.get("title"))
                targets[step.id] = preflight_sink(
                    spec, self.deps.workspace, self.base.storage, self.resolve_adapter
                )
        return targets

    def _sink_targets_runnable(self, ir) -> bool:
        try:
            self._resolve_sink_targets(ir)
            return True
        except Exception:  # noqa: BLE001 — unknown destination/incompatible adapter ⇒ safe local fallback
            return False

    def _dedup_needs_single_node(self, graph, ir) -> bool:
        """Full-row dedup shuffles by ALL columns so identical rows colocate, then DuckDB DISTINCT per
        partition. But Ray's hash-shuffle equality is RAW-BYTE, which distinguishes values DuckDB DISTINCT
        coalesces: -0.0 vs 0.0, and distinct NaN bit-patterns. So two rows differing only in signed-zero /
        NaN-payload hash to DIFFERENT partitions and BOTH survive → one extra row vs single-node DuckDB.
        Fall back to the single-node engine whenever a dedup's schema carries any floating-point column,
        SCALAR OR NESTED. Inspects the RAW DuckDB column types (`rel.types`) — NOT the display type, which
        normalizes STRUCT/MAP/LIST to a bare `struct`/`map`/`list` and would hide a nested double (and maps
        DECIMAL→float, wrongly forcing an exact-decimal dedup local). Needs the schema (the IR carries
        none), so it's separate from the config-only _ray_runnable gate; only paid when a dedup is present."""
        dedups = [s for s in ir.steps if s.op == "dedup"]
        if not dedups:
            return False
        from hub.executors.engine import BuildEngine
        # a raw DuckDB type string carries nested element types (STRUCT(a DOUBLE), DOUBLE[], MAP(…, DOUBLE))
        # so this catches nested floats; DECIMAL(…) / HUGEINT don't match, so exact types still distribute.
        float_re = re.compile(r"\b(?:float|double|real)\b", re.I)
        for s in dedups:
            try:
                with db.run_scope():
                    rel = BuildEngine(graph, self.resolve_adapter, self.deps.registry, full=True,
                                      node_builders=self.deps.node_builders, node_specs=self.node_specs,
                                      output_node=s.id).relation(s.id)
                    types = [str(t) for t in rel.types]  # RAW DuckDB types (schema-only; no data scan)
            except Exception:  # noqa: BLE001 — can't prove the schema is float-free → don't distribute (safe)
                return True
            if any(float_re.search(t) for t in types):
                return True
        return False

    def run(self, plan, graph, target_node_id, placement, run_id=None) -> RunStatus:
        ir = lower_to_ir(graph, target_node_id, self.node_specs, self.deps.node_ir)
        if not self._ray_runnable(ir) or self._dedup_needs_single_node(graph, ir):
            return self.base.run(plan, graph, target_node_id, placement, run_id=run_id)  # safe fallback
        try:
            sink_targets = self._resolve_sink_targets(ir)
        except Exception:  # noqa: BLE001 — resolve/adapter uncertainty ⇒ local, never a partial Ray commit
            return self.base.run(plan, graph, target_node_id, placement, run_id=run_id)
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
        per_node = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        status = RunStatus(run_id=run_id, status="queued", placement="distributed", per_node=per_node,
                           target_node_id=target_node_id)
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
        self._emit(graph, status)
        # PROCESS ISOLATION: run Ray in a fresh subprocess (its main thread inits Ray BEFORE any DuckDB),
        # so the app's shared DuckDB connection never coexists with Ray in one process. The parent only
        # spawns + polls a status file (no DuckDB here), so it can't deadlock. (Ray inline in-process
        # deadlocks against the shared DuckDB connection — see the module docstring.)
        threading.Thread(target=self._supervise, args=(run_id, graph, target_node_id, status),
                         kwargs={"sink_targets": sink_targets},
                         daemon=True).start()
        return status

    def _emit(self, graph, status) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001 — never let persistence break a run
                pass

    def _register_outputs(self, graph, result) -> None:
        """Publish driver-written outputs through the hub-owned catalog/control plane."""
        for output in result.get("outputs") or []:
            step_id, name, uri = output.get("step_id"), output.get("name"), output.get("uri")
            if not (step_id and name and uri):
                raise RuntimeError("ray driver returned an incomplete sink result")
            parents = [u for edge in g.incoming(graph, step_id)
                       for u in [self.base._source_uri(nm_node=edge.source, graph=graph)] if u]
            self.catalog.register_output(name=name, uri=uri, version=None,
                                         parents=parents, pipeline="canvas")

    def _supervise(self, run_id, graph, target, status, materialize_uri=None, requires=None,
                   sink_targets=None) -> None:
        """Parent side: spawn the isolated Ray driver, poll its status file, mirror the result. Touches
        NO DuckDB (only subprocess + files + the DB-backed on_status/on_complete hooks) → never deadlocks.
        `materialize_uri` set = region mode (write target → that uri); else whole-graph mode (write node).
        `requires` = the region's resource need, forwarded to the driver → per-task Ray placement.
        `sink_targets` is the hub-resolved write-step-id → physical URI map; region mode omits it."""
        import json
        import subprocess
        import tempfile

        cancel = self._cancel[run_id]
        status.status = "running"
        self._emit(graph, status)
        work = tempfile.mkdtemp(prefix="dp_ray_")
        job_file, status_file = os.path.join(work, "job.json"), os.path.join(work, "status.json")
        job = {"workspace": self.deps.workspace, "data_dir": self.deps.data_dir, "target": target,
               "graph": graph.model_dump(), "module": os.path.abspath(__file__), "requires": requires,
               "materialize_uri": materialize_uri, "status_file": status_file}
        if sink_targets is not None:  # whole-graph run only; region materialization has no write sink
            job["sink_targets"] = sink_targets
        with open(job_file, "w") as f:
            json.dump(job, f)
        driver = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_driver.py")
        result = None
        try:
            # Redirect the child's stdio to a log file (never an inherited pipe — Ray logs copiously and
            # a full pipe would block the child mid-run; the result comes back via status_file). Own
            # session so Ray's worker signals/pgroup are decoupled from the (daemon-thread) parent.
            _dlog = open(os.path.join(work, "driver.log"), "w")
            # CRITICAL for a kernel launched via `uv run`: Ray detects the uv context and re-launches its
            # WORKERS through uv, which (with the repo pyproject + a VIRTUAL_ENV mismatch) builds a fresh
            # ray-less .venv → workers can't `import ray` → the raylet dies and the run hangs. Strip the
            # uv/venv markers and put the venv's bin on PATH so Ray runs workers with THIS interpreter
            # (which has ray); run from the work dir so uv/Ray don't pick up the repo's pyproject.
            venv_bin = os.path.dirname(sys.executable)
            child_env = {k: v for k, v in os.environ.items()
                         if k not in ("VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX")
                         and not k.startswith("UV_")}
            child_env["PATH"] = venv_bin + os.pathsep + child_env.get("PATH", "")
            child_env["RAY_DATA_DISABLE_PROGRESS_BARS"] = "1"
            child_env["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"  # workers use this interpreter, not a fresh uv venv
            proc = subprocess.Popen([sys.executable, driver, job_file], cwd=work,
                                    stdout=_dlog, stderr=_dlog, start_new_session=True, env=child_env)
            while proc.poll() is None:
                if cancel.is_set():
                    proc.terminate()
                    status.status = "cancelled"
                    break
                # surface the driver's INTERIM progress (it rewrites the status file as it computes/writes)
                # into the parent RunStatus, so a placed region's progress advances mid-run — not just at
                # the region boundary. A partial read (mid-write) raises → skipped until the next tick.
                try:
                    if os.path.exists(status_file):
                        with open(status_file) as f:
                            interim = json.load(f)
                        if interim.get("status") == "running" and interim.get("progress") is not None:
                            status.progress = float(interim["progress"])
                            if interim.get("rows"):
                                status.rows_processed = int(interim["rows"])
                            self._emit(graph, status)
                except (ValueError, OSError):
                    pass
                time.sleep(0.2)
            if os.path.exists(status_file):
                with open(status_file) as f:
                    result = json.load(f)
        except Exception as e:  # noqa: BLE001
            status.status, status.error = "failed", f"{type(e).__name__}: {e}"
        if status.status not in ("cancelled",):
            # only a TERMINAL status file is authoritative — the driver rewrites this same file with an
            # interim {"status":"running",...} as it progresses, and a hard kill (OOM/SIGKILL/segfault)
            # bypasses its finally, leaving that interim behind. Treating "running" as the result would
            # peg the sub-run at running forever and hang RunController._await. A dead driver → fail.
            if result and result.get("status") in ("done", "failed", "cancelled"):
                if result.get("outputs"):
                    try:
                        self._register_outputs(graph, result)
                    except Exception as e:  # noqa: BLE001 — local parity: catalog commit failure fails the run
                        prior = f"{result.get('error')}; " if result.get("error") else ""
                        result = dict(
                            result, status="failed",
                            error=f"{prior}catalog registration failed: {type(e).__name__}: {e}",
                        )
                status.status = result["status"]
                status.error = result.get("error")
                status.output_uri, status.output_table = result.get("output_uri"), result.get("output_table")
                status.rows_processed = status.total_rows = int(result.get("rows") or 0)
                if status.status == "done":
                    status.progress = 1.0
            elif status.status == "running":
                status.status, status.error = "failed", f"ray driver exited without a terminal status (rc={proc.returncode})"
        for p in status.per_node:  # settle per-node progress to the terminal state
            p.status = "done" if status.status == "done" else status.status
        self._emit(graph, status)
        with self._lock:
            self._cancel.pop(run_id, None)
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001
                pass

    def _run_ir_sync(self, ir, graph, target, ray_opts=None, progress=None, sink_targets=None) -> dict:
        """Child side (in the driver subprocess, Ray already init'd): execute the clean IR synchronously
        and return a result dict for the parent. Reuses _build/_commit; the fresh-process DuckDB is safe
        because Ray was init'd before it was created. Sink targets are physical URIs resolved by the hub;
        the isolated driver never reads destination settings."""
        outputs: list[dict[str, str]] = []
        try:
            datasets: dict[str, object] = {}
            rows, out_uri, out_table = 0, None, None
            for step in ir.steps:
                if step.op == "write":
                    target_uri = (sink_targets or {}).get(step.id)
                    if not target_uri:
                        raise RuntimeError(f"missing hub-resolved target URI for write step '{step.id}'")
                    rows, out_uri, out_table = self._commit(step, datasets, target_uri)
                    outputs.append({"step_id": step.id, "name": out_table, "uri": out_uri})
                else:
                    datasets[step.id] = self._build(step, datasets, ray_opts)
            if target and target in datasets:  # a non-sink target → force a real row count
                rows = datasets[target].count()
            return {"status": "done", "rows": rows, "output_uri": out_uri,
                    "output_table": out_table, "outputs": outputs}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(e).__name__}: {e}",
                    "rows": 0, "outputs": outputs}

    def _run_ir_materialize(self, ir, graph, target, uri, ray_opts=None, progress=None) -> dict:
        """Child side, region mode: run the clean IR up to `target` on Ray and materialize it to `uri`.
        WORKER-DIRECT WRITE: `uri` becomes a DIRECTORY of parquet shards, each written in parallel by a
        Ray task — nothing funnels through the driver (the old collect→concat→single-file OOM'd on a big
        region). The RunController's ref-read / _output_exists / _move_tier all accept a parts-dir. Reports
        interim `progress` so the parent's placed-region progress advances mid-run."""
        from hub.plugins.adapters import is_object_uri, object_fs
        try:
            if progress:
                progress(0.05)
            datasets: dict[str, object] = {}
            for step in ir.steps:
                if step.op == "write":  # a region is cut BEFORE any write; ignore a stray one
                    continue
                datasets[step.id] = self._build(step, datasets, ray_opts)
            ds = datasets[target].materialize()  # execute the read→map pipeline ONCE, in the cluster (spillable)
            rows = ds.count()
            if progress:
                progress(0.6, rows)
            out_dir = uri[:-len(".parquet")] if uri.lower().endswith(".parquet") else uri  # a DIR of shards
            # OVERWRITE (not Ray's default append): the dir name is content-addressed + stable, so a
            # recompute after a cancelled/failed partial write (or a cache-pointer eviction) must REPLACE
            # any leftover shards — appending beside them would double the downstream rows.
            from ray.data import SaveMode
            if is_object_uri(out_dir):
                fs, p = object_fs(out_dir)               # creds-aware object filesystem (same as the adapter)
                ds.write_parquet(p, filesystem=fs, mode=SaveMode.OVERWRITE)  # each block → its own object, by a worker
            else:
                os.makedirs(out_dir, exist_ok=True)
                ds.write_parquet(out_dir, mode=SaveMode.OVERWRITE)  # WORKER-DIRECT: parallel shard write, no funnel
            return {"status": "done", "rows": rows, "output_uri": out_dir, "output_table": None}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}

    def _build(self, step, datasets, ray_opts=None):
        import ray
        opts = ray_opts or {}

        if step.op == "read":
            import glob as _glob

            uri = step.config["uri"]
            from hub.plugins.adapters import is_object_uri
            # WORKER-DIRECT read for a local parquet FILE or a parts-DIRECTORY (an upstream region's
            # worker-direct handoff has the .parquet suffix stripped) — Ray reads both natively, so a
            # chained region doesn't re-funnel the whole upstream output through this driver.
            is_parts_dir = os.path.isdir(uri) and _glob.glob(os.path.join(uri, "**", "*.parquet"), recursive=True)
            if not is_object_uri(uri) and (uri.lower().endswith((".parquet", ".pq")) or is_parts_dir):
                try:
                    return ray.data.read_parquet(uri)  # Ray reads on workers, no driver funnel
                except Exception:  # noqa: BLE001 — fall back to the always-works driver-Arrow path below
                    pass
            with db.base_guard():                              # any other source (Lance/HF/Iceberg/CSV/object/plugin)
                tbl = self.resolve_adapter(uri).scan(uri).to_arrow_table()
            return ray.data.from_arrow(tbl)
        parent = datasets[step.inputs[0][0]]                   # clean transforms/passthrough are single-input
        if step.op == "passthrough":
            return parent
        if step.op in CLEAN_TRANSFORM_MODES:
            # `opts` (num_gpus / custom resources from the region's requires) makes Ray schedule each map
            # task onto a worker that has the resource — the planner's placement, honored on the cluster.
            return parent.map_batches(_make_mapper(step.config), batch_format="pyarrow", **opts)
        if step.op == "aggregate":
            return self._build_aggregate(step, parent)
        if step.op == "window":
            return self._build_window(step, parent)
        if step.op == "dedup":
            # full-row DISTINCT: shuffle by ALL columns so identical rows colocate in one partition, then
            # DuckDB DISTINCT per partition. Every surviving row is identical to the dups it replaces, so
            # the result is deterministic + byte-identical (unlike keyed DISTINCT ON — gated out above).
            return self._shuffle_duckdb(parent, list(parent.columns()), "SELECT DISTINCT * FROM _blk")
        if step.op == "join":
            return self._build_join(step, datasets)
        if step.op == "sort":
            keys = parse_sort_keys(step.config.get("by", ""))
            cols = [c for c, _d in keys]
            desc = [d for _c, d in keys]
            # Ray's native sort IS the distributed range-shuffle; repartition(1) then coalesces the ordered
            # range-partitions into ONE block → a single ordered output file (a sharded write's parts read
            # back in arbitrary order would lose the global order). Matches the single-node engine, which
            # also writes one ordered file. `descending` is per-key.
            return parent.sort(cols, descending=desc).repartition(1)
        raise RuntimeError(f"ray backend reached a non-clean op '{step.op}' (should have fallen back)")

    def _build_join(self, step, datasets):
        """Distributed BROADCAST join. Collect the RIGHT (small/dimension) side to the driver and broadcast
        it into the map closure, then DuckDB-joins each LEFT block against the FULL right on its worker,
        using the SHARED join_sql — so semantics, output schema, and the `_2`-suffix / USING-coalesce
        naming are byte-identical to the single-node engine. Each left row joins independently against the
        complete right, so inner/left/cross are correct block-by-block (right/full are gated out)."""
        import pyarrow as pa
        import ray

        from hub.executors.engine import join_sql
        left = datasets[step.inputs[0][0]]                     # incoming-edge order = engine's left, right
        right = datasets[step.inputs[1][0]]
        refs = ray.get(right.to_arrow_refs())                  # broadcast side: driver → workers
        if refs:
            right_tbl = pa.concat_tables(refs)
        else:                                                  # right produced ZERO blocks — keep its TYPED
            sch = right.schema()                               # empty schema so a LEFT join emits correctly-
            right_tbl = getattr(sch, "base_schema", sch).empty_table()  # typed NULLs (not a null-typed crash)
        cfg = step.config
        sql = join_sql(list(left.columns()), list(right_tbl.column_names), "_l", "_r",
                       cfg.get("on"), cfg.get("condition"), cfg.get("how"))

        def _join_block(tbl):                                  # each LEFT block ⋈ the full broadcast right
            import duckdb
            con = duckdb.connect()
            con.register("_l", tbl)
            con.register("_r", right_tbl)
            return con.execute(sql).fetch_arrow_table()

        return left.map_batches(_join_block, batch_format="pyarrow", batch_size=None)

    def _shuffle_duckdb(self, parent, keys, sql):
        """The shared distributed-relational mechanism: RAY hash-shuffles `parent` by `keys` so every row
        of a key-group lands in ONE partition (its default HASH_SHUFFLE), then DUCKDB runs `sql` (reading
        the partition as `_blk`) on each WHOLE partition (batch_size=None → the batch IS the partition, so
        groups are never split). Because each group is complete in its partition, the union of the
        per-partition results equals the single-node DuckDB result BYTE-FOR-BYTE — it IS DuckDB, running
        the same SQL the single-node engine runs, with DuckDB's exact schema. This one mechanism backs
        aggregate/window (and extends to join/dedup) — no operator is reimplemented on Ray."""
        def _run(tbl):                                          # runs on a WORKER, one complete-groups partition
            import duckdb
            con = duckdb.connect()
            con.register("_blk", tbl)
            return con.execute(sql).fetch_arrow_table()

        try:
            parts = int(os.environ.get("DP_RAY_SHUFFLE_PARTITIONS", "0")) or None
        except ValueError:
            parts = None
        shuffled = parent.repartition(parts, keys=keys) if parts else parent.repartition(keys=keys)
        return shuffled.map_batches(_run, batch_format="pyarrow", batch_size=None)

    def _build_aggregate(self, step, parent):
        """Distributed GROUP BY: hash-shuffle by the group key, DuckDB `GROUP BY` per complete partition
        (see _shuffle_duckdb). Any DuckDB aggregate works; only the shuffle key is parsed."""
        cfg = step.config
        keys = parse_group_keys(cfg.get("groupBy", "")) or []   # gating guarantees a non-empty bare-col key
        group = (cfg.get("groupBy") or "").strip()
        aggs = (cfg.get("aggs") or "count(*) AS n").strip()     # DuckDB default (mirrors engine.py:649)
        return self._shuffle_duckdb(parent, keys, f"SELECT {group}, {aggs} FROM _blk GROUP BY {group}")

    def _build_window(self, step, parent):
        """Distributed window: hash-shuffle by PARTITION BY so each window-partition is complete in one Ray
        partition, then DuckDB runs the SAME `expr OVER (…)` per partition — exact, because the window's
        own ORDER BY (applied by DuckDB on the complete group) sets rank/lag, not the shuffle order.
        Mirrors engine.py's window SQL. Gating guarantees a bare-column PARTITION BY as the shuffle key."""
        cfg = step.config
        keys = parse_group_keys(cfg.get("partitionBy", "")) or []
        part = (cfg.get("partitionBy") or "").strip()
        order = (cfg.get("orderBy") or "").strip()
        expr = (cfg.get("expr") or "").strip()
        col = ((cfg.get("as") or "").strip() or "window").replace('"', '""')
        over = " ".join(x for x in [f"PARTITION BY {part}" if part else "",
                                    f"ORDER BY {order}" if order else ""] if x)
        return self._shuffle_duckdb(parent, keys, f'SELECT *, {expr} OVER ({over}) AS "{col}" FROM _blk')

    def _commit(self, step, datasets, target_uri: str) -> tuple[int, str, str]:
        cfg = step.config
        spec = SinkSpec.from_config(cfg, cfg.get("title"))
        ds = datasets[step.inputs[0][0]]
        tbl = _collect_arrow(ds)                                # collect blocks to the driver, typed when empty
        with db.base_guard():
            rel = db.conn().from_arrow(tbl)
            committed = commit_sink(spec, rel, self.deps.workspace, self.base.storage,
                                    self.resolve_adapter, target_uri=target_uri)
        return committed.rows, committed.uri, committed.name


def _collect_arrow(dataset):
    """Collect a Ray Dataset without erasing the schema when it contains zero output blocks."""
    import pyarrow as pa

    batches = list(dataset.iter_batches(batch_format="pyarrow"))
    if batches:
        return pa.concat_tables(batches)
    schema = dataset.schema()
    arrow_schema = getattr(schema, "base_schema", schema)
    if not isinstance(arrow_schema, pa.Schema):
        raise RuntimeError("an empty Ray result did not expose an Arrow schema")
    return pa.Table.from_batches([], schema=arrow_schema)


def register(reg) -> None:
    # opt-in: added as an available backend, selected only when execution == 'ray-data' (never the default)
    reg.add_runner(RayRunner(reg.deps))
