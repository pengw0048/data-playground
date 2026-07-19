"""Pure bounded point-timeline resampling with no reader, durable state, or public DTO."""
from __future__ import annotations

import hashlib
import json
import math
import re
from bisect import bisect_left
from dataclasses import dataclass, replace
from typing import Any, Mapping

import pyarrow as pa

from hub.compound_datasets import ClockMapping, CompoundManifestError, RevisionManifest, open_compound_manifest


COMPUTATION_VERSION = "temporal-resample-v1"
MAX_POINTS = 10_000
MAX_FIELDS = 32
MAX_SELECTED_VALUE_BYTES = 1_048_576
MAX_WINDOW_TICKS = 1_000_000_000_000
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCALAR_TYPES = frozenset({"bool", "int8", "int16", "int32", "int64", "uint8", "uint16",
                           "uint32", "uint64", "float16", "float32", "float64", "double",
                           "string", "large_string"})


class TemporalResampleError(ValueError):
    """Inputs cannot produce one bounded, exact candidate."""


@dataclass(frozen=True)
class DatasetViewIdentity:
    """The exact DatasetView admission fact supplied by the durable caller."""

    dataset_id: str
    revision_id: str
    view_id: str
    definition_sha256: str
    semantic_sha256: str


@dataclass(frozen=True)
class ResampleWindow:
    time_domain: str
    start_tick: int
    end_tick: int


@dataclass(frozen=True)
class FieldSelection:
    field: str
    unit: str


@dataclass(frozen=True)
class FixedGridTarget:
    """An exact, phase-aligned rational grid in one declared target clock."""

    target_clock_id: str
    rate_numerator: int
    rate_denominator: int
    phase_tick: int
    period_ticks: int = 0


@dataclass(frozen=True)
class TemporalResampleSpecV1:
    """Semantic identity for one point-to-point nearest resampling request."""

    compound_dataset_id: str
    compound_revision_id: str
    episode_id: str
    source_stream_id: str
    target_stream_id: str
    output_stream_id: str
    source_view: DatasetViewIdentity
    target_view: DatasetViewIdentity | None
    mapping: ClockMapping
    window: ResampleWindow
    tolerance_ticks: int
    selected_fields: tuple[FieldSelection, ...]
    candidate_cap: int
    output_cap: int
    fixed_grid: FixedGridTarget | None = None
    computation_version: str = COMPUTATION_VERSION

    def identity_document(self) -> dict[str, object]:
        """Return the canonical semantic identity, independent of input ordering."""
        _validate_spec_shape(self)
        if self.fixed_grid is not None and self.fixed_grid.period_ticks <= 0:
            raise TemporalResampleError("fixed grid must be preflight canonicalized before identity")
        return _spec_document(self)

    @property
    def idempotency_digest(self) -> str:
        return _digest(self.identity_document())


@dataclass(frozen=True)
class PointObservation:
    """One already-read point fact; values contain only selected source fields."""

    observation_id: str
    tick: int
    values: Mapping[str, object]


@dataclass(frozen=True)
class ResampleRow:
    target_observation_id: str
    target_tick: int
    source_observation_id: str | None
    source_tick: int | None
    mapped_source_tick: int | None
    signed_delta_ticks: int | None
    absolute_delta_ticks: int | None
    values: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ResampleCandidate:
    spec: TemporalResampleSpecV1
    source_points: tuple[PointObservation, ...]
    target_points: tuple[PointObservation, ...]
    rows: tuple[ResampleRow, ...]
    evidence: dict[str, object]
    digest: str


@dataclass(frozen=True)
class ManagedOutputRevision:
    """Exact output identity; staged artifact schema validation belongs to #589/#590."""

    member_id: str
    dataset_id: str
    revision_id: str


