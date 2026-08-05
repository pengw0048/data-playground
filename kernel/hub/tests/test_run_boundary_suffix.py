"""Resume one local linear run from the nearest admitted retained boundary."""

from __future__ import annotations

import copy
import os
import shutil
import time
import uuid

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient
from sqlalchemy import select

from hub import metadb
from hub.deps import get_deps
from hub.main import app
from hub.models import Graph
from hub.run_boundary_suffix import (
    BoundarySuffixError,
    build_suffix_graph,
    prepare_boundary_suffix,
)


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
        "name": "Boundary resume",
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
                "data": {"title": "Filter", "config": {"predicate": "amount > 1"}},
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
            "targetHandle": "a",
            "data": {"wire": "dataset"},
        },
        {
            "id": "right-join",
            "source": "right",
            "target": "join",
            "sourceHandle": "out",
            "targetHandle": "b",
            "data": {"wire": "dataset"},
        },
    ]
    return graph


@pytest.fixture
def retained_intermediate(tmp_path):
    lance = pytest.importorskip("lance")
    canvas_id = f"resume-{uuid.uuid4().hex}"
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
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Boundary resume"))
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
    try:
        yield graph, status["runId"], output
    finally:
        metadb.delete_canvas_cascade(canvas_id)
        get_deps().catalog.unregister(source_uri)
        shutil.rmtree(source_uri, ignore_errors=True)


def test_downstream_run_reuses_nearest_boundary_without_reexecuting_prefix(
        retained_intermediate, monkeypatch):
    graph, boundary_run_id, _output = retained_intermediate
    builds: list[str] = []

    from hub.executors.engine import BuildEngine
    real_build = BuildEngine.build

    def counting_build(self, node_id, *args, **kwargs):
        builds.append(node_id)
        return real_build(self, node_id, *args, **kwargs)

    monkeypatch.setattr(BuildEngine, "build", counting_build)

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert status["reusedBoundary"]["canvasId"] == graph["id"]
    assert status["reusedBoundary"]["targetNodeId"] == "filter"
    assert status["reusedBoundary"]["boundaryNodeId"] == "sample"
    assert status["reusedBoundary"]["boundaryPortId"] == "out"
    assert status["reusedBoundary"]["boundaryRunId"] == boundary_run_id
    assert len(status["reusedBoundary"]["boundaryExecutionManifestSha256"]) == 64
    by_id = {item["nodeId"]: item for item in status["perNode"]}
    assert by_id["source"]["reused"] is True and by_id["source"]["status"] == "done"
    assert by_id["sample"]["reused"] is True and by_id["sample"]["status"] == "done"
    assert by_id["sample"]["ms"] is None
    assert by_id["filter"]["reused"] is False and by_id["filter"]["status"] == "done"
    assert by_id["filter"]["ms"] is not None
    # Preflight may touch the full cone; the worker must still execute the suffix and must not
    # time the reused prefix as a fresh step.
    assert any(node_id.startswith("__dp_boundary_ref_") for node_id in builds)
    assert "filter" in builds
    assert "uri" not in (status.get("reusedBoundary") or {})


def test_explicit_subprocess_backend_is_not_silently_switched(
        retained_intermediate, monkeypatch):
    graph, _boundary_run_id, _output = retained_intermediate
    selected = copy.deepcopy(graph)
    selected["executionBackend"] = "local-subprocess"

    def reject_local_runner(*_args, **_kwargs):
        raise AssertionError("explicit subprocess run was routed through LocalRunner")

    monkeypatch.setattr(get_deps().runner, "run", reject_local_runner)
    started = client.post("/api/run", json={
        "graph": selected,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })

    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert status.get("reusedBoundary") is None
    assert metadb.run_boundary_admission(status["runId"]) is None


def test_resumed_result_matches_full_recompute(retained_intermediate):
    graph, _boundary_run_id, output = retained_intermediate

    resumed = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert resumed.status_code == 200, resumed.text
    resumed_status = _wait(resumed.json()["runId"])
    assert resumed_status["status"] == "done", resumed_status
    assert resumed_status["reusedBoundary"] is not None
    resumed_rows = pq.read_table(resumed_status["outputs"][0]["uri"]).to_pydict()

    # Expire the retained intermediate so the next run misses admission and recomputes fully.
    os.unlink(output["uri"])
    full = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert full.status_code == 200, full.text
    full_status = _wait(full.json()["runId"])
    assert full_status["status"] == "done", full_status
    assert full_status.get("reusedBoundary") is None
    full_rows = pq.read_table(full_status["outputs"][0]["uri"]).to_pydict()
    assert resumed_rows == full_rows


