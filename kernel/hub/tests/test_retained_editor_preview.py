"""Fullscreen Transform tests may consume only a current retained immediate-upstream result."""

from __future__ import annotations

import copy
import datetime
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from hub import execution_manifest, metadb
from hub.deps import get_deps
from hub.main import app
from hub.models import SampleResult
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.routers import runs as runs_router


client = TestClient(app)


def _graph(canvas_id: str) -> dict:
    uri = get_deps().catalog.get_table("tbl_events").uri
    return {
        "id": canvas_id,
        "name": "Retained editor input",
        "version": 1,
        "requirements": [],
        "nodes": [
            {
                "id": "source",
                "type": "source",
                "position": {"x": 0, "y": 0},
                "data": {"title": "Events", "config": {"uri": uri}},
            },
            {
                "id": "sample",
                "type": "sample",
                "position": {"x": 200, "y": 0},
                "data": {"title": "Sample", "config": {"n": 3, "seed": 42}},
            },
            {
                "id": "transform",
                "type": "transform",
                "position": {"x": 400, "y": 0},
                "data": {
                    "title": "Transform",
                    "config": {
                        "source": "adhoc",
                        "mode": "map",
                        "code": "def fn(row):\n    return row",
                        "onError": "raise",
                    },
                },
            },
        ],
        "edges": [
            {
                "id": "source-sample",
                "source": "source",
                "target": "sample",
                "sourceHandle": "out",
                "targetHandle": "in",
                "data": {"wire": "dataset"},
            },
            {
                "id": "sample-transform",
                "source": "sample",
                "target": "transform",
                "sourceHandle": "out",
                "targetHandle": "in",
                "data": {"wire": "sample"},
            },
        ],
    }


def _wait(run_id: str) -> dict:
    for _ in range(200):
        status = client.get(f"/api/run/{run_id}")
        assert status.status_code == 200, status.text
        document = status.json()
        if document["status"] in ("done", "failed", "cancelled"):
            return document
        time.sleep(0.05)
    pytest.fail(f"run {run_id} did not finish")


def _wait_for_history_projection(run_id: str) -> None:
    """Terminal operational state may commit before its asynchronous history row."""
    for _ in range(200):
        with metadb.session() as session:
            record = session.scalar(
                metadb.select(metadb.RunRecord).where(
                    metadb.RunRecord.run_id == run_id))
            if record is not None:
                return
        time.sleep(0.05)
    pytest.fail(f"run {run_id} did not reach projected history")


def _transform_join_graph(canvas_id: str) -> dict:
    events = get_deps().catalog.get_table("tbl_events")
    events_uri = events.uri
    return {
        "id": canvas_id,
        "name": "Retained Transform schema",
        "version": 1,
        "requirements": [],
        "nodes": [
            {
                "id": "left",
                "type": "source",
                "position": {"x": 0, "y": 0},
                "data": {"title": "Left events", "config": {
                    "uri": events_uri, "registrationId": events.registration_id,
                }},
            },
            {
                "id": "transform",
                "type": "transform",
                "position": {"x": 200, "y": 0},
                "data": {"title": "Derived amount", "config": {
                    "source": "adhoc", "mode": "map", "onError": "raise",
                    "code": "def fn(row):\n    return {**row, 'amount_doubled': row['amount'] * 2}",
                }},
            },
            {
                "id": "right",
                "type": "source",
                "position": {"x": 200, "y": 220},
                "data": {"title": "Right events", "config": {
                    "uri": events_uri, "registrationId": events.registration_id,
                }},
            },
            {
                "id": "join",
                "type": "join",
                "position": {"x": 440, "y": 80},
                "data": {"title": "Join", "config": {"how": "inner", "on": "user_id"}},
            },
        ],
        "edges": [
            {"id": "left-transform", "source": "left", "sourceHandle": "out",
             "target": "transform", "targetHandle": "in", "data": {"wire": "dataset"}},
            {"id": "transform-join", "source": "transform", "sourceHandle": "out",
             "target": "join", "targetHandle": "a", "data": {"wire": "dataset"}},
            {"id": "right-join", "source": "right", "sourceHandle": "out",
             "target": "join", "targetHandle": "b", "data": {"wire": "dataset"}},
        ],
    }


