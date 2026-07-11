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

import re
from dataclasses import dataclass

from hub import graph as g
from hub.ir import resolve_config
from hub.models import Graph, GraphNode

# ops whose result needs ~O(input) memory (a hash/sort build) → they set a region's working-set need;
# streaming ops (scan/filter/select/map/sample/limit/write) need ~O(1) and don't.
_BLOCKING = {"sort", "dedup", "aggregate", "join", "sql", "vector-search", "window"}
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
_VEC_RE = re.compile(r"\[(\d+)\]")  # a fixed-size array/vector suffix — e.g. float[1024] (an embedding)
_LIST_ELEMS = 16                    # assumed element count for a variable-length list (no length in the type)
_NESTED_W = 128                     # a struct/map value — coarse, deliberately generous (stay conservative)


def _col_width(t: str) -> int:
    """Byte width for one (display-typed) column, honoring list/vector dimensionality. The plain scalar
    map alone under-counts embeddings catastrophically: a float[1024] scored as base `float`=8B is a
    ~500x undercount, which then mis-sizes a vector working set as 'tiny' and mis-places it local."""
    t = t.strip().lower()
    base = t.split("[")[0].split("(")[0].strip()
    bw = _TYPE_W.get(base, 16)
    m = _VEC_RE.search(t)
    if m:                                       # fixed-size vector/array: N elements of the base type
        return int(m.group(1)) * bw
    if t.endswith("[]") or base in ("list", "array"):  # variable-length list — assume a modest length
        return _LIST_ELEMS * bw
    if base in ("struct", "map"):               # nested value with no flat width
        return _NESTED_W
    return bw


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
    total = sum(_col_width(str((c.get("type") if isinstance(c, dict) else getattr(c, "type", "")) or ""))
                for c in cols)
    return max(total, 8)


def _coltype(c) -> str:
    return str((c.get("type") if isinstance(c, dict) else getattr(c, "type", "")) or "")


def _colname(c) -> str:
    return str((c.get("name") if isinstance(c, dict) else getattr(c, "name", "")) or "")


_LISTLEN_CACHE: dict[tuple[str, str, str], int] = {}


def _source_width(resolve_adapter, uri: str, cols) -> int:
    """Bytes/row for a SOURCE — like _row_width, but for a variable-length list column (`float[]`) it
    PROBES the real average element count from a bounded sample instead of assuming _LIST_ELEMS. Parquet
    stores a fixed-size embedding as a variable list (the dimension is lost on disk), so a 4096-wide
    embedding otherwise scores 16*w and the byte confirm-gate misses the multi-GB table it targets. The
    byte gate takes the max over the cone, so getting the source right is what makes the gate fire. Memoized."""
    if not cols:
        return _DEFAULT_ROW_BYTES
    total = 0
    for c in cols:
        t = _coltype(c).strip().lower()
        base = t[:-2].strip() if t.endswith("[]") else ("list" if t.split("[")[0].split("(")[0] in ("list", "array") else None)
        if base is not None and not _VEC_RE.search(t):  # a variable list with no known dimension → probe it
            n = _probed_list_len(resolve_adapter, uri, _colname(c))
            bw = _TYPE_W.get(base.split("[")[0].split("(")[0], 16)
            total += (n if n is not None else _LIST_ELEMS) * bw
        else:
            total += _col_width(t)
    return max(total, 8)


def _probed_list_len(resolve_adapter, uri: str, col: str) -> int | None:
    """Average element count of a LIST column over a bounded sample (DuckDB length()), memoized by the
    adapter fingerprint. None on any failure → the caller falls back to the flat _LIST_ELEMS assumption."""
    if not col:
        return None
    try:
        adapter = resolve_adapter(uri)
        fp = adapter.fingerprint(uri)
    except Exception:  # noqa: BLE001
        return None
    key = (uri, fp, col)
    if key in _LISTLEN_CACHE:
        return _LISTLEN_CACHE[key]
    try:
        rel = adapter.scan(uri, columns=[col], limit=1024)
        v = rel.aggregate(f'avg(length("{col.replace(chr(34), chr(34) * 2)}"))').fetchone()[0]
        n = int(round(v)) if v is not None else None
    except Exception:  # noqa: BLE001 — uncountable / not a list here → fall back
        return None
    if n is not None:
        if len(_LISTLEN_CACHE) >= _COUNT_CACHE_MAX:
            _LISTLEN_CACHE.pop(next(iter(_LISTLEN_CACHE)), None)
        _LISTLEN_CACHE[key] = n
    return n


