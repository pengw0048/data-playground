"""Metadata-only admission and owner-scoped durable exact row-identity certification."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Annotated, Literal

import pyarrow as pa
from fastapi import APIRouter, Depends, Response
from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from hub import metadb
from hub.api_errors import APIError, APIErrorCode
from hub.deps import get_deps
from hub.models import (
    DatasetRevisionRowIdentity, ExactDatasetRef, PlanDigest,
    ROW_IDENTITY_FIELD_NAME_MAX, Wire,
)
from hub.row_identity import (
    RowIdentityUnavailable, RowIdentityValidationError,
    freeze_row_identity_spec_from_parquet_fileno,
    row_identity_spec_is_supported,
)
from hub.row_identity_tasks import dispatch
from hub.security import current_user
from hub.storage import ManagedSourceUnavailable, source_read_scope


router = APIRouter()
CONFIRM_ROWS = 1_000_000
CONFIRM_BYTES = 128 * 1024 * 1024
RowIdentityCertificationKeyName = Annotated[
    str, Field(min_length=1, max_length=ROW_IDENTITY_FIELD_NAME_MAX)]


class RowIdentityCertificationRequestV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    key_columns: list[RowIdentityCertificationKeyName] = Field(min_length=1, max_length=16)


class RowIdentityCertificationSubmitV1(RowIdentityCertificationRequestV1):
    submission_id: uuid.UUID
    confirmation_sha256: PlanDigest | None = None


class RowIdentityCertificationFieldV1(Wire):
    name: str = Field(min_length=1, max_length=ROW_IDENTITY_FIELD_NAME_MAX)
    arrow_type: str = Field(min_length=1, max_length=128)


class RowIdentityCertificationPreflightV1(Wire):
    schema_version: Literal[1] = 1
    dataset_ref: ExactDatasetRef
    key_fields: list[RowIdentityCertificationFieldV1]
    schema_sha256: PlanDigest
    spec_sha256: PlanDigest
    estimated_scan_rows: int | None = Field(default=None, ge=0)
    estimated_scan_bytes: int | None = Field(default=None, ge=0)
    needs_confirmation: bool
    reason: Literal["unknown_size", "large_scan"] | None = None
    supported: bool
    confirmation_sha256: PlanDigest


RowIdentityCertificationOutcome = Literal[
    "certified",
    "already_certified_same_spec",
    "conflicting_retained_spec",
    "duplicate_key",
    "null_key",
    "unsupported_type",
    "stale_or_unavailable_revision",
    "cancelled",
    "failed",
]


class RowIdentityCertificationReceiptV1(Wire):
    schema_version: Literal[1] = 1
    task_id: str
    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    schema_sha256: PlanDigest
    spec_sha256: PlanDigest
    key_columns: list[RowIdentityCertificationKeyName] = Field(min_length=1, max_length=16)
    outcome: RowIdentityCertificationOutcome
    certificate: DatasetRevisionRowIdentity | None = None


class RowIdentityCertificationTaskV1(Wire):
    task_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    dataset_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=256)
    schema_sha256: PlanDigest
    spec_sha256: PlanDigest
    key_columns: list[RowIdentityCertificationKeyName] = Field(min_length=1, max_length=16)
    can_cancel: bool = False
    receipt: RowIdentityCertificationReceiptV1 | None = None


def _preflight(request: RowIdentityCertificationRequestV1, storage) -> tuple[
        RowIdentityCertificationPreflightV1, dict]:
    exact = ExactDatasetRef(
        kind="exact", dataset_id=request.dataset_id, revision_id=request.revision_id)
    try:
        facts = metadb.catalog_managed_local_revision_certification_facts(exact)
        with source_read_scope(
                storage, [facts["artifact_uri"]],
                owner=f"row-identity-preflight:{uuid.uuid4().hex}") as guards:
            if len(guards) != 1 or not hasattr(guards[0], "artifact_fileno"):
                raise ManagedSourceUnavailable(
                    "row identity preflight source is unavailable")
            artifact_info = os.fstat(guards[0].artifact_fileno())
            spec = freeze_row_identity_spec_from_parquet_fileno(
                exact, request.key_columns, guards[0].artifact_fileno())
            rows = facts["row_count"]
            total_bytes = int(artifact_info.st_size)
    except KeyError as exc:
        raise APIError(
            410, "Exact managed-local revision is unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    except (ManagedSourceUnavailable, RowIdentityUnavailable,
            OSError, pa.ArrowException) as exc:
        raise APIError(
            410, "Exact managed-local revision is unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    except (RuntimeError, RowIdentityValidationError) as exc:
        raise APIError(
            422, "Row identity certification metadata is not admissible",
            code=APIErrorCode.VALIDATION_ERROR, retryable=False) from exc
    unknown = rows is None or total_bytes is None
    large = ((rows is not None and rows > CONFIRM_ROWS)
             or (total_bytes is not None and total_bytes > CONFIRM_BYTES))
    reason = "unknown_size" if unknown else "large_scan" if large else None
    supported = row_identity_spec_is_supported(spec)
    evidence = {
        "schemaVersion": 1,
        "datasetRef": exact.model_dump(by_alias=True, mode="json"),
        "keyFields": [
            {"name": field.name, "arrowType": field.arrow_type}
            for field in spec.fields
        ],
        "schemaSha256": spec.schema_digest,
        "specSha256": spec.digest,
        "estimatedScanRows": rows,
        "estimatedScanBytes": total_bytes,
        "needsConfirmation": unknown or large,
        "reason": reason,
        "supported": supported,
    }
    confirmation = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()).hexdigest()
    return RowIdentityCertificationPreflightV1.model_validate({
        **evidence, "confirmationSha256": confirmation,
    }), facts


def _task_view(task_id: str, uid: str) -> RowIdentityCertificationTaskV1:
    value = metadb.row_identity_certification_task_view(task_id, uid)
    if value is None:
        raise APIError(
            404, "Row identity certification task not found",
            code=APIErrorCode.NOT_FOUND, retryable=False)
    return RowIdentityCertificationTaskV1.model_validate(value)


@router.post(
    "/catalog/row-identity-certifications/preflight",
    response_model=RowIdentityCertificationPreflightV1,
)
def preflight(
    request: RowIdentityCertificationRequestV1,
    uid: str = Depends(current_user),
) -> RowIdentityCertificationPreflightV1:
    del uid
    return _preflight(request, get_deps().storage)[0]


@router.post(
    "/catalog/row-identity-certifications",
    response_model=RowIdentityCertificationTaskV1,
    status_code=201,
    responses={
        200: {
            "model": RowIdentityCertificationTaskV1,
            "description": "Identical durable submission replayed.",
        },
    },
)
def submit(
    request: RowIdentityCertificationSubmitV1,
    response: Response,
    uid: str = Depends(current_user),
) -> RowIdentityCertificationTaskV1:
    submission_id = str(request.submission_id)
    task_id = metadb.row_identity_certification_submission_id(uid, submission_id)
    prior = metadb.row_identity_certification_task_view(task_id, uid)
    if prior is not None:
        if (prior["datasetId"] != request.dataset_id
                or prior["revisionId"] != request.revision_id
                or prior["keyColumns"] != request.key_columns):
            raise APIError(
                409, "Certification submission id belongs to another exact intent",
                code=APIErrorCode.CONFLICT, retryable=False)
        response.status_code = 200
        dispatch(task_id, get_deps())
        return RowIdentityCertificationTaskV1.model_validate(prior)
    deps = get_deps()
    estimate, facts = _preflight(request, deps.storage)
    if estimate.needs_confirmation:
        if request.confirmation_sha256 != estimate.confirmation_sha256:
            raise APIError(
                409, "Certification confirmation does not match current preflight evidence",
                code=APIErrorCode.CONFLICT, retryable=False)
    elif (request.confirmation_sha256 is not None
          and request.confirmation_sha256 != estimate.confirmation_sha256):
        raise APIError(
            409, "Certification confirmation does not match current preflight evidence",
            code=APIErrorCode.CONFLICT, retryable=False)
    try:
        task, created = metadb.submit_row_identity_certification_task(
            uid=uid, submission_id=submission_id,
            dataset_id=request.dataset_id, revision_id=request.revision_id,
            dataset_name=facts["dataset_name"], keys=list(request.key_columns),
            schema_sha256=estimate.schema_sha256, spec_sha256=estimate.spec_sha256,
            supported=estimate.supported,
            confirmation_sha256=estimate.confirmation_sha256,
            estimated_rows=estimate.estimated_scan_rows,
            estimated_bytes=estimate.estimated_scan_bytes,
            artifact_uri=facts["artifact_uri"])
    except metadb.DurableTaskSubmissionConflict as exc:
        raise APIError(
            409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    except ValueError as exc:
        raise APIError(
            410, "Exact managed-local revision became unavailable during admission",
            code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    response.status_code = 201 if created else 200
    dispatch(task["id"], deps)
    return _task_view(task["id"], uid)


@router.get(
    "/row-identity-certifications/{task_id}",
    response_model=RowIdentityCertificationTaskV1,
)
def status(
    task_id: str,
    uid: str = Depends(current_user),
) -> RowIdentityCertificationTaskV1:
    return _task_view(task_id, uid)


@router.post(
    "/row-identity-certifications/{task_id}/cancel",
    response_model=RowIdentityCertificationTaskV1,
)
def cancel(
    task_id: str,
    uid: str = Depends(current_user),
) -> RowIdentityCertificationTaskV1:
    if metadb.cancel_row_identity_certification_task(task_id, uid) is None:
        raise APIError(
            404, "Row identity certification task not found",
            code=APIErrorCode.NOT_FOUND, retryable=False)
    return _task_view(task_id, uid)
