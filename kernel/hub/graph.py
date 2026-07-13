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


def all_upstream_source_uris(graph: Graph, node_id: str) -> list[str]:
    """Every source URI feeding a node, de-duplicated in stable upstream traversal order."""
    uris: list[str] = []
    seen: set[str] = set()
    for node in upstream_chain(graph, node_id):
        if node.type != "source" or not isinstance(node.data, dict):
            continue
        config = node.data.get("config")
        if not isinstance(config, dict):
            continue
        uri = config.get("uri") or config.get("table")
        if uri and uri not in seen:
            seen.add(uri)
            uris.append(uri)
    return uris


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


def _ports(node: GraphNode, spec, side: str) -> list[tuple[str, str, bool]]:
    """Return effective ``(id, wire, multi)`` ports for structural validation.

    Outputs may be declared per node via ``config.outputs`` (sections use this for named emits).
    Inputs are always defined by the NodeSpec. Keeping this resolution here makes structural and
    wire-type validation agree on the exact port an edge addresses.
    """
    if side == "source":
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        declared = cfg.get("outputs") if isinstance(cfg, dict) else None
        if isinstance(declared, list) and declared:
            return [(str(port), "dataset", False) for port in declared]
    if spec is None:  # intrinsic legacy kinds have one implicit dataset input/output
        return [("out" if side == "source" else "in", "dataset", False)]
    if side == "source":
        ports = spec.outputs
    else:
        ports = spec.inputs
    return [(p.id, p.wire, bool(getattr(p, "multi", False))) for p in ports]


def _port(node: GraphNode, spec, handle: str | None, side: str) -> tuple[str, str, bool] | None:
    ports = _ports(node, spec, side)
    if handle is not None:
        found = next((p for p in ports if p[0] == handle), None)
        # A legacy section without config.outputs has an intentionally dynamic output namespace. New
        # UI-created sections declare outputs, which are validated strictly above; preserve old saved
        # canvases that already wire a named emit but predate that declaration.
        if found is None and side == "source" and node.type == "section":
            cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
            if not isinstance(cfg.get("outputs") if isinstance(cfg, dict) else None, list):
                return handle, "dataset", False
        return found
    if not ports:
        return None
    default_id = "out" if side == "source" else "in"
    return next((p for p in ports if p[0] == default_id), ports[0])


def structural_errors(graph: Graph, node_specs: dict, target_node_id: str | None = None) -> list[str]:
    """Return deterministic errors for graph structure that must never be guessed by an executor.

    ``None`` handles select a conventional default port (``out``/``in`` when present, otherwise the
    first declared port). An explicit handle must exist. Unknown kinds are left to the existing
    unknown-kind gate; intrinsic legacy kinds use their implicit single ``in``/``out`` dataset ports.
    """
    errors: list[str] = []
    nodes: dict[str, GraphNode] = {}
    duplicate_nodes: set[str] = set()
    for node in graph.nodes:
        if node.id in nodes:
            if node.id not in duplicate_nodes:
                errors.append(f"duplicate node id '{node.id}'")
                duplicate_nodes.add(node.id)
        else:
            nodes[node.id] = node

    seen_edges: set[str] = set()
    duplicate_edges: set[str] = set()
    for edge in graph.edges:
        if edge.id in seen_edges and edge.id not in duplicate_edges:
            errors.append(f"duplicate edge id '{edge.id}'")
            duplicate_edges.add(edge.id)
        seen_edges.add(edge.id)

    if target_node_id is not None and target_node_id not in nodes:
        errors.append(f"target node '{target_node_id}' does not exist")

    occupied: dict[tuple[str, str], str] = {}
    for edge in graph.edges:
        source, target = nodes.get(edge.source), nodes.get(edge.target)
        if source is None:
            errors.append(f"edge '{edge.id}' references missing source node '{edge.source}'")
        if target is None:
            errors.append(f"edge '{edge.id}' references missing target node '{edge.target}'")
        if source is None or target is None:
            continue

        source_spec = node_specs.get(source.type)
        if source_spec is not None or source.type in INTRINSIC_KINDS:
            output = _port(source, source_spec, edge.source_handle, "source")
            if output is None:
                if edge.source_handle is None:
                    errors.append(f"source node '{source.id}' has no output port")
                else:
                    errors.append(
                        f"edge '{edge.id}' uses unknown source handle '{edge.source_handle}' on node '{source.id}'"
                    )

        target_spec = node_specs.get(target.type)
        if target_spec is None and target.type not in INTRINSIC_KINDS:
            continue
        input_port = _port(target, target_spec, edge.target_handle, "target")
        if input_port is None:
            if edge.target_handle is None:
                errors.append(f"target node '{target.id}' has no input port")
            else:
                errors.append(
                    f"edge '{edge.id}' uses unknown target handle '{edge.target_handle}' on node '{target.id}'"
                )
            continue
        port_id, _, multi = input_port
        key = (target.id, port_id)
        if not multi and key in occupied:
            errors.append(
                f"input '{port_id}' on node '{target.id}' has multiple incoming edges "
                f"('{occupied[key]}' and '{edge.id}')"
            )
        else:
            occupied[key] = edge.id
    return errors


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
        if ((sspec is None and src.type not in INTRINSIC_KINDS)
                or (tspec is None and tgt.type not in INTRINSIC_KINDS)):
            continue
        out = _port(src, sspec, e.source_handle, "source")
        inp = _port(tgt, tspec, e.target_handle, "target")
        if out is None or inp is None:  # structural_errors reports the precise missing/unknown port
            continue
        input_spec = next((p for p in tspec.inputs if p.id == inp[0]), None) if tspec is not None else None
        accepts = (input_spec.accepts or [input_spec.wire]) if input_spec is not None else [inp[1]]
        if out[1] not in accepts:
            errors.append(f"'{src.type}' output ({out[1]}) can't connect to '{tgt.type}' input (accepts {', '.join(accepts)})")
    return errors
