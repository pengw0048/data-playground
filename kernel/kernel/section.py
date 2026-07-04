"""Section execution — the meta-programming primitive.

A `section` node is a composite node whose implementation is a **driver script** (Python) that
calls the nodes it contains, by alias, with ordinary control flow (for/while/if). It's the
head-pod-script model as a first-class canvas node. Not sample-previewable (full pass only);
loops are bounded by `maxRuns`. See docs/meta-programming.zh.md.

Wire format (node.data.config):
  script:   str                    # Python; API: inputs, params, run, value, concat, emit
  subnodes: [{alias, type, config}]# the nodes this section contains (script calls them by alias)
  params:   dict                   # scalars the script reads (prompts, thresholds, max_iters, …)
  maxRuns:  int                    # hard cap on run() calls (safety; default 200)
"""

from __future__ import annotations

import os

from kernel import db, sandbox
from kernel.models import Graph, GraphNode, Position

Relation = "duckdb.DuckDBPyRelation"


class SectionError(Exception):
    pass


class _Ref:
    """An alias object injected into the script scope so you write run(caption, ...)."""
    def __init__(self, alias: str):
        self.alias = alias


def _materialize(rel):
    """Force-execute a relation to a temp Parquet and return a fresh scan (bounded memory)."""
    from kernel.executors.engine import _spill_root
    d = os.path.join(_spill_root(), "section")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{db.unique_view('sec')}.parquet")
    rel.write_parquet(path)
    return db.conn().read_parquet(path)


def run_section(engine, node, inputs):
    """Execute a section node's driver script; return the emitted relation."""
    from kernel.executors.engine import LoweringEngine

    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    script = (cfg.get("script") or "").strip()
    subnodes = {s["alias"]: s for s in (cfg.get("subnodes") or []) if s.get("alias")}
    params = cfg.get("params") or {}
    max_runs = int(cfg.get("maxRuns", 200))
    if not script:
        raise SectionError("section has no driver script")

    calls = {"n": 0}
    out: dict = {"rel": None}

    def run(ref, **kw):
        calls["n"] += 1
        if calls["n"] > max_runs:
            raise SectionError(f"section exceeded maxRuns={max_runs} (bounded for safety)")
        alias = ref.alias if isinstance(ref, _Ref) else str(ref)
        spec = subnodes.get(alias)
        if not spec:
            raise SectionError(f"section calls unknown node '{alias}'")
        data = kw.pop("data", None)  # a handle to bind as this node's input
        conf = {**(spec.get("config") or {}), **kw}  # remaining kwargs override config (e.g. prompt=…)
        mini = Graph(id="_sec", version=1, edges=[], nodes=[
            GraphNode(id=alias, type=spec["type"], position=Position(x=0, y=0), data={"config": conf})])
        sub = LoweringEngine(mini, engine.resolve_adapter, engine.registry, full=True,
                             node_lowerings=engine.node_lowerings, node_specs=engine.node_specs,
                             bound_inputs={alias: data} if data is not None else None)
        return _materialize(sub.relation(alias))

    def value(handle):
        tbl = handle.limit(1).to_arrow_table()
        if tbl.num_rows == 0:
            return None
        col = "value" if "value" in tbl.column_names else tbl.column_names[-1]
        return tbl.column(col)[0].as_py()

    def concat(handles):
        hs = [h for h in handles if h is not None]
        if not hs:
            raise SectionError("concat() got nothing to concatenate")
        rel = hs[0]
        for h in hs[1:]:
            rel = rel.union(h)
        return _materialize(rel)

    def emit(handle):
        out["rel"] = handle

    ns = sandbox._namespace()
    ns.update({
        "inputs": {"in": inputs[0]} if inputs else {},
        "params": params,
        "run": run, "value": value, "concat": concat, "emit": emit,
    })
    for alias in subnodes:
        ns[alias] = _Ref(alias)  # alias objects: write run(caption, ...)

    sandbox._reject_dunder(script)
    exec(compile(script, "<section>", "exec"), ns)  # noqa: S102 — soft sandbox, full pass only

    if out["rel"] is None:
        raise SectionError("section script did not emit() an output")
    return out["rel"]
