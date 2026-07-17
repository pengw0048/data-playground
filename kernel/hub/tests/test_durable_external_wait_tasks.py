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


def _expire_wait_lease(task_id: str) -> None:
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, task_id, with_for_update=True)
        wait.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)


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

    owners = ("winner-a", "winner-b")
    claim_barrier = threading.Barrier(len(owners))

    def claim(owner):
        claim_barrier.wait()
        return owner, metadb.claim_external_wait_transition(task["id"], owner)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, owners))
    claimed = [(owner, value) for owner, value in claims if value is not None]
    assert len(claimed) == 1
    winner, reclaimed = claimed[0]
    barrier = threading.Barrier(8)

    def commit(_index):
        barrier.wait()
        return metadb.commit_external_wait_transition(
            task["id"], reclaimed["attempt_id"], winner, handle=handle)

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

    third_identity = (identity[0], identity[1], str(uuid.uuid4()))
    third, _ = _submit(third_identity)
    with metadb.session() as session:
        attempt = session.scalar(metadb.select(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == third["id"]))
        session.delete(attempt)
    metadb.fail_corrupt_external_wait_tasks()
    assert metadb.durable_task(third["id"])["status"] == "failed"
    missing_attempt = metadb.list_workspace_runs(identity[0], run_id=third["id"])["items"][0]
    assert missing_attempt["status"] == "failed" and missing_attempt["taskAttempts"] == []


def test_database_due_backoff_budget_deadline_and_regressions(identity):
    task, _ = _submit(identity)
    claim = metadb.claim_external_wait_transition(task["id"], "submit")
    handle = {"provider_kind": "fixture-local", "job_id": "fixture-job"}
    assert metadb.commit_external_wait_transition(
        task["id"], claim["attempt_id"], "submit", handle=handle)
    assert task["id"] not in metadb.due_external_wait_task_ids()

    _make_due(task["id"])
    transient = metadb.claim_external_wait_transition(task["id"], "transient")
    assert metadb.commit_external_wait_transition(
        task["id"], transient["attempt_id"], "transient",
        failure_code="adapter_transient_failure", retry_after=999)
    waiting = metadb.durable_task(task["id"])
    assert waiting["status"] == "running"
    assert waiting["external_wait"]["phase"] == "accepted"
    assert waiting["external_wait"]["poll_count"] == 1
    next_poll = waiting["external_wait"]["next_poll_at"].replace(tzinfo=datetime.timezone.utc)
    assert 0 < (next_poll - datetime.datetime.now(datetime.timezone.utc)).total_seconds() <= 5.1
    assert task["id"] not in metadb.due_external_wait_task_ids()

    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, task["id"], with_for_update=True)
        wait.poll_count = 63
        wait.next_poll_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    budget = metadb.claim_external_wait_transition(task["id"], "budget")
    assert metadb.commit_external_wait_transition(
        task["id"], budget["attempt_id"], "budget",
        failure_code="adapter_transient_failure")
    exhausted = metadb.durable_task(task["id"])
    assert exhausted["status"] == "failed"
    assert exhausted["external_wait"]["diagnostic_code"] == "external_wait_poll_budget"

    deadline_identity = (identity[0], identity[1], str(uuid.uuid4()))
    deadline_task, _ = _submit(deadline_identity)
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, deadline_task["id"], with_for_update=True)
        wait.deadline_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        wait.next_poll_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    assert deadline_task["id"] in metadb.due_external_wait_task_ids()
    metadb.expire_external_wait_deadlines()
    expired = metadb.durable_task(deadline_task["id"])
    assert expired["status"] == "failed"
    assert expired["external_wait"]["diagnostic_code"] == "external_wait_deadline"

    regression_identity = (identity[0], identity[1], str(uuid.uuid4()))
    regression, _ = _submit(regression_identity)
    submit = metadb.claim_external_wait_transition(regression["id"], "submit-regression")
    assert metadb.commit_external_wait_transition(
        regression["id"], submit["attempt_id"], "submit-regression", handle=handle)
    for token, phase, sequence in (("accepted", "accepted", 1), ("running", "running", 2)):
        _make_due(regression["id"])
        poll = metadb.claim_external_wait_transition(regression["id"], token)
        assert metadb.commit_external_wait_transition(
            regression["id"], poll["attempt_id"], token,
            outcome={"phase": phase, "checkpoint": {"sequence": sequence, "token": token},
                     "retry": {"after_seconds": .05}, "diagnostic": None})
    _make_due(regression["id"])
    regressed = metadb.claim_external_wait_transition(regression["id"], "regressed")
    assert metadb.commit_external_wait_transition(
        regression["id"], regressed["attempt_id"], "regressed",
        outcome={"phase": "accepted", "checkpoint": {"sequence": 3, "token": "later"},
                 "retry": None, "diagnostic": None})
    assert metadb.durable_task(regression["id"])["external_wait"][
        "diagnostic_code"] == "phase_regressed"

    checkpoint_identity = (identity[0], identity[1], str(uuid.uuid4()))
    checkpoint_task, _ = _submit(checkpoint_identity)
    submit = metadb.claim_external_wait_transition(checkpoint_task["id"], "submit-checkpoint")
    assert metadb.commit_external_wait_transition(
        checkpoint_task["id"], submit["attempt_id"], "submit-checkpoint", handle=handle)
    _make_due(checkpoint_task["id"])
    first_poll = metadb.claim_external_wait_transition(checkpoint_task["id"], "checkpoint-2")
    assert metadb.commit_external_wait_transition(
        checkpoint_task["id"], first_poll["attempt_id"], "checkpoint-2",
        outcome={"phase": "accepted", "checkpoint": {"sequence": 2, "token": "two"},
                 "retry": None, "diagnostic": None})
    _make_due(checkpoint_task["id"])
    second_poll = metadb.claim_external_wait_transition(checkpoint_task["id"], "checkpoint-1")
    assert metadb.commit_external_wait_transition(
        checkpoint_task["id"], second_poll["attempt_id"], "checkpoint-1",
        outcome={"phase": "running", "checkpoint": {"sequence": 1, "token": "one"},
                 "retry": None, "diagnostic": None})
    assert metadb.durable_task(checkpoint_task["id"])["external_wait"][
        "diagnostic_code"] == "checkpoint_regressed"


