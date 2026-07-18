from __future__ import annotations

from types import SimpleNamespace

import pytest

from hub.models import Graph, ParameterBinding, RunStatus
from hub.routers import runs
from hub.run_parameters import (
    ParameterResolutionError, parse_cli_bindings, resolve_graph_parameters,
)


def _graph(*, parameters=None, branch=False, config=None) -> Graph:
    nodes = [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "Source", "status": "draft",
                 "config": {"uri": "/data/source.parquet"}},
    }, {
        "id": "target", "type": "filter", "position": {"x": 0, "y": 0},
        "data": {"title": "Filter", "status": "draft", "config": config or {}},
    }]
    edges = [{"id": "edge", "source": "source", "target": "target"}]
    if branch:
        nodes.append({
            "id": "other", "type": "filter", "position": {"x": 0, "y": 0},
            "data": {"title": "Other", "status": "draft",
                     "config": {"predicate": {"parameterRef": "other"}}},
        })
    return Graph.model_validate({
        "id": "canvas", "version": 3, "nodes": nodes, "edges": edges,
        "parameters": parameters or [],
    })


def _deps():
    return SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda value: value),
        resolve_adapter=lambda _value: object(),
    )


def test_defaults_types_constraints_and_target_cone_are_canonical():
    graph = _graph(
        branch=True,
        parameters=[
            {"name": "threshold", "type": "float", "default": 1,
             "constraints": {"minimum": 0, "maximum": 2}},
            {"name": "other", "type": "string", "required": True},
        ],
        config={"threshold": {"parameterRef": "threshold"}},
    )
    resolved, canonical = resolve_graph_parameters(graph, [], "target", _deps())
    assert resolved.nodes[1].data["config"]["threshold"] == 1.0
    assert canonical[0]["value"] == 1.0
    assert canonical[0]["declaration"]["constraints"] == {"minimum": 0.0, "maximum": 2.0}
    assert [item["name"] for item in canonical] == ["threshold"]
    assert graph.nodes[1].data["config"]["threshold"] == {"parameterRef": "threshold"}


def test_section_target_resolves_parameters_in_contained_children():
    graph = Graph.model_validate({
        "id": "section-canvas", "version": 1,
        "parameters": [{"name": "threshold", "type": "integer", "required": True}],
        "nodes": [
            {"id": "section", "type": "section", "position": {"x": 0, "y": 0},
             "data": {"config": {}}},
            {"id": "child", "type": "filter", "parentId": "section",
             "position": {"x": 0, "y": 0},
             "data": {"config": {"threshold": {"parameterRef": "threshold"}}}},
        ],
        "edges": [],
    })
    resolved, canonical = resolve_graph_parameters(
        graph, [ParameterBinding(name="threshold", value=4)], "section", _deps())
    assert resolved.nodes[1].data["config"]["threshold"] == 4
    assert canonical[0]["value"] == 4


@pytest.mark.parametrize(("typ", "value", "expected"), [
    ("integer", 12, 12),
    ("float", 12, 12.0),
    ("boolean", False, False),
    ("date", "2026-07-18", "2026-07-18"),
    ("datetime", "2026-07-18T10:30:00-04:00", "2026-07-18T14:30:00Z"),
])
def test_scalar_types_have_deterministic_canonical_values(typ, value, expected):
    graph = _graph(
        parameters=[{"name": "value", "type": typ, "required": True}],
        config={"value": {"parameterRef": "value"}},
    )
    resolved, canonical = resolve_graph_parameters(
        graph, [ParameterBinding(name="value", value=value)], "target", _deps())
    assert resolved.nodes[1].data["config"]["value"] == expected
    assert canonical[0]["value"] == expected


@pytest.mark.parametrize(("bindings", "message"), [
    ([], "required parameter 'value' has no binding or default"),
    ([{"name": "unknown", "value": "x"}], "unknown canvas parameter 'unknown'"),
    ([{"name": "value", "value": "x"}, {"name": "value", "value": "y"}],
     "duplicate canvas parameter 'value'"),
    ([{"name": "value", "value": "env:PRIVATE_VALUE"}], "cannot contain a SecretRef"),
])
def test_required_unknown_duplicate_and_secret_refs_fail_closed(bindings, message):
    graph = _graph(
        parameters=[{"name": "value", "type": "string", "required": True}],
        config={"value": {"parameterRef": "value"}},
    )
    with pytest.raises(ParameterResolutionError, match=message):
        resolve_graph_parameters(
            graph, [ParameterBinding.model_validate(item) for item in bindings], "target", _deps())