def test_current_retained_transform_result_propagates_schema_and_invalidates():
    canvas_id = f"retained-transform-schema-{uuid.uuid4().hex}"
    graph = _transform_join_graph(canvas_id)
    created = client.post("/api/canvas", json=graph)
    assert created.status_code == 200, created.text
    try:
        before = client.post("/api/graph/schema", json={"graph": graph})
        assert before.status_code == 200, before.text
        assert before.json()["transform"]["out"] is None
        assert before.json()["join"]["out"] is None

        started = client.post("/api/run", json={
            "graph": graph, "targetNodeId": "transform", "confirmed": True,
            "submissionId": str(uuid.uuid4()),
        })
        assert started.status_code == 200, started.text
        status = _wait(started.json()["runId"])
        assert status["status"] == "done", status
        _wait_for_history_projection(status["runId"])

        current = client.post("/api/graph/schema", json={"graph": graph})
        assert current.status_code == 200, current.text
        current_schema = current.json()
        transform_columns = [column["name"] for column in current_schema["transform"]["out"]]
        assert transform_columns == ["id", "user_id", "event", "amount", "amount_doubled"]
        assert "amount_doubled" in [column["name"] for column in current_schema["join"]["out"]]

        # A fresh schema request models Canvas reload: evidence is retained in the run/result
        # contract, not in a client preview or a mutation of the saved graph.
        reloaded = client.post("/api/graph/schema", json={"graph": graph})
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["transform"]["out"] == current_schema["transform"]["out"]

        for field, value in (
            ("code", "def fn(row):\n    return {**row, 'amount_tripled': row['amount'] * 3}"),
            ("mode", "filter"),
            ("onError", "drop"),
        ):
            changed = copy.deepcopy(graph)
            changed["nodes"][1]["data"]["config"][field] = value
            response = client.post("/api/graph/schema", json={"graph": changed})
            assert response.status_code == 200, response.text
            assert response.json()["transform"]["out"] is None
            assert response.json()["join"]["out"] is None

        changed_source = copy.deepcopy(graph)
        images = get_deps().catalog.get_table("tbl_images")
        changed_source["nodes"][0]["data"]["config"] = {
            "uri": images.uri, "registrationId": images.registration_id,
        }
        response = client.post("/api/graph/schema", json={"graph": changed_source})
        assert response.status_code == 200, response.text
        assert response.json()["transform"]["out"] is None
        assert response.json()["join"]["out"] is None
    finally:
        metadb.delete_canvas_cascade(canvas_id)


@contextmanager
def _retained_sample(tmp_path, configure_graph=None, *, logical_source=False):
    lance = pytest.importorskip("lance")
    canvas_id = f"retained-editor-{uuid.uuid4().hex}"
    source_uri = str(tmp_path / f"{canvas_id}.lance")
    lance.write_dataset(pa.table({
        "event": ["view", "purchase", "view", "purchase"],
        "amount": [1, 2, 3, 4],
    }), source_uri)
    registered = client.post("/api/catalog/register", json={
        "uri": source_uri, "name": f"exact-{uuid.uuid4().hex}",
    })
    assert registered.status_code == 200, registered.text
    graph = _graph(canvas_id)
    graph["nodes"][0]["data"]["config"] = (
        {"uri": source_uri, "registrationId": registered.json()["registrationId"]}
        if logical_source else {
            "uri": source_uri,
            "datasetRef": {
                "kind": "exact",
                "datasetId": registered.json()["registrationId"],
                "revisionId": "1",
            },
        }
    )
    if configure_graph is not None:
        configure_graph(graph)
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Retained editor input"))
    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "sample",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    _wait_for_history_projection(status["runId"])
    output = status["outputs"][0]
    assert output["nodeId"] == "sample" and output["portId"] == "out"
    try:
        yield graph, status["runId"], output
    finally:
        metadb.delete_canvas_cascade(canvas_id)
        get_deps().catalog.unregister(source_uri)
        shutil.rmtree(source_uri, ignore_errors=True)


@pytest.fixture
def retained_sample(tmp_path):
    with _retained_sample(tmp_path) as retained:
        yield retained