def build_resample_candidate(
    manifest: RevisionManifest, spec: TemporalResampleSpecV1,
    source_points: tuple[PointObservation, ...] | list[PointObservation],
    target_points: tuple[PointObservation, ...] | list[PointObservation],
) -> ResampleCandidate:
    """Validate all facts, then build bounded rows with source reuse and earlier-tick ties."""
    _validate_exact_manifest(manifest)
    spec = _validate_contract(manifest, spec)
    spec = replace(spec, selected_fields=tuple(sorted(spec.selected_fields, key=lambda item: item.field)))
    if not isinstance(source_points, (tuple, list)) or not isinstance(target_points, (tuple, list)):
        raise TemporalResampleError("point collections are invalid")
    if len(source_points) > MAX_POINTS or len(target_points) > MAX_POINTS:
        raise TemporalResampleError("point cap exceeded")
    if spec.fixed_grid is not None and target_points:
        generated = _fixed_grid_points(spec)
        if tuple(target_points) != generated:
            raise TemporalResampleError("fixed grid target observations are not canonical")
    if len(target_points) > spec.candidate_cap or len(target_points) > spec.output_cap:
        raise TemporalResampleError("candidate or output cap exceeded")
    source_stream = next(item for item in manifest.streams if item.id == spec.source_stream_id)
    source_schema = {item.name: item for item in source_stream.observation_schema}
    source = tuple(sorted(_normalize_points(
        source_points, spec.selected_fields, "source", source_schema),
        key=lambda item: (item.tick, item.observation_id)))
    target = (_fixed_grid_points(spec) if spec.fixed_grid is not None
              else _normalize_points(target_points, (), "target", {}))
    if len(target) > spec.candidate_cap or len(target) > spec.output_cap:
        raise TemporalResampleError("fixed grid exceeds candidate or output cap")
    if any(not (spec.window.start_tick <= item.tick < spec.window.end_tick) for item in target):
        raise TemporalResampleError("target point lies outside the exact window")
    mapped = _mapped_sources(source, spec.mapping)
    targets = tuple(sorted(target, key=lambda item: (item.tick, item.observation_id)))
    mapped_ticks = tuple(item.mapped_tick for item in mapped)
    rows_list: list[ResampleRow] = []
    projected_bytes = 0
    for target in targets:
        row = _row_for(target, mapped, mapped_ticks, spec)
        projected_bytes += sum(_serialized_scalar_size(value, MAX_SELECTED_VALUE_BYTES - projected_bytes)
                               for _, value in row.values)
        if projected_bytes > MAX_SELECTED_VALUE_BYTES:
            raise TemporalResampleError("projected selected values exceed their cumulative byte cap")
        rows_list.append(row)
    rows = tuple(rows_list)
    evidence = _evidence(spec, source, targets, rows)
    digest = _digest({"spec": spec.identity_document(), "evidence": evidence,
                      "rows": [_materialization_row(spec, row) for row in rows]})
    return ResampleCandidate(spec, source, targets, rows, evidence, digest)


def preflight_fixed_grid(manifest: RevisionManifest, spec: TemporalResampleSpecV1) -> TemporalResampleSpecV1:
    """Validate and canonicalize a fixed grid before source/target observations are read."""
    _validate_exact_manifest(manifest)
    if spec.fixed_grid is None:
        raise TemporalResampleError("fixed grid target is unavailable")
    return _validate_contract(manifest, spec)


def compose_child_manifest(
    parent: RevisionManifest, candidate: ResampleCandidate, output: ManagedOutputRevision,
) -> dict[str, object]:
    """Compose one canonical child compound manifest without changing its parent."""
    if not isinstance(output, ManagedOutputRevision):
        raise TemporalResampleError("managed output identity is invalid")
    for value in (output.member_id, output.dataset_id, output.revision_id):
        _text(value, "managed output identity")
    rebuilt = build_resample_candidate(
        parent, candidate.spec, candidate.source_points, candidate.target_points)
    if candidate != rebuilt:
        raise TemporalResampleError("candidate is not the canonical rows and evidence")
    candidate = rebuilt
    evidence_digest = _digest(candidate.evidence)
    schema = _output_schema(parent, candidate.spec)
    schema_digest = _compound_member_schema_digest(schema)
    document = _manifest_document(parent)
    document["members"] = sorted([*document["members"], {
        "id": output.member_id, "datasetId": output.dataset_id, "revisionId": output.revision_id,
        "schemaDigest": schema_digest}], key=lambda item: item["id"])
    document["streams"] = sorted([*document["streams"], _output_stream(
        parent, candidate.spec, schema, evidence_digest, candidate.digest)], key=lambda item: item["id"])
    document["bindings"] = sorted([*document["bindings"], *_output_bindings(
        parent, candidate.spec, output.member_id, schema)], key=lambda item: (item["episodeId"], item["streamId"]))
    document["revisionId"] = _manifest_digest(document)
    try:
        open_compound_manifest(_json(document).encode())
    except CompoundManifestError as exc:
        raise TemporalResampleError("child compound manifest is invalid") from exc
    return document
