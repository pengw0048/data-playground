from __future__ import annotations

import datetime
import threading
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from hub import handoff, metadb


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


def _backend_ref(run_id: str) -> dict:
    token = uuid.uuid4().hex
    return {
        "backend": "sink-cleanup-test", "cluster_ref": "cluster",
        "attempt_id": f"attempt-{token}", "submission_id": f"submission-{token}",
        "job_uri": f"s3://sink-cleanup/{run_id}.dpjob",
        "result_uri": f"s3://sink-cleanup/{run_id}.dpresult",
        "control_address": "http://sink-cleanup:8265",
    }


def _bind(run_id: str) -> dict:
    metadb.preallocate_run_owner(run_id, "sink-writer", None)
    ref = _backend_ref(run_id)
    status = {"run_id": run_id, "status": "queued", "per_node": []}
    stored, created = metadb.bind_backend_job(run_id, ref, status)
    assert created is True and stored["attempt_id"] == ref["attempt_id"]
    return ref


def _sink_attempt(run_id: str, label: str) -> dict:
    token = uuid.uuid4().hex
    logical_uri = f"s3://sink-cleanup/{run_id}/{label}-{token}.parquet"
    return metadb.allocate_object_attempt(
        logical_uri=logical_uri, kind="sink", run_id=run_id,
        allocation_key=f"sink-cleanup-{label}-{token}",
        catalog_key_base=f"tbl_sink_cleanup_{label}_{token}",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
        write_lease_seconds=30,
    )


def _commit(handle: dict) -> None:
    parsed = urlsplit(handle["uri"])
    metadb.record_object_attempt_commit(handle["uri"], [{
        "member_id": f"member-{handle['attempt_id']}",
        "key": f"{parsed.netloc}/{parsed.path.lstrip('/')}/part-00000.parquet",
        "member_type": "unversioned_object", "size": 1, "etag": "test",
        "version_id": None, "upload_id": None, "is_latest": True, "is_commit": True,
    }])


def _attempt_states(run_id: str) -> dict[str, str]:
    with metadb.session() as session:
        return {
            row.uri: row.state for row in session.scalars(select(metadb.ObjectAttempt).where(
                metadb.ObjectAttempt.run_id == run_id,
                metadb.ObjectAttempt.kind == "sink",
            ))
        }


def _writer_leases(run_id: str) -> list[str]:
    with metadb.session() as session:
        uris = select(metadb.ObjectAttempt.uri).where(
            metadb.ObjectAttempt.run_id == run_id,
            metadb.ObjectAttempt.kind == "sink",
        )
        return list(session.scalars(select(metadb.ObjectAttemptLease.lease_id).where(
            metadb.ObjectAttemptLease.attempt_uri.in_(uris),
            metadb.ObjectAttemptLease.lease_type.in_(("write", "publish")),
        ).order_by(metadb.ObjectAttemptLease.lease_id)))


def test_active_publication_owner_terminalizes_exact_sink_writers_idempotently():
    run_id = f"sink-cleanup-active-{uuid.uuid4().hex}"
    ref = _bind(run_id)
    allocated = _sink_attempt(run_id, "allocated")
    writing = _sink_attempt(run_id, "writing")
    committed = _sink_attempt(run_id, "committed")
    terminal = _sink_attempt(run_id, "terminal")
    published = _sink_attempt(run_id, "published")
    superseded = _sink_attempt(run_id, "superseded")
    _commit(committed)
    assert metadb.mark_object_attempt_terminal(terminal["uri"]) is True
    with metadb.session() as session:
        session.get(metadb.ObjectAttempt, allocated["uri"]).state = "allocated"
        session.get(metadb.ObjectAttempt, published["uri"]).state = "published"
        session.get(metadb.ObjectAttempt, superseded["uri"]).state = "superseded"

    other_run = f"sink-cleanup-other-{uuid.uuid4().hex}"
    other = _sink_attempt(other_run, "writing")
    other_leases = _writer_leases(other_run)

    assert metadb.request_backend_quarantine(run_id, "external writer stopped") is True
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher") is True

    states = _attempt_states(run_id)
    assert states == {
        allocated["uri"]: "abandoned",
        writing["uri"]: "abandoned",
        committed["uri"]: "abandoned",
        terminal["uri"]: "abandoned",
        published["uri"]: "published",
        superseded["uri"]: "superseded",
    }
    assert _writer_leases(run_id) == []
    with metadb.session() as session:
        for uri in (allocated["uri"], writing["uri"], committed["uri"], terminal["uri"]):
            assert session.get(metadb.ObjectAttempt, uri).terminal_proof_at is not None

    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher") is True
    assert _attempt_states(run_id) == states
    assert _attempt_states(other_run) == {other["uri"]: "writing"}
    assert _writer_leases(other_run) == other_leases
    assert metadb.mark_object_attempt_terminal(other["uri"]) is True


def test_cleanup_rejects_wrong_binding_owner_intent_and_expired_lease():
    run_id = f"sink-cleanup-reject-{uuid.uuid4().hex}"
    ref = _bind(run_id)
    attempt = _sink_attempt(run_id, "reject")
    leases = _writer_leases(run_id)
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"

    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, f"wrong-{ref['attempt_id']}", "publisher") is False
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "wrong-owner") is False
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher") is False
    assert _attempt_states(run_id) == {attempt["uri"]: "writing"}
    assert _writer_leases(run_id) == leases

    assert metadb.request_backend_cancel(run_id) is True
    with metadb.session() as session:
        job = session.get(metadb.RunBackendJob, run_id)
        job.publication_lease_until = metadb._db_now(session) - datetime.timedelta(seconds=1)
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher") is False
    assert _attempt_states(run_id) == {attempt["uri"]: "writing"}
    assert _writer_leases(run_id) == leases

    assert metadb.renew_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) is True
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher") is True


