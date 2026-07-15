from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pathlib
import threading
import types
import uuid

import pytest
from sqlalchemy import event, inspect, select, text

from hub import metadb
from hub.plugins.runner import _persist_local_result_done
from hub.storage import (
    MAX_MANAGED_EXECUTION_SOURCES,
    LocalStorage,
    ManagedSourceLimitExceeded,
    ManagedSourceUnavailable,
    local_result_read_scope,
    source_read_scope,
)


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    """Give every lifecycle regression an isolated schema unless CI explicitly supplies PostgreSQL."""
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'local-result-lifecycle.db'}")
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
def storage(tmp_path):
    value = LocalStorage(str(tmp_path / "outputs"))
    try:
        yield value
    finally:
        value.close()


def _create_canvas() -> tuple[str, str]:
    token = uuid.uuid4().hex
    user_id, canvas_id = f"user-{token}", f"canvas-{token}"
    with metadb.session() as session:
        session.add(metadb.User(id=user_id, name="Local result test"))
        session.flush()
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=user_id, name="Local result test", version=1, doc="{}"))
    return user_id, canvas_id


def _ready_result(storage: LocalStorage, run_id: str) -> str:
    uri = storage.begin_result(f"plan-{uuid.uuid4().hex}", run_id)
    pathlib.Path(uri).write_bytes(b"local-result-test")
    storage.commit_result(uri, run_id)
    return uri


def _done_doc(run_id: str, uri: str) -> dict:
    return {
        "run_id": run_id,
        "status": "done",
        "output_uri": uri,
        "output_table": None,
    }


def _publish_run(storage: LocalStorage, run_id: str, uri: str) -> tuple[str, str, dict]:
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(run_id, user_id, canvas_id)
    doc = _done_doc(run_id, uri)
    metadb.save_run_state(run_id, doc, canvas_id=canvas_id)
    assert storage.release_result(uri, run_id) is True
    return user_id, canvas_id, doc


def _artifact(uri: str):
    with metadb.session() as session:
        return session.get(metadb.LocalResultArtifact, uri)


def test_local_result_end_to_end_owner_reader_and_gc(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)

    with metadb.session() as session:
        row = session.get(metadb.LocalResultArtifact, uri)
        assert row is not None and row.state == "ready"
        assert row.writer_run_id is None and row.writer_token is None
        assert session.get(metadb.LocalResultReference, {
            "uri": uri, "owner_kind": "run_state", "owner_key": run_id,
        }) is not None

    guard = storage.acquire_result_read(uri, f"test:{run_id}")
    guard.check()
    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)
    assert pathlib.Path(uri).exists(), "an active exact reader must prevent reclamation"
    guard.close()

    storage.prune_results(limit=10)
    assert not pathlib.Path(uri).exists()
    assert _artifact(uri) is None


