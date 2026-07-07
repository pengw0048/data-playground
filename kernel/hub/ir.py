"""Engine-neutral execution IR — a typed, portable description of a canvas run.

`BuildEngine._lower` (executors/engine.py) turns a graph into DuckDB relations by reading each node's
`data.config` directly and emitting DuckDB SQL / relation ops. That is perfect for the default
out-of-core engine but useless to any OTHER engine: a non-DuckDB backend (Ray, Spark, a warehouse)
would have to re-read every node's config and re-implement lowering, and it can't run third-party
plugin nodes at all.

This module is the neutral contract that breaks that coupling. `lower_to_ir(graph, target)` reads the
configs ONCE and produces a `CompiledIR`: a topological list of `IRStep`s, each carrying a normalized
`op`, its RESOLVED config (no DuckDB SQL objects — plain strings/values), and its input wiring. A
backend pattern-matches on `op` and never touches node configs. The default engine still lowers
directly (its lowering is proven); the IR is what a SECOND engine consumes — see the `dp_ray`
reference backend, which runs the CLEAN subset of ops (`read` → per-row/-batch `map`/`filter`/
`flat_map`/`map_batches` → `write`) on Ray Data straight from this IR.

`is_clean()` marks a run a simple map-style engine can execute faithfully; everything relational
(`sql`/`join`/`aggregate`/`sort`/`dedup`), reducing (`metric`/`chart`), or opaque (`section`, plugin
kinds) stays `unsupported` — a backend should fall back to the DuckDB engine for those. `plan_is_clean`
answers the same question from a `CompilePlan` (what `ExecutionBackend.can_run` receives), so a backend
can gate WITHOUT re-deriving the IR.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hub import graph as g
from hub.models import CompilePlan, Graph, GraphNode

# transform/notebook modes a per-row / per-batch engine runs faithfully (the reduce-y `aggregate` and
# escape-hatch `callable` modes need a full custom pass, so they are NOT clean).
CLEAN_TRANSFORM_MODES = {"map", "map_batches", "filter", "flat_map", "flat_map_generator"}
# ops a simple map-style engine (e.g. Ray Data) can run end-to-end
CLEAN_OPS = {"read", "write", "passthrough"} | CLEAN_TRANSFORM_MODES

# canvas node type → IR op. `transform`/`notebook` resolve to their MODE (a clean transform mode, or
# `transform:<mode>` when not clean); anything not listed (incl. plugin kinds) → `opaque:<type>`.
_NODE_OP = {
    "source": "read", "filter": "filter_sql", "select": "project_sql", "sql": "sql", "join": "join",
    "aggregate": "aggregate", "sort": "sort", "dedup": "dedup", "sample": "sample", "write": "write",
    "metric": "metric", "chart": "chart", "vector-search": "vector_search", "section": "section",
    "opaque": "opaque", "loop": "loop", "variable": "variable",
}


@dataclass
class IRStep:
    id: str                                  # the node id
    op: str                                  # normalized operator (see _NODE_OP / CLEAN_OPS)
    config: dict                             # RESOLVED, portable config — no DuckDB objects
    inputs: list[tuple[str, str | None]]     # [(source_node_id, source_handle)] in incoming-edge order


@dataclass
class CompiledIR:
    target: str | None
    steps: list[IRStep] = field(default_factory=list)

    def unsupported(self) -> list[str]:
        """The ops (deduped, in first-seen order) that fall outside the clean subset."""
        seen: list[str] = []
        for s in self.steps:
            if s.op not in CLEAN_OPS and s.op not in seen:
                seen.append(s.op)
        return seen

    def is_clean(self) -> bool:
        """True iff every step is in the clean subset — a map-style engine can run the whole graph."""
        return bool(self.steps) and not self.unsupported()

    def by_id(self) -> dict[str, IRStep]:
        return {s.id: s for s in self.steps}


def _cfg(node: GraphNode) -> dict:
    return node.data.get("config", {}) if isinstance(node.data, dict) else {}


def _flag(node: GraphNode, key: str) -> bool:
    return bool(node.data.get(key)) if isinstance(node.data, dict) else False


def _op_and_config(node: GraphNode) -> tuple[str, dict]:
    if _flag(node, "disabled"):
        return "disabled", {}
    if _flag(node, "bypassed"):
        return "passthrough", {}
    t = node.type
    cfg = _cfg(node)

    if t in ("transform", "notebook"):
        mode = cfg.get("mode", "map")
        op = mode if mode in CLEAN_TRANSFORM_MODES else f"transform:{mode}"
        c: dict = {"mode": mode, "onError": cfg.get("onError", "raise")}
        if cfg.get("source") == "library" and cfg.get("processor"):
            c |= {"source": "library", "processor": cfg.get("processor"), "params": cfg.get("params", {})}
        if cfg.get("code"):  # keep the code too — it's the portable, self-contained operator
            c["code"] = cfg["code"]
        return op, c

    op = _NODE_OP.get(t, f"opaque:{t}")  # unknown/plugin kinds → opaque (a backend must fall back)
    if op == "read":
        c = {"uri": cfg.get("uri") or cfg.get("table")}
        opts = {k: str(cfg[k]).strip() for k in ("delimiter", "header") if str(cfg.get(k, "")).strip()}
        if opts:
            c["options"] = opts
        return op, c
    if op == "filter_sql":
        return op, {"predicate": (cfg.get("predicate") or "").strip()}
    if op == "project_sql":
        return op, {"expr": (cfg.get("select") or cfg.get("expr") or "").strip()}
    if op == "sql":
        return op, {"sql": (cfg.get("sql") or "").strip()}
    if op == "join":
        return op, {"on": cfg.get("on", ""), "condition": cfg.get("condition", ""), "how": cfg.get("how", "inner")}
    if op == "aggregate":
        return op, {"groupBy": cfg.get("groupBy") or cfg.get("group", ""), "aggs": cfg.get("aggs", "")}
    if op == "sort":
        return op, {"by": cfg.get("by", "")}
    if op == "dedup":
        return op, {"on": cfg.get("on", "")}
    if op == "sample":
        return op, {"n": cfg.get("n", 1000), "seed": cfg.get("seed", 42)}
    if op == "write":
        return op, {"name": cfg.get("name"), "filename": cfg.get("filename"),
                    "format": cfg.get("format", "parquet"), "writeMode": cfg.get("writeMode", "overwrite")}
    return op, dict(cfg)  # metric/chart/vector-search/section/opaque/loop/variable — carry cfg verbatim


def lower_to_ir(graph: Graph, target_node_id: str | None = None, node_specs: dict | None = None) -> CompiledIR:
    """Lower a canvas graph to the engine-neutral IR — the target's upstream cone in topological order
    (or the whole graph when target is None). Reads each node's config exactly once."""
    chain = g.upstream_chain(graph, target_node_id) if target_node_id else g.topo_order(graph)
    steps: list[IRStep] = []
    for node in chain:
        op, cfg = _op_and_config(node)
        inputs = [(e.source, e.source_handle) for e in g.incoming(graph, node.id)]
        steps.append(IRStep(id=node.id, op=op, config=cfg, inputs=inputs))
    return CompiledIR(target=target_node_id, steps=steps)


# op a PlanStep maps to, WITHOUT the graph — mirrors _op_and_config using only (kind, mode) so that
# ExecutionBackend.can_run (which receives a CompilePlan, not the graph) can gate on the clean subset.
def _plan_step_clean(step) -> bool:
    if step.kind in ("read", "write"):
        return True
    # a transform/notebook node compiles to kind 'op' with mode = the transform mode; a bare relational
    # node (sort/dedup/variable/section/plugin) is also kind 'op' but its mode is its own type → excluded.
    return step.kind == "op" and step.mode in CLEAN_TRANSFORM_MODES


def plan_is_clean(plan: CompilePlan) -> bool:
    """Conservative clean-subset check from a CompilePlan alone (for `can_run`). Conservative because a
    bypassed relational node reads as its own kind here (→ not clean) though the IR would mark it
    `passthrough`; erring toward fallback is always safe (the DuckDB engine runs it correctly)."""
    return bool(plan.acyclic) and bool(plan.steps) and all(_plan_step_clean(s) for s in plan.steps)
