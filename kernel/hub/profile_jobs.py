"""Process-isolated jobs for whole-dataset column profiles.

Sample profiles stay on the interactive in-process path. A whole-dataset profile can execute Python,
plugins, adapters, and native code, so its canvas kernel owns a one-shot OS child and does not publish a
terminal status until that child has exited and been reaped. The process supervisor, workload environment,
and managed-source lease protocol are shared with normal isolated runs.
"""

from __future__ import annotations

import uuid

from hub.models import Graph, PerNodeStatus, RunStatus
from hub.subprocess_runner import SubprocessRunner, _SpawnSetupError


class ProfileProcessRunner(SubprocessRunner):
    """Supervise each full profile in a killable one-shot ``hub.subrun`` child."""

    name = "local-profile"

    def __init__(self, workspace: str, data_dir: str, *, storage=None,
                 deadline_s: float | None = None):
        super().__init__(workspace, data_dir, storage=storage, deadline_s=deadline_s)
        self._profile_identities: dict[str, dict[str, object]] = {}

    def run(self, graph: Graph, node_id: str, *, plan_digest: str,
            profile_attempt_order: int, run_id: str | None = None,
            request_id: str | None = None) -> RunStatus:
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
        super()._evict()
        for run_id in tuple(self._profile_identities):
            if run_id not in self.runs:
                self._profile_identities.pop(run_id, None)
