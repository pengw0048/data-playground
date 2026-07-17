"""Installed-wheel conformance command for one hidden linear checkpoint.

Certifies the complete #418/#425/#426 lifecycle through production APIs only:

``python -m hub.checkpoint_conformance``

Creates and removes its own temporary SQLite workspace. Failures emit one bounded
``stage: code`` line on stderr and never leak paths, tokens, frozen documents, or
tracebacks. Success prints exactly ``checkpoint conformance passed``.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path


class _CheckFailed(Exception):
    def __init__(self, stage: str, code: str):
        self.stage = stage
        self.code = code


def _fail(stage: str, code: str) -> None:
    raise _CheckFailed(stage, code)


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


def _bind_workspace(workspace: Path) -> None:
    """Point process settings at a command-owned workspace; never use developer metadata."""
    from hub import metadb
    from hub.settings import settings

    data_dir = workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    os.environ["DP_WORKSPACE"] = str(workspace)
    os.environ["DP_DATA_DIR"] = str(data_dir)
    os.environ.pop("DP_DATABASE_URL", None)
    os.environ.pop("DP_PLUGINS", None)
    os.environ.pop("DP_EXECUTION", None)
    os.environ.pop("DP_STORAGE", None)
    os.environ.pop("DP_STORAGE_URL", None)
    settings.workspace = str(workspace)
    settings.data_dir = str(data_dir)
    settings.database_url = f"sqlite:///{workspace / 'dataplay.db'}"
    settings.plugin_modules = []
    settings.execution = ""
    if metadb._engine is not None:
        metadb._engine.dispose()
    metadb._engine = metadb._Session = None
    metadb.init_db()


def _dispose_db() -> None:
    from hub import metadb

    if metadb._engine is not None:
        metadb._engine.dispose()
    metadb._engine = metadb._Session = None


def _admit(canvas_id: str, submission: str) -> dict:
    """Construct canonical frozen documents and admit one hidden checkpoint through production APIs."""
    from hub import metadb

    uid = metadb.DEFAULT_USER_ID
    task_id = metadb.durable_task_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    graph = {
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {
                "title": "source", "config": {"table": "events"}}},
            {"id": "checkpoint", "type": "write", "data": {
                "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
            {"id": "final", "type": "write", "data": {
                "title": "final", "config": {"filename": "final.parquet"}}},
        ],
        "edges": [
            {"id": "e1", "source": "source", "target": "checkpoint",
             "sourceHandle": "out", "targetHandle": "in"},
            {"id": "e2", "source": "checkpoint", "target": "final",
             "sourceHandle": "out", "targetHandle": "in"},
        ],
    }
    intent = {
        "destination": {
            "logicalUri": f"managed://conformance/{submission}/final.parquet",
            "name": "final", "provider": "managed-local-file"},
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "final", "provenance": "run",
            "fieldMappings": []}, "parents": []},
    }
    try:
        admission, created = metadb.submit_linear_checkpoint_task(
            uid=uid, canvas_id=canvas_id, submission_id=submission,
            final_target_node_id="final", checkpoint_id=f"cp:{submission}",
            checkpoint_node_id="checkpoint", output_port_id="out",
            task_intent_sha256="a" * 64, graph_prefix_sha256="b" * 64,
            input_manifest_sha256=hashlib.sha256(b"[]").hexdigest(),
            graph_doc=graph, input_manifest=[], write_intent=intent)
    except Exception:
        _fail("admission", "submit_failed")
    if not created or admission.get("task_id") != task_id:
        _fail("admission", "submit_inconsistent")
    return admission


def _create_canvas() -> str:
    from hub import metadb

    try:
        root = metadb.local_workspace_root()
        created = metadb.workspace_create_canvas_action(
            uid=metadb.DEFAULT_USER_ID, container_id=root["id"],
            expected_container_version=root["version"],
            name=f"checkpoint-conformance-{uuid.uuid4().hex[:8]}")
    except Exception:
        _fail("setup", "canvas_create_failed")
    return str(created["id"])


def _claim(task_id: str, owner: str) -> dict:
    from hub import metadb

    try:
        task = metadb.claim_linear_checkpoint_task(task_id, owner)
    except Exception:
        _fail("claim", "claim_failed")
    if task is None or not task.get("attempts"):
        _fail("claim", "claim_empty")
    return task


def _reserve(store, task_id: str, attempt_id: str, owner: str) -> dict:
    from hub import metadb

    try:
        return metadb.reserve_linear_checkpoint_candidate(
            task_id=task_id, attempt_id=attempt_id, owner_token=owner,
            namespace_id=store.namespace_id, storage_root=store.result_root,
            writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    except Exception:
        _fail("reserve", "reserve_failed")


def _expire(attempt_id: str) -> None:
    from hub import metadb

    with metadb.session() as s:
        attempt = s.get(metadb.DurableTaskAttempt, attempt_id)
        if attempt is None:
            _fail("lease", "attempt_missing")
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=300)


def _positive(workspace: Path) -> None:
    from hub import linear_checkpoint as lc
    from hub import metadb
    from hub.storage import LocalStorage

    canvas_id = _create_canvas()
    admission = _admit(canvas_id, uuid.uuid4().hex)
    task_id = admission["task_id"]
    owner = f"conformance-owner-{uuid.uuid4().hex[:12]}"
    store = LocalStorage(str(workspace / "outputs"))

    task = _claim(task_id, owner)
    attempt_id = task["attempts"][-1]["id"]
    candidate = _reserve(store, task_id, attempt_id, owner)
    # Reservation response-loss: exact DB readback must equal the reserved binding.
    try:
        replay = metadb.linear_checkpoint_candidate(task_id)
    except Exception:
        _fail("reserve", "readback_failed")
    if replay != candidate:
        _fail("reserve", "readback_mismatch")

    content = _parquet_bytes(7)
    try:
        evidence = lc.materialize_and_commit_checkpoint(
            store, task_id=task_id, attempt_id=attempt_id, owner_token=owner,
            candidate=candidate, content=content)
    except Exception:
        _fail("commit", "commit_failed")
    if evidence.rows != 7 or evidence.content_sha256 != hashlib.sha256(content).hexdigest():
        _fail("commit", "evidence_mismatch")

    # Commit response-loss: discard the in-memory evidence and reconcile the original.
    try:
        recovered = lc.reconcile_checkpoint(task_id)
    except Exception:
        _fail("reconcile", "reconcile_failed")
    if recovered != evidence:
        _fail("reconcile", "evidence_mismatch")

    # Dispose and reconstruct storage; reopen must fully revalidate the committed artifact.
    del store
    restarted = LocalStorage(str(workspace / "outputs"))
    try:
        guard, reopened = lc.reopen_checkpoint(restarted, task_id)
    except Exception:
        _fail("reopen", "reopen_failed")
    try:
        if reopened != evidence:
            _fail("reopen", "evidence_mismatch")
        guard.check()
    finally:
        guard.close()

    # Bounded maintenance / GC must retain the durable owner.
    try:
        restarted.recover_orphans()
        restarted.prune_results()
    except Exception:
        _fail("retention", "maintenance_failed")
    if lc.reconcile_checkpoint(task_id) != evidence:
        _fail("retention", "owner_lost")
    if metadb.claim_local_result_reclaims(restarted.namespace_id, limit=50):
        _fail("retention", "premature_reclaim")

    _expire(attempt_id)
    try:
        released = lc.release_checkpoint(task_id)
    except Exception:
        _fail("release", "release_failed")
    if not released:
        _fail("release", "release_empty")
    try:
        restarted.prune_results()
    except Exception:
        _fail("reclaim", "prune_failed")
    uri = candidate["uri"]
    if os.path.exists(uri):
        _fail("reclaim", "bytes_retained")
    if lc.release_checkpoint(task_id):
        _fail("release", "not_idempotent")


def _expect_fail(stage: str, code: str, fn) -> None:
    try:
        fn()
    except _CheckFailed:
        raise
    except Exception:
        return
    _fail(stage, code)


def _negatives(workspace: Path) -> None:
    """Exercise certified negative modes through production lifecycle entry points only."""
    from hub import linear_checkpoint as lc
    from hub import metadb
    from hub.storage import LocalStorage

    workspace.mkdir(parents=True, exist_ok=True)
    canvas_id = _create_canvas()
    store = LocalStorage(str(workspace / "outputs"))

    # stale token / fenced lease cannot commit
    admission = _admit(canvas_id, uuid.uuid4().hex)
    task = _claim(admission["task_id"], "owner-a")
    attempt_a = task["attempts"][-1]["id"]
    candidate = _reserve(store, admission["task_id"], attempt_a, "owner-a")
    _expire(attempt_a)
    _expect_fail("stale_token", "accepted", lambda: lc.materialize_and_commit_checkpoint(
        store, task_id=admission["task_id"], attempt_id=attempt_a, owner_token="owner-a",
        candidate=candidate, content=_parquet_bytes(2)))

    # lease transfer/retire: new attempt retires superseded candidate before replacement
    task_b = _claim(admission["task_id"], "owner-b")
    attempt_b = task_b["attempts"][-1]["id"]
    try:
        outcome = lc.recover_checkpoint(
            store, task_id=admission["task_id"], attempt_id=attempt_b, owner_token="owner-b")
    except Exception:
        _fail("lease_retire", "recover_failed")
    if outcome.get("action") != "retire":
        _fail("lease_retire", "unexpected_action")
    _expect_fail("stale_token", "accepted", lambda: metadb.abort_linear_checkpoint_candidate(
        admission["task_id"], attempt_a, "owner-a"))

    # fresh reservation + commit for remaining negative probes on a new admission
    admission2 = _admit(canvas_id, uuid.uuid4().hex)
    task2 = _claim(admission2["task_id"], "owner-c")
    attempt2 = task2["attempts"][-1]["id"]
    candidate2 = _reserve(store, admission2["task_id"], attempt2, "owner-c")

    # symlink / FIFO at the reserved path fail closed
    artifact_name, _lock = store._result_names(candidate2["uri"])
    target = os.path.join(store.result_root, artifact_name)
    os.symlink(os.path.join(str(workspace), "elsewhere.parquet"), target)
    _expect_fail("symlink", "accepted", lambda: store.materialize_checkpoint(candidate2))
    with contextlib.suppress(FileNotFoundError):
        os.unlink(target)
    os.mkfifo(target)
    _expect_fail("fifo", "accepted", lambda: store.materialize_checkpoint(candidate2))
    with contextlib.suppress(FileNotFoundError):
        os.unlink(target)

    # hardlink after materialize is rejected by held-FD proof
    writer = store.materialize_checkpoint(candidate2)
    try:
        writer.write(_parquet_bytes(3))
        writer.seal()
        os.link(artifact_name, "hardlink.parquet",
                src_dir_fd=store._result_dir_fd, dst_dir_fd=store._result_dir_fd)
        _expect_fail("hardlink", "accepted",
                     lambda: store.open_checkpoint_proof(candidate2["uri"], writer.lock_fileno()))
    finally:
        writer.abort()
    # Retire the reserved binding through the production abort path before the next probe.
    try:
        lc.abort_checkpoint(store, task_id=admission2["task_id"], attempt_id=attempt2,
                            owner_token="owner-c")
    except Exception:
        _fail("hardlink", "abort_failed")

    # wrong caller evidence cannot establish commit (foreign owner)
    candidate3 = _reserve(store, admission2["task_id"], attempt2, "owner-c")
    _expect_fail("wrong_authority", "accepted", lambda: metadb.commit_linear_checkpoint(
        task_id=admission2["task_id"], attempt_id=attempt2, owner_token="intruder",
        namespace_id=candidate3["namespace_id"], writer_token=candidate3["writer_token"],
        lock_token=candidate3["lock_token"], generation=candidate3["generation"],
        rows=1, size_bytes=10, content_sha256="c" * 64, schema_sha256="d" * 64, dev=1, ino=1))

    # commit response-loss path: commit succeeds, orchestrator raises, reconcile recovers
    real_commit = metadb.commit_linear_checkpoint

    def commit_then_lose(**kwargs):
        real_commit(**kwargs)
        raise RuntimeError("response lost")

    metadb.commit_linear_checkpoint = commit_then_lose  # type: ignore[method-assign]
    try:
        try:
            lc.materialize_and_commit_checkpoint(
                store, task_id=admission2["task_id"], attempt_id=attempt2,
                owner_token="owner-c", candidate=candidate3, content=_parquet_bytes(4))
        except Exception:
            pass
        else:
            _fail("commit_loss", "raise_missing")
        recovered = lc.reconcile_checkpoint(admission2["task_id"])
        if recovered is None or recovered.rows != 4:
            _fail("commit_loss", "reconcile_missed")
    finally:
        metadb.commit_linear_checkpoint = real_commit  # type: ignore[method-assign]

    # missing/mismatched owner fails closed on release
    from sqlalchemy import select

    _expire(attempt2)
    with metadb.session() as s:
        uri = candidate3["uri"]
        ref = s.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri))
        if ref is not None:
            s.delete(ref)
    _expect_fail("owner_mismatch", "accepted",
                 lambda: metadb.release_linear_checkpoint(admission2["task_id"]))

    # inode replacement after open is rejected by proof recheck
    admission3 = _admit(canvas_id, uuid.uuid4().hex)
    task3 = _claim(admission3["task_id"], "owner-d")
    attempt3 = task3["attempts"][-1]["id"]
    candidate4 = _reserve(store, admission3["task_id"], attempt3, "owner-d")
    writer = store.materialize_checkpoint(candidate4)
    proof = None
    try:
        writer.write(_parquet_bytes(3))
        writer.seal()
        proof = store.open_checkpoint_proof(candidate4["uri"], writer.lock_fileno())
        name, _ = store._result_names(candidate4["uri"])
        replacement = f"{name}.replacement"
        fd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=store._result_dir_fd)
        os.write(fd, _parquet_bytes(9))
        os.close(fd)
        os.replace(replacement, name,
                   src_dir_fd=store._result_dir_fd, dst_dir_fd=store._result_dir_fd)
        _expect_fail("inode_replace", "accepted", proof.recheck)
    finally:
        if proof is not None:
            proof.close()
        writer.abort()

    # cleanup retry: abort an unmaterialized reservation, then reclaim
    admission4 = _admit(canvas_id, uuid.uuid4().hex)
    task4 = _claim(admission4["task_id"], "owner-e")
    attempt4 = task4["attempts"][-1]["id"]
    candidate5 = _reserve(store, admission4["task_id"], attempt4, "owner-e")
    try:
        lc.abort_checkpoint(store, task_id=admission4["task_id"], attempt_id=attempt4,
                            owner_token="owner-e")
        # Idempotent response-loss replay of abort.
        lc.abort_checkpoint(store, task_id=admission4["task_id"], attempt_id=attempt4,
                            owner_token="owner-e")
    except Exception:
        _fail("cleanup_retry", "abort_failed")
    if metadb.linear_checkpoint_candidate(admission4["task_id"]) is not None:
        _fail("cleanup_retry", "still_bound")
    if os.path.exists(candidate5["uri"]):
        _fail("cleanup_retry", "bytes_retained")


def _run(workspace: Path) -> None:
    _bind_workspace(workspace)
    try:
        _positive(workspace)
        _negatives(workspace / "negative")
    finally:
        _dispose_db()


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags; the command owns its temporary workspace
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="dp-checkpoint-conformance-") as directory:
            try:
                _run(Path(directory))
            except _CheckFailed as exc:
                print(f"{exc.stage}: {exc.code}", file=sys.stderr)
                return 1
            except Exception:  # noqa: BLE001 — never leak paths, tokens, docs, or tracebacks
                print("conformance: internal_error", file=sys.stderr)
                return 1
    finally:
        logging.disable(previous)
        _dispose_db()
    print("checkpoint conformance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