def test_cancel_submit_loss_terminal_race_and_retry_fencing(identity):
    before, _ = _submit(identity)
    metadb.request_durable_task_cancel(before["id"])
    assert metadb.claim_external_wait_transition(before["id"], "never-submit") is None
    cancelled = metadb.durable_task(before["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["external_wait"]["phase"] == "cancelled_before_submit"

    race_identity = (identity[0], identity[1], str(uuid.uuid4()))
    task, _ = _submit(race_identity)
    first = metadb.claim_external_wait_transition(task["id"], "lost-submit")
    assert metadb.durable_task(task["id"])["external_wait"]["phase"] == "submitting"
    metadb.request_durable_task_cancel(task["id"])
    assert metadb.commit_external_wait_transition(
        task["id"], first["attempt_id"], "lost-submit",
        failure_code="adapter_transient_failure")
    replay = metadb.claim_external_wait_transition(task["id"], "submit-replay")
    assert replay["handle"] is None and replay["cancel_requested"] is True
    assert metadb.commit_external_wait_transition(
        task["id"], replay["attempt_id"], "submit-replay",
        handle={"provider_kind": "fixture-local", "job_id": "fixture-job"})
    terminal = metadb.claim_external_wait_transition(task["id"], "terminal-race")
    barrier = threading.Barrier(2)
    outcomes = (
        {"phase": "succeeded", "checkpoint": {"sequence": 1, "token": "success"},
         "retry": None, "diagnostic": None},
        {"phase": "cancelled", "checkpoint": {"sequence": 1, "token": "cancel"},
         "retry": None, "diagnostic": None},
    )

    def finish(outcome):
        barrier.wait()
        return metadb.commit_external_wait_transition(
            task["id"], terminal["attempt_id"], "terminal-race", outcome=outcome)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(finish, outcomes))
    assert winners.count(True) == 1
    raced = metadb.durable_task(task["id"])
    assert raced["status"] in ("done", "cancelled")
    assert raced["external_wait"]["phase"] in ("provider_succeeded", "provider_cancelled")

    if raced["status"] == "done":
        with metadb.session() as session:
            row = session.get(metadb.DurableTask, task["id"], with_for_update=True)
            row.status = "failed"
            row.error = "test retry"
    old_attempt = terminal["attempt_id"]
    action = str(uuid.uuid4())
    retried = metadb.retry_durable_task(task["id"], action)
    assert len(retried["attempts"]) == 2
    assert len(metadb.retry_durable_task(task["id"], action)["attempts"]) == 2
    assert not metadb.commit_external_wait_transition(
        task["id"], old_attempt, "terminal-race",
        handle={"provider_kind": "fixture-local", "job_id": "late"})
    second = metadb.claim_external_wait_transition(task["id"], "attempt-2")
    assert metadb.commit_external_wait_transition(
        task["id"], second["attempt_id"], "attempt-2",
        failure_code="adapter_return_invalid")
    third_action = str(uuid.uuid4())
    assert len(metadb.retry_durable_task(task["id"], third_action)["attempts"]) == 3
    assert len(metadb.retry_durable_task(task["id"], third_action)["attempts"]) == 3
    third = metadb.claim_external_wait_transition(task["id"], "attempt-3")
    assert metadb.commit_external_wait_transition(
        task["id"], third["attempt_id"], "attempt-3",
        failure_code="adapter_return_invalid")
    with pytest.raises(ValueError, match="retry limit"):
        metadb.retry_durable_task(task["id"], str(uuid.uuid4()))


def test_malformed_inactive_and_hung_adapters_stay_bounded(identity):
    malformed_identity = (identity[0], identity[1], str(uuid.uuid4()))
    malformed, _ = _submit(malformed_identity)
    claim = metadb.claim_external_wait_transition(malformed["id"], "malformed-submit")
    assert metadb.commit_external_wait_transition(
        malformed["id"], claim["attempt_id"], "malformed-submit",
        handle={"provider_kind": "fixture-local", "job_id": "malformed"})
    _make_due(malformed["id"])

    class Malformed(Adapter):
        def status(self, _handle, checkpoint=None):
            return {"phase": "unknown", "raw": "SECRET /private/provider/path"}

    external_wait_tasks.recover(_deps(Malformed()))
    malformed_final = _wait_until(
        lambda: metadb.durable_task(malformed["id"])
        if metadb.durable_task(malformed["id"])["status"] == "failed" else None)
    assert malformed_final["external_wait"]["diagnostic_code"] == "adapter_return_invalid"
    assert "SECRET" not in json.dumps(
        metadb.list_workspace_runs(identity[0], run_id=malformed["id"]))

    inactive_identity = (identity[0], identity[1], str(uuid.uuid4()))
    inactive, _ = _submit(inactive_identity)
    external_wait_tasks.recover(_deps(None))
    unavailable = _wait_until(
        lambda: metadb.durable_task(inactive["id"])
        if metadb.durable_task(inactive["id"])["external_wait"]["diagnostic_code"] else None)
    assert unavailable["status"] == "running"
    assert unavailable["external_wait"]["diagnostic_code"] == "adapter_unavailable"
    assert len(unavailable["attempts"]) == 1
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, inactive["id"], with_for_update=True)
        wait.deadline_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    external_wait_tasks.recover(_deps(None))
    expired = metadb.durable_task(inactive["id"])
    assert expired["status"] == "failed"
    assert expired["external_wait"]["diagnostic_code"] == "external_wait_deadline"
    assert len(expired["attempts"]) == 1

    hang_identity = (identity[0], identity[1], str(uuid.uuid4()))
    hanging_task, _ = _submit(hang_identity)

    class Hanging(Adapter):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()
            self.first = threading.Event()
            self.second = threading.Event()
            self.entered = 0
            self.lock = threading.Lock()

        def submit(self, request):
            with self.lock:
                self.entered += 1
                (self.second if self.entered == 2 else self.first).set()
            assert self.release.wait(timeout=5)
            return super().submit(request)

    hanging = Hanging()
    try:
        external_wait_tasks.recover(_deps(hanging))
        assert hanging.first.wait(timeout=2)
        _expire_wait_lease(hanging_task["id"])
        external_wait_tasks.recover(_deps(hanging))
        assert hanging.second.wait(timeout=2)
        _expire_wait_lease(hanging_task["id"])
        external_wait_tasks.recover(_deps(hanging))
        assert hanging.entered == 2

        healthy_identity = (identity[0], identity[1], str(uuid.uuid4()))
        healthy, _ = _submit(healthy_identity)
        external_wait_tasks.recover(_deps(Adapter()))
        _wait_until(lambda: metadb.durable_task(healthy["id"])["external_wait"]["phase"] == "accepted")
        assert len(metadb.durable_task(hanging_task["id"])["attempts"]) == 1
    finally:
        hanging.release.set()
        _wait_until(lambda: hanging_task["id"] not in external_wait_tasks._inflight)


