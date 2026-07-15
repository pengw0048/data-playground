"""Process-isolated jobs for whole-dataset column profiles.

Sample profiles stay on the interactive in-process path. A whole-dataset profile can execute Python,
plugins, adapters, and native code, so its canvas kernel owns a one-shot OS child and does not publish a
terminal status until that child has exited and been reaped. The process supervisor, workload environment,
and managed-source lease protocol are shared with normal isolated runs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable

from hub.models import Graph, PerNodeStatus, RunStatus
from hub.subprocess_runner import SubprocessRunner, _SpawnSetupError


def _posix_group_has_live_members(pgid: int) -> bool | None:
    """Inspect a POSIX group, excluding zombies; ``None`` means the platform probe failed."""
    if os.name != "posix" or not hasattr(os, "posix_spawn"):
        return None
    read_fd, write_fd = os.pipe()
    try:
        actions = [
            (os.POSIX_SPAWN_DUP2, write_fd, 1),
            (os.POSIX_SPAWN_CLOSE, read_fd),
            (os.POSIX_SPAWN_CLOSE, write_fd),
        ]
        probe_pid = os.posix_spawn(
            "/bin/ps", ["ps", "-axo", "pgid=,stat="], os.environ,
            file_actions=actions,
        )
    except Exception:  # noqa: BLE001 - cleanup falls back to signal-delivery proof
        os.close(read_fd)
        os.close(write_fd)
        return None
    os.close(write_fd)
    try:
        chunks = []
        while True:
            chunk = os.read(read_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        _, wait_status = os.waitpid(probe_pid, 0)
        if wait_status != 0:
            return None
        for line in b"".join(chunks).decode(errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit() and int(fields[0]) == pgid:
                if not fields[1].upper().startswith(("Z", "X")):
                    return True
        return False
    except Exception:  # noqa: BLE001
        return None
    finally:
        os.close(read_fd)


class ProfileProcessRunner(SubprocessRunner):
    """Supervise each full profile in a killable one-shot ``hub.subrun`` child."""

    name = "local-profile"

    def __init__(self, workspace: str, data_dir: str, *, storage=None,
                 deadline_s: float | None = None):
        super().__init__(workspace, data_dir, storage=storage, deadline_s=deadline_s)
        self._profile_identities: dict[str, dict[str, object]] = {}
        self._terminal_persistence_pending: dict[str, RunStatus] = {}
        self._deferred_completions: dict[
            str, tuple[Graph, str | None, RunStatus]
        ] = {}
        self._terminal_publication_done: set[str] = set()
        self._terminal_publication_rejected: set[str] = set()
        self._completion_done: set[str] = set()
        self._profile_process_groups: dict[str, subprocess.Popen] = {}

    def run(self, graph: Graph, node_id: str, *, plan_digest: str,
            profile_attempt_order: int, run_id: str | None = None,
            request_id: str | None = None) -> RunStatus:
        if os.name != "posix":
            raise RuntimeError(
                "full profiles require POSIX process-group containment on this backend")
        if type(profile_attempt_order) is not int or profile_attempt_order <= 0:
            raise ValueError("profile attempt order must be a positive integer")
        run_id = run_id or f"profile_{uuid.uuid4().hex[:10]}"
        identity = {
            "target_node_id": node_id,
            "plan_digest": plan_digest,
            "profile_attempt_order": profile_attempt_order,
            "request_id": request_id,
        }
        status = RunStatus(
            run_id=run_id,
            status="queued",
            job_type="profile",
            target_node_id=node_id,
            placement="local",
            per_node=[PerNodeStatus(node_id=node_id, status="queued", label="Full profile")],
            plan_digest=plan_digest,
            profile_attempt_order=profile_attempt_order,
            request_id=request_id,
        )
        with self._lock:
            existing = self.runs.get(run_id)
            if existing is not None:
                existing_identity = self._profile_identities.get(run_id)
                if existing_identity == identity:
                    return existing
                raise ValueError(
                    f"profile run id is already bound to a different identity: {run_id}")
            reserved_identity = self._profile_identities.get(run_id)
            if reserved_identity is not None or run_id in self._procs:
                # A concurrent dispatch has reserved the identity but has not yet installed a status.
                # It is not safe to launch another child or pretend that the first dispatch succeeded.
                if reserved_identity == identity:
                    raise ValueError(f"profile run id dispatch is still in progress: {run_id}")
                raise ValueError(
                    f"profile run id is already bound to a different identity: {run_id}")
            self._profile_identities[run_id] = identity

        try:
            source_leases = self._claim_source_leases(graph, node_id, run_id)
            with self._lock:
                self._source_leases[run_id] = source_leases
            return self._spawn(status, {
                "jobKind": "profile",
                "runId": run_id,
                "managedSourceAttempts": source_leases["attempts"],
                "managedLocalSources": source_leases["local_sources"],
            }, graph, node_id)
        except Exception as exc:
            # Once Popen succeeds without reap proof, the base supervisor retains the exact child, job
            # directory, and source ownership for retry/cancel/operator reconciliation.
            if isinstance(exc, _SpawnSetupError) and not exc.reaped:
                raise
            self._release_source_leases(run_id)
            with self._lock:
                self._profile_identities.pop(run_id, None)
            raise

    def retain_terminal_failure(
            self, graph: Graph, status: RunStatus,
            persist: Callable[[Graph, RunStatus], None]) -> RunStatus:
        """Own a proven no-child failure and retry its exact durable publication.

        A metadata error after admission has an unknown commit outcome. Keeping the terminal status in
        the supervisor makes cancel/status deterministic, while an unbounded daemon retry advances both
        RunState and ProfileJobLatest without ever launching another child. Kernel death remains covered
        by the dead-kernel reaper because admission already bound the durable owner row.
        """
        if (status.status != "failed" or status.job_type != "profile"
                or not status.target_node_id or not status.plan_digest
                or status.profile_attempt_order is None):
            raise ValueError("retained profile failure has an invalid durable identity")
        identity = {
            "target_node_id": status.target_node_id,
            "plan_digest": status.plan_digest,
            "profile_attempt_order": status.profile_attempt_order,
            "request_id": status.request_id,
        }
        retained = status.model_copy(deep=True)
        with self._lock:
            existing = self.runs.get(status.run_id)
            if existing is not None:
                if self._profile_identities.get(status.run_id) != identity:
                    raise ValueError("profile run id is already bound to a different identity")
                return existing
            if status.run_id in self._procs:
                raise RuntimeError("cannot retain a no-child failure for a live profile process")
            self._profile_identities[status.run_id] = identity
            self.runs[status.run_id] = retained
        self._complete(graph, status.target_node_id, retained)
        self._queue_terminal_persistence(graph, retained, persist)
        with self._lock:
            self._evict()
        return retained

    def _persist_terminal(
            self, graph: Graph, status: RunStatus,
            callback: Callable[[Graph, RunStatus], None]) -> None:
        """Publish one exact terminal and completion without blocking unrelated runs."""
        from hub.metadb import RunStatePublicationRejected

        attempt = 0
        delay = 0.05
        while True:
            try:
                callback(graph, status)
            except RunStatePublicationRejected:
                logging.getLogger("hub").exception(
                    "profile terminal publication was definitively rejected")
                with self._lock:
                    self._terminal_publication_rejected.add(status.run_id)
                    if self._terminal_persistence_pending.get(status.run_id) is status:
                        self._terminal_persistence_pending.pop(status.run_id, None)
                    self._deferred_completions.pop(status.run_id, None)
                    self._evict()
                return
            except Exception as exc:  # noqa: BLE001 - DB commit outcome remains unknown
                attempt += 1
                if attempt == 1 or attempt & (attempt - 1) == 0:
                    logging.getLogger("hub").warning(
                        "profile terminal publication remains uncertain (attempt %d): %s",
                        attempt, exc,
                    )
                self.publication_retry_wait(delay)
                delay = min(1.0, delay * 2)
                continue
            break

        with self._lock:
            self._terminal_publication_done.add(status.run_id)
            completion = self._deferred_completions.get(status.run_id)
            completion_done = status.run_id in self._completion_done
        if completion is not None and not completion_done and self.on_complete is not None:
            completion_graph, target, completion_status = completion
            completion_attempt = 0
            delay = 0.05
            while True:
                try:
                    # Call the existing history/telemetry hook directly. Unlike the base helper this
                    # must not swallow a DB failure: RunRecord is idempotent by (canvas, run), while
                    # telemetry fan-out is no-throw, so retry cannot duplicate a completed emission.
                    self.on_complete(completion_graph, target, completion_status)
                except Exception as exc:  # noqa: BLE001 - history commit outcome may be unknown
                    completion_attempt += 1
                    if completion_attempt == 1 or completion_attempt & (completion_attempt - 1) == 0:
                        logging.getLogger("hub").warning(
                            "profile completion publication remains uncertain (attempt %d): %s",
                            completion_attempt, exc,
                        )
                    self.publication_retry_wait(delay)
                    delay = min(1.0, delay * 2)
                    continue
                break
        with self._lock:
            self._completion_done.add(status.run_id)
            if self._terminal_persistence_pending.get(status.run_id) is status:
                self._terminal_persistence_pending.pop(status.run_id, None)
            self._deferred_completions.pop(status.run_id, None)
            self._evict()

    def _queue_terminal_persistence(
            self, graph: Graph, status: RunStatus,
            callback: Callable[[Graph, RunStatus], None]) -> None:
        terminal = status.model_copy(deep=True)
        with self._lock:
            if (status.run_id in self._terminal_publication_done
                    or status.run_id in self._terminal_publication_rejected):
                return
            existing = self._terminal_persistence_pending.get(status.run_id)
            if existing is not None:
                if existing.model_dump() != terminal.model_dump():
                    logging.getLogger("hub").error(
                        "ignored conflicting terminal profile publication for %s", status.run_id)
                return
            self._terminal_persistence_pending[status.run_id] = terminal
        threading.Thread(
            target=self._persist_terminal,
            args=(graph, terminal, callback),
            daemon=True,
            name=f"profile-terminal-{status.run_id}",
        ).start()

    def _complete(self, graph: Graph, target: str | None, status: RunStatus) -> None:
        """Defer history/telemetry until the exact terminal RunState is durable."""
        if status.status not in ("done", "failed", "cancelled"):
            super()._complete(graph, target, status)
            return
        terminal = status.model_copy(deep=True)
        with self._lock:
            if status.run_id in self._completion_done:
                return
            existing = self._deferred_completions.get(status.run_id)
            if existing is not None:
                _existing_graph, existing_target, existing_status = existing
                if (existing_target != target
                        or existing_status.model_dump() != terminal.model_dump()):
                    logging.getLogger("hub").error(
                        "ignored conflicting terminal profile completion for %s", status.run_id)
                return
            self._deferred_completions[status.run_id] = (graph, target, terminal)

    def _emit(self, graph: Graph, status: RunStatus, *, strict: bool = False) -> None:
        """Queue every reaped profile terminal for reliable, non-blocking DB publication."""
        if status.status not in ("done", "failed", "cancelled"):
            super()._emit(graph, status, strict=strict)
            return
        callback = self.on_status
        if callback is not None:
            self._queue_terminal_persistence(graph, status, callback)

    def _heartbeat_interval_s(self) -> float:
        """Refresh durable liveness for an owned, healthy child without inventing percentage progress."""
        try:
            configured = float(os.environ.get("DP_PROFILE_HEARTBEAT_S", "30"))
        except ValueError:
            configured = 30.0
        return max(0.1, min(60.0, configured))

    def _spawn_process(
            self, run_id: str, command: list[str], **kwargs) -> subprocess.Popen:
        if os.name != "posix":
            return super()._spawn_process(run_id, command, **kwargs)
        kwargs["start_new_session"] = True
        proc = super()._spawn_process(run_id, command, **kwargs)
        with self._lock:
            self._profile_process_groups[run_id] = proc
        return proc

    def _owns_process_group(self, run_id: str, proc: subprocess.Popen) -> bool:
        with self._lock:
            return self._profile_process_groups.get(run_id) is proc

    def _signal_process(
            self, run_id: str, proc: subprocess.Popen, *, force: bool) -> None:
        if os.name != "posix":
            super()._signal_process(run_id, proc, force=force)
            return
        # The PGID equals the session-leading child's PID. Never signal after this exact Popen's scope
        # ownership has been cleared: a delayed killpg could otherwise hit a reused numeric PGID.
        if not self._owns_process_group(run_id, proc):
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _finalize_process_scope(self, run_id: str, proc: subprocess.Popen) -> None:
        if os.name != "posix":
            super()._finalize_process_scope(run_id, proc)
            return
        if not self._owns_process_group(run_id, proc):
            return
        pgid = proc.pid
        self._signal_process(run_id, proc, force=False)
        deadline = time.monotonic() + 0.5
        unknown = False
        live: bool | None = True
        while time.monotonic() < deadline:
            live = _posix_group_has_live_members(pgid)
            if live is False:
                break
            unknown = unknown or live is None
            time.sleep(0.02)
        else:
            live = True
        if live is not False:
            self._signal_process(run_id, proc, force=True)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                live = _posix_group_has_live_members(pgid)
                if live is False:
                    break
                if live is None:
                    unknown = True
                    # SIGKILL is synchronous process-group stop authority; an unavailable ``ps`` probe
                    # cannot distinguish reparented zombies, which execute no further side effects.
                    time.sleep(0.1)
                    live = False
                    break
                time.sleep(0.02)
            if live is not False:
                raise RuntimeError("profile process group still has live members after SIGKILL")
        if unknown:
            logging.getLogger("hub").debug(
                "profile process-group liveness probe was unavailable after signal delivery")
        with self._lock:
            if self._profile_process_groups.get(run_id) is proc:
                self._profile_process_groups.pop(run_id, None)

    def cancel_acknowledged(self, run_id: str) -> bool:
        with self._lock:
            status = self.runs.get(run_id)
            return bool(
                status is not None and status.status == "cancelled"
                and run_id not in self._procs
                and run_id not in self._profile_process_groups
            )

    def _profile_identity(self, run_id: str, status: RunStatus) -> RunStatus:
        identity = self._profile_identities.get(run_id)
        if identity is None:
            status.status = "failed"
            status.error = "profile supervisor lost the parent job identity"
            status.profile = None
            status.output_uri = status.output_table = None
            return status
        status.run_id = run_id
        status.job_type = "profile"
        status.target_node_id = str(identity["target_node_id"])
        status.plan_digest = str(identity["plan_digest"])
        status.profile_attempt_order = int(identity["profile_attempt_order"])
        request_id = identity["request_id"]
        status.request_id = str(request_id) if request_id is not None else None
        status.placement = "local"
        status.output_uri = status.output_table = None
        return status

    def _sanitize_child_status(self, run_id: str, observed: RunStatus) -> RunStatus:
        """Replace every control-plane identity supplied by the untrusted child."""
        observed = self._profile_identity(run_id, observed)
        node_id = observed.target_node_id or ""
        if observed.status == "done":
            profile = observed.profile
            if (profile is None or profile.sampled or profile.error or profile.not_previewable
                    or profile.row_count < 0):
                observed.status = "failed"
                observed.error = "profile child returned an invalid full-profile result"
                observed.profile = None
            else:
                observed.error = None
                observed.progress = 1.0
                observed.rows_processed = profile.row_count
                observed.total_rows = profile.row_count
        else:
            # Partial/failed child documents never get to smuggle a result into durable state.
            observed.profile = None
            observed.rows_processed = 0
            observed.total_rows = None
            observed.progress = None
            if observed.status in ("queued", "running", "cancelled"):
                observed.error = None
        observed.per_node = [PerNodeStatus(
            node_id=node_id,
            status=observed.status,
            label="Full profile",
            rows=observed.total_rows,
            ms=observed.ms,
            error=observed.error if observed.status == "failed" else None,
        )]
        return observed

    def _finalize_reaped_status(self, run_id: str, status: RunStatus, *,
                                deadline_hit: bool, returncode: int | None) -> RunStatus:
        """Make cancellation/deadline authoritative only after ``wait``/``kill`` reaped the child."""
        status = self._profile_identity(run_id, status)
        if run_id in self._cancelled:
            status.status = "cancelled"
            status.error = None
            status.profile = None
        elif deadline_hit:
            status.status = "failed"
            status.error = (
                f"full profile exceeded the wall-clock deadline of {self.deadline_s:.0f}s — killed")
            status.profile = None
        elif status.status == "cancelled":
            # Cancellation is control-plane intent, never a status the workload may mint for itself.
            status.status = "failed"
            status.error = "profile process reported cancellation without a parent request"
            status.profile = None
        elif status.status == "done" and returncode != 0:
            status.status = "failed"
            status.error = status.error or f"profile process exited (code {returncode})"
            status.profile = None
        return self._sanitize_child_status(run_id, status)

    def _evict(self) -> None:
        # Commit-unknown terminal documents retain their exact in-memory identity until the reliable
        # publisher reaches success or definitive rejection. The normal bound resumes immediately then.
        protected = {
            run_id: self.runs.pop(run_id)
            for run_id in tuple(self._terminal_persistence_pending)
            if run_id in self.runs
        }
        try:
            super()._evict()
        finally:
            self.runs.update(protected)
        for run_id in tuple(self._profile_identities):
            if run_id not in self.runs:
                self._profile_identities.pop(run_id, None)
                self._terminal_publication_done.discard(run_id)
                self._terminal_publication_rejected.discard(run_id)
                self._completion_done.discard(run_id)
