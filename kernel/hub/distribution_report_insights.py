"""Ephemeral typed comparison and bounded bucket examples for retained reports."""

from __future__ import annotations

import datetime
import decimal
import json
import math
import struct
import uuid
from typing import Annotated, Any, Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

from hub import db
from hub.distribution_reports import (
    DistributionCategoricalSectionV1,
    DistributionNumericSectionV1,
    DistributionReportDocumentV1,
    DistributionReportSectionV1,
    DistributionTemporalSectionV1,
)
from hub.distribution_report_tasks import (
    MAX_BUCKETS,
    _bucket_index_expression,
    _numeric_bucket_predicate,
    _temporal_bucket_layout,
)
from hub.models import ColumnSchema, DatasetViewDefinitionV1, SampleProvenance, to_camel
from hub.routers.dataset_views import _defined_relation, _open_exact
from hub.sqlpolicy import quote_identifier


EXAMPLE_ROW_LIMIT = 100
_EXAMPLE_RESPONSE_MAX_BYTES = 256 * 1024
_EXAMPLE_MAX_DEPTH = 16
_EXAMPLE_MAX_NODES = 20_000
_EXAMPLE_MAX_CONTAINER_ITEMS = 4_096
_EXAMPLE_MAX_TEXT_BYTES = 64 * 1024


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


class DistributionReportBucketExamplesV1(_Wire):
    schema_version: Literal[1] = 1
    report_id: str
    dataset_view_id: str
    dataset_id: str
    revision_id: str
    view_definition_sha256: str
    computation_version: str
    sampling_identity: str
    sample_provenance: SampleProvenance | None = None
    section_id: str
    bucket_id: str
    bucket_kind: Literal["numeric", "categorical", "temporal"]
    column_name: str
    bucket_count: int = Field(ge=0)
    example_semantics: Literal["bounded_examples_from_measured_bucket"] = (
        "bounded_examples_from_measured_bucket")
    row_limit: Literal[100] = 100
    returned_rows: int = Field(ge=0, le=100)
    truncated: bool
    rows: list[dict[str, Any]] = Field(max_length=100)


class InvalidReportBucket(ValueError):
    pass


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


def _valid_numeric_histogram_layout(section: DistributionNumericSectionV1) -> bool:
    if section.min is None or section.max is None:
        return False
    if section.min == section.max:
        return (
            len(section.histogram) == 1
            and section.histogram[0].lower == section.min
            and section.histogram[0].upper == section.max
            and section.histogram[0].upper_inclusive
        )
    if section.min > section.max or len(section.histogram) != MAX_BUCKETS:
        return False
    previous_upper = section.min
    for index, bucket in enumerate(section.histogram):
        if (
            bucket.lower != previous_upper
            or bucket.upper <= bucket.lower
            or bucket.upper_inclusive != (index == MAX_BUCKETS - 1)
        ):
            return False
        previous_upper = bucket.upper
    return previous_upper == section.max


