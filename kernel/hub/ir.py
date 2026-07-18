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

import re
from dataclasses import dataclass, field

from hub import graph as g
from hub.models import CompilePlan, Graph, GraphNode

# transform modes a per-row / per-batch engine runs faithfully (the reduce-y `aggregate` and
# escape-hatch `callable` modes need a full custom pass, so they are NOT clean).
CLEAN_TRANSFORM_MODES = {"map", "map_batches", "filter", "flat_map", "flat_map_generator"}
# ops a simple map-style engine (e.g. Ray Data) can run end-to-end
CLEAN_OPS = {"read", "write", "passthrough"} | CLEAN_TRANSFORM_MODES

# RELATIONAL ops a SHUFFLE-capable backend MAY additionally claim — beyond the map-style CLEAN_OPS. This
# is an enumeration, NOT a global gate flip: capability is decided per-backend by passing its own subset
# to plan_is_distributable, so the DuckDB fallback stays authoritative for everything a backend can't yet
# run byte-identically. (ARC3 grows this as each op is validated on a real cluster.)
DISTRIBUTABLE_RELATIONAL = frozenset({"aggregate", "window", "dedup", "join", "sort"})

# A distributed backend runs relational ops by SHUFFLING on a key (Ray) then computing with DuckDB per
# partition (see dp_ray) — so the only thing it parses is the shuffle KEY, not the operation. This keeps
# the hand-written surface tiny and conservative: an un-parseable key returns None → the backend falls
# back to single-node DuckDB. The aggregates/expressions themselves are never parsed — DuckDB runs the
# same SQL fragment the single-node engine does, so the result is identical by construction.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_group_keys(group: str) -> list[str] | None:
    """A GROUP BY fragment → the list of bare group-key columns to hash-shuffle on ([] = a global/no-key
    aggregate), or None if any key is an expression / quoted / parenthesized (no plain shuffle key ⇒ not
    distributable ⇒ DuckDB single-node)."""
    s = (group or "").strip()
    if not s:
        return []  # global aggregate — valid (a single-partition reduce), distinct from unparseable (None)
    keys = [p.strip() for p in s.split(",")]
    return keys if all(_IDENT_RE.match(k) for k in keys) else None


_SORT_TERM = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(asc|desc)?\s*$", re.I)


def parse_sort_keys(by: str) -> list[tuple[str, bool]] | None:
    """An ORDER BY fragment → [(column, descending)], or None if any term is not a bare column with an
    optional ASC/DESC (an expression / NULLS clause / quoted name → None → DuckDB single-node). A
    distributed sort is only claimed for a plain-column key; ties + NULL placement are checked per-op."""
    s = (by or "").strip()
    if not s:
        return None
    out: list[tuple[str, bool]] = []
    for part in s.split(","):
        m = _SORT_TERM.match(part)
        if not m:
            return None
        out.append((m.group(1), (m.group(2) or "asc").lower() == "desc"))
    return out

