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
from hub.models import Graph, dataset_ref_identity

# ops whose result needs ~O(input) memory (a hash/sort build) → they set a region's working-set need;
# streaming ops (scan/filter/select/map/sample/limit/write) need ~O(1) and don't.
_BLOCKING = {"sort", "dedup", "aggregate", "join", "sql", "vector-search", "window", "pivot"}
# opaque code containers: output cardinality can't be known without running them. Transform row modes
# are handled separately below because some have a cardinality contract independent of their code.
_CODE = {"section"}

# A coarse per-column byte width by display type. Variable binary values are deliberately absent:
# their payloads can vary from bytes to megabytes, so no fixed estimate is defensible.
_TYPE_W = {
    "int": 8, "integer": 8, "bigint": 8, "long": 8, "smallint": 8, "tinyint": 8, "hugeint": 16,
    "double": 8, "float": 8, "real": 8, "decimal": 8, "number": 8, "numeric": 8,
    "bool": 1, "boolean": 1, "date": 8, "timestamp": 8, "time": 8, "uuid": 16,
    "string": 24, "str": 24, "text": 24, "varchar": 24, "char": 24, "json": 64,
}
_DEFAULT_ROW_BYTES = 64
_VEC_RE = re.compile(r"\[(\d+)\]")  # a fixed-size array/vector suffix — e.g. float[1024] (an embedding)
_FIXED_BINARY_RE = re.compile(r"fixed_size_binary\s*\[\s*(\d+)\s*\]", re.IGNORECASE)
_LIST_ELEMS = 16                    # assumed element count for a variable-length list (no length in the type)
_NESTED_W = 128                     # a struct/map value — coarse, deliberately generous (stay conservative)


def _col_width(t: str, physical_type: str | None = None) -> int | None:
    """Byte width for one (display-typed) column, honoring list/vector dimensionality. The plain scalar
    map alone under-counts embeddings catastrophically: a float[1024] scored as base `float`=8B is a
    ~500x undercount, which then mis-sizes a vector working set as 'tiny' and mis-places it local."""
    t = t.strip().lower()
    base = t.split("[")[0].split("(")[0].strip()
    if base == "bytes":
        fixed = _FIXED_BINARY_RE.fullmatch(str(physical_type or "").strip())
        return int(fixed.group(1)) if fixed else None
    bw = _TYPE_W.get(base, 16)
    m = _VEC_RE.search(t)
    if m:                                       # fixed-size vector/array: N elements of the base type
        return int(m.group(1)) * bw
    if t.endswith("[]"):
        return _LIST_ELEMS * bw
    if base in ("list", "array"):                # type-erased list — retain the existing coarse estimate
        return _LIST_ELEMS * 16
    if base in ("struct", "map"):               # nested value with no flat width
        return _NESTED_W
    return bw


@dataclass
class SizeEst:
    rows: int | None            # estimated output rows; None = genuinely unknown (don't fabricate)
    bytes: int | None           # estimated output bytes (rows × row width); None when rows unknown
    confidence: str             # "exact" (measured/counted) · "bounded" (an upper bound) · "unknown"
    blocking: bool = False      # this node's OWN op needs ~O(input) memory (drives region placement)
    uncertainty: str | None = None
    may_expand: bool = False    # unknown output may exceed every known upstream population


@dataclass(frozen=True)
class WidthEst:
    bytes_per_row: int | None
    uncertainty: str | None = None


def is_blocking(node_type: str) -> bool:
    return node_type in _BLOCKING


def _physical_type(c) -> str | None:
    value = c.get("physicalType", c.get("physical_type")) if isinstance(c, dict) \
        else getattr(c, "physical_type", None)
    return str(value) if value is not None else None


def _estimated_row_width(cols) -> WidthEst:
    """Bytes/row from schema evidence; missing schemas retain the established coarse fallback."""
    if not cols:
        return WidthEst(_DEFAULT_ROW_BYTES)
    total = 0
    for column in cols:
        logical = _coltype(column)
        width = _col_width(logical, _physical_type(column))
        if width is None:
            name = _colname(column) or "(unnamed)"
            return WidthEst(
                None,
                f'Binary column "{name}" has no fixed-width byte-size evidence; '
                "Data Playground did not scan values to guess.",
            )
        total += width
    return WidthEst(max(total, 8))