def _bucket_filter(
    document: DistributionReportDocumentV1, section_id: str, bucket_id: str,
) -> tuple[str, str, list[Any], int]:
    section = next((item for item in document.sections if item.section_id == section_id), None)
    if section is None or section.kind not in ("numeric", "categorical", "temporal"):
        raise InvalidReportBucket("unsupported distribution report section id")
    column = quote_identifier(section.column_name)
    if section.kind == "numeric":
        matched = next((
            (index, item) for index, item in enumerate(section.histogram)
            if item.bucket_id == bucket_id
        ), None)
        if matched is None or section.min is None or section.max is None:
            raise InvalidReportBucket("unsupported distribution report bucket id")
        _bucket_index, bucket = matched
        value = f"CAST({column} AS DOUBLE)"
        if not _valid_numeric_histogram_layout(section):
            raise InvalidReportBucket("unsupported distribution report bucket layout")
        interval, parameters = _numeric_bucket_predicate(
            value,
            bucket.lower,
            bucket.upper,
            upper_inclusive=bucket.upper_inclusive,
        )
        predicate = (
            f"{column} IS NOT NULL AND isfinite({value}) AND "
            f"{interval}")
        return section.kind, predicate, parameters, bucket.count
    if section.kind == "categorical":
        bucket = next((item for item in section.top if item.bucket_id == bucket_id), None)
        if bucket is None:
            raise InvalidReportBucket("unsupported distribution report bucket id")
        if isinstance(bucket.label, bool):
            return section.kind, f"{column} IS NOT NULL AND {column} = ?", [bucket.label], bucket.count
        return section.kind, f"{column} IS NOT NULL AND CAST({column} AS VARCHAR) = ?", [bucket.label], bucket.count
    matched = next((
        (index, item) for index, item in enumerate(section.buckets)
        if item.bucket_id == bucket_id
    ), None)
    if matched is None or section.min is None or section.max is None:
        raise InvalidReportBucket("unsupported distribution report bucket id")
    bucket_index, bucket = matched
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

    def micros(value: datetime.datetime) -> int:
        delta = value - epoch
        return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds)

    minimum, maximum = micros(section.min), micros(section.max)
    width, bucket_count = _temporal_bucket_layout(minimum, maximum)
    if len(section.buckets) != bucket_count:
        raise InvalidReportBucket("unsupported distribution report bucket layout")
    value = f"epoch_us({column})"
    predicate = (
        f"{column} IS NOT NULL AND isfinite({column}) AND "
        f"{_bucket_index_expression(value)} = ?")
    return (
        section.kind, predicate,
        [minimum, width, bucket_count - 1, bucket_index], bucket.count,
    )


class _ExampleBudgetExceeded(ValueError):
    pass


class _ExampleValueUnavailable(ValueError):
    pass


class _ExampleBudget:
    def __init__(self) -> None:
        self.nodes = _EXAMPLE_MAX_NODES
        self.text_bytes = _EXAMPLE_RESPONSE_MAX_BYTES

    def node(self) -> None:
        self.nodes -= 1
        if self.nodes < 0:
            raise _ExampleBudgetExceeded("example value node budget exceeded")

    def text(self, value: str) -> str:
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise _ExampleValueUnavailable("example text is not valid UTF-8") from exc
        if size > _EXAMPLE_MAX_TEXT_BYTES:
            raise _ExampleValueUnavailable("example text exceeds the scalar bound")
        self.text_bytes -= size
        if self.text_bytes < 0:
            raise _ExampleBudgetExceeded("example text budget exceeded")
        return value


class _ArrowPreflightBudget:
    def __init__(self, budget: _ExampleBudget, response_bytes: int, materialization_bytes: int):
        self.nodes = budget.nodes
        self.text_bytes = budget.text_bytes
        self.response_bytes = response_bytes
        self.materialization_bytes = materialization_bytes

    def node(self) -> None:
        self.nodes -= 1
        if self.nodes < 0:
            raise _ExampleBudgetExceeded("example value node budget exceeded")

    def text(self, size: int) -> None:
        if size > _EXAMPLE_MAX_TEXT_BYTES:
            raise _ExampleValueUnavailable("example text exceeds the scalar bound")
        self.text_bytes -= size
        if self.text_bytes < 0:
            raise _ExampleBudgetExceeded("example text budget exceeded")

    def response(self, size: int) -> None:
        self.response_bytes -= size
        if self.response_bytes < 0:
            raise _ExampleBudgetExceeded("example response budget exceeded")

    def materialization(self, size: int) -> None:
        self.materialization_bytes -= size
        if self.materialization_bytes < 0:
            raise _ExampleBudgetExceeded("example materialization budget exceeded")


def _variable_value_bounds(array: pa.Array, index: int, *, large: bool) -> tuple[int, int]:
    buffers = array.buffers()
    offsets = buffers[1]
    if offsets is None:
        raise _ExampleValueUnavailable("example value has invalid Arrow offsets")
    width, code = (8, "q") if large else (4, "i")
    position = array.offset + index
    byte_offset = position * width
    try:
        start = struct.unpack_from(f"<{code}", offsets, byte_offset)[0]
        end = struct.unpack_from(f"<{code}", offsets, byte_offset + width)[0]
    except struct.error as exc:
        raise _ExampleValueUnavailable("example value has invalid Arrow offsets") from exc
    values = buffers[2]
    value_bytes = 0 if values is None else values.size
    if start < 0 or end < start or end > value_bytes:
        raise _ExampleValueUnavailable("example value has invalid Arrow offsets")
    return start, end


