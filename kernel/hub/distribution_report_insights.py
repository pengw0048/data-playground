"""Ephemeral typed comparison for retained distribution reports."""

from __future__ import annotations

import datetime
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from hub.distribution_reports import (
    DistributionCategoricalSectionV1,
    DistributionNumericSectionV1,
    DistributionReportDocumentV1,
    DistributionReportSectionV1,
    DistributionTemporalSectionV1,
)
from hub.models import ColumnSchema, SampleProvenance, to_camel


class _Wire(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True)


class DistributionReportCompareRequestV1(_Wire):
    schema_version: Literal[1] = 1
    left_report_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    right_report_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


class DistributionReportIdentityV1(_Wire):
    report_id: str
    dataset_view_id: str
    dataset_id: str
    revision_id: str
    view_definition_sha256: str
    computation_version: str
    measured_rows: int = Field(ge=0)
    complete: bool
    sampling_identity: str
    sample_provenance: SampleProvenance | None = None


class DistributionCoverageComparisonV1(_Wire):
    left: DistributionReportIdentityV1
    right: DistributionReportIdentityV1
    comparable: bool
    reason: Literal[
        "compatible_full_coverage",
        "same_deterministic_sample",
        "full_sample_coverage_mismatch",
        "different_deterministic_samples",
    ]


class DistributionHistogramDeltaV1(_Wire):
    left_bucket_id: str
    right_bucket_id: str
    lower: float
    upper: float
    upper_inclusive: bool
    count_delta: int


class DistributionQuantileDeltaV1(_Wire):
    probability: float
    value_delta: float | None = None


class DistributionNumericDeltaV1(_Wire):
    kind: Literal["numeric"] = "numeric"
    count_delta: int
    non_finite_count_delta: int
    min_delta: float | None = None
    max_delta: float | None = None
    mean_delta: float | None = None
    stddev_delta: float | None = None
    quantiles: list[DistributionQuantileDeltaV1] = Field(max_length=5)
    histogram: list[DistributionHistogramDeltaV1] | None = Field(default=None, max_length=20)
    histogram_reason: Literal["equal_edges", "unequal_edges"]


class DistributionCategoryDeltaV1(_Wire):
    label: str | bool
    left_count: int | None = Field(default=None, ge=1)
    right_count: int | None = Field(default=None, ge=1)
    count_delta: int | None = None
    reason: Literal["present_in_both_top_k", "outside_left_top_k", "outside_right_top_k"]


class DistributionCategoricalDeltaV1(_Wire):
    kind: Literal["categorical"] = "categorical"
    categories: list[DistributionCategoryDeltaV1] = Field(max_length=40)
    other_count_delta: int | None = None
    other_count_reason: Literal["same_top_k", "different_top_k"]
    distinct_count_delta: int | None = None
    distinct_count_reason: Literal["exact", "approximate"]


class DistributionTemporalBucketDeltaV1(_Wire):
    left_bucket_id: str
    right_bucket_id: str
    start: datetime.datetime
    end: datetime.datetime
    end_inclusive: bool
    count_delta: int


class DistributionTemporalDeltaV1(_Wire):
    kind: Literal["temporal"] = "temporal"
    buckets: list[DistributionTemporalBucketDeltaV1] | None = Field(default=None, max_length=20)
    bucket_reason: Literal["equal_edges", "unequal_edges"]


DistributionMetricDeltaV1 = Annotated[
    DistributionNumericDeltaV1 | DistributionCategoricalDeltaV1 | DistributionTemporalDeltaV1,
    Field(discriminator="kind"),
]


class DistributionColumnComparisonV1(_Wire):
    match_reason: Literal["stable_field_identity", "name_and_logical_type"]
    field_id: str | None = None
    left_column: ColumnSchema
    right_column: ColumnSchema
    left_sections: list[DistributionReportSectionV1] = Field(max_length=2)
    right_sections: list[DistributionReportSectionV1] = Field(max_length=2)
    comparable: bool
    reason: Literal[
        "compatible",
        "coverage_mismatch",
        "computation_version_mismatch",
        "logical_type_mismatch",
        "section_kind_mismatch",
        "unsupported_section",
    ]
    missing_count_delta: int | None = None
    metric_delta: DistributionMetricDeltaV1 | None = None


class DistributionReportComparisonV1(_Wire):
    schema_version: Literal[1] = 1
    coverage: DistributionCoverageComparisonV1
    columns: list[DistributionColumnComparisonV1] = Field(max_length=64)
    unmatched_left_columns: list[ColumnSchema] = Field(max_length=64)
    unmatched_right_columns: list[ColumnSchema] = Field(max_length=64)


