from __future__ import annotations

import uuid
from urllib.parse import urlsplit

import pytest

from hub import handoff, metadb
from hub.models import RunBackendRef, RunStatus


@pytest.fixture(scope="module", autouse=True)
def _schema():
    metadb.init_db()


def _source_attempt(logical_uri: str, allocation_key: str, run_id: str) -> dict:
    handle = metadb.allocate_object_attempt(
        logical_uri=logical_uri, kind="sink", run_id=run_id,
        allocation_key=allocation_key, catalog_key_base="tbl_backend_source",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
        write_lease_seconds=30,
    )
    parsed = urlsplit(handle["uri"])
    key = f"{parsed.netloc}/{parsed.path.lstrip('/')}/part-00000.parquet"
    metadb.record_object_attempt_commit(handle["uri"], [{
        "member_id": f"member-{handle['attempt_id']}", "key": key,
        "member_type": "unversioned_object", "size": 1, "etag": "test",
        "version_id": None, "upload_id": None, "is_latest": True, "is_commit": True,
    }])
    metadb.catalog_upsert_entry(handle["uri"], "backend-source", {
        "id": "ignored", "name": "backend-source", "uri": handle["uri"],
        "version": f"v{handle['generation']}", "columns": [], "tags": [],
    })
    return handle


def _backend_ref(run_id: str) -> dict:
    token = uuid.uuid4().hex
    return {
        "backend": "source-pin-test", "cluster_ref": "cluster",
        "attempt_id": f"attempt-{token}", "submission_id": f"submission-{token}",
        "job_uri": f"s3://source-pin/{run_id}.dpjob",
        "result_uri": f"s3://source-pin/{run_id}.dpresult",
        "control_address": "http://source-pin:8265",
    }


def _bind(run_id: str, source_uris: list[str]) -> tuple[str, dict, dict]:
    token = metadb.preallocate_run_owner(run_id, "source-reader", None)
    ref = _backend_ref(run_id)
    status = {
        "run_id": run_id, "status": "queued", "per_node": [],
        "backend_ref": {
            key: ref[key] for key in ("backend", "attempt_id", "submission_id")
        },
    }
    stored, created = metadb.bind_backend_job(
        run_id, ref, status, source_uris=source_uris)
    assert created is True and stored["attempt_id"] == ref["attempt_id"]
    return token, ref, status


def _attempt_state(uri: str) -> str:
    with metadb.session() as session:
        return session.get(metadb.ObjectAttempt, uri).state


def _staged_failed_result(run_id: str, ref: dict, owner: str, error: str) -> dict:
    candidate = RunStatus(
        run_id=run_id, status="failed", per_node=[], error=error,
        backend_ref=RunBackendRef(
            backend=ref["backend"], cluster_ref=ref.get("cluster_ref"),
            submission_id=ref["submission_id"], attempt_id=ref["attempt_id"],
            job_uri=ref["job_uri"], result_uri=ref["result_uri"],
            code_ref=ref.get("code_ref"), durable=True,
        ),
    ).model_dump()
    assert metadb.begin_backend_publication_effects(
        run_id, ref["attempt_id"], owner, candidate,
        None, {}, catalog_effects=[], usage_effect=None,
    ) == "started"
    return candidate


