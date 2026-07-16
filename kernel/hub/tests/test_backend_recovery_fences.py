from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import event, select

from hub import metadb
from hub.job_artifacts import RAY_JOB_CONTRACT_VERSION, canonical_json
from hub.models import RunOutput, RunStatus


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


def _bound_run(backend: str) -> tuple[str, str, dict]:
    token = uuid.uuid4().hex
    run_id = f"recovery-fence-{token}"
    canvas_id = f"recovery-fence-canvas-{token}"
    semantic_env: dict[str, str] = {}
    canonical_job = {
        "contract_version": RAY_JOB_CONTRACT_VERSION,
        "run_id": run_id,
        "publication_context": {
            "run_id": run_id,
            "producer": canvas_id,
            "producer_version": 1,
            "lineage_parents": {},
        },
        "graph": {"nodes": [], "edges": []},
        "target": "target",
        "source_attempts": [],
        "sink_targets": None,
        "sink_contracts": {},
        "materialize_uri": None,
        "requires": None,
        "code_ref": "recovery-fence-code",
        "cluster_ref": "recovery-fence-cluster",
        "artifact_prefix": "s3://recovery-fence",
        "workspace": "/workspace",
        "data_dir": "/workspace/data",
        "entrypoint": "python -m hub.ray_jobs_acceptance_entrypoint",
        "module": "hub.ray_jobs_acceptance",
        "semantic_env": semantic_env,
        "semantic_env_sha256": hashlib.sha256(canonical_json(semantic_env)).hexdigest(),
    }
    attempt_id = hashlib.sha256(canonical_json(canonical_job)).hexdigest()[:24]
    ref = {
        "backend": backend,
        "cluster_ref": canonical_job["cluster_ref"],
        "attempt_id": attempt_id,
        "submission_id": f"submission-{token}",
        "job_uri": f"s3://recovery-fence/{run_id}.dpjob",
        "result_uri": f"s3://recovery-fence/{run_id}.dpresult",
        "code_ref": canonical_job["code_ref"],
        "control_address": "http://recovery-fence:8265",
        "durable": True,
    }
    job = {
        **canonical_job,
        "backend": ref["backend"],
        "submission_id": ref["submission_id"],
        "attempt_id": ref["attempt_id"],
        "job_uri": ref["job_uri"],
        "result_uri": ref["result_uri"],
        "durable": True,
    }
    job["envelope_sha256"] = hashlib.sha256(canonical_json(job)).hexdigest()
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID,
            name="Recovery fence test", version=1, doc="{}",
        ))
    metadb.preallocate_run_owner(run_id, metadb.DEFAULT_USER_ID, canvas_id)
    status = _run_status(run_id, ref, "queued")
    stored, created = metadb.bind_backend_job(
        run_id, ref, status, canvas_id=canvas_id,
        job_payload=canonical_json(job),
    )
    assert created is True and stored["attempt_id"] == ref["attempt_id"]
    return run_id, canvas_id, ref


def _backend_ref(ref: dict) -> dict:
    return {
        key: ref.get(key)
        for key in (
            "backend", "cluster_ref", "submission_id", "attempt_id",
            "job_uri", "result_uri", "code_ref", "durable",
        )
    }


def _run_status(
        run_id: str, ref: dict, status: str, error: str | None = None) -> dict:
    outcome = {
        "queued": "pending",
        "running": "pending",
        "done": "committed",
        "failed": "failed",
        "cancelled": "cancelled",
    }[status]
    output_uri = f"{ref['result_uri']}.parquet" if status == "done" else None
    return RunStatus(
        run_id=run_id,
        status=status,
        target_node_id="target",
        rows_processed=0,
        total_rows=0 if status == "done" else None,
        per_node=[],
        progress=1.0 if status == "done" else None,
        error=error,
        outputs=[RunOutput(
            node_id="target",
            port_id="out",
            wire="dataset",
            publication_kind="result",
            outcome=outcome,
            uri=output_uri,
            rows=0 if status == "done" else None,
            error=error if outcome in ("failed", "cancelled") else None,
        )],
        backend_ref=_backend_ref(ref),
    ).model_dump()