def _coverage(document: DistributionReportDocumentV1) -> list[ColumnSchema]:
    section = next(item for item in document.sections if item.kind == "coverage_schema")
    return section.columns


def _identity(document: DistributionReportDocumentV1) -> DistributionReportIdentityV1:
    sampling_identity = (
        document.sample_provenance.identity
        if document.sample_provenance is not None else document.view_definition_sha256)
    return DistributionReportIdentityV1(
        report_id=document.report_id,
        dataset_view_id=document.dataset_view_id,
        dataset_id=document.dataset_id,
        revision_id=document.revision_id,
        view_definition_sha256=document.view_definition_sha256,
        computation_version=document.computation_version,
        measured_rows=document.measured_rows,
        complete=document.complete,
        sampling_identity=sampling_identity,
        sample_provenance=document.sample_provenance,
    )


def _coverage_comparison(
    left: DistributionReportDocumentV1, right: DistributionReportDocumentV1,
) -> DistributionCoverageComparisonV1:
    if left.complete and right.complete:
        comparable, reason = True, "compatible_full_coverage"
    elif left.complete != right.complete:
        comparable, reason = False, "full_sample_coverage_mismatch"
    elif (left.sample_provenance is not None and right.sample_provenance is not None
          and left.sample_provenance.identity == right.sample_provenance.identity):
        comparable, reason = True, "same_deterministic_sample"
    else:
        comparable, reason = False, "different_deterministic_samples"
    return DistributionCoverageComparisonV1(
        left=_identity(left), right=_identity(right), comparable=comparable, reason=reason)


def _matched_columns(
    left: list[ColumnSchema], right: list[ColumnSchema],
) -> tuple[list[tuple[ColumnSchema, ColumnSchema, str]], list[ColumnSchema], list[ColumnSchema]]:
    matches: list[tuple[ColumnSchema, ColumnSchema, str]] = []
    used: set[int] = set()
    for left_column in left:
        candidates = [
            index for index, right_column in enumerate(right) if index not in used and (
                (left_column.field_id is not None
                 and right_column.field_id == left_column.field_id)
                or (left_column.field_id is None and right_column.field_id is None
                    and right_column.name == left_column.name
                    and right_column.type == left_column.type)
            )
        ]
        if len(candidates) != 1:
            continue
        index = candidates[0]
        used.add(index)
        matches.append((
            left_column, right[index],
            "stable_field_identity" if left_column.field_id is not None
            else "name_and_logical_type",
        ))
    matched_left = {id(item[0]) for item in matches}
    return (
        matches,
        [column for column in left if id(column) not in matched_left],
        [column for index, column in enumerate(right) if index not in used],
    )


def _column_sections(
    document: DistributionReportDocumentV1, column_name: str,
) -> list[DistributionReportSectionV1]:
    return [section for section in document.sections
            if getattr(section, "column_name", None) == column_name]


def _finite_delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    value = right - left
    return value if math.isfinite(value) else None


def _numeric_delta(
    left: DistributionNumericSectionV1, right: DistributionNumericSectionV1,
) -> DistributionNumericDeltaV1:
    equal_edges = len(left.histogram) == len(right.histogram) and all(
        (a.lower, a.upper, a.upper_inclusive) == (b.lower, b.upper, b.upper_inclusive)
        for a, b in zip(left.histogram, right.histogram, strict=True))
    histogram = ([DistributionHistogramDeltaV1(
        left_bucket_id=a.bucket_id, right_bucket_id=b.bucket_id,
        lower=a.lower, upper=a.upper, upper_inclusive=a.upper_inclusive,
        count_delta=b.count - a.count,
    ) for a, b in zip(left.histogram, right.histogram, strict=True)] if equal_edges else None)
    return DistributionNumericDeltaV1(
        count_delta=right.count - left.count,
        non_finite_count_delta=right.non_finite_count - left.non_finite_count,
        min_delta=_finite_delta(left.min, right.min),
        max_delta=_finite_delta(left.max, right.max),
        mean_delta=_finite_delta(left.mean, right.mean),
        stddev_delta=_finite_delta(left.stddev, right.stddev),
        quantiles=[DistributionQuantileDeltaV1(
            probability=a.probability, value_delta=_finite_delta(a.value, b.value),
        ) for a, b in zip(left.quantiles, right.quantiles, strict=True)],
        histogram=histogram,
        histogram_reason="equal_edges" if equal_edges else "unequal_edges",
    )


def _label_key(value: str | bool) -> tuple[str, str | bool]:
    return type(value).__name__, value


