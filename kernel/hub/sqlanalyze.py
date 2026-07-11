"""AST-based SQL analysis for the faithful-preview and distributed-execution gates.

We parse with DuckDB's OWN parser — `json_serialize_sql`, the same front-end that EXECUTES these
queries — so detection matches execution exactly and correctly handles what a regex cannot: a column
named `input2` (vs the input CTE `input2`), a quoted window name `OVER "w"`, the keyword `except` used
as a quoted identifier, keywords inside string literals or comments, and arbitrarily nested subqueries.

A non-empty query that does not parse errs toward the SAFE answer (treat it as needing a full pass /
reducing rows / order-sensitive) — it will fail at execution anyway, so a conservative answer never
produces a silent lie. An empty query is a no-op (all False).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

import duckdb

# DuckDB aggregate function names — used to tell a row-REDUCING aggregate from a scalar function. A
# WINDOWED aggregate parses to a distinct AST node (class WINDOW), never a FUNCTION node, so it is
# correctly NOT counted here (a window preserves rows).
_AGG_FNS = frozenset({
    "count", "count_star", "sum", "fsum", "kahan_sum", "sumkahan", "avg", "mean", "min", "max",
    "min_by", "max_by", "arg_min", "arg_max", "median", "mode", "product", "any_value", "arbitrary",
    "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp", "bool_and", "bool_or",
    "bit_and", "bit_or", "bit_xor", "approx_count_distinct", "approx_quantile", "quantile",
    "quantile_cont", "quantile_disc", "reservoir_quantile", "list", "array_agg", "string_agg",
    "group_concat", "listagg", "geomean", "corr", "covar_pop", "covar_samp", "entropy", "kurtosis",
    "skewness", "bitstring_agg", "histogram", "first", "last", "sum_no_overflow",
})

# aggregates whose per-group value depends on intra-group ROW ORDER, which a hash-shuffle does NOT
# preserve — so a distributed GROUP BY using one diverges from single-node DuckDB. We reject by NAME
# (conservative): DuckDB rewrites an ORDER-BY'd `list(x ORDER BY x)` into `list_sort(list(x))`, so the
# intra-aggregate ORDER BY is not reliably visible in the AST; over-rejecting the ordered form to
# single-node is safe (correctness preserved), and the common divergent forms (first/any_value/arg_max)
# are never determinizable anyway.
_ORDER_SENSITIVE_AGG = frozenset({
    "list", "array_agg", "string_agg", "group_concat", "listagg", "first", "last",
    "any_value", "arbitrary", "arg_min", "arg_max", "min_by", "max_by",
})

_INPUT_CTE = re.compile(r"^input\d*$", re.I)  # the sql node's input CTE names: input, input2, input3, …


@lru_cache(maxsize=512)
def _parse(sql: str):
    """DuckDB's parsed AST for `sql` as a dict (its `statements`), or None if it does not parse. Uses a
    throwaway in-memory connection — a PURE parse, no tables resolved, no data touched. Cached by query
    text (previews of the same node repeat). Callers handle empty before calling."""
    try:
        con = duckdb.connect()
        try:
            row = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
        finally:
            con.close()
        doc = json.loads(row[0]) if row and row[0] else None
        return doc if (doc and not doc.get("error")) else None
    except Exception:  # noqa: BLE001 — unavailable/unparseable → callers choose the safe (conservative) default
        return None


def _nodes(ast):
    """Yield every dict node in the AST, recursively (iterative to avoid deep recursion)."""
    stack = [ast]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            yield o
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)


def reduces_rows(q: str) -> bool:
    """Does this SQL aggregate / reduce rows, so a 2000-row sampled preview would present a PARTIAL
    result as complete? True for GROUP BY / HAVING / SELECT DISTINCT / a non-windowed aggregate (incl.
    one in a subquery or scalar position, which a sample would still compute partially). Conservative:
    over-flags (→ full pass), never under-flags."""
    if not (q or "").strip():
        return False
    ast = _parse(q)
    if ast is None:
        return True  # non-empty but unparseable → conservative (it will error at execution regardless)
    for n in _nodes(ast):
        if n.get("type") == "SELECT_NODE":
            if n.get("group_expressions") or n.get("group_sets") or n.get("having") is not None:
                return True
            if any(isinstance(m, dict) and m.get("type") == "DISTINCT_MODIFIER" for m in (n.get("modifiers") or [])):
                return True
        if n.get("class") == "FUNCTION" and (n.get("function_name") or "").lower() in _AGG_FNS:
            return True  # a non-windowed aggregate (a windowed one is a WINDOW node, not FUNCTION)
    return False


def needs_full_input(q: str) -> bool:
    """True if this SQL is row-preserving but reads its input NON-LOCALLY, so a per-input 2000-row sample
    would lie even though it does not reduce rows — it must run over the FULL inputs in preview (the
    display is then bounded by the preview LIMIT), like the join/window nodes:
      - a JOIN — explicit, a comma-join, a subquery-join, or a self-join — detected as >=2 references to
        the input CTEs (input/input2/…); a truncated prefix rarely matches;
      - a set operation (INTERSECT/EXCEPT/UNION) — matches/removes rows across its selects;
      - a window function (`OVER (…)`, the named `OVER "w"`, or QUALIFY) — computed within the sample;
      - an ORDER BY (± LIMIT) — a sorted / top-N result ordered within the 2000-row prefix would show
        entirely different rows than the globally-ordered full run (the dedicated sort node runs full in
        preview for exactly this reason). A windowed / intra-aggregate ORDER BY is NOT a SELECT modifier,
        so it does not trip this.
    A column merely NAMED `input2` is a COLUMN_REF, not a BASE_TABLE, so it is correctly not counted.
    Conservative: over-flags (→ full pass), never under-flags (a silently-wrong sample is the ARC1
    faithful-preview violation this guards)."""
    if not (q or "").strip():
        return False
    ast = _parse(q)
    if ast is None:
        return True  # non-empty but unparseable → conservative
    input_refs = 0
    for n in _nodes(ast):
        cls, typ = n.get("class"), n.get("type")
        if cls == "WINDOW":
            return True
        if typ == "SET_OPERATION_NODE":
            return True
        if typ == "SELECT_NODE":
            if n.get("qualify") is not None:
                return True
            if any(isinstance(m, dict) and m.get("type") == "ORDER_MODIFIER" for m in (n.get("modifiers") or [])):
                return True  # a statement/subquery ORDER BY → sorted/top-N result, unfaithful on a prefix
        if typ == "BASE_TABLE" and _INPUT_CTE.match(str(n.get("table_name") or "")):
            input_refs += 1
            if input_refs >= 2:
                return True
    return False


def agg_has_order_sensitive(select_list: str) -> bool:
    """True if the aggregate select-list (an aggregate node's `aggs` config — e.g. `list(x) AS a,
    count(*) AS n`) uses an order-sensitive aggregate (list/string_agg/first/any_value/arg_max/…), whose
    distributed result would diverge from single-node DuckDB. Wrapped as a SELECT to parse. Unparseable
    or empty → True (safe: over-reject to the single-node engine)."""
    if not (select_list or "").strip():
        return True
    ast = _parse(f"SELECT {select_list} FROM _dp_agg_probe")
    if ast is None:
        return True
    return any(n.get("class") == "FUNCTION" and (n.get("function_name") or "").lower() in _ORDER_SENSITIVE_AGG
               for n in _nodes(ast))
