"""Column profiling — per-column stats over the bounded preview sample.

Uses the SAME build as a preview (source scan bounded), so it's instant and always available where a
preview is. Stats are computed over the sampled Arrow table with pyarrow.compute — robust to
duplicate column names (join outputs) and nested types. The result is explicitly `sampled=True`:
these are stats over the previewed rows, not the whole dataset (a full-dataset profile would be a
full pass, like a run).
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from hub import db, graph as g
from hub.executors.engine import BuildEngine, NotPreviewable, _dedupe_names
from hub.executors.preview import PREVIEW_BUDGET_S, PREVIEW_SCAN
from hub.models import ColumnProfile, Graph, ProfileResult
from hub.plugins.adapters import display_type
from hub.sandbox import run_with_timeout

PROFILE_ROWS = 10000   # cap the rows pulled into Arrow for stats (source scan is already bounded)


def _stat(arr: pa.ChunkedArray, n: int, t: pa.DataType) -> dict:
    """null/distinct/min/max/mean for one column, guarding types that don't support each op."""
    non_null = pc.count(arr).as_py()  # counts only valid (non-null) by default
    out: dict = {"non_null": non_null, "nulls": n - non_null}
    if pa.types.is_nested(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return out  # struct/list/map/blob: a count is all that's meaningful here
    try:
        out["distinct"] = pc.count_distinct(arr).as_py()
    except Exception:  # noqa: BLE001
        pass
    numeric = pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t)
    if numeric or pa.types.is_temporal(t) or pa.types.is_string(t) or pa.types.is_boolean(t):
        try:
            mm = pc.min_max(arr)
            lo, hi = mm["min"].as_py(), mm["max"].as_py()
            out["min"] = None if lo is None else str(lo)
            out["max"] = None if hi is None else str(hi)
        except Exception:  # noqa: BLE001
            pass
    if numeric:
        try:
            out["mean"] = pc.mean(arr).as_py()
        except Exception:  # noqa: BLE001
            pass
    return out


def profile_node(graph: Graph, node_id: str, resolve_adapter, registry,
                 node_builders=None, node_specs=None, cache=None) -> ProfileResult:
    if not g.is_acyclic(graph):
        return ProfileResult(error=True, reason="graph has a cycle — control flow must be encapsulated")
    if node_specs:
        errs = g.type_errors(graph, node_specs)
        if errs:
            return ProfileResult(error=True, reason="incompatible connection: " + "; ".join(errs[:3]))

    engine = BuildEngine(graph, resolve_adapter, registry, sample_k=PREVIEW_SCAN, full=False,
                         node_builders=node_builders, node_specs=node_specs,
                         warm=cache, warm_scope="preview")
    holder: dict = {}

    def work() -> ProfileResult:
        with db.run_scope() as scope:
            holder["scope"] = scope
            tbl = engine.relation(node_id).limit(PROFILE_ROWS).to_arrow_table()
            names = _dedupe_names(tbl.column_names)
            if names != tbl.column_names:
                tbl = tbl.rename_columns(names)
            n = tbl.num_rows
            cols = [ColumnProfile(name=f.name, type=display_type(str(f.type)),
                                  **_stat(tbl.column(f.name), n, f.type))
                    for f in tbl.schema]
            return ProfileResult(columns=cols, row_count=n, sampled=True)

    def on_timeout() -> None:
        sc = holder.get("scope")
        (sc.interrupt() if sc is not None else db.interrupt())

    try:
        return run_with_timeout(work, PREVIEW_BUDGET_S, on_timeout=on_timeout)
    except NotPreviewable as e:
        return ProfileResult(not_previewable=True, reason=e.reason)
    except Exception as e:  # noqa: BLE001
        return ProfileResult(error=True, reason=f"{type(e).__name__}: {e}")
