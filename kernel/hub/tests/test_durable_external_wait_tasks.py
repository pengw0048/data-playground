from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import os
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from hub import external_wait_tasks, metadb
from hub.external_wait import (
    ExternalWaitCheckpoint, ExternalWaitHandle, ExternalWaitPollOutcome,
    ExternalWaitRetryHint,
)


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings
    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('external-waits') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@pytest.fixture
def identity():
    suffix = uuid.uuid4().hex
    uid, canvas = f"external-user-{suffix}", f"external-canvas-{suffix}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="External researcher"))
        session.flush()
        session.add(metadb.Canvas(id=canvas, owner_id=uid, name="External wait", doc="{}"))
    return uid, canvas, submission


def _submit(identity, *, operation="conformance.success", digest="a" * 64):
    uid, canvas, submission = identity
    graph = {"id": canvas, "version": 1, "nodes": [{
        "id": "wait", "type": "external_wait_fixture",
        "data": {"config": {"operation": operation, "documentJson": "{}"}},
    }], "edges": []}
    return metadb.submit_durable_external_wait_task(
        uid=uid, canvas_id=canvas, submission_id=submission, target_node_id="wait",
        intent_sha256=digest, graph_doc=graph, provider_kind="fixture-local",
        operation=operation, document_json="{}")


def _make_due(task_id: str) -> None:
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, task_id, with_for_update=True)
        wait.next_poll_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("condition did not become true")


class Adapter:
    provider_kind = "fixture-local"

    def __init__(self):
        self.keys: set[str] = set()

    def submit(self, request):
        digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:16]
        handle = ExternalWaitHandle(
            provider_kind=self.provider_kind, job_id=f"fixture-success-{digest}")
        if request.operation.endswith("response-loss") and request.idempotency_key not in self.keys:
            self.keys.add(request.idempotency_key)
            raise ConnectionError("SECRET /private/provider/path")
        self.keys.add(request.idempotency_key)
        return handle

    def status(self, _handle, checkpoint=None):
        index = checkpoint.sequence + 1 if checkpoint else 0
        phase = ("accepted", "running", "succeeded")[min(index, 2)]
        return ExternalWaitPollOutcome(
            phase=phase, checkpoint=ExternalWaitCheckpoint(sequence=index, token=f"cp-{index}"),
            retry=None if phase == "succeeded" else ExternalWaitRetryHint(after_seconds=.05))

    def cancel(self, _handle, checkpoint=None):
        sequence = checkpoint.sequence + 1 if checkpoint else 0
        return ExternalWaitPollOutcome(
            phase="cancelled", checkpoint=ExternalWaitCheckpoint(sequence=sequence, token="cancelled"))

    def download(self, *_args):
        raise AssertionError("#408 must not download")


def _deps(adapter):
    return SimpleNamespace(_external_wait_adapter=lambda kind: adapter if kind == "fixture-local" else None)


def test_submit_response_loss_restart_and_provider_success(identity):
    task, created = _submit(identity, operation="conformance.response-loss")
    assert created and task["task_kind"] == "external_wait"
    assert metadb.claim_durable_task(task["id"], "local-owner") is None
    assert task["id"] not in metadb.recoverable_durable_task_ids()

    first = Adapter()
    external_wait_tasks.recover(_deps(first))
    _wait_until(lambda: metadb.durable_task(task["id"])["external_wait"]["diagnostic_code"])
    _make_due(task["id"])
    external_wait_tasks.recover(_deps(first))
    _wait_until(lambda: metadb.durable_task(task["id"])["external_wait"]["phase"] == "accepted")

    # A new adapter instance reconstructs the provider job from the durable handle/checkpoint.
    restarted = Adapter()
    for expected in ("accepted", "running", "provider_succeeded"):
        before = metadb.durable_task(task["id"])["external_wait"]["poll_count"]
        _make_due(task["id"])
        external_wait_tasks.recover(_deps(restarted))
        def advanced():
            current = metadb.durable_task(task["id"])
            if current["external_wait"]["poll_count"] > before:
                return current
            return None
        current = _wait_until(advanced)
        assert current["external_wait"]["phase"] == expected
    final = metadb.durable_task(task["id"])
    assert final["status"] == "done"
    assert len(final["attempts"]) == 1
    assert final["output_receipt"] is None
    assert final["status_doc"]["outputs"] == []

    page = metadb.list_workspace_runs(identity[0], run_id=task["id"])
    encoded = json.dumps(page)
    item = page["items"][0]
    assert item["externalWait"]["phase"] == "provider_succeeded"
    assert item["externalWait"]["attemptNumber"] == 1
    assert item["outputReceipt"] is None and item["outputs"] == []
    for sentinel in ("job_id", "checkpoint", "documentJson", "SECRET", "/private"):
        assert sentinel not in encoded


