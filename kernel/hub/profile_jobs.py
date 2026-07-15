"""Cancellable durable jobs for whole-dataset column profiles.

Small sample profiles stay on the interactive preview path. Whole-dataset profiles can scan an entire
graph, so this runner deliberately uses the same queued/running/terminal status contract as normal
runs. Coverage is complete, while distinct counts remain approximate by design.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from hub.executors.profile import profile_node
from hub.models import Graph, PerNodeStatus, RunStatus


_MAX_PROFILE_JOBS = 100


class ProfileJobRunner:
    """A small job owner for full profiles; status persistence is injected by :mod:`hub.deps`."""

    name = "local-profile"
    cancel_acknowledges_stop = True

    def __init__(self, resolve_adapter, registry, *, node_builders=None, node_specs=None, storage=None):
        self.resolve_adapter = resolve_adapter
        self.registry = registry
        self.node_builders = node_builders if node_builders is not None else {}
        self.node_specs = node_specs if node_specs is not None else {}
        self.storage = storage
        self.on_status = None
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._scopes: dict[str, object] = {}
        self._lock = threading.Lock()

    def run(self, graph: Graph, node_id: str, *, plan_digest: str, run_id: str | None = None,
            request_id: str | None = None) -> RunStatus:
        run_id = run_id or f"profile_{uuid.uuid4().hex[:10]}"
        status = RunStatus(
            run_id=run_id,
            status="queued",
            job_type="profile",
            target_node_id=node_id,
            placement="local",
            per_node=[PerNodeStatus(node_id=node_id, status="queued", label="Full profile")],
            plan_digest=plan_digest,
            request_id=request_id,
        )
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
            self._evict()
        self._emit(graph, status)
        threading.Thread(target=self._execute, args=(run_id, graph, node_id), daemon=True,
                         name=f"profile-{run_id}").start()
        return status

    def status(self, run_id: str) -> RunStatus:
        with self._lock:
            return self.runs[run_id]

    def cancel(self, run_id: str) -> RunStatus:
        with self._lock:
            status = self.runs[run_id]
            if status.status in ("done", "failed", "cancelled"):
                return status
            self._cancel[run_id].set()
            scope = self._scopes.get(run_id)
        # DuckDB work is scoped to the worker thread. Interrupt that exact scope rather than the
        # process-global cursor, matching normal run cancellation and avoiding cross-job collateral.
        if scope is not None:
            try:
                scope.interrupt()
            except Exception:  # noqa: BLE001 - the worker will still observe its cancellation token
                logging.getLogger("hub").debug("profile cancellation interrupt failed", exc_info=True)
        return status

    def _execute(self, run_id: str, graph: Graph, node_id: str) -> None:
        started = time.monotonic()
        with self._lock:
            status = self.runs.get(run_id)
            cancelled = bool(self._cancel.get(run_id) and self._cancel[run_id].is_set())
        if status is None:
            return
        if cancelled:
            self._terminal(graph, run_id, "cancelled", started)
            return

        status.status = "running"
        status.per_node[0].status = "running"
        self._emit(graph, status)

        def remember_scope(scope) -> None:
            with self._lock:
                self._scopes[run_id] = scope
                cancelled_now = bool(self._cancel.get(run_id) and self._cancel[run_id].is_set())
            if cancelled_now:
                scope.interrupt()

        try:
            result = profile_node(
                graph, node_id, self.resolve_adapter, self.registry,
                self.node_builders, self.node_specs, full=True, storage=self.storage,
                scope_callback=remember_scope,
            )
        except Exception as exc:  # noqa: BLE001 - job workers must always reach a visible terminal state
            with self._lock:
                cancelled = bool(self._cancel.get(run_id) and self._cancel[run_id].is_set())
            if cancelled:
                # DuckDB reports its cursor interrupt as an exception. The accepted cancel intent is
                # the authoritative terminal cause; do not present a user-requested stop as a failure.
                self._terminal(graph, run_id, "cancelled", started)
            else:
                self._terminal(graph, run_id, "failed", started,
                               error=f"{type(exc).__name__}: {exc}")
            return
        with self._lock:
            cancelled = bool(self._cancel.get(run_id) and self._cancel[run_id].is_set())
        if cancelled:
            self._terminal(graph, run_id, "cancelled", started)
        elif result.error or result.not_previewable:
            self._terminal(graph, run_id, "failed", started, error=result.reason or "profile failed")
        else:
            self._terminal(graph, run_id, "done", started, profile=result)

    def _terminal(self, graph: Graph, run_id: str, state: str, started: float, *, error: str | None = None,
                  profile=None) -> None:
        with self._lock:
            status = self.runs.get(run_id)
            if status is None:
                return
            status.status = state
            status.ms = max(0, int((time.monotonic() - started) * 1000))
            status.progress = 1.0 if state == "done" else None
            status.error = error
            status.profile = profile
            status.rows_processed = profile.row_count if profile is not None else 0
            status.total_rows = profile.row_count if profile is not None else None
            status.per_node[0].status = state
            status.per_node[0].rows = status.total_rows
            status.per_node[0].ms = status.ms
            if error:
                status.per_node[0].error = error
            self._scopes.pop(run_id, None)
        self._emit(graph, status)

    def _emit(self, graph: Graph, status: RunStatus) -> None:
        callback = self.on_status
        if callback is None:
            return
        try:
            callback(graph, status)
        except Exception:  # noqa: BLE001 - a persistence outage must not strand this worker thread
            logging.getLogger("hub").exception("profile job status persistence failed")

    def _evict(self) -> None:
        terminal = [rid for rid, st in self.runs.items() if st.status in ("done", "failed", "cancelled")]
        while len(self.runs) > _MAX_PROFILE_JOBS and terminal:
            victim = terminal.pop(0)
            self.runs.pop(victim, None)
            self._cancel.pop(victim, None)
            self._scopes.pop(victim, None)