@dataclass(frozen=True)
class _MappedSource:
    point: PointObservation
    mapped_tick: int
def _mapped_sources(source: tuple[PointObservation, ...], mapping: ClockMapping) -> tuple[_MappedSource, ...]:
    """Keep the deterministic representative for each mapped point tick."""
    by_tick: dict[int, PointObservation] = {}
    for point in source:
        mapped_tick = _map_tick(mapping, point.tick)
        prior = by_tick.get(mapped_tick)
        if prior is None or point.observation_id < prior.observation_id:
            by_tick[mapped_tick] = point
    return tuple(_MappedSource(point, tick) for tick, point in sorted(by_tick.items()))


def _validate_exact_manifest(manifest: RevisionManifest) -> None:
    try:
        rebuilt = open_compound_manifest(_json(_manifest_document(manifest)).encode())
    except (AttributeError, CompoundManifestError) as exc:
        raise TemporalResampleError("parent manifest is not exact and canonical") from exc
    if rebuilt != manifest:
        raise TemporalResampleError("parent manifest semantics do not match its exact revision")


def _validate_contract(manifest: RevisionManifest, spec: TemporalResampleSpecV1) -> TemporalResampleSpecV1:
    _validate_spec_shape(spec)
    if (spec.compound_dataset_id, spec.compound_revision_id) != (
            manifest.ref.dataset_id, manifest.ref.revision_id):
        raise TemporalResampleError("spec does not bind the exact compound revision")
    streams = {item.id: item for item in manifest.streams}
    bindings = {(item.episode_id, item.stream_id): item for item in manifest.bindings}
    if spec.episode_id not in {item.episode_id for item in manifest.episodes}:
        raise TemporalResampleError("episode is not in the compound revision")
    if spec.source_stream_id == spec.target_stream_id or any(
            item not in streams for item in (spec.source_stream_id, spec.target_stream_id)):
        raise TemporalResampleError("source and target streams must be distinct declared streams")
    source_binding = bindings[(spec.episode_id, spec.source_stream_id)]
    target_binding = bindings.get((spec.episode_id, spec.target_stream_id))
    if source_binding.state != "present" or source_binding.observation_index is None or source_binding.observation_index.tick_field is None:
        raise TemporalResampleError("resampling requires a present source point stream")
    _validate_view(source_binding.member_id, spec.source_view, manifest, "source")
    source_stream, target_stream = streams[spec.source_stream_id], streams[spec.target_stream_id]
    if spec.fixed_grid is None:
        if (target_binding is None or target_binding.state != "present"
                or target_binding.observation_index is None or target_binding.observation_index.tick_field is None):
            raise TemporalResampleError("stream timestamp resampling requires two present point streams")
        assert spec.target_view is not None
        _validate_view(target_binding.member_id, spec.target_view, manifest, "target")
    if spec.window.time_domain != target_stream.clock.time_domain:
        raise TemporalResampleError("window time domain must equal the target stream clock domain")
    if spec.output_stream_id in streams:
        raise TemporalResampleError("output stream identity already exists in the parent")
    if (spec.mapping.source_clock_id, spec.mapping.target_clock_id) != (
            source_stream.clock.id, target_stream.clock.id):
        raise TemporalResampleError("mapping must directly name the source and target clocks")
    if spec.mapping not in manifest.clock_mappings:
        raise TemporalResampleError("mapping is not declared by the exact compound revision")
    units = {item.field: item.unit for item in source_stream.units}
    schema = {item.name: item for item in source_stream.observation_schema}
    value_refs = set(source_binding.observation_index.value_refs)
    fixed = {"observation_id", "episode_id", "target_tick", "source_observation_id", "source_tick",
             "mapped_source_tick", "signed_delta_ticks", "absolute_delta_ticks"}
    if any(item.field not in value_refs or units.get(item.field) != item.unit or item.field in fixed
           for item in spec.selected_fields):
        raise TemporalResampleError("selected field or unit is not declared by the source stream")
    if any(schema[item.field].type not in _SCALAR_TYPES for item in spec.selected_fields):
        raise TemporalResampleError("selected field type is unsupported by resample v1")
    if spec.fixed_grid is not None:
        spec = replace(spec, fixed_grid=_canonical_fixed_grid(spec.fixed_grid, target_stream.clock))
        # Generate before a source scan or any publication path can begin.  This proves
        # the rate, phase, bounds, and caps are exact and bounded from the immutable
        # manifest contract alone.
        _fixed_grid_points(spec)
    return spec


