"""Owner-scoped durable Task Inbox ledger (#416)."""

from __future__ import annotations

import datetime
import os
import uuid

import pytest

from hub import metadb
from hub.models import (
    DatasetRevision,
    LineagePublication,
    RunOutput,
    RunStatus,
    WriteProvenance,
    WritePublicationIdentity,
    WriteReceipt,
)


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('task-inbox') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


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
    task, created = metadb.submit_durable_local_write_task(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        target_node_id="write", intent_sha256="a" * 64,
        graph_doc=graph, input_manifest=[], write_intent=intent)
    assert created is True
    return task


def _done_status(task_id: str, key: str) -> dict:
    receipt = WriteReceipt(
        dataset_id="ds", revision_id="rev-1",
        head=DatasetRevision(dataset_id="ds", revision_id="rev-1"),
        rows=1, bytes=8, schema=[],
        publication=WritePublicationIdentity(
            provider="managed-local-file",
            logical_uri=f"/tmp/{key}.parquet",
            artifact_uri=f"/tmp/{key}.parquet",
            publish_sequence=1, idempotency_key=key, catalog_version="1"),
        provenance=WriteProvenance(
            publication=LineagePublication(idempotency_key=key, provenance="manual")),
    )
    return RunStatus(
        run_id=task_id, status="done", target_node_id="write",
        outputs=[RunOutput(
            node_id="write", port_id="out", wire="dataset",
            publication_kind="catalog", outcome="committed",
            uri=receipt.publication.artifact_uri, table="result",
            version="1", rows=1, write_receipt=receipt)],
        total_rows=1,
    ).model_dump()


def _inbox_for(owner_id: str, task_id: str) -> list[dict]:
    page = metadb.list_durable_task_inbox_items(owner_id, limit=200)
    return [item for item in page["items"] if item["task_id"] == task_id]


def _inbox_attempt_id(owner_id: str, task_id: str) -> str:
    with metadb.session() as session:
        row = session.scalar(metadb.select(metadb.DurableTaskInboxItem).where(
            metadb.DurableTaskInboxItem.owner_id == owner_id,
            metadb.DurableTaskInboxItem.task_id == task_id,
        ).limit(1))
        assert row is not None
        return row.task_attempt_id


def test_managed_local_completed_failed_cancelled_emit_one_item_each():
    uid, canvas_id = _user_canvas("mlw")

    done_task = _submit_local(uid, canvas_id)
    claimed = metadb.claim_durable_task(done_task["id"], "owner-done")
    attempt = claimed["attempts"][-1]
    key = done_task["write_intent"]["idempotencyKey"]
    assert metadb.finish_durable_task_attempt(
        done_task["id"], attempt["id"], "owner-done", _done_status(done_task["id"], key))
    # Response-loss replay reconciles the same item.
    assert metadb.finish_durable_task_attempt(
        done_task["id"], attempt["id"], "owner-done", _done_status(done_task["id"], key))
    done_items = _inbox_for(uid, done_task["id"])
    assert len(done_items) == 1
    assert done_items[0]["outcome"] == "completed"
    assert _inbox_attempt_id(uid, done_task["id"]) == attempt["id"]
    assert done_items[0]["diagnostic_code"] is None

    failed_task = _submit_local(uid, canvas_id)
    claimed = metadb.claim_durable_task(failed_task["id"], "owner-fail")
    attempt = claimed["attempts"][-1]
    failed = RunStatus(
        run_id=failed_task["id"], status="failed", target_node_id="write", error="boom")
    assert metadb.finish_durable_task_attempt(
        failed_task["id"], attempt["id"], "owner-fail", failed.model_dump())
    failed_items = _inbox_for(uid, failed_task["id"])
    assert len(failed_items) == 1 and failed_items[0]["outcome"] == "failed"
    # Raw exception text must not become a diagnostic code.
    assert failed_items[0]["diagnostic_code"] is None

    cancel_task = _submit_local(uid, canvas_id)
    metadb.request_durable_task_cancel(cancel_task["id"])
    assert metadb.claim_durable_task(cancel_task["id"], "owner-cancel") is None
    cancel_items = _inbox_for(uid, cancel_task["id"])
    assert len(cancel_items) == 1 and cancel_items[0]["outcome"] == "cancelled"


def test_stale_owner_and_progress_emit_nothing():
    uid, canvas_id = _user_canvas("stale")
    task = _submit_local(uid, canvas_id)
    claimed = metadb.claim_durable_task(task["id"], "owner")
    attempt = claimed["attempts"][-1]
    running = RunStatus(run_id=task["id"], status="running", target_node_id="write", progress=0.4)
    assert metadb.update_durable_task_status(
        task["id"], attempt["id"], "owner", running.model_dump()) is True
    assert _inbox_for(uid, task["id"]) == []

    stale = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="late")
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt["id"], "not-owner", stale.model_dump()) is False
    assert _inbox_for(uid, task["id"]) == []


