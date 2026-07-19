"""Bounded, revision-bound temporal evidence for compound datasets.

This is deliberately a read-only calculation.  It consumes a #439 manifest and a
server-owned observation reader; neither paths nor provider handles are part of
the public evidence contract.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from bisect import bisect_left, bisect_right
from typing import Protocol

from hub.compound_datasets import (
    ClockMapping, CompoundManifestError, EpisodeStreamBinding, RevisionManifest,
    StreamDescriptor, map_tick,
)

COMPUTATION_VERSION = "temporal-evidence-v1"
MAX_STREAMS = 8
MAX_OBSERVATIONS_PER_STREAM = 10_000
MAX_INTERVALS_PER_STREAM = 10_000
MAX_PAIR_COMPARISONS = 20_000
MAX_WINDOW_TICKS = 1_000_000_000_000
MAX_TOLERANCE_TICKS = (1 << 63) - 1


class TemporalEvidenceError(ValueError):
    """A request is not safely bounded or contradicts its exact manifest."""


class ObservationReader(Protocol):
    """Private authority which may read only an exact server-owned member."""

    def read(
        self, *, dataset_id: str, revision_id: str, fields: tuple[str, ...],
        stream_id: str, episode_id: str, episode_id_field: str, tick_field: str | None, start_tick_field: str | None,
        end_tick_field: str | None, source_start: int, source_end: int, limit: int,
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class EvidenceWindow:
    time_domain: str
    start_tick: int
    end_tick: int


@dataclass(frozen=True)
class EvidenceRequest:
    episode_id: str
    stream_ids: tuple[str, ...]
    window: EvidenceWindow
    gap_threshold_ticks: int
    pair: tuple[str, str] | None = None
    tolerance_ticks: int = 0
    view_identities: tuple[tuple[str, str, str, str], ...] = ()


def compute_temporal_evidence(
    manifest: RevisionManifest, request: EvidenceRequest, reader: ObservationReader,
) -> dict[str, object]:
    """Compute one deterministic, explicitly bounded evidence record.

    The reader is told both an exact identity and a cap before it can touch a
    relation.  A cap lookahead turns a too-large result into ``truncated`` rather
    than a potentially misleading complete result.
    """
    _validate_request(manifest, request)
    request = replace(request, stream_ids=tuple(sorted(request.stream_ids)))
    streams = {item.id: item for item in manifest.streams}
    bindings = {(item.episode_id, item.stream_id): item for item in manifest.bindings}
    items: dict[str, dict[str, object]] = {}
    for stream_id in request.stream_ids:
        items[stream_id] = _stream_evidence(
            manifest, streams[stream_id], bindings[(request.episode_id, stream_id)], request, reader)
    pair_evidence = _pair_evidence(items, request)
    complete = all(item["complete"] is True for item in items.values())
    if pair_evidence is not None:
        complete = complete and pair_evidence["complete"] is True
    identity_payload = {
        "computationVersion": COMPUTATION_VERSION, "compoundDatasetId": manifest.ref.dataset_id,
        "compoundRevision": manifest.digest,
        "episodeId": request.episode_id, "streams": list(request.stream_ids),
        "window": {"timeDomain": request.window.time_domain, "startTick": request.window.start_tick,
                   "endTick": request.window.end_tick}, "pair": request.pair,
        "toleranceTicks": request.tolerance_ticks,
        "gapThresholdTicks": request.gap_threshold_ticks,
        "members": [
            {"streamId": stream_id, "datasetId": next(member.dataset_id for member in manifest.members
             if binding.member_id == member.id), "revisionId": next(member.revision_id for member in manifest.members
             if binding.member_id == member.id)}
            for stream_id in request.stream_ids
            if (binding := bindings[(request.episode_id, stream_id)]).member_id is not None
        ],
        "datasetViews": [
            {"streamId": stream_id, "viewId": view_id, "definitionSha256": definition_sha,
             "semanticSha256": semantic_sha}
            for stream_id, view_id, definition_sha, semantic_sha in request.view_identities
        ],
    }
    return {
        "schemaVersion": 1,
        "identity": {**identity_payload, "evidenceId": _digest(identity_payload)},
        "complete": complete,
        "approximation": {
            "pointCoverage": "point streams use first-to-last hulls; gaps are reported separately",
            "pairwise": "one selected pair only" if request.pair else "no pair selected",
        },
        "streams": [_public_stream(items[item]) for item in request.stream_ids],
        "pair": pair_evidence,
    }


def _validate_request(manifest: RevisionManifest, request: EvidenceRequest) -> None:
    stream_ids = request.stream_ids
    if not stream_ids or len(stream_ids) > MAX_STREAMS or len(set(stream_ids)) != len(stream_ids):
        raise TemporalEvidenceError("stream set must be unique and within the stream cap")
    if request.episode_id not in {item.episode_id for item in manifest.episodes}:
        raise TemporalEvidenceError("episode is not in the exact compound revision")
    if any(item not in {stream.id for stream in manifest.streams} for item in stream_ids):
        raise TemporalEvidenceError("stream is not in the exact compound revision")
    if request.window.start_tick >= request.window.end_tick:
        raise TemporalEvidenceError("window must be half-open with positive length")
    if request.window.end_tick - request.window.start_tick > MAX_WINDOW_TICKS:
        raise TemporalEvidenceError("window exceeds the temporal evidence cap")
    if request.tolerance_ticks < 0 or request.tolerance_ticks > MAX_TOLERANCE_TICKS:
        raise TemporalEvidenceError("tolerance exceeds the temporal evidence cap")
    if request.gap_threshold_ticks < 1 or request.gap_threshold_ticks > MAX_WINDOW_TICKS:
        raise TemporalEvidenceError("gap threshold exceeds the temporal evidence cap")
    if request.pair is not None:
        if len(request.pair) != 2 or request.pair[0] == request.pair[1] or any(
                item not in stream_ids for item in request.pair):
            raise TemporalEvidenceError("selected pair must name two requested distinct streams")


def _mapping(manifest: RevisionManifest, source: str, target: str) -> ClockMapping | None:
    if source == target:
        return None
    matches = [item for item in manifest.clock_mappings
               if item.source_clock_id == source and item.target_clock_id == target]
    return matches[0] if len(matches) == 1 else None


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _source_bounds(mapping: ClockMapping | None, window: EvidenceWindow) -> tuple[int, int]:
    if mapping is None:
        return window.start_tick, window.end_tick
    return (
        _ceil_div((window.start_tick - mapping.offset_tick) * mapping.scale_denominator,
                  mapping.scale_numerator),
        _ceil_div((window.end_tick - mapping.offset_tick) * mapping.scale_denominator,
                  mapping.scale_numerator),
    )


def _stream_evidence(
    manifest: RevisionManifest, stream: StreamDescriptor, binding: EpisodeStreamBinding,
    request: EvidenceRequest, reader: ObservationReader,
) -> dict[str, object]:
    base = {"streamId": stream.id, "providerCoverage": stream.provider_coverage,
            "nominalRate": (_rate(stream) if stream.nominal_rate else None),
            "corruptCount": 0, "observedCount": 0, "boundedReadCount": 0,
            "coverageIntervals": [], "gaps": [], "firstTick": None, "lastTick": None,
            "measuredRate": None, "clockMapping": None, "complete": False,
            "gapThresholdTicks": request.gap_threshold_ticks,
            "_point": binding.observation_index is not None and binding.observation_index.tick_field is not None}
    if binding.state == "absent":
        return {**base, "state": "absent", "complete": True}
    mapping = _mapping(manifest, stream.clock.id, request.window.time_domain)
    if stream.clock.id != request.window.time_domain and mapping is None:
        return {**base, "state": "unknown", "reason": "no named clock mapping"}
    if mapping is not None:
        base["clockMapping"] = _mapping_identity(mapping)
    assert binding.member_id is not None and binding.observation_index is not None
    member = next(item for item in manifest.members if item.id == binding.member_id)
    index = binding.observation_index
    fields = tuple(dict.fromkeys((index.observation_id_field, index.episode_id_field,
                                  *(item for item in (index.tick_field, index.start_tick_field,
                                                    index.end_tick_field) if item is not None))))
    source_start, source_end = _source_bounds(mapping, request.window)
    try:
        rows = reader.read(dataset_id=member.dataset_id, revision_id=member.revision_id,
                           fields=fields, stream_id=stream.id, episode_id=request.episode_id,
                           episode_id_field=index.episode_id_field,
                           tick_field=index.tick_field,
                           start_tick_field=index.start_tick_field, end_tick_field=index.end_tick_field,
                           source_start=source_start, source_end=source_end,
                           limit=MAX_OBSERVATIONS_PER_STREAM + 1)
    except PermissionError:
        return {**base, "state": "permission", "reason": "exact member permission lost"}
    except ConnectionError:
        return {**base, "state": "unavailable", "reason": "exact member unavailable"}
    except Exception:
        return {**base, "state": "corrupt", "reason": "exact member read failed"}
    if len(rows) > MAX_OBSERVATIONS_PER_STREAM:
        return {**base, "state": "truncated", "boundedReadCount": len(rows),
                "reason": "observation cap reached"}
    facts, corrupt = _normalize_rows(rows, index.observation_id_field, index.tick_field,
                                     index.start_tick_field, index.end_tick_field, mapping,
                                     request.window)
    base["corruptCount"] = corrupt
    base["boundedReadCount"] = len(rows)
    base["observedCount"] = len(facts)
    if len(facts) > MAX_INTERVALS_PER_STREAM:
        return {**base, "state": "truncated", "reason": "interval cap reached"}
    intervals, gaps = _coverage(facts, request.gap_threshold_ticks)
    base["coverageIntervals"], base["gaps"] = intervals, gaps
    if facts:
        base["firstTick"], base["lastTick"] = facts[0][1], max(item[2] for item in facts)
        base["measuredRate"] = _measured_rate(facts)
    return {**base, "state": "corrupt" if corrupt else "available", "complete": corrupt == 0,
            "_facts": facts}


def _normalize_rows(rows, id_field, tick_field, start_field, end_field, mapping, window):
    facts: list[tuple[str, int, int]] = []
    corrupt = 0
    identifiers: dict[str, int] = {}
    for row in rows:
        observation_id = row.get(id_field)
        if isinstance(observation_id, str) and observation_id:
            identifiers[observation_id] = identifiers.get(observation_id, 0) + 1
    for row in rows:
        try:
            observation_id = row[id_field]
            if (not isinstance(observation_id, str) or not observation_id
                    or identifiers.get(observation_id) != 1):
                raise ValueError
            if tick_field is not None:
                tick = row[tick_field]
                if type(tick) is not int: raise ValueError
                start = end = map_tick(mapping, tick) if mapping else tick
            else:
                raw_start, raw_end = row[start_field], row[end_field]
                if type(raw_start) is not int or type(raw_end) is not int or raw_start >= raw_end: raise ValueError
                start = map_tick(mapping, raw_start) if mapping else raw_start
                end = map_tick(mapping, raw_end) if mapping else raw_end
                if start >= end: raise ValueError
            if (start == end and not (window.start_tick <= start < window.end_tick)
                    or start != end and (end <= window.start_tick or start >= window.end_tick)):
                continue
            if start != end:
                start, end = max(start, window.start_tick), min(end, window.end_tick)
            facts.append((observation_id, start, end))
        except (KeyError, TypeError, ValueError, CompoundManifestError):
            corrupt += 1
    facts.sort(key=lambda item: (item[1], item[2], item[0]))
    return facts, corrupt


def _coverage(facts, gap_threshold):
    if not facts: return [], []
    intervals: list[tuple[int, int]] = []
    gaps: list[dict[str, object]] = []
    start, end, end_owner = facts[0][1], facts[0][2], facts[0][0]
    for observation_id, item_start, item_end in facts[1:]:
        gap = item_start - end
        if gap > 0 and gap >= gap_threshold:
            gaps.append({"afterObservationId": end_owner, "beforeObservationId": observation_id,
                         "durationTicks": gap, "thresholdTicks": gap_threshold})
            intervals.append((start, end))
            start, end, end_owner = item_start, item_end, observation_id
        else:
            candidate_end = max(item_end, item_start)
            if candidate_end > end:
                end, end_owner = candidate_end, observation_id
    intervals.append((start, end))
    return intervals, gaps


def _pair_evidence(items, request):
    if request.pair is None: return None
    left, right = (items[request.pair[0]], items[request.pair[1]])
    if (left.get("state") != "available" or right.get("state") != "available"
            or left.get("complete") is not True or right.get("complete") is not True):
        return {"state": "unknown", "complete": False, "reason": "pair member is incomplete",
                "unknownCount": None}
    if "_facts" not in left or "_facts" not in right:
        return {"state": "unknown", "complete": False, "reason": "pair member is not fully readable",
                "unknownCount": None}
    left_facts, right_facts = left.pop("_facts"), right.pop("_facts")
    # Point observations are instantaneous.  Their between-sample hull is useful
    # bounded evidence, but is explicitly marked as an approximation rather than
    # pretending that samples form aligned rows.
    if left["_point"] or right["_point"]:
        overlap, overlap_comparisons = _overlap(_hull(left), _hull(right), MAX_PAIR_COMPARISONS)
        overlap_approximation = "point-stream observed hull"
    else:
        overlap, overlap_comparisons = _overlap(
            left["coverageIntervals"], right["coverageIntervals"], MAX_PAIR_COMPARISONS)
        overlap_approximation = None
    if overlap is None:
        return {"state": "truncated", "complete": False, "reason": "pair comparison cap reached"}
    nearest = _nearest(left_facts, right_facts, request.tolerance_ticks,
                       MAX_PAIR_COMPARISONS - overlap_comparisons)
    if nearest is None:
        return {"state": "truncated", "complete": False, "reason": "pair comparison cap reached"}
    matched, unmatched_left, unmatched_right, deltas, nearest_comparisons = nearest
    return {"state": "available", "complete": left["complete"] and right["complete"],
            "leftStreamId": request.pair[0], "rightStreamId": request.pair[1],
            "overlapTicks": overlap, "overlapApproximation": overlap_approximation,
            "toleranceTicks": request.tolerance_ticks,
            "matchedCount": matched, "unmatchedLeftCount": unmatched_left,
            "unmatchedRightCount": unmatched_right,
            "unknownCount": 0, "nearestDelta": deltas}


def _overlap(left, right, budget):
    """Linear, deterministic half-open interval overlap with a real work budget."""
    left, right = sorted(left), sorted(right)
    i = j = total = comparisons = 0
    while i < len(left) and j < len(right):
        comparisons += 1
        if comparisons > budget:
            return None, comparisons
        start, end = max(left[i][0], right[j][0]), min(left[i][1], right[j][1])
        total += max(0, end - start)
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return total, comparisons


def _nearest(left, right, tolerance, budget=MAX_PAIR_COMPARISONS):
    """Nearest closed-tick support in O(m log m + n log m), with right reuse.

    A proper half-open right interval ``[s,e)`` becomes ``[s,e-1]``; a point
    stays ``[t,t]``.  The monotone prefix maximum identifies the first (thus
    tie-minimal) overlapping entry without scanning a set of intervals.
    """
    entries = sorted((start, end - 1 if start < end else start, end, observation_id)
                     for observation_id, start, end in right)
    starts = [item[0] for item in entries]
    prefix_max: list[int] = []
    maximum = -10**30
    for _start, closed_end, _end, _id in entries:
        maximum = max(maximum, closed_end)
        prefix_max.append(maximum)
    end_entries: dict[int, tuple[int, int, int, str]] = {}
    for entry in entries:
        candidate = (entry[0], entry[1], entry[2], entry[3])
        prior = end_entries.get(entry[1])
        end_entries[entry[1]] = candidate if prior is None else min(prior, candidate)
    closed_ends = sorted(end_entries)
    used: set[str] = set(); deltas: list[int] = []; matched = comparisons = 0
    for _id, start, end in left:
        query_end = end - 1 if start < end else start
        upper = bisect_right(starts, query_end)
        overlap_index = bisect_left(prefix_max, start)
        overlap = overlap_index < upper
        prior_index = bisect_left(closed_ends, start) - 1 if not overlap else -1
        slots = 1 if overlap else int(prior_index >= 0) + int(upper < len(entries))
        if comparisons + slots > budget:
            return None
        comparisons += slots
        candidates: list[tuple[int, int, int, str]] = []
        if overlap:
            candidate_start, candidate_closed_end, candidate_end, candidate_id = entries[overlap_index]
            candidates.append((0, candidate_start, candidate_end, candidate_id))
        else:
            if prior_index >= 0:
                candidate_start, candidate_closed_end, candidate_end, candidate_id = end_entries[
                    closed_ends[prior_index]]
                candidates.append((start - candidate_closed_end, candidate_start, candidate_end, candidate_id))
            if upper < len(entries):
                candidate_start, _candidate_closed_end, candidate_end, candidate_id = entries[upper]
                candidates.append((candidate_start - query_end, candidate_start, candidate_end, candidate_id))
        if candidates:
            delta, _other_start, _other_end, other_id = min(candidates)
            deltas.append(delta)
            if delta <= tolerance:
                matched += 1; used.add(other_id)
    return matched, len(left) - matched, len(right) - len(used), {
        "count": len(deltas), "minimum": min(deltas) if deltas else None,
        "maximum": max(deltas) if deltas else None,
        "tieBreak": "distance,startTick,endTick,observationId",
        "rightReuse": True,
    }, comparisons


def _rate(stream):
    return {"numerator": stream.nominal_rate.numerator, "denominator": stream.nominal_rate.denominator,
            "physicalUnit": stream.nominal_rate.physical_unit}


def _measured_rate(facts):
    if len(facts) < 2 or facts[-1][1] == facts[0][1]: return None
    return {"numerator": len(facts) - 1, "denominator": facts[-1][1] - facts[0][1], "unit": "per tick"}


def _mapping_identity(mapping):
    return {"sourceClockId": mapping.source_clock_id, "targetClockId": mapping.target_clock_id,
            "scaleNumerator": mapping.scale_numerator, "scaleDenominator": mapping.scale_denominator,
            "offsetTick": mapping.offset_tick}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _public_stream(item: dict[str, object]) -> dict[str, object]:
    """Keep normalized observations internal: the API reports evidence, not rows."""
    return {key: value for key, value in item.items() if key not in {"_facts", "_point"}}


def _hull(item):
    if item["firstTick"] is None or item["lastTick"] is None:
        return []
    return [(item["firstTick"], item["lastTick"])]
