"""Resume one local linear run from an admitted exact retained boundary.

Replaces the proven prefix with one internal managed-result Source for compile/dispatch while the
original semantic graph and execution manifest remain the durable user intent. Fan-in and
multi-boundary composition are out of scope for the first vertical.
"""

from __future__ import annotations

import hashlib
import json

from hub import compiler
from hub import graph as graph_mod
from hub.models import (
    Graph,
    GraphEdge,
    GraphEdgeData,
    GraphNode,
    PerNodeStatus,
    Position,
    ReusableExecutionBoundary,
)


class BoundarySuffixError(RuntimeError):
    """Post-admission boundary resume cannot proceed safely."""


def _boundary_internal_id(kind: str, *identity: object) -> str:
    payload = json.dumps([kind, *identity], ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"__dp_boundary_{kind}_{digest}"


def _reserve_internal_id(base: str, occupied: set[str]) -> str:
    candidate = base
    suffix = 1
    while candidate in occupied:
        candidate = f"{base}_{suffix}"
        suffix += 1
    occupied.add(candidate)
    return candidate


def prefix_node_ids(graph: Graph, boundary_node_id: str) -> frozenset[str]:
    """Nodes on every path feeding the boundary, including the boundary itself."""
    return frozenset(node.id for node in graph_mod.upstream_chain(graph, boundary_node_id))


def build_suffix_graph(
        graph: Graph,
        *,
        boundary_node_id: str,
        boundary_port_id: str,
        artifact_uri: str,
        target_node_id: str,
) -> tuple[Graph, str, frozenset[str]]:
    """Cut the linear cone at the boundary and return (suffix_graph, ref_id, prefix_ids)."""
    cone = graph.model_copy(deep=True)
    # Restrict to the target cone so unrelated branches cannot enter the suffix.
    incoming: dict[str, list] = {}
    for edge in cone.edges:
        incoming.setdefault(edge.target, []).append(edge)
    selected = {target_node_id}
    queue = [target_node_id]
    cursor = 0
    while cursor < len(queue):
        current = queue[cursor]
        cursor += 1
        for edge in incoming.get(current, []):
            if edge.source not in selected:
                selected.add(edge.source)
                queue.append(edge.source)
    cone.nodes = [node for node in cone.nodes if node.id in selected]
    cone.edges = [
        edge for edge in cone.edges
        if edge.source in selected and edge.target in selected
    ]

    prefix = prefix_node_ids(cone, boundary_node_id)
    if boundary_node_id not in prefix:
        raise BoundarySuffixError("boundary node is outside the target cone")
    if target_node_id in prefix and target_node_id != boundary_node_id:
        raise BoundarySuffixError("target cannot be upstream of its reusable boundary")
    if target_node_id == boundary_node_id:
        raise BoundarySuffixError("a reusable boundary cannot resume its own target")

    suffix_nodes = [node for node in cone.nodes if node.id not in prefix]
    if not suffix_nodes:
        raise BoundarySuffixError("boundary resume requires at least one downstream step")

    occupied = {node.id for node in graph.nodes} | {edge.id for edge in graph.edges}
    ref_id = _reserve_internal_id(
        _boundary_internal_id("ref", boundary_node_id, boundary_port_id), occupied)
    nodes = [
        GraphNode(
            id=ref_id,
            type="source",
            position=Position(x=0, y=0),
            data={"config": {"uri": artifact_uri}},
        ),
        *suffix_nodes,
    ]
    edges: list[GraphEdge] = []
    for edge in cone.edges:
        if edge.source in prefix and edge.target not in prefix:
            if edge.source != boundary_node_id:
                raise BoundarySuffixError("linear boundary cut saw a non-boundary prefix edge")
            if edge.source_handle not in (None, boundary_port_id, "out"):
                raise BoundarySuffixError("boundary output port does not match the cut edge")
            edge_id = _reserve_internal_id(
                _boundary_internal_id(
                    "edge", edge.id, edge.source, edge.source_handle, edge.target,
                    edge.target_handle),
                occupied,
            )
            edges.append(GraphEdge(
                id=edge_id,
                source=ref_id,
                target=edge.target,
                source_handle=None,
                target_handle=edge.target_handle,
                data=GraphEdgeData(),
            ))
        elif edge.source not in prefix and edge.target not in prefix:
            edges.append(edge)

    suffix = Graph(
        id=getattr(graph, "id", None) or "_boundary_suffix",
        version=getattr(graph, "version", 1) or 1,
        nodes=nodes,
        edges=edges,
        requirements=list(getattr(graph, "requirements", None) or []),
        parameters=list(getattr(graph, "parameters", None) or []),
    )
    publication_uris = tuple(
        graph_mod.all_upstream_publication_uris(graph, boundary_node_id))
    suffix._publication_source_uris = {ref_id: publication_uris}
    suffix._controller_generated_source_ids = {ref_id}
    suffix._input_artifact_uris = {ref_id: artifact_uri}
    suffix._execution_manifest_sha256 = getattr(graph, "_execution_manifest_sha256", None)
    suffix._execution_manifest_doc = getattr(graph, "_execution_manifest_doc", None)
    suffix._parameter_bindings = list(getattr(graph, "_parameter_bindings", None) or [])
    suffix._publication_run_id = getattr(graph, "_publication_run_id", None)
    suffix._publication_attempt_id = getattr(graph, "_publication_attempt_id", None)
    suffix._publication_producer_id = (
        getattr(graph, "_publication_producer_id", None) or getattr(graph, "id", None))
    producer_version = getattr(graph, "_publication_producer_version", None)
    suffix._publication_producer_version = (
        graph.version if producer_version is None else producer_version)
    return suffix, ref_id, prefix


def reused_prefix_statuses(
        intent_graph: Graph,
        *,
        prefix_ids: frozenset[str],
        boundary_node_id: str,
        deps,
) -> list[PerNodeStatus]:
    """Jobs evidence for omitted prefix nodes — done + reused, never freshly timed."""
    full_plan = compiler.compile_plan(
        intent_graph, boundary_node_id, deps.registry, deps.node_specs, deps.node_ir)
    by_id = {step.node_id: step for step in full_plan.steps}
    statuses: list[PerNodeStatus] = []
    for node_id in sorted(prefix_ids):
        step = by_id.get(node_id)
        label = step.label if step is not None else node_id
        statuses.append(PerNodeStatus(
            node_id=node_id, status="done", label=label, reused=True, ms=None, rows=None))
    return statuses


def wire_boundary(persisted: dict) -> ReusableExecutionBoundary:
    return ReusableExecutionBoundary(
        canvas_id=str(persisted["canvas_id"]),
        target_node_id=str(persisted["target_node_id"]),
        boundary_node_id=str(persisted["boundary_node_id"]),
        boundary_port_id=str(persisted["boundary_port_id"]),
        boundary_run_id=str(persisted["boundary_run_id"]),
        boundary_execution_manifest_sha256=str(
            persisted["boundary_execution_manifest_sha256"]),
    )


def prepare_boundary_suffix(
        intent_graph: Graph,
        *,
        persisted: dict,
        target_node_id: str,
        deps,
) -> tuple[Graph, object, list[PerNodeStatus], ReusableExecutionBoundary]:
    """Build the suffix compile/dispatch graph for one admitted boundary."""
    artifact_uri = persisted.get("artifact_uri")
    if not isinstance(artifact_uri, str) or not artifact_uri:
        raise BoundarySuffixError("admitted boundary is missing its managed artifact")
    suffix, _ref_id, prefix = build_suffix_graph(
        intent_graph,
        boundary_node_id=str(persisted["boundary_node_id"]),
        boundary_port_id=str(persisted["boundary_port_id"]),
        artifact_uri=artifact_uri,
        target_node_id=target_node_id,
    )
    plan = compiler.compile_plan(
        suffix, target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise BoundarySuffixError(plan.error or "boundary suffix graph has a cycle")
    reused = reused_prefix_statuses(
        intent_graph, prefix_ids=prefix,
        boundary_node_id=str(persisted["boundary_node_id"]), deps=deps)
    return suffix, plan, reused, wire_boundary(persisted)