def test_superseded_attempt_replay_does_not_duplicate_completed_inbox():
    """A fenced attempt replaying an identical done payload must not mint a second item."""
    uid, canvas_id = _user_canvas("supersede")
    task = _submit_local(uid, canvas_id)
    first = metadb.claim_durable_task(task["id"], "owner-1")["attempts"][-1]
    with metadb.session() as session:
        row = session.get(metadb.DurableTaskAttempt, first["id"], with_for_update=True)
        row.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    second = metadb.claim_durable_task(task["id"], "owner-2")["attempts"][-1]
    assert first["id"] != second["id"]
    key = task["write_intent"]["idempotencyKey"]
    done = _done_status(task["id"], key)
    assert metadb.finish_durable_task_attempt(task["id"], second["id"], "owner-2", done)
    assert len(_inbox_for(uid, task["id"])) == 1
    # Late response-loss replay from the fenced attempt: task-level payload matches, but the
    # attempt is not the terminalizing one.
    assert metadb.finish_durable_task_attempt(task["id"], first["id"], "owner-1", done) is True
    items = _inbox_for(uid, task["id"])
    assert len(items) == 1
    assert _inbox_attempt_id(uid, task["id"]) == second["id"]
    assert items[0]["outcome"] == "completed"


def test_lease_exhaustion_emits_failed_and_retry_keeps_prior_item():
    uid, canvas_id = _user_canvas("lease")
    task = _submit_local(uid, canvas_id)
    with metadb.session() as session:
        row = session.get(metadb.DurableTask, task["id"], with_for_update=True)
        row.max_attempts = 1
    claimed = metadb.claim_durable_task(task["id"], "owner")
    attempt = claimed["attempts"][-1]
    with metadb.session() as session:
        row = session.get(metadb.DurableTaskAttempt, attempt["id"], with_for_update=True)
        row.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    assert metadb.claim_durable_task(task["id"], "recover") is None
    items = _inbox_for(uid, task["id"])
    assert len(items) == 1
    assert items[0]["outcome"] == "failed"
    assert items[0]["diagnostic_code"] == "durable_task_attempts_exhausted"
    assert _inbox_attempt_id(uid, task["id"]) == attempt["id"]

    # Explicit retry after a non-exhausted failure keeps prior history immutable.
    task2 = _submit_local(uid, canvas_id)
    claimed = metadb.claim_durable_task(task2["id"], "owner")
    first = claimed["attempts"][-1]
    failed = RunStatus(
        run_id=task2["id"], status="failed", target_node_id="write", error="retry me")
    assert metadb.finish_durable_task_attempt(
        task2["id"], first["id"], "owner", failed.model_dump())
    retried = metadb.retry_durable_task(task2["id"], str(uuid.uuid4()))
    second = metadb.claim_durable_task(task2["id"], "owner-2")["attempts"][-1]
    key = task2["write_intent"]["idempotencyKey"]
    assert metadb.finish_durable_task_attempt(
        task2["id"], second["id"], "owner-2", _done_status(task2["id"], key))
    items = _inbox_for(uid, task2["id"])
    assert len(items) == 2
    assert {item["outcome"] for item in items} == {"failed", "completed"}
    assert all(item["read_at"] is None for item in items)
    with metadb.session() as session:
        attempt_outcomes = {
            row.task_attempt_id: row.outcome
            for row in session.scalars(metadb.select(metadb.DurableTaskInboxItem).where(
                metadb.DurableTaskInboxItem.owner_id == uid,
                metadb.DurableTaskInboxItem.task_id == task2["id"],
            ))
        }
    assert attempt_outcomes[first["id"]] == "failed"
    assert attempt_outcomes[second["id"]] == "completed"
    assert retried["attempts"][0]["id"] == first["id"]