def _escaped_json_string_size(buffer: pa.Buffer | bytes, start: int, size: int) -> int:
    encoded = 2
    for byte in memoryview(buffer)[start:start + size]:
        if byte in (8, 9, 10, 12, 13, 34, 92):
            encoded += 2
        elif byte < 32:
            encoded += 6
        else:
            encoded += 1
    return encoded


def _preflight_arrow_text(
    array: pa.Array,
    index: int,
    budget: _ArrowPreflightBudget,
    *,
    large: bool,
) -> None:
    start, end = _variable_value_bounds(array, index, large=large)
    size = end - start
    budget.text(size)
    budget.materialization(size)
    values = array.buffers()[2]
    if values is None:
        budget.response(2)
        return
    budget.response(_escaped_json_string_size(values, start, size))


def _preflight_arrow_binary(
    array: pa.Array,
    index: int,
    budget: _ArrowPreflightBudget,
    *,
    large: bool,
) -> None:
    start, end = _variable_value_bounds(array, index, large=large)
    size = end - start
    if size > _EXAMPLE_MAX_TEXT_BYTES:
        raise _ExampleValueUnavailable("example binary value exceeds the scalar bound")
    budget.materialization(size)
    placeholder_size = len(f"<{size} bytes>".encode("utf-8"))
    budget.text(placeholder_size)
    budget.response(placeholder_size + 2)


def _preflight_arrow_value(
    array: pa.Array,
    index: int,
    budget: _ArrowPreflightBudget,
    *,
    depth: int,
) -> None:
    budget.node()
    if depth > _EXAMPLE_MAX_DEPTH:
        raise _ExampleValueUnavailable("example value is nested too deeply")
    scalar = array[index]
    if not scalar.is_valid:
        budget.response(4)
        return
    data_type = array.type
    if pa.types.is_null(data_type):
        budget.response(4)
        return
    if pa.types.is_boolean(data_type):
        budget.response(5)
        return
    if pa.types.is_integer(data_type):
        budget.response(21)
        return
    if pa.types.is_floating(data_type):
        budget.response(32)
        return
    if pa.types.is_decimal(data_type):
        text_size = data_type.precision + abs(data_type.scale) + 16
        budget.text(text_size)
        budget.materialization(text_size)
        budget.response(text_size + 2)
        return
    if pa.types.is_date(data_type) or pa.types.is_time(data_type) or pa.types.is_timestamp(data_type):
        budget.text(64)
        budget.materialization(64)
        budget.response(66)
        return
    if isinstance(data_type, pa.UuidType):
        budget.text(36)
        budget.materialization(36)
        budget.response(38)
        return
    if pa.types.is_string(data_type):
        _preflight_arrow_text(array, index, budget, large=False)
        return
    if pa.types.is_large_string(data_type):
        _preflight_arrow_text(array, index, budget, large=True)
        return
    if pa.types.is_binary(data_type):
        _preflight_arrow_binary(array, index, budget, large=False)
        return
    if pa.types.is_large_binary(data_type):
        _preflight_arrow_binary(array, index, budget, large=True)
        return
    if pa.types.is_fixed_size_binary(data_type):
        size = data_type.byte_width
        if size > _EXAMPLE_MAX_TEXT_BYTES:
            raise _ExampleValueUnavailable("example binary value exceeds the scalar bound")
        budget.materialization(size)
        placeholder_size = len(f"<{size} bytes>".encode("utf-8"))
        budget.text(placeholder_size)
        budget.response(placeholder_size + 2)
        return
    if (
        pa.types.is_list(data_type)
        or pa.types.is_large_list(data_type)
        or pa.types.is_fixed_size_list(data_type)
    ):
        values = scalar.values
        item_count = len(values)
        if item_count > _EXAMPLE_MAX_CONTAINER_ITEMS:
            raise _ExampleValueUnavailable("example container exceeds the item bound")
        budget.response(2 + max(0, item_count - 1))
        for item_index in range(item_count):
            _preflight_arrow_value(values, item_index, budget, depth=depth + 1)
        return
    if pa.types.is_struct(data_type):
        item_count = data_type.num_fields
        if item_count > _EXAMPLE_MAX_CONTAINER_ITEMS:
            raise _ExampleValueUnavailable("example object exceeds the item bound")
        budget.response(2 + max(0, item_count - 1) + item_count)
        for field_index, field in enumerate(data_type):
            key = field.name.encode("utf-8", errors="strict")
            budget.text(len(key))
            budget.response(_escaped_json_string_size(key, 0, len(key)))
            _preflight_arrow_value(
                array.field(field_index), index, budget, depth=depth + 1)
        return
    if pa.types.is_map(data_type):
        entries = scalar.values
        item_count = len(entries)
        if item_count > _EXAMPLE_MAX_CONTAINER_ITEMS:
            raise _ExampleValueUnavailable("example container exceeds the item bound")
        budget.response(2 + max(0, item_count - 1))
        keys, items = entries.field(0), entries.field(1)
        for item_index in range(item_count):
            budget.node()
            if depth + 1 > _EXAMPLE_MAX_DEPTH:
                raise _ExampleValueUnavailable("example value is nested too deeply")
            budget.response(3)
            _preflight_arrow_value(keys, item_index, budget, depth=depth + 2)
            _preflight_arrow_value(items, item_index, budget, depth=depth + 2)
        return
    if pa.types.is_dictionary(data_type):
        dictionary_index = array.indices[index].as_py()
        if not isinstance(dictionary_index, int):
            raise _ExampleValueUnavailable("example dictionary index is invalid")
        _preflight_arrow_value(array.dictionary, dictionary_index, budget, depth=depth)
        budget.nodes += 1
        return
    raise _ExampleValueUnavailable("example Arrow value is not JSON-compatible")


