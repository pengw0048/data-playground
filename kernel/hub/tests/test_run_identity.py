from __future__ import annotations

import datetime
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import select

from hub import handoff, metadb
from hub.job_artifacts import RAY_JOB_CONTRACT_VERSION
from hub.models import RunBackendRef, RunStatus


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


def _canvas() -> str:
    canvas_id = f"run-identity-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID,
            name="Run identity test", version=1, doc="{}",
        ))
    return canvas_id


def _ref(run_id: str) -> dict:
    token = uuid.uuid4().hex
    return {
        "backend": "identity-test", "cluster_ref": "cluster",
        "attempt_id": f"attempt-{token}", "submission_id": f"submission-{token}",
        "job_uri": f"s3://identity-test/{run_id}.dpjob",
        "result_uri": f"s3://identity-test/{run_id}.dpresult",
        "control_address": "http://identity-test:8265",
    }


def _status(run_id: str, status: str = "queued", ref: dict | None = None) -> dict:
    doc = {"run_id": run_id, "status": status, "per_node": []}
    if ref is not None:
        doc["backend_ref"] = {
            key: ref[key] for key in ("backend", "attempt_id", "submission_id")
        }
    return doc


def _terminal_status(run_id: str, status: str, ref: dict, *, error: str | None = None) -> dict:
    return RunStatus(
        run_id=run_id, status=status, per_node=[], error=error,
        rows_processed=0, total_rows=0,
        progress=1.0 if status == "done" else None,
        backend_ref=RunBackendRef(
            backend=ref["backend"], cluster_ref=ref.get("cluster_ref"),
            submission_id=ref["submission_id"], attempt_id=ref["attempt_id"],
            job_uri=ref["job_uri"], result_uri=ref["result_uri"],
            code_ref=ref.get("code_ref"), durable=True,
        ),
    ).model_dump()


def _sink_attempt(run_id: str, *, require_live_preallocation: bool = False) -> dict:
    token = uuid.uuid4().hex
    logical_uri = f"s3://identity-test/results/{token}.parquet"
    return metadb.allocate_object_attempt(
        logical_uri=logical_uri, kind="sink", run_id=run_id,
        allocation_key=f"identity-allocation-{token}",
        catalog_key_base="tbl_identity_test",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
        write_lease_seconds=30,
        require_live_preallocation=require_live_preallocation,
    )


def test_bind_run_owner_fills_identity_once_without_reclassification():
    first_canvas, second_canvas = _canvas(), _canvas()
    run_id = f"owner-{uuid.uuid4().hex}"
    metadb.bind_run_owner(run_id, "owner-a", first_canvas)
    metadb.bind_run_owner(run_id, "owner-a", first_canvas)

    with pytest.raises(RuntimeError, match="different authorization identity"):
        metadb.bind_run_owner(run_id, "owner-b", first_canvas)
    with pytest.raises(RuntimeError, match="different authorization identity"):
        metadb.bind_run_owner(run_id, "owner-a", second_canvas)

    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert (state.created_by, state.auth_canvas_id, state.canvas_id) == (
            "owner-a", first_canvas, first_canvas)

    open_run_id = f"open-owner-{uuid.uuid4().hex}"
    metadb.save_run_state(
        open_run_id, _status(open_run_id, "running"), canvas_id="ad-hoc-graph")
    metadb.bind_run_owner(open_run_id, "open-owner", None)
    metadb.bind_run_owner(open_run_id, "open-owner", None)
    with pytest.raises(RuntimeError, match="different authorization identity"):
        metadb.bind_run_owner(open_run_id, "other-owner", None)
    with metadb.session() as session:
        state = session.get(metadb.RunState, open_run_id)
        assert (state.created_by, state.auth_canvas_id, state.canvas_id) == (
            "open-owner", None, "ad-hoc-graph")


@pytest.mark.parametrize(
    "conflict", ["status", "creator", "auth-canvas", "canvas", "cleared"])
def test_bind_run_owner_never_overwrites_terminal_fence_identity(conflict):
    canvas_id, other_canvas = _canvas(), _canvas()
    run_id = f"fence-conflict-{conflict}-{uuid.uuid4().hex}"
    fence_identity = {
        "created_by": "other-owner" if conflict == "creator" else None,
        "auth_canvas_id": other_canvas if conflict == "auth-canvas" else None,
        "canvas_id": {"canvas": other_canvas, "cleared": None}.get(conflict, canvas_id),
    }
    with metadb.session() as session:
        session.add(metadb.RunState(
            run_id=run_id, canvas_id=canvas_id, status="done",
            doc=json.dumps({"run_id": run_id, "status": "done"}),
        ))
        fence_status = "failed" if conflict == "status" else "done"
        session.add(metadb.RunTerminalFence(
            run_id=run_id, status=fence_status, **fence_identity,
        ))

    try:
        with pytest.raises(RuntimeError, match="terminal run fence"):
            metadb.bind_run_owner(run_id, "owner-a", canvas_id)
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            assert (state.created_by, state.auth_canvas_id, state.canvas_id) == (
                None, None, canvas_id,
            )
            assert (fence.created_by, fence.auth_canvas_id, fence.canvas_id) == (
                fence_identity["created_by"], fence_identity["auth_canvas_id"],
                fence_identity["canvas_id"],
            )
            assert fence.status == fence_status
    finally:
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            if state is not None:
                session.delete(state)
            if fence is not None:
                session.delete(fence)


