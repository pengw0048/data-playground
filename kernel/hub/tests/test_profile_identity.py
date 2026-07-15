"""Server-minted identity for whole-dataset profile recovery."""

from __future__ import annotations

from copy import deepcopy

from hub.models import Graph
from hub.profile_identity import profile_plan_digest


class _Adapter:
    def __init__(self, fingerprints: dict[str, str]):
        self.fingerprints = fingerprints

    def fingerprint(self, uri: str) -> str:
        return self.fingerprints[uri]


def _graph() -> Graph:
    return Graph.model_validate({
        "id": "identity-canvas",
        "version": 4,
        "requirements": ["polars==1.0", "numpy==2.0"],
        "nodes": [
            {
                "id": "source", "type": "source", "position": {"x": 1, "y": 2},
                "data": {"title": "Input", "status": "latest", "history": ["ignored"],
                         "config": {"uri": "file:///data.parquet"}},
            },
            {
                "id": "metric", "type": "metric", "position": {"x": 3, "y": 4},
                "data": {"title": "Revenue", "config": {"expr": "sum(amount)"}},
            },
            {
                "id": "unrelated", "type": "filter", "position": {"x": 5, "y": 6},
                "data": {"title": "Elsewhere", "config": {"expr": "x > 0"}},
            },
        ],
        "edges": [{
            "id": "edge-ui-id", "source": "source", "target": "metric",
            "sourceHandle": "out", "targetHandle": "in", "data": {"wire": "dataset"},
        }],
    })


def _digest(graph: Graph, fingerprint: str = "generation-1") -> str:
    adapter = _Adapter({"file:///data.parquet": fingerprint})
    return profile_plan_digest(graph, "metric", lambda _uri: adapter)


def test_profile_identity_is_canonical_and_scoped_to_the_execution_cone():
    original = _graph()
    changed = deepcopy(original)
    changed.version = 99
    changed.nodes.reverse()
    changed.requirements.reverse()
    changed.nodes[1].position.x = 999
    changed.nodes[1].data["status"] = "failed"
    changed.nodes[1].data["history"] = ["different"]
    changed.edges[0].id = "different-ui-edge-id"
    changed.nodes[0].data["config"]["expr"] = "unrelated edit"

    assert _digest(changed) == _digest(original)


def test_profile_identity_changes_for_execution_and_source_revisions():
    original = _graph()

    config_edit = deepcopy(original)
    next(node for node in config_edit.nodes if node.id == "metric").data["config"]["expr"] = "avg(amount)"
    assert _digest(config_edit) != _digest(original)

    title_edit = deepcopy(original)
    next(node for node in title_edit.nodes if node.id == "metric").data["title"] = "Average revenue"
    assert _digest(title_edit) != _digest(original)

    assert _digest(original, "generation-2") != _digest(original, "generation-1")
