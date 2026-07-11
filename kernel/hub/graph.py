"""Graph helpers — topological order, upstream chains, cycle detection.

The top-level canvas graph must be acyclic (control flow is encapsulated).
"""

from __future__ import annotations

from hub.models import Graph, GraphEdge, GraphNode, Position


class CycleError(Exception):
    pass


def layout(graph: Graph, x0: float = 80.0, y0: float = 80.0, dx: float = 280.0, dy: float = 170.0) -> None:
    """Assign positions to a fresh graph's nodes by left-to-right topological layering — column = the
    longest path from a root, rows stack within a column. Mutates positions in place. Used to lay out an
    IMPORTED pipeline so its nodes don't all stack at (0,0). Iterative (no recursion, so a long chain can't
    overflow the stack); a cyclic graph degrades to a single column rather than raising."""
    ids = {n.id for n in graph.nodes}
    depth: dict[str, int] = {}
    try:
        ordered = topo_order(graph)  # parents precede children → each node's parents are already scored
    except CycleError:
        ordered = graph.nodes  # malformed (shouldn't happen post-import); lay out in list order, all col 0
    for n in ordered:
        ps = [e.source for e in incoming(graph, n.id) if e.source in ids and e.source != n.id]
        depth[n.id] = 1 + max((depth.get(p, 0) for p in ps), default=-1)
    per_col: dict[int, int] = {}
    for n in graph.nodes:
        c = depth.get(n.id, 0)
        r = per_col.get(c, 0)
        per_col[c] = r + 1
        n.position = Position(x=x0 + c * dx, y=y0 + r * dy)


def node_map(graph: Graph) -> dict[str, GraphNode]:
    return {n.id: n for n in graph.nodes}


def resolve_source_refs(graph: Graph, resolve) -> None:
    """Rewrite each `source` node's uri IN PLACE via `resolve` (a name/id → uri function, e.g.
    catalog.resolve_ref) so a source can name a catalog table instead of only a path/uri. A no-op for
    real paths/uris and unknown tokens. Called at the graph-consuming API entry points, so the engine,
    compiler, and estimator all see resolved uris; the persisted canvas doc is untouched."""
    for n in graph.nodes:
        if n.type != "source" or not isinstance(n.data, dict):
            continue
        cfg = n.data.get("config")
        if isinstance(cfg, dict) and (cfg.get("uri") or cfg.get("table")):
            cfg["uri"] = resolve(cfg.get("uri") or cfg.get("table"))


def incoming(graph: Graph, node_id: str) -> list[GraphEdge]:
    return [e for e in graph.edges if e.target == node_id]


def outgoing(graph: Graph, node_id: str) -> list[GraphEdge]:
    return [e for e in graph.edges if e.source == node_id]


def parents(graph: Graph, node_id: str) -> list[str]:
    return [e.source for e in incoming(graph, node_id)]


def _visit(node_id: str, graph: Graph, state: dict[str, int], order: list[str]) -> None:
    # iterative post-order DFS (0=white 1=gray 2=black) — no recursion, so a long node chain
    # can't blow Python's recursion limit.
    stack: list[tuple[str, bool]] = [(node_id, False)]
    while stack:
        nid, done = stack.pop()
        if done:
            if state.get(nid, 0) != 2:
                state[nid] = 2
                order.append(nid)
            continue
        if state.get(nid, 0) != 0:  # already gray/black — skip (handles diamonds)
            continue
        state[nid] = 1
        stack.append((nid, True))   # finalize after all ancestors
        for p in parents(graph, nid):
            pc = state.get(p, 0)
            if pc == 1:
                raise CycleError(f"cycle through node {p}")
            if pc == 0:
                stack.append((p, False))


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


# Node types the engine executes natively even though they carry no NodeSpec (legacy / intrinsic).
# Any type outside node_specs ∪ node_builders ∪ these is unknown — a missing plugin or a typo — and
# must fail closed rather than silently pass its input through (which would omit the intended work
# yet report success). See executors/engine.py's catch-all and routers/runs._reject_unknown_kinds.
INTRINSIC_KINDS = frozenset({"notebook", "loop", "variable", "opaque"})


def unknown_kinds(graph: Graph, known) -> list[tuple[str, str]]:
    """[(node_id, type), ...] for nodes whose type is neither a registered kind nor an intrinsic one."""
    allow = set(known) | INTRINSIC_KINDS
    return [(n.id, n.type) for n in graph.nodes if n.type not in allow]


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
