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

from kernel import graph as g
from kernel.executors.engine import LoweringEngine
from kernel.models import (
    CompilePlan,
    Graph,
    PerNodeStatus,
    Placement,
    RunEstimate,
    RunStatus,
)

_OP_SECONDS_PER_1K = {
    "read": 0.01, "sample": 0.005, "filter": 0.008, "select": 0.006, "op": 0.02, "sql": 0.02,
    "join": 0.05, "reduce": 0.03, "write": 0.03, "opaque": 0.2, "error_gate": 0.001,
}
_COST_PER_SEC = 0.0008
_DISTRIBUTED_ROWS = 20_000_000
_CONFIRM_COST = 5.0


class LocalRunner:
    name = "local-out-of-core"

    def __init__(self, resolve_adapter, registry, catalog, workspace: str, node_lowerings=None,
                 node_specs=None):
        self.resolve_adapter = resolve_adapter
        self.registry = registry
        self.catalog = catalog
        self.workspace = workspace
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
    def estimate(self, plan: CompilePlan, rows: int) -> RunEstimate:
        seconds = max(0.15, sum(_OP_SECONDS_PER_1K.get(s.kind, 0.02) * (rows / 1000.0) for s in plan.steps))
        cost = round(seconds * _COST_PER_SEC * (1 + rows / 1_000_000), 4)
        placement: Placement = "distributed" if rows >= _DISTRIBUTED_ROWS else "local"
        needs_confirm = cost >= _CONFIRM_COST or placement == "distributed"
        return RunEstimate(rows=rows, seconds=round(seconds, 2), cost_usd=cost, placement=placement,
                           needs_confirm=needs_confirm, breakdown=f"{rows:,} rows · {len(plan.steps)} steps · out-of-core")

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
        threading.Thread(target=self._execute, args=(run_id, plan, graph, target_node_id), daemon=True).start()
        return status

    def _execute(self, run_id: str, plan: CompilePlan, graph: Graph, target: str | None) -> None:
        status = self.runs[run_id]
        cancel = self._cancel[run_id]
        started = time.time()
        status.status = "running"
        phash = self._plan_hash(graph, target)
        cached = self._cache.get(phash)
        engine = LoweringEngine(graph, self.resolve_adapter, self.registry, full=True,
                                node_lowerings=self.node_lowerings, node_specs=self.node_specs)
        nm = g.node_map(graph)
        rows_seen = 0
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
            self._cache[phash] = {"rows": rows_seen, "uri": status.output_uri, "table": status.output_table}
        except Exception as e:  # noqa: BLE001
            status.status = "failed"
            status.error = f"{type(e).__name__}: {e}"
            for p in status.per_node:
                if p.status == "running":
                    p.status = "failed"
        finally:
            status.ms = int((time.time() - started) * 1000)
            status.total_rows = rows_seen
            status.cost_usd = round(status.ms / 1000 * _COST_PER_SEC, 4)

    def _count(self, engine: LoweringEngine, node_id: str, cached: dict | None) -> int:
        if cached and cached.get("rows") is not None:
            return cached["rows"]
        return int(engine.relation(node_id).aggregate("count(*) AS n").fetchone()[0])

    def _commit_write(self, node, graph: Graph, engine: LoweringEngine, status: RunStatus,
                      cached: dict | None) -> int:
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        name = cfg.get("name") or node.data.get("title") or "output"
        name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        fmt = (cfg.get("format") or "parquet").lower()
        ext = {"parquet": ".parquet", "csv": ".csv", "lance": ".lance"}.get(fmt, ".parquet")
        out_dir = os.path.join(self.workspace, "outputs")
        os.makedirs(out_dir, exist_ok=True)
        uri = os.path.join(out_dir, f"{name}{ext}")

        parents = g.parents(graph, node.id)
        parent_rel = engine.relation(parents[0]) if parents else None
        if parent_rel is None:
            return 0
        adapter = self.resolve_adapter(uri)
        res = adapter.write(uri, parent_rel, cfg.get("writeMode", "overwrite"))
        rows = int(res.get("rows") or 0)

        parent_uris = [u for pid in parents for u in [self._source_uri(nm_node=pid, graph=graph)] if u]
        self.catalog.register_output(name=name, uri=uri, version="v1", parents=parent_uris, pipeline="canvas")
        status.output_uri = uri
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
            st.status = "cancelled"
        return st
