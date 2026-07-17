from __future__ import annotations

import queue
import threading
import uuid

from pydantic import ValidationError

from hub import metadb
from hub.external_wait import (
    ExternalWaitCheckpoint, ExternalWaitHandle, ExternalWaitPollOutcome,
    ExternalWaitSubmitRequest,
)
_CALL_TIMEOUT_SECONDS = 1.0
_MAX_PROVIDER_CALLS = 8
_calls: queue.Queue[tuple[dict, str, object]] = queue.Queue(maxsize=_MAX_PROVIDER_CALLS)
_capacity = threading.BoundedSemaphore(_MAX_PROVIDER_CALLS)
_state_lock = threading.Lock()
_inflight: dict[str, int] = {}
_workers_started = False

def _commit_failure(claim: dict, token: str, code: str, delay: float = .25) -> None:
    metadb.commit_external_wait_transition(
        claim["task_id"], claim["attempt_id"], token,
        failure_code=code, retry_after=delay)

def _provider_call(claim: dict, deps) -> tuple[str, dict | None]:
    adapter = deps._external_wait_adapter(claim["provider_kind"])
    if adapter is None:
        return "adapter_unavailable", None
    request = ExternalWaitSubmitRequest.model_validate(claim["submit_request"])
    if claim["handle"] is None:
        handle = ExternalWaitHandle.model_validate(adapter.submit(request))
        if handle.provider_kind != request.provider_kind:
            raise ValueError("provider kind mismatch")
        return "handle", handle.model_dump(mode="json")
    handle = ExternalWaitHandle.model_validate(claim["handle"])
    checkpoint = (ExternalWaitCheckpoint.model_validate(claim["checkpoint"])
                  if claim["checkpoint"] is not None else None)
    raw = (adapter.cancel(handle, checkpoint) if claim["cancel_requested"]
           else adapter.status(handle, checkpoint))
    return "outcome", ExternalWaitPollOutcome.model_validate(raw).model_dump(mode="json")

def _perform(claim: dict, token: str, deps) -> None:
    timer = threading.Timer(
        _CALL_TIMEOUT_SECONDS, _commit_failure,
        args=(claim, token, "adapter_transition_timeout"))
    timer.daemon = True
    try:
        timer.start()
        try:
            kind, value = _provider_call(claim, deps)
        except (ValidationError, ValueError, TypeError, KeyError):
            kind, value = "adapter_return_invalid", None
        except BaseException:
            kind, value = "adapter_transient_failure", None
        timer.cancel()
        if kind == "handle":
            metadb.commit_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token, handle=value)
        elif kind == "outcome":
            metadb.commit_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token, outcome=value)
        else:
            _commit_failure(claim, token, kind, 1.0 if kind == "adapter_unavailable" else .25)
    finally:
        with _state_lock:
            count = _inflight.get(claim["task_id"], 1) - 1
            if count:
                _inflight[claim["task_id"]] = count
            else:
                _inflight.pop(claim["task_id"], None)
        _capacity.release()

def _worker() -> None:
    while True:
        item = _calls.get()
        try:
            _perform(*item)
        except BaseException:
            pass
        finally:
            _calls.task_done()

def _start_workers() -> None:
    global _workers_started
    with _state_lock:
        if _workers_started:
            return
        _workers_started = True
        for index in range(_MAX_PROVIDER_CALLS):
            threading.Thread(
                target=_worker, daemon=True, name=f"dp-external-wait-{index + 1}").start()

def recover(deps) -> None:
    _start_workers()
    metadb.fail_corrupt_external_wait_tasks()
    metadb.expire_external_wait_deadlines()
    for task_id in metadb.due_external_wait_task_ids():
        if not _capacity.acquire(blocking=False):
            return
        with _state_lock:
            if _inflight.get(task_id, 0) >= 2:
                _capacity.release()
                continue
            _inflight[task_id] = _inflight.get(task_id, 0) + 1
        token = uuid.uuid4().hex
        claim = metadb.claim_external_wait_transition(task_id, token)
        if claim is None:
            with _state_lock:
                _inflight[task_id] -= 1
                if not _inflight[task_id]:
                    _inflight.pop(task_id)
            _capacity.release()
            continue
        _calls.put_nowait((claim, token, deps))