def test_terminal_commit_then_raise_is_proved_by_exact_receipt(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(run_id, user_id, canvas_id)
    doc = _done_doc(run_id, uri)
    calls = 0

    def committed_then_raised() -> None:
        nonlocal calls
        calls += 1
        metadb.save_run_state(run_id, doc, canvas_id=canvas_id)
        raise ConnectionError("connection lost after commit")

    _persist_local_result_done(
        committed_then_raised,
        lambda: storage.result_publication_receipt(uri, run_id, doc),
        wait=lambda _delay: pytest.fail("an exact committed receipt must avoid replay"),
    )
    assert calls == 1
    assert storage.release_result(uri, run_id) is True
    assert metadb.get_run_state(run_id) == doc

    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_managed_local_done_requires_a_preexisting_run_state(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    doc = _done_doc(run_id, uri)
    receipt_calls = 0

    def receipt() -> bool:
        nonlocal receipt_calls
        receipt_calls += 1
        return False

    with pytest.raises(metadb.RunStatePublicationRejected, match="pre-existing"):
        _persist_local_result_done(
            lambda: metadb.save_run_state(run_id, doc), receipt,
            wait=lambda _delay: pytest.fail("definitive rejection must not retry"),
        )
    assert receipt_calls == 0
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_id) is None
        assert session.get(metadb.LocalResultReference, {
            "uri": uri, "owner_kind": "run_state", "owner_key": run_id,
        }) is None
    storage.abort_result(uri, run_id)


def test_canvas_deleted_before_terminal_call_cannot_be_resurrected(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(run_id, user_id, canvas_id)
    metadb.delete_canvas_cascade(canvas_id)

    with pytest.raises(metadb.RunStatePublicationRejected, match="pre-existing"):
        metadb.save_run_state(run_id, _done_doc(run_id, uri), canvas_id=canvas_id)
    assert metadb.get_run_state(run_id) is None
    storage.abort_result(uri, run_id)


def test_writer_release_commit_unknown_retries_without_dropping_fence(storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(run_id, user_id, canvas_id)
    metadb.save_run_state(run_id, _done_doc(run_id, uri), canvas_id=canvas_id)
    writer_fd = storage.result_lock_fd(uri, run_id)
    original = metadb.release_local_result_writer
    calls = 0

    def committed_then_raised(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            raise ConnectionError("connection lost after writer release")
        return result

    monkeypatch.setattr(metadb, "release_local_result_writer", committed_then_raised)
    with pytest.raises(ConnectionError, match="writer release"):
        storage.release_result(uri, run_id)
    assert uri in storage._pending_writer_releases
    if writer_fd is not None:
        os.fstat(writer_fd)

    storage.retry_result_fences(limit=1)
    assert uri not in storage._pending_writer_releases
    if writer_fd is not None:
        with pytest.raises(OSError):
            os.fstat(writer_fd)

    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_reader_release_commit_unknown_retries_with_exact_fds(storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    guard = storage.acquire_result_read(uri, f"reader:{run_id}")
    lock_fd, artifact_fd = guard.fileno(), guard.artifact_fileno()
    original = metadb.release_local_result_read
    calls = 0

    def committed_then_raised(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 1:
            raise ConnectionError("connection lost after reader release")

    monkeypatch.setattr(metadb, "release_local_result_read", committed_then_raised)
    with pytest.raises(ConnectionError, match="reader release"):
        guard.close()
    assert guard.reader_id in storage._pending_reader_releases
    os.fstat(artifact_fd)
    if lock_fd is not None:
        os.fstat(lock_fd)

    storage.retry_result_fences(limit=1)
    assert guard._closed is True
    assert guard.reader_id not in storage._pending_reader_releases
    with pytest.raises(OSError):
        os.fstat(artifact_fd)
    if lock_fd is not None:
        with pytest.raises(OSError):
            os.fstat(lock_fd)

    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_failed_no_flock_reader_acquire_retains_commit_unknown_cleanup(
        storage, monkeypatch):
    storage.lock_supported = False
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    original_open = storage._open_result_artifact
    original_release = metadb.release_local_result_read

    def fail_open(_uri, **_kwargs):
        raise RuntimeError("artifact open failed")

    def fail_release(*_args, **_kwargs):
        raise ConnectionError("reader release unavailable")

    monkeypatch.setattr(storage, "_open_result_artifact", fail_open)
    monkeypatch.setattr(metadb, "release_local_result_read", fail_release)
    with pytest.raises(RuntimeError, match="artifact open failed"):
        storage.acquire_result_read(uri, f"failed-reader:{run_id}")

    assert len(storage._pending_reader_releases) == 1
    pending = next(iter(storage._pending_reader_releases.values()))
    assert pending._artifact_fd == -1 and pending._lock_fd is None
    assert metadb.local_result_read_active(
        uri, storage.namespace_id, pending.reader_id)

    monkeypatch.setattr(storage, "_open_result_artifact", original_open)
    monkeypatch.setattr(metadb, "release_local_result_read", original_release)
    storage.retry_result_fences(limit=1)
    assert storage._pending_reader_releases == {}
    assert not metadb.local_result_read_active(
        uri, storage.namespace_id, pending.reader_id)
    metadb.delete_canvas_cascade(canvas_id)


def test_reader_cleanup_failure_does_not_replace_active_execution_error(storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    original = metadb.release_local_result_read

    def fail_release(*_args, **_kwargs):
        raise ConnectionError("reader release unavailable")

    monkeypatch.setattr(metadb, "release_local_result_read", fail_release)
    with pytest.raises(ValueError, match="query failed"):
        with local_result_read_scope(storage, [uri], owner="failing-query"):
            raise ValueError("query failed")
    assert len(storage._pending_reader_releases) == 1

    monkeypatch.setattr(metadb, "release_local_result_read", original)
    storage.retry_result_fences(limit=1)
    assert storage._pending_reader_releases == {}
    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_successful_reader_scope_does_not_fail_on_commit_unknown_cleanup(
        storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    original = metadb.release_local_result_read

    def fail_release(*_args, **_kwargs):
        raise ConnectionError("reader release unavailable")

    monkeypatch.setattr(metadb, "release_local_result_read", fail_release)
    completed = False
    with local_result_read_scope(storage, [uri], owner="successful-query"):
        completed = True
    assert completed is True
    assert len(storage._pending_reader_releases) == 1
    pending = next(iter(storage._pending_reader_releases.values()))
    artifact_fd, lock_fd = pending.artifact_fileno(), pending.fileno()
    os.fstat(artifact_fd)
    if lock_fd is not None:
        os.fstat(lock_fd)

    monkeypatch.setattr(metadb, "release_local_result_read", original)
    storage.retry_result_fences(limit=1)
    assert storage._pending_reader_releases == {}
    with pytest.raises(OSError):
        os.fstat(artifact_fd)
    if lock_fd is not None:
        with pytest.raises(OSError):
            os.fstat(lock_fd)
    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_abort_commit_unknown_replays_exact_writer_and_deletes(storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    writer_fd = storage.result_lock_fd(uri, run_id)
    original = metadb.abandon_local_result
    calls = 0

    def committed_then_raised(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            raise ConnectionError("connection lost after abort claim")
        return result

    monkeypatch.setattr(metadb, "abandon_local_result", committed_then_raised)
    with pytest.raises(ConnectionError, match="abort claim"):
        storage.abort_result(uri, run_id)
    assert uri in storage._pending_aborts
    row = _artifact(uri)
    assert row is not None and row.state == "deleting"
    assert row.writer_run_id == run_id and row.writer_token is not None
    if writer_fd is not None:
        os.fstat(writer_fd)

    storage.retry_result_fences(limit=1)
    assert uri not in storage._pending_aborts
    assert _artifact(uri) is None
    assert not pathlib.Path(uri).exists()
    if writer_fd is not None:
        with pytest.raises(OSError):
            os.fstat(writer_fd)


def test_no_flock_begin_commit_unknown_retains_exact_abort_for_retry(storage, monkeypatch):
    storage.lock_supported = False
    original_begin = metadb.begin_local_result
    original_abandon = metadb.abandon_local_result

    def committed_then_raised(*args, **kwargs):
        original_begin(*args, **kwargs)
        raise ConnectionError("connection lost after reservation commit")

    def cleanup_unavailable(*_args, **_kwargs):
        raise ConnectionError("cleanup unavailable")

    monkeypatch.setattr(metadb, "begin_local_result", committed_then_raised)
    monkeypatch.setattr(metadb, "abandon_local_result", cleanup_unavailable)
    with pytest.raises(ConnectionError, match="reservation commit"):
        storage.begin_result(f"plan-{uuid.uuid4().hex}", f"run-{uuid.uuid4().hex}")

    assert len(storage._pending_aborts) == 1
    uri, owner = next(iter(storage._pending_aborts.items()))
    row = _artifact(uri)
    assert row is not None and row.state == "writing"
    assert (row.writer_run_id, row.writer_token) == owner
    assert storage._result_tokens[uri] == owner
    assert storage._writer_lock_fds[uri] is None

    monkeypatch.setattr(metadb, "abandon_local_result", original_abandon)
    storage.retry_result_fences(limit=1)
    assert storage._pending_aborts == {}
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()


def test_local_result_begin_replay_requires_exact_reservation(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = storage.begin_result(f"plan-{uuid.uuid4().hex}", run_id)
    row = _artifact(uri)
    assert row is not None
    args = (
        uri,
        storage.namespace_id,
        storage.result_root,
        row.lock_name,
        row.lock_protected,
        run_id,
        row.writer_token,
        row.lock_token,
    )
    metadb.begin_local_result(*args)
    with pytest.raises(RuntimeError, match="already exists"):
        metadb.begin_local_result(*args[:-2], uuid.uuid4().hex, args[-1])
    storage.abort_result(uri, run_id)


def test_delayed_writer_release_closes_fence_after_gc_claim(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(run_id, user_id, canvas_id)
    metadb.save_run_state(run_id, _done_doc(run_id, uri), canvas_id=canvas_id)
    writer_fd = storage.result_lock_fd(uri, run_id)
    metadb.delete_canvas_cascade(canvas_id)

    claims = metadb.claim_local_result_reclaims(
        storage.namespace_id, limit=1, prefer_fresh=True)
    assert len(claims) == 1 and claims[0][0] == uri
    row = _artifact(uri)
    assert row is not None and row.state == "deleting"
    assert row.writer_run_id is None and row.writer_token is None

    assert storage.release_result(uri, run_id) is True
    if writer_fd is not None:
        with pytest.raises(OSError):
            os.fstat(writer_fd)
    storage.prune_results(limit=1)
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()


def test_wrong_owner_cannot_release_explicit_deleting_writer(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    owner = storage._result_tokens[uri]
    metadb.abandon_local_result(uri, storage.namespace_id, owner[0], owner[1])
    with pytest.raises(RuntimeError, match="lost ownership"):
        metadb.release_local_result_writer(
            uri, storage.namespace_id, "wrong-run", uuid.uuid4().hex)
    storage.abort_result(uri, run_id)


def test_no_flock_abort_delete_failure_is_retried_from_durable_queue(
        storage, monkeypatch):
    storage.lock_supported = False
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    original_delete = storage._delete_claimed_result

    def fail_once(*_args, **_kwargs):
        raise OSError("transient delete failure")

    monkeypatch.setattr(storage, "_delete_claimed_result", fail_once)
    with pytest.raises(OSError, match="transient"):
        storage.abort_result(uri, run_id)
    assert uri not in storage._pending_aborts
    row = _artifact(uri)
    assert row is not None and row.state == "deleting" and pathlib.Path(uri).exists()

    monkeypatch.setattr(storage, "_delete_claimed_result", original_delete)
    storage.prune_results(limit=1)
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()


def test_abort_delete_commit_then_raise_has_authoritative_absence(storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    original = metadb.delete_local_result

    def committed_then_raised(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("connection lost after delete commit")

    monkeypatch.setattr(metadb, "delete_local_result", committed_then_raised)
    with pytest.raises(ConnectionError, match="delete commit"):
        storage.abort_result(uri, run_id)
    assert uri not in storage._pending_aborts
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()


def test_concurrent_abort_retries_have_one_local_delete_actor(storage, monkeypatch):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    original_abandon = metadb.abandon_local_result
    original_delete = storage._delete_claimed_result
    barrier = threading.Barrier(2)
    delete_calls = 0
    delete_lock = threading.Lock()

    def synchronized_abandon(*args, **kwargs):
        token = original_abandon(*args, **kwargs)
        barrier.wait(timeout=10)
        return token

    def counted_delete(*args, **kwargs):
        nonlocal delete_calls
        with delete_lock:
            delete_calls += 1
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(metadb, "abandon_local_result", synchronized_abandon)
    monkeypatch.setattr(storage, "_delete_claimed_result", counted_delete)
    errors = []

    def abort():
        try:
            storage.abort_result(uri, run_id)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=abort) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [] and delete_calls == 1
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()


def test_read_guard_detects_artifact_inode_replacement(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    guard = storage.acquire_result_read(uri, f"reader:{run_id}")
    backup = uri + ".held"
    os.replace(uri, backup)
    pathlib.Path(uri).write_bytes(b"replacement")
    os.chmod(uri, 0o600)
    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            guard.check()
    finally:
        os.replace(backup, uri)
    guard.close()
    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_interactive_scope_deduplicates_local_alias_and_ignores_unmanaged(storage, tmp_path):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    ordinary = tmp_path / "ordinary.parquet"
    ordinary.write_bytes(b"ordinary")
    file_uri = pathlib.Path(uri).as_uri()

    with source_read_scope(
            storage, [uri, file_uri, file_uri.replace("file://", "FILE://"), str(ordinary)],
            owner="interactive-local") as guards:
        assert len(guards) == 1
        assert metadb.local_result_read_active(
            uri, storage.namespace_id, guards[0].reader_id)
        metadb.delete_canvas_cascade(canvas_id)
        storage.prune_results(limit=10)
        assert pathlib.Path(uri).exists()

    storage.prune_results(limit=10)
    assert not pathlib.Path(uri).exists()


def test_interactive_scope_rejects_noncanonical_file_alias_before_body(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    file_uri = pathlib.Path(uri).as_uri()
    aliases = [
        file_uri + "?download=1",
        file_uri + "?",
        file_uri + "#",
        file_uri.replace("file:///", "file://other-host/"),
        file_uri.replace("file:///", "file:/"),
    ]
    bodies = 0
    for alias in aliases:
        with pytest.raises(ManagedSourceUnavailable, match="unavailable or expired"):
            with source_read_scope(storage, [alias], owner="invalid-file-alias"):
                bodies += 1
    assert bodies == 0
    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)


def test_warm_preview_reacquires_local_source_guard_on_cache_hit(storage, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import db
    from hub.executors.preview import preview_node
    from hub.models import Graph
    from hub.relation_cache import RelationCache

    run_id = f"run-{uuid.uuid4().hex}"
    uri = storage.begin_result(f"plan-{uuid.uuid4().hex}", run_id)
    pq.write_table(pa.table({"value": [1]}), uri)
    storage.commit_result(uri, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)

    acquisitions = []
    scans = 0
    fingerprints = 0
    original_acquire = storage.acquire_result_read

    def acquire(source_uri, owner):
        guard = original_acquire(source_uri, owner)
        acquisitions.append(guard)
        return guard

    monkeypatch.setattr(storage, "acquire_result_read", acquire)

    def active() -> bool:
        return bool(acquisitions and metadb.local_result_read_active(
            uri, storage.namespace_id, acquisitions[-1].reader_id))

    class Adapter:
        def fingerprint(self, source_uri):
            nonlocal fingerprints
            assert source_uri == uri and active()
            fingerprints += 1
            return "stable-local-source"

        def preview_scan(self, source_uri, **_kwargs):
            nonlocal scans
            assert source_uri == uri and active()
            scans += 1
            return db.conn().sql("SELECT 1 AS value")

        def scan(self, _source_uri, **_kwargs):
            raise AssertionError("interactive preview must use the bounded adapter capability")

    graph = Graph.model_validate({
        "id": f"warm-{uuid.uuid4().hex}",
        "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }],
        "edges": [],
    })
    cache = RelationCache()
    for _ in range(2):
        result = preview_node(
            graph, "source", 5, lambda _uri: Adapter(), {}, cache=cache, storage=storage)
        assert not result.error and result.rows == [{"value": 1}]
        assert not active()
    assert len(acquisitions) == 2 and fingerprints == 2
    assert scans == 1, "the warm hit must still reacquire the exact local source guard"

    metadb.delete_canvas_cascade(canvas_id)
    storage.prune_results(limit=10)
    assert not pathlib.Path(uri).exists()


def test_managed_source_cap_fails_before_any_reader_acquisition(storage, monkeypatch):
    uris = [
        os.path.join(
            storage.result_root,
            f"__result_cap_{index}_{uuid.uuid4().hex}.parquet",
        )
        for index in range(MAX_MANAGED_EXECUTION_SOURCES + 1)
    ]
    acquired = []

    def acquire(uri, owner):
        acquired.append((uri, owner))
        raise AssertionError("preflight must run before the first acquisition")

    monkeypatch.setattr(storage, "acquire_result_read", acquire)
    with pytest.raises(ManagedSourceLimitExceeded, match="at most"):
        with local_result_read_scope(storage, uris, owner="cap-test"):
            pass
    assert acquired == []


def test_region_mixed_source_cap_fails_before_local_or_object_acquisition(
        storage, monkeypatch):
    from hub import handoff
    from hub import run_controller as controller_mod
    from hub.models import Graph

    run_id = f"run-{uuid.uuid4().hex}"
    local_uri = _ready_result(storage, run_id)
    object_uris = [
        f"s3://bucket/table.attempt-{index:03d}"
        for index in range(MAX_MANAGED_EXECUTION_SOURCES)
    ]
    acquired_local = []
    acquired_object = []

    monkeypatch.setattr(
        controller_mod.g,
        "all_upstream_source_uris",
        lambda *_args, **_kwargs: [local_uri, *object_uris],
    )
    monkeypatch.setattr(
        storage,
        "acquire_result_read",
        lambda *args, **kwargs: acquired_local.append((args, kwargs)),
    )
    monkeypatch.setattr(
        handoff,
        "managed_read_lease",
        lambda *args, **kwargs: acquired_object.append((args, kwargs)),
    )
    controller = controller_mod.RunController(
        types.SimpleNamespace(storage=storage), base=None, place_fn=None)
    graph = Graph(id="cap", version=1, nodes=[], edges=[])

    with pytest.raises(RuntimeError, match="ownership lease unavailable"):
        with controller._region_source_lease_scope(
                graph, "target", run_id=run_id, region_id="region"):
            pass
    assert acquired_local == []
    assert acquired_object == []
    storage.abort_result(local_uri, run_id)


@pytest.mark.parametrize("target_kind", ["reserved", "hardlink"])
def test_subprocess_without_resolver_preflights_local_sink_namespace(
        storage, monkeypatch, tmp_path, target_kind):
    from hub.models import CompilePlan, Graph, PlanStep
    from hub.subprocess_runner import SubprocessRunner

    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    alias = tmp_path / "sink-hardlink.parquet"
    target = uri
    if target_kind == "hardlink":
        os.link(uri, alias)
        target = str(alias)
    monkeypatch.setattr(storage, "output_uri", lambda *_args, **_kwargs: target)
    graph = Graph.model_validate({
        "id": "sink-preflight",
        "version": 1,
        "nodes": [{
            "id": "write",
            "type": "write",
            "position": {"x": 0, "y": 0},
            "data": {"config": {"filename": "out.parquet"}},
        }],
        "edges": [],
    })
    plan = CompilePlan(target_node_id="write", steps=[
        PlanStep(node_id="write", kind="write", label="write"),
    ])
    runner = SubprocessRunner(
        str(tmp_path), str(tmp_path), catalog=object(), storage=storage,
        resolve_adapter=None,
    )
    try:
        with pytest.raises(ValueError, match="reserved"):
            runner._claim_sink_contracts(plan, graph, run_id)
    finally:
        if alias.exists():
            alias.unlink()
        storage.abort_result(uri, run_id)


def test_output_sink_rejects_hardlink_alias_without_namespace_scan(storage, monkeypatch, tmp_path):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    alias = tmp_path / "hardlink.parquet"
    os.link(uri, alias)
    monkeypatch.setattr(os, "listdir", lambda *_args, **_kwargs: pytest.fail(
        "sink validation must not scan retained results"))
    try:
        with pytest.raises(ValueError, match="reserved"):
            storage.ensure_output_allowed(str(alias))
    finally:
        alias.unlink()
    storage.abort_result(uri, run_id)


def test_source_rejects_plain_and_file_uri_hardlink_alias_before_reader_acquisition(
        storage, monkeypatch, tmp_path):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    alias = tmp_path / "source-hardlink.parquet"
    os.link(uri, alias)
    acquired = []
    original_acquire = storage.acquire_result_read

    def acquire(*args, **kwargs):
        acquired.append(args)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(storage, "acquire_result_read", acquire)
    try:
        for source in (str(alias), alias.as_uri()):
            with pytest.raises(RuntimeError, match="hard-linked local source"):
                with local_result_read_scope(storage, [source], owner="hardlink-source"):
                    pass
        assert acquired == []
        assert _artifact(uri).state == "ready"
    finally:
        alias.unlink()
    storage.abort_result(uri, run_id)


def test_dead_lock_poison_does_not_block_reclaim_or_orphan_cleanup(storage):
    if not storage.lock_supported:
        pytest.skip("requires POSIX flock")
    poison_run = f"run-{uuid.uuid4().hex}"
    poison_uri = _ready_result(storage, poison_run)
    poison = _artifact(poison_uri)
    assert poison is not None
    with storage._result_lock:
        poison_fd = storage._writer_lock_fds.pop(poison_uri)
        storage._result_tokens.pop(poison_uri)
    os.close(poison_fd)
    poison_lock = pathlib.Path(storage._result_lock_root, poison.lock_name)
    poison_lock.write_bytes(b"not-a-canonical-token")
    os.chmod(poison_lock, 0o600)

    fresh_run = f"run-{uuid.uuid4().hex}"
    fresh_uri = _ready_result(storage, fresh_run)
    _user_id, fresh_canvas, _doc = _publish_run(storage, fresh_run, fresh_uri)
    metadb.delete_canvas_cascade(fresh_canvas)

    orphan_token = uuid.uuid4().hex
    orphan_temp = pathlib.Path(
        storage.result_root,
        f"__result_orphan_{orphan_token}.parquet.tmp-deadbeef",
    )
    orphan_temp.write_bytes(b"stale")
    os.chmod(orphan_temp, 0o600)
    orphan_lock = pathlib.Path(
        storage._result_lock_root,
        f"__result_orphan_lock_{uuid.uuid4().hex}.lock",
    )
    orphan_lock.write_bytes(b"partial")
    os.chmod(orphan_lock, 0o600)

    storage.prune_results(limit=10)

    assert _artifact(poison_uri) is not None and pathlib.Path(poison_uri).exists()
    assert _artifact(fresh_uri) is None and not pathlib.Path(fresh_uri).exists()
    assert not orphan_temp.exists()
    assert not orphan_lock.exists()


def test_dead_writer_and_ephemeral_reader_are_reconciled_then_reclaimed(storage):
    if not storage.lock_supported:
        pytest.skip("requires POSIX flock")
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    guard = storage.acquire_result_read(uri, f"dead-reader:{run_id}")

    with storage._result_lock:
        writer_fd = storage._writer_lock_fds.pop(uri)
        storage._result_tokens.pop(uri)
        storage._reader_guards.pop(guard.reader_id)
    os.close(writer_fd)
    if guard._lock_fd is not None:
        os.close(guard._lock_fd)
    os.close(guard._artifact_fd)
    guard._lock_fd = None
    guard._artifact_fd = -1
    guard._closed = True

    storage.prune_results(limit=10)

    with metadb.session() as session:
        assert session.get(metadb.LocalResultReference, {
            "uri": uri,
            "owner_kind": metadb._LOCAL_RESULT_EPHEMERAL_OWNER_KIND,
            "owner_key": guard.reader_id,
        }) is None
        assert session.get(metadb.LocalResultArtifact, uri) is None
    assert not pathlib.Path(uri).exists()


def test_gc_poison_deleting_backlog_does_not_starve_fresh_artifact(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    _user_id, canvas_id, _doc = _publish_run(storage, run_id, uri)
    metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as session:
        for index in range(4):
            token = uuid.uuid4().hex
            session.add(metadb.LocalResultArtifact(
                uri=os.path.join(
                    storage.result_root,
                    f"__result_poison_{index}_{token}.parquet"),
                namespace_id=storage.namespace_id,
                storage_root=storage.result_root,
                lock_name=f"__result_poison_{index}_{token}.lock",
                lock_token=uuid.uuid4().hex,
                lock_protected=True,
                state="deleting",
                writer_run_id=None,
                writer_token=None,
                delete_token=uuid.uuid4().hex,
                delete_attempted_at=metadb._now(),
            ))

    storage.prune_results(limit=1)  # retry one poison row
    assert pathlib.Path(uri).exists()
    storage.prune_results(limit=1)  # alternating fresh turn must not starve behind poison
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()


def test_deleting_rotation_does_not_depend_on_database_timestamp_precision(storage):
    attempted_at = metadb._now().replace(microsecond=0)
    uris = []
    with metadb.session() as session:
        for index in range(4):
            token = uuid.uuid4().hex
            uri = os.path.join(
                storage.result_root,
                f"__result_rotation_{index}_{token}.parquet")
            uris.append(uri)
            session.add(metadb.LocalResultArtifact(
                uri=uri,
                namespace_id=storage.namespace_id,
                storage_root=storage.result_root,
                lock_name=f"__result_rotation_{index}_{token}.lock",
                lock_token=uuid.uuid4().hex,
                lock_protected=True,
                state="deleting",
                writer_run_id=None,
                writer_token=None,
                delete_token=uuid.uuid4().hex,
                delete_attempted_at=attempted_at,
            ))

    claimed = [
        metadb.claim_local_result_reclaims(
            storage.namespace_id, limit=1, prefer_fresh=False)[0][0]
        for _ in uris
    ]
    assert len(set(claimed)) == len(uris)
    assert set(claimed) == set(uris)


def test_orphan_lock_scan_reclaims_malformed_pre_db_locks_but_keeps_registered(
        storage):
    if not storage.lock_supported:
        pytest.skip("requires POSIX flock")
    lock_root = pathlib.Path(storage._result_lock_root)
    for index in range(300):
        (lock_root / f"decoy-{index:04d}").write_bytes(b"x")
    orphan_names = [
        f"__result_empty_{uuid.uuid4().hex}.lock",
        f"__result_partial_{uuid.uuid4().hex}.lock",
    ]
    (lock_root / orphan_names[0]).write_bytes(b"")
    (lock_root / orphan_names[1]).write_bytes(b"partial-token")
    keep_token = uuid.uuid4().hex
    keep_name = f"__result_keep_{keep_token}.lock"
    keep_uri = os.path.join(storage.result_root, keep_name[:-len(".lock")] + ".parquet")
    (lock_root / keep_name).write_bytes(b"partial-token")
    for name in (*orphan_names, keep_name):
        os.chmod(lock_root / name, 0o600)
    with metadb.session() as session:
        session.add(metadb.LocalResultArtifact(
            uri=keep_uri,
            namespace_id=storage.namespace_id,
            storage_root=storage.result_root,
            lock_name=keep_name,
            lock_token=uuid.uuid4().hex,
            lock_protected=True,
            state="writing",
            writer_run_id="crashed-writer",
            writer_token=uuid.uuid4().hex,
        ))

    for _ in range(5):
        storage.prune_results(limit=10)
        if all(not (lock_root / name).exists() for name in orphan_names):
            break
    assert all(not (lock_root / name).exists() for name in orphan_names)
    assert (lock_root / keep_name).exists()


def test_registry_lock_flushes_pending_owner_sql_before_registry_update():
    statements = []
    engine = metadb.engine()

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        with metadb.session() as session:
            session.add(metadb.ResultCache(
                key=f"flush-order-{uuid.uuid4().hex}", doc="{}"))
            metadb._lock_local_result_registry(session)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    owner_insert = next(
        index for index, statement in enumerate(statements)
        if "insert into result_cache" in statement)
    registry_update = next(
        index for index, statement in enumerate(statements)
        if "update local_result_registry" in statement)
    assert owner_insert < registry_update


def test_primary_writer_run_must_publish_before_secondary_owners(storage):
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    with pytest.raises(RuntimeError, match="exact writer run"):
        metadb.put_result(f"early-cache-{uuid.uuid4().hex}", {"uri": uri})

    wrong_run = f"run-{uuid.uuid4().hex}"
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(wrong_run, user_id, canvas_id)
    with pytest.raises(RuntimeError, match="exact writer run"):
        metadb.save_run_state(
            wrong_run, _done_doc(wrong_run, uri), canvas_id=canvas_id)
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri))) == []

    metadb.bind_run_owner(run_id, user_id, canvas_id)
    metadb.save_run_state(run_id, _done_doc(run_id, uri), canvas_id=canvas_id)
    assert storage.release_result(uri, run_id) is True
    metadb.put_result(f"late-cache-{uuid.uuid4().hex}", {"uri": uri})


def test_nested_visual_and_legacy_section_sources_are_durable_owner_candidates(storage):
    uris = [_ready_result(storage, f"run-{uuid.uuid4().hex}") for _ in range(2)]
    doc = {
        "nodes": [
            {
                "id": "visual-section",
                "type": "section",
                "data": {"config": {}},
            },
            {
                "id": "visual-source",
                "parentId": "visual-section",
                "type": "source",
                "data": {"config": {"uri": uris[0]}},
            },
            {
                "id": "legacy-section",
                "type": "section",
                "data": {"config": {"subnodes": [{
                    "alias": "nested",
                    "type": "section",
                    "config": {"subnodes": [{
                        "alias": "source",
                        "type": "source",
                        "config": {"uri": uris[1]},
                    }]},
                }]}},
            },
        ],
    }
    assert metadb._canvas_local_result_candidates(doc) == set(uris)
    for uri in uris:
        storage.abort_result(uri, _artifact(uri).writer_run_id)


@pytest.mark.parametrize(("subnode_type", "protected_field"), [
    ("source", "uri"),
    ("section", "script"),
])
def test_section_runtime_cannot_override_lifecycle_owner_fields(
        subnode_type, protected_field):
    from hub.models import Graph, GraphNode, Position
    from hub.section import SectionError, run_section

    section = GraphNode(
        id="outer",
        type="section",
        position=Position(x=0, y=0),
        data={"config": {
            "script": f"run(child, {protected_field}='replacement')",
            "subnodes": [{
                "alias": "child",
                "type": subnode_type,
                "config": ({"uri": "/stable/source.parquet"}
                           if subnode_type == "source"
                           else {"script": "emit(inputs['in'])", "subnodes": []}),
            }],
        }},
    )
    engine = types.SimpleNamespace(
        graph=Graph(id="section-owner", version=1, nodes=[section], edges=[]),
        resolve_adapter=None,
        registry=None,
        node_builders={},
        node_specs={},
        spill_files=[],
    )
    with pytest.raises(SectionError, match=f"protected.*{protected_field}"):
        run_section(engine, section, [])


def test_subprocess_terminal_owner_rejection_aborts_result_without_failed_reemit(
        storage, tmp_path):
    from hub.models import Graph, RunStatus
    from hub.subprocess_runner import SubprocessRunner

    run_id = f"run-{uuid.uuid4().hex}"
    uri = storage.begin_result(f"plan-{uuid.uuid4().hex}", run_id)
    pathlib.Path(uri).write_bytes(b"child-result")
    job_dir = tmp_path / "terminal-rejection"
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child",
        status="done",
        per_node=[],
        output_uri=uri,
    ).model_dump()))

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    emitted = []

    def reject_done(_graph, status):
        emitted.append(status.status)
        if status.status == "done":
            raise metadb.RunStatePublicationRejected("run owner was deleted")

    graph = Graph(id="terminal-rejection", version=1, nodes=[], edges=[])
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), storage=storage)
    runner.publication_retry_wait = lambda _delay: pytest.fail(
        "definitive owner rejection must not retry")
    runner.on_status = reject_done
    runner.runs[run_id] = RunStatus(
        run_id=run_id, status="running", placement="local", per_node=[])
    runner._local_results[run_id] = {
        "uri": uri,
        "cache_key": None,
        "run_state_owner": True,
    }

    runner._watch(
        run_id,
        FinishedProcess(),
        str(status_file),
        str(job_dir),
        graph,
        None,
    )

    final = runner.status(run_id)
    assert final.status == "failed"
    assert final.output_uri is None and final.output_table is None
    assert emitted == ["done"]
    assert _artifact(uri) is None and not pathlib.Path(uri).exists()
    assert run_id not in runner._local_results


def test_ray_managed_local_source_falls_back_unpinned_and_fails_when_pinned(
        storage, monkeypatch, tmp_path):
    from hub.ir import lower_to_ir
    from hub.models import Graph, RunStatus

    plugin_path = pathlib.Path(__file__).parents[3] / "examples/plugins/dp_ray/__init__.py"
    spec = importlib.util.spec_from_file_location(
        f"dp_ray_local_result_{uuid.uuid4().hex}", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    base = types.SimpleNamespace(on_status=None, on_complete=None)
    deps = types.SimpleNamespace(
        runner=base,
        resolve_adapter=lambda _uri: None,
        catalog=None,
        node_specs={},
        node_ir={},
        storage=storage,
        registry=None,
        node_builders={},
    )
    runner = module.RayRunner(deps)
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    graph = Graph.model_validate({
        "id": "ray-managed-local",
        "version": 1,
        "nodes": [{
            "id": "source",
            "type": "source",
            "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }],
        "edges": [],
    })
    # Use the real lowerer contract from the built-in registry without constructing a full Deps tree.
    from hub.nodespecs import BUILTIN_NODE_SPECS
    runner.node_specs = {item.kind: item for item in BUILTIN_NODE_SPECS}
    ir = lower_to_ir(graph, "source", runner.node_specs, {})
    reason = runner._source_unsupported_reason(graph, "source", ir)
    assert reason is not None and "managed local full result" in reason

    fallback_calls = []
    fallback = RunStatus(
        run_id="ray-unpinned", status="done", placement="local", per_node=[])
    monkeypatch.setattr(
        module,
        "_allocate_handoff_uri",
        lambda output_uri, child_run_id, _kind: (
            f"{output_uri}.attempt-{child_run_id}"),
    )
    monkeypatch.setattr(module, "read_manifest", lambda _uri: None)
    monkeypatch.setattr(module, "attempt_has_commit_record", lambda _uri: False)
    monkeypatch.setattr(module, "attempt_has_contents", lambda _uri: False)
    monkeypatch.setattr(
        runner,
        "_materialize_local",
        lambda *args, **kwargs: fallback_calls.append((args, kwargs)) or fallback,
    )

    assert runner.run_unit(
        graph,
        "source",
        str(tmp_path / "unpinned-handoff"),
        run_id="ray-unpinned",
    ) is fallback
    pinned = runner.run_unit(
        graph,
        "source",
        str(tmp_path / "pinned-handoff"),
        requires={"labels": {"engine": "ray"}},
        run_id="ray-pinned",
    )
    assert pinned.status == "failed"
    assert "managed local full result" in (pinned.error or "")
    assert len(fallback_calls) == 1
    storage.abort_result(uri, run_id)


def test_deps_worker_mode_skips_global_storage_maintenance(tmp_path, monkeypatch):
    from hub import pool_runner, storage as storage_mod
    from hub.deps import Deps

    calls = []

    class Storage:
        def recover_orphans(self):
            calls.append("recover")

        def prune_results(self):
            calls.append("prune")

        @staticmethod
        def list_outputs():
            return []

    class Catalog:
        pass

    monkeypatch.setattr(storage_mod, "make_storage", lambda _workspace: Storage())
    monkeypatch.setattr(
        Deps, "_load_bundled", lambda self: setattr(self, "catalog", Catalog()))
    monkeypatch.setattr(Deps, "_load_plugins", lambda _self: None)
    monkeypatch.setattr(pool_runner, "pool_workers_from_env", lambda: [])

    Deps(str(tmp_path / "worker"), str(tmp_path / "data"), maintain_storage=False)
    assert calls == []
    Deps(str(tmp_path / "hub"), str(tmp_path / "data"), maintain_storage=True)
    assert calls == ["recover", "prune"]


def test_namespace_marker_initialization_closes_scandir_duplicate_fds(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    result_root = output_root / ".dp-results"
    lock_root = result_root / ".locks"
    lock_root.mkdir(parents=True)
    for index in range(300):
        (result_root / f".namespace-id.tmp-{index:04d}").write_bytes(b"stale")
    original_dup = os.dup
    duplicated = []

    def tracked_dup(fd):
        new_fd = original_dup(fd)
        duplicated.append(new_fd)
        return new_fd

    monkeypatch.setattr(os, "dup", tracked_dup)
    created = LocalStorage(str(output_root))
    created.close()

    assert len(duplicated) == 2
    for fd in duplicated:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_migration_0021_has_exact_schema_constraints(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from alembic import command
    from hub.settings import settings

    url = f"sqlite:///{tmp_path / 'migration-0021.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = metadb._alembic_cfg()
    command.upgrade(cfg, "0020_object_attempt_lifecycle")
    command.upgrade(cfg, "0021_local_result_artifacts")
    engine = sa.create_engine(url)
    inspector = inspect(engine)
    assert {item["name"] for item in inspector.get_unique_constraints(
        "local_result_artifacts")} >= {"uq_local_result_artifact_namespace_lock"}
    assert {item["name"] for item in inspector.get_check_constraints(
        "local_result_artifacts")} >= {
            "ck_local_result_artifact_state",
            "ck_local_result_artifact_writer_pair",
            "ck_local_result_artifact_lock_pair",
            "ck_local_result_artifact_delete_state",
            "ck_local_result_artifact_ready_commit",
        }
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO local_result_artifacts
                    (uri, namespace_id, storage_root, lock_name, lock_protected, state,
                     writer_run_id, created_at)
                VALUES
                    ('/tmp/.dp-results/__result_bad_00000000000000000000000000000000.parquet',
                     'namespace', '/tmp/.dp-results', 'bad.lock', false, 'writing',
                     'unpaired-writer', CURRENT_TIMESTAMP)
            """))
    command.downgrade(cfg, "0020_object_attempt_lifecycle")
    assert "local_result_artifacts" not in inspect(engine).get_table_names()
    engine.dispose()


def test_postgres_canvas_delete_wins_terminal_publication_without_resurrection(
        storage, monkeypatch):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")
    run_id = f"run-{uuid.uuid4().hex}"
    uri = _ready_result(storage, run_id)
    user_id, canvas_id = _create_canvas()
    metadb.bind_run_owner(run_id, user_id, canvas_id)
    doc = _done_doc(run_id, uri)
    initial_read = threading.Event()
    continue_save = threading.Event()
    original_session = metadb.session

    class _PausingSession:
        def __init__(self, real):
            self._real = real
            self._paused = False

        def get(self, entity, ident, *args, **kwargs):
            value = self._real.get(entity, ident, *args, **kwargs)
            if (not self._paused and entity is metadb.RunState
                    and ident == run_id and not kwargs.get("with_for_update")):
                self._paused = True
                initial_read.set()
                assert continue_save.wait(timeout=10)
            return value

        def __getattr__(self, name):
            return getattr(self._real, name)

    @contextlib.contextmanager
    def instrumented_session():
        with original_session() as real:
            if threading.current_thread().name == "terminal-save-race":
                yield _PausingSession(real)
            else:
                yield real

    monkeypatch.setattr(metadb, "session", instrumented_session)
    errors = []

    def save_terminal():
        try:
            metadb.save_run_state(run_id, doc, canvas_id=canvas_id)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = threading.Thread(target=save_terminal, name="terminal-save-race")
    thread.start()
    assert initial_read.wait(timeout=10)
    metadb.delete_canvas_cascade(canvas_id)
    continue_save.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], metadb.RunStatePublicationRejected)

    storage.abort_result(uri, run_id)
    with metadb.session() as session:
        assert session.get(metadb.RunState, run_id) is None
        assert session.get(metadb.LocalResultReference, {
            "uri": uri, "owner_kind": "run_state", "owner_key": run_id,
        }) is None
        assert session.get(metadb.LocalResultArtifact, uri) is None
    assert not pathlib.Path(uri).exists()