def _validate_view(member_id: str | None, view: DatasetViewIdentity,
                   manifest: RevisionManifest, role: str) -> None:
    member = next((item for item in manifest.members if item.id == member_id), None)
    if member is None or (view.dataset_id, view.revision_id) != (member.dataset_id, member.revision_id):
        raise TemporalResampleError(f"{role} DatasetView does not bind its exact member")


def _validate_spec_shape(spec: TemporalResampleSpecV1) -> None:
    if not isinstance(spec, TemporalResampleSpecV1):
        raise TemporalResampleError("resample spec is invalid")
    for value in (spec.compound_dataset_id, spec.compound_revision_id, spec.episode_id,
                  spec.source_stream_id, spec.target_stream_id, spec.output_stream_id, spec.computation_version):
        _text(value, "spec identity")
    if _OPAQUE_ID.fullmatch(spec.output_stream_id) is None:
        raise TemporalResampleError("output stream identity is invalid")
    if spec.computation_version != COMPUTATION_VERSION:
        raise TemporalResampleError("computation version is unsupported")
    if spec.fixed_grid is None and spec.target_view is None:
        raise TemporalResampleError("stream timestamp target DatasetView is invalid")
    if spec.fixed_grid is not None and spec.target_view is not None:
        raise TemporalResampleError("fixed grid must not bind a target DatasetView")
    for view in (spec.source_view, spec.target_view):
        if view is None:
            continue
        if not isinstance(view, DatasetViewIdentity):
            raise TemporalResampleError("DatasetView identity is invalid")
        for value in (view.dataset_id, view.revision_id, view.view_id, view.definition_sha256,
                      view.semantic_sha256):
            _text(value, "DatasetView identity")
        if _SHA256.fullmatch(view.definition_sha256) is None or _SHA256.fullmatch(view.semantic_sha256) is None:
            raise TemporalResampleError("DatasetView digest is invalid")
    if not isinstance(spec.window, ResampleWindow):
        raise TemporalResampleError("window is invalid")
    _text(spec.window.time_domain, "window time domain")
    _int64(spec.window.start_tick); _int64(spec.window.end_tick)
    if spec.window.start_tick >= spec.window.end_tick or (
            spec.window.end_tick - spec.window.start_tick > MAX_WINDOW_TICKS):
        raise TemporalResampleError("window is invalid or exceeds its cap")
    _int64(spec.tolerance_ticks)
    if spec.tolerance_ticks < 0:
        raise TemporalResampleError("tolerance must be nonnegative")
    for cap in (spec.candidate_cap, spec.output_cap):
        if type(cap) is not int or not 1 <= cap <= MAX_POINTS:
            raise TemporalResampleError("candidate and output caps are invalid")
    if not isinstance(spec.selected_fields, tuple) or not 1 <= len(spec.selected_fields) <= MAX_FIELDS:
        raise TemporalResampleError("selected fields are invalid")
    if len({item.field for item in spec.selected_fields if isinstance(item, FieldSelection)}) != len(spec.selected_fields):
        raise TemporalResampleError("selected fields must be unique")
    for item in spec.selected_fields:
        if not isinstance(item, FieldSelection):
            raise TemporalResampleError("selected field is invalid")
        _text(item.field, "selected field"); _text(item.unit, "selected unit")
    _validate_mapping(spec.mapping)
    if spec.fixed_grid is not None:
        _validate_fixed_grid_shape(spec.fixed_grid)


