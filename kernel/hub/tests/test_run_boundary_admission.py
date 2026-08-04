"""Admit one exact retained local result as an opaque reusable execution boundary."""

from __future__ import annotations

import copy
import os
import shutil
import time
import uuid

import pytest
import pyarrow as pa
from fastapi.testclient import TestClient
from sqlalchemy import select

from hub import metadb
from hub.deps import get_deps
from hub.main import app
from hub.models import Graph
from hub.routers import runs as runs_router


client = TestClient(app)


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
    for _ in range(200):
        with metadb.session() as session:
            record = session.scalar(
                select(metadb.RunRecord).where(metadb.RunRecord.run_id == run_id))
            if record is not None:
                return
        time.sleep(0.05)
    pytest.fail(f"run {run_id} did not reach projected history")


def _linear_graph(canvas_id: str, source_uri: str, registration_id: str) -> dict:
    return {
        "id": canvas_id,
        "name": "Boundary admission",
        "version": 1,
        "requirements": [],
        "nodes": [
            {
                "id": "source",
                "type": "source",
                "position": {"x": 0, "y": 0},
                "data": {"title": "Events", "config": {
                    "uri": source_uri,
                    "datasetRef": {
                        "kind": "exact",
                        "datasetId": registration_id,
                        "revisionId": "1",
                    },
                }},
            },
            {
                "id": "sample",
                "type": "sample",
                "position": {"x": 200, "y": 0},
                "data": {"title": "Sample", "config": {"n": 3, "seed": 42}},
            },
            {
                "id": "filter",
                "type": "filter",
                "position": {"x": 400, "y": 0},
                "data": {"title": "Filter", "config": {
                    "predicate": "amount > 1",
                }},
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
                "id": "sample-filter",
                "source": "sample",
                "target": "filter",
                "sourceHandle": "out",
                "targetHandle": "in",
                "data": {"wire": "dataset"},
            },
        ],
    }


def _fan_in_graph(canvas_id: str, source_uri: str, registration_id: str) -> dict:
    graph = _linear_graph(canvas_id, source_uri, registration_id)
    graph["nodes"].append({
        "id": "right",
        "type": "source",
        "position": {"x": 200, "y": 220},
        "data": {"title": "Right", "config": {
            "uri": source_uri,
            "datasetRef": {
                "kind": "exact",
                "datasetId": registration_id,
                "revisionId": "1",
            },
        }},
    })
    graph["nodes"].append({
        "id": "join",
        "type": "join",
        "position": {"x": 440, "y": 80},
        "data": {"title": "Join", "config": {"how": "inner", "on": "event"}},
    })
    graph["edges"] = [
        {
            "id": "source-sample",
            "source": "source",
            "target": "sample",
            "sourceHandle": "out",
            "targetHandle": "in",
            "data": {"wire": "dataset"},
        },
        {
            "id": "sample-join",
            "source": "sample",
            "target": "join",
            "sourceHandle": "out",
            "targetHandle": "left",
            "data": {"wire": "dataset"},
        },
        {
            "id": "right-join",
            "source": "right",
            "target": "join",
            "sourceHandle": "out",
            "targetHandle": "right",
            "data": {"wire": "dataset"},
        },
    ]
    return graph


@pytest.fixture
def retained_intermediate(tmp_path):
    lance = pytest.importorskip("lance")
    canvas_id = f"boundary-{uuid.uuid4().hex}"
    source_uri = str(tmp_path / f"{canvas_id}.lance")
    lance.write_dataset(pa.table({
        "event": ["view", "purchase", "view", "purchase"],
        "amount": [1, 2, 3, 4],
    }), source_uri)
    registered = client.post("/api/catalog/register", json={
        "uri": source_uri, "name": f"exact-{uuid.uuid4().hex}",
    })
    assert registered.status_code == 200, registered.text
    registration_id = registered.json()["registrationId"]
    graph = _linear_graph(canvas_id, source_uri, registration_id)
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Boundary admission"))
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