def _begin_terminal(
        run_id: str, ref: dict, owner: str, status: str,
        error: str | None = None) -> tuple[str, dict]:
    terminal = _run_status(run_id, ref, status, error)
    validated_result = None
    if status == "done":
        payload = metadb.backend_job_artifact_payload(run_id)
        assert payload is not None
        job = json.loads(payload)
        validated_result = {
            "contract_version": job["contract_version"],
            "attempt_id": ref["attempt_id"],
            "submission_id": ref["submission_id"],
            "envelope_sha256": job["envelope_sha256"],
            "status": "done",
            "rows": 0,
            "error": None,
            # Private Ray v4 artifact fields are mapped once into the public RunOutput above.
            "output_uri": f"{ref['result_uri']}.parquet",
            "output_table": None,
            "outputs": [],
        }
    outcome = metadb.begin_backend_publication_effects(
        run_id, ref["attempt_id"], owner, terminal,
        validated_result, {}, [], None,
    )
    return outcome, terminal


def _publish_terminal(
        run_id: str, ref: dict, owner: str, status: str,
        error: str | None = None) -> bool:
    outcome, terminal = _begin_terminal(run_id, ref, owner, status, error)
    if outcome != "started":
        return False
    return metadb.finish_backend_publication(
        run_id, ref["attempt_id"], owner, terminal)


def _finish_failed(run_id: str, ref: dict, owner: str) -> None:
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    assert _publish_terminal(
        run_id, ref, owner, "failed", "terminal publication won") is True


def test_recovery_block_marker_updates_only_a_live_unpublished_exact_backend():
    backend = f"recovery-blocked-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    blocked = {
        "run_id": run_id, "status": "running", "per_node": [],
        "error": "malformed recovery row",
    }
    assert metadb.mark_backend_recovery_blocked(
        run_id, backend, blocked, "malformed recovery row") is True
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "running"
        assert json.loads(state.doc) == blocked
        assert job.recovery_blocked_reason == "malformed recovery row"

    _finish_failed(run_id, ref, "terminal-owner")
    assert metadb.mark_backend_recovery_blocked(
        run_id, backend, {**blocked, "status": "queued"}, "stale recovery") is False
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "failed"
        assert json.loads(state.doc)["error"] == "terminal publication won"
        assert job.publication_state == "published"
        assert job.recovery_blocked_reason is None
    metadb.delete_canvas_cascade(canvas_id)


def test_unhandled_backend_notes_only_live_unpublished_rows_and_repairs_doc_identity():
    missing = f"missing-recovery-{uuid.uuid4().hex}"
    available = f"available-recovery-{uuid.uuid4().hex}"
    missing_run, missing_canvas, missing_ref = _bound_run(missing)
    available_run, available_canvas, available_ref = _bound_run(available)
    terminal_run, terminal_canvas, terminal_ref = _bound_run(missing)
    _finish_failed(terminal_run, terminal_ref, "terminal-owner")

    with metadb.session() as session:
        state = session.get(metadb.RunState, missing_run)
        state.doc = json.dumps({
            "run_id": "wrong-run", "status": "done", "per_node": "invalid",
        })
        # This module may run after another backend suite against the same test database. Treat those
        # unrelated plugins as available so the assertion isolates only the rows created here.
        unrelated_backends = set(session.scalars(select(
            metadb.RunBackendJob.backend,
        ).where(metadb.RunBackendJob.run_id.not_in((
            missing_run, available_run, terminal_run,
        )))))
    assert metadb.note_unhandled_backend_jobs({available, *unrelated_backends}) == 1
    with metadb.session() as session:
        missing_state = session.get(metadb.RunState, missing_run)
        available_state = session.get(metadb.RunState, available_run)
        terminal_state = session.get(metadb.RunState, terminal_run)
        missing_doc = json.loads(missing_state.doc)
        assert (missing_state.status, missing_doc["run_id"], missing_doc["status"]) == (
            "queued", missing_run, "queued")
        assert missing_doc["per_node"] == []
        assert "unavailable in this process" in missing_doc["error"]
        assert json.loads(available_state.doc).get("error") is None
        assert json.loads(terminal_state.doc)["error"] == "terminal publication won"
        assert session.get(
            metadb.RunBackendJob, terminal_run).publication_state == "published"

    _finish_failed(missing_run, missing_ref, "missing-terminal-owner")
    _finish_failed(available_run, available_ref, "available-terminal-owner")
    for canvas_id in (missing_canvas, available_canvas, terminal_canvas):
        metadb.delete_canvas_cascade(canvas_id)