def _preview(graph: dict, port_id: str = "out"):
    return client.post("/api/run/editor-preview", json={
        "graph": graph,
        "nodeId": "transform",
        "portId": port_id,
        "k": 2,
        "offset": 0,
    })


def _retained_result(graph: dict, node_id: str = "sample", port_id: str = "out"):
    return client.post("/api/run/retained-result", json={
        "graph": graph,
        "nodeId": node_id,
        "portId": port_id,
    })


def test_canvas_recovers_exact_retained_result_without_creating_a_run(retained_sample):
    graph, run_id, output = retained_sample
    history_before = metadb.list_runs(graph["id"])

    response = _retained_result(graph)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "runId": run_id,
        "executionManifestSha256": history_before[0]["executionManifestSha256"],
        "output": output,
    }
    page = client.post(f"/api/run/{run_id}/sample", json={
        "nodeId": "sample", "portId": "out", "k": 2, "offset": 0,
    })
    assert page.status_code == 200, page.text
    assert len(page.json()["rows"]) == 2
    assert metadb.list_runs(graph["id"]) == history_before

    changed = copy.deepcopy(graph)
    changed["nodes"][1]["data"]["config"]["seed"] = 99
    stale = _retained_result(changed)
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "retained_upstream_stale"

    os.unlink(output["uri"])
    retained_identity = _retained_result(graph)
    assert retained_identity.status_code == 200, retained_identity.text
    expired = client.post(f"/api/run/{run_id}/sample", json={
        "nodeId": "sample", "portId": "out", "k": 2, "offset": 0,
    })
    assert expired.status_code == 410, expired.text
    assert expired.json()["code"] == "resource_gone"
    assert expired.json()["retryable"] is False


def test_canvas_recovers_retained_result_from_its_registered_logical_uri(tmp_path):
    with _retained_sample(tmp_path) as (graph, run_id, _output):
        graph["nodes"][0]["data"]["config"].pop("datasetRef")

        response = _retained_result(graph)

        assert response.status_code == 200, response.text
        assert response.json()["runId"] == run_id


def test_canvas_does_not_treat_missing_bindings_as_defaults_or_fall_back(tmp_path):
    def configure(graph):
        graph["parameters"] = [{
            "name": "sample_size",
            "type": "integer",
            "default": 3,
        }]
        graph["nodes"][1]["data"]["config"]["n"] = {
            "parameterRef": "sample_size",
        }

    with _retained_sample(tmp_path, configure) as (graph, default_run_id, _output):
        started = client.post("/api/run", json={
            "graph": graph,
            "targetNodeId": "sample",
            "confirmed": True,
            "submissionId": str(uuid.uuid4()),
            "parameterBindings": [{"name": "sample_size", "value": 2}],
        })
        assert started.status_code == 200, started.text
        bound = _wait(started.json()["runId"])
        assert bound["status"] == "done", bound
        _wait_for_history_projection(bound["runId"])
        assert bound["runId"] != default_run_id
        history_before = metadb.list_runs(graph["id"])

        unknown = _retained_result(graph)
        assert unknown.status_code == 409, unknown.text
        assert unknown.json()["code"] == "retained_upstream_stale"
        assert "parameter bindings" in unknown.json()["detail"]

        exact = client.post("/api/run/retained-result", json={
            "graph": graph,
            "nodeId": "sample",
            "portId": "out",
            "parameterBindings": [{"name": "sample_size", "value": 2}],
        })
        assert exact.status_code == 200, exact.text
        assert exact.json()["runId"] == bound["runId"]

        defaults = client.post("/api/run/retained-result", json={
            "graph": graph,
            "nodeId": "sample",
            "portId": "out",
            "parameterBindings": [],
        })
        assert defaults.status_code == 200, defaults.text
        assert defaults.json()["runId"] == default_run_id
        assert metadb.list_runs(graph["id"]) == history_before


