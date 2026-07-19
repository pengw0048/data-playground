"""Strict, read-only compound dataset revision manifests.

This module deliberately owns only a bounded public identity document.  It does not
locate member bytes, materialize a provider, or infer missing data from a failed
read.  A caller may use the exact member references in :class:`RevisionManifest`
with its own resolver, but resolver state (paths, URLs, handles, and credentials)
never crosses this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from hub.models import ExactDatasetRef


MAX_MANIFEST_BYTES = 128 * 1024
_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MISSING_DATA = frozenset({"not-recorded", "not-applicable", "redacted"})
_STRING_INDEX_TYPES = frozenset({"string", "large_string"})
_INTEGER_INDEX_TYPES = frozenset({
    "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
})


class CompoundManifestError(ValueError):
    """The public immutable manifest is malformed or contradicts itself."""


@dataclass(frozen=True)
class CompoundDatasetRef:
    """One immutable compound dataset revision."""

    dataset_id: str
    revision_id: str


@dataclass(frozen=True)
class EpisodeRef:
    """An opaque episode identity, stable only inside one logical dataset."""

    dataset_id: str
    episode_id: str


@dataclass(frozen=True)
class ObservationRef:
    """Revision-bound logical observation identity, never a physical row location."""

    dataset_id: str
    revision_id: str
    episode_id: str
    stream_id: str
    observation_id: str


@dataclass(frozen=True)
class RationalUnit:
    """A positive rational count of one named physical time unit per tick."""

    numerator: int
    denominator: int
    physical_unit: Literal["second"]


@dataclass(frozen=True)
class ClockDescriptor:
    """The explicit clock domain and tick unit for one or more streams."""

    id: str
    time_domain: str
    tick_unit: RationalUnit


@dataclass(frozen=True)
class TabularMemberRef:
    """One exact tabular member; it intentionally contains no locator."""

    id: str
    dataset_id: str
    revision_id: str
    schema_digest: str


@dataclass(frozen=True)
class ImmutableAsset:
    """Opaque immutable asset facts.  Locating bytes is private resolver work."""

    id: str
    media_type: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class RationalRate:
    """A positive rational number of observations per one named physical time unit."""

    numerator: int
    denominator: int
    physical_unit: Literal["second"]


@dataclass(frozen=True)
class FieldUnit:
    """An explicit unit declaration for one observation value field."""

    field: str
    unit: str


@dataclass(frozen=True)
class ObservationIndexDescriptor:
    """The bounded schema used to read stable observations from a tabular member."""

    observation_id_field: str
    episode_id_field: str
    tick_field: str | None
    start_tick_field: str | None
    end_tick_field: str | None
    value_refs: tuple[str, ...]


@dataclass(frozen=True)
class ObservationSchemaField:
    """One ordered, immutable observation-index schema field."""

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class StreamDescriptor:
    """One generic stream definition, independent of a transport or domain ontology."""

    id: str
    kind: str
    observation_schema: tuple[ObservationSchemaField, ...]
    timing: Literal["regular", "irregular"]
    nominal_rate: RationalRate | None
    clock: ClockDescriptor
    units: tuple[FieldUnit, ...]
    missing_data: str
    provider_coverage: str | None
    transform_chain: tuple[str, ...]


@dataclass(frozen=True)
class EpisodeStreamBinding:
    """An explicit present/absent fact for exactly one episode and stream pair."""

    episode_id: str
    stream_id: str
    state: Literal["present", "absent"]
    member_id: str | None
    asset_ids: tuple[str, ...]
    observation_index: ObservationIndexDescriptor | None


@dataclass(frozen=True)
class ClockMapping:
    """One direct affine mapping between named clocks, with no rounding permission."""

    source_clock_id: str
    target_clock_id: str
    scale_numerator: int
    scale_denominator: int
    offset_tick: int


@dataclass(frozen=True)
class RevisionManifest:
    """Validated immutable public contract for one compound revision."""

    ref: CompoundDatasetRef
    members: tuple[TabularMemberRef, ...]
    assets: tuple[ImmutableAsset, ...]
    episodes: tuple[EpisodeRef, ...]
    streams: tuple[StreamDescriptor, ...]
    bindings: tuple[EpisodeStreamBinding, ...]
    clock_mappings: tuple[ClockMapping, ...]
    digest: str


def open_compound_manifest(payload: bytes | bytearray | memoryview, *,
                           max_bytes: int = MAX_MANIFEST_BYTES) -> RevisionManifest:
    """Decode, validate and canonicalize one bounded JSON manifest before any member read.

    ``payload`` is deliberately bytes rather than an already-parsed mapping: the size and duplicate
    key checks must run before a JSON document could be trusted.  Runtime access failures are not
    represented here; they are distinct from a declared ``absent`` binding.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise CompoundManifestError("compound manifest limit is invalid")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise CompoundManifestError("compound manifest must be JSON bytes")
    if max_bytes > MAX_MANIFEST_BYTES:
        raise CompoundManifestError("compound manifest limit exceeds the public hard cap")
    payload_size = payload.nbytes if isinstance(payload, memoryview) else len(payload)
    if payload_size > max_bytes:
        raise CompoundManifestError("compound manifest exceeds its byte limit")
    raw = bytes(payload)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except (RecursionError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise CompoundManifestError("compound manifest is not valid JSON") from exc
    _bounded_json(document)
    try:
        return _decode_manifest(document)
    except CompoundManifestError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CompoundManifestError("compound manifest is invalid") from exc


# A descriptive synonym makes the intended public reader easy to discover without introducing a
# separate, divergent decoder path.
decode_compound_manifest = open_compound_manifest


def map_tick(mapping: ClockMapping, tick: int) -> int:
    """Map one tick only when the affine result is an exact target tick."""
    _integer(tick)
    if (type(mapping.scale_numerator) is not int or type(mapping.scale_denominator) is not int
            or mapping.scale_numerator <= 0 or mapping.scale_denominator <= 0
            or mapping.scale_numerator > _MAX_SAFE_INTEGER
            or mapping.scale_denominator > _MAX_SAFE_INTEGER
            or type(mapping.offset_tick) is not int or abs(mapping.offset_tick) > _MAX_SAFE_INTEGER):
        raise CompoundManifestError("clock mapping is invalid")
    numerator = tick * mapping.scale_numerator
    if numerator % mapping.scale_denominator:
        raise CompoundManifestError("clock mapping does not land on an integral target tick")
    target_tick = numerator // mapping.scale_denominator + mapping.offset_tick
    if abs(target_tick) > _MAX_SAFE_INTEGER:
        raise CompoundManifestError("clock mapping target tick is invalid")
    return target_tick


def _decode_manifest(document: object) -> RevisionManifest:
    doc = _keys(document, {
        "version", "datasetId", "revisionId", "members", "assets", "episodes", "streams",
        "bindings", "clockMappings",
    })
    if doc["version"] != _SCHEMA_VERSION or type(doc["version"]) is not int:
        raise CompoundManifestError("compound manifest version is unsupported")
    dataset_id = _opaque_id(doc["datasetId"])
    revision_id = _digest(doc["revisionId"])
    members = tuple(_decode_member(item) for item in _list(doc["members"]))
    assets = tuple(_decode_asset(item) for item in _list(doc["assets"]))
    episodes = tuple(EpisodeRef(dataset_id, _opaque_id(_keys(item, {"id"})["id"]))
                     for item in _list(doc["episodes"]))
    streams = tuple(_decode_stream(item) for item in _list(doc["streams"]))
    bindings = tuple(_decode_binding(item) for item in _list(doc["bindings"]))
    mappings = tuple(_decode_mapping(item) for item in _list(doc["clockMappings"]))
    _unique((member.id for member in members), "member")
    _unique((asset.id for asset in assets), "asset")
    _unique((episode.episode_id for episode in episodes), "episode")
    _unique((stream.id for stream in streams), "stream")
    _validate_cross_references(members, assets, episodes, streams, bindings, mappings)
    members = tuple(sorted(members, key=lambda item: item.id))
    assets = tuple(sorted(assets, key=lambda item: item.id))
    episodes = tuple(sorted(episodes, key=lambda item: item.episode_id))
    streams = tuple(sorted(streams, key=lambda item: item.id))
    bindings = tuple(sorted(bindings, key=lambda item: (item.episode_id, item.stream_id)))
    mappings = tuple(sorted(mappings, key=_mapping_sort_key))
    canonical = _canonical_document(
        dataset_id, members, assets, episodes, streams, bindings, mappings)
    digest = hashlib.sha256(_json_bytes(canonical)).hexdigest()
    if revision_id != digest:
        raise CompoundManifestError("compound manifest revision does not match its canonical digest")
    return RevisionManifest(
        ref=CompoundDatasetRef(dataset_id, revision_id), members=members, assets=assets,
        episodes=episodes, streams=streams, bindings=bindings, clock_mappings=mappings,
        digest=digest)


def _decode_member(value: object) -> TabularMemberRef:
    doc = _keys(value, {"id", "datasetId", "revisionId", "schemaDigest"})
    dataset_id, revision_id = _exact_dataset_identity(doc["datasetId"], doc["revisionId"])
    return TabularMemberRef(
        _opaque_id(doc["id"]), dataset_id, revision_id, _digest(doc["schemaDigest"]))


def _decode_asset(value: object) -> ImmutableAsset:
    doc = _keys(value, {"id", "mediaType", "byteLength", "sha256"})
    media_type = doc["mediaType"]
    if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
        raise CompoundManifestError("compound asset media type is invalid")
    length = _integer(doc["byteLength"])
    if length < 0:
        raise CompoundManifestError("compound asset byte length is invalid")
    return ImmutableAsset(_opaque_id(doc["id"]), media_type, length, _digest(doc["sha256"]))


def _decode_stream(value: object) -> StreamDescriptor:
    doc = _keys(value, {
        "id", "kind", "observationSchema", "timing", "nominalRate", "clock", "units",
        "missingData", "providerCoverage", "transformChain",
    })
    kind = _opaque_id(doc["kind"])
    schema = _schema_fields(doc["observationSchema"])
    timing = doc["timing"]
    if timing not in ("regular", "irregular"):
        raise CompoundManifestError("compound stream timing is invalid")
    rate = _decode_rate(doc["nominalRate"]) if doc["nominalRate"] is not None else None
    if (timing == "regular") != (rate is not None):
        raise CompoundManifestError("compound stream timing and nominal rate disagree")
    coverage = doc["providerCoverage"]
    if coverage is not None:
        coverage = _opaque_id(coverage)
    units = tuple(sorted((_decode_field_unit(item) for item in _list(doc["units"])),
                         key=lambda item: item.field))
    _unique((item.field for item in units), "unit field")
    transforms = tuple(_opaque_id(item) for item in _list(doc["transformChain"]))
    missing = doc["missingData"]
    if missing not in _MISSING_DATA:
        raise CompoundManifestError("compound missing-data semantics are invalid")
    return StreamDescriptor(
        id=_opaque_id(doc["id"]), kind=kind, observation_schema=schema,
        timing=timing, nominal_rate=rate, clock=_decode_clock(doc["clock"]), units=units,
        missing_data=missing, provider_coverage=coverage, transform_chain=transforms)


def _decode_clock(value: object) -> ClockDescriptor:
    doc = _keys(value, {"id", "timeDomain", "tickUnit"})
    return ClockDescriptor(_opaque_id(doc["id"]), _opaque_id(doc["timeDomain"]),
                           _decode_unit(doc["tickUnit"]))


def _decode_unit(value: object) -> RationalUnit:
    doc = _keys(value, {"numerator", "denominator", "physicalUnit"})
    numerator, denominator = _integer(doc["numerator"]), _integer(doc["denominator"])
    if numerator <= 0 or denominator <= 0:
        raise CompoundManifestError("compound rational unit is invalid")
    if doc["physicalUnit"] != "second":
        raise CompoundManifestError("compound physical unit is ambiguous")
    return RationalUnit(numerator, denominator, "second")


def _decode_rate(value: object) -> RationalRate:
    doc = _keys(value, {"numerator", "denominator", "physicalUnit"})
    numerator, denominator = _integer(doc["numerator"]), _integer(doc["denominator"])
    if numerator <= 0 or denominator <= 0:
        raise CompoundManifestError("compound nominal rate is invalid")
    if doc["physicalUnit"] != "second":
        raise CompoundManifestError("compound nominal rate unit is ambiguous")
    return RationalRate(numerator, denominator, "second")


def _decode_field_unit(value: object) -> FieldUnit:
    doc = _keys(value, {"field", "unit"})
    unit = doc["unit"]
    if (not isinstance(unit, str) or not unit or len(unit) > 128 or unit != unit.strip()
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in unit)):
        raise CompoundManifestError("compound field unit is invalid")
    return FieldUnit(_field_name(doc["field"]), unit)


