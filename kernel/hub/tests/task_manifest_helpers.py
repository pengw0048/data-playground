"""Test-only builders for canonical DurableTask execution manifests."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from hub.execution_manifest import build_execution_manifest, execution_manifest_admission
from hub.linear_checkpoint_tasks import graph_prefix_sha256
from hub.models import Graph, WriteIntent
from hub.nodespecs import BUILTIN_NODE_SPECS, NodeSpec, PortSpec


def task_manifest_deps(graph: Graph):
    specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
    for node in graph.nodes:
        if node.type not in specs:
            specs[node.type] = NodeSpec(
                kind=node.type,
                title=node.type,
                category="compute",
                inputs=[PortSpec(id="in")],
                outputs=[PortSpec(id="out")],
                source=f"plugin:{node.type}",
            )
    plugins = [
        {"name": kind, "package": kind, "version": "test", "source": "test"}
        for kind, spec in specs.items()
        if spec.source.startswith("plugin:")
    ]
    return SimpleNamespace(node_specs=specs, plugins=plugins)


def with_task_manifest(
    values: dict,
    *,
    target_key: str = "target_node_id",
    deps=None,
) -> dict:
    """Return low-level submission kwargs with one internally consistent manifest."""
    result = dict(values)
    graph = Graph.model_validate(result["graph_doc"])
    intent = WriteIntent.model_validate(result["write_intent"])
    target = str(result[target_key])
    digest, document = build_execution_manifest(
        graph,
        target_node_id=target,
        target_port_id=None,
        input_manifest=result.get("input_manifest", []),
        write_intent=intent,
        deps=deps or task_manifest_deps(graph),
    )
    result["execution_manifest_sha256"] = digest
    result["execution_manifest_doc"] = document
    if "intent_sha256" in result:
        result["intent_sha256"] = digest
    if "task_intent_sha256" in result:
        admission = execution_manifest_admission(digest, document)
        manifest_payload = json.dumps(
            admission["input_manifest"], sort_keys=True, separators=(",", ":"))
        result["task_intent_sha256"] = digest
        result["graph_prefix_sha256"] = graph_prefix_sha256(
            Graph.model_validate(admission["graph_doc"]),
            str(result["checkpoint_node_id"]),
        )
        result["input_manifest_sha256"] = hashlib.sha256(
            manifest_payload.encode()).hexdigest()
    return result