def test_bind_run_owner_refreshes_state_after_concurrent_terminal_publication(monkeypatch):
    if metadb.engine().dialect.name != "sqlite":
        pytest.skip("SQLite-specific FOR UPDATE interleaving")

    canvas_id = _canvas()
    run_id = f"terminal-bind-race-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.RunState(
            run_id=run_id, canvas_id=canvas_id, status="running",
            doc=json.dumps({"run_id": run_id, "status": "running"}),
        ))

    original_backfill = metadb._backfill_terminal_fence_identity
    bind_loaded_state = threading.Event()
    terminal_published = threading.Event()
    bind_errors: list[BaseException] = []

    def pause_before_backfill(session, state):
        bind_loaded_state.set()
        if not terminal_published.wait(timeout=10):
            raise AssertionError("terminal publication did not reach the bind race window")
        assert state.status == "running"
        return original_backfill(session, state)

    monkeypatch.setattr(metadb, "_backfill_terminal_fence_identity", pause_before_backfill)

    def bind_owner():
        try:
            metadb.bind_run_owner(run_id, "owner-a", canvas_id)
        except BaseException as exc:  # noqa: BLE001 - propagate thread failures to the test
            bind_errors.append(exc)

    thread = threading.Thread(target=bind_owner)
    thread.start()
    try:
        assert bind_loaded_state.wait(timeout=10)
        metadb.save_run_state(
            run_id, _status(run_id, "done"), canvas_id=canvas_id)
        assert metadb.terminal_run_identity(run_id) == (None, None, canvas_id)
    finally:
        terminal_published.set()
        thread.join(timeout=10)

    try:
        assert not thread.is_alive()
        assert bind_errors == []
        assert metadb.terminal_run_identity(run_id) == (
            "owner-a", canvas_id, canvas_id,
        )
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            assert (state.status, state.created_by, state.auth_canvas_id, state.canvas_id) == (
                "done", "owner-a", canvas_id, canvas_id,
            )
    finally:
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            if state is not None:
                session.delete(state)
            if fence is not None:
                session.delete(fence)


def test_preallocation_discard_requires_exact_identity_and_abandons_attempt():
    canvas_id, other_canvas = _canvas(), _canvas()
    run_id = f"discard-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "owner", canvas_id)
    attempt = _sink_attempt(run_id)

    metadb.reap_orphaned_runs()
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state is not None and state.preallocation_token == token, (
            "a future preallocation lease was reaped")
    assert metadb.renew_run_preallocation(run_id, "wrong-token") is False
    assert metadb.discard_run_preallocation(
        run_id, "wrong-token", "owner", canvas_id) is False
    assert metadb.discard_run_preallocation(
        run_id, token, "wrong-owner", canvas_id) is False
    assert metadb.discard_run_preallocation(
        run_id, token, "owner", other_canvas) is False
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_id) is not None
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "writing"

    assert metadb.discard_run_preallocation(run_id, token, "owner", canvas_id) is True
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_id) is None
        assert session.get(metadb.RunTerminalFence, run_id).status == "failed"
        object_row = session.get(metadb.ObjectAttempt, attempt["uri"])
        assert object_row.state == "abandoned" and object_row.terminal_proof_at is not None
        assert not list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == attempt["uri"])))
    with pytest.raises(metadb.TerminalRunIdError, match="already terminal"):
        metadb.preallocate_run_owner(run_id, "owner", canvas_id)


def test_backend_bind_consumes_lease_and_preserves_operational_canvas():
    operational_canvas, wrong_canvas = _canvas(), _canvas()
    run_id = f"backend-bind-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "open-owner", None)
    attempt = _sink_attempt(run_id)
    ref = _ref(run_id)
    status = _status(run_id, ref=ref)

    stored, created = metadb.bind_backend_job(
        run_id, ref, status, canvas_id=operational_canvas)
    assert created is True and stored["attempt_id"] == ref["attempt_id"]
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state.canvas_id == operational_canvas
        assert state.auth_canvas_id is None and state.created_by == "open-owner"
        assert state.preallocation_token is None
        assert state.preallocation_expires_at is None

    bad_status = _status(run_id, ref={**ref, "attempt_id": "wrong-attempt"})
    with pytest.raises(RuntimeError, match="does not match the durable backend binding"):
        metadb.finish_run_preallocation(run_id, token, bad_status)
    assert metadb.finish_run_preallocation(run_id, token, status) is True
    with pytest.raises(RuntimeError, match="canvas changed"):
        metadb.bind_backend_job(run_id, ref, status, canvas_id=wrong_canvas)
    assert metadb.discard_run_preallocation(
        run_id, token, "open-owner", None) is False
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "writing"