def _row_width(cols) -> int:
    """Numeric runtime batching width; unknown columns retain the historical 16-byte fallback."""
    if not cols:
        return _DEFAULT_ROW_BYTES
    total = sum(
        _col_width(_coltype(column), _physical_type(column)) or 16
        for column in cols
    )
    return max(total, 8)


def _coltype(c) -> str:
    return str((c.get("type") if isinstance(c, dict) else getattr(c, "type", "")) or "")


def _colname(c) -> str:
    return str((c.get("name") if isinstance(c, dict) else getattr(c, "name", "")) or "")


_LISTLEN_CACHE: dict[tuple[str, str, str], int] = {}


def _source_width(resolve_adapter, uri: str, cols) -> WidthEst:
    """Bytes/row for a SOURCE — like _row_width, but for a variable-length list column (`float[]`) it
    PROBES the real average element count from a bounded sample instead of assuming _LIST_ELEMS. Parquet
    stores a fixed-size embedding as a variable list (the dimension is lost on disk), so a 4096-wide
    embedding otherwise scores 16*w and the byte confirm-gate misses the multi-GB table it targets. The
    byte gate takes the max over the cone, so getting the source right is what makes the gate fire. Memoized."""
    if not cols:
        return WidthEst(_DEFAULT_ROW_BYTES)
    total = 0
    for c in cols:
        t = _coltype(c).strip().lower()
        base = t[:-2].strip() if t.endswith("[]") else ("list" if t.split("[")[0].split("(")[0] in ("list", "array") else None)
        if base is not None and not _VEC_RE.search(t):  # a variable list with no known dimension → probe it
            n = _probed_list_len(resolve_adapter, uri, _colname(c))
            element_type = base.split("[")[0].split("(")[0]
            bw = None if element_type == "bytes" else _TYPE_W.get(element_type, 16)
            if bw is None:
                return _estimated_row_width(cols)
            total += (n if n is not None else _LIST_ELEMS) * bw
        else:
            width = _col_width(t, _physical_type(c))
            if width is None:
                return _estimated_row_width(cols)
            total += width
    return WidthEst(max(total, 8))


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
        preview_scan = getattr(adapter, "preview_scan", None)
        if not callable(preview_scan):
            return None
        rel = preview_scan(uri, columns=[col], limit=1024)
        v = rel.aggregate(f'avg(length("{col.replace(chr(34), chr(34) * 2)}"))').fetchone()[0]
        n = int(round(v)) if v is not None else None
    except Exception:  # noqa: BLE001 — uncountable / not a list here → fall back
        return None
    if n is not None:
        if len(_LISTLEN_CACHE) >= _COUNT_CACHE_MAX:
            _LISTLEN_CACHE.pop(next(iter(_LISTLEN_CACHE)), None)
        _LISTLEN_CACHE[key] = n
    return n


_COUNT_CACHE: dict[tuple[str, ...], int] = {}
_COUNT_CACHE_MAX = 256


def _counted(
        resolve_adapter, uri: str, revision_id: str | None = None, *,
        dataset_id: str | None = None,
) -> int | None:
    """Read a bounded count when the adapter explicitly provides the matching capability.

    Preflight must never call the ordinary `count`, which may parse/scan an entire CSV, JSON, remote
    table, or plugin source before the user has admitted a cancellable full job. An exact revision
    never falls back to current-head metadata.
    """
    try:
        adapter = resolve_adapter(uri)
        if revision_id is not None:
            revision_detail = getattr(adapter, "revision_detail", None)
            if not callable(revision_detail):
                return None
            key = ("revision", uri, str(dataset_id or ""), revision_id)
        else:
            metadata_count = getattr(adapter, "metadata_count", None)
            if not callable(metadata_count):
                return None
            key = ("head", uri, str(adapter.fingerprint(uri)))
    except Exception:  # noqa: BLE001 — unknown metadata is safer than a fallback scan
        return None
    if key in _COUNT_CACHE:
        return _COUNT_CACHE[key]
    try:
        if revision_id is not None:
            detail = revision_detail(uri, revision_id, preview_limit=1)
            if (not isinstance(detail, dict)
                    or str(detail.get("revision_id") or "") != revision_id):
                return None
            value = detail.get("row_count")
            n = int(value) if value is not None and int(value) >= 0 else None
        else:
            n = metadata_count(uri)
    except Exception:  # noqa: BLE001 — uncountable source → unknown, not a fabricated number
        return None
    if n is not None:
        if len(_COUNT_CACHE) >= _COUNT_CACHE_MAX:
            _COUNT_CACHE.pop(next(iter(_COUNT_CACHE)), None)
        _COUNT_CACHE[key] = n
    return n


