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

ENVIRONMENT SENSITIVITY of the live path. Verified: Ray Data runs on a dev box, and this backend executes
a real single-op graph correctly IN-PROCESS. But driving the FULL subprocess path to completion is
sensitive to the local Ray environment — observed on one macOS box: the child's Ray cluster spawned its
worker pool but the task stayed unscheduled (workers idle), plausibly a macOS worker-spawn / local-Ray
quirk. So the opt-in `test_ray_backend_live_differential` may hang on such a setup — **verify on a clean
Linux Ray environment**, where Ray Data is far more reliable.
The Part B MECHANISM (a plugin node's `ir` hook → clean op → routed to a distributed backend, operator
byte-identical) is verified WITHOUT a cluster by `test_plugin_node_ir_hook_runs_on_duckdb_and_ray`.

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
from hub.models import PerNodeStatus, RunStatus

_KNOWN_EXT = (".parquet", ".pq", ".csv", ".tsv", ".arrow", ".feather", ".ipc", ".json", ".lance")


def _make_mapper(config: dict):
    """A Ray Data batch UDF that reuses the DuckDB engine's EXACT operator — so a transform produces the
    same rows on Ray as locally. Captures only plain strings, so it cloudpickles to Ray workers."""
    code, mode, on_error = config.get("code"), config["mode"], config.get("onError", "raise")

    def _op(table):  # a pyarrow.Table block
        import pyarrow as pa

        from hub import sandbox
        from hub.executors.engine import _apply_fn

        fn = sandbox.compile_operator(code, mode)
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

    def _supervise(self, run_id, graph, target, status) -> None:
        """Parent side: spawn the isolated Ray driver, poll its status file, mirror the result. Touches
        NO DuckDB (only subprocess + files + the DB-backed on_status/on_complete hooks) → never deadlocks."""
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
                       "status_file": status_file}, f)
        driver = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_driver.py")
        result = None
        try:
            # Redirect the child's stdio to a log file (never an inherited pipe — Ray logs copiously and
            # a full pipe would block the child mid-run; the result comes back via status_file). Own
            # session so Ray's worker signals/pgroup are decoupled from the (daemon-thread) parent.
            _dlog = open(os.path.join(work, "driver.log"), "w")
            proc = subprocess.Popen([sys.executable, driver, job_file],
                                    stdout=_dlog, stderr=_dlog, start_new_session=True,
                                    env={**os.environ, "RAY_DATA_DISABLE_PROGRESS_BARS": "1"})
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

    def _build(self, step, datasets):
        import ray

        if step.op == "read":
            uri = step.config["uri"]
            with db.base_guard():                              # any source (incl. Lance/HF/Iceberg/plugin)
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