def test_cleanup_rolls_back_all_sink_and_lease_changes(monkeypatch):
    run_id = f"sink-cleanup-rollback-{uuid.uuid4().hex}"
    ref = _bind(run_id)
    first = _sink_attempt(run_id, "first")
    second = _sink_attempt(run_id, "second")
    before_states = _attempt_states(run_id)
    before_leases = _writer_leases(run_id)
    assert metadb.request_backend_quarantine(run_id, "external writer stopped") is True
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"

    original = metadb._terminalize_bound_backend_sink_attempt
    calls = 0

    def fail_after_first(row, now):
        nonlocal calls
        calls += 1
        original(row, now)
        if calls == 2:
            raise ConnectionError("sink cleanup transaction failed")

    monkeypatch.setattr(metadb, "_terminalize_bound_backend_sink_attempt", fail_after_first)
    with pytest.raises(ConnectionError, match="sink cleanup transaction failed"):
        metadb.terminalize_bound_backend_sink_attempts(
            run_id, ref["attempt_id"], "publisher")

    assert _attempt_states(run_id) == before_states == {
        first["uri"]: "writing", second["uri"]: "writing",
    }
    assert _writer_leases(run_id) == before_leases
    binding = metadb.backend_job(run_id)
    assert binding["publication_state"] == "pending"

    monkeypatch.setattr(metadb, "_terminalize_bound_backend_sink_attempt", original)
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher") is True


def test_trusted_sink_set_must_attest_every_active_bound_attempt():
    run_id = f"sink-cleanup-attested-{uuid.uuid4().hex}"
    ref = _bind(run_id)
    first = _sink_attempt(run_id, "first")
    second = _sink_attempt(run_id, "second")
    before_states = _attempt_states(run_id)
    before_leases = _writer_leases(run_id)
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"

    # A trusted envelope authorizes cleanup without separate cancel/quarantine intent, but only when
    # its hash-bound physical sink set covers every active writer owned by the exact bound run.
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher",
        expected_sink_uris=[first["uri"]],
    ) is False
    assert _attempt_states(run_id) == before_states
    assert _writer_leases(run_id) == before_leases

    foreign_run = f"sink-cleanup-foreign-{uuid.uuid4().hex}"
    foreign = _sink_attempt(foreign_run, "foreign")
    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher",
        expected_sink_uris=sorted([first["uri"], second["uri"], foreign["uri"]]),
    ) is False
    with pytest.raises(ValueError, match="canonical, sorted, and unique"):
        metadb.terminalize_bound_backend_sink_attempts(
            run_id, ref["attempt_id"], "publisher",
            expected_sink_uris=[first["uri"], first["uri"]],
        )
    assert _attempt_states(run_id) == before_states
    assert _writer_leases(run_id) == before_leases

    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "publisher",
        expected_sink_uris=sorted([first["uri"], second["uri"]]),
    ) is True
    assert _attempt_states(run_id) == {
        first["uri"]: "abandoned", second["uri"]: "abandoned",
    }
    assert _writer_leases(run_id) == []
    assert _attempt_states(foreign_run) == {foreign["uri"]: "writing"}
    assert metadb.mark_object_attempt_terminal(foreign["uri"]) is True


def test_postgres_stale_precheck_cannot_cleanup_after_publication_takeover():
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    run_id = f"sink-cleanup-takeover-{uuid.uuid4().hex}"
    ref = _bind(run_id)
    attempt = _sink_attempt(run_id, "takeover")
    before_leases = _writer_leases(run_id)
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "owner-a", 30) == "claimed"

    owner_a_prechecked = threading.Event()
    release_owner_a = threading.Event()
    stale_results: list[bool] = []
    errors: list[BaseException] = []

    def stale_owner_cleanup() -> None:
        try:
            with metadb.session() as session:
                current_owner = session.get(
                    metadb.RunBackendJob, run_id).publication_owner
            if current_owner != "owner-a":
                raise AssertionError("owner A lost publication before the stale precheck")
            owner_a_prechecked.set()
            if not release_owner_a.wait(timeout=10):
                raise TimeoutError("stale owner A was not released")
            stale_results.append(metadb.terminalize_bound_backend_sink_attempts(
                run_id, ref["attempt_id"], "owner-a",
                expected_sink_uris=[attempt["uri"]],
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    stale_owner = threading.Thread(target=stale_owner_cleanup, name="stale-owner-a")
    stale_owner.start()
    try:
        assert owner_a_prechecked.wait(timeout=5)
        with metadb.session() as session:
            job = session.get(metadb.RunBackendJob, run_id)
            job.publication_lease_until = (
                metadb._db_now(session) - datetime.timedelta(seconds=1)
            )
        assert metadb.claim_backend_publication(
            run_id, ref["attempt_id"], "owner-b", 30) == "claimed"
    finally:
        release_owner_a.set()
        stale_owner.join(timeout=10)

    assert not stale_owner.is_alive()
    assert errors == [] and stale_results == [False]
    assert _attempt_states(run_id) == {attempt["uri"]: "writing"}
    assert _writer_leases(run_id) == before_leases
    with metadb.session() as session:
        assert session.get(metadb.RunBackendJob, run_id).publication_owner == "owner-b"

    assert metadb.terminalize_bound_backend_sink_attempts(
        run_id, ref["attempt_id"], "owner-b",
        expected_sink_uris=[attempt["uri"]],
    ) is True
    assert _attempt_states(run_id) == {attempt["uri"]: "abandoned"}
    assert _writer_leases(run_id) == []
