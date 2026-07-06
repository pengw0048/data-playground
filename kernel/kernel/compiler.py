"""Compile: canvas graph → typed logical plan (PRD §9 tier 2, §10 /graph/compile).

The plan mirrors the execution: source-read → operators → sink/commit → error-gate. It is what
a runner executes and what estimate/placement reason over. Opaque/aggregate stages are single
black-box steps (not sample-previewable, P8).
"""

from __future__ import annotations

from kernel import graph as g
from kernel.executors.engine import node_previewable
from kernel.models import CompilePlan, GraphNode, PlanStep

_STEP_KIND = {
    "source": "read", "sample": "sample", "filter": "filter", "select": "select",
    "sort": "op", "dedup": "op", "sql": "sql", "join": "join", "aggregate": "reduce",
    "transform": "op", "notebook": "op", "metric": "reduce", "write": "write",
    "opaque": "opaque", "loop": "loop", "branch": "branch", "variable": "op",
    "vector-search": "query",
}


def _label(node: GraphNode) -> str:
    if isinstance(node.data, dict):
        return node.data.get("title") or node.type
    return node.type


def compile_plan(graph, target_node_id: str | None = None, registry=None, node_specs=None) -> CompilePlan:
    if not g.is_acyclic(graph):
        return CompilePlan(target_node_id=target_node_id, steps=[], acyclic=False,
                           error="graph has a cycle — control flow must be encapsulated (§5.7)")

    chain = (g.upstream_chain(graph, target_node_id) if target_node_id else g.topo_order(graph))
    steps: list[PlanStep] = []
    for node in chain:
        kind = _STEP_KIND.get(node.type, "op")
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        mode = cfg.get("mode") if node.type in ("transform", "notebook") else node.type
        steps.append(PlanStep(node_id=node.id, kind=kind, mode=mode,
                              previewable=node_previewable(node, registry, node_specs), label=_label(node)))
    return CompilePlan(target_node_id=target_node_id, steps=steps, acyclic=True)
