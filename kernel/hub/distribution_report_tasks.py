"""Bounded local computation and recovery for the certified distribution-report Task."""

from __future__ import annotations

import datetime
import logging
import math
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from hub import db, distribution_reports, metadb
from hub.models import ColumnSchema, DatasetViewDefinitionV1
from hub.plugins.adapters import (
    RevisionPermissionLost,
    RevisionProviderOffline,
    RevisionUnavailable,
    relation_columns,
)
from hub.routers.dataset_views import _defined_relation, _open_exact
from hub.sqlpolicy import quote_identifier
from hub.storage import ManagedSourceReadError


COMPUTATION_VERSION = "distribution-v1"
MAX_COLUMNS = 64
MAX_CATEGORIES = 20
MAX_BUCKETS = 20
MAX_LABEL_BYTES = 1024
DEADLINE_SECONDS = 30.0
CONFIRM_ROWS = 1_000_000
CONFIRM_BYTES = 128 * 1024 * 1024

_NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC",
)
_TEMPORAL_TYPES = ("DATE", "TIMESTAMP")
_CATEGORICAL_TYPES = ("VARCHAR", "BOOLEAN", "UUID", "ENUM")
_WIDE_INTEGER_TYPES = ("BIGINT", "UBIGINT", "HUGEINT", "UHUGEINT")
_EXACT_FLOAT_INTEGER_LIMIT = 2**53
_MAX_SAFE_STDDEV_SPAN = math.sqrt(sys.float_info.max)
_active_lock = threading.Lock()
_active: dict[str, threading.Thread] = {}


class _WorkerStop(RuntimeError):
    pass


@dataclass
class _LeaseState:
    deadline_at: float
    lost: bool = False
    cancel: bool = False
    deadline: bool = False
    interrupt: Callable[[], None] | None = None

    def check(self) -> None:
        if self.lost:
            raise _WorkerStop("lease_lost")
        if self.cancel:
            raise _WorkerStop("cancelled")
        if self.deadline or time.monotonic() >= self.deadline_at:
            self.deadline = True
            raise _WorkerStop("deadline")


def estimate_distribution_report(view: DatasetViewDefinitionV1) -> dict:
    """Use only retained revision metadata; never scan the DatasetView population."""
    binding = metadb.catalog_revision_binding(view.dataset_ref.dataset_id)
    artifact = metadb.managed_local_file_revision_artifact(
        view.dataset_ref.dataset_id, view.dataset_ref.revision_id)
    rows = None
    total_bytes = None
    if binding is not None and artifact is not None:
        try:
            detail = metadb.managed_local_file_revision_detail(
                str(binding["uri"]), view.dataset_ref.revision_id)
            rows = detail["table"].row_count
            total_bytes = os.path.getsize(artifact)
        except (KeyError, OSError):
            rows = total_bytes = None
    unknown = rows is None or total_bytes is None
    large = ((rows is not None and rows > CONFIRM_ROWS)
             or (total_bytes is not None and total_bytes > CONFIRM_BYTES))
    reason = "unknown_size" if unknown else "large_scan" if large else None
    return {
        "schema_version": 1,
        "dataset_view_id": view.id,
        "view_definition_sha256": view.definition_sha256,
        "estimated_scan_rows": rows,
        "estimated_scan_bytes": total_bytes,
        "selected_column_count": len(view.selected_columns),
        "needs_confirmation": unknown or large,
        "reason": reason,
        "limits": {
            "reported_columns": MAX_COLUMNS,
            "top_categories": MAX_CATEGORIES,
            "histogram_buckets": MAX_BUCKETS,
            "deadline_seconds": int(DEADLINE_SECONDS),
        },
    }


def _section_id(index: int, kind: str) -> str:
    return f"column-{index:03d}-{kind}"


def _bucket_index_expression(value: str) -> str:
    """The integer temporal membership function shared by report counts and drill-down."""
    quotient = f"(({value} - ?) // ?)"
    return f"least(CAST({quotient} AS INTEGER), ?)"


def _numeric_bucket_edges(minimum: float, maximum: float) -> list[float]:
    width = (maximum - minimum) / MAX_BUCKETS
    return [minimum + width * bucket for bucket in range(MAX_BUCKETS)] + [maximum]


def _numeric_bucket_predicate(
    value: str,
    lower: float,
    upper: float,
    *,
    upper_inclusive: bool,
) -> tuple[str, list[float]]:
    upper_operator = "<=" if upper_inclusive else "<"
    return f"({value} >= ? AND {value} {upper_operator} ?)", [lower, upper]


