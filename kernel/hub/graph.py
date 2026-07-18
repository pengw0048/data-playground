"""Graph helpers — topological order, upstream chains, cycle detection.

The top-level canvas graph must be acyclic (control flow is encapsulated).
"""

from __future__ import annotations

import math

from dataclasses import dataclass

from hub.models import Graph, GraphEdge, GraphNode, Position, dataset_ref_identity


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


_MAX_EXECUTION_NODES = 10_000
MAX_EFFECTIVE_OUTPUT_PORTS = 64


@dataclass(frozen=True)
class EffectiveOutputPort:
    """The declaration-order identity used by execution, inspection, and durable run history."""

    id: str
    label: str | None
    wire: str


def effective_output_ports_for_node(
        node: GraphNode, spec) -> list[EffectiveOutputPort]:
    """Validate one already-resolved node/spec pair without another graph scan."""
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    has_dynamic_outputs = (
        node.type == "section" and isinstance(cfg, dict) and "outputs" in cfg)
    declared = cfg.get("outputs") if has_dynamic_outputs else None
    if has_dynamic_outputs:
        if not isinstance(declared, list):
            raise ValueError(f"node '{node.id}' output declaration must be a list")
        ports: list[EffectiveOutputPort] = []
        for port in declared:
            if not isinstance(port, str):
                raise ValueError(f"node '{node.id}' output port ids must be strings")
            if port != port.strip():
                raise ValueError(f"node '{node.id}' output port '{port}' contains surrounding whitespace")
            if not port:
                raise ValueError(f"node '{node.id}' declares an empty output port id")
            if len(port) > 128:
                raise ValueError(f"node '{node.id}' output port '{port[:32]}…' exceeds 128 characters")
            ports.append(EffectiveOutputPort(id=port, label=port, wire="dataset"))
    else:
        ports = []
        for port in spec.outputs:
            port_id = str(port.id)
            label = port.label
            if not port_id or port_id != port_id.strip() or len(port_id) > 128:
                raise ValueError(f"node '{node.id}' declares an invalid output port id")
            if label is not None and len(label) > 256:
                raise ValueError(f"node '{node.id}' output port '{port_id}' label exceeds 256 characters")
            ports.append(EffectiveOutputPort(id=port_id, label=label, wire=str(port.wire)))
    if not ports:
        raise ValueError(f"node '{node.id}' must declare at least one output port")
    if len(ports) > MAX_EFFECTIVE_OUTPUT_PORTS:
        raise ValueError(
            f"node '{node.id}' declares {len(ports)} output ports; "
            f"the supported maximum is {MAX_EFFECTIVE_OUTPUT_PORTS}")
    ids = [port.id for port in ports]
    duplicate = next((port_id for index, port_id in enumerate(ids)
                      if port_id in ids[:index]), None)
    if duplicate is not None:
        raise ValueError(f"node '{node.id}' declares duplicate output port '{duplicate}'")
    return ports


def effective_output_ports(
        graph: Graph, node_id: str, node_specs: dict) -> list[EffectiveOutputPort]:
    """Resolve one bounded, declaration-ordered output set; never use runtime dict ordering."""
    node = node_map(graph).get(node_id)
    if node is None:
        raise ValueError(f"target node '{node_id}' does not exist")
    spec = node_specs.get(node.type)
    if spec is None:
        raise ValueError(f"node '{node_id}' has unknown kind '{node.type}'")
    return effective_output_ports_for_node(node, spec)


def require_output_port(
        graph: Graph, node_id: str, node_specs: dict, port_id: str | None) -> EffectiveOutputPort:
    """Select one relation port without guessing for a multi-output node."""
    ports = effective_output_ports(graph, node_id, node_specs)
    if port_id is None:
        if len(ports) != 1:
            raise ValueError(f"node '{node_id}' requires an explicit output port")
        return ports[0]
    found = next((port for port in ports if port.id == port_id), None)
    if found is None:
        raise KeyError(f"node '{node_id}' has no output port '{port_id}'")
    return found


def execution_nodes(graph: Graph, roots: list[GraphNode]) -> list[GraphNode]:
    """Nodes a selected execution cone can actually reach, including nested Section bodies.

    A section executes its ``parent_id`` children rather than ordinary graph edges. Bound the
    iterative walk so malformed containment cannot turn dispatch or source fingerprinting into an
    unbounded traversal. This is the shared traversal for every private execution sidecar.
    """
    children: dict[str, list[GraphNode]] = {}
    for node in graph.nodes:
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node)

    queue = list(roots)
    seen: set[str] = set()
    result: list[GraphNode] = []
    cursor = 0
    while cursor < len(queue):
        if cursor >= _MAX_EXECUTION_NODES:
            raise RuntimeError("section execution traversal exceeds the supported node limit")
        node = queue[cursor]
        cursor += 1
        if not isinstance(node, GraphNode) or node.id in seen:
            continue
        seen.add(node.id)
        result.append(node)
        if node.type == "section":
            queue.extend(children.get(node.id, []))
    return result


