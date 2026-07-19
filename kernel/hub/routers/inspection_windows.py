"""One server-owned, bounded raw inspection read for the reference compound fixture."""
from __future__ import annotations

import json
from typing import Iterator

import pyarrow
from fastapi import APIRouter, Depends

from hub.api_errors import APIError, APIErrorCode
from hub.compound_datasets import CompoundManifestError, map_tick
from hub.compound_fixture import (
    FixtureUnavailable, current_user_fixture_reference, episode_reference_windows,
    fixture_asset_available,
)
from hub.models import (
    ExactDatasetRef, InspectionWindowEvidenceV1, InspectionWindowObservationV1,
    InspectionWindowRequestV1, InspectionWindowResponseV1, InspectionWindowStreamV1,
)
from hub.plugins.adapters import RevisionPermissionLost, RevisionProviderOffline, RevisionUnavailable
from hub.routers.dataset_views import _defined_relation, _open_exact
from hub.routers.temporal_evidence import _CatalogObservationReader
from hub.security import current_user
from hub.sqlpolicy import quote_identifier
from hub.temporal_evidence import (
    MAX_OBSERVATIONS_PER_STREAM, EvidenceRequest, EvidenceWindow, TemporalEvidenceError,
    _mapping, _source_bounds, compute_temporal_evidence,
)


router = APIRouter()
# Fixture source bytes are server-capped and digested before parsing; Arrow response rows use
# 128-row batches followed by the separate per-row canonical 1 MB output budget below.
_RAW_BATCH_ROWS = 128
_RAW_BYTES_PER_STREAM = 1_000_000


class _StaleAuthority(ValueError):
    """The current user's saved exact views no longer bind the immutable authority."""


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _columns(stream):
    return [{"name": field.name, "type": field.type, "nullable": field.nullable,
             "provenance": "declared"} for field in stream.observation_schema]


