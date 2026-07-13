"""Column profiling — per-column stats, over the bounded preview sample OR the whole dataset.

SAMPLED (default): the SAME build as a preview (source scan bounded), computed over the sampled Arrow
table with pyarrow.compute — instant and always available where a preview is, marked `sampled=True`.

FULL (`full=True`): a full pass, like a run — builds the real relation (`full=True`, no source cap) and
computes the stats out-of-core in DuckDB with one aggregate scan: exact count/nulls/min/max/mean over
EVERY row, and an HLL estimate for distinct (`approx_count_distinct`). Marked `sampled=False` so the UI
can say so. This also profiles nodes a sampled profile refuses (aggregate/sql that reduce rows).
"""

from __future__ import annotations

import os
import uuid

import pyarrow as pa
import pyarrow.compute as pc

from hub import db, graph as g
from hub.executors.engine import BuildEngine, NotPreviewable, _dedupe_names
from hub.executors.preview import _CODE_CELL_KINDS, PREVIEW_BUDGET_S, PREVIEW_SCAN
from hub.models import ColumnProfile, Graph, ProfileResult
from hub.storage import ManagedSourceReadError
from hub.plugins.adapters import display_type
from hub.sandbox import run_with_timeout

PROFILE_ROWS = 10000   # cap the rows pulled into Arrow for stats (source scan is already bounded)

try:  # a full profile is a full pass "like a run" — bound it by the SAME deadline knob (P0-EXEC-02)
    PROFILE_FULL_BUDGET_S = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
except ValueError:
    PROFILE_FULL_BUDGET_S = 3600.0


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


def _full_stats(engine: BuildEngine, node_id: str) -> ProfileResult:
    """Whole-dataset stats via one out-of-core DuckDB aggregate scan: exact count/nulls/min/max/mean,
    HLL distinct. Column refs use the view's DuckDB-deduped names (join outputs can share a name)."""
    rel = engine.relation(node_id)
    fields = list(rel.limit(0).to_arrow_table().schema)  # arrow types, positional — no row scan
    display = _dedupe_names([f.name for f in fields])
    con = db.conn()
    v = engine._view(rel, "prof")                        # unique view; DuckDB auto-dedupes its columns
    vcols = con.sql(f"SELECT * FROM {v} LIMIT 0").columns
    sel = ["count(*) AS n_all"]
    plan = []
    for i, f in enumerate(fields):
        t = f.type
        q = '"' + str(vcols[i]).replace('"', '""') + '"'
        nested = pa.types.is_nested(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t)
        numeric = pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t)
        has_mm = not nested and (numeric or pa.types.is_temporal(t) or pa.types.is_string(t)
                                 or pa.types.is_boolean(t))
        sel.append(f"count({q}) AS c{i}_nn")
        if not nested:
            sel.append(f"approx_count_distinct({q}) AS c{i}_d")
        if has_mm:
            sel += [f"min({q}) AS c{i}_mn", f"max({q}) AS c{i}_mx"]
        if numeric:
            sel.append(f"avg({q}) AS c{i}_av")
        plan.append((i, display[i], t, has_mm, numeric))
    r = con.sql(f"SELECT {', '.join(sel)} FROM {v}")
    d = dict(zip(r.columns, r.fetchone()))
    n = int(d["n_all"] or 0)
    cols = []
    for (i, name, t, has_mm, numeric) in plan:
        nn = int(d.get(f"c{i}_nn") or 0)
        prof: dict = {"non_null": nn, "nulls": n - nn}
        if d.get(f"c{i}_d") is not None:
            prof["distinct"] = int(d[f"c{i}_d"])
        if has_mm:
            lo, hi = d.get(f"c{i}_mn"), d.get(f"c{i}_mx")
            prof["min"] = None if lo is None else str(lo)
            prof["max"] = None if hi is None else str(hi)
        if numeric and d.get(f"c{i}_av") is not None:
            prof["mean"] = float(d[f"c{i}_av"])
        cols.append(ColumnProfile(name=name, type=display_type(str(t)), **prof))
    return ProfileResult(columns=cols, row_count=n, sampled=False)


def profile_node(graph: Graph, node_id: str, resolve_adapter, registry,
                 node_builders=None, node_specs=None, cache=None, full: bool = False,
                 storage=None) -> ProfileResult:
    if not g.is_acyclic(graph):
        return ProfileResult(error=True, reason="graph has a cycle — control flow must be encapsulated")
    if node_specs:
        errs = g.type_errors(graph, node_specs)
        if errs:
            return ProfileResult(error=True, reason="incompatible connection: " + "; ".join(errs[:3]))

    from hub import auth
    if auth.auth_enabled() and any(n.type in _CODE_CELL_KINDS for n in g.upstream_chain(graph, node_id)):
        return ProfileResult(not_previewable=True, reason=(
            "profiling a Python cell is disabled in multi-user mode — the in-process timeout can't kill "
            "a runaway cell; run it (runs execute in a killable, deadline-bounded child)"))

    if full:
        # a full-dataset profile is an intentional full pass (like a run) — no short preview budget, but
        # a deadline + interruptible cursor so a huge/runaway pure-SQL aggregate can't pin the warm kernel
        # forever (P0-EXEC-02). Mirrors the sampled path: scope opened on the WORKER thread (thread-local
        # cursor), on_timeout interrupts it. It profiles reducing nodes (aggregate/sql) a sample refuses.
        eng = BuildEngine(graph, resolve_adapter, registry, sample_k=None, full=True,
                          node_builders=node_builders, node_specs=node_specs, warm=cache,
                          warm_scope="full")
        holder: dict = {}

        def work() -> ProfileResult:
            from hub.storage import source_read_scope
            with source_read_scope(
                    storage, g.all_upstream_source_uris(graph, node_id),
                    owner=f"profile:{uuid.uuid4().hex}"):
                with db.run_scope() as scope:
                    holder["scope"] = scope
                    return _full_stats(eng, node_id)

        def on_timeout() -> None:
            sc = holder.get("scope")
            (sc.interrupt() if sc is not None else db.interrupt())

        try:
            return run_with_timeout(work, PROFILE_FULL_BUDGET_S, on_timeout=on_timeout)
        except ManagedSourceReadError as e:
            return ProfileResult(error=True, reason=str(e))
        except NotPreviewable as e:
            return ProfileResult(not_previewable=True, reason=e.reason)
        except Exception as e:  # noqa: BLE001
            return ProfileResult(error=True, reason=f"{type(e).__name__}: {e}")

    engine = BuildEngine(graph, resolve_adapter, registry, sample_k=PREVIEW_SCAN, full=False,
                         node_builders=node_builders, node_specs=node_specs,
                         warm=cache, warm_scope="preview")
    holder: dict = {}

    def work() -> ProfileResult:
        from hub.storage import source_read_scope
        with source_read_scope(
                storage, g.all_upstream_source_uris(graph, node_id),
                owner=f"profile:{uuid.uuid4().hex}"):
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
    except ManagedSourceReadError as e:
        return ProfileResult(error=True, reason=str(e))
    except NotPreviewable as e:
        return ProfileResult(not_previewable=True, reason=e.reason)
    except Exception as e:  # noqa: BLE001
        return ProfileResult(error=True, reason=f"{type(e).__name__}: {e}")