def _execution_source_bindings(
        graph: Graph, roots: list[GraphNode]) -> list[tuple[GraphNode, dict]]:
    """Source configs from the shared execution traversal, including nested Section bodies."""
    bindings: list[tuple[GraphNode, dict]] = []
    for node in execution_nodes(graph, roots):
        data = node.data if isinstance(node.data, dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        if node.type == "source":
            bindings.append((node, config))
    return bindings


def _execution_source_configs(graph: Graph, roots: list[GraphNode]) -> list[dict]:
    return [config for _node, config in _execution_source_bindings(graph, roots)]


def resolve_source_refs(graph: Graph, resolve) -> None:
    """Rewrite every executable source binding IN PLACE through the catalog resolver.

    This includes section-contained children. Without that shared traversal the engine could execute
    an unresolved token while ownership guards protected a different (or no) URI.
    """
    for node, cfg in _execution_source_bindings(graph, list(graph.nodes)):
        value = cfg.get("uri")
        if value:
            cfg["uri"] = resolve(value)
        # A pinned/as-of core revision is read from its immutable artifact rather than the mutable
        # catalog head. Carry that server-resolved physical identity privately so every preview,
        # inspection, and execution read scope fences the same file the adapter will actually open.
        # Admission-bound graphs already carry an authoritative private binding and must retain it.
        if str(node.id) in graph._input_artifact_uris:
            continue
        dataset_ref = cfg.get("datasetRef")
        if not isinstance(dataset_ref, dict):
            continue
        try:
            dataset_id, revision_id = dataset_ref_identity(dataset_ref)
        except ValueError:
            continue
        from hub import metadb
        artifact_uri = metadb.managed_local_file_revision_artifact(dataset_id, revision_id)
        if artifact_uri is not None:
            graph._input_artifact_uris[str(node.id)] = artifact_uri


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
    """Every executable source URI feeding a node, including section-contained sources."""
    uris: list[str] = []
    seen: set[str] = set()
    exact_artifacts = getattr(graph, "_input_artifact_uris", {})
    for node, config in _execution_source_bindings(graph, upstream_chain(graph, node_id)):
        uri = exact_artifacts.get(node.id) or config.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            uris.append(uri)
    return uris


def all_upstream_publication_uris(graph: Graph, node_id: str) -> list[str]:
    """Original source URIs to record when publishing a derived catalog output.

    Region execution replaces a cut with a physical ref-source. The ref is the URI the runner must
    read and lease, but catalog lineage must retain the original sources the unsplit graph would have
    recorded. Only the controller-owned private sidecar can override a synthetic source; client graph
    data is never trusted as provenance.
    """
    overrides = getattr(graph, "_publication_source_uris", {})
    uris: list[str] = []
    seen: set[str] = set()
    for node in upstream_chain(graph, node_id):
        if node.type != "source" or not isinstance(node.data, dict):
            continue
        configured = overrides.get(node.id)
        if configured is None:
            config = node.data.get("config")
            if not isinstance(config, dict):
                continue
            uri = config.get("uri")
            configured = (uri,) if uri else ()
        for uri in configured:
            if uri and uri not in seen:
                seen.add(uri)
                uris.append(uri)
    return uris


def execution_source_uris(graph: Graph, target_node_id: str | None) -> list[str]:
    """Exact, stable source URI set for the execution cone selected by ``target_node_id``."""
    if target_node_id is not None:
        return all_upstream_source_uris(graph, target_node_id)
    uris: list[str] = []
    seen: set[str] = set()
    exact_artifacts = getattr(graph, "_input_artifact_uris", {})
    for node, config in _execution_source_bindings(graph, list(graph.nodes)):
        uri = exact_artifacts.get(node.id) or config.get("uri")
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


def unknown_kinds(graph: Graph, known) -> list[tuple[str, str]]:
    """[(node_id, type), ...] for nodes whose type is not currently registered."""
    allow = set(known)
    return [(n.id, n.type) for n in graph.nodes if n.type not in allow]


def validation_error(
        graph: Graph, node_specs: dict, known_kinds=(),
        target_node_id: str | None = None) -> tuple[str, bool] | None:
    """One authoritative, side-effect-free graph validity decision for every ingress.

    Returns ``(message, acyclic)`` so compile can keep its error-plan response while execution,
    importers, and graph-building tools enforce exactly the same structural contract.
    """
    unknown = unknown_kinds(graph, set(node_specs) | set(known_kinds))
    if unknown:
        node_id, kind = unknown[0]
        return (
            f"unknown node kind '{kind}' (node '{node_id}') — "
            "install its plugin or remove the node",
            True,
        )
    structural = structural_errors(graph, node_specs, target_node_id)
    if structural:
        return "invalid graph: " + "; ".join(structural[:5]), True
    parameter = parameter_errors(graph, node_specs)
    if parameter:
        return "invalid graph: " + "; ".join(parameter[:5]), True
    incompatible = type_errors(graph, node_specs)
    if incompatible:
        return "incompatible connection: " + "; ".join(incompatible[:5]), True
    if not is_acyclic(graph):
        return "graph has a cycle — control flow must be encapsulated (§5.7)", False
    return None


def parameter_errors(graph: Graph, node_specs: dict) -> list[str]:
    """Validate descriptor-defined typed config without inventing plugin semantics.

    ``columns`` is an ordered list of field names, never a comma-delimited convenience string.
    Numeric descriptors accept only their declared JSON number shape; individual plugins remain
    responsible for semantic bounds such as positive-only values.
    """
    errors: list[str] = []
    for node in graph.nodes:
        spec = node_specs.get(node.type)
        if spec is None:
            continue
        config = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        if not isinstance(config, dict):
            continue
        for param in getattr(spec, "params", []):
            param_type = getattr(param, "type", None)
            if param.name not in config or config[param.name] is None:
                if (param_type in ("int", "float") and getattr(param, "required", False)
                        and getattr(param, "default", None) is None):
                    errors.append(f"node '{node.id}' parameter '{param.name}' is required")
                continue
            value = config[param.name]
            if param_type == "int" and (type(value) is not int or not -(2**53 - 1) <= value <= 2**53 - 1):
                errors.append(
                    f"node '{node.id}' parameter '{param.name}' must be a complete safe integer")
            elif param_type == "float" and not _finite_json_number(value):
                errors.append(f"node '{node.id}' parameter '{param.name}' must be a finite number")
            elif param_type == "columns" and (not isinstance(value, list)
                    or any(not isinstance(column, str) or not column.strip() for column in value)):
                errors.append(
                    f"node '{node.id}' parameter '{param.name}' must be an ordered list of column names")
    return errors


def _finite_json_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _ports(node: GraphNode, spec, side: str) -> list[tuple[str, str, bool]]:
    """Return effective ``(id, wire, multi)`` ports for structural validation.

    Outputs use the same validated resolver as execution and durable history. Inputs remain static
    NodeSpec declarations. Invalid output declarations return no selectable port; the structural pass
    reports the resolver's precise validation error separately.
    """
    if spec is None:  # unknown kinds are reported by the unknown-kind gate, never given implicit ports
        return []
    if side == "source":
        try:
            return [(port.id, port.wire, False)
                    for port in effective_output_ports_for_node(node, spec)]
        except ValueError:
            return []
    return [(p.id, p.wire, bool(getattr(p, "multi", False))) for p in spec.inputs]


def _port(node: GraphNode, spec, handle: str | None, side: str) -> tuple[str, str, bool] | None:
    ports = _ports(node, spec, side)
    if handle is not None:
        return next((p for p in ports if p[0] == handle), None)
    if not ports:
        return None
    default_id = "out" if side == "source" else "in"
    return next((p for p in ports if p[0] == default_id), ports[0])


def structural_errors(graph: Graph, node_specs: dict, target_node_id: str | None = None) -> list[str]:
    """Return deterministic errors for graph structure that must never be guessed by an executor.

    A missing target handle selects its conventional primary input; a missing source handle is valid
    only for a single-output node. An explicit handle must exist. Unknown kinds are left to the
    unknown-kind gate; registered plugin kinds use the ports from their required NodeSpec.
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

    effective_outputs: dict[str, list[EffectiveOutputPort]] = {}
    for node in nodes.values():
        if node.type not in node_specs:
            continue
        try:
            effective_outputs[node.id] = effective_output_ports_for_node(
                node, node_specs[node.type])
        except ValueError as exc:
            errors.append(str(exc))

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
        if source_spec is not None:
            if edge.source_handle is None and len(effective_outputs.get(source.id, [])) > 1:
                errors.append(
                    f"edge '{edge.id}' must identify a source handle on multi-output node "
                    f"'{source.id}'")
            output = _port(source, source_spec, edge.source_handle, "source")
            if output is None:
                if edge.source_handle is None:
                    errors.append(f"source node '{source.id}' has no output port")
                else:
                    errors.append(
                        f"edge '{edge.id}' uses unknown source handle '{edge.source_handle}' on node '{source.id}'"
                    )

        target_spec = node_specs.get(target.type)
        if target_spec is None:
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
        if sspec is None or tspec is None:
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
