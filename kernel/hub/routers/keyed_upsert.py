"""Dataset-scoped admission for one certified keyed upsert into a managed-local dataset (#637).

Composes the frozen keyed-upsert service with the ordinary durable-task lifecycle. It changes no
UpsertIntent semantics and owns no publication path: preflight is side-effect-free, submission is
idempotent on an owner+submission id, and a moved head fails closed permanently.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from hub import keyed_upsert, metadb
from hub.api_errors import APIError, APIErrorCode
from hub.deps import get_deps
from hub.keyed_upsert import KeyedUpsertError, UpsertEvidenceV1, build_upsert_intent
from hub.keyed_upsert_tasks import dispatch
from hub.models import ColumnSchema, ExactDatasetRef, Wire, WriteReceipt
from hub.row_identity import RowIdentityError
from hub.security import current_user


router = APIRouter()


class UpsertRequestV1(Wire):
    """One intent to upsert a payload revision into a managed-local dataset's current head."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    submission_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    expected_head_revision_id: str = Field(min_length=1, max_length=256)
    payload_dataset_id: str = Field(min_length=1, max_length=128)
    payload_revision_id: str = Field(min_length=1, max_length=256)
    keys: list[str] = Field(min_length=1, max_length=16)


class UpsertRetryRequestV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    retry_request_id: str = Field(min_length=1, max_length=128)


class UpsertPreflightV1(Wire):
    """Side-effect-free projection of one prospective upsert."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    head: ExactDatasetRef
    expected_head: ExactDatasetRef
    keys: list[str]
    output_schema: list[ColumnSchema]
    evidence: UpsertEvidenceV1
    eligible: bool = True


class UpsertTaskV1(Wire):
    """Owner-scoped status of one durable keyed-upsert Task."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    task_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    dataset_id: str = Field(min_length=1, max_length=128)
    expected_head_revision_id: str = Field(min_length=1, max_length=256)
    payload_dataset_id: str = Field(min_length=1, max_length=128)
    payload_revision_id: str = Field(min_length=1, max_length=256)
    child_revision_id: str | None = Field(default=None, min_length=1, max_length=256)
    diagnostic_code: str | None = Field(default=None, max_length=64)
    can_cancel: bool = False
    can_retry: bool = False
    receipt: WriteReceipt | None = None
    evidence: UpsertEvidenceV1 | None = None


def _map_upsert_error(exc: Exception) -> APIError:
    message = str(exc)
    if "stale" in message:
        return APIError(409, "upsert expected head moved before admission",
                        code=APIErrorCode.CONFLICT, retryable=False)
    if "unavailable" in message:
        return APIError(410, "upsert base or payload revision is unavailable",
                        code=APIErrorCode.RESOURCE_GONE, retryable=False)
    return APIError(422, "upsert intent is not admissible",
                    code=APIErrorCode.VALIDATION_ERROR, retryable=False)