def test_cancel_request_is_rejected_after_terminal_publication():
    backend = f"cancel-terminal-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    _finish_failed(run_id, ref, "terminal-owner")

    assert metadb.request_backend_cancel(run_id) is False
    with metadb.session() as session:
        job = session.get(metadb.RunBackendJob, run_id)
        assert job.publication_state == "published"
        assert job.cancel_requested is False
    metadb.delete_canvas_cascade(canvas_id)


def test_quarantine_only_allows_failed_terminal_publication():
    backend = f"quarantine-terminal-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"quarantine-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    assert metadb.request_backend_quarantine(run_id, "invalid artifact") is True

    outcome, _terminal = _begin_terminal(run_id, ref, owner, "done")
    assert outcome == "quarantined"
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "queued"
        assert job.publication_state == "pending"
        assert job.quarantine_reason == "invalid artifact"

    assert _publish_terminal(
        run_id, ref, owner, "failed", "artifact quarantine won") is True
    assert metadb.request_backend_quarantine(run_id, "late overwrite") is False
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "failed"
        assert job.publication_state == "published"
        assert job.quarantine_reason == "invalid artifact"
    metadb.delete_canvas_cascade(canvas_id)


def test_effects_started_fences_missing_submission_claim():
    backend = f"effects-submit-fence-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    outcome, terminal = _begin_terminal(
        run_id, ref, owner, "failed", "terminal effects own recovery")
    assert outcome == "started"

    assert metadb.claim_backend_submission_after_missing(
        run_id, ref["attempt_id"], "late-submitter", 30) == "busy"
    with metadb.session() as session:
        job = session.get(metadb.RunBackendJob, run_id)
        assert job.publication_state == "effects_started"
        assert job.submission_state == "queued"
        assert job.submission_owner is None

    assert metadb.finish_backend_publication(
        run_id, ref["attempt_id"], owner, terminal) is True
    metadb.delete_canvas_cascade(canvas_id)


def test_effects_started_fences_stop_fence_claim():
    backend = f"effects-stop-fence-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    assert metadb.note_backend_submission_observed(
        run_id, ref["attempt_id"]) is True
    assert metadb.request_backend_cancel(run_id) is True

    publication_owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], publication_owner, 30) == "claimed"
    outcome, terminal = _begin_terminal(
        run_id, ref, publication_owner, "cancelled", "cancel intent won")
    assert outcome == "started"

    assert metadb.claim_backend_stop_fence(
        run_id, ref["attempt_id"], "late-stop-owner", 30) == "busy"
    with metadb.session() as session:
        job = session.get(metadb.RunBackendJob, run_id)
        assert job.publication_state == "effects_started"
        assert job.submission_state == "submitted"
        assert job.submission_owner is None

    assert metadb.finish_backend_publication(
        run_id, ref["attempt_id"], publication_owner, terminal) is True
    metadb.delete_canvas_cascade(canvas_id)


def test_live_stop_control_blocks_publication_until_terminal_proof():
    backend = f"stop-control-barrier-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    assert metadb.note_backend_submission_observed(
        run_id, ref["attempt_id"]) is True
    assert metadb.request_backend_cancel(run_id) is True
    assert metadb.note_backend_submission_observed(
        run_id, ref["attempt_id"]) is True
    with metadb.session() as session:
        assert session.get(metadb.RunBackendJob, run_id).submission_state == "stopping"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "stale-publisher", 30) == "submission"

    assert metadb.settle_backend_stop_control(run_id, ref["attempt_id"]) is True
    _finish_failed(run_id, ref, "terminal-owner")
    metadb.delete_canvas_cascade(canvas_id)


def test_result_reconciliation_blocks_publication_until_remote_winner_settles():
    backend = f"result-reconciliation-barrier-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    assert metadb.claim_backend_submission_after_missing(
        run_id, ref["attempt_id"], "crashed-submitter", 30) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, run_id).submission_lease_until = None
    assert metadb.claim_backend_stop_fence(
        run_id, ref["attempt_id"], "result-controller", 30,
        result_reconcile=True,
    ) == "claimed"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "stale-publisher", 30) == "submission"
    assert metadb.note_backend_submission_observed(
        run_id, ref["attempt_id"]) is True
    with metadb.session() as session:
        assert session.get(
            metadb.RunBackendJob, run_id).submission_state == "result_submitted"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "stale-publisher", 30) == "submission"

    assert metadb.settle_backend_result_reconciliation(
        run_id, ref["attempt_id"]) is True
    _finish_failed(run_id, ref, "terminal-owner")
    metadb.delete_canvas_cascade(canvas_id)


