"""Materialize, prove, commit, reconcile, and reopen one exact hidden linear checkpoint."""

from __future__ import annotations

import datetime
import hashlib
import io
import os
import stat
import uuid

import pytest
from sqlalchemy import event, func, select

from hub import linear_checkpoint as lc
from hub import metadb
from hub.storage import LocalStorage

from hub.tests.test_linear_checkpoint_admission import (  # reuse the admission fixtures/helpers
    _identity, _submit, _metadata_schema)  # noqa: F401


def _parquet_bytes(rows: int = 5) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({
        "id": pa.array(list(range(rows)), pa.int64()),
        "label": pa.array([f"row-{i}" for i in range(rows)], pa.string()),
    })
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def _storage(tmp_path) -> LocalStorage:
    return LocalStorage(str(tmp_path / "outputs"))


def _reserve(values: dict, store: LocalStorage, *, owner: str = "lease-owner") -> dict:
    admission, _created = _submit(values)
    task = metadb.claim_linear_checkpoint_task(admission["task_id"], owner)
    assert task is not None
    attempt = task["attempts"][-1]
    candidate = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"], attempt_id=attempt["id"], owner_token=owner,
        namespace_id=store.namespace_id, storage_root=store.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    return {"task_id": admission["task_id"], "attempt_id": attempt["id"],
            "owner": owner, "candidate": candidate}


def _commit(store, ctx, content):
    return lc.materialize_and_commit_checkpoint(
        store, task_id=ctx["task_id"], attempt_id=ctx["attempt_id"],
        owner_token=ctx["owner"], candidate=ctx["candidate"], content=content)


