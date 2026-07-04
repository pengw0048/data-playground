"""Per-node OUTPUT schema, computed metadata-only (no row scan) — powers column suggestions in
the editor (typed ports). A node whose columns can't be known without running Python (a transform
cell, a section, vector-search, or any plugin op) is an UNTYPED port: null until it actually runs.

This is the "typed vs untyped port" split — relational ops (source/filter/select/sort/sample/
dedup/join/sql/aggregate/metric) carry a schema DuckDB can resolve lazily; code ops don't.
"""

from __future__ import annotations

from kernel import db, graph as g
from kernel.executors.engine import LoweringEngine, _disabled
from kernel.models import Graph
from kernel.plugins.adapters import relation_columns
from kernel.sandbox import run_with_timeout

# kinds whose output columns require EXECUTING code (Python / a real query) → untyped port
_UNTYPED = {"transform", "notebook", "section", "vector-search", "loop", "opaque"}
_BUDGET_S = 5.0  # metadata-only, so this is generous; a bound guards a pathological schema sniff


def schema_for_graph(graph: Graph, resolve_adapter, registry,
                     node_lowerings=None, node_specs=None) -> dict[str, list | None]:
    if not g.is_acyclic(graph):
        return {}
    untyped = _UNTYPED | set(node_lowerings or {})  # plugin kinds are untyped too (they execute)
    # schema_only → sources scan with limit=0 (no materialization, even for eager adapters like Lance)
    engine = LoweringEngine(graph, resolve_adapter, registry, sample_k=None, full=True,
                            node_lowerings=node_lowerings, node_specs=node_specs, schema_only=True)

    def work() -> dict[str, list | None]:
        out: dict[str, list | None] = {}
        with db.lock():
            try:
                for n in graph.nodes:
                    chain = g.upstream_chain(graph, n.id)  # incl. n, in topo order
                    # a code op / disabled node anywhere upstream → this port isn't typeable cheaply
                    if any(c.type in untyped or _disabled(c) for c in chain):
                        out[n.id] = None
                        continue
                    try:
                        rel = engine.relation(n.id)  # lazy relation — .columns/.types are metadata only
                        out[n.id] = [c.model_dump(by_alias=True) for c in relation_columns(rel)]
                    except Exception:  # noqa: BLE001 — unwired / bad config → treat as unknown
                        out[n.id] = None
            finally:
                db.drop_created_views()
        return out

    try:
        return run_with_timeout(work, _BUDGET_S)
    except Exception:  # noqa: BLE001 — timeout or hard failure → no suggestions rather than a stall
        return {}