def test_backend_bind_rejects_wrong_authorization_canvas_without_consuming_lease():
    canvas_id, wrong_canvas = _canvas(), _canvas()
    run_id = f"wrong-canvas-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "owner", canvas_id)
    ref = _ref(run_id)
    status = _status(run_id, ref=ref)

    with pytest.raises(RuntimeError, match="does not match its authorization canvas"):
        metadb.bind_backend_job(run_id, ref, status, canvas_id=wrong_canvas)
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state.preallocation_token == token
        assert session.get(metadb.RunBackendJob, run_id) is None
    assert metadb.discard_run_preallocation(run_id, token, "owner", canvas_id) is True


def test_finish_without_backend_consumes_lease_for_a_live_local_run():
    # A prebound runner that owns the run locally (Popen / unsupported-shape fallback) returns a live,
    # non-terminal status with no backend binding. That must release the preallocation fence and keep
    # the run live — never fabricate a terminal failure or abandon the live writer's attempt.
    run_id = f"finish-live-local-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "owner", None)
    attempt = _sink_attempt(run_id)

    assert metadb.finish_run_preallocation(run_id, token, _status(run_id, "queued")) is True
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state is not None and state.status == "queued"
        assert state.preallocation_token is None and state.preallocation_expires_at is None
        assert session.get(metadb.RunTerminalFence, run_id) is None
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "writing"
    # A live local run left in the shared test DB would otherwise look like a boot orphan to later
    # reaper assertions; terminate it the way its in-process supervisor would.
    metadb.save_run_state(run_id, {**_status(run_id, "failed"), "error": "test cleanup"})


def test_finish_without_backend_terminalizes_attempt_on_terminal_status():
    run_id = f"finish-no-backend-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "owner", None)
    attempt = _sink_attempt(run_id)

    failed = {**_status(run_id, "failed"), "error": "unsupported before submission"}
    assert metadb.finish_run_preallocation(run_id, token, failed) is True
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state.status == "failed" and state.preallocation_token is None
        assert state.preallocation_expires_at is None
        assert session.get(metadb.RunTerminalFence, run_id).status == "failed"
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "abandoned"


def test_finish_accepts_matching_terminal_fence_after_status_detail_is_pruned(monkeypatch):
    canvas_id = _canvas()
    run_id = f"finish-pruned-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "owner", canvas_id)
    ref = _ref(run_id)
    queued = _status(run_id, ref=ref)
    envelope_sha256 = f"identity-envelope-{uuid.uuid4().hex}"
    job_payload = json.dumps({
        "run_id": run_id, "backend": ref["backend"],
        "submission_id": ref["submission_id"], "attempt_id": ref["attempt_id"],
        "envelope_sha256": envelope_sha256,
    }, sort_keys=True, separators=(",", ":")).encode()
    metadb.bind_backend_job(
        run_id, ref, queued, canvas_id=canvas_id, job_payload=job_payload)
    owner = f"publisher-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"

    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 0)
    done = _terminal_status(run_id, "done", ref)
    validated_result = {
        "contract_version": RAY_JOB_CONTRACT_VERSION,
        "attempt_id": ref["attempt_id"], "submission_id": ref["submission_id"],
        "envelope_sha256": envelope_sha256, "status": "done", "rows": 0,
        "error": None, "output_uri": None, "output_table": None, "outputs": [],
    }
    assert metadb.begin_backend_publication_effects(
        run_id, ref["attempt_id"], owner, done,
        validated_result, {}, catalog_effects=[], usage_effect=None,
    ) == "started"
    assert metadb.finish_backend_publication(
        run_id, ref["attempt_id"], owner, done) is True
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_id) is None
        assert session.get(metadb.RunBackendJob, run_id) is None
        assert session.get(metadb.RunTerminalFence, run_id).status == "done"

    assert metadb.finish_run_preallocation(run_id, token, done) is True
    assert metadb.finish_run_preallocation(run_id, token, queued) is False
    assert metadb.finish_run_preallocation(
        run_id, token, {**done, "status": "failed"}) is False

    metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as session:
        fence = session.get(metadb.RunTerminalFence, run_id)
        if fence is not None:
            session.delete(fence)