def _validate_fixed_grid_shape(grid: FixedGridTarget) -> None:
    if not isinstance(grid, FixedGridTarget):
        raise TemporalResampleError("fixed grid target is invalid")
    _text(grid.target_clock_id, "fixed grid target clock")
    for value in (grid.rate_numerator, grid.rate_denominator, grid.phase_tick):
        _int64(value)
    if grid.rate_numerator <= 0 or grid.rate_denominator <= 0:
        raise TemporalResampleError("fixed grid rate must be positive")


def _canonical_fixed_grid(grid: FixedGridTarget, clock) -> FixedGridTarget:
    """Normalize semantically equivalent rates and phases without float arithmetic."""
    _validate_fixed_grid_shape(grid)
    if grid.target_clock_id != clock.id:
        raise TemporalResampleError("fixed grid target clock is not the target stream clock")
    numerator_gcd = math.gcd(grid.rate_numerator, grid.rate_denominator)
    rate_numerator = grid.rate_numerator // numerator_gcd
    rate_denominator = grid.rate_denominator // numerator_gcd
    period_numerator = rate_denominator * clock.tick_unit.denominator
    period_denominator = rate_numerator * clock.tick_unit.numerator
    if period_numerator % period_denominator:
        raise TemporalResampleError("fixed grid rate does not yield an integral target-clock period")
    period = period_numerator // period_denominator
    if not 0 < period <= INT64_MAX:
        raise TemporalResampleError("fixed grid period is invalid or overflows signed-int64")
    return FixedGridTarget(
        grid.target_clock_id, rate_numerator, rate_denominator, grid.phase_tick % period, period)