def _registered_source_dataset_id(uri: str) -> str | None:
    """Return the dataset authority already bound to one logical Source URI."""
    from hub import metadb, workspace_providers

    try:
        provider_id = workspace_providers.provider_dataset_identity(uri)
    except Exception:  # malformed/unavailable provider identity cannot authorize an exact claim
        return None
    if isinstance(provider_id, str) and provider_id:
        return provider_id
    try:
        binding = metadb.catalog_revision_binding_for_uri(uri)
    except Exception:  # noqa: BLE001 — missing metadata leaves exact identity unproven
        return None
    dataset_id = binding.get("dataset_id") if isinstance(binding, dict) else None
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def _source_revision_identity(
        config: dict, uri: str,
) -> tuple[str | None, str | None, bool]:
    """Return ``(dataset_id, revision_id, pinned)`` without resolving a mutable head.

    ``pinned`` remains true for an invalid/unresolved DatasetRef so callers fail closed instead of
    treating exact intent as permission to consult ``metadata_count``.
    """
    if "datasetRef" in config:
        dataset_ref = config.get("datasetRef")
        if not isinstance(dataset_ref, dict):
            return None, None, True
        try:
            dataset_id, revision_id = dataset_ref_identity(dataset_ref)
        except ValueError:
            return None, None, True
        if _registered_source_dataset_id(uri) != dataset_id:
            return dataset_id, None, True
        return dataset_id, revision_id, True
    revision_id = config.get("_input_revision_id")
    if isinstance(revision_id, str) and revision_id:
        dataset_id = config.get("_input_dataset_id")
        if (isinstance(dataset_id, str) and dataset_id
                and _registered_source_dataset_id(uri) != dataset_id):
            return dataset_id, None, True
        return (
            dataset_id if isinstance(dataset_id, str) and dataset_id else None,
            revision_id,
            True,
        )
    return None, None, False


def _transform_semantics(node, resolve_processor) -> tuple[str | None, str]:
    """Return execution-owned row semantics, resolving exact Library versions when necessary."""
    config = resolve_config(node)
    mode = config.get("mode", "map")
    if config.get("source") == "library":
        processor = config.get("processor")
        version = config.get("version")
        if (not callable(resolve_processor) or not isinstance(processor, str) or not processor
                or not isinstance(version, str) or not version):
            return None, str(config.get("onError", "raise"))
        try:
            mode = getattr(resolve_processor(processor, version), "mode", None)
        except Exception:  # noqa: BLE001 — unavailable exact code has unknown semantics
            return None, str(config.get("onError", "raise"))
    return (
        str(mode) if isinstance(mode, str) and mode else None,
        str(config.get("onError", "raise")),
    )


def _sized(
        rows: int | None, conf: str, width: WidthEst, blocking: bool = False, *,
        may_expand: bool = False,
) -> SizeEst:
    byts = rows * width.bytes_per_row \
        if rows is not None and width.bytes_per_row is not None else None
    return SizeEst(
        rows=rows, bytes=byts, confidence=conf, blocking=blocking,
        uncertainty=width.uncertainty if rows is not None and byts is None else None,
        may_expand=may_expand,
    )