def test_expired_preallocation_is_reaped_only_without_backend():
    run_id = f"expired-{uuid.uuid4().hex}"
    token = metadb.preallocate_run_owner(run_id, "owner", None)
    attempt = _sink_attempt(run_id, require_live_preallocation=True)
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        state.preallocation_expires_at = (
            metadb._db_now(session) - datetime.timedelta(seconds=5))

    assert metadb.reap_orphaned_runs() == 1
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state.status == "failed" and state.preallocation_token is None
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "abandoned"
    assert metadb.discard_run_preallocation(run_id, token, "owner", None) is False


def test_reaped_preallocation_rejects_late_sink_allocation():
    run_id = f"late-allocation-{uuid.uuid4().hex}"
    metadb.preallocate_run_owner(run_id, "owner", None)
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        state.preallocation_expires_at = (
            metadb._db_now(session) - datetime.timedelta(seconds=5))

    assert metadb.reap_orphaned_runs() == 1
    with pytest.raises(RuntimeError, match="live unbound run preallocation"):
        _sink_attempt(run_id, require_live_preallocation=True)
    with metadb.session() as session:
        assert session.scalar(select(metadb.ObjectAttempt.uri).where(
            metadb.ObjectAttempt.run_id == run_id)) is None


@pytest.mark.parametrize("open_mode", [False, True], ids=["authorized", "open-real"])
def test_canvas_delete_waits_for_preallocated_sink_cleanup(open_mode):
    canvas_id = _canvas()
    run_id = f"delete-preallocated-{uuid.uuid4().hex}"
    auth_canvas_id = None if open_mode else canvas_id
    token = metadb.preallocate_run_owner(
        run_id, "owner", auth_canvas_id,
        operational_canvas_id=canvas_id if open_mode else None,
    )
    attempt = _sink_attempt(run_id, require_live_preallocation=True)
    if open_mode:
        with metadb.session() as session:
            session.get(metadb.RunState, run_id).status = "allocating"

    with pytest.raises(metadb.ActiveBackendJobsError, match="active run"):
        metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as session:
        assert session.get(metadb.Canvas, canvas_id) is not None
        assert session.get(metadb.RunState, run_id).preallocation_token == token
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "writing"

    assert metadb.discard_run_preallocation(
        run_id, token, "owner", auth_canvas_id) is True
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, attempt["uri"]).state == "abandoned"
    metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as session:
        assert session.get(metadb.Canvas, canvas_id) is None


def test_requested_real_canvas_delete_race_cannot_downgrade_to_ad_hoc():
    canvas_id = f"requested-race-{uuid.uuid4().hex}"
    run_id = f"requested-race-run-{uuid.uuid4().hex}"
    state = metadb.RunState(
        run_id=run_id, canvas_id=None, status="queued", doc="{}",
        created_by="owner", auth_canvas_id=None,
    )

    class SimulatedSession:
        def __init__(self, requested_exists: bool):
            self._scalars = iter((canvas_id if requested_exists else None,))

        def execute(self, _statement):
            class Result:
                @staticmethod
                def one_or_none():
                    return (None, None, None)

            return Result()

        def scalar(self, _statement):
            return next(self._scalars)

        def get(self, model, _key, **_kwargs):
            if model is metadb.Canvas:
                return None
            if model is metadb.RunState:
                return state
            raise AssertionError(f"unexpected model lock: {model}")

    with pytest.raises(RuntimeError, match="deleted during backend binding"):
        metadb._lock_existing_run_identity(
            SimulatedSession(True), run_id, requested_canvas_id=canvas_id)
    assert metadb._lock_existing_run_identity(
        SimulatedSession(False), run_id, requested_canvas_id=canvas_id) is state


def test_postgres_concurrent_backend_bind_consumes_one_preallocation():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    run_id = f"concurrent-bind-{uuid.uuid4().hex}"
    metadb.preallocate_run_owner(run_id, "owner", None)
    ref = _ref(run_id)
    status = _status(run_id, ref=ref)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def bind() -> None:
        try:
            barrier.wait(timeout=2)
            _stored, created = metadb.bind_backend_job(
                run_id, ref, status, canvas_id="ad-hoc-concurrent")
            results.append(created)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=bind) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [] and sorted(results) == [False, True]


def test_postgres_real_canvas_lock_precedes_run_state_creation():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    canvas_id = _canvas()
    run_id = f"canvas-lock-{uuid.uuid4().hex}"
    started = threading.Event()
    errors: list[BaseException] = []

    def preallocate() -> None:
        started.set()
        try:
            metadb.preallocate_run_owner(run_id, "owner", canvas_id)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    with metadb.session() as session:
        session.get(metadb.Canvas, canvas_id, with_for_update=True)
        thread = threading.Thread(target=preallocate)
        thread.start()
        assert started.wait(timeout=2)
        time.sleep(0.05)
        assert thread.is_alive(), "preallocation bypassed the real Canvas lock"
        assert session.get(metadb.RunState, run_id) is None
    thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
