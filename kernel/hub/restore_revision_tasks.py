"""One fenced durable restore that republishes a retained revision's bytes as a new head.

The source is an immutable retained artifact, so every attempt is deterministic: publication copies
those bytes under the frozen typed replace intent. Response-loss and restart reconcile through the
ordinary managed-local write receipt; a moved head fails closed without mutating history.
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid

from hub import metadb
from hub.local_writes import write_managed_local_file
from hub.models import RunOutput, RunStatus, WriteIntent, WriteReceipt


_active_lock = threading.Lock()
_active: dict[str, threading.Thread] = {}


class RestoreSourceUnavailable(RuntimeError):
    """The retained source revision is no longer readable; restore must not mutate the head."""


def _failed(task_id: str, target_node_id: str, exc: BaseException) -> dict:
    if (isinstance(exc, metadb.ManagedLocalWriteConflict)
            and str(exc) == "replace expected head is stale"):
        code = "stale_expected_head"
    elif isinstance(exc, RestoreSourceUnavailable):
        code = "revision_unavailable"
    else:
        code = "restore_write_failed"
    return RunStatus(run_id=task_id, status="failed", target_node_id=target_node_id,
                     error=code).model_dump()


def _cancelled(task_id: str, target_node_id: str) -> dict:
    return RunStatus(run_id=task_id, status="cancelled", target_node_id=target_node_id).model_dump()


def _done(task_id: str, target_node_id: str, intent: WriteIntent, receipt: WriteReceipt) -> RunStatus:
    return RunStatus(
        run_id=task_id, status="done", target_node_id=target_node_id, progress=1.0,
        total_rows=receipt.rows, outputs=[RunOutput(
            node_id=target_node_id, port_id="out", wire="dataset",
            publication_kind="catalog", outcome="committed",
            uri=receipt.publication.artifact_uri, table=intent.destination.name,
            version=receipt.publication.catalog_version, rows=receipt.rows,
            write_receipt=receipt)],
    )


def _receipt(intent: WriteIntent) -> WriteReceipt | None:
    prior = metadb.catalog_managed_local_write_receipt(intent.model_dump(by_alias=True, mode="json"))
    return WriteReceipt.model_validate(prior) if prior is not None else None


def _heartbeat(task_id: str, attempt_id: str, owner_token: str) -> None:
    if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
        raise RuntimeError("restore revision task owner lost the lease")
    if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
        raise RuntimeError("cancelled")


def _publish(deps, *, task_id: str, attempt_id: str, owner_token: str, target_node_id: str,
             intent: WriteIntent, source: dict) -> RunStatus:
    prior = _receipt(intent)
    if prior is not None:
        return _done(task_id, target_node_id, intent, prior)
    source_uri = metadb.managed_local_file_revision_artifact(
        source["dataset_id"], source["revision_id"])
    if source_uri is None:
        raise RestoreSourceUnavailable("restore source revision is unavailable")

    def write_artifact(candidate_uri: str) -> None:
        _heartbeat(task_id, attempt_id, owner_token)
        guard = deps.storage.acquire_result_read(source_uri, owner=f"restore-revision:{task_id}")
        fd = os.dup(guard.artifact_fileno())
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(fd, "rb") as reader, open(candidate_uri, "wb") as target:
                fd = -1
                while chunk := reader.read(1024 * 1024):
                    _heartbeat(task_id, attempt_id, owner_token)
                    target.write(chunk)
            guard.check()
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            guard.close()

    try:
        receipt = write_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=intent,
            write_artifact=write_artifact,
            before_publish=lambda: _heartbeat(task_id, attempt_id, owner_token))
        return _done(task_id, target_node_id, intent, receipt)
    except Exception:
        prior = _receipt(intent)
        if prior is not None:
            return _done(task_id, target_node_id, intent, prior)
        raise


def _worker(task_id: str, deps) -> None:
    owner_token = f"{uuid.uuid4().hex}:{threading.get_ident()}"
    try:
        claimed = metadb.claim_restore_revision_task(task_id, owner_token)
        if claimed is None:
            return
        attempt_id = str(claimed["attempts"][-1]["id"])
        target_node_id = str(claimed["target_node_id"])
        intent = WriteIntent.model_validate(claimed["write_intent"])
        source = claimed["restore_source"]
        try:
            _heartbeat(task_id, attempt_id, owner_token)
            status = _publish(
                deps, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                target_node_id=target_node_id, intent=intent, source=source)
            if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                prior = _receipt(intent)
                status = (_done(task_id, target_node_id, intent, prior) if prior is not None
                          else RunStatus.model_validate(_cancelled(task_id, target_node_id)))
            metadb.finish_durable_task_attempt(task_id, attempt_id, owner_token, status.model_dump())
        except BaseException as exc:
            prior = None
            with contextlib.suppress(Exception):
                prior = _receipt(intent)
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
                                  name=f"dp-restore-revision-{str(task_id)[-12:]}")
        _active[str(task_id)] = thread
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_restore_revision_task_ids():
        dispatch(task_id, deps)
