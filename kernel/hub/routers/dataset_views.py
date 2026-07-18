"""Immutable exact-revision DatasetViews and their bounded Workspace detail surface."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

import duckdb
from fastapi import APIRouter, Depends, Response

from hub import db, metadb, paths
from hub.api_errors import APIError, APIErrorCode
from hub.backends import DatasetRevisionAdapter, Relation
from hub.deps import get_deps
from hub.executors.engine import _table_to_rows
from hub.models import (
    DatasetRevisionLastKnown,
    DatasetViewCreateRequest,
    DatasetViewDefinitionV1,
    DatasetViewDeleteResult,
    DatasetViewPlacement,
    DatasetViewPreview,
    ExactDatasetRef,
    SampleProvenance,
)
from hub.plugins.adapters import (
    RevisionPermissionLost,
    RevisionProviderOffline,
    RevisionUnavailable,
    relation_columns,
    revision_adapter_for_uri,
)
from hub.security import current_user
from hub.sqlpolicy import (
    FragmentKind,
    SQLPolicyError,
    identifier,
    identifier_key,
    quote_identifier,
    validate_fragment,
)
from hub.storage import ManagedSourceReadError, source_read_scope


router = APIRouter()

_INTEGER_LOGICAL_TYPES = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
})


class _DatasetViewUnsupported(ValueError):
    pass


def supports_dataset_view_source(uri: str, adapter: object) -> bool:
    """Return the exact server-owned capability advertised by the Catalog UI."""
    try:
        local = paths.local_path(uri)
    except ValueError:
        return False
    return (
        local is not None
        and isinstance(adapter, DatasetRevisionAdapter)
        and "exact" in set(getattr(adapter, "revision_selectors", ()))
        and getattr(adapter, "retention_owner", "provider") in {"core", "provider"}
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _request_payload(request: DatasetViewCreateRequest, *, include_name: bool) -> dict:
    payload = {
        "schemaVersion": 1,
        "datasetRef": {
            "kind": "exact",
            "datasetId": request.dataset_ref.dataset_id,
            "revisionId": request.dataset_ref.revision_id,
        },
        "selectedColumns": request.selected_columns,
        "predicate": request.predicate,
        "sampling": request.sampling.model_dump(by_alias=True, mode="json"),
    }
    if request.temporal_window is not None:
        payload["temporalWindow"] = request.temporal_window.model_dump(
            by_alias=True, mode="json")
    if include_name:
        payload["name"] = request.name
    return payload


def _stored_definition(uid: str, view_id: str) -> DatasetViewDefinitionV1:
    row = metadb.dataset_view_get(uid, view_id)
    if row is None:
        raise APIError(
            404, "DatasetView not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    if row["deleted"]:
        raise APIError(
            410, "DatasetView was deleted", code=APIErrorCode.RESOURCE_GONE, retryable=False)
    try:
        return DatasetViewDefinitionV1.model_validate(row["definition"])
    except ValueError as exc:  # pragma: no cover - committed definitions are server-authored
        raise RuntimeError("persisted DatasetView definition is invalid") from exc


@dataclass(frozen=True)
class _ExactSource:
    uri: str
    adapter: DatasetRevisionAdapter
    retention_owner: Literal["core", "provider"]
    detail: dict
    relation: Relation


@contextlib.contextmanager
def _open_exact(ref: ExactDatasetRef, *, operation: str) -> Iterator[_ExactSource]:
    binding = metadb.catalog_revision_binding(ref.dataset_id)
    if binding is None or binding["dataset_id"] != ref.dataset_id:
        raise RevisionUnavailable("revision_unavailable")
    uri = str(binding["uri"])
    adapter = revision_adapter_for_uri(uri, get_deps().resolve_adapter)
    if not supports_dataset_view_source(uri, adapter):
        raise _DatasetViewUnsupported(
            "DatasetViews currently support advertised local exact revision providers only")
    assert isinstance(adapter, DatasetRevisionAdapter)
    retention_owner = str(getattr(adapter, "retention_owner", "provider"))
    if retention_owner not in {"core", "provider"}:
        raise _DatasetViewUnsupported("the source does not declare a durable revision owner")
    artifact_uri = metadb.managed_local_file_revision_artifact(
        ref.dataset_id, ref.revision_id)
    if retention_owner == "core" and artifact_uri is None:
        raise RevisionUnavailable("revision_unavailable")
    scope = (source_read_scope(
        get_deps().storage,
        [artifact_uri],
        owner=f"dataset-view:{operation}:{uuid.uuid4().hex}",
    ) if artifact_uri is not None else contextlib.nullcontext())
    with scope, db.run_scope():
        detail = adapter.revision_detail(uri, ref.revision_id, preview_limit=1)
        if str(detail.get("revision_id")) != ref.revision_id:
            raise RevisionUnavailable("revision_unavailable")
        relation = adapter.open_revision(uri, ref.revision_id)
        yield _ExactSource(
            uri=uri,
            adapter=adapter,
            retention_owner=retention_owner,
            detail=detail,
            relation=relation,
        )


def _defined_relation(
    source: _ExactSource,
    definition: DatasetViewCreateRequest | DatasetViewDefinitionV1,
) -> Relation:
    relation = source.relation
    available = list(relation.columns)
    selected = [
        identifier(column, available, label="DatasetView column")
        for column in definition.selected_columns
    ]
    if len({identifier_key(column) for column in selected}) != len(selected):
        raise SQLPolicyError("DatasetView columns resolve to the same input column")
    window = definition.temporal_window
    if window is not None:
        time_field = identifier(
            window.time_field, available, label="DatasetView temporal window field")
        time_index = next(
            index for index, column in enumerate(available)
            if identifier_key(column) == identifier_key(time_field)
        )
        if str(relation.types[time_index]).upper() not in _INTEGER_LOGICAL_TYPES:
            raise _DatasetViewUnsupported(
                "DatasetView temporal window field must be an integer column")
    if definition.predicate:
        predicate = validate_fragment(
            FragmentKind.PREDICATE, definition.predicate, con=db.conn()).sql
        relation = relation.filter(predicate)
    if window is not None:
        start_tick, end_tick = int(window.start_tick), int(window.end_tick)
        relation = relation.filter(
            f"{quote_identifier(time_field)} >= {start_tick} "
            f"AND {quote_identifier(time_field)} < {end_tick}"
        )
    relation = relation.project(", ".join(quote_identifier(column) for column in selected))
    # DuckDB relations bind lazily. Force schema binding without reading a row so invalid predicates
    # and projections fail before the immutable definition is committed.
    relation.limit(0).to_arrow_table()
    if definition.sampling.kind == "reservoir":
        view = db.unique_view("dataset_view")
        relation.create_view(view)
        relation = db.conn().sql(
            f"SELECT * FROM {quote_identifier(view)} USING SAMPLE "
            f"{definition.sampling.size} ROWS (reservoir, {definition.sampling.seed})"
        )
    return relation


def _reservoir_provenance(
    request: DatasetViewCreateRequest, *, semantic_sha256: str, returned_rows: int,
) -> SampleProvenance:
    sampling = request.sampling
    assert sampling.kind == "reservoir"
    exact_population = returned_rows if returned_rows < sampling.size else None
    identity = _canonical_sha256({
        "schemaVersion": 1,
        "semanticSha256": semantic_sha256,
        "strategy": "reservoir",
        "seed": sampling.seed,
        "requestedRows": sampling.size,
        "datasetIdentity": request.dataset_ref.dataset_id,
        "datasetRevision": request.dataset_ref.revision_id,
    })
    limitation = (
        "The deterministic reservoir scanned the complete filtered exact revision."
        if exact_population is not None else
        "The deterministic reservoir scanned the complete filtered exact revision; "
        "the total population size is not retained."
    )
    return SampleProvenance(
        strategy="reservoir",
        seed=sampling.seed,
        requested_rows=sampling.size,
        scanned_rows=exact_population,
        returned_rows=returned_rows,
        total_rows=exact_population,
        dataset_identity=request.dataset_ref.dataset_id,
        dataset_revision=request.dataset_ref.revision_id,
        identity=identity,
        limitations=[limitation],
    )


def _map_dataset_view_error(exc: Exception) -> APIError:
    if isinstance(exc, metadb.DatasetViewSubmissionConflict):
        return APIError(
            409, "DatasetView submission id belongs to a different request",
            code=APIErrorCode.CONFLICT, retryable=False)
    if isinstance(exc, metadb.WorkspaceVersionConflict):
        return APIError(
            409, "DatasetView source placement changed; retry the request",
            code=APIErrorCode.CONFLICT, retryable=True)
    if isinstance(exc, metadb.DatasetViewGone):
        return APIError(
            410, "DatasetView submission was deleted",
            code=APIErrorCode.RESOURCE_GONE, retryable=False)
    if isinstance(exc, (RevisionPermissionLost, PermissionError)):
        return APIError(
            403, "DatasetView source permission was lost",
            code=APIErrorCode.PERMISSION_DENIED, retryable=False)
    if isinstance(exc, (RevisionProviderOffline, ConnectionError, TimeoutError)):
        return APIError(
            503, "DatasetView source provider is offline",
            code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True)
    if isinstance(exc, (RevisionUnavailable, ManagedSourceReadError, KeyError)):
        return APIError(
            410, "DatasetView exact source revision is unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False)
    if isinstance(exc, (_DatasetViewUnsupported, SQLPolicyError, duckdb.Error, ValueError)):
        return APIError(
            422, "DatasetView definition is not executable for this exact source",
            code=APIErrorCode.VALIDATION_ERROR, retryable=False)
    raise exc


@router.post(
    "/dataset-views",
    response_model=DatasetViewDefinitionV1,
    status_code=201,
    responses={
        200: {
            "model": DatasetViewDefinitionV1,
            "description": "Identical submission replayed from its immutable definition.",
        },
    },
)
def create_dataset_view(
    request: DatasetViewCreateRequest,
    response: Response,
    uid: str = Depends(current_user),
) -> DatasetViewDefinitionV1:
    """Create or replay one immutable exact-revision view and its Workspace placement."""
    request_sha256 = _canonical_sha256(_request_payload(request, include_name=True))
    prior = metadb.dataset_view_submission(uid, request.submission_id)
    if prior is not None:
        if prior["requestSha256"] != request_sha256:
            raise _map_dataset_view_error(metadb.DatasetViewSubmissionConflict())
        if prior["deleted"]:
            raise _map_dataset_view_error(metadb.DatasetViewGone())
        response.status_code = 200
        return DatasetViewDefinitionV1.model_validate(prior["definition"])

    semantic_sha256 = _canonical_sha256(_request_payload(request, include_name=False))
    try:
        with _open_exact(request.dataset_ref, operation="create") as source:
            relation = _defined_relation(source, request)
            provenance = None
            if request.sampling.kind == "reservoir":
                returned_rows = int(relation.aggregate("count(*) AS n").fetchone()[0])
                provenance = _reservoir_provenance(
                    request, semantic_sha256=semantic_sha256, returned_rows=returned_rows)
            workspace = metadb.dataset_view_source_workspace(request.dataset_ref.dataset_id)
            view_id = uuid.uuid4().hex
            placement_id = uuid.uuid4().hex
            committed_at = source.detail.get("committed_at")
            ref = ExactDatasetRef(
                kind="exact",
                dataset_id=request.dataset_ref.dataset_id,
                revision_id=request.dataset_ref.revision_id,
                last_known=(DatasetRevisionLastKnown(committed_at=committed_at)
                            if committed_at is not None else None),
            )
            created_at = datetime.datetime.now(datetime.timezone.utc)
            base = {
                "schemaVersion": 1,
                "id": view_id,
                "creatorId": uid,
                "name": request.name,
                "datasetRef": ref.model_dump(by_alias=True, mode="json"),
                "placement": DatasetViewPlacement(
                    container_id=workspace["containerId"],
                    placement_id=placement_id,
                    source_registration_id=workspace["sourceRegistrationId"],
                ).model_dump(by_alias=True, mode="json"),
                "selectedColumns": request.selected_columns,
                "predicate": request.predicate,
                "sampling": request.sampling.model_dump(by_alias=True, mode="json"),
                "sampleProvenance": (
                    provenance.model_dump(by_alias=True, mode="json") if provenance else None),
                "retentionOwner": source.retention_owner,
                "createdAt": created_at.isoformat(),
                "semanticSha256": semantic_sha256,
            }
            if request.temporal_window is not None:
                base["temporalWindow"] = request.temporal_window.model_dump(
                    by_alias=True, mode="json")
            definition_sha256 = _canonical_sha256(base)
            definition = DatasetViewDefinitionV1.model_validate({
                **base, "definitionSha256": definition_sha256,
            })
            document = json.dumps(
                definition.model_dump(by_alias=True, mode="json"),
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            )
            stored, created = metadb.dataset_view_create(
                uid=uid,
                view_id=view_id,
                placement_id=placement_id,
                submission_id=request.submission_id,
                request_sha256=request_sha256,
                definition_sha256=definition_sha256,
                definition_doc=document,
                source_dataset_id=request.dataset_ref.dataset_id,
                source_registration_id=workspace["sourceRegistrationId"],
                expected_container_id=workspace["containerId"],
            )
            response.status_code = 201 if created else 200
            return DatasetViewDefinitionV1.model_validate(stored)
    except Exception as exc:
        raise _map_dataset_view_error(exc) from None


@router.get("/dataset-views/{view_id}", response_model=DatasetViewDefinitionV1)
def get_dataset_view(
    view_id: str,
    uid: str = Depends(current_user),
) -> DatasetViewDefinitionV1:
    return _stored_definition(uid, view_id)


@router.post("/dataset-views/{view_id}/preview", response_model=DatasetViewPreview)
def preview_dataset_view(
    view_id: str,
    uid: str = Depends(current_user),
) -> DatasetViewPreview:
    """Replay the immutable exact definition and return at most 100 rows."""
    definition = _stored_definition(uid, view_id)
    try:
        with _open_exact(definition.dataset_ref, operation="preview") as source:
            relation = _defined_relation(source, definition)
            columns = relation_columns(relation)
            table = relation.limit(101).to_arrow_table()
            has_more = table.num_rows > 100
            rows = _table_to_rows(table.slice(0, 100))
            evidence = definition.sample_provenance
            row_count = (
                evidence.returned_rows if evidence is not None else
                len(rows) if not has_more else None
            )
            if (evidence is not None
                    and evidence.returned_rows <= 100
                    and table.num_rows != evidence.returned_rows):
                raise RevisionUnavailable("revision_unavailable")
            return DatasetViewPreview(
                columns=columns,
                rows=rows,
                row_count=row_count,
                has_more=has_more,
                sample_provenance=definition.sample_provenance,
            )
    except Exception as exc:
        raise _map_dataset_view_error(exc) from None


@router.delete("/dataset-views/{view_id}", response_model=DatasetViewDeleteResult)
def delete_dataset_view(
    view_id: str,
    uid: str = Depends(current_user),
) -> DatasetViewDeleteResult:
    deleted = metadb.dataset_view_delete(uid, view_id)
    if deleted is None:
        raise APIError(
            404, "DatasetView not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    return DatasetViewDeleteResult(deleted=deleted)