def _preflight_arrow_row(
    batch: pa.RecordBatch,
    row_index: int,
    budget: _ExampleBudget,
    response_bytes: int,
    materialization_bytes: int,
) -> _ArrowPreflightBudget:
    probe = _ArrowPreflightBudget(budget, response_bytes, materialization_bytes)
    probe.node()
    if batch.num_columns > _EXAMPLE_MAX_CONTAINER_ITEMS:
        raise _ExampleValueUnavailable("example object exceeds the item bound")
    probe.response(2 + max(0, batch.num_columns - 1) + batch.num_columns)
    for column_index, field in enumerate(batch.schema):
        key = field.name.encode("utf-8", errors="strict")
        probe.text(len(key))
        probe.response(_escaped_json_string_size(key, 0, len(key)))
        _preflight_arrow_value(
            batch.column(column_index), row_index, probe, depth=1)
    return probe


def _arrow_scalar_as_py(array: pa.Array, index: int) -> Any:
    return array[index].as_py()


def _materialize_arrow_row(batch: pa.RecordBatch, row_index: int) -> dict[str, Any]:
    try:
        return {
            field.name: _arrow_scalar_as_py(batch.column(index), row_index)
            for index, field in enumerate(batch.schema)
        }
    except (OverflowError, TypeError, UnicodeError, ValueError) as exc:
        raise _ExampleValueUnavailable("example Arrow value cannot be materialized") from exc