def test_exact_route_admission_and_persistence_failure_submit_nothing(identity, monkeypatch):
    from fastapi import HTTPException
    from hub.models import Graph
    from hub.routers.runs import _external_wait_request, start_run

    uid, canvas, submission = identity
    graph = Graph.model_validate({
        "id": canvas, "version": 1,
        "nodes": [{"id": "wait", "type": "external_wait_fixture", "data": {
            "config": {"operation": "conformance.success", "documentJson": "{}"}}}],
        "edges": [],
    })

    class Counting(Adapter):
        def __init__(self):
            super().__init__()
            self.submits = 0
            self.submitted_keys = []

        def submit(self, request):
            self.submits += 1
            self.submitted_keys.append(request.idempotency_key)
            return super().submit(request)

    adapter = Counting()
    deps = SimpleNamespace(
        external_wait_nodes={"external_wait_fixture": "fixture-local"},
        _external_wait_adapter=lambda kind: adapter if kind == "fixture-local" else None)
    request = _external_wait_request(deps, graph, "wait")
    assert request.provider_kind == "fixture-local"
    status, owner = start_run(deps, graph, "wait", uid, submission_id=submission)
    assert status.status == "queued" and owner is None
    task_id = status.run_id
    _wait_until(lambda: metadb.durable_task(task_id)["external_wait"]["phase"] == "accepted")
    replay, _ = start_run(deps, graph, "wait", uid, submission_id=submission)
    with metadb.session() as session:
        key = session.get(metadb.DurableExternalWait, task_id).idempotency_key
    assert replay.run_id == task_id and adapter.submitted_keys.count(key) == 1

    invalid = graph.model_copy(deep=True)
    invalid.nodes.append(Graph.model_validate({
        "id": canvas, "version": 1,
        "nodes": [{"id": "other", "type": "external_wait_fixture", "data": {"config": {}}}],
        "edges": [],
    }).nodes[0])
    with pytest.raises(HTTPException) as exc:
        _external_wait_request(deps, invalid, "wait")
    assert exc.value.status_code == 409

    blocked_adapter = Counting()
    blocked_deps = SimpleNamespace(
        external_wait_nodes=deps.external_wait_nodes,
        _external_wait_adapter=lambda kind: blocked_adapter if kind == "fixture-local" else None)
    with pytest.raises(HTTPException) as exc:
        start_run(blocked_deps, graph, "wait", uid, submission_id=str(uuid.uuid4()),
                  input_manifest=[])
    assert exc.value.status_code == 409 and blocked_adapter.submits == 0

    def persistence_failed(**_kwargs):
        raise RuntimeError("database unavailable SECRET")

    monkeypatch.setattr(metadb, "submit_durable_external_wait_task", persistence_failed)
    with pytest.raises(RuntimeError, match="database unavailable"):
        start_run(blocked_deps, graph, "wait", uid, submission_id=str(uuid.uuid4()))
    assert blocked_adapter.submits == 0
