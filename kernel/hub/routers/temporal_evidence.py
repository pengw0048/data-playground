"""Authenticated API for one bounded compound temporal-evidence read."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from hub.compound_datasets import CompoundManifestError, open_compound_manifest
from hub.models import (
    DatasetViewDefinitionV1, TemporalEvidenceRequestV1, TemporalEvidenceResponseV1,
)
from hub.plugins.adapters import RevisionPermissionLost, RevisionProviderOffline, RevisionUnavailable
from hub.routers.dataset_views import _defined_relation, _open_exact, _stored_definition
from hub.security import current_user
from hub.sqlpolicy import quote_identifier
from hub.temporal_evidence import (
    EvidenceRequest, EvidenceWindow, ObservationReader, TemporalEvidenceError,
    _mapping, _source_bounds, compute_temporal_evidence,
)

router = APIRouter()


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class _CatalogObservationReader(ObservationReader):
    """Resolve manifest member identities through catalog authority, never a URI."""

    def __init__(self, views: dict[str, DatasetViewDefinitionV1]) -> None:
        self._views = views

    def read(self, *, dataset_id, revision_id, fields, stream_id, episode_id, episode_id_field, tick_field,
             start_tick_field, end_tick_field, source_start, source_end, limit):
        # _open_exact retains the adapter's exact revision, retention owner, and source read fence.
        from hub.models import ExactDatasetRef  # avoid router import cycles during app startup
        try:
            view = self._views[stream_id]
            with _open_exact(ExactDatasetRef(kind="exact", dataset_id=dataset_id,
                                              revision_id=revision_id), operation="temporal-evidence") as source:
                # Replaying this stored definition preserves its immutable predicate,
                # projection, temporal bound, exact revision, and retention read fence.
                relation = _defined_relation(source, view)
                if set(fields) - set(relation.columns):
                    raise ValueError("DatasetView omits compound observation-index fields")
                episode = quote_identifier(episode_id_field)
                # The manifest schema verifies names and the selected fields below are quoted anyway.
                conditions = [f"{episode} = {_literal(episode_id)}"]
                if tick_field is not None:
                    tick = quote_identifier(tick_field)
                    conditions.extend([f"{tick} >= {source_start}", f"{tick} < {source_end}"])
                else:
                    assert start_tick_field is not None and end_tick_field is not None
                    start, end = quote_identifier(start_tick_field), quote_identifier(end_tick_field)
                    conditions.extend([f"{end} > {source_start}", f"{start} < {source_end}"])
                relation = relation.filter(" AND ".join(conditions)).project(
                    ", ".join(quote_identifier(field) for field in fields))
                return relation.limit(limit).to_arrow_table().to_pylist()
        except (RevisionPermissionLost, PermissionError):
            raise PermissionError from None
        except (RevisionProviderOffline, ConnectionError, TimeoutError, RevisionUnavailable):
            raise ConnectionError from None


@router.post("/temporal-evidence", response_model=TemporalEvidenceResponseV1)
def temporal_evidence(
    request: TemporalEvidenceRequestV1,
    uid: str = Depends(current_user),
) -> TemporalEvidenceResponseV1:
    """Return evidence only; raw observations and physical member locations stay private."""
    try:
        manifest = open_compound_manifest(request.manifest_json.encode("utf-8"))
        bindings = {(item.episode_id, item.stream_id): item for item in manifest.bindings}
        members = {item.id: item for item in manifest.members}
        streams = {item.id: item for item in manifest.streams}
        if request.episode_id not in {item.episode_id for item in manifest.episodes}:
            raise TemporalEvidenceError("episode is not in the exact compound revision")
        if set(request.stream_ids) - set(streams):
            raise TemporalEvidenceError("stream is not in the exact compound revision")
        views = {item.stream_id: _stored_definition(uid, item.dataset_view_id)
                 for item in request.stream_views}
        for stream_id in request.stream_ids:
            binding = bindings[(request.episode_id, stream_id)]
            if binding.state == "absent":
                if stream_id in views:
                    raise TemporalEvidenceError("declared-absent stream cannot bind a DatasetView")
                continue
            if stream_id not in views:
                raise TemporalEvidenceError("present stream requires a server-owned DatasetView")
            view, member = views[stream_id], members[binding.member_id]
            if (view.dataset_ref.dataset_id, view.dataset_ref.revision_id) != (
                    member.dataset_id, member.revision_id):
                raise TemporalEvidenceError("DatasetView exact revision does not match manifest member")
            if view.sampling.kind != "all" or view.temporal_window is None:
                raise TemporalEvidenceError("temporal evidence requires an all-rows bounded DatasetView")
            index = binding.observation_index
            assert index is not None
            required = {index.observation_id_field, index.episode_id_field,
                        *(field for field in (index.tick_field, index.start_tick_field,
                                               index.end_tick_field) if field is not None)}
            if not required.issubset(set(view.selected_columns)):
                raise TemporalEvidenceError("DatasetView omits compound observation-index fields")
        reference = next((view for view in views.values() if view.id == request.reference_view_id), None)
        if reference is None or reference.temporal_window is None:
            raise TemporalEvidenceError("reference DatasetView must be a selected bounded member view")
        reference_streams = [stream_id for stream_id, view in views.items() if view.id == reference.id]
        if len(reference_streams) != 1:
            raise TemporalEvidenceError("reference DatasetView must bind exactly one selected stream")
        if streams[reference_streams[0]].clock.id != reference.temporal_window.time_domain:
            raise TemporalEvidenceError("reference DatasetView time domain must name its stream clock")
        reference_window = EvidenceWindow(reference.temporal_window.time_domain,
                                          int(reference.temporal_window.start_tick),
                                          int(reference.temporal_window.end_tick))
        for stream_id, view in views.items():
            binding, stream = bindings[(request.episode_id, stream_id)], streams[stream_id]
            assert binding.observation_index is not None and view.temporal_window is not None
            expected_field = (binding.observation_index.tick_field
                              or binding.observation_index.start_tick_field)
            if view.temporal_window.time_field != expected_field:
                raise TemporalEvidenceError("DatasetView time field must match the observation index")
            if view.temporal_window.time_domain != stream.clock.id:
                raise TemporalEvidenceError("DatasetView time domain must match the stream clock")
            mapping = _mapping(manifest, stream.clock.id, reference_window.time_domain)
            if stream.clock.id != reference_window.time_domain and mapping is None:
                raise TemporalEvidenceError("present stream lacks a named mapping to the reference window")
            source_start, source_end = _source_bounds(mapping, reference_window)
            if (int(view.temporal_window.start_tick) > source_start
                    or int(view.temporal_window.end_tick) < source_end):
                raise TemporalEvidenceError("DatasetView window does not cover the evidence window")
        pair = (request.pair.left_stream_id, request.pair.right_stream_id) if request.pair else None
        return TemporalEvidenceResponseV1.model_validate(compute_temporal_evidence(
            manifest,
            EvidenceRequest(
                episode_id=request.episode_id, stream_ids=tuple(request.stream_ids), pair=pair,
                tolerance_ticks=int(request.tolerance_ticks),
                gap_threshold_ticks=int(request.gap_threshold_ticks),
                window=reference_window,
                view_identities=tuple(sorted(
                    (stream_id, view.id, view.definition_sha256, view.semantic_sha256)
                    for stream_id, view in views.items())),
            ),
            _CatalogObservationReader(views),
        ))
    except (CompoundManifestError, TemporalEvidenceError, ValueError) as exc:
        # A malformed manifest/window cannot safely be partially scanned.
        from hub.api_errors import APIError, APIErrorCode
        raise APIError(422, str(exc), code=APIErrorCode.VALIDATION_ERROR, retryable=False) from None