def test_settled_result_fence_keeps_provenance_without_blocking_publication():
    backend = f"settled-result-fence-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    assert metadb.claim_backend_submission_after_missing(
        run_id, ref["attempt_id"], "crashed-submitter", 30) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, run_id).submission_lease_until = None
    assert metadb.claim_backend_stop_fence(
        run_id, ref["attempt_id"], "result-controller", 30,
        result_reconcile=True,
    ) == "claimed"
    assert metadb.note_backend_stop_fence_accepted(
        run_id, ref["attempt_id"], "result-controller"
    ) is True
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "stale-publisher", 30) == "submission"

    assert metadb.settle_backend_result_reconciliation(
        run_id, ref["attempt_id"]
    ) is True
    with metadb.session() as session:
        assert session.get(
            metadb.RunBackendJob, run_id).submission_state == "result_fenced"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "terminal-owner", 30) == "claimed"
    _finish_failed(run_id, ref, "terminal-owner")
    metadb.delete_canvas_cascade(canvas_id)


def test_submission_claim_clears_expired_publication_owner_and_prevents_renewal():
    backend = f"expired-publication-owner-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "stale-publisher", 30) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, run_id).publication_lease_until = None
    assert metadb.claim_backend_submission_after_missing(
        run_id, ref["attempt_id"], "submission-winner", 30) == "claimed"
    with metadb.session() as session:
        assert session.get(metadb.RunBackendJob, run_id).publication_owner is None
    assert metadb.renew_backend_publication(
        run_id, ref["attempt_id"], "stale-publisher", 30) is False

    assert metadb.note_backend_submission_observed(
        run_id, ref["attempt_id"]) is True
    _finish_failed(run_id, ref, "terminal-owner")
    metadb.delete_canvas_cascade(canvas_id)


