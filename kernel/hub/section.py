"""Section execution — the meta-programming primitive.

A `section` node is a composite node whose implementation is a **driver script** (Python) that
calls the nodes it contains, by alias, with ordinary control flow (for/while/if). It's the
head-pod-script model as a first-class canvas node. Not sample-previewable (full pass only).
See docs/meta-programming.zh.md.

Trust model: the driver script runs with the SAME soft sandbox as the `transform` node — the
kernel executes user-authored Python by design, so this is a footgun guard, not a
security boundary. `maxRuns` caps run() calls (a bounded loop), but a script that never calls
run() (e.g. `while True: pass`) is not time-bounded and, like any node, holds the shared DuckDB
lock for the run; true isolation of untrusted code needs OS-level sandboxing (out of scope for
this internal tool).

Wire format (node.data.config):
  script:   str                    # Python; API: inputs, params, run, value, concat, emit
  params:   dict                   # scalars the script reads (prompts, thresholds, max_iters, …)
  outputs:  [str]                  # declared output port names (default ["out"]); emit()ed by name
  maxRuns:  int                    # cap on run() calls (default 200)
"""

from __future__ import annotations

import os

from hub import db, sandbox
from hub.models import Graph, GraphNode, Position

Relation = "duckdb.DuckDBPyRelation"


class SectionError(Exception):
    pass


class _Ref:
    """An alias object injected into the script scope so you write run(caption, ...)."""
    def __init__(self, alias: str):
        self.alias = alias


def _materialize(engine, rel):
    """Force-execute a relation to a temp Parquet and return a fresh scan (bounded memory).

    The path is registered on the engine's run-scoped spill list so the runner GCs it at
    end-of-run (else a maxRuns loop over a large dataset would leak ~maxRuns parquet files)."""
    from hub.executors.engine import _spill_root
    d = os.path.join(_spill_root(), "section")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{db.unique_view('sec')}.parquet")
    rel.write_parquet(path)
    engine.spill_files.append(path)
    return db.conn().read_parquet(path)


def _collect_children(engine, node) -> dict:
    """The callable nodes of a section, as {alias -> {type, config}}.

    The canvas nodes whose parent_id is this section are its body. The alias is each node's title,
    so the driver calls run("clean rows", …)."""
    kids = [n for n in engine.graph.nodes if getattr(n, "parent_id", None) == node.id]
    out: dict = {}
    for k in kids:
        data = k.data if isinstance(k.data, dict) else {}
        alias = str(data.get("title") or k.id).strip()
        # keep the node id so run() can carry this kid's own contained subtree (nested sections)
        out[alias] = {"alias": alias, "type": k.type, "config": data.get("config", {}) or {}, "id": k.id}
    return out


def _descendants(graph, root_id: str) -> list:
    """Every node contained (transitively) inside root_id, via parent_id — for nested sections."""
    kids_by_parent: dict = {}
    for n in graph.nodes:
        pid = getattr(n, "parent_id", None)
        if pid:
            kids_by_parent.setdefault(pid, []).append(n)
    out, stack, seen = [], list(kids_by_parent.get(root_id, [])), set()
    while stack:
        n = stack.pop()
        if n.id in seen:
            continue
        seen.add(n.id)
        out.append(n)
        stack.extend(kids_by_parent.get(n.id, []))
    return out


def run_section(engine, node, inputs):
    """Execute a section node's driver script; return {output port -> emitted relation}.

    A single-output section emit(rel)s to the default "out" port; a multi-output section
    emit("name", rel)s to named ports that the outer graph wires by source_handle.
    """
    from hub.executors.engine import BuildEngine

    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    script = (cfg.get("script") or "").strip()
    children = _collect_children(engine, node)
    params = cfg.get("params") or {}
    max_runs = int(cfg.get("maxRuns", 200))
    if not script:
        raise SectionError("section has no driver script")

    calls = {"n": 0}
    outs: dict = {}  # output port name -> emitted relation (multi-output; default port is "out")

    def run(ref, **kw):
        calls["n"] += 1
        if calls["n"] > max_runs:
            raise SectionError(f"section exceeded maxRuns={max_runs} (bounded for safety)")
        alias = ref.alias if isinstance(ref, _Ref) else str(ref)
        spec = children.get(alias)
        if not spec:
            raise SectionError(f"section calls unknown node '{alias}'")
        data = kw.pop("data", None)  # a handle to bind as this node's input
        protected = ({"uri"} if spec.get("type") == "source" else
                     {"script"} if spec.get("type") == "section" else set())
        overridden = protected & set(kw)
        if overridden:
            fields = ", ".join(sorted(overridden))
            raise SectionError(
                f"section runtime overrides cannot change protected '{spec['type']}' fields: {fields}")
        conf = {**(spec.get("config") or {}), **kw}  # remaining kwargs override config (e.g. prompt=…)
        nodes = [GraphNode(id=alias, type=spec["type"], position=Position(x=0, y=0), data={"config": conf})]
        # nested sections: carry the aliased node's contained subtree so, when it's itself a section,
        # its own children resolve. Their parent_id points at the ORIGINAL node id (not `alias`), so
        # reparent the direct children onto `alias`; deeper descendants keep their original parent.
        orig = spec.get("id")
        if orig:
            for n in _descendants(engine.graph, orig):
                pid = alias if getattr(n, "parent_id", None) == orig else getattr(n, "parent_id", None)
                nodes.append(GraphNode(id=n.id, type=n.type, position=n.position,
                                       data=(n.data if isinstance(n.data, dict) else {}), parent_id=pid))
        mini = Graph(id="_sec", version=1, edges=[], nodes=nodes)
        sub = BuildEngine(mini, engine.resolve_adapter, engine.registry, full=True,
                             node_builders=engine.node_builders, node_specs=engine.node_specs,
                             bound_inputs={alias: data} if data is not None else None,
                             spill_files=engine.spill_files)  # share spill ownership with the parent run
        return _materialize(engine, sub.relation(alias))

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
        # UNION ALL BY NAME: align by column name (not position) so per-iteration results whose
        # columns are in a different order don't silently misalign; missing columns become NULL.
        views = []
        for h in hs:
            name = db.unique_view("cc")
            h.create_view(name, replace=True)
            views.append(name)
        sql = " UNION ALL BY NAME ".join(f"SELECT * FROM {v}" for v in views)
        return _materialize(engine, db.conn().sql(sql))

    def emit(handle, data=None):
        # emit(rel) -> the default "out" port; emit("name", rel) -> a named output port.
        rel, port = (handle, "out") if data is None else (data, str(handle))
        if not hasattr(rel, "write_parquet"):  # a DuckDB relation; guards emit("out") / emit(None)
            raise SectionError(
                f"emit() expects a relation for port '{port}', got {type(rel).__name__}. "
                "Use emit(rel) for the default output, or emit('port', rel) for a named port.")
        outs[port] = rel

    ns = sandbox._namespace()
    ns.update({
        "inputs": {"in": inputs[0]} if inputs else {},
        "params": params,
        "run": run, "value": value, "concat": concat, "emit": emit,
    })
    for alias in children:
        if alias.isidentifier():  # bare alias object: run(caption, …); other titles use run("my title", …)
            ns[alias] = _Ref(alias)

    sandbox._reject_dunder(script)
    exec(compile(script, "<section>", "exec"), ns)  # noqa: S102 — soft sandbox, full pass only

    if not outs:
        raise SectionError("section script did not emit() an output")
    return outs