def _authority(dataset_id: str, revision_id: str, uid: str):
    try:
        authority = current_user_fixture_reference(uid)
    except FixtureUnavailable as exc:
        raise APIError(410, "Compound fixture is unavailable", code=APIErrorCode.RESOURCE_GONE,
                       retryable=False) from exc
    current = authority.manifest.ref
    if dataset_id != current.dataset_id:
        raise APIError(404, "Compound revision not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    if revision_id != authority.manifest.digest:
        raise APIError(409, "Compound revision is stale; reopen the exact revision",
                       code=APIErrorCode.CONFLICT, retryable=False)
    return authority


def _request(authority, request: InspectionWindowRequestV1):
    try:
        start, end = int(request.start_tick), int(request.end_tick)
        bounds = episode_reference_windows()[request.episode_id]
        if start < int(bounds[0]) or end > int(bounds[1]):
            raise TemporalEvidenceError("inspection window is outside the declared episode")
        streams = {stream.id: stream for stream in authority.manifest.streams}
        bindings = {(item.episode_id, item.stream_id): item for item in authority.manifest.bindings}
        if set(request.stream_ids) - set(streams):
            raise TemporalEvidenceError("stream is not in the exact compound revision")
        if any((request.episode_id, stream_id) not in bindings for stream_id in request.stream_ids):
            raise TemporalEvidenceError("episode is not in the exact compound revision")
        reference_clock = next(stream.clock.id for stream in authority.manifest.streams
                               if stream.clock.time_domain == "reference")
        window = EvidenceWindow(reference_clock, start, end)
        for stream_id in request.stream_ids:
            binding = bindings[(request.episode_id, stream_id)]
            if binding.state == "absent":
                continue
            view, member = authority.views.get(stream_id), next(
                item for item in authority.manifest.members if item.id == binding.member_id)
            if view is None or (view.dataset_ref.dataset_id, view.dataset_ref.revision_id) != (
                    member.dataset_id, member.revision_id):
                raise _StaleAuthority("saved member view does not match the exact compound revision")
            index = binding.observation_index
            assert index is not None
            required = {index.observation_id_field, index.episode_id_field, *index.value_refs,
                        *(field for field in (index.tick_field, index.start_tick_field,
                                               index.end_tick_field) if field is not None)}
            if (view.sampling.kind != "all" or view.temporal_window is None
                    or not required.issubset(view.selected_columns)
                    or view.temporal_window.time_domain != streams[stream_id].clock.id
                    or view.temporal_window.time_field != (index.tick_field or index.start_tick_field)):
                raise _StaleAuthority("saved member view is no longer a bounded exact inspection view")
            mapping = _mapping(authority.manifest, streams[stream_id].clock.id, reference_clock)
            if streams[stream_id].clock.id != reference_clock and mapping is None:
                raise _StaleAuthority("saved member view lacks the declared reference-clock mapping")
            source_start, source_end = _source_bounds(mapping, window)
            if (int(view.temporal_window.start_tick) > source_start
                    or int(view.temporal_window.end_tick) < source_end):
                raise _StaleAuthority("saved member view no longer covers the inspection window")
        pair = ((request.pair.left_stream_id, request.pair.right_stream_id)
                if request.pair is not None else None)
        return EvidenceRequest(
            episode_id=request.episode_id, stream_ids=tuple(request.stream_ids), window=window,
            gap_threshold_ticks=int(request.gap_threshold_ticks), tolerance_ticks=int(request.tolerance_ticks),
            pair=pair,
        ), bindings, streams
    except _StaleAuthority:
        raise
    except (KeyError, StopIteration, ValueError, CompoundManifestError) as exc:
        raise TemporalEvidenceError("inspection request does not match the exact compound revision") from exc


def _rows(authority, view, *, member, index, stream_id: str, episode_id: str, source_start: int,
          source_end: int) -> Iterator[dict[str, object]]:
    fields = tuple(field.name for field in next(
        stream for stream in authority.manifest.streams if stream.id == stream_id).observation_schema)
    try:
        with _open_exact(ExactDatasetRef(kind="exact", dataset_id=member.dataset_id,
                                         revision_id=member.revision_id), operation="inspection-window") as source:
            relation = _defined_relation(source, view)
            if set(fields) - set(relation.columns):
                raise _StaleAuthority("saved member view omits declared observation fields")
            episode = quote_identifier(index.episode_id_field)
            if index.tick_field is not None:
                tick = quote_identifier(index.tick_field)
                condition = f"{episode} = {_literal(episode_id)} AND {tick} >= {source_start} AND {tick} < {source_end}"
                order = f"{tick}, {quote_identifier(index.observation_id_field)}"
            else:
                start, end = quote_identifier(index.start_tick_field), quote_identifier(index.end_tick_field)
                condition = f"{episode} = {_literal(episode_id)} AND {end} > {source_start} AND {start} < {source_end}"
                order = f"{start}, {end}, {quote_identifier(index.observation_id_field)}"
            reader = relation.filter(condition).project(
                ", ".join(quote_identifier(field) for field in fields)
            ).order(order).limit(MAX_OBSERVATIONS_PER_STREAM + 1).to_arrow_reader(_RAW_BATCH_ROWS)
            for batch in reader:
                yield from batch.to_pylist()
    except _StaleAuthority:
        raise
    except (RevisionPermissionLost, PermissionError):
        raise PermissionError from None
    except (RevisionProviderOffline, ConnectionError, TimeoutError, RevisionUnavailable):
        raise ConnectionError from None


def _observations(authority, view, binding, stream, evidence_request):
    """Map and account rows incrementally; the first over-budget row is lookahead only."""
    index = binding.observation_index
    assert index is not None and binding.member_id is not None
    member = next(item for item in authority.manifest.members if item.id == binding.member_id)
    mapping = _mapping(authority.manifest, stream.clock.id, evidence_request.window.time_domain)
    source_start, source_end = _source_bounds(mapping, evidence_request.window)
    assets = {item.id: item for item in authority.manifest.assets}
    output: list[InspectionWindowObservationV1] = []
    corrupt = used_bytes = row_count = 0
    truncated = False
    identifiers: set[str] = set()
    try:
        rows = _rows(authority, view, member=member, index=index, stream_id=stream.id,
                     episode_id=evidence_request.episode_id, source_start=source_start, source_end=source_end)
        for row in rows:
            row_count += 1
            if row_count > MAX_OBSERVATIONS_PER_STREAM:
                truncated = True
                break
            try:
                observation_id = row[index.observation_id_field]
                if not isinstance(observation_id, str) or not observation_id or observation_id in identifiers:
                    raise ValueError
                identifiers.add(observation_id)
                if index.tick_field is not None:
                    tick = row[index.tick_field]
                    if type(tick) is not int:
                        raise ValueError
                    start = map_tick(mapping, tick) if mapping else tick
                    end, kind = None, "point"
                    if not evidence_request.window.start_tick <= start < evidence_request.window.end_tick:
                        continue
                else:
                    raw_start, raw_end = row[index.start_tick_field], row[index.end_tick_field]
                    if type(raw_start) is not int or type(raw_end) is not int or raw_start >= raw_end:
                        raise ValueError
                    start = map_tick(mapping, raw_start) if mapping else raw_start
                    end = map_tick(mapping, raw_end) if mapping else raw_end
                    if start >= end or end <= evidence_request.window.start_tick or start >= evidence_request.window.end_tick:
                        continue
                    start, end, kind = max(start, evidence_request.window.start_tick), min(end, evidence_request.window.end_tick), "interval"
                references = []
                for asset_id in binding.asset_ids:
                    if any(row.get(field) == asset_id for field in index.value_refs):
                        asset = assets[asset_id]
                        references.append({"id": asset.id, "mediaType": asset.media_type,
                                           "byteLength": asset.byte_length, "sha256": asset.sha256,
                                           "status": "available" if fixture_asset_available(authority) else "unavailable"})
                item = InspectionWindowObservationV1(
                    observationId=observation_id, kind=kind, startTick=start, endTick=end,
                    values={field: row[field] for field in index.value_refs}, assets=references,
                )
                encoded = json.dumps(item.model_dump(by_alias=True, mode="json"), sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")
                if used_bytes + len(encoded) > _RAW_BYTES_PER_STREAM:
                    truncated = True
                    break
                used_bytes += len(encoded)
                output.append(item)
            except (KeyError, TypeError, ValueError, CompoundManifestError):
                corrupt += 1
    except PermissionError:
        return [], corrupt, False, "permission", "exact member permission lost"
    except ConnectionError:
        return [], corrupt, False, "unavailable", "exact member unavailable"
    except _StaleAuthority:
        return [], corrupt, False, "partial", "saved exact member view is stale"
    except (OSError, pyarrow.ArrowException):
        return [], corrupt, False, "corrupt", "exact member read failed"
    return output, corrupt, not truncated and corrupt == 0, ("truncated" if truncated else None), None


@router.post(
    "/compound-datasets/{dataset_id}/revisions/{revision_id}/inspection-window",
    response_model=InspectionWindowResponseV1,
)
def inspection_window(dataset_id: str, revision_id: str, request: InspectionWindowRequestV1,
                      uid: str = Depends(current_user)) -> InspectionWindowResponseV1:
    """Return redacted evidence plus capped raw points/intervals for one exact fixture revision."""
    authority = _authority(dataset_id, revision_id, uid)
    try:
        evidence_request, bindings, streams = _request(authority, request)
        evidence_document = compute_temporal_evidence(authority.manifest, evidence_request,
                                                       _CatalogObservationReader(authority.views))
        evidence = InspectionWindowEvidenceV1.model_validate({
            key: evidence_document[key] for key in ("complete", "approximation", "streams", "pair")})
        evidence_by_stream = {item.stream_id: item for item in evidence.streams}
        observations = []
        for stream_id in evidence_request.stream_ids:
            binding, stream, source = bindings[(request.episode_id, stream_id)], streams[stream_id], evidence_by_stream[stream_id]
            if binding.state == "absent":
                observations.append(InspectionWindowStreamV1(
                    streamId=stream_id, state="absent", complete=True, columns=_columns(stream)))
                continue
            if source.state in {"permission", "unavailable", "unknown", "truncated"}:
                observations.append(InspectionWindowStreamV1(streamId=stream_id, state=source.state,
                                    complete=False, reason=source.reason, columns=_columns(stream)))
                continue
            raw, corrupt, complete, state, reason = _observations(
                authority, authority.views[stream_id], binding, stream, evidence_request)
            if state is None and source.state == "corrupt":
                state, complete, reason = ("partial" if raw else "corrupt"), False, (
                    source.reason or "temporal evidence contains corrupt observations")
            elif state is None and corrupt:
                state, reason = ("partial", "some raw observations are corrupt") if raw else ("corrupt", "raw observations are corrupt")
            if state is None and binding.asset_ids and not fixture_asset_available(authority):
                state, complete, reason = "partial", False, "declared asset is unavailable"
            observations.append(InspectionWindowStreamV1(streamId=stream_id, state=state or "present",
                                complete=complete and source.complete, reason=reason, corruptCount=corrupt,
                                columns=_columns(stream), observations=raw))
        complete = evidence.complete and all(item.complete for item in observations)
        return InspectionWindowResponseV1(
            identity={"compoundDatasetId": authority.manifest.ref.dataset_id,
                      "compoundRevision": authority.manifest.digest, "episodeId": request.episode_id,
                      "referenceClockId": evidence_request.window.time_domain,
                      "startTick": evidence_request.window.start_tick, "endTick": evidence_request.window.end_tick,
                      "streamIds": list(evidence_request.stream_ids)},
            complete=complete,
            limits={"maxRowsPerStream": MAX_OBSERVATIONS_PER_STREAM,
                    "maxRawBytesPerStream": _RAW_BYTES_PER_STREAM},
            evidence=evidence, observations=observations,
        )
    except _StaleAuthority as exc:
        raise APIError(409, "Saved exact inspection state is stale; reopen the revision",
                       code=APIErrorCode.CONFLICT, retryable=False) from exc
    except (CompoundManifestError, TemporalEvidenceError, ValueError) as exc:
        raise APIError(422, str(exc), code=APIErrorCode.VALIDATION_ERROR, retryable=False) from None
