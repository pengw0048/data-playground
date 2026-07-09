"""Per-node output-SIZE estimate — a placement-independent, bottom-up pass over the graph.

Feeds three consumers: the run confirm-gate (rows at the target), the placement policy (does a region's
working set fit a backend's memory? — see placement.py / run_controller.py), and an optional UI size hint.

Conservative BY CONSTRUCTION so we never under-estimate and mis-place a big step "local":
- a row-reducing op we can't bound (filter, dedup) keeps its INPUT row count — an honest UPPER bound,
  never a fabricated selectivity fraction.
- a genuinely unknown count (aggregate collapse, join fan-out, sql, opaque code) is reported as
  rows=None / confidence="unknown" — the UI shows nothing rather than a made-up number, and the
  confirm-gate errs toward asking.
- a MEASURED actual (a prior run's real row count, or a materialized boundary) overrides the estimate.

The estimate is placement-INDEPENDENT (a property of the data flow, not of where it runs), so it's a
single pass computed BEFORE placement — which is what lets the cost-based placement avoid a chicken-and-egg.
"""

from __future__ import annotations

from dataclasses import dataclass

from hub import graph as g
from hub.ir import resolve_config
from hub.models import Graph, GraphNode

# ops whose result needs ~O(input) memory (a hash/sort build) → they set a region's working-set need;
# streaming ops (scan/filter/select/map/sample/limit/write) need ~O(1) and don't.
_BLOCKING = {"sort", "dedup", "aggregate", "join", "sql", "vector-search"}
# code ops: output cardinality can't be known without running them.
_CODE = {"transform", "notebook", "opaque", "section", "loop"}

# a coarse per-column byte width by (display) type — for turning a row count into a working-set size.
_TYPE_W = {
    "int": 8, "integer": 8, "bigint": 8, "long": 8, "smallint": 8, "tinyint": 8, "hugeint": 16,
    "double": 8, "float": 8, "real": 8, "decimal": 8, "number": 8, "numeric": 8,
    "bool": 1, "boolean": 1, "date": 8, "timestamp": 8, "time": 8, "uuid": 16,
    "string": 24, "str": 24, "text": 24, "varchar": 24, "char": 24, "json": 64, "blob": 64, "bytea": 64,
}
_DEFAULT_ROW_BYTES = 64


@dataclass
class SizeEst:
    rows: int | None            # estimated output rows; None = genuinely unknown (don't fabricate)
    bytes: int | None           # estimated output bytes (rows × row width); None when rows unknown
    confidence: str             # "exact" (measured/counted) · "bounded" (an upper bound) · "unknown"
    blocking: bool = False      # this node's OWN op needs ~O(input) memory (drives region placement)


def is_blocking(node_type: str) -> bool:
    return node_type in _BLOCKING


def _row_width(cols) -> int:
    """Bytes/row from a node's (display-typed) column schema, or a default when the schema is unknown."""
    if not cols:
        return _DEFAULT_ROW_BYTES
    total = 0
    for c in cols:
        t = str((c.get("type") if isinstance(c, dict) else getattr(c, "type", "")) or "").strip().lower()
        base = t.split("[")[0].split("(")[0]
        total += _TYPE_W.get(base, 16)
    return max(total, 8)


def _sized(rows: int | None, conf: str, width: int, blocking: bool = False) -> SizeEst:
    return SizeEst(rows=rows, bytes=(rows * width if rows is not None else None), confidence=conf, blocking=blocking)


def estimate_sizes(graph: Graph, resolve_adapter, *, target: str | None = None,
                   schemas: dict | None = None, actuals: dict[str, int | None] | None = None) -> dict[str, SizeEst]:
    """Estimate node output sizes in topological order. `target` restricts the pass to that node's
    upstream cone (bounds how many sources we count); None estimates the whole graph (for the UI hint).
    `schemas` (node_id -> column list, e.g. from executors.schema.schema_for_graph) sharpens the byte
    width; `actuals` (node_id -> measured rows) overrides the estimate for nodes that already ran."""
    if not g.is_acyclic(graph):
        return {}
    schemas = schemas or {}
    actuals = actuals or {}
    out: dict[str, SizeEst] = {}
    order = g.topo_order(graph)
    if target:
        cone = {n.id for n in g.upstream_chain(graph, target)}
        order = [n for n in order if n.id in cone]

    def width(nid: str) -> int:
        return _row_width(schemas.get(nid))

    def inputs(nid: str) -> list[SizeEst]:
        return [out[e.source] for e in g.incoming(graph, nid) if e.source in out]

    for node in order:
        nid = node.id
        t = node.type
        w = width(nid)

        # 1) a measured actual always wins (the canvas is iterative — the 2nd run has ground truth)
        if actuals.get(nid) is not None:
            out[nid] = _sized(int(actuals[nid]), "exact", w, is_blocking(t))
            continue

        ins = inputs(nid)
        first = ins[0] if ins else None

        # 2) a bypassed node passes its input through unchanged; disabled produces nothing downstream
        if node.data.get("bypassed") if isinstance(node.data, dict) else False:
            out[nid] = first or _sized(None, "unknown", w)
            continue

        if t == "source":
            uri = resolve_config(node).get("uri")
            n = None
            if uri:
                try:
                    n = resolve_adapter(uri).count(uri)
                except Exception:  # noqa: BLE001 — uncountable source → unknown, not a fabricated number
                    n = None
            out[nid] = _sized(n, "exact" if n is not None else "unknown", w)
            continue

        if t == "sample":
            k = resolve_config(node).get("n")
            k = int(k) if k is not None else None
            base = first.rows if first else None
            if k is not None:
                rows = min(k, base) if base is not None else k       # a sample is always ≤ n
                out[nid] = _sized(rows, first.confidence if (first and base is not None and base <= k) else "bounded", w)
            else:
                out[nid] = first or _sized(None, "unknown", w)
            continue

        if t in ("filter", "dedup", "assert"):  # row-reducing but unbounded → keep input as an UPPER bound
            base = first.rows if first else None
            conf = "unknown" if not first or first.confidence == "unknown" else "bounded"
            out[nid] = _sized(base, conf, w, is_blocking(t))
            continue

        if t in ("select", "sort", "write", "chart", "window", "fill"):  # row-preserving
            base = first.rows if first else None
            out[nid] = _sized(base, first.confidence if first else "unknown", w, is_blocking(t))
            continue

        if t == "metric":  # collapses to a single value
            out[nid] = _sized(1, "bounded", w)
            continue

        if t in ("aggregate", "join", "sql", "unnest") or t in _CODE or t in ("vector-search",):
            # genuinely unknown output cardinality — never fabricate. blocking per op type (drives placement).
            out[nid] = _sized(None, "unknown", w, is_blocking(t))
            continue

        # plugin / unknown kind: unknown output, treat as non-blocking (streamed) unless it declares otherwise
        out[nid] = _sized(None, "unknown", w, False)

    return out
