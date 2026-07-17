from __future__ import annotations

import datetime
import json
import uuid

from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app
from hub.models import RunStatus, WorkspaceRunPage


client = TestClient(app)


def _identity() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    uid = f"jobs-user-{suffix}"
    stranger = f"jobs-stranger-{suffix}"
    with metadb.session() as session:
        session.add_all([
            metadb.User(id=uid, name="Jobs researcher"),
            metadb.User(id=stranger, name="Jobs stranger"),
            metadb.Canvas(id=f"jobs-a-{suffix}", owner_id=uid, name="Alpha canvas"),
            metadb.Canvas(id=f"jobs-b-{suffix}", owner_id=uid, name="Beta canvas"),
            metadb.Canvas(id=f"jobs-secret-{suffix}", owner_id=stranger, name="Secret canvas"),
        ])
    return uid, suffix


def _live(canvas_id: str, run_id: str, status: str = "running") -> None:
    doc = RunStatus(
        run_id=run_id, status=status, target_node_id="live-node",
        placement="distributed", per_node=[],
    ).model_dump()
    doc["outputs"] = [{
        "node_id": "live-node", "port_id": "out", "port_label": "Result",
        "wire": "dataset", "publication_kind": "result", "outcome": "pending",
    }]
    with metadb.session() as session:
        session.add(metadb.RunState(
            run_id=run_id, canvas_id=canvas_id, status=status,
            doc=json.dumps(doc), created_by="test", auth_canvas_id=canvas_id,
            target_node_id="live-node",
        ))


def test_workspace_jobs_are_accessible_filtered_and_keyset_paginated():
    uid, suffix = _identity()
    alpha, beta, secret = f"jobs-a-{suffix}", f"jobs-b-{suffix}", f"jobs-secret-{suffix}"
    assert metadb.record_run(
        alpha, "failed-node", "run", "failed", error="warehouse rejected batch",
        per_node=[{"node_id": "failed-node", "status": "failed", "label": "Publish climate"}],
        run_id=f"failed-{suffix}")
    assert metadb.record_run(
        beta, None, "run", "cancelled", run_id=f"cancelled-{suffix}")
    assert metadb.record_run(
        secret, None, "run", "failed", error="must stay private", run_id=f"secret-{suffix}")
    _live(beta, f"live-{suffix}")

    first = metadb.list_workspace_runs(uid, limit=2)
    assert len(first["items"]) == 2
    assert first["hasMore"] is True and first["nextCursor"]
    second = metadb.list_workspace_runs(uid, limit=2, cursor=first["nextCursor"])
    visible = first["items"] + second["items"]
    assert {item["canvasId"] for item in visible} == {alpha, beta}
    assert all(item["canvasId"] != secret for item in visible)
    assert len({item["id"] for item in visible}) == 3

    running = metadb.list_workspace_runs(uid, status="running")
    WorkspaceRunPage.model_validate(running)
    assert [item["runId"] for item in running["items"]] == [f"live-{suffix}"]
    assert running["items"][0]["backend"] == "distributed"
    assert running["items"][0]["attempt"] == f"live-{suffix}"
    distributed = metadb.list_workspace_runs(uid, backend="distributed")
    assert [item["runId"] for item in distributed["items"]] == [f"live-{suffix}"]
    unknown = metadb.list_workspace_runs(uid, backend="unknown")
    assert f"failed-{suffix}" in {item["runId"] for item in unknown["items"]}
    text = metadb.list_workspace_runs(uid, text="warehouse rejected")
    assert [item["runId"] for item in text["items"]] == [f"failed-{suffix}"]
    label = metadb.list_workspace_runs(uid, text="publish climate")
    assert [item["runId"] for item in label["items"]] == [f"failed-{suffix}"]
    node = metadb.list_workspace_runs(uid, node_id="failed-node")
    assert [item["runId"] for item in node["items"]] == [f"failed-{suffix}"]


def test_workspace_jobs_route_enforces_visibility_and_rejects_bad_cursor():
    uid, suffix = _identity()
    canvas_id = f"jobs-a-{suffix}"
    assert metadb.record_run(
        canvas_id, None, "run", "failed", error="route failure",
        run_id=f"route-{suffix}")

    response = client.get("/api/jobs?limit=1", headers={"X-DP-User": uid})
    assert response.status_code == 200, response.text
    page = WorkspaceRunPage.model_validate(response.json())
    assert page.items and page.items[0].canvas_id == canvas_id
    bad = client.get("/api/jobs?cursor=not-a-cursor", headers={"X-DP-User": uid})
    assert bad.status_code == 422
    bad_padding = client.get("/api/jobs?cursor=a", headers={"X-DP-User": uid})
    assert bad_padding.status_code == 422
    oversized = client.get("/api/jobs?limit=101", headers={"X-DP-User": uid})
    assert oversized.status_code == 422
    future = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(days=1)).isoformat()
    empty = client.get(
        "/api/jobs", params={"after": future}, headers={"X-DP-User": uid})
    assert empty.status_code == 200 and empty.json()["items"] == []
    literal_wildcard = client.get(
        "/api/jobs", params={"q": "%"}, headers={"X-DP-User": uid})
    assert literal_wildcard.status_code == 200 and literal_wildcard.json()["items"] == []