def estimate_sizes(graph: Graph, resolve_adapter, *, target: str | None = None,
                   schemas: dict | None = None, actuals: dict[str, int | None] | None = None,
                   storage=None,
                   no_row_probe_source_ids: set[str] | None = None,
                   resolve_processor=None) -> dict[str, SizeEst]:
    """Fence managed sources for the entire fingerprint/count estimation pass."""
    import uuid
    from hub.storage import source_read_scope

    with source_read_scope(
            storage, g.execution_source_uris(graph, target),
            owner=f"estimate:{uuid.uuid4().hex}"):
        return _estimate_sizes_unfenced(
            graph, resolve_adapter, target=target, schemas=schemas, actuals=actuals,
            no_row_probe_source_ids=no_row_probe_source_ids,
            resolve_processor=resolve_processor)


def _estimate_sizes_unfenced(graph: Graph, resolve_adapter, *, target: str | None = None,
                             schemas: dict | None = None,
                             actuals: dict[str, int | None] | None = None,
                             no_row_probe_source_ids: set[str] | None = None,
                             resolve_processor=None,
                             ) -> dict[str, SizeEst]:
    """Estimate node output sizes in topological order. `target` restricts the pass to that node's
    upstream cone (bounds how many sources we count); None estimates the whole graph (for the UI hint).
    `schemas` (node_id -> column list, e.g. from executors.schema.schema_for_graph) sharpens the byte
    width; `actuals` (node_id -> measured rows) overrides the estimate for nodes that already ran."""
    if not g.is_acyclic(graph):
        return {}
    schemas = schemas or {}
    actuals = actuals or {}
    no_row_probe_source_ids = no_row_probe_source_ids or set()
    out: dict[str, SizeEst] = {}
    order = g.topo_order(graph)
    if target:
        cone = {n.id for n in g.upstream_chain(graph, target)}
        order = [n for n in order if n.id in cone]

    widths: dict[str, WidthEst] = {}  # per-node width, including explicit unknown-width evidence

    def width(nid: str) -> WidthEst:
        return _estimated_row_width(schemas.get(nid))

    def in_width(nid: str) -> WidthEst | None:
        candidates = [widths[e.source] for e in g.incoming(graph, nid) if e.source in widths]
        if not candidates:
            return None
        unknown = next((item for item in candidates if item.bytes_per_row is None), None)
        if unknown is not None:
            return unknown
        return max(candidates, key=lambda item: item.bytes_per_row or 0)

    def inputs(nid: str) -> list[SizeEst]:
        return [out[e.source] for e in g.incoming(graph, nid) if e.source in out]

    for node in order:
        nid = node.id
        t = node.type
        w = width(nid)  # coarse display-derived width; SHARPENED per node type just below
        uri = resolve_config(node).get("uri") if t == "source" else None
        bypassed = node.data.get("bypassed") if isinstance(node.data, dict) else False
        source_config: dict = {}
        source_dataset_id: str | None = None
        source_revision_id: str | None = None
        source_pinned = False
        if t == "source":
            raw_config = node.data.get("config", {}) if isinstance(node.data, dict) else {}
            source_config = raw_config if isinstance(raw_config, dict) else {}
            source_dataset_id, source_revision_id, source_pinned = (
                _source_revision_identity(source_config, str(uri or "")))

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
            # true per-row size — but never combine a pinned revision's rows with a mutable-head value probe.
            if nid in no_row_probe_source_ids:
                columns = schemas.get(nid)
                w = (
                    _estimated_row_width(columns)
                    if columns
                    else WidthEst(
                        None,
                        "Prepared input column width is unknown because pre-admission "
                        "sizing did not read source rows.",
                    )
                )
            elif source_pinned:
                # Exact schema evidence may still carry a fixed vector dimension. A variable list has
                # only the established coarse width until an exact-revision width SPI exists.
                w = _estimated_row_width(schemas.get(nid))
            else:
                w = _source_width(resolve_adapter, uri, schemas.get(nid))
        elif pass_through:
            upstream = in_width(nid)
            if upstream is not None:
                if upstream.bytes_per_row is None or w.bytes_per_row is None:
                    w = upstream if upstream.bytes_per_row is None else w
                elif upstream.bytes_per_row > w.bytes_per_row:
                    w = upstream
        widths[nid] = w

        transform_semantics = (
            _transform_semantics(node, resolve_processor)
            if t == "transform" and not bypassed
            else None
        )
        transform_actual_is_reusable = (
            t != "transform" or transform_semantics == ("map", "raise")
        )

        # 1) a measured actual always wins (the canvas is iterative — the 2nd run has ground truth)
        # except for transforms without a strict 1:1 contract. A prior filter/map-skip output is not
        # an upper bound for the next run, and variable fan-out must retain the unknown-population gate.
        if actuals.get(nid) is not None and transform_actual_is_reusable:
            out[nid] = _sized(int(actuals[nid]), "exact", w, is_blocking(t))
            continue

        ins = inputs(nid)
        first = ins[0] if ins else None

        # 2) a bypassed node passes its input through; disabled produces nothing downstream
        if bypassed:
            out[nid] = first or _sized(None, "unknown", w)
            continue

        if t == "source":
            protected = nid in no_row_probe_source_ids
            if not uri:
                n = None
            elif protected:
                # ``metadata_count`` is an explicit bounded current-head capability and is safe
                # before admission freezes that head. Once a revision is pinned, using the
                # current count would describe the wrong input; revision_detail is not a metadata-
                # only contract because it may read preview rows.
                n = (
                    _counted(resolve_adapter, uri)
                    if not source_pinned
                    else None
                )
            elif source_pinned and source_revision_id is None:
                n = None
            else:
                n = _counted(
                    resolve_adapter, uri,
                    source_revision_id,
                    dataset_id=source_dataset_id,
                )
            size = _sized(n, "exact" if n is not None else "unknown", w)
            if protected and n is None:
                size.uncertainty = (
                    "Prepared input may require an unrestricted full read, but no bounded "
                    "metadata row count is available."
                )
            out[nid] = size
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

        if t == "union":
            # A UNION ALL consumes every input row, so using only the first branch understates both the
            # scan and the output. UNION DISTINCT can only reduce that sum, making the same value a safe
            # upper bound. If any branch is unknown, a partial sum is not a complete admission signal.
            if not ins or any(item.rows is None or item.confidence == "unknown" for item in ins):
                out[nid] = _sized(None, "unknown", w)
            else:
                rows = sum(int(item.rows) for item in ins if item.rows is not None)
                mode = str(resolve_config(node).get("mode") or "all").lower()
                exact = (len(ins) == 1 or mode == "all") and all(
                    item.confidence == "exact" for item in ins
                )
                out[nid] = _sized(rows, "exact" if exact else "bounded", w)
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

        if t == "transform":
            mode, on_error = transform_semantics or (None, "raise")
            base = first.rows if first else None
            if mode == "map" and on_error == "raise":
                # The executor emits exactly one value per input row or fails the whole step.
                out[nid] = _sized(
                    base, first.confidence if first else "unknown", w)
            elif mode == "map" and on_error == "skip":
                # A failed input row is omitted; every successful input still emits exactly one row.
                confidence = (
                    "unknown" if not first or first.confidence == "unknown" else "bounded")
                out[nid] = _sized(base, confidence, w)
            elif mode == "filter":
                # Predicate false and skip-on-error can only remove rows.
                confidence = (
                    "unknown" if not first or first.confidence == "unknown" else "bounded")
                out[nid] = _sized(base, confidence, w)
            else:
                # flat-map and batch transforms may emit any number of rows. Unavailable exact
                # Library descriptors are equally opaque; never trust the client-copied mode.
                out[nid] = _sized(None, "unknown", w, may_expand=True)
            continue

        if t == "metric":  # collapses to a single value
            out[nid] = _sized(1, "bounded", w)
            continue

        if t in ("aggregate", "join", "sql", "unnest", "pivot") or t in _CODE or t in ("vector-search",):
            # genuinely unknown output cardinality — never fabricate. blocking per op type (drives placement).
            out[nid] = _sized(None, "unknown", w, is_blocking(t))
            continue

        # plugin / unknown kind: unknown output, treat as non-blocking (streamed) unless it declares otherwise
        out[nid] = _sized(None, "unknown", w, False)

    return out
