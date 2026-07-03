"""Graph helpers — topological order, upstream chains, cycle detection.

The top-level canvas graph must be acyclic (control flow is encapsulated, PRD §5.7).
"""

from __future__ import annotations

from kernel.models import Graph, GraphEdge, GraphNode


class CycleError(Exception):
    pass


def node_map(graph: Graph) -> dict[str, GraphNode]:
    return {n.id: n for n in graph.nodes}


def incoming(graph: Graph, node_id: str) -> list[GraphEdge]:
    return [e for e in graph.edges if e.target == node_id]


def outgoing(graph: Graph, node_id: str) -> list[GraphEdge]:
    return [e for e in graph.edges if e.source == node_id]


def parents(graph: Graph, node_id: str) -> list[str]:
    return [e.source for e in incoming(graph, node_id)]


def _visit(node_id: str, graph: Graph, state: dict[str, int], order: list[str]) -> None:
    color = state.get(node_id, 0)  # 0=white 1=gray 2=black
    if color == 1:
        raise CycleError(f"cycle through node {node_id}")
    if color == 2:
        return
    state[node_id] = 1
    for p in parents(graph, node_id):
        _visit(p, graph, state, order)
    state[node_id] = 2
    order.append(node_id)


def upstream_chain(graph: Graph, node_id: str) -> list[GraphNode]:
    """Nodes on every path feeding node_id (incl. node_id), in topological order."""
    nm = node_map(graph)
    if node_id not in nm:
        return []
    order: list[str] = []
    state: dict[str, int] = {}
    _visit(node_id, graph, state, order)
    return [nm[nid] for nid in order]


def topo_order(graph: Graph) -> list[GraphNode]:
    nm = node_map(graph)
    order: list[str] = []
    state: dict[str, int] = {}
    for n in graph.nodes:
        _visit(n.id, graph, state, order)
    return [nm[nid] for nid in order]


def is_acyclic(graph: Graph) -> bool:
    try:
        topo_order(graph)
        return True
    except CycleError:
        return False


def nearest_source(graph: Graph, chain: list[GraphNode]) -> GraphNode | None:
    for n in chain:
        if n.type == "source":
            return n
    return None


def type_errors(graph: Graph, node_specs: dict) -> list[str]:
    """Server-side typed-wire validation (defense-in-depth; the frontend also blocks these).

    An edge is valid iff the source port's output wire is accepted by the target input port.
    Returns human-readable errors for incompatible edges (unknown kinds are skipped).
    """
    nm = node_map(graph)
    errors: list[str] = []
    for e in graph.edges:
        src, tgt = nm.get(e.source), nm.get(e.target)
        if not src or not tgt:
            continue
        sspec, tspec = node_specs.get(src.type), node_specs.get(tgt.type)
        if not sspec or not tspec or not sspec.outputs or not tspec.inputs:
            continue
        out = next((p for p in sspec.outputs if p.id == e.source_handle), sspec.outputs[0])
        inp = next((p for p in tspec.inputs if p.id == e.target_handle), tspec.inputs[0])
        accepts = inp.accepts or [inp.wire]
        if out.wire not in accepts:
            errors.append(f"'{src.type}' output ({out.wire}) can't connect to '{tgt.type}' input (accepts {', '.join(accepts)})")
    return errors
