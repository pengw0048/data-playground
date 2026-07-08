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
collects to the driver and writes via the adapter, so the catalog/write contract is unchanged. A
production backend would use `ray.data.read_*`/`write_parquet` for fully-distributed I/O; cancellation
here is cooperative between IR steps (Ray has no cheap mid-Dataset abort), weaker than the DuckDB
backend's cursor interrupt.

Opt-in: `pip install 'data-playground[ray]'`, drop this folder in `<workspace>/plugins/`, and select it
via Settings → Execution or `DP_EXECUTION=ray-data`. It never becomes the default (the kernel is), so a
small graph won't spin up Ray unless you ask.
"""

from __future__ import annotations

import os
import threading
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
        ir = lower_to_ir(graph, target_node_id, self.node_specs)
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
        threading.Thread(target=self._execute, args=(run_id, ir, graph, target_node_id, status),
                         daemon=True).start()
        return status

    def _emit(self, graph, status) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001 — never let persistence break a run
                pass

    def _execute(self, run_id, ir, graph, target, status) -> None:
        import ray

        cancel = self._cancel[run_id]
        status.status = "running"
        self._emit(graph, status)
        rows_seen = 0
        try:
            ray.init(ignore_reinit_error=True, configure_logging=False, log_to_driver=False,
                     include_dashboard=False)
            datasets: dict[str, object] = {}
            for step in ir.steps:
                if cancel.is_set():
                    status.status = "cancelled"
                    return
                pn = next((p for p in status.per_node if p.node_id == step.id), None)
                if pn:
                    pn.status = "running"
                if step.op == "write":
                    rows_seen = self._commit(step, datasets, graph, status)
                else:
                    datasets[step.id] = self._build(step, datasets)
                if pn:
                    pn.status = "done"
                    pn.rows = rows_seen or None
                status.rows_processed = rows_seen
                self._emit(graph, status)
            if target and target in datasets:  # a non-sink target → force a real row count
                rows_seen = datasets[target].count()
                status.rows_processed = rows_seen
            status.status = "done"
        except Exception as e:  # noqa: BLE001
            status.status = "cancelled" if cancel.is_set() else "failed"
            if status.status == "failed":
                status.error = f"{type(e).__name__}: {e}"
            for p in status.per_node:
                if p.status == "running":
                    p.status = status.status
        finally:
            status.total_rows = rows_seen
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
            if self.on_complete:
                try:
                    self.on_complete(graph, target, status)
                except Exception:  # noqa: BLE001
                    pass

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

    def _commit(self, step, datasets, graph, status) -> int:
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
        status.output_uri, status.output_table = out_uri, name
        return int(res.get("rows") or 0)


def register(reg) -> None:
    # opt-in: added as an available backend, selected only when execution == 'ray-data' (never the default)
    reg.add_runner(RayRunner(reg.deps))