def test_retained_editor_preview_reuses_current_upstream_without_freezing_transform(
        retained_sample):
    graph, run_id, output = retained_sample
    first = _preview(graph)
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["rows"]) == 2
    assert body["editorTestInput"] == {
        "runId": run_id,
        "nodeId": "sample",
        "portId": "out",
        "label": "Sample",
        "rows": 3,
    }
    assert "inputManifest" not in body or body["inputManifest"] is None
    assert body.get("sampleProvenance") is None
    assert output["uri"] not in json.dumps(body, sort_keys=True)

    edited = copy.deepcopy(graph)
    edited["nodes"][2]["data"]["config"]["code"] = (
        "def fn(row):\n    return {**row, 'edited_in_fullscreen': True}")
    changed_code = _preview(edited)
    assert changed_code.status_code == 200, changed_code.text
    assert all(row["edited_in_fullscreen"] is True for row in changed_code.json()["rows"])

    edited_edge = copy.deepcopy(edited)
    edited_edge["edges"][1]["id"] = "display-only-edge-replacement"
    edited_edge["edges"][1]["data"] = {"wire": "dataset"}
    changed_edge = _preview(edited_edge)
    assert changed_edge.status_code == 200, changed_edge.text


@pytest.mark.parametrize("drift", ["core-package", "node-spec", "plugin-version"])
def test_retained_editor_preview_rejects_descriptor_drift(tmp_path, monkeypatch, drift):
    deps = get_deps()
    plugin_status = None
    if drift == "plugin-version":
        plugin_status = {
            "name": "retained-editor-descriptor",
            "package": "retained-editor-descriptor",
            "version": "1.0.0",
            "source": "test",
        }
        monkeypatch.setattr(deps, "plugins", [*deps.plugins, plugin_status])
        monkeypatch.setitem(
            deps.node_specs,
            "sample",
            deps.node_specs["sample"].model_copy(
                update={"source": "plugin:retained-editor-descriptor"}),
        )

    with _retained_sample(tmp_path) as (graph, _run_id, _output):
        baseline = _preview(graph)
        assert baseline.status_code == 200, baseline.text
        if drift == "core-package":
            monkeypatch.setattr(
                execution_manifest, "core_package_version",
                lambda: "retained-editor-drift",
            )
        elif drift == "node-spec":
            spec = deps.node_specs["sample"]
            monkeypatch.setitem(
                deps.node_specs,
                "sample",
                spec.model_copy(update={"can_bypass": not spec.can_bypass}),
            )
        else:
            assert plugin_status is not None
            monkeypatch.setitem(plugin_status, "version", "2.0.0")

        response = _preview(graph)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "retained_upstream_stale"


def test_transform_only_parameter_binding_does_not_change_upstream_identity(tmp_path):
    def configure(graph):
        graph["parameters"] = [{
            "name": "editor_code",
            "type": "string",
            "required": True,
        }]

    with _retained_sample(tmp_path, configure) as (graph, _run_id, _output):
        edited = copy.deepcopy(graph)
        edited["nodes"][2]["data"]["config"]["code"] = {
            "parameterRef": "editor_code",
        }
        response = client.post("/api/run/editor-preview", json={
            "graph": edited,
            "nodeId": "transform",
            "portId": "out",
            "k": 2,
            "offset": 0,
            "parameterBindings": [{
                "name": "editor_code",
                "value": "def fn(row):\n    return {**row, 'parameterized': True}",
            }],
        })
        assert response.status_code == 200, response.text
        assert all(row["parameterized"] is True for row in response.json()["rows"])


def test_upstream_parameter_binding_change_remains_stale(tmp_path):
    def configure(graph):
        graph["parameters"] = [{
            "name": "sample_size",
            "type": "integer",
            "default": 3,
        }]
        graph["nodes"][1]["data"]["config"]["n"] = {
            "parameterRef": "sample_size",
        }

    with _retained_sample(tmp_path, configure) as (graph, _run_id, _output):
        response = client.post("/api/run/editor-preview", json={
            "graph": graph,
            "nodeId": "transform",
            "portId": "out",
            "k": 2,
            "offset": 0,
            "parameterBindings": [{"name": "sample_size", "value": 4}],
        })
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "retained_upstream_stale"