def test_bind_backend_sources_requires_exact_canonical_published_attempts():
    first_logical = f"s3://source-pin/{uuid.uuid4().hex}/first.parquet"
    second_logical = f"s3://source-pin/{uuid.uuid4().hex}/second.parquet"
    first = _source_attempt(first_logical, f"source-{uuid.uuid4().hex}", "source-first")
    second = _source_attempt(second_logical, f"source-{uuid.uuid4().hex}", "source-second")
    ordered = sorted([first["uri"], second["uri"]])

    duplicate_run = f"source-duplicate-{uuid.uuid4().hex}"
    duplicate_token = metadb.preallocate_run_owner(duplicate_run, "reader", None)
    duplicate_ref = _backend_ref(duplicate_run)
    with pytest.raises(ValueError, match="sorted, and unique"):
        metadb.bind_backend_job(
            duplicate_run, duplicate_ref,
            {"run_id": duplicate_run, "status": "queued", "per_node": []},
            source_uris=[ordered[0], ordered[0]],
        )
    assert metadb.discard_run_preallocation(
        duplicate_run, duplicate_token, "reader", None) is True

    unsorted_run = f"source-unsorted-{uuid.uuid4().hex}"
    unsorted_token = metadb.preallocate_run_owner(unsorted_run, "reader", None)
    unsorted_ref = _backend_ref(unsorted_run)
    with pytest.raises(ValueError, match="sorted, and unique"):
        metadb.bind_backend_job(
            unsorted_run, unsorted_ref,
            {"run_id": unsorted_run, "status": "queued", "per_node": []},
            source_uris=list(reversed(ordered)),
        )
    assert metadb.discard_run_preallocation(
        unsorted_run, unsorted_token, "reader", None) is True

    unmanaged_run = f"source-unmanaged-{uuid.uuid4().hex}"
    unmanaged_token = metadb.preallocate_run_owner(unmanaged_run, "reader", None)
    unmanaged_ref = _backend_ref(unmanaged_run)
    with pytest.raises(ValueError, match="valid attempt"):
        metadb.bind_backend_job(
            unmanaged_run, unmanaged_ref,
            {"run_id": unmanaged_run, "status": "queued", "per_node": []},
            source_uris=["s3://source-pin/plain.parquet"],
        )
    assert metadb.discard_run_preallocation(
        unmanaged_run, unmanaged_token, "reader", None) is True

    run_id = f"source-order-{uuid.uuid4().hex}"
    _token, ref, status = _bind(run_id, ordered)
    assert metadb.backend_source_pins(run_id) == [
        {"uri": uri, "generation": 1} for uri in ordered
    ]
    stored, created = metadb.bind_backend_job(
        run_id, ref, status, source_uris=ordered)
    assert created is False and stored["attempt_id"] == ref["attempt_id"]
    with pytest.raises(RuntimeError, match="source generations changed"):
        metadb.bind_backend_job(run_id, ref, status, source_uris=[ordered[0]])

    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"
    terminal = _staged_failed_result(run_id, ref, "publisher", "test cleanup")
    assert metadb.finish_backend_publication(
        run_id, ref["attempt_id"], "publisher", terminal,
    ) is True
    metadb.catalog_delete_entry(first["uri"])
    metadb.catalog_delete_entry(second["uri"])


def test_source_pin_survives_pointer_replacement_then_releases_into_gc():
    token = uuid.uuid4().hex
    logical_uri = f"s3://source-pin/{token}/replace.parquet"
    allocation_key = f"source-replace-{token}"
    first = _source_attempt(logical_uri, allocation_key, f"source-first-{token}")
    run_id = f"source-reader-{token}"
    _lease_token, ref, _status = _bind(run_id, [first["uri"]])

    second = _source_attempt(logical_uri, allocation_key, f"source-second-{token}")
    assert second["generation"] == first["generation"] + 1
    assert _attempt_state(first["uri"]) == "published", (
        "catalog pointer replacement retired a source still pinned by a backend run")
    assert _attempt_state(second["uri"]) == "published"
    assert all(action["uri"] != first["uri"] for action in
               metadb.object_attempt_gc_batch(0, 0))

    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"
    terminal = _staged_failed_result(run_id, ref, "publisher", "complete")
    assert metadb.finish_backend_publication(
        run_id, ref["attempt_id"], "publisher", terminal,
    ) is True
    assert metadb.backend_source_pins(run_id) == []
    assert _attempt_state(first["uri"]) == "superseded"
    actions = metadb.object_attempt_gc_batch(0, 0)
    assert any(action["uri"] == first["uri"] for action in actions)

    metadb.catalog_delete_entry(second["uri"])


def test_terminal_publication_rollback_preserves_source_pin(monkeypatch):
    token = uuid.uuid4().hex
    logical_uri = f"s3://source-pin/{token}/rollback.parquet"
    source = _source_attempt(logical_uri, f"source-rollback-{token}", f"source-{token}")
    run_id = f"source-rollback-reader-{token}"
    _lease_token, ref, _status = _bind(run_id, [source["uri"]])
    assert metadb.claim_backend_publication(
        run_id, ref["attempt_id"], "publisher", 30) == "claimed"
    terminal = _staged_failed_result(run_id, ref, "publisher", "failed")

    original = metadb.sync_local_result_owner

    def fail_final_owner(*_args, **_kwargs):
        raise ConnectionError("terminal owner transaction failed")

    monkeypatch.setattr(metadb, "sync_local_result_owner", fail_final_owner)
    with pytest.raises(ConnectionError, match="terminal owner transaction failed"):
        metadb.finish_backend_publication(
            run_id, ref["attempt_id"], "publisher", terminal,
        )
    monkeypatch.setattr(metadb, "sync_local_result_owner", original)

    binding = metadb.backend_job(run_id)
    assert binding["publication_state"] == "effects_started"
    assert metadb.get_run_state(run_id)["status"] == "queued"
    assert metadb.backend_source_pins(run_id) == [
        {"uri": source["uri"], "generation": source["generation"]}
    ]
    assert _attempt_state(source["uri"]) == "published"

    assert metadb.finish_backend_publication(
        run_id, ref["attempt_id"], "publisher", terminal,
    ) is True
    assert metadb.backend_source_pins(run_id) == []
    assert _attempt_state(source["uri"]) == "published", (
        "the catalog still owns the current source generation")
    metadb.catalog_delete_entry(source["uri"])