@pytest.mark.parametrize("value", ["s3://research-bucket/input", "https://example.test/data"])
def test_public_uri_like_strings_are_not_mistaken_for_registered_secret_refs(value):
    graph = _graph(
        parameters=[{"name": "value", "type": "string", "required": True}],
        config={"value": {"parameterRef": "value"}},
    )
    resolved, _canonical = resolve_graph_parameters(
        graph, [ParameterBinding(name="value", value=value)], "target", _deps())
    assert resolved.nodes[1].data["config"]["value"] == value


def test_public_uri_like_dataset_ids_are_allowed_for_exact_bindings():
    graph = _graph(parameters=[{"name": "input", "type": "dataset", "required": True}])
    graph.nodes[0].data["config"]["datasetRef"] = {"parameterRef": "input"}
    resolved, canonical = resolve_graph_parameters(graph, [ParameterBinding(
        name="input",
        value={"kind": "exact", "datasetId": "s3://catalog/dataset", "revisionId": "v1"},
    )], "target", _deps())
    assert resolved.nodes[0].data["config"]["datasetRef"]["datasetId"] == "s3://catalog/dataset"
    assert canonical[0]["value"]["revisionId"] == "v1"


@pytest.mark.parametrize(("declaration", "value", "message"), [
    ({"name": "count", "type": "integer"}, "not-an-integer", "safe integer"),
    ({"name": "uri", "type": "string"}, "env:PRIVATE_URI", "SecretRef"),
])
def test_explicit_unused_bindings_are_still_validated(declaration, value, message):
    graph = _graph(parameters=[declaration])
    with pytest.raises(ParameterResolutionError, match=message):
        resolve_graph_parameters(
            graph, [ParameterBinding(name=declaration["name"], value=value)], "target", _deps())


def test_dates_require_real_dates_and_datetime_timezone():
    for typ, value, message in [
        ("date", "2026-02-30", "ISO date"),
        ("datetime", "2026-07-18T10:30:00", "explicit timezone"),
    ]:
        graph = _graph(
            parameters=[{"name": "value", "type": typ}],
            config={"value": {"parameterRef": "value"}},
        )
        with pytest.raises(ParameterResolutionError, match=message):
            resolve_graph_parameters(
                graph, [ParameterBinding(name="value", value=value)], "target", _deps())


def test_exact_and_latest_dataset_intent(monkeypatch):
    graph = _graph(parameters=[{"name": "input", "type": "dataset"}])
    graph.nodes[0].data["config"]["datasetRef"] = {"parameterRef": "input"}
    exact, canonical = resolve_graph_parameters(graph, [ParameterBinding(
        name="input", value={"kind": "exact", "datasetId": "dataset-1", "revisionId": "r1"},
    )], "target", _deps())
    assert exact.nodes[0].data["config"]["datasetRef"] == {
        "kind": "exact", "datasetId": "dataset-1", "revisionId": "r1",
    }
    assert canonical[0]["value"]["kind"] == "exact"

    class Adapter:
        def resolve_revision(self, _uri):
            return {"revision_id": "r2"}

    monkeypatch.setattr("hub.run_parameters.revision_adapter_for_uri", lambda *_args: Adapter())
    monkeypatch.setattr(
        "hub.run_parameters.metadb.catalog_revision_binding_for_uri",
        lambda _uri: {"dataset_id": "dataset-1"},
    )
    latest, canonical = resolve_graph_parameters(graph, [ParameterBinding(
        name="input", value={"kind": "latest", "datasetId": "dataset-1"},
    )], "target", _deps())
    assert latest.nodes[0].data["config"]["datasetRef"]["revisionId"] == "r2"
    assert canonical[0]["value"] == {
        "kind": "latest", "datasetId": "dataset-1", "resolvedRevisionId": "r2",
    }

    with pytest.raises(ParameterResolutionError, match="SecretRef"):
        resolve_graph_parameters(graph, [ParameterBinding(
            name="input", value={"kind": "latest", "datasetId": "env:PRIVATE_DATASET"},
        )], "target", _deps())


def test_retained_latest_intent_is_validated_without_consulting_mutable_head(monkeypatch):
    graph = _graph(parameters=[{"name": "input", "type": "dataset", "required": True}])
    graph.nodes[0].data["config"]["datasetRef"] = {"parameterRef": "input"}

    def mutable_head_access_is_a_bug(*_args, **_kwargs):
        raise AssertionError("retained replay consulted mutable provider state")

    monkeypatch.setattr(
        "hub.run_parameters.revision_adapter_for_uri", mutable_head_access_is_a_bug)
    monkeypatch.setattr(
        "hub.run_parameters.metadb.catalog_revision_binding_for_uri",
        mutable_head_access_is_a_bug,
    )
    resolved, canonical = resolve_graph_parameters(
        graph,
        [ParameterBinding(
            name="input", value={"kind": "latest", "datasetId": "dataset-1"})],
        "target",
        _deps(),
        freeze_latest=False,
    )

    assert resolved.nodes[0].data["config"]["datasetRef"] == {
        "kind": "latest", "datasetId": "dataset-1",
    }
    assert canonical[0]["value"] == {"kind": "latest", "datasetId": "dataset-1"}


