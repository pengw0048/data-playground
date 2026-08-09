"""Server-minted identity for whole-dataset profile recovery."""

from __future__ import annotations

from copy import deepcopy

from hub.models import Graph
from hub.plan_key import plan_hash
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
    return profile_plan_digest(graph, "metric", "out", lambda _uri: adapter)


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


def test_admitted_identity_distinguishes_dataset_or_provider_replacement_at_same_revision():
    original = _graph()
    config = next(node for node in original.nodes if node.id == "source").data["config"]
    config.update({
        "_input_dataset_id": "dataset-a",
        "_input_provider": "lance",
        "_input_revision_id": "1",
    })

    for field, replacement in (
            ("_input_dataset_id", "dataset-b"),
            ("_input_provider", "replacement-provider")):
        replaced = deepcopy(original)
        next(node for node in replaced.nodes if node.id == "source").data["config"][field] = replacement
        assert _digest(replaced) != _digest(original)
        assert plan_hash(replaced, "metric", lambda _uri: None) != plan_hash(
            original, "metric", lambda _uri: None)


def test_profile_identity_changes_for_the_selected_output_port():
    graph = _graph()
    adapter = _Adapter({"file:///data.parquet": "generation-1"})

    left = profile_plan_digest(graph, "metric", "left", lambda _uri: adapter)
    right = profile_plan_digest(graph, "metric", "right", lambda _uri: adapter)

    assert left != right


def test_profile_identity_ignores_chart_presentation_type():
    graph = Graph.model_validate({
        "id": "chart-identity",
        "version": 1,
        "nodes": [
            {
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": "file:///data.parquet"}},
            },
            {
                "id": "chart", "type": "chart", "position": {"x": 1, "y": 0},
                "data": {"config": {
                    "chartType": "bar", "agg": "count", "xMode": "column", "x": "event",
                }},
            },
        ],
        "edges": [{
            "id": "edge", "source": "source", "target": "chart",
            "data": {"wire": "dataset"},
        }],
    })
    adapter = _Adapter({"file:///data.parquet": "generation-1"})
    baseline = profile_plan_digest(graph, "chart", "out", lambda _uri: adapter)

    for chart_type in ("line", "scatter", "area"):
        switched = deepcopy(graph)
        switched.nodes[1].data["config"]["chartType"] = chart_type
        assert profile_plan_digest(switched, "chart", "out", lambda _uri: adapter) == baseline

    semantic = deepcopy(graph)
    semantic.nodes[1].data["config"]["agg"] = "sum"
    semantic.nodes[1].data["config"]["y"] = "amount"
    assert profile_plan_digest(semantic, "chart", "out", lambda _uri: adapter) != baseline


def test_profile_identity_ignores_filter_builder_mirror():
    graph = Graph.model_validate({
        "id": "filter-identity",
        "version": 1,
        "nodes": [
            {
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": "file:///data.parquet"}},
            },
            {
                "id": "filter", "type": "filter", "position": {"x": 1, "y": 0},
                "data": {"config": {"predicate": "event = 'purchase'"}},
            },
        ],
        "edges": [{
            "id": "edge", "source": "source", "target": "filter",
            "data": {"wire": "dataset"},
        }],
    })
    adapter = _Adapter({"file:///data.parquet": "generation-1"})
    baseline = profile_plan_digest(graph, "filter", "out", lambda _uri: adapter)

    mirrored = deepcopy(graph)
    mirrored.nodes[1].data["config"]["filterBuilder"] = {
        "conditions": [{"col": "event", "op": "=", "val": "purchase", "type": "string"}],
    }
    assert profile_plan_digest(mirrored, "filter", "out", lambda _uri: adapter) == baseline

    semantic = deepcopy(graph)
    semantic.nodes[1].data["config"]["predicate"] = "event = 'refund'"
    assert profile_plan_digest(semantic, "filter", "out", lambda _uri: adapter) != baseline
