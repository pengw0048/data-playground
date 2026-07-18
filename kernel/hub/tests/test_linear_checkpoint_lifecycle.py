"""Transfer/reattach, retire, abort, release, retain, and delete one exact hidden checkpoint."""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from sqlalchemy import func, select

from hub import linear_checkpoint as lc
from hub import metadb
from hub.storage import LocalStorage

from hub.tests.test_linear_checkpoint_admission import (  # reuse admission fixtures/helpers
    _identity, _submit, _metadata_schema)  # noqa: F401
from hub.tests.test_linear_checkpoint_commit import (  # reuse commit helpers
    _commit, _parquet_bytes, _reserve, _storage)  # noqa: F401


def _expire_lease(attempt_id: str) -> None:
    with metadb.session() as s:
        attempt = s.get(metadb.DurableTaskAttempt, attempt_id)
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=300)


def _set_task_status(task_id: str, status: str) -> None:
    with metadb.session() as s:
        task = s.get(metadb.DurableTask, task_id)
        task.status = status
        task.completed_at = metadb._now()


def _materialize_only(store: LocalStorage, ctx: dict, content: bytes) -> dict:
    """Produce the exact reserved file/lock on disk, bind its inode, leave it uncommitted."""
    writer = store.materialize_checkpoint(ctx["candidate"])
    try:
        writer.write(content)
        dev, ino = writer.identity()
        metadb.bind_linear_checkpoint_materialization(
            task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
            uri=ctx["candidate"]["uri"], dev=dev, ino=ino)
        writer.seal()
    finally:
        writer.release()
    candidate = metadb.linear_checkpoint_candidate(ctx["task_id"])
    assert candidate is not None and candidate["dev"] == dev and candidate["ino"] == ino
    ctx["candidate"] = candidate
    return candidate


def _artifact(uri: str):
    with metadb.session() as s:
        return s.get(metadb.LocalResultArtifact, uri)


def _owner_count(uri: str) -> int:
    with metadb.session() as s:
        return s.scalar(select(func.count()).select_from(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri,
            metadb.LocalResultReference.owner_kind == metadb._LINEAR_CHECKPOINT_OWNER_KIND))


# --------------------------------------------------------------------------- abort

