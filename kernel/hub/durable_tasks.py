"""Bounded durable ownership for managed-local create/replace Write tasks.

This is deliberately not a scheduler. It owns exactly one frozen saved-canvas Write consumer and
delegates execution/cancellation to an isolated LocalRunner while SQL owns identity, leases, attempts,
and terminal truth.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from hub import compiler, metadb
from hub.models import Graph, RunStatus
from hub.plugins.runner import LocalRunner


_active_lock = threading.Lock()
_active: dict[str, tuple[LocalRunner, threading.Thread]] = {}


def _failed(task_id: str, target: str, exc: BaseException) -> dict:
    return RunStatus(
        run_id=task_id, status="failed", target_node_id=target,
        error=f"{type(exc).__name__}: {exc}",
    ).model_dump()


def _worker(task_id: str, deps) -> None:
    owner_token = f"{uuid.uuid4().hex}:{threading.get_ident()}"
    runner: LocalRunner | None = None
    try:
        claimed = metadb.claim_durable_task(task_id, owner_token)
        if claimed is None:
            return
        attempt = claimed["attempts"][-1]
        attempt_id = str(attempt["id"])
        target = str(claimed["target_node_id"])
        try:
            graph = Graph.model_validate(claimed["graph_doc"])
            from hub.local_run_inputs import bind_manifest
            graph = bind_manifest(
                graph, target, claimed["input_manifest"], deps.resolve_adapter)
            plan = compiler.compile_plan(
                graph, target, deps.registry, deps.node_specs, deps.node_ir)
            if not plan.acyclic:
                raise RuntimeError(plan.error or "durable task graph has a cycle")
            runner = LocalRunner(
                deps.resolve_adapter, deps.registry, deps.catalog, deps.workspace,
                node_builders=deps.node_builders, node_specs=deps.node_specs,
                storage=deps.storage,
            )
            with _active_lock:
                active = _active.get(task_id)
                if active is not None and active[1] is threading.current_thread():
                    _active[task_id] = (runner, active[1])

            def persist(_graph, status: RunStatus) -> None:
                # Task/Attempt are the durable terminal truth. LocalRunner publishes a terminal
                # callback before its worker has fully unwound, so only persist live progress here;
                # the supervisor records terminal state after wait_for_worker below.
                if status.status not in ("done", "failed", "cancelled"):
                    metadb.update_durable_task_status(
                        task_id, attempt_id, owner_token, status.model_dump())

            runner.on_status = persist
            runner.on_complete = None
            status = runner.run(
                plan, graph, target, "local", run_id=task_id,
                cancel_check=lambda: metadb.durable_task_attempt_should_stop(
                    task_id, attempt_id, owner_token),
            )
            next_heartbeat = 0.0
            while status.status not in ("done", "failed", "cancelled"):
                now = time.monotonic()
                if now >= next_heartbeat:
                    next_heartbeat = now + 1.0
                    if not metadb.heartbeat_durable_task(
                            task_id, attempt_id, owner_token):
                        runner.cancel(task_id)
                    elif metadb.durable_task_attempt_should_stop(
                            task_id, attempt_id, owner_token):
                        runner.cancel(task_id)
                time.sleep(0.1)
                status = runner.status(task_id)
            runner.wait_for_worker(task_id, timeout=5)
            metadb.finish_durable_task_attempt(
                task_id, attempt_id, owner_token, status.model_dump())
        except BaseException as exc:
            logging.getLogger("hub").exception("durable local write task failed")
            metadb.finish_durable_task_attempt(
                task_id, attempt_id, owner_token, _failed(task_id, target, exc))
    finally:
        with _active_lock:
            current = _active.get(task_id)
            if current is not None and current[1] is threading.current_thread():
                _active.pop(task_id, None)


def dispatch(task_id: str, deps) -> None:
    """Start one local supervisor only after the durable transaction committed."""
    with _active_lock:
        current = _active.get(str(task_id))
        if current is not None and current[1].is_alive():
            return
        thread = threading.Thread(
            target=_worker, args=(str(task_id), deps), daemon=True,
            name=f"dp-durable-task-{str(task_id)[-12:]}",
        )
        # Store a harmless placeholder runner until the worker has claimed and constructed its owner.
        _active[str(task_id)] = (None, thread)  # type: ignore[arg-type]
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_durable_task_ids():
        dispatch(task_id, deps)


def request_cancel(task_id: str) -> dict | None:
    task = metadb.request_durable_task_cancel(task_id)
    if task is None:
        return None
    with _active_lock:
        active = _active.get(str(task_id))
    if active is not None and active[0] is not None:
        try:
            active[0].cancel(str(task_id))
        except KeyError:
            pass
    return task


def retry(task_id: str, retry_request_id: str, deps) -> dict:
    task = metadb.retry_durable_task(task_id, retry_request_id)
    dispatch(task_id, deps)
    return task