def test_external_wait_terminals_and_corrupt_recovery_emit():
    uid, canvas_id = _user_canvas("ew")
    submission = str(uuid.uuid4())
    intent = {
        "destination": {
            "logicalUri": f"file:///tmp/{canvas_id}.parquet", "name": "external_result",
            "provider": "managed-local-file",
        },
        "mode": "create", "expectedSchema": [{"name": "value", "type": "int"}],
        "idempotencyKey": f"external-wait-test:{submission}",
        "partitions": [],
        "provenance": {"publication": {
            "idempotencyKey": f"external-wait-test:{submission}", "provenance": "manual",
        }, "parents": []},
    }
    graph = {"id": canvas_id, "version": 1, "nodes": [
        {"id": "wait", "type": "external_wait_fixture", "data": {"config": {
            "operation": "conformance.success", "documentJson": "{}",
            "outputSchema": [{"name": "value", "type": "int"}],
        }}},
        {"id": "write", "type": "write", "data": {"config": {
            "destination": intent["destination"]["logicalUri"], "mode": "create"}}},
    ], "edges": [{"id": "wait-write", "source": "wait", "target": "write",
                  "sourceHandle": "out", "targetHandle": "in"}]}

    deadline_task, _ = metadb.submit_durable_external_wait_task(
        uid=uid, canvas_id=canvas_id, submission_id=submission, target_node_id="write",
        intent_sha256="a" * 64, graph_doc=graph, provider_kind="fixture-local",
        operation="conformance.success", document_json="{}", write_intent=intent)
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, deadline_task["id"], with_for_update=True)
        wait.deadline_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    metadb.expire_external_wait_deadlines()
    items = _inbox_for(uid, deadline_task["id"])
    assert len(items) == 1
    assert items[0]["outcome"] == "failed"
    assert items[0]["diagnostic_code"] == "external_wait_deadline"

    cancel_submission = str(uuid.uuid4())
    cancel_intent = {**intent, "idempotencyKey": f"external-wait-test:{cancel_submission}"}
    cancel_intent["provenance"] = {"publication": {
        "idempotencyKey": cancel_intent["idempotencyKey"], "provenance": "manual",
    }, "parents": []}
    cancel_task, _ = metadb.submit_durable_external_wait_task(
        uid=uid, canvas_id=canvas_id, submission_id=cancel_submission, target_node_id="write",
        intent_sha256="b" * 64, graph_doc=graph, provider_kind="fixture-local",
        operation="conformance.success", document_json="{}", write_intent=cancel_intent)
    metadb.request_durable_task_cancel(cancel_task["id"])
    assert metadb.claim_external_wait_transition(cancel_task["id"], "cancel") is None
    items = _inbox_for(uid, cancel_task["id"])
    assert len(items) == 1 and items[0]["outcome"] == "cancelled"

    missing_submission = str(uuid.uuid4())
    missing_intent = {**intent, "idempotencyKey": f"external-wait-test:{missing_submission}"}
    missing_intent["provenance"] = {"publication": {
        "idempotencyKey": missing_intent["idempotencyKey"], "provenance": "manual",
    }, "parents": []}
    missing_task, _ = metadb.submit_durable_external_wait_task(
        uid=uid, canvas_id=canvas_id, submission_id=missing_submission, target_node_id="write",
        intent_sha256="c" * 64, graph_doc=graph, provider_kind="fixture-local",
        operation="conformance.success", document_json="{}", write_intent=missing_intent)
    with metadb.session() as session:
        attempt = session.scalar(metadb.select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == missing_task["id"]))
        session.delete(attempt)
    metadb.fail_corrupt_external_wait_tasks()
    recovered = metadb.durable_task(missing_task["id"])
    assert recovered["status"] == "failed"
    assert len(recovered["attempts"]) == 1
    items = _inbox_for(uid, missing_task["id"])
    assert len(items) == 1
    assert items[0]["outcome"] == "failed"
    assert items[0]["diagnostic_code"] == "external_wait_evidence_invalid"
    assert _inbox_attempt_id(uid, missing_task["id"]) == recovered["attempts"][0]["id"]


def test_owner_list_count_mark_read_and_cross_owner_isolation():
    owner_a, canvas_a = _user_canvas("owner-a")
    owner_b, canvas_b = _user_canvas("owner-b")
    task_a = _submit_local(owner_a, canvas_a)
    task_b = _submit_local(owner_b, canvas_b)
    for task, owner_token in ((task_a, "a"), (task_b, "b")):
        claimed = metadb.claim_durable_task(task["id"], owner_token)
        attempt = claimed["attempts"][-1]
        failed = RunStatus(
            run_id=task["id"], status="failed", target_node_id="write", error="x")
        assert metadb.finish_durable_task_attempt(
            task["id"], attempt["id"], owner_token, failed.model_dump())

    page_a = metadb.list_durable_task_inbox_items(owner_a)
    page_b = metadb.list_durable_task_inbox_items(owner_b)
    assert {item["task_id"] for item in page_a["items"]} == {task_a["id"]}
    assert {item["task_id"] for item in page_b["items"]} == {task_b["id"]}
    assert page_a["items"][0]["job_available"] is True
    assert page_a["items"][0]["canvas_name"] == "owner-a"
    assert metadb.count_durable_task_inbox_unread(owner_a) == 1
    item_a = page_a["items"][0]
    assert metadb.durable_task_inbox_item(owner_b, item_a["id"]) is None
    assert metadb.mark_durable_task_inbox_item_read(owner_b, item_a["id"]) is None
    first = metadb.mark_durable_task_inbox_item_read(owner_a, item_a["id"])
    second = metadb.mark_durable_task_inbox_item_read(owner_a, item_a["id"])
    assert first is not None and first["read_at"] is not None
    assert second["read_at"] == first["read_at"]
    assert metadb.count_durable_task_inbox_unread(owner_a) == 0
    unread = metadb.list_durable_task_inbox_items(owner_a, unread_only=True)
    assert unread["items"] == []


