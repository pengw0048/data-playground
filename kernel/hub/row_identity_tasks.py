"""Fenced durable worker for exact managed-local row-identity certification."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from hub import db, metadb
from hub.models import ExactDatasetRef
from hub.row_identity import (
    ManagedLocalLanceRowIdentityFenceV1,
    RowIdentityPreviewEvidenceTooLarge,
    RowIdentityRevisionMismatch,
    RowIdentityUnavailable,
    RowIdentityValidationError,
    RowIdentityValueTooLarge,
    certify_and_commit_exact_row_identity,
    certify_and_commit_managed_local_lance_row_identity,
    serialize_row_identity_coverage,
)


_active_lock = threading.Lock()
_active: dict[str, threading.Thread] = {}


@dataclass
class _LeaseState:
    lost: bool = False
    cancel: bool = False
    interrupt: Callable[[], None] | None = None

    def check(self) -> None:
        if self.lost:
            raise RuntimeError("row identity certification lease was lost")
        if self.cancel:
            raise RuntimeError("row identity certification was cancelled")


def _monitor(
        task_id: str, attempt_id: str, owner_token: str,
        state: _LeaseState, done: threading.Event,
) -> None:
    while not done.wait(1.0):
        if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
            state.lost = True
        elif metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
            state.cancel = True
        if (state.lost or state.cancel) and state.interrupt is not None:
            state.interrupt()
            return


def _worker(task_id: str, deps) -> None:
    owner_token = f"row-identity:{uuid.uuid4().hex}:{threading.get_ident()}"
    try:
        claim = metadb.claim_row_identity_certification_task(task_id, owner_token)
        if claim is None:
            return
        attempt_id = str(claim["attempts"][-1]["id"])
        admission = claim["row_identity_certification"]
        exact = ExactDatasetRef(
            kind="exact", dataset_id=admission["dataset_id"],
            revision_id=admission["revision_id"])
        keys = list(admission["keys"])
        if not admission["supported"]:
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token, "unsupported_type")
            return
        state = _LeaseState()
        done = threading.Event()
        monitor = threading.Thread(
            target=_monitor,
            args=(task_id, attempt_id, owner_token, state, done),
            daemon=True, name=f"dp-row-identity-lease-{task_id[-8:]}")
        monitor.start()
        try:
            with db.run_scope() as scope:
                state.interrupt = scope.interrupt
                state.check()

                if admission["source_kind"] == "lance":
                    expected_fence = ManagedLocalLanceRowIdentityFenceV1(
                        dataset_id=exact.dataset_id,
                        revision_id=exact.revision_id,
                        schema_sha256=admission["schema_sha256"],
                        row_identity_spec_sha256=admission["spec_sha256"],
                        physical_incarnation_sha256=admission[
                            "physical_incarnation_sha256"],
                    )

                    def commit_lance(certificate, fence) -> bool:
                        state.check()
                        document = serialize_row_identity_coverage(
                            certificate, exact, admission["spec_sha256"])
                        committed = metadb.finish_managed_local_lance_row_identity_certification_scan(
                            task_id, attempt_id, owner_token, document,
                            physical_incarnation_sha256=fence.physical_incarnation_sha256)
                        if not committed:
                            state.lost = True
                        return committed

                    certify_and_commit_managed_local_lance_row_identity(
                        exact, keys, commit=commit_lance,
                        owner=f"row-identity-task:{task_id}",
                        expected_fence=expected_fence)
                else:
                    def commit_parquet(
                            certificate, artifact_dev: int, artifact_ino: int) -> bool:
                        state.check()
                        document = serialize_row_identity_coverage(
                            certificate, exact, admission["spec_sha256"])
                        committed = metadb.finish_row_identity_certification_scan(
                            task_id, attempt_id, owner_token, document,
                            artifact_dev=artifact_dev, artifact_ino=artifact_ino)
                        if not committed:
                            state.lost = True
                        return committed

                    certify_and_commit_exact_row_identity(
                        deps.storage, exact, keys, commit=commit_parquet,
                        owner=f"row-identity-task:{task_id}",
                        expected_schema_sha256=admission["schema_sha256"],
                        expected_spec_sha256=admission["spec_sha256"])
        except (RowIdentityUnavailable, RowIdentityRevisionMismatch):
            if state.lost:
                return
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token,
                "cancelled" if state.cancel else "stale_or_unavailable_revision")
        except RowIdentityValueTooLarge:
            if state.lost:
                return
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token,
                "cancelled" if state.cancel else "identity_value_over_limit")
        except RowIdentityPreviewEvidenceTooLarge:
            if state.lost:
                return
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token,
                "cancelled" if state.cancel else "preview_identity_evidence_over_budget")
        except RowIdentityValidationError:
            if state.lost:
                return
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token,
                "cancelled" if state.cancel else "failed")
        except ValueError:
            if state.lost:
                return
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token,
                "cancelled" if state.cancel else "failed")
        except BaseException:
            if state.lost:
                return
            if not state.cancel:
                logging.getLogger("hub").exception(
                    "row identity certification task failed")
            metadb.finish_row_identity_certification_failure(
                task_id, attempt_id, owner_token,
                "cancelled" if state.cancel else "failed")
        finally:
            state.interrupt = None
            done.set()
            monitor.join(timeout=2)
    finally:
        with _active_lock:
            if _active.get(task_id) is threading.current_thread():
                _active.pop(task_id, None)


def dispatch(task_id: str, deps) -> None:
    with _active_lock:
        current = _active.get(str(task_id))
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(
            target=_worker, args=(str(task_id), deps), daemon=True,
            name=f"dp-row-identity-{str(task_id)[-12:]}")
        _active[str(task_id)] = thread
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_row_identity_certification_task_ids():
        dispatch(task_id, deps)