def test_retained_editor_preview_bounds_a_long_upstream_title(tmp_path):
    title = "S" * 257

    def configure(graph):
        graph["nodes"][1]["data"]["title"] = title

    with _retained_sample(tmp_path, configure) as (graph, _run_id, _output):
        response = _preview(graph)
        assert response.status_code == 200, response.text
        assert response.json()["editorTestInput"]["label"] == title[:256]


def test_invalid_transform_output_is_a_request_error_not_a_candidate_miss(retained_sample):
    graph, _run_id, _output = retained_sample
    response = _preview(graph, "missing-output")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "output_port_not_found"


@pytest.mark.parametrize("mutate", [
    lambda graph: graph["nodes"][1]["data"]["config"].update({"seed": 99}),
    lambda graph: graph["nodes"][0]["data"]["config"]["datasetRef"].update({
        "revisionId": "other-revision",
    }),
    lambda graph: graph["edges"][1].update({"sourceHandle": "missing-output"}),
], ids=["sample-config", "source-binding", "source-output-port"])
def test_retained_editor_preview_rejects_stale_upstream_plan(retained_sample, mutate):
    graph, _run_id, _output = retained_sample
    changed = copy.deepcopy(graph)
    mutate(changed)
    response = _preview(changed)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "retained_upstream_stale"