def _schema_fields(value: object) -> tuple[ObservationSchemaField, ...]:
    fields = _list(value)
    if not fields:
        raise CompoundManifestError("compound observation schema is empty")
    result: list[ObservationSchemaField] = []
    names: list[str] = []
    for field in fields:
        doc = _keys(field, {"name", "type", "nullable"})
        name = _field_name(doc["name"])
        field_type = _schema_type(doc["type"])
        if type(doc["nullable"]) is not bool:
            raise CompoundManifestError("compound schema field is invalid")
        names.append(name)
        result.append(ObservationSchemaField(name, field_type, doc["nullable"]))
    _unique(names, "schema field")
    return tuple(result)


def _decode_binding(value: object) -> EpisodeStreamBinding:
    doc = _keys(value, {"episodeId", "streamId", "state", "memberId", "assetIds", "observationIndex"})
    state = doc["state"]
    if state not in ("present", "absent"):
        raise CompoundManifestError("compound stream state is invalid")
    member_id = None if doc["memberId"] is None else _opaque_id(doc["memberId"])
    asset_ids = tuple(sorted(_opaque_id(item) for item in _list(doc["assetIds"])))
    _unique(asset_ids, "binding asset")
    index = None if doc["observationIndex"] is None else _decode_index(doc["observationIndex"])
    if state == "present" and (member_id is None or index is None):
        raise CompoundManifestError("present compound stream lacks an immutable tabular index")
    if state == "absent" and (member_id is not None or asset_ids or index is not None):
        raise CompoundManifestError("absent compound stream has materialized bindings")
    return EpisodeStreamBinding(_opaque_id(doc["episodeId"]), _opaque_id(doc["streamId"]),
                                state, member_id, asset_ids, index)