_COUNT_CACHE: dict[tuple[str, str], int] = {}
_COUNT_CACHE_MAX = 256


def _counted(resolve_adapter, uri: str) -> int | None:
    """adapter.count(uri), memoized by the adapter's fingerprint (size+mtime for a local file), so a
    CSV/JSON source — whose count is a full parse — isn't re-scanned on every keystroke-triggered
    estimate. Only real counts are cached (a transient failure retries); a changed file gets a new
    fingerprint and recounts. Object-store uris can't be stat'd, so their count is keyed by the uri and
    effectively cached for the process — acceptable for a hint (re-scanning an object every edit is worse)."""
    try:
        adapter = resolve_adapter(uri)
        fp = adapter.fingerprint(uri)
    except Exception:  # noqa: BLE001 — no adapter / can't fingerprint → count uncached (best-effort)
        try:
            return resolve_adapter(uri).count(uri)
        except Exception:  # noqa: BLE001
            return None
    key = (uri, fp)
    if key in _COUNT_CACHE:
        return _COUNT_CACHE[key]
    try:
        n = adapter.count(uri)
    except Exception:  # noqa: BLE001 — uncountable source → unknown, not a fabricated number
        return None
    if n is not None:
        if len(_COUNT_CACHE) >= _COUNT_CACHE_MAX:
            _COUNT_CACHE.pop(next(iter(_COUNT_CACHE)), None)
        _COUNT_CACHE[key] = n
    return n


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

    widths: dict[str, int] = {}  # per-node per-row byte width, propagated from the MEASURED source width

    def width(nid: str) -> int:
        return _row_width(schemas.get(nid))

    def in_width(nid: str) -> int:  # widest input's per-row width (0 if no sized input yet)
        return max((widths[e.source] for e in g.incoming(graph, nid) if e.source in widths), default=0)

    def inputs(nid: str) -> list[SizeEst]:
        return [out[e.source] for e in g.incoming(graph, nid) if e.source in out]

    for node in order:
        nid = node.id
        t = node.type
        w = width(nid)  # coarse display-derived width; SHARPENED per node type just below
        uri = resolve_config(node).get("uri") if t == "source" else None
        bypassed = node.data.get("bypassed") if isinstance(node.data, dict) else False

        # a row-preserving/reducing op keeps its columns' widths — PROPAGATE the input's measured width
        # (max, conservative: never under-estimate) rather than re-derive from coarse display types, which
        # lose vector dims / decimal / nested widths. So a measured embedding width survives downstream to
        # the blocking op that actually sets a region's working set. (select stays here on PURPOSE: its
        # post-projection display width would UNDER-count a KEPT probed vector column whose display type
        # lost its dim, so we err wide — a full fix needs per-column width lineage. union: same-schema
        # concat → the widest input width is correct.)
        pass_through = t in ("filter", "dedup", "assert", "select", "sort", "write", "chart",
                             "window", "fill", "sample", "union")
        # MEASURE the per-row width ONCE, here — EVERY branch below (a measured actual, a source that
        # already ran, a pass-through) reuses it, so the sharpened width is never dropped to a coarse
        # default and always survives to the downstream blocking op / the byte gate / placement.
        if bypassed:                                        # passes its input through unchanged
            w = in_width(nid) or w
        elif t == "source" and uri:
            # MEASURE list/vector-column widths (embeddings) from the real schema so the byte gate sees the
            # true per-row size — a float[1024] scored as base `float`=8B mis-sizes a region ~1000x small.
            w = _source_width(resolve_adapter, uri, schemas.get(nid))
        elif pass_through:
            w = max(w, in_width(nid))
        widths[nid] = w

        # 1) a measured actual always wins (the canvas is iterative — the 2nd run has ground truth)
        if actuals.get(nid) is not None:
            out[nid] = _sized(int(actuals[nid]), "exact", w, is_blocking(t))
            continue

        ins = inputs(nid)
        first = ins[0] if ins else None

        # 2) a bypassed node passes its input through; disabled produces nothing downstream
        if bypassed:
            out[nid] = first or _sized(None, "unknown", w)
            continue

        if t == "source":
            n = _counted(resolve_adapter, uri) if uri else None
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
