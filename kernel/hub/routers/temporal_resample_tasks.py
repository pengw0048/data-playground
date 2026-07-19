from __future__ import annotations

from fastapi import APIRouter, Depends

from hub import metadb
from hub.api_errors import APIError, APIErrorCode
from hub.deps import get_deps
from hub.models import (
    TemporalResampleRetryRequestV1, TemporalResampleTaskResponseV1, TemporalResampleWriteRequestV1,
)
from hub.security import current_user
from hub.temporal_resample_tasks import (
    TemporalResampleAdmissionError, candidate_for_request, dispatch, public_task,
)
from hub.temporal_resample_diagnostics import TemporalResampleDiagnosticCode


router = APIRouter()


def _error(exc: TemporalResampleAdmissionError) -> APIError:
    status, retryable = {
        TemporalResampleDiagnosticCode.PERMISSION_DENIED: (403, False),
        TemporalResampleDiagnosticCode.PROVIDER_OFFLINE: (503, True),
        TemporalResampleDiagnosticCode.REVISION_UNAVAILABLE: (410, False),
    }.get(exc.code, (422, False))
    return APIError(
        status, "temporal resample admission rejected", code=exc.code, retryable=retryable)


def _owned(task_id: str, uid: str) -> dict:
    task = metadb.durable_task(task_id, include_admission=False)
    if task is None or task["owner_id"] != uid or task["task_kind"] != "temporal_resample_write":
        raise APIError(404, "temporal resample task not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    return task


@router.post("/temporal-resample-write", response_model=TemporalResampleTaskResponseV1)
def submit(request: TemporalResampleWriteRequestV1, uid: str = Depends(current_user)) -> TemporalResampleTaskResponseV1:
    try:
        _manifest, candidate = candidate_for_request(uid, request)
        task, _created = metadb.submit_temporal_resample_task(
            uid=uid, request=request, candidate_sha256=candidate.digest)
    except TemporalResampleAdmissionError as exc:
        raise _error(exc) from None
    except metadb.DurableTaskSubmissionConflict as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from None
    dispatch(task["id"], get_deps())
    current = _owned(task["id"], uid)
    return public_task(current)


@router.get("/temporal-resample-write/{task_id}", response_model=TemporalResampleTaskResponseV1)
def status(task_id: str, uid: str = Depends(current_user)) -> TemporalResampleTaskResponseV1:
    return public_task(_owned(task_id, uid))


@router.post("/temporal-resample-write/{task_id}/retry", response_model=TemporalResampleTaskResponseV1)
def retry(task_id: str, request: TemporalResampleRetryRequestV1,
          uid: str = Depends(current_user)) -> TemporalResampleTaskResponseV1:
    _owned(task_id, uid)
    try:
        metadb.retry_durable_task(task_id, request.retry_request_id)
    except ValueError as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from None
    dispatch(task_id, get_deps())
    return public_task(_owned(task_id, uid))


@router.post("/temporal-resample-write/{task_id}/cancel", response_model=TemporalResampleTaskResponseV1)
def cancel(task_id: str, uid: str = Depends(current_user)) -> TemporalResampleTaskResponseV1:
    _owned(task_id, uid)
    task = metadb.request_durable_task_cancel(task_id)
    if task is None:
        raise APIError(404, "temporal resample task not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    return public_task(task)
