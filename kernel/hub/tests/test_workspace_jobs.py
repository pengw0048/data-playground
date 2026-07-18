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


def _live(
        canvas_id: str, run_id: str, status: str = "running",
        *, progress: float | None = None,
        updated_at: datetime.datetime | None = None) -> None:
    doc = RunStatus(
        run_id=run_id, status=status, target_node_id="live-node",
        placement="distributed", per_node=[], progress=progress,
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
            updated_at=updated_at or datetime.datetime.now(datetime.timezone.utc),
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


def test_workspace_jobs_keep_the_durable_backend_attempt_after_live_state_pruning():
    uid, suffix = _identity()
    canvas_id = f"jobs-a-{suffix}"
    run_id = f"durable-attempt-{suffix}"
    assert metadb.record_run(canvas_id, None, "run", "done", run_id=run_id)
    with metadb.session() as session:
        session.add(metadb.RunBackendJob(
            run_id=run_id,
            backend="ray-jobs",
            attempt_id=f"attempt-{suffix}",
            submission_id=f"submission-{suffix}",
            job_uri=f"s3://jobs/{suffix}",
            result_uri=f"s3://results/{suffix}",
        ))

    page = metadb.list_workspace_runs(uid, run_id=run_id)

    assert page["items"][0]["backend"] == "ray-jobs"
    assert page["items"][0]["attempt"] == f"attempt-{suffix}"


def test_workspace_jobs_project_only_owned_progress_and_canonical_update_times():
    uid, suffix = _identity()
    canvas_id = f"jobs-a-{suffix}"
    live_id = f"progress-{suffix}"
    terminal_id = f"terminal-{suffix}"
    fixed = datetime.datetime(2026, 7, 18, 12, 34, 56)
    _live(canvas_id, live_id, progress=0.375, updated_at=fixed)
    assert metadb.record_run(canvas_id, None, "run", "failed", run_id=terminal_id)

    page = WorkspaceRunPage.model_validate(metadb.list_workspace_runs(uid))
    by_run = {item.run_id: item for item in page.items}
    assert by_run[live_id].progress == 0.375
    assert by_run[live_id].updated_at == "2026-07-18T12:34:56+00:00"
    assert by_run[terminal_id].progress is None
    assert by_run[terminal_id].updated_at is None

    with metadb.session() as session:
        live = session.get(metadb.RunState, live_id)
        malformed = json.loads(live.doc)
        malformed["progress"] = "nearly done"
        live.doc = json.dumps(malformed)
    malformed_page = WorkspaceRunPage.model_validate(
        metadb.list_workspace_runs(uid, run_id=live_id))
    assert malformed_page.items[0].progress is None


def test_workspace_jobs_project_task_attempt_progress_updates_and_viewer_actions():
    uid, suffix = _identity()
    viewer = f"jobs-viewer-{suffix}"
    canvas_id = f"jobs-a-{suffix}"
    submission = str(uuid.uuid4())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    graph = {
        "id": canvas_id, "version": 1,
        "nodes": [{"id": "write", "type": "write", "data": {
            "title": "result", "config": {"filename": "result.parquet"},
        }}], "edges": [],
    }
    intent = {
        "destination": {
            "logicalUri": f"/tmp/{suffix}/result.parquet", "name": "result",
            "provider": "managed-local-file",
        },
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id,
            "producer": canvas_id, "producerVersion": 1,
            "stepId": "write", "provenance": "run", "fieldMappings": [],
        }, "parents": []},
    }
    task, _ = metadb.submit_durable_local_write_task(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        target_node_id="write", intent_sha256="a" * 64,
        graph_doc=graph, input_manifest=[], write_intent=intent,
    )
    first = metadb.claim_durable_task(task["id"], "first-owner")["attempts"][-1]
    with metadb.session() as session:
        row = session.get(metadb.DurableTaskAttempt, first["id"])
        row.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    second = metadb.claim_durable_task(task["id"], "second-owner")["attempts"][-1]
    status = RunStatus(
        run_id=task["id"], status="running", target_node_id="write", progress=0.625)
    assert metadb.update_durable_task_status(
        task["id"], second["id"], second["owner_token"], status.model_dump())
    metadb.request_durable_task_cancel(task["id"])

    first_update = datetime.datetime(2026, 7, 18, 12, 0, 0)
    second_update = datetime.datetime(2026, 7, 18, 12, 5, 0)
    with metadb.session() as session:
        session.add(metadb.User(id=viewer, name="Jobs viewer"))
        session.add(metadb.CanvasShare(canvas_id=canvas_id, user_id=viewer, role="viewer"))
        task_row = session.get(metadb.DurableTask, task["id"])
        task_row.updated_at = second_update
        first_row = session.get(metadb.DurableTaskAttempt, first["id"])
        first_row.completed_at = first_update
        second_row = session.get(metadb.DurableTaskAttempt, second["id"])
        second_row.cancel_requested_at = second_update

    owner_item = WorkspaceRunPage.model_validate(
        metadb.list_workspace_runs(uid, run_id=task["id"])).items[0]
    assert owner_item.progress == 0.625
    assert owner_item.updated_at == "2026-07-18T12:05:00+00:00"
    assert owner_item.cancel_requested is True
    assert [attempt.status for attempt in owner_item.task_attempts] == ["fenced", "running"]
    assert owner_item.task_attempts[0].updated_at == "2026-07-18T12:00:00+00:00"
    assert owner_item.task_attempts[1].progress == 0.625
    assert owner_item.task_attempts[1].updated_at == "2026-07-18T12:05:00+00:00"

    viewer_item = WorkspaceRunPage.model_validate(
        metadb.list_workspace_runs(viewer, run_id=task["id"])).items[0]
    assert viewer_item.progress == owner_item.progress
    assert viewer_item.updated_at == owner_item.updated_at
    assert viewer_item.can_cancel is False and viewer_item.can_retry is False


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