def test_semantic_drift_rejects_reuse_and_runs_prefix(retained_intermediate, monkeypatch):
    graph, _boundary_run_id, _output = retained_intermediate
    builds: list[str] = []
    from hub.executors.engine import BuildEngine
    real_build = BuildEngine.build

    def counting_build(self, node_id, *args, **kwargs):
        builds.append(node_id)
        return real_build(self, node_id, *args, **kwargs)

    monkeypatch.setattr(BuildEngine, "build", counting_build)

    changed = copy.deepcopy(graph)
    changed["nodes"][1]["data"]["config"]["seed"] = 99
    started = client.post("/api/run", json={
        "graph": changed,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert status.get("reusedBoundary") is None
    assert "sample" in builds


def test_expired_before_admission_falls_back_to_full_run(retained_intermediate):
    graph, _boundary_run_id, output = retained_intermediate
    os.unlink(output["uri"])

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert status.get("reusedBoundary") is None


def test_post_admission_integrity_failure_fails_explicitly(
        retained_intermediate, monkeypatch):
    graph, _boundary_run_id, output = retained_intermediate
    import hub.run_boundary_suffix as suffix_mod
    real_prepare = suffix_mod.prepare_boundary_suffix

    def unlink_then_prepare(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        os.unlink(output["uri"])
        return result

    monkeypatch.setattr(suffix_mod, "prepare_boundary_suffix", unlink_then_prepare)

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "failed", status
    assert status["reusedBoundary"]["boundaryNodeId"] == "sample"
    assert all(output["outcome"] != "committed" for output in status["outputs"])
    assert status.get("error")


def test_fan_in_falls_back_to_full_run(retained_intermediate, monkeypatch):
    graph, _boundary_run_id, _output = retained_intermediate
    source = graph["nodes"][0]["data"]["config"]
    fan_in = _fan_in_graph(
        graph["id"], source["uri"], source["datasetRef"]["datasetId"])
    builds: list[str] = []
    from hub.executors.engine import BuildEngine
    real_build = BuildEngine.build

    def counting_build(self, node_id, *args, **kwargs):
        builds.append(node_id)
        return real_build(self, node_id, *args, **kwargs)

    monkeypatch.setattr(BuildEngine, "build", counting_build)

    started = client.post("/api/run", json={
        "graph": fan_in,
        "targetNodeId": "join",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert status.get("reusedBoundary") is None
    assert "sample" in builds


def test_resume_survives_hub_process_restart(retained_intermediate):
    graph, boundary_run_id, _output = retained_intermediate
    get_deps().run_index.clear()

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": "filter",
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
    })
    assert started.status_code == 200, started.text
    status = _wait(started.json()["runId"])
    assert status["status"] == "done", status
    assert status["reusedBoundary"]["boundaryRunId"] == boundary_run_id
    persisted = metadb.run_boundary_admission(status["runId"])
    assert persisted is not None
    assert persisted["boundary_run_id"] == boundary_run_id


def test_suffix_graph_keeps_original_manifest_identity(retained_intermediate):
    graph, _boundary_run_id, output = retained_intermediate
    deps = get_deps()
    intent = Graph.model_validate(graph)
    intent._execution_manifest_sha256 = "a" * 64
    intent._execution_manifest_doc = "{}"
    artifact_uri = metadb._local_result_candidate(output["uri"])
    assert artifact_uri is not None
    persisted = {
        "canvas_id": graph["id"],
        "target_node_id": "filter",
        "boundary_node_id": "sample",
        "boundary_port_id": "out",
        "boundary_run_id": "run-boundary",
        "boundary_execution_manifest_sha256": "b" * 64,
    }
    suffix, plan, reused, boundary = prepare_boundary_suffix(
        intent, persisted=persisted, artifact_uri=artifact_uri,
        target_node_id="filter", deps=deps)
    assert suffix._execution_manifest_sha256 == "a" * 64
    step_ids = {step.node_id for step in plan.steps}
    assert "filter" in step_ids
    assert "sample" not in step_ids and "source" not in step_ids
    assert {item.node_id for item in reused} == {"source", "sample"}
    assert all(item.reused for item in reused)
    assert boundary.boundary_node_id == "sample"
    assert list(suffix._input_artifact_uris.values()) == [artifact_uri]


def test_suffix_cut_requires_the_admitted_named_port_and_preserves_edge_contract():
    graph = Graph.model_validate({
        "id": "named-suffix",
        "name": "Named suffix",
        "version": 1,
        "requirements": [],
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "events"}}},
            {"id": "assert", "type": "assert", "data": {"config": {
                "predicate": "amount > 0", "severity": "warn",
            }}},
            {"id": "filter", "type": "filter", "data": {"config": {
                "predicate": "amount > 1",
            }}},
        ],
        "edges": [
            {
                "id": "source-assert", "source": "source", "sourceHandle": "out",
                "target": "assert", "targetHandle": "in", "data": {"wire": "dataset"},
            },
            {
                "id": "assert-filter", "source": "assert", "sourceHandle": "pass",
                "target": "filter", "targetHandle": "in", "data": {"wire": "dataset"},
            },
        ],
    })

    suffix, _ref_id, _prefix = build_suffix_graph(
        graph, boundary_node_id="assert", boundary_port_id="pass",
        artifact_uri="/tmp/named-boundary.parquet", target_node_id="filter")

    assert len(suffix.edges) == 1
    assert suffix.edges[0].source_handle == "out"
    assert suffix.edges[0].target_handle == "in"
    assert suffix.edges[0].data.wire == "dataset"
    with pytest.raises(BoundarySuffixError, match="output port"):
        build_suffix_graph(
            graph, boundary_node_id="assert", boundary_port_id="out",
            artifact_uri="/tmp/named-boundary.parquet", target_node_id="filter")