def test_boundary_admission_selects_nearest_retained_intermediate(retained_intermediate):
    graph, run_id, output = retained_intermediate

    response = client.post("/api/run/boundary-admission", json={
        "graph": graph,
        "targetNodeId": "filter",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admitted"] is True
    assert body["reason"] == "admitted"
    assert body["targetNodeId"] == "filter"
    assert body["boundary"] == {
        "canvasId": graph["id"],
        "targetNodeId": "filter",
        "boundaryNodeId": "sample",
        "boundaryPortId": "out",
        "boundaryRunId": run_id,
        "boundaryExecutionManifestSha256": metadb.canvas_result_latest(
            graph["id"])[0]["result_execution_manifest_sha256"],
    }
    assert "uri" not in body
    assert "artifactUri" not in body
    assert "uri" not in (body.get("boundary") or {})
    assert output["uri"] not in response.text


def test_boundary_admission_rejects_client_artifact_uri(retained_intermediate):
    graph, _run_id, output = retained_intermediate

    response = client.post("/api/run/boundary-admission", json={
        "graph": graph,
        "targetNodeId": "filter",
        "artifactUri": output["uri"],
    })

    assert response.status_code == 422, response.text


def test_boundary_admission_refuses_fan_in(retained_intermediate):
    graph, _run_id, _output = retained_intermediate
    source = graph["nodes"][0]["data"]["config"]
    fan_in = _fan_in_graph(
        graph["id"], source["uri"], source["datasetRef"]["datasetId"])

    response = client.post("/api/run/boundary-admission", json={
        "graph": fan_in,
        "targetNodeId": "join",
    })

    assert response.status_code == 200, response.text
    assert response.json() == {
        "admitted": False,
        "reason": "not_linear",
        "targetNodeId": "join",
        "boundary": None,
        "message": "reusable boundaries require a linear target cone",
    }


def test_boundary_admission_rejects_semantic_drift(retained_intermediate):
    graph, _run_id, _output = retained_intermediate

    changed = copy.deepcopy(graph)
    changed["nodes"][1]["data"]["config"]["seed"] = 99
    stale = client.post("/api/run/boundary-admission", json={
        "graph": changed,
        "targetNodeId": "filter",
    })
    assert stale.status_code == 200, stale.text
    assert stale.json()["admitted"] is False
    assert stale.json()["reason"] == "stale"

    wrong_port = client.post("/api/run/boundary-admission", json={
        "graph": graph,
        "targetNodeId": "source",
    })
    assert wrong_port.status_code == 200, wrong_port.text
    assert wrong_port.json()["admitted"] is False
    assert wrong_port.json()["reason"] == "no_candidate"


def test_boundary_admission_rejects_expired_artifact(retained_intermediate):
    graph, _run_id, output = retained_intermediate
    os.unlink(output["uri"])

    response = client.post("/api/run/boundary-admission", json={
        "graph": graph,
        "targetNodeId": "filter",
    })

    assert response.status_code == 200, response.text
    assert response.json()["admitted"] is False
    assert response.json()["reason"] in {"expired", "stale", "no_candidate"}


def test_local_run_persists_and_revalidates_boundary_after_restart(retained_intermediate):
    graph, boundary_run_id, output = retained_intermediate

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    downstream = _wait(started.json()["runId"])
    assert downstream["status"] == "done", downstream
    run_id = downstream["runId"]

    persisted = metadb.run_boundary_admission(run_id, include_artifact_uri=True)
    assert persisted is not None
    assert persisted["boundary_run_id"] == boundary_run_id
    assert persisted["boundary_node_id"] == "sample"
    assert persisted["boundary_port_id"] == "out"
    assert persisted["artifact_uri"] == metadb._local_result_candidate(output["uri"])
    assert persisted["revalidated_at"] is not None

    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "run_boundary_admission",
            metadb.LocalResultReference.owner_key == run_id,
        )))
    assert [ref.uri for ref in refs] == [persisted["artifact_uri"]]

    # Hub restart: forget process state and re-read the durable admission identity.
    get_deps().run_index.clear()
    revived = metadb.run_boundary_admission(run_id, include_artifact_uri=True)
    assert revived == persisted
    ok, reason, message = runs_router._revalidate_reusable_execution_boundary(
        Graph.model_validate(graph),
        graph["id"],
        metadb.DEFAULT_USER_ID,
        get_deps(),
        revived,
    )
    assert ok is True and reason == "admitted" and message is None

    os.unlink(output["uri"])
    ok, reason, _message = runs_router._revalidate_reusable_execution_boundary(
        Graph.model_validate(graph),
        graph["id"],
        metadb.DEFAULT_USER_ID,
        get_deps(),
        revived,
    )
    assert ok is False
    assert reason in {"expired", "stale", "unreadable"}


def test_same_named_nodes_do_not_collide(tmp_path):
    lance = pytest.importorskip("lance")
    left_id = f"boundary-left-{uuid.uuid4().hex}"
    right_id = f"boundary-right-{uuid.uuid4().hex}"
    source_uri = str(tmp_path / f"{left_id}.lance")
    lance.write_dataset(pa.table({
        "event": ["view", "purchase"],
        "amount": [1, 2],
    }), source_uri)
    registered = client.post("/api/catalog/register", json={
        "uri": source_uri, "name": f"exact-{uuid.uuid4().hex}",
    })
    assert registered.status_code == 200, registered.text
    registration_id = registered.json()["registrationId"]

    def materialize(canvas_id: str, seed: int) -> tuple[dict, str]:
        graph = _linear_graph(canvas_id, source_uri, registration_id)
        graph["nodes"][1]["data"]["title"] = "Sample"
        graph["nodes"][1]["data"]["config"]["seed"] = seed
        with metadb.session() as session:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name=canvas_id))
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
        return graph, status["runId"]

    try:
        left_graph, left_run = materialize(left_id, 11)
        right_graph, right_run = materialize(right_id, 22)
        assert left_run != right_run

        left = client.post("/api/run/boundary-admission", json={
            "graph": left_graph, "targetNodeId": "filter",
        }).json()
        right = client.post("/api/run/boundary-admission", json={
            "graph": right_graph, "targetNodeId": "filter",
        }).json()
        assert left["admitted"] and right["admitted"]
        assert left["boundary"]["boundaryRunId"] == left_run
        assert right["boundary"]["boundaryRunId"] == right_run
        assert left["boundary"]["boundaryRunId"] != right["boundary"]["boundaryRunId"]
    finally:
        metadb.delete_canvas_cascade(left_id)
        metadb.delete_canvas_cascade(right_id)
        get_deps().catalog.unregister(source_uri)
        shutil.rmtree(source_uri, ignore_errors=True)


def test_clearing_boundary_releases_owner_refs(retained_intermediate):
    graph, boundary_run_id, output = retained_intermediate
    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    downstream = _wait(started.json()["runId"])
    run_id = downstream["runId"]
    persisted = metadb.run_boundary_admission(run_id, include_artifact_uri=True)
    assert persisted is not None
    assert persisted["boundary_run_id"] == boundary_run_id

    assert metadb.clear_run_boundary_admission(run_id) is True
    assert metadb.run_boundary_admission(run_id) is None
    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "run_boundary_admission",
            metadb.LocalResultReference.owner_key == run_id,
        )))
    assert refs == []
    assert os.path.exists(output["uri"])
