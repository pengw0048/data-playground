"""Per-node OUTPUT schema, computed metadata-only (no row scan) — powers the typed ports + column
suggestions in the editor. A node whose columns can't be known without running Python (a transform
cell, a section, vector-search, or any plugin op) is an UNTYPED port: null until it actually runs —
UNLESS the user pins a schema contract on it (config.outputSchema), which types it (and, via a typed
stand-in relation, everything downstream) without running.

This is the "typed vs untyped port" split — relational ops (source/filter/select/sort/sample/dedup/
join/sql/aggregate/metric) carry a schema DuckDB resolves lazily; code ops don't, unless declared.
"""

from __future__ import annotations

import uuid

from hub import db, graph as g
from hub.executors.engine import BuildEngine, _bypassed, _disabled, declared_schema
from hub.models import Graph
from hub.plugins.adapters import relation_columns

# kinds whose output columns require EXECUTING code (Python / a real query) → untyped port when undeclared
# pivot's output columns are the DISTINCT DATA VALUES of the pivot column — unknowable without a full
# pass, and the lazy limit=0 schema probe would report only the group keys (a misleading subset) — so
# treat it as untyped (null port) rather than authoritative.
_UNTYPED = {"transform", "section", "vector-search", "pivot"}


def _norm_col(c) -> dict:
    """A declared column (dict on the wire, or a model) → the ColumnSchema wire shape the frontend reads."""
    if isinstance(c, dict):
        return {"name": str(c.get("name", "")), "type": str(c.get("type", "")),
                "capabilities": list(c.get("capabilities") or [])}
    return {"name": str(getattr(c, "name", "")), "type": str(getattr(c, "type", "")),
            "capabilities": list(getattr(c, "capabilities", []) or [])}


def schema_for_graph(graph: Graph, resolve_adapter, registry,
                     node_builders=None, node_specs=None, storage=None) -> dict[str, list | None]:
    if not g.is_acyclic(graph):
        return {}
    untyped = _UNTYPED | set(node_builders or {})  # plugin kinds are untyped too (they execute)

    def blocks(c) -> bool:
        """This node can't be typed cheaply: a code op WITHOUT a declared contract, or a disabled node.
        A declared code op is NOT blocking — the engine stands in a typed relation for it (schema_only)."""
        return (c.type in untyped and declared_schema(c) is None) or _disabled(c)

    # schema_only → sources scan with limit=0 (metadata only, no materialization even for eager
    # adapters like Lance), so this whole pass is cheap. We do NOT wrap it in a timeout: a timeout
    # abandons a worker thread that still holds the shared DuckDB lock, wedging every later query.
    engine = BuildEngine(graph, resolve_adapter, registry, sample_k=None, full=True,
                            node_builders=node_builders, node_specs=node_specs, schema_only=True)
    out: dict[str, list | None] = {}
    # run on our own cursor (scope exit drops the views it minted); doesn't block concurrent runs
    from hub.storage import source_read_scope
    with source_read_scope(
            storage, g.execution_source_uris(graph, None),
            owner=f"schema:{uuid.uuid4().hex}"):
        with db.run_scope():
            for n in graph.nodes:
                if node_specs and n.type in node_specs:
                    try:
                        ports = g.effective_output_ports_for_node(n, node_specs[n.type])
                    except ValueError:
                        out[n.id] = None
                        continue
                    if len(ports) > 1:
                        # The schema response is node-keyed, not port-keyed. Until #266 introduces
                        # durable per-port schema identity, returning one declaration for a named-output
                        # node would silently assign it to every port.
                        out[n.id] = None
                        continue
                # a declared code op: its OWN port is the declared contract, verbatim (exact user types) —
                # unless disabled (emits nothing) or bypassed (passes input through), where the declaration
                # doesn't apply; those fall through to the chain check / engine passthrough below.
                if n.type in untyped and declared_schema(n) is not None and not _disabled(n) and not _bypassed(n):
                    out[n.id] = [_norm_col(c) for c in declared_schema(n)]
                    continue
                chain = g.upstream_chain(graph, n.id)  # incl. n, in topo order
                # a blocking op anywhere upstream → this port isn't typeable cheaply
                if any(blocks(c) for c in chain):
                    out[n.id] = None
                    continue
                try:
                    rel = engine.relation(n.id)  # lazy — .columns/.types are metadata only (declared code ops stand in)
                    out[n.id] = [c.model_dump(by_alias=True) for c in relation_columns(rel)]
                except Exception:  # noqa: BLE001 — unwired / bad config → treat as unknown
                    out[n.id] = None
    return out


def schema_for_graph_ports(graph: Graph, resolve_adapter, registry,
                           node_builders=None, node_specs=None, storage=None) -> dict[str, dict[str, list | None]]:
    """Per-*port* metadata schemas for the public graph-inspection endpoint.

    ``schema_for_graph`` remains node-keyed because placement and join analysis operate on
    single-output relations.  Inspection is different: collapsing named outputs would silently
    attribute one port's columns to another.  Each declared port therefore gets an explicit
    entry, including ``None`` when metadata cannot honestly establish its schema.
    """
    if not g.is_acyclic(graph):
        return {}
    untyped = _UNTYPED | set(node_builders or {})

    def blocks(c) -> bool:
        return (c.type in untyped and declared_schema(c) is None) or _disabled(c)

    engine = BuildEngine(graph, resolve_adapter, registry, sample_k=None, full=True,
                         node_builders=node_builders, node_specs=node_specs, schema_only=True)
    out: dict[str, dict[str, list | None]] = {}
    from hub.storage import source_read_scope
    with source_read_scope(
            storage, g.execution_source_uris(graph, None),
            owner=f"schema:{uuid.uuid4().hex}"):
        with db.run_scope():
            for n in graph.nodes:
                try:
                    ports = (g.effective_output_ports_for_node(n, node_specs[n.type])
                             if node_specs else [g.EffectiveOutputPort("out", None, "dataset")])
                except (KeyError, ValueError):
                    out[n.id] = {}
                    continue
                values: dict[str, list | None] = {}
                chain = g.upstream_chain(graph, n.id)
                for port in ports:
                    if n.type in untyped and declared_schema(n) is not None and not _disabled(n) and not _bypassed(n):
                        # A node-wide declaration describes exactly one effective output.  Do not
                        # project it onto sibling named ports.
                        values[port.id] = [_norm_col(c) for c in declared_schema(n)] if len(ports) == 1 else None
                    elif any(blocks(c) for c in chain):
                        values[port.id] = None
                    else:
                        try:
                            rel = engine.relation(n.id, port.id)
                            values[port.id] = [c.model_dump(by_alias=True) for c in relation_columns(rel)]
                        except Exception:  # noqa: BLE001 — unwired / bad config is unknown, never guessed
                            values[port.id] = None
                out[n.id] = values
    return out