def _numeric_bucket_case(value: str, edges: list[float]) -> tuple[str, list[float]]:
    if len(edges) != MAX_BUCKETS + 1:
        raise ValueError("numeric histogram requires the fixed bounded edges")
    clauses = []
    parameters = []
    for bucket, (lower, upper) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
        predicate, bounds = _numeric_bucket_predicate(
            value, lower, upper, upper_inclusive=bucket == MAX_BUCKETS - 1)
        clauses.append(f"WHEN {predicate} THEN {bucket}")
        parameters.extend(bounds)
    return f"CASE {' '.join(clauses)} END", parameters


def _temporal_bucket_layout(minimum: int, maximum: int) -> tuple[int, int]:
    population_span = maximum - minimum + 1
    width = max(1, (population_span + MAX_BUCKETS - 1) // MAX_BUCKETS)
    bucket_count = min(MAX_BUCKETS, max(1, (population_span + width - 1) // width))
    return width, bucket_count


def _finite_float(value) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _utc(micros: int) -> datetime.datetime:
    return (
        datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(microseconds=int(micros)))


def _unsupported(
    section_id: str, reason: str, *, column: str | None = None,
    omitted: int | None = None,
) -> dict:
    result = {"kind": "unsupported", "sectionId": section_id,
              "reason": reason, "partial": True}
    if column is not None:
        result["columnName"] = column
    if omitted is not None:
        result["omittedCount"] = omitted
    return result


def _numeric_section(
    con, table: str, column: str, physical_type: str, index: int, missing: int,
) -> list[dict]:
    del missing  # reconciliation is validated by the document model
    quoted = quote_identifier(column)
    section_id = _section_id(index, "numeric")
    if physical_type.startswith(("DECIMAL", "NUMERIC")):
        return [_unsupported(
            section_id, "numeric_precision_unsupported", column=column)]
    if physical_type.startswith(_WIDE_INTEGER_TYPES):
        raw_bounds = con.execute(
            f"SELECT min({quoted}), max({quoted}) FROM {quote_identifier(table)}"
        ).fetchone()
        assert raw_bounds is not None
        if ((raw_bounds[0] is not None
             and int(raw_bounds[0]) < -_EXACT_FLOAT_INTEGER_LIMIT)
                or (raw_bounds[1] is not None
                    and int(raw_bounds[1]) > _EXACT_FLOAT_INTEGER_LIMIT)):
            return [_unsupported(
                section_id, "numeric_precision_unsupported", column=column)]
    value = f"CAST({quoted} AS DOUBLE)"
    row = con.execute(
        f"SELECT count(*) FILTER (WHERE {quoted} IS NOT NULL AND isfinite({value})), "
        f"count(*) FILTER (WHERE {quoted} IS NOT NULL AND NOT isfinite({value})), "
        f"min({value}) FILTER (WHERE isfinite({value})), "
        f"max({value}) FILTER (WHERE isfinite({value})) "
        f"FROM {quote_identifier(table)}"
    ).fetchone()
    assert row is not None
    count, non_finite = int(row[0]), int(row[1])
    if count == 0:
        return [_unsupported(
            section_id, "empty_finite_population", column=column)]
    minimum, maximum = float(row[2]), float(row[3])
    edges = None
    if minimum != maximum:
        span = maximum - minimum
        if not math.isfinite(span) or span > _MAX_SAFE_STDDEV_SPAN:
            return [_unsupported(
                section_id, "numeric_range_unsupported", column=column)]
        edges = _numeric_bucket_edges(minimum, maximum)
        if any(upper <= lower for lower, upper in zip(edges, edges[1:])):
            return [_unsupported(
                section_id, "numeric_range_unsupported", column=column)]
        stats = con.execute(
            f"SELECT ? + avg({value} - ?) FILTER (WHERE isfinite({value})), "
            f"stddev_pop({value}) FILTER (WHERE isfinite({value})), "
            f"quantile_cont({value}, [0.0, 0.25, 0.5, 0.75, 1.0]) "
            f"FILTER (WHERE isfinite({value})) FROM {quote_identifier(table)}",
            [minimum, minimum],
        ).fetchone()
        assert stats is not None
        mean, stddev, quantile_values = stats
    else:
        mean, stddev = minimum, 0.0
        quantile_values = [minimum] * 5
    quantiles = [{"probability": probability, "value": float(quantile)}
                 for probability, quantile in zip(
                     (0.0, 0.25, 0.5, 0.75, 1.0), quantile_values, strict=True)]
    histogram: list[dict] = []
    if minimum == maximum:
        histogram.append({
            "bucketId": f"column-{index:03d}-numeric-000",
            "lower": minimum, "upper": maximum, "count": count, "upperInclusive": True,
        })
    else:
        assert edges is not None
        bucket_case, parameters = _numeric_bucket_case(value, edges)
        buckets = con.execute(
            f"SELECT {bucket_case} AS bucket, "
            f"count(*) FROM {quote_identifier(table)} "
            f"WHERE {quoted} IS NOT NULL AND isfinite({value}) "
            "GROUP BY bucket ORDER BY bucket",
            parameters,
        ).fetchall()
        counts = {int(bucket): int(bucket_count) for bucket, bucket_count in buckets}
        for bucket, (lower, upper) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
            histogram.append({
                "bucketId": f"column-{index:03d}-numeric-{bucket:03d}",
                "lower": lower, "upper": upper, "count": counts.get(bucket, 0),
                "upperInclusive": bucket == MAX_BUCKETS - 1,
            })
    return [{
        "kind": "numeric", "sectionId": section_id,
        "columnName": column, "count": count, "nonFiniteCount": non_finite,
        "min": minimum, "max": maximum, "mean": _finite_float(mean),
        "stddev": _finite_float(stddev), "quantiles": quantiles, "histogram": histogram,
    }]


def _categorical_section(
    con, table: str, column: str, physical_type: str, index: int, missing: int,
    measured_rows: int,
) -> list[dict]:
    quoted = quote_identifier(column)
    boolean = physical_type == "BOOLEAN"
    label = quoted if boolean else f"CAST({quoted} AS VARCHAR)"
    rows = con.execute(
        f"SELECT {label} AS label, count(*) AS n FROM {quote_identifier(table)} "
        f"WHERE {quoted} IS NOT NULL GROUP BY {quoted} "
        f"ORDER BY n DESC, {label} ASC LIMIT {MAX_CATEGORIES}"
    ).fetchall()
    if any(len(str(value).encode("utf-8", errors="strict")) > MAX_LABEL_BYTES
           for value, _count in rows):
        return [_unsupported(
            _section_id(index, "categorical"), "oversized_label", column=column)]
    distinct_expression = (
        f"count(DISTINCT {quoted})" if boolean else f"approx_count_distinct({quoted})")
    distinct = int(con.execute(
        f"SELECT {distinct_expression} FROM {quote_identifier(table)} "
        f"WHERE {quoted} IS NOT NULL").fetchone()[0])
    top = [{
        "bucketId": f"column-{index:03d}-categorical-{position:03d}",
        "label": value, "count": int(count),
    } for position, (value, count) in enumerate(rows)]
    other = measured_rows - missing - sum(item["count"] for item in top)
    return [{
        "kind": "categorical", "sectionId": _section_id(index, "categorical"),
        "columnName": column, "top": top, "otherCount": other,
        "distinctCount": distinct, "distinctCountApproximate": not boolean,
    }]


def _temporal_section(
    con, table: str, column: str, index: int, missing: int, measured_rows: int,
) -> list[dict]:
    quoted = quote_identifier(column)
    row = con.execute(
        f"SELECT count(*) FILTER (WHERE {quoted} IS NOT NULL AND isfinite({quoted})), "
        f"count(*) FILTER (WHERE {quoted} IS NOT NULL AND NOT isfinite({quoted})), "
        f"min(epoch_us({quoted})) FILTER (WHERE isfinite({quoted})), "
        f"max(epoch_us({quoted})) FILTER (WHERE isfinite({quoted})) "
        f"FROM {quote_identifier(table)}"
    ).fetchone()
    assert row is not None
    finite, non_finite = int(row[0]), int(row[1])
    if non_finite:
        return [_unsupported(
            _section_id(index, "temporal"), "non_finite_values", column=column)]
    if finite == 0:
        return [_unsupported(
            _section_id(index, "temporal"), "empty_finite_population", column=column)]
    minimum, maximum = int(row[2]), int(row[3])
    width, bucket_count = _temporal_bucket_layout(minimum, maximum)
    grouped = con.execute(
        f"SELECT {_bucket_index_expression(f'epoch_us({quoted})')} bucket, "
        f"count(*) FROM {quote_identifier(table)} WHERE {quoted} IS NOT NULL "
        "GROUP BY bucket ORDER BY bucket",
        [minimum, width, bucket_count - 1],
    ).fetchall()
    counts = {int(bucket): int(count) for bucket, count in grouped}
    buckets = []
    for bucket in range(bucket_count):
        start = minimum + width * bucket
        final = bucket == bucket_count - 1
        end = maximum if final else minimum + width * (bucket + 1)
        buckets.append({
            "bucketId": f"column-{index:03d}-temporal-{bucket:03d}",
            "start": _utc(start), "end": _utc(end), "count": counts.get(bucket, 0),
            "endInclusive": final,
        })
    assert sum(item["count"] for item in buckets) + missing == measured_rows
    return [{
        "kind": "temporal", "sectionId": _section_id(index, "temporal"),
        "columnName": column, "min": _utc(minimum), "max": _utc(maximum),
        "buckets": buckets,
    }]


def _report_columns(source, relation) -> list[ColumnSchema]:
    """Carry only unambiguous provider/declared field identities from exact revision evidence."""
    inferred = relation_columns(relation)
    try:
        evidence = [ColumnSchema.model_validate(item) for item in source.detail.get("columns", [])]
    except (TypeError, ValueError):
        return inferred
    field_id_counts: dict[str, int] = {}
    for column in evidence:
        if column.field_id is not None:
            field_id_counts[column.field_id] = field_id_counts.get(column.field_id, 0) + 1
    by_name: dict[str, list[ColumnSchema]] = {}
    for column in evidence:
        by_name.setdefault(column.name, []).append(column)
    result = []
    for column in inferred:
        candidates = [item for item in by_name.get(column.name, []) if (
            item.type == column.type
            and item.field_id is not None
            and item.provenance in ("declared", "provider")
            and field_id_counts.get(item.field_id) == 1
        )]
        if len(candidates) == 1:
            result.append(column.model_copy(update={
                "field_id": candidates[0].field_id,
                "provenance": candidates[0].provenance,
            }))
        else:
            result.append(column)
    return result


def compute_distribution_report(claim: dict, lease: _LeaseState) -> dict:
    view = DatasetViewDefinitionV1.model_validate(claim["view_snapshot"])
    with _open_exact(view.dataset_ref, operation="distribution-report") as source:
        relation = _defined_relation(source, view)
        columns = _report_columns(source, relation)
        safe_columns = [column for column in columns
                        if len(column.name.encode("utf-8", errors="strict")) <= 512]
        eligible = safe_columns[:MAX_COLUMNS]
        report_view = db.unique_view("distribution_report")
        relation.create_view(report_view)
        con = db.conn()
        lease.interrupt = con.interrupt
        try:
            lease.check()
            expressions = ["count(*)"] + [
                f"count(*) FILTER (WHERE {quote_identifier(column.name)} IS NULL)"
                for column in eligible
            ]
            counts = con.execute(
                f"SELECT {', '.join(expressions)} FROM {quote_identifier(report_view)}"
            ).fetchone()
            assert counts is not None
            measured_rows = int(counts[0])
            sections: list[dict] = [{
                "kind": "coverage_schema", "sectionId": "coverage-schema",
                "selectedColumnCount": len(columns), "reportedColumnCount": len(eligible),
                "columns": [column.model_dump(by_alias=True, mode="json") for column in eligible],
            }]
            missing_counts: dict[str, int] = {}
            for index, (column, missing) in enumerate(zip(eligible, counts[1:], strict=True)):
                missing_counts[column.name] = int(missing)
                sections.append({
                    "kind": "missingness", "sectionId": _section_id(index, "missingness"),
                    "columnName": column.name, "missingCount": int(missing),
                })
            oversized = len(columns) - len(safe_columns)
            limited = len(safe_columns) - len(eligible)
            if limited:
                sections.append(_unsupported(
                    "omitted-columns", "column_limit", omitted=limited))
            if oversized:
                sections.append(_unsupported(
                    "oversized-column-labels", "oversized_label", omitted=oversized))
            for index, column in enumerate(eligible):
                lease.check()
                physical = str(column.physical_type or "").upper()
                missing = missing_counts[column.name]
                if physical.startswith(_NUMERIC_TYPES):
                    sections.extend(_numeric_section(
                        con, report_view, column.name, physical, index, missing))
                elif physical.startswith(_TEMPORAL_TYPES):
                    sections.extend(_temporal_section(
                        con, report_view, column.name, index, missing, measured_rows))
                elif physical.startswith(_CATEGORICAL_TYPES):
                    sections.extend(_categorical_section(
                        con, report_view, column.name, physical, index, missing, measured_rows))
                else:
                    unsupported = _unsupported(
                        _section_id(index, "unsupported"), "unsupported_type", column=column.name)
                    unsupported["partial"] = False
                    sections.append(unsupported)
            lease.check()
        finally:
            lease.interrupt = None
            con.execute(f"DROP VIEW IF EXISTS {quote_identifier(report_view)}")
    limitations = list(view.sample_provenance.limitations if view.sample_provenance else [])
    limitations.append(
        "Statistics are exact over the deterministic reservoir sample; no full-population claim."
        if view.sampling.kind == "reservoir"
        else "The report scanned every row selected by the exact DatasetView."
    )
    return {
        "schemaVersion": 1,
        "reportId": claim["report_id"],
        "taskId": claim["task"]["id"],
        "datasetViewId": view.id,
        "datasetId": view.dataset_ref.dataset_id,
        "revisionId": view.dataset_ref.revision_id,
        "viewDefinitionSha256": view.definition_sha256,
        "computationVersion": COMPUTATION_VERSION,
        "measuredRows": measured_rows,
        "complete": view.sampling.kind == "all",
        "sampleProvenance": (view.sample_provenance.model_dump(by_alias=True, mode="json")
                             if view.sample_provenance else None),
        "limitations": limitations,
        "sections": sections,
    }


def _lease_monitor(
    task_id: str, attempt_id: str, owner_token: str,
    state: _LeaseState, done: threading.Event,
) -> None:
    while not done.wait(1.0):
        if not distribution_reports.heartbeat_distribution_report(
                task_id, attempt_id, owner_token):
            state.lost = True
            if state.interrupt is not None:
                state.interrupt()
            return
        if distribution_reports.distribution_report_should_stop(
                task_id, attempt_id, owner_token):
            state.cancel = True
            if state.interrupt is not None:
                state.interrupt()
        if time.monotonic() >= state.deadline_at:
            state.deadline = True
            if state.interrupt is not None:
                state.interrupt()


def _worker(task_id: str) -> None:
    owner_token = f"report:{uuid.uuid4().hex}:{threading.get_ident()}"
    try:
        claim = distribution_reports.claim_distribution_report(task_id, owner_token)
        if claim is None:
            return
        attempt_id = str(claim["task"]["attempts"][-1]["id"])
        state = _LeaseState(deadline_at=time.monotonic() + DEADLINE_SECONDS)
        done = threading.Event()
        monitor = threading.Thread(
            target=_lease_monitor,
            args=(task_id, attempt_id, owner_token, state, done),
            daemon=True, name=f"dp-report-lease-{task_id[-8:]}")
        monitor.start()
        try:
            document = compute_distribution_report(claim, state)
            state.check()
            distribution_reports.complete_distribution_report(
                task_id=task_id, attempt_id=attempt_id,
                owner_token=owner_token, report=document)
        except _WorkerStop as exc:
            if str(exc) == "lease_lost":
                return
            distribution_reports.fail_distribution_report(
                task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                failure_code=("distribution_report_deadline"
                              if str(exc) == "deadline"
                              else "distribution_report_computation_failed"))
        except (RevisionUnavailable, RevisionPermissionLost, RevisionProviderOffline,
                ManagedSourceReadError, KeyError):
            distribution_reports.fail_distribution_report(
                task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                failure_code="distribution_report_revision_unavailable")
        except BaseException:
            if state.lost:
                return
            if state.deadline or state.cancel:
                distribution_reports.fail_distribution_report(
                    task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                    failure_code=("distribution_report_deadline"
                                  if state.deadline
                                  else "distribution_report_computation_failed"))
            else:
                logging.getLogger("hub").exception("distribution report computation failed")
                distribution_reports.fail_distribution_report(
                    task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)
        finally:
            done.set()
            monitor.join(timeout=2)
    finally:
        with _active_lock:
            if _active.get(task_id) is threading.current_thread():
                _active.pop(task_id, None)


def dispatch(task_id: str) -> None:
    with _active_lock:
        current = _active.get(str(task_id))
        if current is not None and current.is_alive():
            return
        thread = threading.Thread(
            target=_worker, args=(str(task_id),), daemon=True,
            name=f"dp-distribution-report-{str(task_id)[-12:]}")
        _active[str(task_id)] = thread
        thread.start()


def recover() -> None:
    for task_id in distribution_reports.due_distribution_report_task_ids():
        dispatch(task_id)
