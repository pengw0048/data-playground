"""Dataset-scoped admission for restoring a retained revision as a new current head."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hub import metadb
from hub.api_errors import APIError, APIErrorCode
from hub.deps import get_deps
from hub.models import (
    ExactDatasetRef, LineagePublication, RestoreRevisionRequestV1, RestoreRevisionTaskV1,
    WriteDestination, WriteIntent, WriteProvenance,
)
from hub.restore_revision_tasks import dispatch
from hub.security import current_user


router = APIRouter()


def _task_view(task_id: str, uid: str) -> RestoreRevisionTaskV1:
    value = metadb.restore_revision_task_view(task_id, uid)
    if value is None:
        raise APIError(404, "restore revision task not found", code=APIErrorCode.NOT_FOUND,
                       retryable=False)
    return RestoreRevisionTaskV1.model_validate(value)


def _build_intent(dataset_id: str, revision_id: str, head: dict,
                  submission_id: str) -> WriteIntent:
    name = head.get("name")
    if not isinstance(name, str) or not name:
        raise APIError(409, "restore destination head has no stable name",
                       code=APIErrorCode.CONFLICT, retryable=False)
    try:
        detail = metadb.managed_local_file_revision_detail(head["artifact_uri"], revision_id)
    except (KeyError, ValueError) as exc:
        raise APIError(410, "restore source revision is unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    key = f"restore-revision:{dataset_id}:{submission_id}"
    return WriteIntent(
        destination=WriteDestination(
            logical_uri=head["logical_uri"], name=name, dataset_id=dataset_id),
        mode="replace", expected_schema=detail["table"].columns,
        expected_head=ExactDatasetRef(
            kind="exact", dataset_id=dataset_id, revision_id=str(head["revision_id"])),
        idempotency_key=key,
        provenance=WriteProvenance(publication=LineagePublication(
            idempotency_key=key, provenance="manual", producer="restore-revision",
            producer_version=1, step_id=f"restore-revision:{revision_id}"), parents=[]),
    )


@router.post("/catalog/revisions/{dataset_id}/{revision_id}/restore",
             response_model=RestoreRevisionTaskV1)
def submit(dataset_id: str, revision_id: str, request: RestoreRevisionRequestV1,
           uid: str = Depends(current_user)) -> RestoreRevisionTaskV1:
    task_id = metadb.restore_revision_submission_id(uid, request.submission_id)
    existing = metadb.durable_task(task_id, include_admission=False)
    if existing is not None:
        view = _task_view(task_id, uid)
        if view.source_dataset_id != dataset_id or view.source_revision_id != revision_id:
            raise APIError(409, "restore submission id is already used for another revision",
                           code=APIErrorCode.CONFLICT, retryable=False)
        dispatch(task_id, get_deps())
        return view
    head = metadb.catalog_managed_local_head_for_dataset(dataset_id)
    if head is None:
        raise APIError(404, "restore requires a core managed-local dataset",
                       code=APIErrorCode.NOT_FOUND, retryable=False)
    if head.get("state") != "active" or head.get("revision_id") is None:
        raise APIError(409, "restore destination head is not active",
                       code=APIErrorCode.CONFLICT, retryable=False)
    if metadb.managed_local_file_revision_artifact(dataset_id, revision_id) is None:
        raise APIError(410, "restore source revision is unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    if request.expected_head_revision_id != str(head["revision_id"]):
        raise APIError(409, "restore destination head moved before submission",
                       code=APIErrorCode.CONFLICT, retryable=False)
    intent = _build_intent(dataset_id, revision_id, head, request.submission_id)
    try:
        task, _created = metadb.submit_restore_revision_task(
            uid=uid, submission_id=request.submission_id, source_dataset_id=dataset_id,
            source_revision_id=revision_id, intent=intent)
    except metadb.DurableTaskSubmissionConflict as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from None
    except ValueError as exc:
        raise APIError(422, "restore submission is invalid", code=APIErrorCode.VALIDATION_ERROR,
                       retryable=False) from exc
    dispatch(task["id"], get_deps())
    return _task_view(task["id"], uid)


@router.get("/restore-revision/{task_id}", response_model=RestoreRevisionTaskV1)
def status(task_id: str, uid: str = Depends(current_user)) -> RestoreRevisionTaskV1:
    return _task_view(task_id, uid)