def test_inbox_keyset_cursor_filter_and_role_revocation():
    owner, canvas_id = _user_canvas("cursor-owner")
    peer = f"cursor-peer-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=peer, name="shared editor"))
    metadb.share_canvas(canvas_id, peer, "editor")

    task_ids = []
    for index in range(3):
        task = _submit_local(peer, canvas_id)
        claimed = metadb.claim_durable_task(task["id"], f"peer-{index}")
        attempt = claimed["attempts"][-1]
        failed = RunStatus(
            run_id=task["id"], status="failed", target_node_id="write", error="x")
        assert metadb.finish_durable_task_attempt(
            task["id"], attempt["id"], f"peer-{index}", failed.model_dump())
        task_ids.append(task["id"])
    base = datetime.datetime(2026, 7, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)
    with metadb.session() as session:
        for index, task_id in enumerate(task_ids):
            item = session.scalar(metadb.select(metadb.DurableTaskInboxItem).where(
                metadb.DurableTaskInboxItem.task_id == task_id))
            item.terminal_at = base + datetime.timedelta(seconds=index)

    page = metadb.list_durable_task_inbox_items(peer, limit=2)
    assert len(page["items"]) == 2 and page["has_more"] is True and page["next_cursor"]
    assert [item["task_id"] for item in page["items"]] == [task_ids[2], task_ids[1]]
    cont = metadb.list_durable_task_inbox_items(peer, limit=2, cursor=page["next_cursor"])
    assert [item["task_id"] for item in cont["items"]] == [task_ids[0]]
    assert cont["has_more"] is False

    mismatched = None
    try:
        metadb.list_durable_task_inbox_items(
            peer, cursor=page["next_cursor"], unread_only=True)
    except ValueError as exc:
        mismatched = str(exc)
    assert mismatched == "invalid Inbox cursor"
    try:
        metadb.list_durable_task_inbox_items(peer, cursor="not-a-cursor")
        raise AssertionError("expected invalid cursor")
    except ValueError as exc:
        assert str(exc) == "invalid Inbox cursor"

    before = metadb.list_durable_task_inbox_items(peer)["items"][0]
    assert before["job_available"] is True and before["canvas_name"] == "cursor-owner"
    metadb.unshare_canvas(canvas_id, peer)
    after = metadb.list_durable_task_inbox_items(peer)["items"][0]
    assert after["job_available"] is False
    assert after["canvas_name"] is None
    assert after["task_id"] == before["task_id"]
    # Shared-canvas owner never sees the peer's personal Inbox.
    assert metadb.list_durable_task_inbox_items(owner)["items"] == []


def test_canvas_delete_cascades_inbox_items():
    uid, canvas_id = _user_canvas("cascade")
    task = _submit_local(uid, canvas_id)
    claimed = metadb.claim_durable_task(task["id"], "owner")
    attempt = claimed["attempts"][-1]
    failed = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="stop")
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt["id"], "owner", failed.model_dump())
    assert len(_inbox_for(uid, task["id"])) == 1
    metadb.delete_canvas_cascade(canvas_id)
    assert metadb.list_durable_task_inbox_items(uid)["items"] == []
    assert metadb.durable_task(task["id"]) is None


def test_invalid_diagnostic_maps_to_fallback():
    assert metadb._canonical_inbox_diagnostic(
        "external_wait", "not_a_real_code", "failed") == "external_wait_failed"
    assert metadb._canonical_inbox_diagnostic(
        "external_wait", "external_wait_deadline", "failed") == "external_wait_deadline"
    assert metadb._canonical_inbox_diagnostic(
        "managed_local_write", "boom traceback", "failed") == "managed_local_write_failed"
    assert metadb._canonical_inbox_diagnostic(
        "managed_local_write", None, "failed") is None
    assert metadb._canonical_inbox_diagnostic(
        "external_wait", "external_wait_deadline", "completed") is None