def test_durable_retry_adopts_before_freezing_latest(monkeypatch):
    graph = Graph.model_validate({
        "id": "durable-parameter-replay", "version": 1,
        "parameters": [{"name": "input", "type": "dataset", "required": True}],
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {
                "uri": "/data/source.lance", "datasetRef": {"parameterRef": "input"},
            }}},
            {"id": "write", "type": "write", "data": {"config": {}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    binding = ParameterBinding(
        name="input", value={"kind": "latest", "datasetId": "dataset-1"})

    def mutable_head_access_is_a_bug(*_args, **_kwargs):
        raise AssertionError("durable replay consulted mutable provider state")

    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs, "_require_graph_read_access", lambda *_args: None)
    monkeypatch.setattr(runs.metadb, "canvas_exists", lambda _canvas: True)
    monkeypatch.setattr(
        runs.metadb, "durable_task_submission_id", lambda *_args: "retained-task")
    monkeypatch.setattr(runs.metadb, "durable_task", lambda *_args, **_kwargs: {
        "id": "retained-task", "execution_manifest_sha256": "a" * 64,
    })
    monkeypatch.setattr(
        "hub.run_parameters.revision_adapter_for_uri", mutable_head_access_is_a_bug)
    monkeypatch.setattr(
        "hub.run_parameters.metadb.catalog_revision_binding_for_uri",
        mutable_head_access_is_a_bug,
    )

    def adopt(_deps, _task, retry_graph, target, supplied_inputs, supplied_write):
        assert retry_graph.nodes[0].data["config"]["datasetRef"] == {
            "kind": "latest", "datasetId": "dataset-1",
        }
        assert retry_graph._parameter_bindings[0]["value"] == {
            "kind": "latest", "datasetId": "dataset-1",
        }
        assert target == "write"
        assert supplied_inputs is supplied_write is None
        return RunStatus(run_id="retained-task", status="queued")

    monkeypatch.setattr(runs, "_adopt_manifest_durable_task", adopt)
    status, owner = runs.start_run(
        SimpleNamespace(), graph, "write", "local", confirmed=True,
        submission_id="retry-submission", parameter_bindings=[binding])

    assert status.run_id == "retained-task"
    assert owner is None


@pytest.mark.parametrize("doc", [
    {
        "parameters": [{"name": "known", "type": "string"}],
        "config": {"value": {"parameterRef": "missing"}},
    },
    {
        "parameters": [{"name": "known", "type": "string"}],
        "config": {"value": {"parameterRef": "known", "fallback": "x"}},
    },
    {
        "parameters": [{"name": "dataset", "type": "dataset"}],
        "config": {"value": {"parameterRef": "dataset"}},
    },
])
def test_graph_durable_contract_rejects_dangling_malformed_or_misplaced_refs(doc):
    with pytest.raises(ValueError, match="parameter"):
        _graph(parameters=doc["parameters"], config=doc["config"])


def test_source_dataset_ref_requires_a_dataset_parameter():
    graph = _graph(parameters=[{"name": "value", "type": "string"}])
    graph_doc = graph.model_dump(by_alias=True, mode="json")
    graph_doc["nodes"][0]["data"]["config"]["datasetRef"] = {"parameterRef": "value"}
    with pytest.raises(ValueError, match="must have type 'dataset'"):
        Graph.model_validate(graph_doc)


def test_cli_parsing_is_typed_and_preserves_duplicates_for_server_rejection():
    graph = _graph(parameters=[
        {"name": "count", "type": "integer"},
        {"name": "enabled", "type": "boolean"},
        {"name": "input", "type": "dataset"},
    ])
    parsed = parse_cli_bindings(
        graph, ["count=12", "enabled=false", "input=dataset-1@latest", "count=13"])
    assert [item.value for item in parsed] == [
        12, False, {"kind": "latest", "datasetId": "dataset-1"}, 13,
    ]
    with pytest.raises(ParameterResolutionError, match="duplicate"):
        resolve_graph_parameters(graph, parsed, None, _deps())


def test_save_reload_and_unparameterized_canvas_are_unchanged():
    graph = _graph(parameters=[{"name": "value", "type": "string", "label": "Value"}])
    reloaded = Graph.model_validate_json(graph.model_dump_json(by_alias=True))
    assert reloaded.parameters[0].name == "value"

    legacy = _graph()
    resolved, canonical = resolve_graph_parameters(legacy, [], "target", _deps())
    assert resolved.model_dump(by_alias=True) == legacy.model_dump(by_alias=True)
    assert canonical == []