def _fixed_grid_points(spec: TemporalResampleSpecV1) -> tuple[PointObservation, ...]:
    """Emit phase-aligned target-clock ticks in ``[start, end)`` without rounding."""
    grid = spec.fixed_grid
    if grid is None:
        raise TemporalResampleError("fixed grid target is unavailable")
    period = grid.period_ticks
    if type(period) is not int or not 0 < period <= INT64_MAX:
        raise TemporalResampleError("fixed grid period is unavailable")
    # ceil((start - phase) / period) stays exact for negative ticks too.  The
    # first tick is the phase-aligned grid point in the half-open window.
    first = grid.phase_tick + _ceil_div(spec.window.start_tick - grid.phase_tick, period) * period
    if first >= spec.window.end_tick:
        return ()
    _int64(first)
    count = ((spec.window.end_tick - 1 - first) // period) + 1
    if count > spec.candidate_cap or count > spec.output_cap or count > MAX_POINTS:
        raise TemporalResampleError("fixed grid exceeds candidate or output cap")
    return tuple(PointObservation(
        f"fixed-grid:{grid.target_clock_id}:{first + index * period}", first + index * period, {})
        for index in range(count))


def _ceil_div(numerator: int, denominator: int) -> int:
    return -((-numerator) // denominator)


def _validate_mapping(mapping: ClockMapping) -> None:
    if not isinstance(mapping, ClockMapping):
        raise TemporalResampleError("clock mapping is invalid")
    _text(mapping.source_clock_id, "clock id"); _text(mapping.target_clock_id, "clock id")
    for value in (mapping.scale_numerator, mapping.scale_denominator, mapping.offset_tick):
        _int64(value)
    if mapping.scale_numerator <= 0 or mapping.scale_denominator <= 0:
        raise TemporalResampleError("clock mapping scale is invalid")


def _normalize_points(points: tuple[PointObservation, ...] | list[PointObservation],
                      selected: tuple[FieldSelection, ...], role: str,
                      schema: Mapping[str, Any]) -> tuple[PointObservation, ...]:
    result: list[PointObservation] = []
    ids: set[str] = set()
    expected = {item.field for item in selected}
    value_bytes = 0
    for point in points:
        if not isinstance(point, PointObservation):
            raise TemporalResampleError(f"{role} point is invalid")
        _text(point.observation_id, f"{role} observation identity"); _int64(point.tick)
        if point.observation_id in ids:
            raise TemporalResampleError(f"duplicate {role} observation identity")
        ids.add(point.observation_id)
        if (not isinstance(point.values, Mapping) or len(point.values) > MAX_FIELDS
                or set(point.values) != expected):
            raise TemporalResampleError(f"{role} values do not match the selected fields")
        values: dict[str, object] = {}
        for key in sorted(point.values):
            value_bytes += _serialized_scalar_size(point.values[key], MAX_SELECTED_VALUE_BYTES - value_bytes)
            if value_bytes > MAX_SELECTED_VALUE_BYTES:
                raise TemporalResampleError("selected values exceed their cumulative byte cap")
            values[key] = _arrow_scalar(point.values[key], schema[key])
        result.append(PointObservation(point.observation_id, point.tick, values))
    return tuple(result)


def _row_for(target: PointObservation, source: tuple[_MappedSource, ...],
             mapped_ticks: tuple[int, ...], spec: TemporalResampleSpecV1) -> ResampleRow:
    index = bisect_left(mapped_ticks, target.tick)
    candidates = tuple(item for item in (
        source[index - 1] if index else None,
        source[index] if index < len(source) else None,
    ) if item is not None and abs(target.tick - item.mapped_tick) <= spec.tolerance_ticks)
    if not candidates:
        return ResampleRow(target.observation_id, target.tick, None, None, None, None, None,
                           tuple((item.field, None) for item in spec.selected_fields))
    chosen = min(candidates, key=lambda item: (
        abs(target.tick - item.mapped_tick), item.mapped_tick, item.point.observation_id))
    signed = target.tick - chosen.mapped_tick
    return ResampleRow(target.observation_id, target.tick, chosen.point.observation_id, chosen.point.tick,
                       chosen.mapped_tick, signed, abs(signed),
                       tuple((item.field, chosen.point.values[item.field]) for item in spec.selected_fields))
def _map_tick(mapping: ClockMapping, tick: int) -> int:
    numerator = tick * mapping.scale_numerator
    if numerator % mapping.scale_denominator:
        raise TemporalResampleError("clock mapping does not land on an integral target tick")
    mapped = numerator // mapping.scale_denominator + mapping.offset_tick
    _int64(mapped)
    return mapped


def _evidence(spec: TemporalResampleSpecV1, source: tuple[PointObservation, ...],
              target: tuple[PointObservation, ...], rows: tuple[ResampleRow, ...]) -> dict[str, object]:
    matched = tuple(item for item in rows if item.source_observation_id is not None)
    signed = [item.signed_delta_ticks for item in matched]
    absolute = [item.absolute_delta_ticks for item in matched]
    return {
        "schemaVersion": 1,
        "computationVersion": spec.computation_version,
        "spec": spec.identity_document(),
        "sourcePointsSha256": _digest(_points_document(source)),
        "targetPointsSha256": _digest(_points_document(target)),
        "sourcePointCount": len(source), "targetPointCount": len(target),
        "matchedCount": len(matched), "unmatchedTargetCount": len(rows) - len(matched),
        "gapTargetObservationIds": [item.target_observation_id for item in rows if item.source_observation_id is None],
        "signedDeltaTicks": _summary(signed), "absoluteDeltaTicks": _summary(absolute),
        "complete": True,
    }


def _spec_document(spec: TemporalResampleSpecV1) -> dict[str, object]:
    def view(value: DatasetViewIdentity) -> dict[str, object]:
        return {"datasetId": value.dataset_id, "revisionId": value.revision_id, "viewId": value.view_id,
                "definitionSha256": value.definition_sha256, "semanticSha256": value.semantic_sha256}
    mapping = spec.mapping
    document: dict[str, object] = {
        "schemaVersion": 1, "computationVersion": spec.computation_version,
        "compoundDatasetId": spec.compound_dataset_id, "compoundRevisionId": spec.compound_revision_id,
        "episodeId": spec.episode_id, "sourceStreamId": spec.source_stream_id,
        "targetStreamId": spec.target_stream_id, "outputStreamId": spec.output_stream_id,
        "sourceView": view(spec.source_view),
        "mapping": {"sourceClockId": mapping.source_clock_id, "targetClockId": mapping.target_clock_id,
                    "scaleNumerator": mapping.scale_numerator, "scaleDenominator": mapping.scale_denominator,
                    "offsetTick": mapping.offset_tick},
        "window": {"timeDomain": spec.window.time_domain, "startTick": spec.window.start_tick,
                   "endTick": spec.window.end_tick}, "toleranceTicks": spec.tolerance_ticks,
        "selectedFields": [{"field": item.field, "unit": item.unit}
                           for item in sorted(spec.selected_fields, key=lambda item: item.field)],
        "candidateCap": spec.candidate_cap, "outputCap": spec.output_cap,
        "method": "nearest", "tieBreak": "earlier-source-tick", "gapPolicy": "null-plus-evidence",
    }
    if spec.target_view is not None:
        document["targetView"] = view(spec.target_view)
    if spec.fixed_grid is not None:
        grid = spec.fixed_grid
        document["fixedGrid"] = {
            "targetClockId": grid.target_clock_id,
            "rateNumerator": grid.rate_numerator,
            "rateDenominator": grid.rate_denominator,
            "phaseTick": grid.phase_tick,
        }
    return document


def _points_document(points: tuple[PointObservation, ...]) -> list[dict[str, object]]:
    return [{"observationId": item.observation_id, "tick": item.tick,
             "values": {key: item.values[key] for key in sorted(item.values)}}
            for item in sorted(points, key=lambda item: (item.tick, item.observation_id))]


def _materialization_row(spec: TemporalResampleSpecV1, row: ResampleRow) -> dict[str, object]:
    """Map one candidate row to the fixed child member schema."""
    return {"observation_id": row.target_observation_id, "episode_id": spec.episode_id,
            "target_tick": row.target_tick, "source_observation_id": row.source_observation_id,
            "source_tick": row.source_tick, "mapped_source_tick": row.mapped_source_tick,
            "signed_delta_ticks": row.signed_delta_ticks,
            "absolute_delta_ticks": row.absolute_delta_ticks, **dict(row.values)}

def _summary(values: list[int | None]) -> dict[str, int | None]:
    present = [item for item in values if item is not None]
    return {"count": len(present), "minimum": min(present) if present else None,
            "maximum": max(present) if present else None}

def _manifest_document(parent: RevisionManifest) -> dict[str, Any]:
    def clock(value):
        return {"id": value.id, "timeDomain": value.time_domain, "tickUnit": {
            "numerator": value.tick_unit.numerator, "denominator": value.tick_unit.denominator,
            "physicalUnit": value.tick_unit.physical_unit}}
    def index(value):
        return None if value is None else {"observationIdField": value.observation_id_field,
            "episodeIdField": value.episode_id_field, "tickField": value.tick_field,
            "startTickField": value.start_tick_field, "endTickField": value.end_tick_field,
            "valueRefs": list(value.value_refs)}
    return {"version": 1, "datasetId": parent.ref.dataset_id, "revisionId": parent.ref.revision_id,
        "members": [{"id": item.id, "datasetId": item.dataset_id, "revisionId": item.revision_id,
                     "schemaDigest": item.schema_digest} for item in parent.members],
        "assets": [{"id": item.id, "mediaType": item.media_type, "byteLength": item.byte_length,
                    "sha256": item.sha256} for item in parent.assets],
        "episodes": [{"id": item.episode_id} for item in parent.episodes],
        "streams": [{"id": item.id, "kind": item.kind, "observationSchema": [
            {"name": field.name, "type": field.type, "nullable": field.nullable}
            for field in item.observation_schema], "timing": item.timing,
            "nominalRate": None if item.nominal_rate is None else {
                "numerator": item.nominal_rate.numerator, "denominator": item.nominal_rate.denominator,
                "physicalUnit": item.nominal_rate.physical_unit}, "clock": clock(item.clock),
            "units": [{"field": unit.field, "unit": unit.unit} for unit in item.units],
            "missingData": item.missing_data, "providerCoverage": item.provider_coverage,
            "transformChain": list(item.transform_chain)} for item in parent.streams],
        "bindings": [{"episodeId": item.episode_id, "streamId": item.stream_id, "state": item.state,
            "memberId": item.member_id, "assetIds": list(item.asset_ids),
            "observationIndex": index(item.observation_index)} for item in parent.bindings],
        "clockMappings": [{"sourceClockId": item.source_clock_id, "targetClockId": item.target_clock_id,
            "scaleNumerator": item.scale_numerator, "scaleDenominator": item.scale_denominator,
            "offsetTick": item.offset_tick} for item in parent.clock_mappings]}


def _output_schema(parent: RevisionManifest, spec: TemporalResampleSpecV1) -> list[dict[str, object]]:
    source = next(item for item in parent.streams if item.id == spec.source_stream_id)
    types = {item.name: item.type for item in source.observation_schema}
    fixed = [("observation_id", "string", False), ("episode_id", "string", False),
             ("target_tick", "int64", False), ("source_observation_id", "string", True),
             ("source_tick", "int64", True), ("mapped_source_tick", "int64", True),
             ("signed_delta_ticks", "int64", True), ("absolute_delta_ticks", "int64", True)]
    return ([{"name": name, "type": type_, "nullable": nullable} for name, type_, nullable in fixed]
            + [{"name": item.field, "type": types[item.field], "nullable": True}
               for item in spec.selected_fields])


def _output_stream(parent: RevisionManifest, spec: TemporalResampleSpecV1, schema: list[dict[str, object]],
                   evidence_digest: str, candidate_digest: str) -> dict[str, object]:
    target = next(item for item in parent.streams if item.id == spec.target_stream_id)
    clock = target.clock
    fixed = spec.fixed_grid
    return {"id": spec.output_stream_id, "kind": "derived-point-resample", "observationSchema": schema,
        "timing": "regular" if fixed is not None else "irregular",
        "nominalRate": None if fixed is None else {
            "numerator": fixed.rate_numerator, "denominator": fixed.rate_denominator,
            "physicalUnit": "second"}, "clock": {"id": clock.id,
        "timeDomain": clock.time_domain, "tickUnit": {"numerator": clock.tick_unit.numerator,
        "denominator": clock.tick_unit.denominator, "physicalUnit": clock.tick_unit.physical_unit}},
        "units": [{"field": item.field, "unit": item.unit} for item in spec.selected_fields],
        "missingData": "not-recorded", "providerCoverage": None,
        "transformChain": [spec.computation_version, f"evidence-sha256:{evidence_digest}",
                           f"candidate-sha256:{candidate_digest}"]}


def _output_bindings(parent: RevisionManifest, spec: TemporalResampleSpecV1, member_id: str,
                     schema: list[dict[str, object]]) -> list[dict[str, object]]:
    values = [item["name"] for item in schema if item["name"] not in {
        "observation_id", "episode_id", "target_tick"}]
    index = {"observationIdField": "observation_id", "episodeIdField": "episode_id",
             "tickField": "target_tick", "startTickField": None, "endTickField": None,
             "valueRefs": values}
    return [{"episodeId": item.episode_id, "streamId": spec.output_stream_id,
             "state": "present" if item.episode_id == spec.episode_id else "absent",
             "memberId": member_id if item.episode_id == spec.episode_id else None, "assetIds": [],
             "observationIndex": index if item.episode_id == spec.episode_id else None}
            for item in parent.episodes]


def _compound_member_schema_digest(schema: list[dict[str, object]]) -> str:
    return _digest([(item["name"], item["type"]) for item in schema])

def _manifest_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({key: value for key, value in document.items() if key != "revisionId"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()

def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or value != value.strip():
        raise TemporalResampleError(f"{label} is invalid")

def _int64(value: object) -> None:
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise TemporalResampleError("signed-int64 value is invalid")


def _serialized_scalar_size(value: object, remaining: int) -> int:
    if type(value) not in (type(None), bool, int, float, str):
        raise TemporalResampleError("selected value is not a supported scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise TemporalResampleError("selected floating value must be finite")
    if isinstance(value, str) and len(value) > remaining:
        return remaining + 1
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

def _arrow_scalar(value: object, field: Any) -> object:
    if value is None and not field.nullable:
        raise TemporalResampleError("null selected value contradicts its source schema")
    target = pa.type_for_alias(field.type)
    valid_type = (pa.types.is_boolean(target) and type(value) is bool
        or pa.types.is_integer(target) and type(value) is int
        or pa.types.is_floating(target) and type(value) in (int, float)
        or (pa.types.is_string(target) or pa.types.is_large_string(target)) and type(value) is str)
    if value is not None and not valid_type:
        raise TemporalResampleError("selected value contradicts its Arrow scalar type")
    try:
        result = pa.array([value], type=target, from_pandas=False)[0].as_py()
    except (TypeError, ValueError, OverflowError) as exc:
        raise TemporalResampleError("selected value contradicts its Arrow scalar type") from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise TemporalResampleError("selected floating value must be finite")
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