def test_replay_conflict_cancel_retry_and_cleanup(identity):
    task, _ = _submit(identity)
    replay, created = _submit(identity)
    assert created is False and replay["id"] == task["id"]
    with pytest.raises(metadb.DurableTaskSubmissionConflict):
        _submit(identity, digest="b" * 64)

    claim = metadb.claim_external_wait_transition(task["id"], "submit")
    assert metadb.commit_external_wait_transition(
        task["id"], claim["attempt_id"], "submit",
        handle={"provider_kind": "fixture-local", "job_id": "fixture-job"})
    metadb.request_durable_task_cancel(task["id"])
    _make_due(task["id"])
    cancel = metadb.claim_external_wait_transition(task["id"], "cancel")
    outcome = {"phase": "cancelled", "checkpoint": {"sequence": 1, "token": "cancelled"},
               "retry": None, "diagnostic": None}
    assert metadb.commit_external_wait_transition(
        task["id"], cancel["attempt_id"], "cancel", outcome=outcome)
    with metadb.session() as session:
        old_key = session.get(metadb.DurableExternalWait, task["id"]).idempotency_key
    retried = metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    assert len(retried["attempts"]) == 2
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, task["id"])
        assert wait.phase == "unsubmitted" and wait.handle_doc is None
        assert wait.idempotency_key != old_key
    claim = metadb.claim_external_wait_transition(task["id"], "finish")
    metadb.commit_external_wait_transition(
        task["id"], claim["attempt_id"], "finish", failure_code="adapter_return_invalid")
    metadb.delete_canvas_cascade(identity[1])
    with metadb.session() as session:
        assert session.get(metadb.DurableExternalWait, task["id"]) is None
        assert session.get(metadb.DurableTask, task["id"]) is None


def test_expired_and_concurrent_same_token_commits_are_fenced(identity):
    task, _ = _submit(identity)
    claim = metadb.claim_external_wait_transition(task["id"], "expired")
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, task["id"], with_for_update=True)
        wait.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    handle = {"provider_kind": "fixture-local", "job_id": "fixture-job"}
    assert not metadb.commit_external_wait_transition(
        task["id"], claim["attempt_id"], "expired", handle=handle)

    reclaimed = metadb.claim_external_wait_transition(task["id"], "winner")
    barrier = threading.Barrier(8)

    def commit(_index):
        barrier.wait()
        return metadb.commit_external_wait_transition(
            task["id"], reclaimed["attempt_id"], "winner", handle=handle)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(commit, range(8)))
    assert results.count(True) == 1


def test_missing_or_invalid_evidence_fails_closed_and_stays_listable(identity):
    task, _ = _submit(identity)
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, task["id"], with_for_update=True)
        wait.submit_request = "not-json SECRET"
    assert metadb.claim_external_wait_transition(task["id"], "corrupt") is None
    failed = metadb.durable_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["external_wait"]["diagnostic_code"] == "external_wait_evidence_invalid"
    assert "SECRET" not in json.dumps(metadb.list_workspace_runs(identity[0], run_id=task["id"]))

    second_identity = (identity[0], identity[1], str(uuid.uuid4()))
    second, _ = _submit(second_identity)
    with metadb.session() as session:
        session.delete(session.get(metadb.DurableExternalWait, second["id"]))
    metadb.fail_corrupt_external_wait_tasks()
    assert metadb.durable_task(second["id"])["status"] == "failed"
    assert metadb.list_workspace_runs(identity[0], run_id=second["id"])["items"][0]["externalWait"] is None