def test_retained_editor_preview_classifies_missing_expired_and_denied(
        retained_sample, monkeypatch):
    graph, _run_id, output = retained_sample
    real_scope = runs_router.source_read_scope

    def denied(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(runs_router, "source_read_scope", denied)
    forbidden = _preview(graph)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "permission_denied"
    monkeypatch.setattr(runs_router, "source_read_scope", real_scope)

    artifact = output["uri"]
    assert os.path.isfile(artifact)
    os.unlink(artifact)
    expired = _preview(graph)
    assert expired.status_code == 410
    assert expired.json()["code"] == "retained_upstream_expired"


def test_corrupt_retained_editor_artifact_redacts_private_diagnostics(
        retained_sample):
    graph, _run_id, output = retained_sample
    target_uri = output["uri"]
    with open(target_uri, "wb") as stream:
        stream.write(b"not a parquet artifact")
    response = _preview(graph)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"] is True
    assert body["failureCategory"] == "runtime_error"
    assert body["reason"]
    assert "retained upstream result" in body["reason"]
    assert body["reason"] != "retained upstream result"
    encoded = json.dumps(body, sort_keys=True)
    assert output["uri"] not in body["reason"] and output["uri"] not in encoded
    assert target_uri not in body["reason"] and target_uri not in encoded


def test_retained_editor_redaction_covers_structured_diagnostics_not_user_rows():
    output_uri = "/private/result-root"
    target_uri = "/private/result-root/member.parquet"
    result = SampleResult(
        error=True,
        reason=f"failed to read {output_uri} through {target_uri}",
        user_code_exception={
            "node_id": "transform",
            "exception_type": "ValueError",
            "message": f"bad input from {target_uri}",
            "guidance": f"inspect {output_uri}",
        },
    )
    runs_router._redact_retained_editor_diagnostics(
        result, (output_uri, target_uri))
    assert result.reason == (
        "failed to read retained upstream result through retained upstream result")
    assert result.user_code_exception is not None
    assert result.user_code_exception.message == "bad input from retained upstream result"
    assert result.user_code_exception.guidance == "inspect retained upstream result"

    successful = SampleResult(
        rows=[{"value": target_uri}],
        row_count=1,
        has_more=False,
        completeness="complete",
    )
    runs_router._redact_retained_editor_diagnostics(
        successful, (output_uri, target_uri))
    assert successful.rows == [{"value": target_uri}]


def test_retained_editor_preview_rejects_mutable_local_source_without_reading(
        tmp_path, monkeypatch):
    source = tmp_path / "mutable.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), source)
    registered = client.post("/api/catalog/register", json={
        "uri": str(source), "name": f"mutable-{uuid.uuid4().hex}",
    })
    assert registered.status_code == 200, registered.text
    canvas_id = f"retained-head-{uuid.uuid4().hex}"
    graph = _graph(canvas_id)
    graph["nodes"][0]["data"]["config"]["uri"] = str(source)
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Retained head"))
    try:
        started = client.post("/api/run", json={
            "graph": graph,
            "targetNodeId": "sample",
            "confirmed": True,
            "submissionId": str(uuid.uuid4()),
        })
        assert started.status_code == 200, started.text
        status = _wait(started.json()["runId"])
        assert status["status"] == "done", status
        def forbidden_scan(*_args, **_kwargs):
            raise AssertionError("editor reuse must not scan a mutable Source")

        monkeypatch.setattr(DuckDBAdapter, "scan", forbidden_scan)
        stale = _preview(graph)
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "retained_upstream_stale"
    finally:
        get_deps().catalog.unregister(str(source))
        metadb.delete_canvas_cascade(canvas_id)


def test_exact_source_reuses_retained_rows_without_reopening_provider(
        retained_sample, monkeypatch):
    lance = pytest.importorskip("lance")
    graph, run_id, _output = retained_sample
    source_uri = graph["nodes"][0]["data"]["config"]["uri"]
    lance.write_dataset(pa.table({
        "event": ["new-head"], "amount": [999],
    }), source_uri, mode="append")

    def forbidden_revision_read(*_args, **_kwargs):
        raise AssertionError("editor reuse must not reopen the exact provider")

    monkeypatch.setattr(LanceAdapter, "open_revision", forbidden_revision_read)
    monkeypatch.setattr(LanceAdapter, "preview_revision", forbidden_revision_read)
    response = _preview(graph)
    assert response.status_code == 200, response.text
    assert response.json()["editorTestInput"]["runId"] == run_id


def test_logical_workspace_source_reuses_retained_rows_after_head_advances(
        tmp_path, monkeypatch):
    """Workspace Use Sources carry registrationId, while the run owns its exact revision."""
    lance = pytest.importorskip("lance")
    with _retained_sample(tmp_path, logical_source=True) as (graph, run_id, _output):
        source_uri = graph["nodes"][0]["data"]["config"]["uri"]
        lance.write_dataset(pa.table({
            "event": ["new-head"], "amount": [999],
        }), source_uri, mode="append")

        def forbidden_revision_read(*_args, **_kwargs):
            raise AssertionError("editor reuse must read only the retained upstream result")

        monkeypatch.setattr(LanceAdapter, "open_revision", forbidden_revision_read)
        monkeypatch.setattr(LanceAdapter, "preview_revision", forbidden_revision_read)
        response = _preview(graph)
        assert response.status_code == 200, response.text
        assert response.json()["editorTestInput"]["runId"] == run_id


def test_retained_editor_preview_uses_terminal_state_before_history_projection(tmp_path):
    """The editor can open immediately after a local run reaches terminal state."""
    with _retained_sample(tmp_path, logical_source=True) as (graph, run_id, _output):
        # Model the narrow real ordering window: RunState has committed first, but the asynchronous
        # history projection has not yet run.  The terminal state remains Canvas- and manifest-bound.
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            assert state is not None
            assert state.canvas_id == graph["id"]
            assert state.status == "done"
            assert json.loads(state.doc)["target_node_id"] == "sample"
            record = session.scalar(
                metadb.select(metadb.RunRecord).where(metadb.RunRecord.run_id == run_id))
            assert record is not None
            session.delete(record)

        response = _preview(graph)
        assert response.status_code == 200, response.text
        assert response.json()["editorTestInput"]["runId"] == run_id


def test_canvas_prefers_newer_terminal_result_before_history_projection(tmp_path):
    """A committed live result stays ahead of older projected history."""
    with _retained_sample(
            tmp_path, logical_source=True) as (graph, older_run_id, _output):
        started = client.post("/api/run", json={
            "graph": graph,
            "targetNodeId": "sample",
            "confirmed": True,
            "submissionId": str(uuid.uuid4()),
        })
        assert started.status_code == 200, started.text
        newer = _wait(started.json()["runId"])
        assert newer["status"] == "done", newer
        newer_run_id = newer["runId"]
        _wait_for_history_projection(newer_run_id)

        # Model the real publication window deterministically: B's committed RunState is newer, while
        # its asynchronous RunRecord projection has not landed and A remains in projected history.
        with metadb.session() as session:
            older_state = session.get(metadb.RunState, older_run_id)
            newer_state = session.get(metadb.RunState, newer_run_id)
            assert older_state is not None and newer_state is not None
            older_state.updated_at = datetime.datetime(
                2026, 1, 1, tzinfo=datetime.timezone.utc)
            newer_state.updated_at = datetime.datetime(
                2026, 1, 2, tzinfo=datetime.timezone.utc)
            newer_record = session.scalar(
                metadb.select(metadb.RunRecord).where(
                    metadb.RunRecord.run_id == newer_run_id))
            assert newer_record is not None
            session.delete(newer_record)

        history_before = metadb.list_runs(graph["id"])
        assert [item["runId"] for item in history_before] == [older_run_id]
        response = _retained_result(graph)
        assert response.status_code == 200, response.text
        assert response.json()["runId"] == newer_run_id
        assert metadb.list_runs(graph["id"]) == history_before

        # Without comparable terminal evidence for A, choosing either candidate would be a guess.
        with metadb.session() as session:
            older_state = session.get(metadb.RunState, older_run_id)
            assert older_state is not None
            session.delete(older_state)
        ambiguous = _retained_result(graph)
        assert ambiguous.status_code == 404, ambiguous.text
        assert ambiguous.json()["code"] == "retained_upstream_unavailable"
        assert metadb.list_runs(graph["id"]) == history_before


def test_terminal_state_does_not_outlive_history_retention_for_editor_reuse(tmp_path):
    with _retained_sample(tmp_path, logical_source=True) as (graph, run_id, _output):
        # Normal history pruning removes the admission together with the RunRecord. A still-retained
        # operational RunState must not resurrect that older result as editor history.
        with metadb.session() as session:
            record = session.scalar(
                metadb.select(metadb.RunRecord).where(metadb.RunRecord.run_id == run_id))
            admission = session.get(metadb.RunInputAdmission, run_id)
            assert record is not None and admission is not None
            session.delete(record)
            session.delete(admission)

        response = _preview(graph)
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "retained_upstream_unavailable"


def test_projected_history_is_never_overridden_by_live_state(tmp_path):
    with _retained_sample(tmp_path, logical_source=True) as (graph, run_id, _output):
        with metadb.session() as session:
            record = session.scalar(
                metadb.select(metadb.RunRecord).where(metadb.RunRecord.run_id == run_id))
            assert record is not None
            record.outputs = "[]"

        response = _preview(graph)
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "retained_upstream_unavailable"


def test_logical_workspace_source_registration_change_invalidates_reuse(tmp_path):
    with _retained_sample(tmp_path, logical_source=True) as (graph, _run_id, _output):
        changed = copy.deepcopy(graph)
        changed["nodes"][0]["data"]["config"]["registrationId"] = "replaced-registration"
        response = _preview(changed)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "retained_upstream_stale"


def test_official_transform_run_does_not_depend_on_editor_retained_input(retained_sample):
    graph, _run_id, output = retained_sample
    assert _preview(graph).status_code == 200
    os.unlink(output["uri"])

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "transform",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert [(item["nodeId"], item["portId"]) for item in status["outputs"]] == [
        ("transform", "out"),
    ]


def test_unrelated_recent_run_history_cannot_hide_the_retained_candidate(retained_sample):
    graph, run_id, _output = retained_sample
    with metadb.session() as session:
        for index in range(60):
            session.add(metadb.RunRecord(
                canvas_id=graph["id"],
                run_id=f"unrelated-{uuid.uuid4().hex}",
                target_node_id="transform",
                job_type="run",
                status="done",
                outputs="[]",
            ))

    response = _preview(graph)
    assert response.status_code == 200, response.text
    assert response.json()["editorTestInput"]["runId"] == run_id


def test_retained_editor_preview_refuses_a_run_from_another_visible_canvas(retained_sample):
    graph, _run_id, _output = retained_sample
    other = copy.deepcopy(graph)
    other["id"] = f"other-canvas-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=other["id"], owner_id=metadb.DEFAULT_USER_ID, name="Other canvas"))
    try:
        response = _preview(other)
        assert response.status_code == 404
        assert response.json()["code"] == "retained_upstream_unavailable"
    finally:
        metadb.delete_canvas_cascade(other["id"])
