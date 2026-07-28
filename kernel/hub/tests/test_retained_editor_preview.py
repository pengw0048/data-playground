"""Fullscreen Transform tests may consume only a current retained immediate-upstream result."""

from __future__ import annotations

import copy
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


@contextmanager
def _retained_sample(tmp_path, configure_graph=None):
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
    graph["nodes"][0]["data"]["config"] = {
        "uri": source_uri,
        "datasetRef": {
            "kind": "exact",
            "datasetId": registered.json()["registrationId"],
            "revisionId": "1",
        },
    }
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
