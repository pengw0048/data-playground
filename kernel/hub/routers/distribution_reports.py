"""Owner-scoped API for one bounded built-in DatasetView distribution report."""

from __future__ import annotations

import json
import uuid
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from hub import distribution_reports
from hub.api_errors import APIError, APIErrorCode
from hub.distribution_report_insights import (
    DistributionReportBucketExamplesV1,
    DistributionReportCompareRequestV1,
    DistributionReportComparisonV1,
    InvalidReportBucket,
    bucket_examples,
    compare_reports,
)
from hub.distribution_report_tasks import (
    COMPUTATION_VERSION,
    dispatch,
    estimate_distribution_report,
)
from hub.models import DatasetViewDefinitionV1, to_camel
from hub.plugins.adapters import (
    RevisionPermissionLost,
    RevisionProviderOffline,
    RevisionUnavailable,
)
from hub.routers.dataset_views import _stored_definition
from hub.security import current_user
from hub.storage import ManagedSourceReadError


router = APIRouter()


class DistributionReportEstimateV1(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    schema_version: Literal[1] = 1
    dataset_view_id: str
    view_definition_sha256: str
    estimated_scan_rows: int | None = Field(default=None, ge=0)
    estimated_scan_bytes: int | None = Field(default=None, ge=0)
    selected_column_count: int = Field(ge=1, le=500)
    needs_confirmation: bool
    reason: Literal["unknown_size", "large_scan"] | None = None
    limits: dict[str, int]


class DistributionReportSubmitRequestV1(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid")

    submission_id: uuid.UUID
    confirmed: bool = False


def _report_view(uid: str, view_id: str) -> DatasetViewDefinitionV1:
    view = _stored_definition(uid, view_id)
    if view.retention_owner != "core":
        raise APIError(
            422, "Distribution reports require a core-retained exact DatasetView revision",
            code=APIErrorCode.VALIDATION_ERROR, retryable=False)
    return view


def _raise_corrupt_report(
        exc: distribution_reports.DistributionReportUnavailable) -> NoReturn:
    raise APIError(
        500, "Retained distribution report state is corrupt",
        code=APIErrorCode.INTERNAL_ERROR, retryable=False) from exc


@router.post(
    "/dataset-views/{view_id}/distribution-reports/estimate",
    response_model=DistributionReportEstimateV1,
)
def estimate_report(
    view_id: str,
    uid: str = Depends(current_user),
) -> DistributionReportEstimateV1:
    """Return retained metadata signals without scanning source rows."""
    return DistributionReportEstimateV1.model_validate(
        estimate_distribution_report(_report_view(uid, view_id)))


@router.post(
    "/dataset-views/{view_id}/distribution-reports",
    response_model=distribution_reports.DistributionReportEnvelopeViewV1,
    status_code=201,
    responses={
        200: {
            "model": distribution_reports.DistributionReportEnvelopeViewV1,
            "description": "Identical submission replayed from its durable report identity.",
        },
    },
)
def submit_report(
    view_id: str,
    request: DistributionReportSubmitRequestV1,
    response: Response,
    uid: str = Depends(current_user),
) -> distribution_reports.DistributionReportEnvelopeViewV1:
    view = _report_view(uid, view_id)
    submission_id = str(request.submission_id)
    task_id = distribution_reports._task_id(uid, submission_id)
    try:
        prior = distribution_reports.distribution_report(owner_id=uid, task_id=task_id)
    except distribution_reports.DistributionReportUnavailable as exc:
        _raise_corrupt_report(exc)
    if prior is not None:
        if prior["intent"]["datasetViewId"] != view.id:
            raise APIError(
                409, "Distribution report submission id belongs to another DatasetView",
                code=APIErrorCode.CONFLICT, retryable=False)
        response.status_code = 200
        return distribution_reports.public_distribution_report(prior)
    estimate = estimate_distribution_report(view)
    if estimate["needs_confirmation"] and not request.confirmed:
        raise APIError(
            409, "Distribution report requires confirmation for a large or unknown full scan",
            code=APIErrorCode.CONFLICT, retryable=False)
    intent = distribution_reports.DistributionReportIntentV1(
        submission_id=submission_id,
        dataset_view_id=view.id,
        view_definition_sha256=view.definition_sha256,
        computation_version=COMPUTATION_VERSION,
        max_attempts=3,
    )
    try:
        envelope, created = distribution_reports.admit_distribution_report(
            owner_id=uid, intent=intent)
    except distribution_reports.DistributionReportSubmissionConflict as exc:
        raise APIError(
            409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from exc
    except distribution_reports.DistributionReportUnavailable as exc:
        raise APIError(
            410, "Distribution report exact revision is unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    response.status_code = 201 if created else 200
    dispatch(envelope["task"]["id"])
    return distribution_reports.public_distribution_report(envelope)


@router.get(
    "/distribution-reports/{report_id}",
    response_model=distribution_reports.DistributionReportEnvelopeViewV1,
)
def get_report(
    report_id: str,
    uid: str = Depends(current_user),
) -> distribution_reports.DistributionReportEnvelopeViewV1:
    try:
        envelope = distribution_reports.distribution_report_by_id(
            owner_id=uid, report_id=report_id)
    except distribution_reports.DistributionReportUnavailable as exc:
        _raise_corrupt_report(exc)
    if envelope is None:
        raise HTTPException(404, "Distribution report not found")
    return distribution_reports.public_distribution_report(envelope)


def _completed_document(uid: str, report_id: str) -> tuple[
    distribution_reports.DistributionReportDocumentV1,
    DatasetViewDefinitionV1,
]:
    envelope = distribution_reports.distribution_report_by_id(
        owner_id=uid, report_id=report_id)
    if envelope is None:
        raise HTTPException(404, "Distribution report not found")
    if envelope["report"] is None:
        raise APIError(
            409, "Distribution report is not complete",
            code=APIErrorCode.CONFLICT, retryable=True)
    return (
        distribution_reports.DistributionReportDocumentV1.model_validate_json(
            json.dumps(envelope["report"])),
        DatasetViewDefinitionV1.model_validate(envelope["view_snapshot"]),
    )


@router.post(
    "/distribution-reports/compare",
    response_model=DistributionReportComparisonV1,
)
def compare_distribution_reports(
    request: DistributionReportCompareRequestV1,
    uid: str = Depends(current_user),
) -> DistributionReportComparisonV1:
    """Compare two retained documents without rescanning either measured population."""
    left, _left_view = _completed_document(uid, request.left_report_id)
    right, _right_view = _completed_document(uid, request.right_report_id)
    return compare_reports(left, right)


@router.get(
    "/distribution-reports/{report_id}/sections/{section_id}/buckets/{bucket_id}/examples",
    response_model=DistributionReportBucketExamplesV1,
)
def get_distribution_report_bucket_examples(
    report_id: str,
    section_id: str,
    bucket_id: str,
    uid: str = Depends(current_user),
) -> DistributionReportBucketExamplesV1:
    """Replay one frozen view and filter by one server-issued retained bucket."""
    if any(not value or len(value) > 64 for value in (section_id, bucket_id)):
        raise APIError(
            422, "Unsupported distribution report bucket id",
            code=APIErrorCode.VALIDATION_ERROR, retryable=False)
    document, view = _completed_document(uid, report_id)
    try:
        return bucket_examples(document, view, section_id, bucket_id)
    except InvalidReportBucket as exc:
        raise APIError(
            422, str(exc), code=APIErrorCode.VALIDATION_ERROR, retryable=False) from exc
    except (RevisionUnavailable, ManagedSourceReadError, KeyError) as exc:
        raise APIError(
            410, "Distribution report source revision is unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
    except RevisionPermissionLost as exc:
        raise APIError(
            403, "Distribution report source permission was lost",
            code=APIErrorCode.PERMISSION_DENIED, retryable=False) from exc
    except (RevisionProviderOffline, ConnectionError, TimeoutError) as exc:
        raise APIError(
            503, "Distribution report source provider is offline",
            code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True) from exc


@router.get(
    "/dataset-views/{view_id}/distribution-reports",
    response_model=list[distribution_reports.DistributionReportEnvelopeViewV1],
)
def list_reports(
    view_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    uid: str = Depends(current_user),
) -> list[distribution_reports.DistributionReportEnvelopeViewV1]:
    _stored_definition(uid, view_id)
    try:
        items = [distribution_reports.public_distribution_report(item) for item in
                 distribution_reports.list_distribution_reports(
                     owner_id=uid, dataset_view_id=view_id, limit=limit)]
    except distribution_reports.DistributionReportUnavailable as exc:
        _raise_corrupt_report(exc)
    return items