def _json_safe_example(value: Any, budget: _ExampleBudget, *, depth: int = 0) -> Any:
    budget.node()
    if depth > _EXAMPLE_MAX_DEPTH:
        raise _ExampleValueUnavailable("example value is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return budget.text("NaN" if math.isnan(value) else (
            "Infinity" if value > 0 else "-Infinity"))
    if isinstance(value, decimal.Decimal):
        converted = float(value)
        if math.isfinite(converted) and decimal.Decimal(repr(converted)) == value:
            return converted
        return budget.text(str(value))
    if isinstance(value, str):
        return budget.text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return budget.text(f"<{len(value)} bytes>")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return budget.text(value.isoformat())
    if isinstance(value, uuid.UUID):
        return budget.text(str(value))
    if isinstance(value, (list, tuple)):
        if len(value) > _EXAMPLE_MAX_CONTAINER_ITEMS:
            raise _ExampleValueUnavailable("example container exceeds the item bound")
        return [_json_safe_example(item, budget, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > _EXAMPLE_MAX_CONTAINER_ITEMS:
            raise _ExampleValueUnavailable("example object exceeds the item bound")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _ExampleValueUnavailable("example object keys must be strings")
            normalized_key = budget.text(key)
            result[normalized_key] = _json_safe_example(item, budget, depth=depth + 1)
        return result
    raise _ExampleValueUnavailable("example value is not JSON-compatible")


def _bounded_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    size = 1024
    budget = _ExampleBudget()
    for row in rows:
        try:
            normalized = _json_safe_example(row, budget)
        except _ExampleBudgetExceeded:
            break
        except _ExampleValueUnavailable:
            continue
        if not isinstance(normalized, dict):  # pragma: no cover - Arrow rows are mappings
            continue
        encoded = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
        if size + len(encoded) > _EXAMPLE_RESPONSE_MAX_BYTES:
            break
        retained.append(normalized)
        size += len(encoded)
    return retained


def _bounded_arrow_rows(reader: pa.RecordBatchReader) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    size = 1024
    materialization_bytes = _EXAMPLE_RESPONSE_MAX_BYTES
    budget = _ExampleBudget()
    for batch in reader:
        for row_index in range(batch.num_rows):
            try:
                probe = _preflight_arrow_row(
                    batch,
                    row_index,
                    budget,
                    _EXAMPLE_RESPONSE_MAX_BYTES - size,
                    materialization_bytes,
                )
            except _ExampleBudgetExceeded:
                continue
            except _ExampleValueUnavailable:
                continue
            materialization_bytes = probe.materialization_bytes
            try:
                normalized = _json_safe_example(
                    _materialize_arrow_row(batch, row_index), budget)
            except _ExampleBudgetExceeded:
                return retained
            except _ExampleValueUnavailable:
                continue
            if not isinstance(normalized, dict):  # pragma: no cover - rows are mappings
                continue
            encoded = json.dumps(
                normalized, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False).encode("utf-8")
            if size + len(encoded) > _EXAMPLE_RESPONSE_MAX_BYTES:  # pragma: no cover
                return retained
            retained.append(normalized)
            size += len(encoded)
    return retained


def bucket_examples(
    document: DistributionReportDocumentV1,
    view: DatasetViewDefinitionV1,
    section_id: str,
    bucket_id: str,
) -> DistributionReportBucketExamplesV1:
    kind, predicate, parameters, bucket_count = _bucket_filter(document, section_id, bucket_id)
    section = next(item for item in document.sections if item.section_id == section_id)
    with _open_exact(view.dataset_ref, operation="distribution-report-drill-down") as source:
        relation = _defined_relation(source, view)
        table_name = db.unique_view("distribution_report_bucket")
        relation.create_view(table_name)
        reader = None
        try:
            reader = db.conn().sql(
                f"SELECT * FROM {quote_identifier(table_name)} WHERE {predicate} "
                f"LIMIT {EXAMPLE_ROW_LIMIT}",
                params=parameters,
            ).to_arrow_reader(batch_size=1)
            rows = _bounded_arrow_rows(reader)
        finally:
            if reader is not None:
                reader.close()
            db.conn().execute(f"DROP VIEW IF EXISTS {quote_identifier(table_name)}")
    sampling_identity = (
        document.sample_provenance.identity
        if document.sample_provenance is not None else document.view_definition_sha256)
    return DistributionReportBucketExamplesV1(
        report_id=document.report_id,
        dataset_view_id=document.dataset_view_id,
        dataset_id=document.dataset_id,
        revision_id=document.revision_id,
        view_definition_sha256=document.view_definition_sha256,
        computation_version=document.computation_version,
        sampling_identity=sampling_identity,
        sample_provenance=document.sample_provenance,
        section_id=section_id,
        bucket_id=bucket_id,
        bucket_kind=kind,
        column_name=section.column_name,
        bucket_count=bucket_count,
        returned_rows=len(rows),
        truncated=bucket_count > len(rows),
        rows=rows,
    )