def _resolve(request: UpsertRequestV1):
    head = metadb.catalog_managed_local_head_for_dataset(request.dataset_id)
    if head is None:
        raise APIError(404, "upsert requires a core managed-local dataset",
                       code=APIErrorCode.NOT_FOUND, retryable=False)
    if head.get("state") != "active" or head.get("revision_id") is None:
        raise APIError(409, "upsert destination head is not active",
                       code=APIErrorCode.CONFLICT, retryable=False)
    if request.expected_head_revision_id != str(head["revision_id"]):
        raise APIError(409, "upsert destination head moved before submission",
                       code=APIErrorCode.CONFLICT, retryable=False)
    name = head.get("name")
    if not isinstance(name, str) or not name:
        raise APIError(409, "upsert destination head has no stable name",
                       code=APIErrorCode.CONFLICT, retryable=False)
    if metadb.managed_local_file_revision_artifact(
            request.payload_dataset_id, request.payload_revision_id) is None:
        raise APIError(410, "upsert payload revision is unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    try:
        detail = metadb.managed_local_file_revision_detail(
            head["artifact_uri"], str(head["revision_id"]))
    except (KeyError, ValueError) as exc:
        raise APIError(410, "upsert base revision is unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    from hub.models import WriteDestination

    base = ExactDatasetRef(kind="exact", dataset_id=request.dataset_id,
                           revision_id=str(head["revision_id"]))
    payload = ExactDatasetRef(kind="exact", dataset_id=request.payload_dataset_id,
                              revision_id=request.payload_revision_id)
    try:
        intent = build_upsert_intent(
            base=base, head=payload, keys=list(request.keys),
            destination=WriteDestination(
                logical_uri=head["logical_uri"], name=name, dataset_id=request.dataset_id),
            output_schema=detail["table"].columns)
    except (ValueError, KeyedUpsertError) as exc:
        raise APIError(422, "upsert intent is invalid", code=APIErrorCode.VALIDATION_ERROR,
                       retryable=False) from exc
    return intent, base, payload


def _task_view(task_id: str, uid: str) -> UpsertTaskV1:
    value = metadb.keyed_upsert_task_view(task_id, uid)
    if value is None:
        raise APIError(404, "keyed upsert task not found", code=APIErrorCode.NOT_FOUND,
                       retryable=False)
    return UpsertTaskV1.model_validate(value)


@router.post("/catalog/upsert/preflight", response_model=UpsertPreflightV1)
def preflight(request: UpsertRequestV1, uid: str = Depends(current_user)) -> UpsertPreflightV1:
    deps = get_deps()
    intent, base, payload = _resolve(request)
    try:
        evidence = keyed_upsert.preflight_upsert(storage=deps.storage, intent=intent)
    except RowIdentityError as exc:
        raise _map_upsert_error(exc) from exc
    except KeyedUpsertError as exc:
        raise _map_upsert_error(exc) from exc
    return UpsertPreflightV1(
        base=base, head=payload, expected_head=base, keys=list(request.keys),
        output_schema=intent.output_schema, evidence=evidence, eligible=True)


@router.post("/catalog/upsert", response_model=UpsertTaskV1)
def submit(request: UpsertRequestV1, uid: str = Depends(current_user)) -> UpsertTaskV1:
    task_id = metadb.keyed_upsert_submission_id(uid, request.submission_id)
    existing = metadb.durable_task(task_id, include_admission=False)
    if existing is not None:
        view = _task_view(task_id, uid)
        if (view.dataset_id != request.dataset_id
                or view.payload_dataset_id != request.payload_dataset_id
                or view.payload_revision_id != request.payload_revision_id):
            raise APIError(409, "upsert submission id is already used for another intent",
                           code=APIErrorCode.CONFLICT, retryable=False)
        dispatch(task_id, get_deps())
        return view
    deps = get_deps()
    intent, _base, _payload = _resolve(request)
    try:
        evidence = keyed_upsert.preflight_upsert(storage=deps.storage, intent=intent)
    except (RowIdentityError, KeyedUpsertError) as exc:
        raise _map_upsert_error(exc) from exc
    try:
        task, _created = metadb.submit_keyed_upsert_task(
            uid=uid, submission_id=request.submission_id,
            intent=intent.model_dump(by_alias=True, mode="json"),
            evidence=evidence.model_dump(by_alias=True, mode="json"))
    except metadb.DurableTaskSubmissionConflict as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from None
    except ValueError as exc:
        raise APIError(422, "upsert submission is invalid", code=APIErrorCode.VALIDATION_ERROR,
                       retryable=False) from exc
    dispatch(task["id"], deps)
    return _task_view(task["id"], uid)


@router.get("/keyed-upsert/{task_id}", response_model=UpsertTaskV1)
def status(task_id: str, uid: str = Depends(current_user)) -> UpsertTaskV1:
    return _task_view(task_id, uid)


@router.post("/keyed-upsert/{task_id}/cancel", response_model=UpsertTaskV1)
def cancel(task_id: str, uid: str = Depends(current_user)) -> UpsertTaskV1:
    if metadb.cancel_keyed_upsert_task(task_id, uid) is None:
        raise APIError(404, "keyed upsert task not found", code=APIErrorCode.NOT_FOUND,
                       retryable=False)
    return _task_view(task_id, uid)


@router.post("/keyed-upsert/{task_id}/retry", response_model=UpsertTaskV1)
def retry(task_id: str, request: UpsertRetryRequestV1,
          uid: str = Depends(current_user)) -> UpsertTaskV1:
    try:
        retried = metadb.retry_keyed_upsert_task(task_id, uid, request.retry_request_id)
    except ValueError as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    if retried is None:
        raise APIError(404, "keyed upsert task not found", code=APIErrorCode.NOT_FOUND,
                       retryable=False)
    dispatch(task_id, get_deps())
    return _task_view(task_id, uid)
