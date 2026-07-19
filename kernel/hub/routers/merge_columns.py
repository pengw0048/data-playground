"""Narrow headless admission for one certified local add-or-replace merge."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from pydantic import ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from hub import metadb
from hub.api_errors import APIError, APIErrorCode
from hub.deps import get_deps
from hub.merge_columns import (
    MergeColumnRuleV1, MergeColumnsError, MergeColumnsIntentV1, merge_output_schema,
    sparse_output_merge_evidence,
)
from hub.merge_columns_tasks import dispatch
from hub.models import (
    ColumnSchema, DurableMergeColumnsView, ExactDatasetRef, Graph, LineagePublication,
    Wire, WriteDestination, WriteIntent, WriteProvenance,
)
from hub.plugins.catalog import InMemoryCatalog
from hub.security import current_user
from hub.sinks import SinkSpec, is_core_managed_local_file_sink, preflight_sink
from hub.sparse_outputs import (
    SparseOutputAdmissionRequest, SparseOutputError, SparseOutputMaterializationConflict,
    admit_sparse_output, materialize_sparse_output, prepare_sparse_output_admission,
)


router = APIRouter()


class MergeColumnsRequestV1(Wire):
    """One graph-shaped request; no storage or SparseOutput authority crosses this boundary."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    graph: Graph
    submission_id: str = Field(min_length=1, max_length=128)
    identity_columns: list[str] = Field(min_length=1, max_length=16)
    rules: list[MergeColumnRuleV1] = Field(min_length=1, max_length=128)

    @field_validator("submission_id")
    @classmethod
    def _submission_id(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value:
            raise ValueError("submissionId must be trimmed and NUL-free")
        return value.lower()


class MergeColumnsPreflightV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    base: ExactDatasetRef
    declared_key: list[str] = Field(default_factory=list, max_length=32)
    identity_columns: list[str] = Field(min_length=1, max_length=16)
    coverage: "MergeColumnsCoverageV1"
    rules: list[MergeColumnRuleV1]
    expected_head: ExactDatasetRef
    output_schema: list[ColumnSchema]
    provenance: "MergeColumnsPreflightProvenanceV1"
    eligible: bool = True


class MergeColumnsCoverageCountsV1(Wire):
    rows: int = Field(ge=0)
    unique_identities: int = Field(ge=0)
    null_rows: int = Field(ge=0)
    duplicate_groups: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)


class MergeColumnsCoverageV1(Wire):
    base: MergeColumnsCoverageCountsV1
    candidate: MergeColumnsCoverageCountsV1
    matched_identities: int = Field(ge=0)
    missing_identities: int = Field(ge=0)
    extra_identities: int = Field(ge=0)
    status: str


class MergeColumnsPreflightProvenanceV1(Wire):
    producer: str = "source_to_select"
    source: str = "exact"
    select_kind: str = "builtin"
    select_version: int = 1


