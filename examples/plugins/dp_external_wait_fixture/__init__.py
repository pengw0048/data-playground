"""Deterministic offline fixture for ``hub.external_wait`` conformance."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from hub.external_wait import (ExternalWaitCheckpoint, ExternalWaitDiagnostic,
                               ExternalWaitDownloadEvidence, ExternalWaitHandle,
                               ExternalWaitPollOutcome, ExternalWaitRetryHint,
                               ExternalWaitSubmitRequest)
from hub.sdk import NodeSpec, ParamSpec

PROVIDER_KIND = "fixture-local"
NODE_KIND = "external_wait_fixture"
_SENTINEL = "external-wait-secret-sentinel /private/configured/path"


@dataclass
class _Job:
    handle: ExternalWaitHandle
    scenario: str
    phase_index: int = 0
    transient_failed: bool = False
    cancelled: ExternalWaitPollOutcome | None = None


class FixtureExternalWaitAdapter:
    provider_kind = PROVIDER_KIND

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._by_key: dict[str, _Job] = {}
        self._by_id: dict[str, _Job] = {}

    def submit(self, request: ExternalWaitSubmitRequest):
        scenario = request.operation.removeprefix("conformance.")
        existing = self._by_key.get(request.idempotency_key)
        if existing is not None:
            return existing.handle
        digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:16]
        handle = ExternalWaitHandle(
            provider_kind=self.provider_kind,
            job_id=f"fixture-{self._namespace}-{scenario}-{digest}")
        job = _Job(handle=handle, scenario=scenario)
        self._by_key[request.idempotency_key] = job
        self._by_id[handle.job_id] = job
        if scenario == "oversized-handle":
            return {"provider_kind": self.provider_kind, "job_id": "x" * 257}
        if scenario == "response-loss":
            raise RuntimeError(_SENTINEL)
        return handle

    def _job(self, handle: ExternalWaitHandle) -> _Job:
        job = self._by_id.get(handle.job_id)
        prefix = f"fixture-{self._namespace}-"
        if job is None and handle.job_id.startswith(prefix):
            scenario = handle.job_id[len(prefix):].rsplit("-", 1)[0]
            if scenario in {"success", "response-loss", "failed", "cancelled", "transient-status",
                            "regressed-phase", "invalid-download-digest", "invalid-download-size",
                            "invalid-download-path", "malformed-outcome", "non-finite-retry"}:
                job = _Job(handle=handle, scenario=scenario)
                self._by_id[handle.job_id] = job
        if job is None or handle.provider_kind != self.provider_kind:
            raise LookupError(_SENTINEL)
        return job

    @staticmethod
    def _outcome(phase: str, sequence: int):
        checkpoint = ExternalWaitCheckpoint(sequence=sequence, token=f"fixture-checkpoint-{sequence}")
        if phase in {"accepted", "running"}:
            return ExternalWaitPollOutcome(
                phase=phase, checkpoint=checkpoint,
                retry=ExternalWaitRetryHint(after_seconds=0.01),
            )
        if phase == "failed":
            return ExternalWaitPollOutcome(
                phase="failed", checkpoint=checkpoint,
                diagnostic=ExternalWaitDiagnostic(
                    code="fixture_failed", message="The deterministic fixture failed.",
                ),
            )
        return ExternalWaitPollOutcome(phase=phase, checkpoint=checkpoint)

    def status(self, handle: ExternalWaitHandle, checkpoint: ExternalWaitCheckpoint | None = None):
        job = self._job(handle)
        if job.cancelled is not None:
            return job.cancelled
        if job.scenario == "malformed-outcome":
            return {"phase": "unknown", "raw": _SENTINEL}
        if job.scenario == "non-finite-retry":
            return {"phase": "running", "checkpoint": None, "retry": {"after_seconds": float("inf")}}
        phases = {
            "response-loss": ("accepted", "running", "succeeded"),
            "failed": ("accepted", "failed"),
            "cancelled": ("accepted",),
            "transient-status": ("accepted", "running", "succeeded"),
            "regressed-phase": ("accepted", "running", "accepted"),
            "invalid-download-digest": ("accepted", "running", "succeeded"),
            "invalid-download-size": ("accepted", "running", "succeeded"),
            "invalid-download-path": ("accepted", "running", "succeeded"),
        }.get(job.scenario, ("accepted", "running", "succeeded"))
        index = checkpoint.sequence + 1 if checkpoint is not None else job.phase_index
        if job.scenario == "transient-status" and index == 1 and not job.transient_failed:
            job.transient_failed = True
            raise ConnectionError(_SENTINEL)
        index = min(index, len(phases) - 1)
        outcome = self._outcome(phases[index], index)
        job.phase_index = min(index + 1, len(phases) - 1)
        return outcome

    def cancel(self, handle: ExternalWaitHandle, checkpoint: ExternalWaitCheckpoint | None = None):
        del checkpoint
        job = self._job(handle)
        if job.cancelled is None:
            job.cancelled = self._outcome("cancelled", job.phase_index + 1)
        return job.cancelled

    def download(self, handle: ExternalWaitHandle, target: Path):
        job = self._job(handle)
        if job.scenario == "invalid-download-digest":
            target.write_bytes(b"fixture-result")
            return {"result_id": "fixture-result", "bytes_written": 14,
                    "sha256": "not-a-digest", "media_type": "application/octet-stream"}
        content = f"result:{job.handle.job_id}".encode()
        if job.scenario == "invalid-download-path":
            target.parent.rmdir()
            target.parent.symlink_to(target.parent.parent, target_is_directory=True)
        if not target.exists():
            target.write_bytes(content)
        return ExternalWaitDownloadEvidence(
            result_id=f"result-{job.handle.job_id}",
            bytes_written=len(content) + (1 if job.scenario == "invalid-download-size" else 0),
            sha256=hashlib.sha256(content).hexdigest(), media_type="application/octet-stream",
        )


def register(reg) -> None:
    reg.add_external_wait_adapter(FixtureExternalWaitAdapter(reg.workspace_identity()))
    reg.add_external_wait_node(NodeSpec(
        kind=NODE_KIND, title="external wait fixture", category="control", tag="wait",
        inputs=[], outputs=[], previewable=False,
        params=[ParamSpec(
            name="operation", type="select", default="conformance.success",
            options=["conformance.success", "conformance.response-loss", "conformance.failed",
                     "conformance.transient-status", "conformance.cancelled"]),
                ParamSpec(name="documentJson", type="text", default="{}")],
        blurb="deterministic offline external-wait fixture",
    ), PROVIDER_KIND)