def _decode_index(value: object) -> ObservationIndexDescriptor:
    doc = _keys(value, {
        "observationIdField", "episodeIdField", "tickField", "startTickField", "endTickField",
        "valueRefs",
    })
    tick = None if doc["tickField"] is None else _field_name(doc["tickField"])
    start = None if doc["startTickField"] is None else _field_name(doc["startTickField"])
    end = None if doc["endTickField"] is None else _field_name(doc["endTickField"])
    if (tick is None) == (start is None or end is None):
        raise CompoundManifestError("compound observation index has ambiguous time fields")
    ids = _field_name(doc["observationIdField"]), _field_name(doc["episodeIdField"])
    values = tuple(_field_name(item) for item in _list(doc["valueRefs"]))
    if len(set((*ids, *(item for item in (tick, start, end) if item is not None), *values))) != (
            2 + (1 if tick is not None else 2) + len(values)):
        raise CompoundManifestError("compound observation index repeats a field")
    return ObservationIndexDescriptor(ids[0], ids[1], tick, start, end, values)


def _decode_mapping(value: object) -> ClockMapping:
    doc = _keys(value, {
        "sourceClockId", "targetClockId", "scaleNumerator", "scaleDenominator", "offsetTick",
    })
    numerator, denominator = _integer(doc["scaleNumerator"]), _integer(doc["scaleDenominator"])
    if numerator <= 0 or denominator <= 0:
        raise CompoundManifestError("compound clock mapping scale is invalid")
    return ClockMapping(_opaque_id(doc["sourceClockId"]), _opaque_id(doc["targetClockId"]),
                        numerator, denominator, _integer(doc["offsetTick"]))


