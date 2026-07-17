"""Materialize, prove, commit, reconcile, and reopen one exact hidden linear checkpoint.

This is the second #418-dependent leaf. It turns one reserved DB-only candidate (persisted by
``metadb.reserve_linear_checkpoint_candidate``) into committed checkpoint truth backed by a single
durable owner, and can rebuild a validated reader after the producing process is gone.

It also owns the exact hidden lifecycle after commit: reattaching or retiring an uncommitted
candidate under a fenced recovery attempt, aborting uncommitted work, and explicitly releasing
committed truth. It deliberately owns no product route/worker/Jobs, generic checkpoint SPI, GC
policy, or backup/restore driver (retired/aborted/released artifacts become reclaimable through the
existing bounded local-result GC). Checkpoint truth is proven from the exact committed bytes on disk;
no caller-supplied rows, hash, or schema can establish it.
"""

from __future__ import annotations

import contextlib
import datetime
import os

from pydantic import BaseModel, ConfigDict, Field

from hub import metadb


class CheckpointEvidence(BaseModel):
    """One strict, path-free record of committed checkpoint truth.

    Only bounded canonical identities, digests, and counts are carried. No storage path, root, lock
    token, raw document, provider, or traceback data is admitted, and extra fields are forbidden so a
    wider evidence surface cannot silently leak in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=256)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_node_id: str = Field(min_length=1, max_length=256)
    output_port_id: str = Field(min_length=1, max_length=128)
    task_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer_attempt_id: str = Field(min_length=1, max_length=256)
    generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: int = Field(ge=0)
    bytes: int = Field(gt=0)
    committed_dev: int = Field(ge=0)
    committed_ino: int = Field(ge=0)
    committed_at: datetime.datetime

    @classmethod
    def from_doc(cls, doc: dict) -> "CheckpointEvidence":
        """Project the internal committed doc onto the strict, extra-forbidden evidence surface."""
        return cls(
            task_id=doc["task_id"], checkpoint_id=doc["checkpoint_id"],
            checkpoint_node_id=doc["checkpoint_node_id"], output_port_id=doc["output_port_id"],
            task_intent_sha256=doc["task_intent_sha256"],
            graph_prefix_sha256=doc["graph_prefix_sha256"],
            input_manifest_sha256=doc["input_manifest_sha256"],
            producer_attempt_id=doc["attempt_id"], generation=doc["generation"],
            content_sha256=doc["content_sha256"], schema_sha256=doc["schema_sha256"],
            rows=doc["rows"], bytes=doc["bytes"],
            committed_dev=doc["dev"], committed_ino=doc["ino"],
            committed_at=doc["committed_at"])


def materialize_and_commit_checkpoint(
        storage, *, task_id: str, attempt_id: str, owner_token: str,
        candidate: dict, content: bytes) -> CheckpointEvidence:
    """Materialize the reserved candidate, prove it from one held descriptor, and fence-commit it.

    ``candidate`` is exactly the binding returned by the reservation/candidate readback. ``content``
    is the producer's parquet bytes; its rows/hash/schema are never trusted — evidence is proven from
    the exact materialized inode. On an unknown commit response the writer keeps its fence only when a
    DB readback proves the reservation committed; otherwise the exact file and lock are cleaned.
    """
    writer = storage.materialize_checkpoint(candidate)
    proof = None
    try:
        writer.write(content)
        writer.seal()
        proof = storage.open_checkpoint_proof(candidate["uri"], writer.lock_fileno())
        evidence = proof.evidence
        # The final identity check occurs immediately before the fenced SQL commit while the exact
        # read descriptor is still held open.
        proof.recheck()
        doc = metadb.commit_linear_checkpoint(
            task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
            namespace_id=candidate["namespace_id"], writer_token=candidate["writer_token"],
            lock_token=candidate["lock_token"], generation=candidate["generation"],
            rows=evidence["rows"], size_bytes=evidence["bytes"],
            content_sha256=evidence["content_sha256"], schema_sha256=evidence["schema_sha256"],
            dev=evidence["dev"], ino=evidence["ino"])
    except Exception:
        if proof is not None:
            proof.close()
        committed = False
        try:
            state = metadb.reconcile_linear_checkpoint(task_id)
            committed = isinstance(state, dict) and state.get("phase") == "committed"
        except Exception:
            committed = False
        if committed:
            # Commit-before-response-loss: never erase committed truth, only drop the write FD/lock.
            writer.release()
        else:
            writer.abort()
        raise
    proof.close()
    writer.release()
    return CheckpointEvidence.from_doc(doc)


def reconcile_checkpoint(task_id: str) -> CheckpointEvidence | None:
    """Return committed evidence only when the DB proves complete truth; None while still reserved."""
    state = metadb.reconcile_linear_checkpoint(task_id)
    if isinstance(state, dict) and state.get("phase") == "committed":
        return CheckpointEvidence.from_doc(state)
    return None


def reopen_checkpoint(storage, task_id: str) -> tuple[object, CheckpointEvidence]:
    """Rebuild a held, fully revalidated reader guard for one committed checkpoint after disposal."""
    committed = metadb.linear_checkpoint_committed(task_id)
    if committed is None:
        raise RuntimeError("checkpoint is not committed")
    guard = storage.reopen_checkpoint(committed)
    return guard, CheckpointEvidence.from_doc(committed)


def _reclaim_retired(storage, outcome: dict) -> None:
    """Promptly delete a just-retired/aborted candidate; bounded GC is the durable guarantee."""
    uri = outcome.get("uri")
    if uri is None:
        return
    # The DB already marked the exact artifact deleting (reclaimable). A transient filesystem failure
    # here must not undo that outcome — the local-result reaper finishes the exact deletion later.
    with contextlib.suppress(OSError, RuntimeError):
        storage.discard_checkpoint_artifact(
            uri, outcome["delete_token"], outcome.get("lock_token"))


def abort_checkpoint(storage, *, task_id: str, attempt_id: str, owner_token: str) -> None:
    """Abort the current uncommitted candidate under its exact attempt and reclaim its exact file.

    Refuses committed truth and is idempotent under response loss: a replay finds nothing bound and
    leaves already-reclaimable state untouched.
    """
    outcome = metadb.abort_linear_checkpoint_candidate(task_id, attempt_id, owner_token)
    _reclaim_retired(storage, outcome)


def release_checkpoint(task_id: str) -> bool:
    """Explicitly release committed truth so its bytes become reclaimable; idempotent when gone."""
    return metadb.release_linear_checkpoint(task_id) is not None


def recover_checkpoint(
        storage, *, task_id: str, attempt_id: str, owner_token: str) -> dict:
    """Decide, under the current fenced attempt, whether to reattach, retire, reserve, or read truth.

    ``reattach`` returns the exact same-generation candidate for :func:`commit_reattached_checkpoint`;
    ``retire`` has already abandoned the superseded candidate's exact file before returning; ``reserve``
    means nothing is bound; ``committed`` returns already-won evidence a late owner must not mutate.
    """
    outcome = metadb.reattach_or_retire_linear_checkpoint(task_id, attempt_id, owner_token)
    action = outcome["action"]
    if action == "committed":
        return {"action": "committed",
                "evidence": CheckpointEvidence.from_doc(outcome["committed"])}
    if action == "reattach":
        return {"action": "reattach", "candidate": outcome["candidate"]}
    if action == "retire":
        _reclaim_retired(storage, outcome)
        return {"action": "retire"}
    return {"action": "reserve"}


def commit_reattached_checkpoint(
        storage, *, task_id: str, attempt_id: str, owner_token: str,
        candidate: dict) -> CheckpointEvidence:
    """Prove and fence-commit an already-materialized same-generation candidate after reattach.

    The exact reserved file is never re-created; it is reopened under its own lock, proven from the
    held descriptor, and committed. The pre-existing file is never destroyed on a failure, and any
    rejection is surfaced unchanged; commit-response loss is recovered by the caller through
    :func:`reconcile_checkpoint`, exactly as :func:`materialize_and_commit_checkpoint` requires.
    """
    lock_fd = storage.reattach_checkpoint(candidate)
    proof = None
    try:
        proof = storage.open_checkpoint_proof(candidate["uri"], lock_fd)
        evidence = proof.evidence
        proof.recheck()
        doc = metadb.commit_linear_checkpoint(
            task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
            namespace_id=candidate["namespace_id"], writer_token=candidate["writer_token"],
            lock_token=candidate["lock_token"], generation=candidate["generation"],
            rows=evidence["rows"], size_bytes=evidence["bytes"],
            content_sha256=evidence["content_sha256"], schema_sha256=evidence["schema_sha256"],
            dev=evidence["dev"], ino=evidence["ino"])
    finally:
        if proof is not None:
            proof.close()
        os.close(lock_fd)
    return CheckpointEvidence.from_doc(doc)