def test_materialize_prove_commit_reconcile_and_reopen(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    content = _parquet_bytes(7)

    evidence = _commit(store, ctx, content)

    assert evidence.rows == 7
    assert evidence.bytes == len(content)
    assert evidence.content_sha256 == hashlib.sha256(content).hexdigest()
    assert evidence.generation == ctx["candidate"]["generation"]
    assert evidence.producer_attempt_id == ctx["attempt_id"]

    uri = ctx["candidate"]["uri"]
    assert os.path.isfile(uri)
    info = os.stat(uri)
    assert (evidence.committed_dev, evidence.committed_ino) == (info.st_dev, info.st_ino)
    assert info.st_size == len(content)
    # extra fields are forbidden on the strict evidence surface
    with pytest.raises(Exception):
        lc.CheckpointEvidence(**{**evidence.model_dump(), "storage_root": store.result_root})

    with metadb.session() as s:
        artifact = s.get(metadb.LocalResultArtifact, uri)
        assert artifact.state == "ready" and artifact.committed_at is not None
        assert artifact.writer_run_id is None and artifact.writer_token is None
        owners = s.scalars(select(metadb.LocalResultReference.owner_kind).where(
            metadb.LocalResultReference.uri == uri)).all()
        assert owners == [metadb._LINEAR_CHECKPOINT_OWNER_KIND]
        checkpoint = s.get(metadb.DurableCheckpoint, ctx["task_id"])
        assert checkpoint.phase == "committed"

    # reconcile is idempotent and returns identical committed evidence
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence

    # reopen after the producing storage object is gone: a validated held guard, never a raw path
    reopened_store = LocalStorage(str(tmp_path / "outputs"))
    guard, reopened_evidence = lc.reopen_checkpoint(reopened_store, ctx["task_id"])
    try:
        assert reopened_evidence == evidence
        assert not isinstance(guard, str)
        guard.check()  # post-consumption guard check
        assert metadb.local_result_read_active(uri, reopened_store.namespace_id, guard.reader_id)
    finally:
        guard.close()
    assert not metadb.local_result_read_active(uri, reopened_store.namespace_id, guard.reader_id)
    # committed truth survives an ordinary read-guard lifecycle
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence


def test_committed_owner_survives_reclaim_scan(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    evidence = _commit(store, ctx, _parquet_bytes(3))

    # A maintenance/GC reclaim pass must not touch an artifact with a durable owner.
    assert metadb.claim_local_result_reclaims(store.namespace_id, limit=50) == []
    assert metadb.local_result_lock_candidates(store.namespace_id, limit=50) == []
    assert os.path.isfile(ctx["candidate"]["uri"])
    assert lc.reconcile_checkpoint(ctx["task_id"]) == evidence


def test_commit_replay_returns_original_evidence_and_rejects_different_evidence(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    content = _parquet_bytes(4)
    evidence = _commit(store, ctx, content)
    candidate = ctx["candidate"]

    replay = metadb.commit_linear_checkpoint(
        task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
        namespace_id=candidate["namespace_id"], writer_token=candidate["writer_token"],
        lock_token=candidate["lock_token"], generation=candidate["generation"],
        rows=evidence.rows, size_bytes=evidence.bytes,
        content_sha256=evidence.content_sha256, schema_sha256=evidence.schema_sha256,
        dev=evidence.committed_dev, ino=evidence.committed_ino)
    assert lc.CheckpointEvidence.from_doc(replay) == evidence

    with pytest.raises(RuntimeError, match="replay changed committed evidence"):
        metadb.commit_linear_checkpoint(
            task_id=ctx["task_id"], attempt_id=ctx["attempt_id"], owner_token=ctx["owner"],
            namespace_id=candidate["namespace_id"], writer_token=candidate["writer_token"],
            lock_token=candidate["lock_token"], generation=candidate["generation"],
            rows=evidence.rows + 1, size_bytes=evidence.bytes,
            content_sha256=evidence.content_sha256, schema_sha256=evidence.schema_sha256,
            dev=evidence.committed_dev, ino=evidence.committed_ino)


def test_commit_rejects_foreign_owner_and_wrong_authority(tmp_path):
    # A held descriptor with valid evidence still cannot commit under a foreign lease/authority.
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    candidate = ctx["candidate"]
    writer = store.materialize_checkpoint(candidate)
    try:
        writer.write(_parquet_bytes(2))
        writer.seal()
        proof = store.open_checkpoint_proof(candidate["uri"], writer.lock_fileno())
        try:
            base = dict(
                task_id=ctx["task_id"], attempt_id=ctx["attempt_id"],
                namespace_id=candidate["namespace_id"], writer_token=candidate["writer_token"],
                lock_token=candidate["lock_token"], generation=candidate["generation"],
                rows=proof.evidence["rows"], size_bytes=proof.evidence["bytes"],
                content_sha256=proof.evidence["content_sha256"],
                schema_sha256=proof.evidence["schema_sha256"],
                dev=proof.evidence["dev"], ino=proof.evidence["ino"])
            with pytest.raises(RuntimeError, match="stale or fenced"):
                metadb.commit_linear_checkpoint(owner_token="intruder", **base)
            with pytest.raises(RuntimeError, match="does not match the reserved candidate"):
                metadb.commit_linear_checkpoint(
                    owner_token=ctx["owner"], **{**base, "writer_token": uuid.uuid4().hex})
        finally:
            proof.close()
        # Nothing committed; the reservation is still reserved and the writer can clean up.
        assert metadb.reconcile_linear_checkpoint(ctx["task_id"])["phase"] == "reserved"
    finally:
        writer.abort()


def test_stale_or_fenced_owner_cannot_commit(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    # Expire the current lease so the producer is fenced before it commits.
    with metadb.session() as s:
        attempt = s.get(metadb.DurableTaskAttempt, ctx["attempt_id"])
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=120)
    with pytest.raises(RuntimeError, match="stale or fenced"):
        _commit(store, ctx, _parquet_bytes(3))
    # The failed writer cleaned its exact file and lock; nothing committed.
    assert not os.path.exists(ctx["candidate"]["uri"])
    state = metadb.reconcile_linear_checkpoint(ctx["task_id"])
    assert state["phase"] == "reserved"


def test_commit_before_response_loss_keeps_committed_truth(tmp_path, monkeypatch):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    content = _parquet_bytes(6)
    real_commit = metadb.commit_linear_checkpoint

    def commit_then_lose_response(**kwargs):
        real_commit(**kwargs)
        raise RuntimeError("response lost after a durable commit")

    monkeypatch.setattr(metadb, "commit_linear_checkpoint", commit_then_lose_response)
    with pytest.raises(RuntimeError, match="response lost"):
        _commit(store, ctx, content)
    monkeypatch.undo()

    # The orchestrator kept the fence because the DB readback proved a commit; truth is intact.
    assert os.path.isfile(ctx["candidate"]["uri"])
    evidence = lc.reconcile_checkpoint(ctx["task_id"])
    assert evidence is not None
    assert evidence.content_sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("target", ["INSERT INTO local_result_references",
                                    "UPDATE local_result_artifacts"])
def test_commit_failure_rolls_back_and_cleans_the_exact_file(tmp_path, target):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    engine = metadb.engine()

    def fail_on_target(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith(target.upper()):
            raise RuntimeError("injected commit failure")

    event.listen(engine, "before_cursor_execute", fail_on_target)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            _commit(store, ctx, _parquet_bytes(3))
    finally:
        event.remove(engine, "before_cursor_execute", fail_on_target)

    # Rolled back to reserved, no durable owner leaked, and the failed file/lock were removed.
    assert not os.path.exists(ctx["candidate"]["uri"])
    with metadb.session() as s:
        assert s.scalar(select(func.count()).select_from(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == ctx["candidate"]["uri"])) == 0
        checkpoint = s.get(metadb.DurableCheckpoint, ctx["task_id"])
        assert checkpoint.phase == "reserved" and checkpoint.committed_at is None
        artifact = s.get(metadb.LocalResultArtifact, ctx["candidate"]["uri"])
        assert artifact.state == "writing" and artifact.committed_at is None


def test_materialize_fails_closed_on_symlink_and_fifo(tmp_path):
    for planter in ("symlink", "fifo"):
        store = _storage(tmp_path / planter)
        ctx = _reserve(_identity(), store)
        artifact_name, lock_name = store._result_names(ctx["candidate"]["uri"])
        target = os.path.join(store.result_root, artifact_name)
        if planter == "symlink":
            os.symlink(os.path.join(tmp_path, "elsewhere.parquet"), target)
        else:
            os.mkfifo(target)
        with pytest.raises((FileExistsError, RuntimeError, OSError)):
            store.materialize_checkpoint(ctx["candidate"])
        # The planted node is untouched; the writer never adopted a non-regular file.
        assert os.path.islink(target) or stat.S_ISFIFO(os.lstat(target).st_mode)
        # The reservation remains reserved and uncommitted.
        assert metadb.reconcile_linear_checkpoint(ctx["task_id"])["phase"] == "reserved"


def test_proof_rejects_hardlinked_artifact(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    candidate = ctx["candidate"]
    writer = store.materialize_checkpoint(candidate)
    try:
        writer.write(_parquet_bytes(3))
        writer.seal()
        artifact_name, _lock = store._result_names(candidate["uri"])
        # A second hard link makes the inode multi-link; the proof must refuse it.
        os.link(artifact_name, "hardlink.parquet",
                src_dir_fd=store._result_dir_fd, dst_dir_fd=store._result_dir_fd)
        with pytest.raises(RuntimeError, match="single-link"):
            store.open_checkpoint_proof(candidate["uri"], writer.lock_fileno())
    finally:
        writer.abort()


def test_proof_recheck_detects_inode_replacement_after_open(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    candidate = ctx["candidate"]
    writer = store.materialize_checkpoint(candidate)
    proof = None
    try:
        writer.write(_parquet_bytes(3))
        writer.seal()
        proof = store.open_checkpoint_proof(candidate["uri"], writer.lock_fileno())
        artifact_name, _lock = store._result_names(candidate["uri"])
        # Atomically swap a different inode in at the exact reserved path after the descriptor is held.
        replacement = f"{artifact_name}.replacement"
        fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=store._result_dir_fd)
        os.write(fd, _parquet_bytes(9))
        os.close(fd)
        os.replace(replacement, artifact_name,
                   src_dir_fd=store._result_dir_fd, dst_dir_fd=store._result_dir_fd)
        # The held inode lost its only link during the swap; the recheck fails closed.
        with pytest.raises(RuntimeError, match="identity changed|single-link"):
            proof.recheck()
    finally:
        if proof is not None:
            proof.close()
        writer.abort()


def test_evidence_read_detects_truncation_during_streaming(tmp_path, monkeypatch):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    candidate = ctx["candidate"]
    content = _parquet_bytes(400000)
    if len(content) <= 1024 * 1024:
        pytest.skip("payload is not large enough to exercise multi-chunk streaming")
    writer = store.materialize_checkpoint(candidate)
    try:
        writer.write(content)
        writer.seal()
        real_pread = os.pread
        state = {"truncated": False}

        def truncating_pread(fd, size, offset):
            data = real_pread(fd, size, offset)
            if not state["truncated"]:
                state["truncated"] = True
                artifact_name, _lock = store._result_names(candidate["uri"])
                victim = os.open(artifact_name, os.O_RDWR, dir_fd=store._result_dir_fd)
                try:
                    os.ftruncate(victim, 16)
                finally:
                    os.close(victim)
            return data

        monkeypatch.setattr(os, "pread", truncating_pread)
        with pytest.raises(RuntimeError, match="changed size during read"):
            store.open_checkpoint_proof(candidate["uri"], writer.lock_fileno())
    finally:
        writer.abort()


def test_reopen_detects_committed_evidence_mismatch(tmp_path):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    _commit(store, ctx, _parquet_bytes(5))
    uri = ctx["candidate"]["uri"]

    # Corrupt the committed content record so reopen's full evidence revalidation fails closed.
    with metadb.session() as s:
        checkpoint = s.get(metadb.DurableCheckpoint, ctx["task_id"])
        checkpoint.content_sha256 = "e" * 64
    reopened_store = LocalStorage(str(tmp_path / "outputs"))
    with pytest.raises(RuntimeError, match="disagrees with committed evidence"):
        lc.reopen_checkpoint(reopened_store, ctx["task_id"])
    assert os.path.isfile(uri)