def _validate_cross_references(
        members: tuple[TabularMemberRef, ...], assets: tuple[ImmutableAsset, ...],
        episodes: tuple[EpisodeRef, ...], streams: tuple[StreamDescriptor, ...],
        bindings: tuple[EpisodeStreamBinding, ...], mappings: tuple[ClockMapping, ...]) -> None:
    member_ids, asset_ids = {item.id for item in members}, {item.id for item in assets}
    episode_ids, stream_ids = {item.episode_id for item in episodes}, {item.id for item in streams}
    if not episodes or not streams:
        raise CompoundManifestError("compound manifest requires episodes and streams")
    expected = {(episode_id, stream_id) for episode_id in episode_ids for stream_id in stream_ids}
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        key = (binding.episode_id, binding.stream_id)
        if key not in expected or key in seen:
            raise CompoundManifestError("compound episode stream bindings are incomplete or duplicate")
        seen.add(key)
        if binding.member_id is not None and binding.member_id not in member_ids:
            raise CompoundManifestError("compound binding has a dangling member")
        if any(asset_id not in asset_ids for asset_id in binding.asset_ids):
            raise CompoundManifestError("compound binding has a dangling asset")
        if binding.observation_index is not None:
            stream = next(stream for stream in streams if stream.id == binding.stream_id)
            schema_by_name = {field.name: field for field in stream.observation_schema}
            schema_fields = set(schema_by_name)
            descriptor_fields = {
                binding.observation_index.observation_id_field,
                binding.observation_index.episode_id_field,
                *binding.observation_index.value_refs,
            }
            descriptor_fields.update(field for field in (
                binding.observation_index.tick_field, binding.observation_index.start_tick_field,
                binding.observation_index.end_tick_field) if field is not None)
            if not descriptor_fields <= schema_fields:
                raise CompoundManifestError("compound observation index is not in the stream schema")
            _validate_index_roles(binding.observation_index, schema_by_name)
            role_fields = {
                binding.observation_index.observation_id_field,
                binding.observation_index.episode_id_field,
                *(field for field in (
                    binding.observation_index.tick_field,
                    binding.observation_index.start_tick_field,
                    binding.observation_index.end_tick_field) if field is not None),
            }
            if set(binding.observation_index.value_refs) != schema_fields - role_fields:
                raise CompoundManifestError("compound observation value references are ambiguous")
            if not {unit.field for unit in stream.units} <= set(
                    binding.observation_index.value_refs):
                raise CompoundManifestError("compound field unit is not a stream value")
    if seen != expected:
        raise CompoundManifestError("compound episode stream bindings are incomplete or duplicate")
    clocks: dict[str, ClockDescriptor] = {}
    for stream in streams:
        schema_fields = {field.name for field in stream.observation_schema}
        if any(unit.field not in schema_fields for unit in stream.units):
            raise CompoundManifestError("compound field unit has a dangling schema field")
        existing = clocks.setdefault(stream.clock.id, stream.clock)
        if existing != stream.clock:
            raise CompoundManifestError("compound clock descriptors disagree")
    mapping_pairs: set[tuple[str, str]] = set()
    for mapping in mappings:
        pair = mapping.source_clock_id, mapping.target_clock_id
        if (mapping.source_clock_id not in clocks or mapping.target_clock_id not in clocks
                or mapping.source_clock_id == mapping.target_clock_id or pair in mapping_pairs):
            raise CompoundManifestError("compound clock mapping is dangling or ambiguous")
        mapping_pairs.add(pair)