def test_abort_uncommitted_cleans_exact_file_and_is_reclaimable(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    _materialize_only(store, ctx, _parquet_bytes(3))
    assert os.path.isfile(uri)

    lc.abort_checkpoint(store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"],
                        owner_token=ctx["owner"])

    # The exact file/lock and DB artifact row are gone; the checkpoint fell back to pending.
    assert not os.path.exists(uri)
    _, lock_name = store._result_names(uri)
    with pytest.raises(FileNotFoundError):
        os.stat(lock_name, dir_fd=store._result_lock_dir_fd, follow_symlinks=False)
    assert _artifact(uri) is None
    assert metadb.reconcile_linear_checkpoint(ctx["task_id"]) == {"phase": "pending",
                                                                  "candidate": None}


def test_abort_before_materialization_retires_the_reserved_row(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    assert not os.path.exists(uri)  # reserved only; never materialized

    lc.abort_checkpoint(store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"],
                        owner_token=ctx["owner"])

    assert _artifact(uri) is None
    assert metadb.reconcile_linear_checkpoint(ctx["task_id"])["phase"] == "pending"


def test_abort_is_idempotent_under_response_loss(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    _materialize_only(store, ctx, _parquet_bytes(2))

    first = metadb.abort_linear_checkpoint_candidate(
        ctx["task_id"], ctx["attempt_id"], ctx["owner"])
    assert first["uri"] == ctx["candidate"]["uri"] and first["delete_token"]
    # A replay after a lost response finds nothing bound and never fabricates a second deletion token.
    replay = metadb.abort_linear_checkpoint_candidate(
        ctx["task_id"], ctx["attempt_id"], ctx["owner"])
    assert replay == {"uri": None, "delete_token": None, "lock_token": None,
                      "namespace_id": None, "lock_name": None}


def test_abort_refuses_committed_truth(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    _commit(store, ctx, _parquet_bytes(4))
    with pytest.raises(RuntimeError, match="cannot abort a committed checkpoint"):
        metadb.abort_linear_checkpoint_candidate(ctx["task_id"], ctx["attempt_id"], ctx["owner"])
    # Committed truth is untouched.
    assert os.path.isfile(ctx["candidate"]["uri"]) and _owner_count(ctx["candidate"]["uri"]) == 1


def test_abort_by_a_fenced_owner_is_refused(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    _materialize_only(store, ctx, _parquet_bytes(2))
    _expire_lease(ctx["attempt_id"])
    with pytest.raises(RuntimeError, match="stale or fenced"):
        metadb.abort_linear_checkpoint_candidate(ctx["task_id"], ctx["attempt_id"], ctx["owner"])
    assert os.path.isfile(ctx["candidate"]["uri"])  # nothing cleaned under a fenced lease


# ----------------------------------------------------------------- reattach / retire

def test_recover_reattaches_the_exact_same_generation_candidate(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    content = _parquet_bytes(9)
    _materialize_only(store, ctx, content)  # produced but uncommitted; producer process "restarts"
    before = os.stat(uri)

    recovered = lc.recover_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    assert recovered["action"] == "reattach"
    assert recovered["candidate"]["generation"] == ctx["candidate"]["generation"]

    evidence = lc.commit_reattached_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
        candidate=recovered["candidate"])
    after = os.stat(uri)
    # Same generation, same inode: the file was reattached, never re-materialized.
    assert evidence.generation == ctx["candidate"]["generation"]
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert evidence.rows == 9
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence


def test_recover_retires_a_superseded_candidate_before_a_replacement_reservation(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    admission, _created = _submit(values)
    task_a = metadb.claim_linear_checkpoint_task(admission["task_id"], "owner-a")
    attempt_a = task_a["attempts"][-1]["id"]
    old = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt_a, owner_token="owner-a",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    _materialize_only(store, {
        "task_id": admission["task_id"], "attempt_id": attempt_a, "owner": "owner-a",
        "candidate": old}, _parquet_bytes(3))
    old_uri = old["uri"]
    assert os.path.isfile(old_uri)

    # The producer dies; its lease expires and a new fenced attempt is minted.
    _expire_lease(attempt_a)
    task_b = metadb.claim_linear_checkpoint_task(admission["task_id"], "owner-b")
    attempt_b = task_b["attempts"][-1]["id"]
    assert attempt_b != attempt_a

    recovered = lc.recover_checkpoint(
        store, task_id=admission["task_id"], attempt_id=attempt_b, owner_token="owner-b")
    assert recovered["action"] == "retire"
    # The superseded generation's exact file is gone and the binding fell back to pending.
    assert not os.path.exists(old_uri)
    assert metadb.reconcile_linear_checkpoint(admission["task_id"])["phase"] == "pending"

    # A replacement generation can now be reserved and committed by the current attempt.
    new = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt_b, owner_token="owner-b",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    assert new["generation"] != old["generation"] and new["uri"] != old_uri
    evidence = lc.materialize_and_commit_checkpoint(
        store, task_id=admission["task_id"], attempt_id=attempt_b, owner_token="owner-b",
        candidate=new, content=_parquet_bytes(5))
    assert evidence.rows == 5
    assert lc.reconcile_checkpoint(admission["task_id"]) == evidence


def test_a_fenced_old_owner_cannot_mutate_new_state(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    admission, _created = _submit(values)
    task_a = metadb.claim_linear_checkpoint_task(admission["task_id"], "owner-a")
    attempt_a = task_a["attempts"][-1]["id"]
    old = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt_a, owner_token="owner-a",
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    _materialize_only(store, {
        "task_id": admission["task_id"], "attempt_id": attempt_a, "owner": "owner-a",
        "candidate": old}, _parquet_bytes(3))
    _expire_lease(attempt_a)
    task_b = metadb.claim_linear_checkpoint_task(admission["task_id"], "owner-b")
    attempt_b = task_b["attempts"][-1]["id"]

    # The late old owner is fenced from recovery, abort, and commit of any state.
    for call in (
        lambda: metadb.reattach_or_retire_linear_checkpoint(
            admission["task_id"], attempt_a, "owner-a"),
        lambda: metadb.abort_linear_checkpoint_candidate(
            admission["task_id"], attempt_a, "owner-a"),
        lambda: metadb.commit_linear_checkpoint(
            task_id=admission["task_id"], attempt_id=attempt_a, owner_token="owner-a",
            namespace_id=old["namespace_id"], writer_token=old["writer_token"],
            lock_token=old["lock_token"], generation=old["generation"], rows=3, size_bytes=10,
            content_sha256="a" * 64, schema_sha256="b" * 64, dev=1, ino=1),
    ):
        with pytest.raises(RuntimeError, match="stale or fenced"):
            call()
    # The current attempt still owns recovery.
    assert lc.recover_checkpoint(
        store, task_id=admission["task_id"], attempt_id=attempt_b,
        owner_token="owner-b")["action"] == "retire"


# --------------------------------------------------------------------------- release

def test_release_committed_truth_and_then_reclaim(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    lc.materialize_and_commit_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
        candidate=ctx["candidate"], content=_parquet_bytes(6))
    # A committed owner is retained against reclaim until an explicit release.
    assert metadb.claim_local_result_reclaims(store.namespace_id, limit=50) == []
    _expire_lease(ctx["attempt_id"])

    assert lc.release_checkpoint(ctx["task_id"]) is True
    # Owner + entire hidden lifecycle removed; artifact keeps bytes but is now unreferenced.
    assert _owner_count(uri) == 0
    with metadb.session() as s:
        assert s.get(metadb.DurableCheckpoint, ctx["task_id"]) is None
        assert s.get(metadb.DurableTask, ctx["task_id"]) is None
        assert s.scalar(select(func.count()).select_from(metadb.DurableTaskAttempt).where(
            metadb.DurableTaskAttempt.task_id == ctx["task_id"])) == 0
    # Now reclaimable, and bounded GC removes the exact bytes.
    store.prune_results()
    assert not os.path.exists(uri)
    # Idempotent: a second release reports nothing to release.
    assert lc.release_checkpoint(ctx["task_id"]) is False


def test_release_refuses_a_live_attempt_lease(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    _commit(store, ctx, _parquet_bytes(4))  # commit leaves the attempt lease live
    with pytest.raises(RuntimeError, match="live attempt lease"):
        metadb.release_linear_checkpoint(ctx["task_id"])
    assert _owner_count(ctx["candidate"]["uri"]) == 1


def test_release_fails_closed_and_preserves_truth_on_inconsistency(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    _commit(store, ctx, _parquet_bytes(5))
    _expire_lease(ctx["attempt_id"])
    # Corrupt the sole owner so the committed set is inconsistent.
    with metadb.session() as s:
        ref = s.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri))
        s.delete(ref)
    with pytest.raises(RuntimeError, match="inconsistent"):
        metadb.release_linear_checkpoint(ctx["task_id"])
    # Primary truth (checkpoint + artifact bytes) is preserved.
    with metadb.session() as s:
        assert s.get(metadb.DurableCheckpoint, ctx["task_id"]).phase == "committed"
    assert os.path.isfile(uri)


# ----------------------------------------------------------------- retention / GC

def test_committed_owner_survives_reader_expiry_and_restart(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    evidence = _commit(store, ctx, _parquet_bytes(4))

    # Temporary reader comes and goes; its ephemeral lease must not retire the durable owner.
    guard, _ev = lc.reopen_checkpoint(store, ctx["task_id"])
    guard.close()
    assert metadb.claim_local_result_reclaims(store.namespace_id, limit=50) == []

    # A full maintenance pass and a fresh Hub process (new storage object) preserve truth.
    store.prune_results()
    assert os.path.isfile(uri) and _owner_count(uri) == 1
    restarted = LocalStorage(str(tmp_path / "outputs"))
    restarted.prune_results()
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence
    guard2, _ev2 = lc.reopen_checkpoint(restarted, ctx["task_id"])
    guard2.close()


# ----------------------------------------------------------------- canvas deletion

def _delete_canvas(canvas_id: str) -> None:
    metadb.delete_canvas_cascade(canvas_id)


def test_canvas_deletion_releases_committed_and_frees_bytes(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    ctx = _reserve(values, store)
    uri = ctx["candidate"]["uri"]
    _commit(store, ctx, _parquet_bytes(5))
    _set_task_status(ctx["task_id"], "done")

    _delete_canvas(values["canvas_id"])

    # The hidden lifecycle is gone and the durable owner no longer pins the artifact.
    with metadb.session() as s:
        assert s.get(metadb.DurableCheckpoint, ctx["task_id"]) is None
        assert s.get(metadb.DurableTask, ctx["task_id"]) is None
    assert _owner_count(uri) == 0
    store.prune_results()
    assert not os.path.exists(uri)


def test_canvas_deletion_retires_reserved_uncommitted(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    ctx = _reserve(values, store)
    uri = ctx["candidate"]["uri"]
    _materialize_only(store, ctx, _parquet_bytes(3))
    _set_task_status(ctx["task_id"], "failed")

    _delete_canvas(values["canvas_id"])

    with metadb.session() as s:
        assert s.get(metadb.DurableCheckpoint, ctx["task_id"]) is None
    store.prune_results()
    assert not os.path.exists(uri)


def test_canvas_deletion_blocks_on_an_active_checkpoint_task(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    ctx = _reserve(values, store)  # claim leaves the task running
    with pytest.raises(metadb.ActiveBackendJobsError, match="active durable task"):
        _delete_canvas(values["canvas_id"])
    assert metadb.reconcile_linear_checkpoint(ctx["task_id"])["phase"] == "reserved"


def test_canvas_deletion_fails_closed_on_inconsistent_committed_owner(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    ctx = _reserve(values, store)
    uri = ctx["candidate"]["uri"]
    _commit(store, ctx, _parquet_bytes(5))
    _set_task_status(ctx["task_id"], "done")
    with metadb.session() as s:  # drop the sole owner to corrupt committed truth
        s.delete(s.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri)))

    with pytest.raises(RuntimeError, match="inconsistent"):
        _delete_canvas(values["canvas_id"])
    # The canvas and its checkpoint are preserved by the rolled-back deletion.
    with metadb.session() as s:
        assert s.get(metadb.Canvas, values["canvas_id"]) is not None
        assert s.get(metadb.DurableCheckpoint, ctx["task_id"]).phase == "committed"


def test_reused_canvas_id_does_not_inherit_retained_checkpoint(tmp_path):
    store = _storage(tmp_path)
    values = _identity()
    ctx = _reserve(values, store)
    _commit(store, ctx, _parquet_bytes(3))
    _set_task_status(ctx["task_id"], "done")
    _delete_canvas(values["canvas_id"])

    # Recreate a canvas under the exact same id; it must not inherit the retired checkpoint owner.
    with metadb.session() as s:
        s.add(metadb.Canvas(id=values["canvas_id"], owner_id=values["uid"],
                            name="reused", doc="{}"))
    assert _owner_count(ctx["candidate"]["uri"]) == 0
    with metadb.session() as s:
        assert s.get(metadb.DurableCheckpoint, ctx["task_id"]) is None


# ----------------------------------------------------------------- backup / restore

@pytest.fixture
def _isolated_db(tmp_path_factory):
    """A fresh metadata DB so a whole-database restore audit is not tripped by other tests' rows."""
    from hub.settings import settings

    orig_url = settings.database_url
    orig_engine, orig_session = metadb._engine, metadb._Session
    settings.database_url = f"sqlite:///{tmp_path_factory.mktemp('cp-audit') / 'metadata.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = orig_url
        metadb._engine, metadb._Session = orig_engine, orig_session


def test_restore_audit_accepts_complete_sets_and_rejects_corruption(_isolated_db, tmp_path):
    store = _storage(tmp_path)
    committed_ctx = _reserve(_identity(), store)
    _commit(store, committed_ctx, _parquet_bytes(6))
    reserved_ctx = _reserve(_identity(), store)
    _materialize_only(store, reserved_ctx, _parquet_bytes(2))

    report = {row["task_id"]: row for row in metadb.linear_checkpoint_restore_audit()}
    assert report[committed_ctx["task_id"]]["phase"] == "committed"
    assert report[reserved_ctx["task_id"]]["phase"] == "reserved"

    # A dangling durable owner with no committed checkpoint rejects the restore.
    with metadb.session() as s:
        s.add(metadb.LocalResultReference(
            uri=reserved_ctx["candidate"]["uri"],
            owner_kind=metadb._LINEAR_CHECKPOINT_OWNER_KIND, owner_key="ghost"))
    with pytest.raises(RuntimeError, match="no committed checkpoint"):
        metadb.linear_checkpoint_restore_audit()


def test_restore_audit_rejects_a_missing_committed_artifact(_isolated_db, tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    _commit(store, ctx, _parquet_bytes(4))
    # Simulate a restore that dropped the artifact inventory row for a committed checkpoint.
    with metadb.session() as s:
        s.delete(s.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri)))
        s.delete(s.get(metadb.LocalResultArtifact, uri))
    with pytest.raises(RuntimeError, match="inconsistent"):
        metadb.linear_checkpoint_restore_audit()


# ----------------------------------------------------------------- #449 / #450 / #451 rework

def test_prune_results_inside_recovery_window_preserves_uncommitted_candidate(tmp_path):
    """#449: maintenance must not destroy a reserved candidate before reattach."""
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    content = _parquet_bytes(6)
    _materialize_only(store, ctx, content)
    assert os.path.isfile(uri)

    # Drop the producer (lock free, lease still alive) and run the exact startup/periodic reclaim path.
    store.prune_results()
    assert os.path.isfile(uri)
    assert _artifact(uri) is not None
    assert metadb.reconcile_linear_checkpoint(ctx["task_id"])["phase"] == "reserved"
    assert metadb.claim_local_result_reclaims(store.namespace_id, limit=50) == []

    recovered = lc.recover_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    assert recovered["action"] == "reattach"
    evidence = lc.commit_reattached_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
        candidate=recovered["candidate"])
    assert evidence.rows == 6
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence


def test_pre_commit_inode_swap_cannot_become_committed_truth(tmp_path):
    """#450 window 1: a path swap between seal and commit fails closed (no attacker evidence)."""
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    candidate = ctx["candidate"]
    writer = store.materialize_checkpoint(candidate)
    try:
        writer.write(_parquet_bytes(9))
        metadb.bind_linear_checkpoint_materialization(
            task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
            uri=candidate["uri"], dev=writer.identity()[0], ino=writer.identity()[1])
        writer.seal()
        proof = store.prove_checkpoint(writer)
        assert proof.evidence["rows"] == 9
        name, _ = store._result_names(candidate["uri"])
        replacement = f"{name}.replacement"
        fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=store._result_dir_fd)
        os.write(fd, _parquet_bytes(2))
        os.close(fd)
        os.replace(replacement, name,
                   src_dir_fd=store._result_dir_fd, dst_dir_fd=store._result_dir_fd)
        # Path no longer names the held inode; the final recheck fails closed.
        with pytest.raises(RuntimeError, match="identity changed|single-link"):
            proof.recheck()
        proof.close()
    finally:
        writer.abort()
    # Nothing committed; reserved binding may still exist until abort.
    assert metadb.reconcile_linear_checkpoint(ctx["task_id"])["phase"] == "reserved"


def test_reattach_after_inode_swap_fails_closed(tmp_path):
    """#450 window 2: reattach refuses a swapped parquet that keeps the lock file."""
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    uri = ctx["candidate"]["uri"]
    _materialize_only(store, ctx, _parquet_bytes(9))
    name, _ = store._result_names(uri)
    replacement = f"{name}.replacement"
    fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                 dir_fd=store._result_dir_fd)
    os.write(fd, _parquet_bytes(2))
    os.close(fd)
    os.replace(replacement, name,
               src_dir_fd=store._result_dir_fd, dst_dir_fd=store._result_dir_fd)
    recovered = lc.recover_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    assert recovered["action"] == "reattach"
    with pytest.raises(RuntimeError, match="materialized identity"):
        lc.commit_reattached_checkpoint(
            store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"],
            owner_token=ctx["owner"], candidate=recovered["candidate"])


def test_indeterminate_reconcile_readback_releases_never_aborts(tmp_path, monkeypatch):
    """#451: when reconcile itself raises after a durable commit, keep the committed bytes."""
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    content = _parquet_bytes(4)
    real_commit = metadb.commit_linear_checkpoint
    real_reconcile = metadb.reconcile_linear_checkpoint

    def commit_then_lose(**kwargs):
        real_commit(**kwargs)
        raise RuntimeError("response lost after durable commit")

    def reconcile_raises(task_id):
        raise RuntimeError("correlated DB outage during reconcile")

    monkeypatch.setattr(metadb, "commit_linear_checkpoint", commit_then_lose)
    monkeypatch.setattr(metadb, "reconcile_linear_checkpoint", reconcile_raises)
    with pytest.raises(RuntimeError, match="response lost"):
        _commit(store, ctx, content)
    monkeypatch.undo()

    assert os.path.isfile(ctx["candidate"]["uri"])
    monkeypatch.setattr(metadb, "reconcile_linear_checkpoint", real_reconcile)
    evidence = lc.reconcile_checkpoint(ctx["task_id"])
    assert evidence is not None and evidence.rows == 4
    guard, reopened = lc.reopen_checkpoint(store, ctx["task_id"])
    try:
        assert reopened == evidence
    finally:
        guard.close()
