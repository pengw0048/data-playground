"""Parent durable worker for Source -> Select(checkpoint) -> Select(*) -> Write.

Phases: checkpoint_pending, planning, children, gather, publishing, terminal.
Reuses #414 checkpoint lifecycle, #421 plan/unit/slot evidence, and existing
managed-local WriteIntent/WriteReceipt publication. Certified executor is
identity_projection_v1 only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import uuid

from sqlalchemy import func, select

from hub import bounded_fanout as fanout
from hub import identity_projection as identity
from hub import linear_checkpoint as lc
from hub import metadb
from hub.linear_checkpoint_tasks import (
    _materialize_prefix,
    _wait_for_owned_worker,
    checkpoint_identity,
    graph_prefix_sha256,
)
from hub.models import RunOutput, RunStatus, WriteIntent, WriteReceipt
from hub.plugins.runner import LocalRunner


_active_lock = threading.Lock()
_active: dict[str, tuple[LocalRunner | None, threading.Thread, str | None]] = {}
_MAX_UNIT_ATTEMPTS = 3
_DRAIN_POLL_SECONDS = 0.1
log = logging.getLogger("hub")


def _failed(task_id: str, target: str, exc: BaseException) -> dict:
    return RunStatus(
        run_id=task_id, status="failed", target_node_id=target,
        error=f"{type(exc).__name__}: {exc}",
    ).model_dump()


def _cancelled(task_id: str, target: str) -> dict:
    return RunStatus(
        run_id=task_id, status="cancelled", target_node_id=target,
    ).model_dump()


def _progress(
        task_id: str, target: str, *, phase: str, progress: float,
        outputs: list | None = None, error: str | None = None) -> dict:
    status = RunStatus(
        run_id=task_id, status="running", target_node_id=target,
        progress=progress, error=error, outputs=outputs or [],
    )
    doc = status.model_dump()
    doc["fanout_phase"] = phase
    return doc


def _done_status(task_id: str, target: str, write_intent: WriteIntent, receipt: WriteReceipt) -> RunStatus:
    output = RunOutput(
        node_id=target, port_id="out", wire="dataset",
        publication_kind="catalog", outcome="committed",
        uri=receipt.publication.artifact_uri,
        table=write_intent.destination.name,
        version=receipt.publication.catalog_version,
        rows=receipt.rows,
        write_receipt=receipt,
    )
    return RunStatus(
        run_id=task_id, status="done", target_node_id=target, progress=1.0,
        outputs=[output], total_rows=receipt.rows,
    )


def _unit_attempts_under_parent(unit_id: str, parent_attempt_id: str) -> int:
    with metadb.session() as session:
        return int(session.scalar(select(func.count()).select_from(fanout.BoundedFanoutUnitAttempt).where(
            fanout.BoundedFanoutUnitAttempt.unit_id == str(unit_id),
            fanout.BoundedFanoutUnitAttempt.parent_attempt_id == str(parent_attempt_id),
        )) or 0)


def _plan_has_running_slots(parent_task_id: str) -> bool:
    with metadb.session() as session:
        units = list(session.scalars(select(fanout.BoundedFanoutUnit).where(
            fanout.BoundedFanoutUnit.parent_task_id == str(parent_task_id))))
        for unit in units:
            if not unit.active_attempt_id:
                continue
            attempt = session.get(fanout.BoundedFanoutUnitAttempt, unit.active_attempt_id)
            if attempt is not None and attempt.status == "running":
                return True
    return False


def _pause_and_drain(
        deps, task_id: str, attempt_id: str, owner_token: str) -> None:
    with contextlib.suppress(Exception):
        fanout.pause_plan(
            parent_task_id=task_id, parent_attempt_id=attempt_id, owner_token=owner_token)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if not _plan_has_running_slots(task_id):
            return
        if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
            return
        time.sleep(_DRAIN_POLL_SECONDS)


def _ensure_units_ready_for_retry(
        plan: dict, task_id: str, attempt_id: str, owner_token: str) -> dict:
    """Reset non-done units onto the current parent attempt before reclaiming."""
    for unit in plan["units"]:
        if unit["status"] == "done":
            continue
        plan = fanout.retry_unit(
            parent_task_id=task_id, unit_id=unit["unit_id"],
            parent_attempt_id=attempt_id, owner_token=owner_token)
    return plan


def _run_child_unit(
        deps, *, claimed: dict, attempt_id: str, owner_token: str,
        unit: dict) -> None:
    task_id = claimed["id"]
    prior = _unit_attempts_under_parent(unit["unit_id"], attempt_id)
    if prior >= _MAX_UNIT_ATTEMPTS:
        raise RuntimeError(
            f"bounded fan-out unit exhausted {_MAX_UNIT_ATTEMPTS} internal attempts")
    claim = fanout.claim_unit(
        parent_task_id=task_id, unit_id=unit["unit_id"],
        parent_attempt_id=attempt_id, owner_token=owner_token)
    claim_token = claim["claim_token"]
    unit_attempt_id = claim["attempt_id"]
    try:
        candidate = fanout.reserve_unit_artifact(deps.storage, attempt_id=unit_attempt_id)
        guard, _evidence = lc.reopen_checkpoint(deps.storage, task_id)
        try:
            if not fanout.heartbeat_attempt(
                    attempt_id=unit_attempt_id, claim_token=claim_token, owner_token=owner_token):
                raise RuntimeError("fan-out unit claim lost its lease")
            if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                raise RuntimeError("bounded fan-out parent owner lost the lease")
            if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                raise RuntimeError("cancelled")
            content = identity.project_range_from_guard(
                guard, int(unit["range_start"]), int(unit["range_end"]))
            guard.check()
        finally:
            with contextlib.suppress(Exception):
                guard.close()
        fanout.commit_unit_evidence(
            deps.storage, attempt_id=unit_attempt_id, claim_token=claim_token,
            owner_token=owner_token, candidate=candidate, content=content)
    except Exception as exc:
        if str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(
                task_id, attempt_id, owner_token):
            with contextlib.suppress(Exception):
                fanout.cancel_attempt(
                    attempt_id=unit_attempt_id, claim_token=claim_token,
                    owner_token=owner_token, diagnostic="cancelled")
            raise RuntimeError("cancelled") from exc
        # Contract / evidence failures fail the parent; do not emit Inbox for internals.
        with contextlib.suppress(Exception):
            fanout.fail_attempt(
                attempt_id=unit_attempt_id, claim_token=claim_token,
                owner_token=owner_token, diagnostic=f"{type(exc).__name__}: {exc}")
        raise


def _run_gather(
        deps, *, claimed: dict, attempt_id: str, owner_token: str, plan: dict) -> dict:
    task_id = claimed["id"]
    gather = next(unit for unit in plan["units"] if unit["kind"] == "gather")
    if gather["status"] == "done":
        return plan
    prior = _unit_attempts_under_parent(gather["unit_id"], attempt_id)
    if prior >= _MAX_UNIT_ATTEMPTS:
        raise RuntimeError(
            f"bounded fan-out gather exhausted {_MAX_UNIT_ATTEMPTS} internal attempts")
    claim = fanout.claim_unit(
        parent_task_id=task_id, unit_id=gather["unit_id"],
        parent_attempt_id=attempt_id, owner_token=owner_token)
    claim_token = claim["claim_token"]
    unit_attempt_id = claim["attempt_id"]
    try:
        children = sorted(
            (unit for unit in plan["units"] if unit["kind"] == "child"),
            key=lambda item: int(item["partition_index"]))
        if any(child["status"] != "done" or not child.get("result_uri") for child in children):
            raise RuntimeError("gather requires validated child evidence")
        parts: list[bytes] = []
        for child in children:
            if not fanout.heartbeat_attempt(
                    attempt_id=unit_attempt_id, claim_token=claim_token, owner_token=owner_token):
                raise RuntimeError("fan-out gather claim lost its lease")
            if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                raise RuntimeError("bounded fan-out parent owner lost the lease")
            if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                raise RuntimeError("cancelled")
            guard = deps.storage.acquire_result_read(
                child["result_uri"], owner=f"fanout-gather:{task_id}")
            try:
                fd = os.dup(guard.artifact_fileno())
                try:
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(fd, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    parts.append(b"".join(chunks))
                finally:
                    os.close(fd)
                guard.check()
            finally:
                guard.close()
        content = identity.concat_parquet_in_order(
            parts, expected_schema_sha256=plan["checkpoint_schema_sha256"])
        candidate = fanout.reserve_unit_artifact(deps.storage, attempt_id=unit_attempt_id)
        return fanout.commit_unit_evidence(
            deps.storage, attempt_id=unit_attempt_id, claim_token=claim_token,
            owner_token=owner_token, candidate=candidate, content=content)
    except Exception as exc:
        if str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(
                task_id, attempt_id, owner_token):
            with contextlib.suppress(Exception):
                fanout.cancel_attempt(
                    attempt_id=unit_attempt_id, claim_token=claim_token,
                    owner_token=owner_token, diagnostic="cancelled")
            raise RuntimeError("cancelled") from exc
        with contextlib.suppress(Exception):
            fanout.fail_attempt(
                attempt_id=unit_attempt_id, claim_token=claim_token,
                owner_token=owner_token, diagnostic=f"{type(exc).__name__}: {exc}")
        raise


def _publish_from_gather(
        deps, claimed: dict, attempt_id: str, owner_token: str, plan: dict) -> RunStatus:
    task_id = claimed["id"]
    target = claimed["target_node_id"]
    write_intent = WriteIntent.model_validate(claimed["write_intent"])
    prior = metadb.catalog_managed_local_write_receipt(
        write_intent.model_dump(by_alias=True, mode="json"))
    if prior is not None:
        return _done_status(task_id, target, write_intent, WriteReceipt.model_validate(prior))

    gather = next(unit for unit in plan["units"] if unit["kind"] == "gather")
    if gather["status"] != "done" or not gather.get("result_uri"):
        raise RuntimeError("gather evidence is required before publication")

    metadb.update_durable_task_status(
        task_id, attempt_id, owner_token,
        _progress(task_id, target, phase="publishing", progress=0.85))
    guard = deps.storage.acquire_result_read(
        gather["result_uri"], owner=f"fanout-publish:{task_id}")
    from hub.local_writes import write_managed_local_file

    def write_artifact(candidate_uri: str) -> None:
        if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
            raise RuntimeError("cancelled")
        fd = os.dup(guard.artifact_fileno())
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            src = os.fdopen(fd, "rb")
            fd = -1
            with src, open(candidate_uri, "wb") as dst:
                while True:
                    if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                        raise RuntimeError("bounded fan-out publication owner lost the lease")
                    if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                        raise RuntimeError("cancelled")
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        guard.check()

    try:
        if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
            return RunStatus.model_validate(_cancelled(task_id, target))
        receipt = write_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=write_intent,
            write_artifact=write_artifact, before_publish=guard.check)
        guard.check()
        return _done_status(task_id, target, write_intent, receipt)
    except RuntimeError as exc:
        if str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(
                task_id, attempt_id, owner_token):
            prior = metadb.catalog_managed_local_write_receipt(
                write_intent.model_dump(by_alias=True, mode="json"))
            if prior is not None:
                return _done_status(
                    task_id, target, write_intent, WriteReceipt.model_validate(prior))
            return RunStatus.model_validate(_cancelled(task_id, target))
        raise
    finally:
        try:
            guard.close()
        except Exception:
            log.warning("fan-out gather read guard release failed", exc_info=True)


def _commit_checkpoint_phase(
        deps, claimed: dict, attempt_id: str, owner_token: str) -> lc.CheckpointEvidence:
    task_id = claimed["id"]
    target = claimed["target_node_id"]
    recovery = lc.recover_checkpoint(
        deps.storage, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)
    if recovery["action"] == "committed":
        return recovery["evidence"]
    if recovery["action"] == "reattach":
        return lc.commit_reattached_checkpoint(
            deps.storage, task_id=task_id, attempt_id=attempt_id,
            owner_token=owner_token, candidate=recovery["candidate"])
    if recovery["action"] == "retire":
        pass
    if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
        raise RuntimeError("cancelled")
    metadb.update_durable_task_status(
        task_id, attempt_id, owner_token,
        _progress(task_id, target, phase="checkpoint_pending", progress=0.15))
    candidate = metadb.reserve_linear_checkpoint_candidate(
        task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
        namespace_id=deps.storage.namespace_id,
        storage_root=deps.storage.result_root,
        writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    evidence = _materialize_prefix(deps, claimed, attempt_id, owner_token, candidate)
    if evidence is None:
        evidence = lc.reconcile_checkpoint(task_id)
    if evidence is None:
        raise RuntimeError("checkpoint commit did not produce durable evidence")
    return evidence


def _worker(task_id: str, deps) -> None:
    owner_token = f"{uuid.uuid4().hex}:{threading.get_ident()}"
    runner: LocalRunner | None = None
    try:
        claimed = metadb.claim_bounded_fanout_write_task(task_id, owner_token)
        if claimed is None:
            return
        with metadb.session() as session:
            checkpoint = session.get(metadb.DurableCheckpoint, task_id)
            if checkpoint is None:
                return
            claimed = {
                **claimed,
                "checkpoint_node_id": checkpoint.checkpoint_node_id,
                "output_port_id": checkpoint.output_port_id,
                "checkpoint_id": checkpoint.checkpoint_id,
            }
        attempt = claimed["attempts"][-1]
        attempt_id = str(attempt["id"])
        target = str(claimed["target_node_id"])
        try:
            if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                return
            try:
                _commit_checkpoint_phase(deps, claimed, attempt_id, owner_token)
            except RuntimeError as exc:
                if str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(
                        task_id, attempt_id, owner_token):
                    with contextlib.suppress(OSError, RuntimeError):
                        lc.abort_checkpoint(
                            deps.storage, task_id=task_id, attempt_id=attempt_id,
                            owner_token=owner_token)
                    _pause_and_drain(deps, task_id, attempt_id, owner_token)
                    if _wait_for_owned_worker(runner, task_id, attempt_id, owner_token):
                        metadb.finish_durable_task_attempt(
                            task_id, attempt_id, owner_token, _cancelled(task_id, target))
                    return
                raise

            metadb.update_durable_task_status(
                task_id, attempt_id, owner_token,
                _progress(task_id, target, phase="planning", progress=0.4))
            if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                _pause_and_drain(deps, task_id, attempt_id, owner_token)
                metadb.finish_durable_task_attempt(
                    task_id, attempt_id, owner_token, _cancelled(task_id, target))
                return

            plan = fanout.create_or_reopen_plan(
                parent_task_id=task_id, parent_attempt_id=attempt_id, owner_token=owner_token)
            plan = _ensure_units_ready_for_retry(plan, task_id, attempt_id, owner_token)

            gather = next(unit for unit in plan["units"] if unit["kind"] == "gather")
            if gather["status"] != "done":
                metadb.update_durable_task_status(
                    task_id, attempt_id, owner_token,
                    _progress(task_id, target, phase="children", progress=0.55))
                children = sorted(
                    (unit for unit in plan["units"] if unit["kind"] == "child"),
                    key=lambda item: int(item["partition_index"]))
                for child in children:
                    if child["status"] == "done":
                        continue
                    if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                        _pause_and_drain(deps, task_id, attempt_id, owner_token)
                        metadb.finish_durable_task_attempt(
                            task_id, attempt_id, owner_token, _cancelled(task_id, target))
                        return
                    _run_child_unit(
                        deps, claimed=claimed, attempt_id=attempt_id,
                        owner_token=owner_token, unit=child)
                    plan = fanout.create_or_reopen_plan(
                        parent_task_id=task_id, parent_attempt_id=attempt_id,
                        owner_token=owner_token)

                metadb.update_durable_task_status(
                    task_id, attempt_id, owner_token,
                    _progress(task_id, target, phase="gather", progress=0.75))
                if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                    _pause_and_drain(deps, task_id, attempt_id, owner_token)
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token, _cancelled(task_id, target))
                    return
                plan = _run_gather(
                    deps, claimed=claimed, attempt_id=attempt_id,
                    owner_token=owner_token, plan=plan)

            status = _publish_from_gather(deps, claimed, attempt_id, owner_token, plan)
            if status.status == "cancelled" or metadb.durable_task_attempt_should_stop(
                    task_id, attempt_id, owner_token):
                prior = metadb.catalog_managed_local_write_receipt(claimed["write_intent"])
                if prior is not None:
                    status = _done_status(
                        task_id, target, WriteIntent.model_validate(claimed["write_intent"]),
                        WriteReceipt.model_validate(prior))
                else:
                    _pause_and_drain(deps, task_id, attempt_id, owner_token)
                    status = RunStatus.model_validate(_cancelled(task_id, target))
            metadb.finish_durable_task_attempt(
                task_id, attempt_id, owner_token, status.model_dump())
        except BaseException as exc:
            log.exception("bounded fan-out task failed")
            if runner is None or _wait_for_owned_worker(
                    runner, task_id, attempt_id, owner_token):
                prior = None
                with contextlib.suppress(Exception):
                    prior = metadb.catalog_managed_local_write_receipt(claimed["write_intent"])
                if prior is not None:
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token,
                        _done_status(
                            task_id, target,
                            WriteIntent.model_validate(claimed["write_intent"]),
                            WriteReceipt.model_validate(prior)).model_dump())
                elif metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                    _pause_and_drain(deps, task_id, attempt_id, owner_token)
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token, _cancelled(task_id, target))
                else:
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token, _failed(task_id, target, exc))
    finally:
        with _active_lock:
            current = _active.get(task_id)
            if current is not None and current[1] is threading.current_thread():
                _active.pop(task_id, None)


def dispatch(task_id: str, deps) -> None:
    """Start one fan-out supervisor after the durable admission transaction committed."""
    with _active_lock:
        current = _active.get(str(task_id))
        if current is not None and current[1].is_alive():
            return
        thread = threading.Thread(
            target=_worker, args=(str(task_id), deps), daemon=True,
            name=f"dp-bounded-fanout-{str(task_id)[-12:]}",
        )
        _active[str(task_id)] = (None, thread, None)
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_bounded_fanout_write_task_ids():
        dispatch(task_id, deps)


# Re-export checkpoint identity helpers used by admission. Cancellation is driven entirely by the
# `cancel_requested` DB flag (durable_task_attempt_should_stop); the fan-out worker registers no
# interruptible LocalRunner, so there is no request_cancel here.
__all__ = [
    "dispatch", "recover",
    "checkpoint_identity", "graph_prefix_sha256",
]
