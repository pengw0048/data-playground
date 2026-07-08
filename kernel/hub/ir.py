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


def resolve_config(node: GraphNode) -> dict:
    """The canonical, resolved config for a built-in node's TYPE — the SINGLE place built-in node config
    is read + key-normalized, so the IR AND the DuckDB engine (executors/engine.py `_lower`) consume the
    same thing and can't diverge (the class of bug that produced the earlier plan_is_clean mismatch).
    Type-keyed (independent of bypass/disabled — those are op-level, handled by `_op_and_config`). Only
    KEY canonicalization + `uri||table` resolution + `options` nesting + structural defaults (`how`) live
    here; VALUE-level normalization (`.strip()`, engine-context defaults like the preview sample size)
    stays in the consumer, so this never changes what the engine computes."""
    t = node.type
    cfg = _cfg(node)
    if t in ("transform", "notebook"):
        c: dict = {"mode": cfg.get("mode", "map"), "onError": cfg.get("onError", "raise")}
        if cfg.get("source") == "library" and cfg.get("processor"):
            c |= {"source": "library", "processor": cfg.get("processor"), "params": cfg.get("params", {})}
        if cfg.get("code"):  # keep the code too — it's the portable, self-contained operator
            c["code"] = cfg["code"]
        return c
    if t == "source":
        opts = {k: str(cfg[k]).strip().lower() if k == "header" else str(cfg[k]).strip()
                for k in ("delimiter", "header") if str(cfg.get(k, "")).strip()}
        opts = {k: v for k, v in opts.items() if k != "header" or v in ("yes", "no")}  # header must be yes/no
        c = {"uri": cfg.get("uri") or cfg.get("table")}
        if opts:
            c["options"] = opts
        return c
    if t == "filter":
        return {"predicate": cfg.get("predicate", "")}
    if t == "select":
        return {"expr": cfg.get("select") or cfg.get("expr") or ""}
    if t == "sql":
        return {"sql": cfg.get("sql", "")}
    if t == "join":
        return {"on": cfg.get("on", ""), "condition": cfg.get("condition", ""), "how": cfg.get("how", "inner")}
    if t == "aggregate":
        return {"groupBy": cfg.get("groupBy") or cfg.get("group") or "", "aggs": cfg.get("aggs", "")}
    if t == "sort":
        return {"by": cfg.get("by", "")}
    if t == "dedup":
        return {"on": cfg.get("on", "")}
    if t == "sample":
        return {"n": cfg.get("n"), "seed": cfg.get("seed", 42)}  # n=None → the engine applies sample_k
    if t == "write":
        return {"name": cfg.get("name"), "filename": cfg.get("filename"),
                "title": node.data.get("title") if isinstance(node.data, dict) else None,
                "format": cfg.get("format", "parquet"), "writeMode": cfg.get("writeMode", "overwrite")}
    return dict(cfg)  # metric/chart/vector-search/section/opaque/loop/variable — carry cfg verbatim


def _op_and_config(node: GraphNode) -> tuple[str, dict]:
    if _flag(node, "disabled"):
        return "disabled", {}
    if _flag(node, "bypassed"):
        return "passthrough", {}
    cfg = resolve_config(node)
    t = node.type
    if t in ("transform", "notebook"):
        mode = cfg.get("mode", "map")
        op = mode if mode in CLEAN_TRANSFORM_MODES else f"transform:{mode}"
    else:
        op = _NODE_OP.get(t, f"opaque:{t}")  # unknown/plugin kinds → opaque (a backend must fall back)
    return op, cfg


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
    """A FAST clean-subset pre-gate from a CompilePlan alone (for `ExecutionBackend.can_run`). A CompilePlan
    doesn't carry a node's `disabled`/`bypassed` flags, so this can't see them — it classifies purely by
    (kind, mode). That means it can disagree with `CompiledIR.is_clean()` on nodes whose flags matter (a
    disabled node the plan reads as its clean kind; a bypassed relational node the IR would call
    `passthrough`). So a backend MUST re-derive the IR and re-check `is_clean()` before executing — `dp_ray`
    does, falling back to the DuckDB engine on a mismatch. (A disabled node makes the graph unrunnable on
    ANY backend anyway, so the disagreement never yields a wrong result, only a fallback.)"""
    return bool(plan.acyclic) and bool(plan.steps) and all(_plan_step_clean(s) for s in plan.steps)