def _categorical_delta(
    left: DistributionCategoricalSectionV1, right: DistributionCategoricalSectionV1,
) -> DistributionCategoricalDeltaV1:
    left_counts = {_label_key(item.label): item.count for item in left.top}
    right_counts = {_label_key(item.label): item.count for item in right.top}
    labels = [item.label for item in left.top]
    labels.extend(item.label for item in right.top if _label_key(item.label) not in left_counts)
    categories = []
    for label in labels:
        left_count, right_count = left_counts.get(_label_key(label)), right_counts.get(_label_key(label))
        if left_count is None:
            reason = "outside_left_top_k"
        elif right_count is None:
            reason = "outside_right_top_k"
        else:
            reason = "present_in_both_top_k"
        categories.append(DistributionCategoryDeltaV1(
            label=label, left_count=left_count, right_count=right_count,
            count_delta=(right_count - left_count
                         if left_count is not None and right_count is not None else None),
            reason=reason,
        ))
    same_top = set(left_counts) == set(right_counts)
    exact_distinct = not left.distinct_count_approximate and not right.distinct_count_approximate
    return DistributionCategoricalDeltaV1(
        categories=categories,
        other_count_delta=(right.other_count - left.other_count if same_top else None),
        other_count_reason="same_top_k" if same_top else "different_top_k",
        distinct_count_delta=(right.distinct_count - left.distinct_count
                              if exact_distinct else None),
        distinct_count_reason="exact" if exact_distinct else "approximate",
    )


def _temporal_delta(
    left: DistributionTemporalSectionV1, right: DistributionTemporalSectionV1,
) -> DistributionTemporalDeltaV1:
    equal_edges = len(left.buckets) == len(right.buckets) and all(
        (a.start, a.end, a.end_inclusive) == (b.start, b.end, b.end_inclusive)
        for a, b in zip(left.buckets, right.buckets, strict=True))
    buckets = ([DistributionTemporalBucketDeltaV1(
        left_bucket_id=a.bucket_id, right_bucket_id=b.bucket_id,
        start=a.start, end=a.end, end_inclusive=a.end_inclusive,
        count_delta=b.count - a.count,
    ) for a, b in zip(left.buckets, right.buckets, strict=True)] if equal_edges else None)
    return DistributionTemporalDeltaV1(
        buckets=buckets, bucket_reason="equal_edges" if equal_edges else "unequal_edges")


def compare_reports(
    left: DistributionReportDocumentV1, right: DistributionReportDocumentV1,
) -> DistributionReportComparisonV1:
    coverage = _coverage_comparison(left, right)
    matches, unmatched_left, unmatched_right = _matched_columns(_coverage(left), _coverage(right))
    columns = []
    for left_column, right_column, match_reason in matches:
        left_sections = _column_sections(left, left_column.name)
        right_sections = _column_sections(right, right_column.name)
        left_missing = next((item for item in left_sections if item.kind == "missingness"), None)
        right_missing = next((item for item in right_sections if item.kind == "missingness"), None)
        left_metric = next((item for item in left_sections if item.kind != "missingness"), None)
        right_metric = next((item for item in right_sections if item.kind != "missingness"), None)
        base_comparable = coverage.comparable and left.computation_version == right.computation_version
        missing_delta = (
            right_missing.missing_count - left_missing.missing_count
            if base_comparable and left_missing is not None and right_missing is not None else None)
        metric_delta = None
        if not coverage.comparable:
            reason = "coverage_mismatch"
        elif left.computation_version != right.computation_version:
            reason = "computation_version_mismatch"
        elif left_column.type != right_column.type:
            reason = "logical_type_mismatch"
        elif left_metric is None or right_metric is None or left_metric.kind != right_metric.kind:
            reason = "section_kind_mismatch"
        elif left_metric.kind == "numeric":
            reason, metric_delta = "compatible", _numeric_delta(left_metric, right_metric)
        elif left_metric.kind == "categorical":
            reason, metric_delta = "compatible", _categorical_delta(left_metric, right_metric)
        elif left_metric.kind == "temporal":
            reason, metric_delta = "compatible", _temporal_delta(left_metric, right_metric)
        else:
            reason = "unsupported_section"
        columns.append(DistributionColumnComparisonV1(
            match_reason=match_reason,
            field_id=left_column.field_id,
            left_column=left_column,
            right_column=right_column,
            left_sections=left_sections,
            right_sections=right_sections,
            comparable=reason == "compatible",
            reason=reason,
            missing_count_delta=missing_delta,
            metric_delta=metric_delta,
        ))
    return DistributionReportComparisonV1(
        coverage=coverage, columns=columns,
        unmatched_left_columns=unmatched_left,
        unmatched_right_columns=unmatched_right,
    )