def _canonical_document(dataset_id: str, members: tuple[TabularMemberRef, ...],
                        assets: tuple[ImmutableAsset, ...], episodes: tuple[EpisodeRef, ...],
                        streams: tuple[StreamDescriptor, ...], bindings: tuple[EpisodeStreamBinding, ...],
                        mappings: tuple[ClockMapping, ...]) -> dict[str, Any]:
    """Build the identity document.  Only explicitly unordered collections are sorted."""
    def unit(value: RationalUnit | RationalRate | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"numerator": value.numerator, "denominator": value.denominator,
                "physicalUnit": value.physical_unit}
    def clock(value: ClockDescriptor) -> dict[str, Any]:
        return {"id": value.id, "timeDomain": value.time_domain, "tickUnit": unit(value.tick_unit)}
    def index(value: ObservationIndexDescriptor | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"observationIdField": value.observation_id_field,
                "episodeIdField": value.episode_id_field, "tickField": value.tick_field,
                "startTickField": value.start_tick_field, "endTickField": value.end_tick_field,
                "valueRefs": list(value.value_refs)}
    return {
        "version": _SCHEMA_VERSION, "datasetId": dataset_id,
        "members": [{"id": item.id, "datasetId": item.dataset_id,
                     "revisionId": item.revision_id, "schemaDigest": item.schema_digest}
                    for item in sorted(members, key=lambda item: item.id)],
        "assets": [{"id": item.id, "mediaType": item.media_type, "byteLength": item.byte_length,
                    "sha256": item.sha256} for item in sorted(assets, key=lambda item: item.id)],
        "episodes": [{"id": item.episode_id} for item in sorted(episodes, key=lambda item: item.episode_id)],
        "streams": [{"id": item.id, "kind": item.kind, "observationSchema": [
                        {"name": field.name, "type": field.type, "nullable": field.nullable}
                        for field in item.observation_schema],
                     "timing": item.timing, "nominalRate": unit(item.nominal_rate),
                     "clock": clock(item.clock), "units": [
                         {"field": value.field, "unit": value.unit} for value in item.units],
                     "missingData": item.missing_data, "providerCoverage": item.provider_coverage,
                     "transformChain": list(item.transform_chain)}
                    for item in sorted(streams, key=lambda item: item.id)],
        "bindings": [{"episodeId": item.episode_id, "streamId": item.stream_id, "state": item.state,
                      "memberId": item.member_id, "assetIds": sorted(item.asset_ids),
                      "observationIndex": index(item.observation_index)}
                     for item in sorted(bindings, key=lambda item: (item.episode_id, item.stream_id))],
        "clockMappings": [{"sourceClockId": item.source_clock_id, "targetClockId": item.target_clock_id,
                           "scaleNumerator": item.scale_numerator,
                           "scaleDenominator": item.scale_denominator, "offsetTick": item.offset_tick}
                          for item in sorted(mappings, key=_mapping_sort_key)],
    }


