"""One fenced durable SparseOutput merge that publishes only a committed full candidate."""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from hub import linear_checkpoint as lc, metadb
from hub.local_writes import write_managed_local_file
from hub.merge_columns import (
    MergeColumnsIntentV1, merge_columns_publication_context,
    merge_sparse_output_candidate,
)
from hub.managed_sidecar_merge import (
    ManagedSidecarMergeIntentV1, managed_sidecar_merge_document,
    merge_managed_sidecar_candidate,
)
from hub.models import RunOutput, RunStatus, WriteIntent, WriteReceipt


_active_lock = threading.Lock()
_active: dict[str, threading.Thread] = {}


def _status(task_id: str, target_node_id: str, *, phase: str, progress: float) -> dict:
    doc = RunStatus(run_id=task_id, status="running", target_node_id=target_node_id,
                    progress=progress).model_dump()
    doc["merge_phase"] = phase
    return doc


def _failed(task_id: str, target_node_id: str, exc: BaseException) -> dict:
    if (isinstance(exc, metadb.ManagedLocalWriteConflict)
            and str(exc) == "replace expected head is stale"):
        code = "stale_expected_head"
    elif str(exc).startswith("checkpoint_invalid:"):
        code = "checkpoint_invalid"
    else:
        code = "merge_columns_write_failed"
    return RunStatus(run_id=task_id, status="failed", target_node_id=target_node_id,
                     error=code).model_dump()


def _cancelled(task_id: str, target_node_id: str) -> dict:
    return RunStatus(run_id=task_id, status="cancelled", target_node_id=target_node_id).model_dump()


def _done(
        task_id: str, target_node_id: str, intent: WriteIntent,
        receipt: WriteReceipt) -> RunStatus:
    return RunStatus(
        run_id=task_id, status="done", target_node_id=target_node_id, progress=1.0,
        total_rows=receipt.rows, outputs=[RunOutput(
            node_id=target_node_id, port_id="out", wire="dataset",
            publication_kind="catalog", outcome="committed",
            uri=receipt.publication.artifact_uri, table=intent.destination.name,
            version=receipt.publication.catalog_version, rows=receipt.rows,
            write_receipt=receipt)],
    )


def _heartbeat(task_id: str, attempt_id: str, owner_token: str) -> None:
    if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
        raise RuntimeError("merge columns task owner lost the lease")
    if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
        raise RuntimeError("cancelled")


def _publication_context(
        frozen: MergeColumnsIntentV1 | ManagedSidecarMergeIntentV1, *, task_id: str | None = None,
        attempt_id: str | None = None, owner_token: str | None = None):
    if isinstance(frozen, MergeColumnsIntentV1):
        return merge_columns_publication_context(
            frozen, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)
    document = managed_sidecar_merge_document(frozen)
    return metadb.MergeColumnsPublicationContext(
        merge_doc=document, merge_sha256=hashlib.sha256(document.encode()).hexdigest(),
        task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)


def _receipt(
        intent: WriteIntent, frozen: MergeColumnsIntentV1 | ManagedSidecarMergeIntentV1) -> WriteReceipt | None:
    prior = metadb.catalog_managed_local_write_receipt(
        intent.model_dump(by_alias=True, mode="json"),
        merge_publication=_publication_context(frozen))
    return WriteReceipt.model_validate(prior) if prior is not None else None


def _candidate_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


def _publish_candidate(
        deps, *, task_id: str, attempt_id: str, owner_token: str,
        target_node_id: str, frozen: MergeColumnsIntentV1 | ManagedSidecarMergeIntentV1,
        intent: WriteIntent) -> RunStatus:
    prior = _receipt(intent, frozen)
    if prior is not None:
        return _done(task_id, target_node_id, intent, prior)
    if not metadb.update_merge_columns_task_phase(
            task_id, attempt_id, owner_token, phase="publishing",
            status_doc=_status(
                task_id, target_node_id, phase="publishing", progress=0.8)):
        raise RuntimeError("merge columns publication owner is stale or fenced")
    try:
        guard, _evidence = lc.reopen_checkpoint(deps.storage, task_id)
    except Exception as exc:
        raise RuntimeError(f"checkpoint_invalid: {type(exc).__name__}") from exc

    def write_artifact(uri: str) -> None:
        _heartbeat(task_id, attempt_id, owner_token)
        fd = os.dup(guard.artifact_fileno())
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(fd, "rb") as source, open(uri, "wb") as target:
                fd = -1
                while chunk := source.read(1024 * 1024):
                    _heartbeat(task_id, attempt_id, owner_token)
                    target.write(chunk)
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        guard.check()

    try:
        receipt = write_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=intent,
            write_artifact=write_artifact, before_publish=lambda: (_heartbeat(
                task_id, attempt_id, owner_token), guard.check()),
            merge_publication=_publication_context(
                frozen, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token))
        guard.check()
        return _done(task_id, target_node_id, intent, receipt)
    except Exception:
        prior = _receipt(intent, frozen)
        if prior is not None:
            return _done(task_id, target_node_id, intent, prior)
        raise
    finally:
        with contextlib.suppress(Exception):
            guard.close()