class MergeColumnsTaskV1(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    task_id: str
    status: str
    can_retry: bool
    can_cancel: bool
    merge_columns: DurableMergeColumnsView | None = None


def _node_config(node) -> dict:
    return node.data.get("config", {}) if isinstance(node.data, dict) else {}


def _request_sha256(request: MergeColumnsRequestV1) -> str:
    """Hash only the frozen consumer semantics, never Canvas presentation state."""
    source, select, write = _shape(request)
    source_config = _node_config(source)
    try:
        base = ExactDatasetRef.model_validate(source_config.get("datasetRef"))
    except Exception as exc:
        raise APIError(409, "merge-columns requires one exact Source revision",
                       code=APIErrorCode.CONFLICT, retryable=False) from exc
    if base.kind != "exact":
        raise APIError(409, "merge-columns requires one exact Source revision",
                       code=APIErrorCode.CONFLICT, retryable=False)
    try:
        spec = SinkSpec.from_config(
            _node_config(write), write.data.get("title") if isinstance(write.data, dict) else None)
    except ValueError:
        raise APIError(409, "merge-columns requires the default managed-local Parquet destination",
                       code=APIErrorCode.CONFLICT, retryable=False) from None
    payload = json.dumps({
        "canvasId": request.graph.id,
        "source": {"uri": source_config.get("uri"),
                   "datasetRef": base.model_dump(by_alias=True, mode="json")},
        "select": _node_config(select)["select"],
        "sink": {
            "name": spec.name, "filename": spec.filename, "extension": spec.extension,
            "mode": spec.mode, "destinationId": spec.destination_id,
            "destinationPath": spec.destination_path, "partitionBy": spec.partition_by,
        },
        "identityColumns": request.identity_columns,
        "rules": [item.model_dump(by_alias=True, mode="json") for item in request.rules],
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _shape(request: MergeColumnsRequestV1):
    """Accept only Source -> Select -> Write with ordinary ports and no execution modifiers."""
    graph = request.graph
    if len(graph.nodes) != 3 or len(graph.edges) != 2:
        raise APIError(409, "merge-columns requires exactly Source -> Select -> Write",
                       code=APIErrorCode.CONFLICT, retryable=False)
    write = next((node for node in graph.nodes if node.type == "write"), None)
    select = next((node for node in graph.nodes if node.type == "select"), None)
    source = next((node for node in graph.nodes if node.type == "source"), None)
    if write is None or select is None or source is None:
        raise APIError(409, "merge-columns requires Source -> Select -> Write",
                       code=APIErrorCode.CONFLICT, retryable=False)
    incoming = {(edge.source, edge.target, edge.source_handle, edge.target_handle) for edge in graph.edges}
    if incoming != {(source.id, select.id, None, None), (select.id, write.id, None, None)}:
        # Existing graphs often omit the default handles; explicit out/in has identical meaning.
        expected = {(source.id, select.id), (select.id, write.id)}
        if {(edge.source, edge.target) for edge in graph.edges} != expected or any(
                edge.source_handle not in (None, "out") or edge.target_handle not in (None, "in")
                for edge in graph.edges):
            raise APIError(409, "merge-columns requires Source -> Select -> Write default ports",
                           code=APIErrorCode.CONFLICT, retryable=False)
    if any(bool(node.data.get("disabled") or node.data.get("bypassed"))
           for node in (source, select, write) if isinstance(node.data, dict)):
        raise APIError(409, "merge-columns rejects disabled or bypassed nodes",
                       code=APIErrorCode.CONFLICT, retryable=False)
    config = _node_config(select)
    if set(config) != {"select"} or not isinstance(config.get("select"), str):
        raise APIError(409, "merge-columns Select must contain only a deterministic select expression",
                       code=APIErrorCode.CONFLICT, retryable=False)
    return source, select, write


def _actor_and_canvas(request: MergeColumnsRequestV1, uid: str) -> str:
    canvas_id = str(request.graph.id)
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id)
        role = metadb.canvas_role(canvas_id, uid)
        if canvas is None or role not in ("owner", "editor"):
            raise APIError(404, "merge-columns canvas not found", code=APIErrorCode.CANVAS_NOT_FOUND,
                           retryable=False)
        return str(canvas.owner_id)


def _prepared(request: MergeColumnsRequestV1, uid: str):
    canvas_owner = _actor_and_canvas(request, uid)
    source, select, write = _shape(request)
    source_cfg = _node_config(source)
    try:
        base = ExactDatasetRef.model_validate(source_cfg.get("datasetRef"))
    except Exception as exc:
        raise APIError(409, "merge-columns requires one exact Source revision",
                       code=APIErrorCode.CONFLICT, retryable=False) from exc
    source_uri = source_cfg.get("uri")
    binding = metadb.catalog_revision_binding_for_uri(str(source_uri)) if source_uri else None
    exact_uri = metadb.managed_local_file_revision_artifact(base.dataset_id, base.revision_id)
    if (base.kind != "exact" or binding is None or binding["dataset_id"] != base.dataset_id
            or exact_uri is None or str(source_uri) != exact_uri):
        raise APIError(409, "merge-columns Source must be an exact managed-local Parquet revision",
                       code=APIErrorCode.CONFLICT, retryable=False)
    deps = get_deps()
    try:
        spec = SinkSpec.from_config(
            _node_config(write), write.data.get("title") if isinstance(write.data, dict) else None)
        logical_uri = preflight_sink(spec, deps.workspace, deps.storage, deps.resolve_adapter)
        adapter = deps.resolve_adapter(logical_uri)
    except (ValueError, NotImplementedError):
        raise APIError(409, "merge-columns requires the default managed-local Parquet destination",
                       code=APIErrorCode.CONFLICT, retryable=False) from None
    if (type(deps.catalog) is not InMemoryCatalog
            or not is_core_managed_local_file_sink(spec, logical_uri, adapter, deps.storage)):
        raise APIError(409, "merge-columns requires the default managed-local Parquet destination",
                       code=APIErrorCode.CONFLICT, retryable=False)
    head = metadb.catalog_managed_local_write_head(logical_uri)
    if (head is None or head.get("state") != "active" or head.get("dataset_id") != base.dataset_id
            or head.get("revision_id") != base.revision_id):
        raise APIError(409, "merge-columns destination head must equal the exact Source revision",
                       code=APIErrorCode.CONFLICT, retryable=False)
    sparse_submission = "merge:" + request.submission_id
    sparse_request = SparseOutputAdmissionRequest(
        owner_id=canvas_owner, canvas_id=str(request.graph.id), submission_id=sparse_submission,
        dataset_ref=base, select_config={"expr": select.data["config"]["select"]},
        identity_columns=request.identity_columns,
        provenance={"idempotencyKey": f"merge-columns:{request.graph.id}:{request.submission_id}",
                    "provenance": "manual"},
    )
    try:
        preparation = prepare_sparse_output_admission(deps.storage, sparse_request)
        output_schema = merge_output_schema(
            preparation.base_schema, preparation.sidecar_schema,
            list(preparation.identity_columns), request.rules)
    except (SparseOutputError, MergeColumnsError) as exc:
        raise APIError(422, "merge-columns admission is invalid", code=APIErrorCode.VALIDATION_ERROR,
                       retryable=False) from exc
    return (deps, canvas_owner, base, logical_uri, spec, head, sparse_request, preparation,
            output_schema)


def _preflight_response(preparation, base, head, output_schema, rules, identity_columns, logical_uri):
    evidence = json.loads(preparation.documents["evidence"])
    declared = metadb.catalog_declared_keys([logical_uri]).get(logical_uri, [])
    def counts(value: dict) -> MergeColumnsCoverageCountsV1:
        return MergeColumnsCoverageCountsV1(
            rows=value["rows"], unique_identities=value["uniqueIdentities"],
            null_rows=value["nullRows"], duplicate_groups=value["duplicateGroups"],
            duplicate_rows=value["duplicateRows"])
    return MergeColumnsPreflightV1(
        base=base, declared_key=list(declared), identity_columns=list(identity_columns),
        coverage=MergeColumnsCoverageV1(
            base=counts(evidence["base"]), candidate=counts(evidence["candidate"]),
            matched_identities=evidence["matchedIdentities"],
            missing_identities=evidence["missingIdentities"],
            extra_identities=evidence["extraIdentities"], status=evidence["status"]), rules=rules,
        expected_head=ExactDatasetRef(kind="exact", dataset_id=str(head["dataset_id"]),
                                      revision_id=str(head["revision_id"])),
        output_schema=output_schema, provenance=MergeColumnsPreflightProvenanceV1(),
        eligible=evidence["status"] == "complete",
    )


def _task_view(task_id: str, uid: str) -> MergeColumnsTaskV1:
    value = metadb.merge_columns_task_view(task_id, uid)
    if value is None:
        raise APIError(404, "merge-columns task not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    raw = value.get("mergeColumns")
    return MergeColumnsTaskV1(
        task_id=str(value["taskId"]), status=str(value["status"]),
        can_retry=bool(value["canRetry"]), can_cancel=bool(value["canCancel"]),
        merge_columns=DurableMergeColumnsView.model_validate(raw) if raw is not None else None)


@router.post("/merge-columns/preflight", response_model=MergeColumnsPreflightV1)
def preflight(request: MergeColumnsRequestV1, uid: str = Depends(current_user)) -> MergeColumnsPreflightV1:
    _deps, _owner, base, logical_uri, _spec, head, _sparse, preparation, output_schema = _prepared(
        request, uid)
    return _preflight_response(preparation, base, head, output_schema, request.rules,
                               request.identity_columns, logical_uri)


@router.post("/merge-columns", response_model=MergeColumnsTaskV1)
def submit(request: MergeColumnsRequestV1, uid: str = Depends(current_user)) -> MergeColumnsTaskV1:
    task_id = metadb.durable_task_submission_id(uid, str(request.graph.id), request.submission_id)
    existing = metadb.durable_task(task_id, include_admission=False)
    if existing is not None:
        # Replays retain the frozen durable work, but they do not let a collaborator
        # who has since lost Canvas access trigger another dispatch.
        _task_view(task_id, uid)
        # Compare only caller-owned canonical semantics; do not reopen a moved source/head on replay.
        current = metadb.merge_columns_task_request_sha256(task_id)
        if current != _request_sha256(request):
            raise APIError(409, "merge-columns submission request changed", code=APIErrorCode.CONFLICT,
                           retryable=False)
        dispatch(task_id, get_deps())
        return _task_view(task_id, uid)
    (deps, canvas_owner, base, logical_uri, spec, head, sparse_request, _preparation,
     output_schema) = _prepared(
        request, uid)
    if json.loads(_preparation.documents["evidence"])["status"] != "complete":
        raise APIError(409, "merge-columns requires complete identity coverage",
                       code=APIErrorCode.CONFLICT, retryable=False)
    sparse_id: str | None = None
    try:
        sparse = admit_sparse_output(deps.storage, sparse_request)
        sparse_id = sparse.id
        materialized = materialize_sparse_output(
            deps.storage, sparse.id,
            hashlib.sha256(f"merge-columns-v1:{task_id}".encode()).hexdigest()[:32])
        if not materialized.committed:
            raise SparseOutputMaterializationConflict("SparseOutput materialization did not commit")
        committed = metadb.reconcile_sparse_output_materialization(sparse.id)
        if committed is None or committed.get("phase") != "committed":
            raise SparseOutputMaterializationConflict("SparseOutput materialization is unavailable")
        key = f"merge-columns:{request.graph.id}:{request.submission_id}"
        intent = MergeColumnsIntentV1(
            base=base, sparse_output_id=sparse.id,
            sparse_evidence=sparse_output_merge_evidence(sparse.document, committed),
            rules=request.rules, output_schema=output_schema,
            write_intent=WriteIntent(
                destination=WriteDestination(logical_uri=logical_uri, name=spec.name,
                                             dataset_id=base.dataset_id),
                mode="replace", expected_schema=output_schema, expected_head=base,
                idempotency_key=key,
                # The source and destination are the same logical dataset.  Ordinary catalog
                # parent URIs would self-reference the moving head and are rejected by the lineage
                # CAS.  The exact base is instead bound by expected_head and the private immutable
                # merge publication record, both of which become the receipt's exact parent_head.
                provenance=WriteProvenance(publication=LineagePublication(
                    idempotency_key=key, provenance="manual", producer="merge-columns",
                    producer_version=1, step_id="merge-columns"), parents=[]),
            ),
        )
        task, _created = metadb.submit_merge_columns_task(
            uid=uid, canvas_id=str(request.graph.id), submission_id=request.submission_id,
            intent=intent, sparse_owner_id=canvas_owner, request_sha256=_request_sha256(request))
    except metadb.DurableTaskSubmissionConflict as exc:
        # A post-commit response loss may leave the deterministic Task durable.  Reconcile it before
        # surfacing a failure; never tear down a retained sidecar in an unconditional finally block.
        existing = metadb.durable_task(task_id, include_admission=False)
        if existing is not None:
            if metadb.merge_columns_task_request_sha256(task_id) == _request_sha256(request):
                dispatch(task_id, deps)
                return _task_view(task_id, uid)
        if sparse_id is not None:
            metadb.release_unclaimed_merge_sparse_output(sparse_id)
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from None
    except (SparseOutputError, MergeColumnsError, ValueError) as exc:
        raise APIError(422, "merge-columns submission is invalid", code=APIErrorCode.VALIDATION_ERROR,
                       retryable=False) from exc
    dispatch(task["id"], deps)
    return _task_view(task["id"], uid)


@router.get("/merge-columns/{task_id}", response_model=MergeColumnsTaskV1)
def status(task_id: str, uid: str = Depends(current_user)) -> MergeColumnsTaskV1:
    return _task_view(task_id, uid)


@router.post("/merge-columns/{task_id}/cancel", response_model=MergeColumnsTaskV1)
def cancel(task_id: str, uid: str = Depends(current_user)) -> MergeColumnsTaskV1:
    if metadb.cancel_merge_columns_task(task_id, uid) is None:
        raise APIError(404, "merge-columns task not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    return _task_view(task_id, uid)


class _RetryRequest(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    retry_request_id: str = Field(min_length=1, max_length=256)


@router.post("/merge-columns/{task_id}/retry", response_model=MergeColumnsTaskV1)
def retry(task_id: str, request: _RetryRequest, uid: str = Depends(current_user)) -> MergeColumnsTaskV1:
    try:
        retried = metadb.retry_merge_columns_task(task_id, uid, request.retry_request_id)
    except ValueError as exc:
        raise APIError(409, str(exc), code=APIErrorCode.CONFLICT, retryable=False) from None
    if retried is None:
        raise APIError(404, "merge-columns task not found", code=APIErrorCode.NOT_FOUND, retryable=False)
    dispatch(task_id, get_deps())
    return _task_view(task_id, uid)
