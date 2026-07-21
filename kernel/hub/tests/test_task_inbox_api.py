"""Owner-scoped Inbox HTTP surface (#417)."""

from __future__ import annotations

import datetime
import uuid

from fastapi.testclient import TestClient

from hub import metadb
from hub.tests.task_manifest_helpers import with_task_manifest
from hub.main import app
from hub.models import (
    DurableTaskInboxItemView,
    DurableTaskInboxPage,
    DurableTaskInboxUnreadCount,
    RunStatus,
)


client = TestClient(app)


def _user_canvas(prefix: str):
    suffix = uuid.uuid4().hex
    uid, canvas_id = f"{prefix}-user-{suffix}", f"{prefix}-canvas-{suffix}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name=f"{prefix} researcher"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name=prefix, doc="{}"))
    return uid, canvas_id


def _submit_local(uid: str, canvas_id: str):
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
            "logicalUri": f"/tmp/{uuid.uuid4().hex}/result.parquet", "name": "result",
            "provider": "managed-local-file",
        },
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "write", "provenance": "run",
            "fieldMappings": [],
        }, "parents": []},
    }
    task, created = metadb.submit_durable_local_write_task(**with_task_manifest(dict(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        target_node_id="write", intent_sha256="a" * 64,
        graph_doc=graph, input_manifest=[], write_intent=intent)))
    assert created is True
    return task


def _fail_task(task: dict, owner_token: str) -> None:
    claimed = metadb.claim_durable_task(task["id"], owner_token)
    attempt = claimed["attempts"][-1]
    failed = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="secret boom")
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt["id"], owner_token, failed.model_dump())


def test_inbox_routes_owner_isolation_cursor_and_mark_read():
    owner_a, canvas_a = _user_canvas("api-a")
    owner_b, canvas_b = _user_canvas("api-b")
    task_a = _submit_local(owner_a, canvas_a)
    task_b = _submit_local(owner_b, canvas_b)
    _fail_task(task_a, "a")
    _fail_task(task_b, "b")

    listed = client.get("/api/inbox", headers={"X-DP-User": owner_a})
    assert listed.status_code == 200, listed.text
    page = DurableTaskInboxPage.model_validate(listed.json())
    assert len(page.items) == 1
    item = page.items[0]
    assert item.task_id == task_a["id"]
    assert item.outcome == "failed"
    assert item.job_available is True
    assert item.canvas_name == "api-a"
    body = listed.json()["items"][0]
    assert "error" not in body
    assert "ownerId" not in body
    assert "diagnosticCode" in body
    assert "executionManifestSha256" not in body
    assert "outputReceipt" not in body

    stranger = client.get("/api/inbox", headers={"X-DP-User": owner_b})
    assert {row["taskId"] for row in stranger.json()["items"]} == {task_b["id"]}

    count = client.get("/api/inbox/unread-count", headers={"X-DP-User": owner_a})
    assert DurableTaskInboxUnreadCount.model_validate(count.json()).count == 1

    denied = client.post(
        f"/api/inbox/{item.id}/read", headers={"X-DP-User": owner_b})
    assert denied.status_code == 404

    first = client.post(
        f"/api/inbox/{item.id}/read", headers={"X-DP-User": owner_a})
    assert first.status_code == 200, first.text
    marked = DurableTaskInboxItemView.model_validate(first.json())
    assert marked.read_at is not None
    second = client.post(
        f"/api/inbox/{item.id}/read", headers={"X-DP-User": owner_a})
    assert second.json()["readAt"] == first.json()["readAt"]
    assert client.get(
        "/api/inbox/unread-count", headers={"X-DP-User": owner_a}).json()["count"] == 0
    unread = client.get(
        "/api/inbox", params={"filter": "unread"}, headers={"X-DP-User": owner_a})
    assert unread.json()["items"] == []

    bad = client.get("/api/inbox?cursor=not-a-cursor", headers={"X-DP-User": owner_a})
    assert bad.status_code == 422
    missing = client.post(
        f"/api/inbox/{uuid.uuid4().hex}/read", headers={"X-DP-User": owner_a})
    assert missing.status_code == 404


def test_inbox_route_keyset_and_deleted_item_unavailable():
    uid, canvas_id = _user_canvas("api-page")
    tasks = []
    for index in range(3):
        task = _submit_local(uid, canvas_id)
        _fail_task(task, f"tok-{index}")
        tasks.append(task)
    base = datetime.datetime(2026, 7, 17, 15, 0, 0, tzinfo=datetime.timezone.utc)
    with metadb.session() as session:
        for index, task in enumerate(tasks):
            item = session.scalar(metadb.select(metadb.DurableTaskInboxItem).where(
                metadb.DurableTaskInboxItem.task_id == task["id"]))
            item.terminal_at = base + datetime.timedelta(seconds=index)

    first = client.get(
        "/api/inbox", params={"limit": 2}, headers={"X-DP-User": uid})
    page = DurableTaskInboxPage.model_validate(first.json())
    assert page.has_more and page.next_cursor
    assert [item.task_id for item in page.items] == [tasks[2]["id"], tasks[1]["id"]]
    cont = client.get(
        "/api/inbox",
        params={"limit": 2, "cursor": page.next_cursor},
        headers={"X-DP-User": uid},
    )
    assert [item["taskId"] for item in cont.json()["items"]] == [tasks[0]["id"]]
    mismatched = client.get(
        "/api/inbox",
        params={"filter": "unread", "cursor": page.next_cursor},
        headers={"X-DP-User": uid},
    )
    assert mismatched.status_code == 422

    doomed = page.items[0].id
    metadb.delete_canvas_cascade(canvas_id)
    gone = client.post(f"/api/inbox/{doomed}/read", headers={"X-DP-User": uid})
    assert gone.status_code == 404
    empty = client.get("/api/inbox", headers={"X-DP-User": uid})
    assert empty.json()["items"] == []