# canvas node type → IR op. `transform` resolves to its MODE (a clean transform mode, or
# `transform:<mode>` when not clean); anything not listed (incl. plugin kinds) → `opaque:<type>`.
_NODE_OP = {
    "source": "read", "filter": "filter_sql", "select": "project_sql", "sql": "sql", "join": "join",
    "union": "union",
    "aggregate": "aggregate", "sort": "sort", "dedup": "dedup", "sample": "sample", "write": "write",
    "window": "window",
    "metric": "metric", "chart": "chart", "vector-search": "vector_search", "section": "section",
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

    def is_distributable(self, extra_ops: frozenset[str]) -> bool:
        """True iff every step is in the clean subset OR in `extra_ops` — a shuffle-capable backend that
        claims `extra_ops` (e.g. {'aggregate'}) can run the whole graph. Keeps `is_clean` intact; a
        backend still re-checks per-op runnability (e.g. parse_aggs != None) before executing."""
        allowed = CLEAN_OPS | extra_ops
        return bool(self.steps) and all(s.op in allowed for s in self.steps)

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
    KEY canonicalization + source options nesting + structural defaults (`how`) live
    here; VALUE-level normalization (`.strip()`, engine-context defaults like the preview sample size)
    stays in the consumer, so this never changes what the engine computes."""
    t = node.type
    cfg = _cfg(node)
    if t == "transform":
        c: dict = {"mode": cfg.get("mode", "map"), "onError": cfg.get("onError", "raise")}
        if cfg.get("batchFormat") in ("pandas", "arrow"):  # map_batches representation (else row-dicts)
            c["batchFormat"] = cfg["batchFormat"]
        if cfg.get("source") == "library":  # keep 'source' even without a processor, so the engine's
            c["source"] = "library"          # library branch still runs (and errors honestly if unconfigured)
            if cfg.get("processor"):
                c |= {"processor": cfg.get("processor"), "params": cfg.get("params", {})}
        if cfg.get("code"):  # keep the code too — it's the portable, self-contained operator
            c["code"] = cfg["code"]
        # A schema-changing Ray UDF can produce zero rows, at which point Ray 2.56 drops every output
        # block and its schema. Carry the portable contract so a distributed backend can publish a typed
        # empty result without re-running user code. Named refs are resolved before isolated dispatch.
        if isinstance(cfg.get("outputSchema"), (list, dict)):
            c["outputSchema"] = cfg["outputSchema"]
        if cfg.get("enforceSchema") is True:
            c["enforceSchema"] = True
        return c
    if t == "source":
        opts = {k: str(cfg[k]).strip().lower() if k == "header" else str(cfg[k]).strip()
                for k in ("delimiter", "header") if str(cfg.get(k, "")).strip()}
        opts = {k: v for k, v in opts.items() if k != "header" or v in ("yes", "no")}  # header must be yes/no
        c = {"uri": cfg.get("uri")}
        dataset_ref = cfg.get("datasetRef")
        if isinstance(dataset_ref, dict):
            # Keep the strict intent variant and its one-time exact resolution evidence together.
            c["datasetRef"] = dataset_ref
        # Hub-only local-run admission evidence.  It is written only onto the private dispatch copy
        # after source resolution; accepting it here lets the default engine open that exact revision
        # without exposing a client-side Source configuration surface.
        if isinstance(cfg.get("_input_revision_id"), str) and cfg["_input_revision_id"]:
            c["_input_revision_id"] = cfg["_input_revision_id"]
        if isinstance(cfg.get("_input_artifact_uri"), str) and cfg["_input_artifact_uri"]:
            c["_input_artifact_uri"] = cfg["_input_artifact_uri"]
        if isinstance(cfg.get("_input_provider_uri"), str) and cfg["_input_provider_uri"]:
            c["_input_provider_uri"] = cfg["_input_provider_uri"]
        if opts:
            c["options"] = opts
        return c
    if t == "filter":
        return {"predicate": cfg.get("predicate", "")}
    if t == "assert":  # a data-quality gate: rows where `predicate` is not TRUE = violations
        return {"predicate": cfg.get("predicate", ""), "severity": cfg.get("severity", "warn")}
    if t == "select":
        return {"expr": cfg.get("select") or cfg.get("expr") or ""}
    if t == "sql":
        return {"sql": cfg.get("sql", "")}
    if t == "join":
        return {"on": cfg.get("on", ""), "condition": cfg.get("condition", ""), "how": cfg.get("how", "inner")}
    if t == "union":
        return {"mode": cfg.get("mode", "all"), "align": cfg.get("align", "name")}
    if t == "aggregate":
        return {"groupBy": cfg.get("groupBy") or cfg.get("group") or "", "aggs": cfg.get("aggs", "")}
    if t == "sort":
        return {"by": cfg.get("by", "")}
    if t == "dedup":
        return {"on": cfg.get("on", "")}
    if t == "window":
        return {"expr": cfg.get("expr", ""), "partitionBy": cfg.get("partitionBy", ""),
                "orderBy": cfg.get("orderBy", ""), "as": cfg.get("as") or "window"}
    if t == "fill":
        return {"columns": cfg.get("columns", ""), "method": cfg.get("method", "constant"),
                "value": cfg.get("value", "")}
    if t == "unnest":
        return {"column": cfg.get("column", "")}
    if t == "sample":
        return {"n": cfg.get("n"), "seed": cfg.get("seed", 42)}  # n=None → the engine applies sample_k
    if t == "write":
        return {"name": cfg.get("name"), "filename": cfg.get("filename"),
                "title": node.data.get("title") if isinstance(node.data, dict) else None,
                "format": cfg.get("format", "parquet"), "writeMode": cfg.get("writeMode", "overwrite"),
                "destId": cfg.get("destId"), "destPath": cfg.get("destPath", ""),
                "partitionBy": cfg.get("partitionBy", "")}
    return dict(cfg)  # metric/chart/vector-search/section and plugin configs stay verbatim


def _op_and_config(node: GraphNode, node_ir: dict | None = None) -> tuple[str, dict]:
    if _flag(node, "disabled"):
        return "disabled", {}
    if _flag(node, "bypassed"):
        return "passthrough", {}
    t = node.type
    if t == "transform":
        cfg = resolve_config(node)
        mode = cfg.get("mode", "map")
        return (mode if mode in CLEAN_TRANSFORM_MODES else f"transform:{mode}"), cfg
    if t in _NODE_OP:
        return _NODE_OP[t], resolve_config(node)  # a built-in node
    # a plugin/unknown kind: use its engine-neutral emit hook (reg.add_node(..., ir=…)) if it has one, so
    # it lowers to a real op (e.g. a clean `map`) a distributed backend can run — else it's opaque (DuckDB-only)
    hook = (node_ir or {}).get(t)
    if callable(hook):
        try:
            emitted = hook(node)
        except Exception:  # noqa: BLE001 — a buggy plugin hook must NOT brick compile/estimate/run
            emitted = None  #                 (/graph/compile runs on every edit) → degrade to opaque
        if isinstance(emitted, dict) and emitted.get("op"):
            return emitted["op"], dict(emitted.get("config", {}))
    return f"opaque:{t}", dict(_cfg(node))


def lower_to_ir(graph: Graph, target_node_id: str | None = None, node_specs: dict | None = None,
                node_ir: dict | None = None) -> CompiledIR:
    """Lower a canvas graph to the engine-neutral IR — the target's upstream cone in topological order
    (or the whole graph when target is None). Reads each node's config exactly once. `node_ir` (kind →
    ir hook, from deps.node_ir) lets plugin nodes emit a real op instead of `opaque`."""
    chain = g.upstream_chain(graph, target_node_id) if target_node_id else g.topo_order(graph)
    steps: list[IRStep] = []
    for node in chain:
        op, cfg = _op_and_config(node, node_ir)
        inputs = [(e.source, e.source_handle) for e in g.incoming(graph, node.id)]
        steps.append(IRStep(id=node.id, op=op, config=cfg, inputs=inputs))
    return CompiledIR(target=target_node_id, steps=steps)


def plan_is_clean(plan: CompilePlan) -> bool:
    """Clean-subset gate from a CompilePlan alone (for `ExecutionBackend.can_run`). Each PlanStep now
    carries its IR `op` (compile_plan sets it via `_op_and_config`, incl. a plugin node's engine-neutral
    emit hook), so this is the AUTHORITATIVE classification — the same the IR uses — not a (kind, mode)
    heuristic. Note `op` is derived without the disabled/bypassed flags, so a backend should still
    re-check `is_clean()` on the freshly-lowered IR before executing (dp_ray does); erring toward the
    DuckDB fallback is always safe."""
    return bool(plan.acyclic) and bool(plan.steps) and all(s.op in CLEAN_OPS for s in plan.steps)


def plan_is_distributable(plan: CompilePlan, extra_ops: frozenset[str]) -> bool:
    """Like `plan_is_clean`, but a shuffle-capable backend also admits the relational `extra_ops` it
    claims (e.g. {'aggregate'}). Same op-derivation caveat as plan_is_clean — the backend re-checks the
    freshly-lowered IR (and per-op runnability, e.g. parse_aggs) before executing; DuckDB fallback is
    always the safe default."""
    allowed = CLEAN_OPS | extra_ops
    return bool(plan.acyclic) and bool(plan.steps) and all(s.op in allowed for s in plan.steps)