def _worker(task_id: str, deps) -> None:
    owner_token = f"{uuid.uuid4().hex}:{threading.get_ident()}"
    try:
        claimed = metadb.claim_merge_columns_task(task_id, owner_token)
        if claimed is None:
            return
        attempt_id = str(claimed["attempts"][-1]["id"])
        target_node_id = str(claimed["target_node_id"])
        if claimed.get("merge_columns_producer") == "managed-sidecar":
            frozen = ManagedSidecarMergeIntentV1.model_validate(
                claimed["managed_sidecar_merge_intent"])
        else:
            frozen = MergeColumnsIntentV1.model_validate(claimed["merge_columns_intent"])
        intent = WriteIntent.model_validate(claimed["write_intent"])
        try:
            _heartbeat(task_id, attempt_id, owner_token)
            prior = _receipt(intent, frozen)
            if prior is not None:
                metadb.finish_durable_task_attempt(
                    task_id, attempt_id, owner_token,
                    _done(task_id, target_node_id, intent, prior).model_dump())
                return
            recovery = lc.recover_checkpoint(
                deps.storage, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)
            if recovery["action"] == "committed":
                status = _publish_candidate(
                    deps, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                    target_node_id=target_node_id, frozen=frozen, intent=intent)
            else:
                if recovery["action"] == "reattach":
                    lc.commit_reattached_checkpoint(
                        deps.storage, task_id=task_id, attempt_id=attempt_id,
                        owner_token=owner_token, candidate=recovery["candidate"])
                else:
                    if not metadb.update_merge_columns_task_phase(
                            task_id, attempt_id, owner_token, phase="validating",
                            status_doc=_status(
                                task_id, target_node_id, phase="validating", progress=0.1)):
                        return
                    _heartbeat(task_id, attempt_id, owner_token)
                    if not metadb.update_merge_columns_task_phase(
                            task_id, attempt_id, owner_token, phase="merging",
                            status_doc=_status(
                                task_id, target_node_id, phase="merging", progress=0.35)):
                        return
                    table = (merge_managed_sidecar_candidate(storage=deps.storage, intent=frozen)
                             if isinstance(frozen, ManagedSidecarMergeIntentV1)
                             else merge_sparse_output_candidate(storage=deps.storage, intent=frozen))
                    _heartbeat(task_id, attempt_id, owner_token)
                    candidate = metadb.reserve_linear_checkpoint_candidate(
                        task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                        namespace_id=deps.storage.namespace_id, storage_root=deps.storage.result_root,
                        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
                    try:
                        lc.materialize_and_commit_checkpoint(
                            deps.storage, task_id=task_id, attempt_id=attempt_id,
                            owner_token=owner_token, candidate=candidate,
                            content=_candidate_bytes(table))
                    except Exception:
                        # An unknown commit response is reconciled by the shared checkpoint lifecycle.
                        if lc.reconcile_checkpoint(task_id) is None:
                            # Before committed truth, cancellation and failures retire the exact
                            # reserved writer/lock binding under this same unexpired owner fence.
                            lc.abort_checkpoint(
                                deps.storage, task_id=task_id, attempt_id=attempt_id,
                                owner_token=owner_token)
                            raise
                if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token,
                        _cancelled(task_id, target_node_id))
                    return
                status = _publish_candidate(
                    deps, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                    target_node_id=target_node_id, frozen=frozen, intent=intent)
            if (status.status == "cancelled"
                    or metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token)):
                prior = _receipt(intent, frozen)
                status = (_done(task_id, target_node_id, intent, prior)
                          if prior is not None else RunStatus.model_validate(
                              _cancelled(task_id, target_node_id)))
            metadb.finish_durable_task_attempt(task_id, attempt_id, owner_token, status.model_dump())
        except BaseException as exc:
            prior = None
            with contextlib.suppress(Exception):
                prior = _receipt(intent, frozen)
            if prior is not None:
                status = _done(task_id, target_node_id, intent, prior).model_dump()
            elif metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                status = _cancelled(task_id, target_node_id)
            else:
                status = _failed(task_id, target_node_id, exc)
            metadb.finish_durable_task_attempt(task_id, attempt_id, owner_token, status)
    finally:
        with _active_lock:
            if _active.get(task_id) is threading.current_thread():
                _active.pop(task_id, None)


def dispatch(task_id: str, deps) -> None:
    with _active_lock:
        current = _active.get(str(task_id))
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(target=_worker, args=(str(task_id), deps), daemon=True,
                                  name=f"dp-merge-columns-{str(task_id)[-12:]}")
        _active[str(task_id)] = thread
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_merge_columns_task_ids():
        dispatch(task_id, deps)


def request_cancel(task_id: str) -> None:
    # The shared SQL cancel flag is the authority.  This hook merely wakes a running worker at its
    # next bounded heartbeat; unlike LocalRunner there is no process-local child to interrupt.
    return None