def _mapping_sort_key(mapping: ClockMapping) -> tuple[str, str, int, int, int]:
    return (mapping.source_clock_id, mapping.target_clock_id, mapping.scale_numerator,
            mapping.scale_denominator, mapping.offset_tick)


def _validate_index_roles(index: ObservationIndexDescriptor,
                          schema: dict[str, ObservationSchemaField]) -> None:
    id_fields = (schema[index.observation_id_field], schema[index.episode_id_field])
    if any(field.nullable or field.type not in _STRING_INDEX_TYPES for field in id_fields):
        raise CompoundManifestError("compound observation identity fields are invalid")
    time_names = ((index.tick_field,) if index.tick_field is not None
                  else (index.start_tick_field, index.end_tick_field))
    time_fields = tuple(schema[name] for name in time_names if name is not None)
    if (any(field.nullable or field.type not in _INTEGER_INDEX_TYPES for field in time_fields)
            or len({field.type for field in time_fields}) != 1):
        raise CompoundManifestError("compound observation time fields are invalid")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(pairs) != len({key for key, _ in pairs}):
        raise ValueError("duplicate object key")
    return dict(pairs)


def _bounded_json(value: object, depth: int = 0) -> None:
    if depth > 16:
        raise CompoundManifestError("compound manifest is too deeply nested")
    if value is None or type(value) in (str, bool):
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            raise CompoundManifestError("compound manifest integer is unsafe")
        return
    if type(value) is float:
        raise CompoundManifestError("compound manifest cannot use floating point")
    if isinstance(value, list):
        if len(value) > 4096:
            raise CompoundManifestError("compound manifest collection is oversized")
        for item in value:
            _bounded_json(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 128:
            raise CompoundManifestError("compound manifest object is oversized")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise CompoundManifestError("compound manifest key is invalid")
            _bounded_json(item, depth + 1)
        return
    raise CompoundManifestError("compound manifest contains a non-JSON value")


def _keys(value: object, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CompoundManifestError("compound manifest object shape is invalid")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise CompoundManifestError("compound manifest collection is invalid")
    return value


def _opaque_id(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise CompoundManifestError("compound opaque identifier is invalid")
    return value


def _field_name(value: object) -> str:
    if not isinstance(value, str) or _FIELD_NAME.fullmatch(value) is None:
        raise CompoundManifestError("compound field name is invalid")
    return value


def _schema_type(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256 or value != value.strip()
            or any(ord(char) < 0x20 or ord(char) > 0x7e for char in value)
            or "  " in value):
        raise CompoundManifestError("compound schema type is invalid")
    return value


def _exact_dataset_identity(dataset_id: object, revision_id: object) -> tuple[str, str]:
    try:
        ref = ExactDatasetRef.model_validate({
            "kind": "exact", "datasetId": dataset_id, "revisionId": revision_id,
        })
    except (TypeError, ValueError) as exc:
        raise CompoundManifestError("compound exact member identity is invalid") from exc
    return ref.dataset_id, ref.revision_id


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CompoundManifestError("compound checksum is invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or abs(value) > _MAX_SAFE_INTEGER:
        raise CompoundManifestError("compound integer is invalid")
    return value


def _unique(values: Any, label: str) -> None:
    values = list(values)
    if len(values) != len(set(values)):
        raise CompoundManifestError(f"compound {label} identifiers are duplicate")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
