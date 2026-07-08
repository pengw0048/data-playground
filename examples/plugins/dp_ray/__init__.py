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
source read / sink write / `register_output` go through the app's SHARED DuckDB base connection, and a
materialization on that pre-existing connection wedges once `ray.init()` has run in the same process. So
`run()` spawns a fresh subprocess (`_driver.py`) whose OWN process holds its DuckDB + Ray (`ray.init`
before any DuckDB), and the parent only polls a status file — no DuckDB in the parent, no in-process
coexistence. This is the production-correct shape (same isolation the built-in SubprocessRunner uses).

The `uv` fix. If the kernel is launched via `uv run` (common), Ray's default behavior
(`RAY_ENABLE_UV_RUN_RUNTIME_ENV`) re-launches its WORKERS through `uv` too — which builds a fresh,
ray-less `.venv`, so workers can't `import ray`, the raylet dies, and the run hangs. The driver sets
`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (before `import ray`) so workers use its own interpreter (which has
ray); `_supervise` also strips uv/`VIRTUAL_ENV` markers and runs the child off the repo's pyproject.
With that, the live differential (`test_ray_backend_live_differential`) passes on macOS AND Linux — it's
opt-in only because it needs the `[ray]` extra + is slow. `DP_RAY_NUM_CPUS` optionally caps the worker
pool. The Part B mechanism (a plugin node's `ir` hook → clean op → routed here) is also covered
cluster-free by `test_plugin_node_ir_hook_runs_on_duckdb_and_ray`.

Opt-in: `pip install 'data-playground[ray]'`, drop this folder in `<workspace>/plugins/`, and select it
via Settings → Execution or `DP_EXECUTION=ray-data`. It never becomes the default (the kernel is), so a
small graph won't spin up Ray unless you ask.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

from hub import db, graph as g
from hub.ir import CLEAN_TRANSFORM_MODES, lower_to_ir, plan_is_clean
from hub.models import PerNodeStatus, ResourceSpec, RunStatus, WorkerInfo

_KNOWN_EXT = (".parquet", ".pq", ".csv", ".tsv", ".arrow", ".feather", ".ipc", ".json", ".lance")


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
        return plan_is_clean(plan)

    def estimate(self, plan, rows):
        return self.base.estimate(plan, rows)  # reuse the hub-side confirm gate verbatim

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
        return [WorkerInfo(id="ray", capacity=ResourceSpec(mem="1000GB", labels={"engine": "ray"}), state="idle")]

    def place(self, requires) -> "str | None":
        labels = getattr(requires, "labels", None) or {}
        return "ray" if labels.get("engine") == "ray" else None

    def reachable_tiers(self):
        return ("local", "object")

    def run_unit(self, graph, output_node, output_uri, run_id=None) -> RunStatus:
        """Run ONE region's subgraph on Ray and materialize output_node → output_uri (the RunController
        handoff contract). A clean region runs distributed on Ray (reads worker-direct); anything else
        falls back to the base runner's run_unit (subprocess-materialize, always correct)."""
        ir = lower_to_ir(graph, output_node, self.node_specs, self.deps.node_ir)
        if not self._ray_runnable(ir):
            return self._materialize_local(graph, output_node, output_uri, run_id)  # non-clean → local engine
        run_id = run_id or f"unit_{uuid.uuid4().hex[:10]}"
        status = RunStatus(run_id=run_id, status="queued", placement="distributed", target_node_id=output_node,
                           per_node=[PerNodeStatus(node_id=output_node, status="queued", label=output_node)])
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
        threading.Thread(target=self._supervise, args=(run_id, graph, output_node, status),
                         kwargs={"materialize_uri": output_uri}, daemon=True).start()
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
        # every clean transform must carry inlined code (a Ray worker has no access to the driver's
        # processor registry), else defer to the DuckDB engine.
        return ir.is_clean() and all(
            s.config.get("code") for s in ir.steps if s.op in CLEAN_TRANSFORM_MODES)

    def run(self, plan, graph, target_node_id, placement, run_id=None) -> RunStatus:
        ir = lower_to_ir(graph, target_node_id, self.node_specs, self.deps.node_ir)
        if not self._ray_runnable(ir):
            return self.base.run(plan, graph, target_node_id, placement, run_id=run_id)  # safe fallback
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
                         daemon=True).start()
        return status

    def _emit(self, graph, status) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001 — never let persistence break a run
                pass

    def _supervise(self, run_id, graph, target, status, materialize_uri=None) -> None:
        """Parent side: spawn the isolated Ray driver, poll its status file, mirror the result. Touches
        NO DuckDB (only subprocess + files + the DB-backed on_status/on_complete hooks) → never deadlocks.
        `materialize_uri` set = region mode (write target → that uri); else whole-graph mode (write node)."""
        import json
        import subprocess
        import tempfile

        cancel = self._cancel[run_id]
        status.status = "running"
        self._emit(graph, status)
        work = tempfile.mkdtemp(prefix="dp_ray_")
        job_file, status_file = os.path.join(work, "job.json"), os.path.join(work, "status.json")
        with open(job_file, "w") as f:
            json.dump({"workspace": self.deps.workspace, "data_dir": self.deps.data_dir, "target": target,
                       "graph": graph.model_dump(), "module": os.path.abspath(__file__),
                       "materialize_uri": materialize_uri, "status_file": status_file}, f)
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
                time.sleep(0.2)
            if os.path.exists(status_file):
                with open(status_file) as f:
                    result = json.load(f)
        except Exception as e:  # noqa: BLE001
            status.status, status.error = "failed", f"{type(e).__name__}: {e}"
        if status.status not in ("cancelled",):
            if result:
                status.status = result.get("status", "failed")
                status.error = result.get("error")
                status.output_uri, status.output_table = result.get("output_uri"), result.get("output_table")
                status.rows_processed = status.total_rows = int(result.get("rows") or 0)
            elif status.status == "running":
                status.status, status.error = "failed", "ray driver exited without writing a status"
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

    def _run_ir_sync(self, ir, graph, target) -> dict:
        """Child side (in the driver subprocess, Ray already init'd): execute the clean IR synchronously
        and return a result dict for the parent. Reuses _build/_commit; the fresh-process DuckDB is safe
        because Ray was init'd before it was created."""
        try:
            datasets: dict[str, object] = {}
            rows, out_uri, out_table = 0, None, None
            for step in ir.steps:
                if step.op == "write":
                    rows, out_uri, out_table = self._commit(step, datasets, graph)
                else:
                    datasets[step.id] = self._build(step, datasets)
            if target and target in datasets:  # a non-sink target → force a real row count
                rows = datasets[target].count()
            return {"status": "done", "rows": rows, "output_uri": out_uri, "output_table": out_table}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}

    def _run_ir_materialize(self, ir, graph, target, uri) -> dict:
        """Child side, region mode: run the clean IR up to `target` on Ray (reads worker-direct) and
        write that dataset to `uri` as a SINGLE parquet — the RunController handoff contract (a
        downstream region's ref-source reads one file). The final collect matches run()'s sink funnel."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        from hub.plugins.adapters import is_object_uri
        try:
            datasets: dict[str, object] = {}
            for step in ir.steps:
                if step.op == "write":  # a region is cut BEFORE any write; ignore a stray one
                    continue
                datasets[step.id] = self._build(step, datasets)
            ds = datasets[target]
            batches = list(ds.iter_batches(batch_format="pyarrow"))
            tbl = pa.concat_tables(batches) if batches else pa.table({})
            if is_object_uri(uri):
                with db.base_guard():
                    db.ensure_object_store()
                    db.conn().from_arrow(tbl).write_parquet(uri)
            else:
                os.makedirs(os.path.dirname(uri) or ".", exist_ok=True)
                pq.write_table(tbl, uri)
            return {"status": "done", "rows": tbl.num_rows, "output_uri": uri, "output_table": None}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}

    def _build(self, step, datasets):
        import ray

        if step.op == "read":
            uri = step.config["uri"]
            from hub.plugins.adapters import is_object_uri
            if uri.lower().endswith((".parquet", ".pq")) and not is_object_uri(uri):
                try:
                    return ray.data.read_parquet(uri)  # WORKER-DIRECT: Ray reads parquet on workers, no driver funnel
                except Exception:  # noqa: BLE001 — fall back to the always-works driver-Arrow path below
                    pass
            with db.base_guard():                              # any other source (Lance/HF/Iceberg/CSV/object/plugin)
                tbl = self.resolve_adapter(uri).scan(uri).to_arrow_table()
            return ray.data.from_arrow(tbl)
        parent = datasets[step.inputs[0][0]]                   # clean transforms/passthrough are single-input
        if step.op == "passthrough":
            return parent
        if step.op in CLEAN_TRANSFORM_MODES:
            return parent.map_batches(_make_mapper(step.config), batch_format="pyarrow")
        raise RuntimeError(f"ray backend reached a non-clean op '{step.op}' (should have fallen back)")

    def _commit(self, step, datasets, graph) -> tuple[int, str, str]:
        import pyarrow as pa

        cfg = step.config
        raw = cfg.get("filename") or cfg.get("name") or cfg.get("title") or "output"  # match LocalRunner
        fname = "".join(c if c.isalnum() or c in "_-." else "_" for c in str(raw)).strip(".") or "output"
        base, ext = os.path.splitext(fname)
        if ext.lower() not in _KNOWN_EXT:
            ext = {"parquet": ".parquet", "csv": ".csv", "lance": ".lance"}.get(
                (cfg.get("format") or "parquet").lower(), ".parquet")
            base = fname
        name, mode = base, cfg.get("writeMode", "overwrite")
        uri = self.base.storage.output_uri(base, ext)          # reference: default storage (no destId)
        ds = datasets[step.inputs[0][0]]
        batches = list(ds.iter_batches(batch_format="pyarrow"))  # collect blocks to the driver
        tbl = pa.concat_tables(batches) if batches else pa.table({})
        with db.base_guard():
            rel = db.conn().from_arrow(tbl)
            res = self.resolve_adapter(uri).write(uri, rel, mode)  # same write + catalog contract as local
        out_uri = res.get("uri", uri)
        parents = [u for (sid, _h) in step.inputs for u in [self.base._source_uri(nm_node=sid, graph=graph)] if u]
        self.catalog.register_output(name=name, uri=out_uri, version="v1", parents=parents, pipeline="canvas")
        return int(res.get("rows") or 0), out_uri, name


def register(reg) -> None:
    # opt-in: added as an available backend, selected only when execution == 'ray-data' (never the default)
    reg.add_runner(RayRunner(reg.deps))