@pytest.mark.parametrize("recovery_helper", ["mark", "unhandled"])
def test_postgres_terminal_publication_fences_recovery_flush(recovery_helper):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    backend = f"pg-recovery-{recovery_helper}-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    outcome, terminal = _begin_terminal(
        run_id, ref, owner, "failed", "terminal publication won")
    assert outcome == "started"

    publisher_at_flush = threading.Event()
    release_publisher = threading.Event()
    helper_started = threading.Event()
    publisher_result: list[bool] = []
    helper_result: list[bool | int] = []
    errors: list[BaseException] = []

    def pause_terminal_flush(session, _flush_context, _instances) -> None:
        if threading.current_thread().name != f"publisher-{recovery_helper}":
            return
        if not any(
                isinstance(row, metadb.RunBackendJob)
                and row.run_id == run_id
                and row.publication_state == "published"
                for row in session.dirty):
            return
        publisher_at_flush.set()
        if not release_publisher.wait(timeout=10):
            raise TimeoutError("terminal publisher was not released")

    def publish() -> None:
        try:
            publisher_result.append(metadb.finish_backend_publication(
                run_id, ref["attempt_id"], owner, terminal,
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def recover() -> None:
        helper_started.set()
        try:
            if recovery_helper == "mark":
                helper_result.append(metadb.mark_backend_recovery_blocked(
                    run_id, backend,
                    {"run_id": run_id, "status": "running", "per_node": [],
                     "error": "stale recovery"},
                    "stale recovery",
                ))
            else:
                with metadb.session() as session:
                    other_backends = set(session.scalars(select(
                        metadb.RunBackendJob.backend
                    ).where(metadb.RunBackendJob.backend != backend)))
                helper_result.append(
                    metadb.note_unhandled_backend_jobs(other_backends))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    event.listen(metadb._Session.class_, "before_flush", pause_terminal_flush)
    publisher = threading.Thread(
        target=publish, name=f"publisher-{recovery_helper}")
    helper = threading.Thread(target=recover, name=f"recovery-{recovery_helper}")
    try:
        publisher.start()
        assert publisher_at_flush.wait(timeout=5)
        helper.start()
        assert helper_started.wait(timeout=2)
        time.sleep(0.1)
        assert helper.is_alive(), "recovery helper bypassed the terminal publication locks"
        release_publisher.set()
        publisher.join(timeout=10)
        helper.join(timeout=10)
    finally:
        release_publisher.set()
        event.remove(metadb._Session.class_, "before_flush", pause_terminal_flush)

    assert not publisher.is_alive() and not helper.is_alive()
    assert errors == []
    assert publisher_result == [True]
    assert helper_result == ([False] if recovery_helper == "mark" else [0])
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        doc = json.loads(state.doc)
        assert (state.status, doc["status"], doc["error"]) == (
            "failed", "failed", "terminal publication won")
        assert job.publication_state == "published"
        assert job.recovery_blocked_reason is None
    metadb.delete_canvas_cascade(canvas_id)


@pytest.mark.parametrize("recovery_helper", ["mark", "unhandled"])
def test_postgres_recovery_flush_precedes_and_is_overwritten_by_terminal_publication(
        recovery_helper):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    backend = f"pg-recovery-first-{recovery_helper}-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    outcome, terminal = _begin_terminal(
        run_id, ref, owner, "failed", "terminal publication won")
    assert outcome == "started"

    recovery_at_flush = threading.Event()
    release_recovery = threading.Event()
    publisher_started = threading.Event()
    publisher_result: list[bool] = []
    helper_result: list[bool | int] = []
    errors: list[BaseException] = []

    def pause_recovery_flush(session, _flush_context, _instances) -> None:
        if threading.current_thread().name != f"recovery-first-{recovery_helper}":
            return
        if not any(
                isinstance(row, metadb.RunState) and row.run_id == run_id
                for row in session.dirty):
            return
        recovery_at_flush.set()
        if not release_recovery.wait(timeout=10):
            raise TimeoutError("recovery helper was not released")

    def recover() -> None:
        try:
            if recovery_helper == "mark":
                helper_result.append(metadb.mark_backend_recovery_blocked(
                    run_id, backend,
                    {"run_id": run_id, "status": "running", "per_node": [],
                     "error": "temporary recovery fence"},
                    "temporary recovery fence",
                ))
            else:
                with metadb.session() as session:
                    other_backends = set(session.scalars(select(
                        metadb.RunBackendJob.backend
                    ).where(metadb.RunBackendJob.backend != backend)))
                helper_result.append(
                    metadb.note_unhandled_backend_jobs(other_backends))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def publish() -> None:
        publisher_started.set()
        try:
            publisher_result.append(metadb.finish_backend_publication(
                run_id, ref["attempt_id"], owner, terminal,
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    event.listen(metadb._Session.class_, "before_flush", pause_recovery_flush)
    helper = threading.Thread(
        target=recover, name=f"recovery-first-{recovery_helper}")
    publisher = threading.Thread(
        target=publish, name=f"publisher-second-{recovery_helper}")
    try:
        helper.start()
        assert recovery_at_flush.wait(timeout=5)
        publisher.start()
        assert publisher_started.wait(timeout=2)
        time.sleep(0.1)
        assert publisher.is_alive(), "terminal publisher bypassed recovery's run locks"
        release_recovery.set()
        helper.join(timeout=10)
        publisher.join(timeout=10)
    finally:
        release_recovery.set()
        event.remove(metadb._Session.class_, "before_flush", pause_recovery_flush)

    assert not helper.is_alive() and not publisher.is_alive()
    assert errors == []
    assert helper_result == ([True] if recovery_helper == "mark" else [1])
    assert publisher_result == [True]
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        doc = json.loads(state.doc)
        assert (state.status, doc["status"], doc["error"]) == (
            "failed", "failed", "terminal publication won")
        assert job.publication_state == "published"
        assert job.recovery_blocked_reason is None
    metadb.delete_canvas_cascade(canvas_id)


def test_postgres_terminal_publication_fences_late_cancel_request():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    backend = f"pg-cancel-terminal-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    outcome, terminal = _begin_terminal(
        run_id, ref, owner, "failed", "terminal publication won")
    assert outcome == "started"

    publisher_at_flush = threading.Event()
    release_publisher = threading.Event()
    cancel_started = threading.Event()
    publisher_result: list[bool] = []
    cancel_result: list[bool] = []
    errors: list[BaseException] = []

    def pause_terminal_flush(session, _flush_context, _instances) -> None:
        if threading.current_thread().name != "publisher-before-cancel":
            return
        if not any(
                isinstance(row, metadb.RunBackendJob)
                and row.run_id == run_id
                and row.publication_state == "published"
                for row in session.dirty):
            return
        publisher_at_flush.set()
        if not release_publisher.wait(timeout=10):
            raise TimeoutError("terminal publisher was not released")

    def publish() -> None:
        try:
            publisher_result.append(metadb.finish_backend_publication(
                run_id, ref["attempt_id"], owner, terminal,
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def cancel() -> None:
        cancel_started.set()
        try:
            cancel_result.append(metadb.request_backend_cancel(run_id))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    event.listen(metadb._Session.class_, "before_flush", pause_terminal_flush)
    publisher = threading.Thread(target=publish, name="publisher-before-cancel")
    canceller = threading.Thread(target=cancel, name="late-cancel")
    try:
        publisher.start()
        assert publisher_at_flush.wait(timeout=5)
        canceller.start()
        assert cancel_started.wait(timeout=2)
        time.sleep(0.1)
        assert not canceller.is_alive(), \
            "effects_started should reject late cancellation immediately"
        assert cancel_result == [False]
        release_publisher.set()
        publisher.join(timeout=10)
        canceller.join(timeout=10)
    finally:
        release_publisher.set()
        event.remove(metadb._Session.class_, "before_flush", pause_terminal_flush)

    assert not publisher.is_alive() and not canceller.is_alive()
    assert errors == []
    assert publisher_result == [True]
    assert cancel_result == [False]
    with metadb.session() as session:
        job = session.get(metadb.RunBackendJob, run_id)
        assert job.publication_state == "published"
        assert job.cancel_requested is False
    metadb.delete_canvas_cascade(canvas_id)


def test_postgres_cancel_request_can_linearize_before_terminal_publication():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    backend = f"pg-cancel-first-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"

    cancel_updated = threading.Event()
    release_cancel = threading.Event()
    publisher_started = threading.Event()
    publisher_result: list[bool] = []
    cancel_result: list[bool] = []
    errors: list[BaseException] = []

    def pause_cancel_update(
            _connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name != "cancel-before-publisher":
            return
        lowered = statement.lower()
        if "update run_backend_jobs" not in lowered or "cancel_requested" not in lowered:
            return
        cancel_updated.set()
        if not release_cancel.wait(timeout=10):
            raise TimeoutError("cancel request was not released")

    def cancel() -> None:
        try:
            cancel_result.append(metadb.request_backend_cancel(run_id))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def publish() -> None:
        publisher_started.set()
        try:
            publisher_result.append(_publish_terminal(
                run_id, ref, owner, "failed",
                "terminal publication followed cancellation",
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    event.listen(metadb.engine(), "after_cursor_execute", pause_cancel_update)
    canceller = threading.Thread(target=cancel, name="cancel-before-publisher")
    publisher = threading.Thread(target=publish, name="publisher-after-cancel")
    try:
        canceller.start()
        assert cancel_updated.wait(timeout=5)
        publisher.start()
        assert publisher_started.wait(timeout=2)
        time.sleep(0.1)
        assert publisher.is_alive(), "terminal publisher bypassed cancel's job lock"
        release_cancel.set()
        canceller.join(timeout=10)
        publisher.join(timeout=10)
    finally:
        release_cancel.set()
        event.remove(metadb.engine(), "after_cursor_execute", pause_cancel_update)

    assert not canceller.is_alive() and not publisher.is_alive()
    assert errors == []
    assert cancel_result == [True]
    assert publisher_result == [True]
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "failed"
        assert job.publication_state == "published"
        assert job.cancel_requested is True
    metadb.delete_canvas_cascade(canvas_id)


def test_postgres_terminal_publication_fences_late_quarantine():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    backend = f"pg-quarantine-terminal-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"
    outcome, terminal = _begin_terminal(run_id, ref, owner, "done")
    assert outcome == "started"

    publisher_at_flush = threading.Event()
    release_publisher = threading.Event()
    quarantine_started = threading.Event()
    publisher_result: list[bool] = []
    quarantine_result: list[bool] = []
    errors: list[BaseException] = []

    def pause_terminal_flush(session, _flush_context, _instances) -> None:
        if threading.current_thread().name != "publisher-before-quarantine":
            return
        if not any(
                isinstance(row, metadb.RunBackendJob)
                and row.run_id == run_id
                and row.publication_state == "published"
                for row in session.dirty):
            return
        publisher_at_flush.set()
        if not release_publisher.wait(timeout=10):
            raise TimeoutError("terminal publisher was not released")

    def publish() -> None:
        try:
            publisher_result.append(metadb.finish_backend_publication(
                run_id, ref["attempt_id"], owner, terminal,
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def quarantine() -> None:
        quarantine_started.set()
        try:
            quarantine_result.append(
                metadb.request_backend_quarantine(run_id, "late corruption observer"))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    event.listen(metadb._Session.class_, "before_flush", pause_terminal_flush)
    publisher = threading.Thread(target=publish, name="publisher-before-quarantine")
    quarantiner = threading.Thread(target=quarantine, name="late-quarantine")
    try:
        publisher.start()
        assert publisher_at_flush.wait(timeout=5)
        quarantiner.start()
        assert quarantine_started.wait(timeout=2)
        time.sleep(0.1)
        assert not quarantiner.is_alive(), \
            "effects_started should reject late quarantine immediately"
        assert quarantine_result == [False]
        release_publisher.set()
        publisher.join(timeout=10)
        quarantiner.join(timeout=10)
    finally:
        release_publisher.set()
        event.remove(metadb._Session.class_, "before_flush", pause_terminal_flush)

    assert not publisher.is_alive() and not quarantiner.is_alive()
    assert errors == []
    assert publisher_result == [True]
    assert quarantine_result == [False]
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "done"
        assert job.publication_state == "published"
        assert job.quarantine_reason is None
    metadb.delete_canvas_cascade(canvas_id)


def test_postgres_quarantine_fences_done_but_allows_failed_publication():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    backend = f"pg-quarantine-first-{uuid.uuid4().hex}"
    run_id, canvas_id, ref = _bound_run(backend)
    owner = f"terminal-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], owner, 30) == "claimed"

    quarantine_updated = threading.Event()
    release_quarantine = threading.Event()
    publisher_started = threading.Event()
    publisher_result: list[bool] = []
    quarantine_result: list[bool] = []
    errors: list[BaseException] = []

    def pause_quarantine_update(
            _connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name != "quarantine-before-publisher":
            return
        lowered = statement.lower()
        if "update run_backend_jobs" not in lowered or "quarantine_reason" not in lowered:
            return
        quarantine_updated.set()
        if not release_quarantine.wait(timeout=10):
            raise TimeoutError("quarantine request was not released")

    def quarantine() -> None:
        try:
            quarantine_result.append(
                metadb.request_backend_quarantine(run_id, "artifact corruption won"))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def publish_done() -> None:
        publisher_started.set()
        try:
            publisher_result.append(
                _publish_terminal(run_id, ref, owner, "done"))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    event.listen(metadb.engine(), "after_cursor_execute", pause_quarantine_update)
    quarantiner = threading.Thread(
        target=quarantine, name="quarantine-before-publisher")
    publisher = threading.Thread(target=publish_done, name="publisher-after-quarantine")
    try:
        quarantiner.start()
        assert quarantine_updated.wait(timeout=5)
        publisher.start()
        assert publisher_started.wait(timeout=2)
        time.sleep(0.1)
        assert publisher.is_alive(), "done publisher bypassed quarantine's job lock"
        release_quarantine.set()
        quarantiner.join(timeout=10)
        publisher.join(timeout=10)
    finally:
        release_quarantine.set()
        event.remove(metadb.engine(), "after_cursor_execute", pause_quarantine_update)

    assert not quarantiner.is_alive() and not publisher.is_alive()
    assert errors == []
    assert quarantine_result == [True]
    assert publisher_result == [False]
    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        job = session.get(metadb.RunBackendJob, run_id)
        assert state.status == "queued"
        assert job.publication_state == "pending"
        assert job.quarantine_reason == "artifact corruption won"

    assert _publish_terminal(
        run_id, ref, owner, "failed", "artifact quarantine won") is True
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_id).status == "failed"
        assert session.get(metadb.RunBackendJob, run_id).publication_state == "published"
    metadb.delete_canvas_cascade(canvas_id)
