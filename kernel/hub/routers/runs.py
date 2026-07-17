"""Compile / preview / schema / estimate / run / cancel, plus destinations and the agent —
the execution routes (and where a run writes). Split out of main.py; all authed at include time.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from hub import auth, compiler, db, destinations, metadb, placement
from hub import graph as graph_mod
from hub.agent import AgentCredentialError, agent_credential_error_status, agent_status, run_agent
from hub.api_errors import APIError, APIErrorCode
from hub.backends import (
    BackendStatusUnavailable, DatasetRevisionAdapter,
    backend_supports_admitted_input_manifests, backend_supports_named_multi_output_runs,
)
from hub.deps import get_deps
from hub.executors.preview import preview_node
from hub.executors.profile import profile_node
from hub.executors.schema import schema_for_graph, schema_for_graph_ports
from hub.plugins.adapters import revision_adapter_for_uri
from hub.run_outputs import (
    UnsupportedRunOutputs, expected_run_outputs, preflight_run_output_target,
    require_single_run_output,
)
from hub.security import current_user
from hub.settings import settings
from hub.storage import ManagedSourceReadError, source_read_scope
from hub.models import (
    CompilePlan,
    CompileRequest,
    EstimateRequest,
    InputDrift,
    InputDriftRequest,
    InputDriftSource,
    JoinAnalysis,
    PreviewRequest,
    ProfileEstimate,
    ProfileEstimateRequest,
    ProfileIdentity,
    ProfileIdentityRequest,
    ProfileJobRequest,
    ProfileResult,
    RunEstimate,
    RunRequest,
    RunOutput,
    RunStatus,
    SampleResult,
    dataset_ref_identity,
)

router = APIRouter()

_RUN_INDEX_MAX = 1000  # cap deps.run_index (run_id -> owning runner); well above either runner's own cap
_RUN_MUTATE_ROLES = ("owner", "editor")
_EXPORT_CHUNK_BYTES = 1024 * 1024
_RUN_OUTPUT_SAMPLE_ROW_BUDGET = 2_000
_EXPORT_MEDIA_TYPES = {
    ".parquet": "application/vnd.apache.parquet",
    ".arrow": "application/vnd.apache.arrow.file",
    ".feather": "application/vnd.apache.arrow.file",
    ".ipc": "application/vnd.apache.arrow.file",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".json": "application/json",
}


def _local_run_intent_sha256(
        graph, target_node_id: str | None,
        input_manifest: list[dict[str, str]] | None = None) -> str:
    """Hash caller intent before source resolution so a retry cannot be retargeted by a moved head."""
    doc = graph.model_dump(mode="json")
    payload = json.dumps(
        {"graph": doc, "target_node_id": target_node_id, "input_manifest": input_manifest},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _local_run_source_nodes(graph, target_node_id: str | None):
    """Return execution-cone Sources in graph order; duplicate node ids are rejected upstream."""
    cone = graph_mod.upstream_chain(graph, target_node_id) if target_node_id else graph.nodes
    return [node for node in cone if node.type == "source"]


def _resolve_local_run_manifest(graph, target_node_id: str | None, deps) -> list[dict[str, str]]:
    """Resolve every local-run Source once through its registered exact-revision provider."""
    resolved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest: list[dict[str, str]] = []
    for node in _local_run_source_nodes(graph, target_node_id):
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        uri = str(cfg.get("uri") or "")
        binding = metadb.catalog_revision_binding_for_uri(uri)
        adapter = revision_adapter_for_uri(uri, deps.resolve_adapter)
        if binding is None or not isinstance(adapter, DatasetRevisionAdapter):
            raise APIError(410, "local_run_input_revision_unavailable",
                           code=APIErrorCode.RESOURCE_GONE, retryable=False)
        dataset_ref = cfg.get("datasetRef")
        try:
            if isinstance(dataset_ref, dict):
                dataset_id, revision_id = dataset_ref_identity(dataset_ref)
                if str(binding["dataset_id"]) != dataset_id:
                    raise ValueError("selected dataset identity does not match the current registration")
                with db.base_guard():
                    adapter.open_revision(uri, revision_id)
            else:
                resolved = adapter.resolve_revision(uri)
                revision_id = str(resolved.get("revision_id") or "")
        except Exception as exc:  # missing pins and provider errors never permit a fallback to head
            raise APIError(410, "local_run_input_revision_unavailable",
                           code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
        provider = str(getattr(adapter, "name", "") or "")
        if not revision_id or not provider:
            raise APIError(410, "local_run_input_revision_unavailable",
                           code=APIErrorCode.RESOURCE_GONE, retryable=False)
        manifest.append({
            "node_id": str(node.id), "dataset_id": str(binding["dataset_id"]),
            "revision_id": revision_id, "provider": provider, "resolved_at": resolved_at,
        })
    return manifest


def _bind_local_run_manifest(
        graph, manifest: list[dict[str, str]], deps, target_node_id: str | None = None):
    """Reopen persisted exact bindings and attach them only to the dispatch copy of a graph."""
    from hub.local_run_inputs import LocalRunInputError, bind_manifest

    try:
        return bind_manifest(graph, target_node_id, manifest, deps.resolve_adapter)
    except LocalRunInputError as exc:
        unavailable = "unavailable" in str(exc)
        raise APIError(
            410 if unavailable else 409,
            "local_run_input_revision_unavailable" if unavailable
            else "local_run_input_manifest_does_not_match_graph",
            code=APIErrorCode.RESOURCE_GONE if unavailable else APIErrorCode.INVALID_REQUEST,
            retryable=False,
        ) from exc


def _inspection_manifest_graph(
        graph, target_node_id: str | None, supplied: list[dict[str, str]] | None, deps,
) -> tuple[object, list[dict[str, str]] | None]:
    """Reuse the local-run manifest contract for one exact inspector input set.

    Registered revision providers are resolved once and immediately reopened. Existing unversioned
    inspector behavior remains available when no manifest can be minted, but a caller-supplied or
    explicitly pinned binding always fails closed instead of substituting latest.
    """
    pinned = any(
        isinstance(node.data.get("config"), dict)
        and node.data["config"].get("datasetRef") is not None
        for node in _local_run_source_nodes(graph, target_node_id)
    )
    try:
        with source_read_scope(
                deps.storage, graph_mod.execution_source_uris(graph, target_node_id),
                owner=f"inspection-manifest:{uuid.uuid4().hex}"):
            manifest = supplied if supplied is not None else _resolve_local_run_manifest(
                graph, target_node_id, deps)
            return _bind_local_run_manifest(
                graph, manifest, deps, target_node_id), manifest
    except ManagedSourceReadError as exc:
        raise HTTPException(400, str(exc)) from exc
    except APIError:
        if supplied is not None or pinned:
            raise
        return graph, None


def _input_drift(
        graph, target_node_id: str, preview_manifest: list[dict[str, str]], deps) -> InputDrift:
    """Compare exact preview inputs with latest heads while keeping the preview binding untouched."""
    from hub.local_run_inputs import LocalRunInputError, validate_manifest_graph

    try:
        retained = validate_manifest_graph(
            graph, target_node_id, preview_manifest, require_bound_revisions=False)
    except LocalRunInputError as exc:
        raise APIError(
            409, "local_run_input_manifest_does_not_match_graph",
            code=APIErrorCode.INVALID_REQUEST, retryable=False,
        ) from exc
    try:
        latest = _resolve_local_run_manifest(graph, target_node_id, deps)
    except APIError:
        # A removed/replaced registration is itself drift. Keep the retained side inspectable and
        # report latest as unavailable instead of turning comparison into an opaque route failure.
        latest = []
    latest_by_node = {item["node_id"]: item for item in latest}
    sources: list[InputDriftSource] = []
    for item in retained:
        current = latest_by_node.get(item["node_id"])
        if (current is not None
                and current["dataset_id"] == item["dataset_id"]
                and current["revision_id"] == item["revision_id"]):
            continue
        readable = False
        compatibility = None
        binding = metadb.catalog_revision_binding(item["dataset_id"])
        if binding is not None:
            adapter = revision_adapter_for_uri(str(binding["uri"]), deps.resolve_adapter)
            if isinstance(adapter, DatasetRevisionAdapter):
                try:
                    with db.base_guard():
                        before = adapter.revision_detail(
                            str(binding["uri"]), item["revision_id"], preview_limit=1)
                        readable = True
                        if (current is not None
                                and current["dataset_id"] == item["dataset_id"]):
                            after = adapter.revision_detail(
                                str(binding["uri"]), current["revision_id"], preview_limit=1)
                            compatibility = metadb.diff_columns(
                                before["columns"], after["columns"])
                except Exception:
                    # Drift must remain inspectable when retention has already removed the old input.
                    # The subsequent exact run still fails closed with the stable 410 admission error.
                    readable = False
                    compatibility = None
        sources.append(InputDriftSource(
            node_id=item["node_id"], dataset_id=item["dataset_id"],
            preview_revision_id=item["revision_id"],
            latest_revision_id=(current["revision_id"] if current is not None else None),
            old_revision_readable=readable, compatibility=compatibility,
        ))
    return InputDrift(drifted=bool(sources), sources=sources)
_EXPORT_OPENAPI_CONTENT = {
    media_type.split(";", 1)[0]: {"schema": {"type": "string", "format": "binary"}}
    for media_type in sorted(set(_EXPORT_MEDIA_TYPES.values()))
}
_EXPORT_OPENAPI_HEADERS = {
    "Cache-Control": {
        "description": "private, no-store; full-result bytes are never cached by shared intermediaries.",
        "schema": {"type": "string"},
    },
    "Content-Disposition": {
        "description": "A sanitized filename ending in -full-result plus the native extension.",
        "schema": {"type": "string"},
    },
    "Content-Length": {
        "description": "Exact native artifact byte size when available.",
        "schema": {"type": "integer"},
    },
    "X-Content-Type-Options": {
        "description": "Always nosniff for downloaded native data.",
        "schema": {"type": "string", "enum": ["nosniff"]},
    },
    "X-Data-Scope": {
        "description": "Always full-result for this route.",
        "schema": {"type": "string", "enum": ["full-result"]},
    },
}


class RunOutputSampleRequest(BaseModel):
    """A run-owned artifact page request; the URI is resolved server-side from durable metadata."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    node_id: str = Field(min_length=1, max_length=256)
    port_id: str = Field(min_length=1, max_length=128)
    k: int = Field(default=50, ge=0, le=_RUN_OUTPUT_SAMPLE_ROW_BUDGET)
    offset: int = Field(default=0, ge=0, lt=_RUN_OUTPUT_SAMPLE_ROW_BUDGET)


class _ExportNotAcceptable(RuntimeError):
    """The durable artifact is valid, but cannot be represented as one native byte stream."""


class _ExportResources:
    """Idempotent response-owned lifecycle scope.

    StreamingResponse may finish through normal exhaustion, cancellation, or an ASGI disconnect. The
    outer response ``finally`` is the authoritative cleanup boundary; iterator cleanup is only an eager
    fast path.
    """

    def __init__(self, stack: contextlib.ExitStack):
        self._stack = stack
        self._lock = threading.RLock()
        self._closed = False
        self._closing_thread_id: int | None = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            current_thread_id = threading.get_ident()
            if self._closing_thread_id == current_thread_id:
                # Closing the ExitStack can finalize the response body iterator, whose ``finally``
                # calls back into this owner. The same-thread recursion must not wait on itself or
                # start a second traversal of callbacks that are already being closed.
                return
            self._closing_thread_id = current_thread_id
            try:
                # Keep the lock until every callback has completed. A concurrent response-finally caller
                # must not observe "closed" and return while the iterator thread still owns the FD/lease.
                self._stack.close()
            except Exception:  # storage-owned guards retain uncertain cleanup for bounded retry/expiry
                logging.getLogger("hub").exception("full-result export resource cleanup is pending")
            finally:
                self._closed = True
                self._closing_thread_id = None


class _OwnedStreamingResponse(StreamingResponse):
    """A stream whose lease/file owner is released even when ASGI cancels the body iterator."""

    def __init__(self, *args, resources: _ExportResources, **kwargs):
        super().__init__(*args, **kwargs)
        self._resources = resources

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._resources.close()


def _require_graph_read_access(graph, uid: str) -> tuple[str | None, str | None]:
    """Authorize a caller-supplied graph by its saved-canvas identity before touching its sources.

    In shared/auth mode, graph analysis is only meaningful inside a real canvas the caller can read;
    owner, editor, and viewer are all read roles. Unknown ids and private canvases both return 404 so
    the endpoint does not become a canvas-enumeration oracle. Open single-user mode keeps supporting
    ad-hoc graphs. This is an identity check only: pinning the payload to a saved revision is separate.
    """
    if not auth.auth_enabled():
        return None, None
    cid = graph.get("id") if isinstance(graph, dict) else getattr(graph, "id", None)
    cid = str(cid or "")
    role = metadb.canvas_role(cid, uid) if cid else None
    if role is None:
        raise APIError(
            404,
            f"canvas '{cid}' not found",
            code=APIErrorCode.CANVAS_NOT_FOUND,
            retryable=False,
        )
    return cid, role


def _invalid_graph(graph, deps, target_node_id: str | None = None) -> tuple[str, bool] | None:
    """Compatibility wrapper around the shared graph-ingress validator."""
    return graph_mod.validation_error(
        graph, deps.node_specs, deps.node_builders, target_node_id)


def _reject_invalid(graph, deps, target_node_id: str | None = None) -> None:
    """400 on any graph that compile would reject."""
    invalid = _invalid_graph(graph, deps, target_node_id)
    if invalid:
        raise APIError(
            400,
            invalid[0],
            code=APIErrorCode.INVALID_GRAPH,
            retryable=False,
        )


def _inspection_port(graph, node_id: str, port_id: str | None, deps) -> str:
    """Validate a single-relation request before it can acquire a source or compute resource."""
    try:
        return graph_mod.require_output_port(graph, node_id, deps.node_specs, port_id).id
    except KeyError as exc:
        raise APIError(
            400, str(exc).strip("'"), code=APIErrorCode.OUTPUT_PORT_NOT_FOUND,
            retryable=False,
        ) from exc
    except ValueError as exc:
        raise APIError(
            400, str(exc), code=APIErrorCode.OUTPUT_PORT_REQUIRED,
            retryable=False,
        ) from exc


def _require_single_backend_output(graph, node_id: str, deps) -> None:
    try:
        require_single_run_output(graph, node_id, deps.node_specs)
    except ValueError as exc:
        raise APIError(
            400, str(exc), code=APIErrorCode.MULTI_OUTPUT_UNSUPPORTED,
            retryable=False,
        ) from exc


def _run_output_preflight(plan, requested_target: str | None) -> str | None:
    try:
        output_target = preflight_run_output_target(plan, requested_target)
    except UnsupportedRunOutputs as exc:
        raise APIError(
            400, str(exc), code=APIErrorCode.MULTI_OUTPUT_UNSUPPORTED,
            retryable=False,
        ) from exc
    return output_target


def _require_backend_run_output_support(backend, graph, node_id: str, deps) -> bool:
    """Admit a multi-output full run only when its selected execution owner opts in explicitly."""
    outputs = expected_run_outputs(graph, node_id, deps.node_specs)
    if len(outputs) <= 1:
        return False
    if backend_supports_named_multi_output_runs(backend):
        return True
    try:
        backend_name = str(getattr(backend, "name", "unknown"))
    except Exception:  # noqa: BLE001 - diagnostic metadata must not escape the admission error
        backend_name = "unknown"
    backend_name = backend_name.replace("\n", " ").replace("\r", " ")[:120]
    raise APIError(
        400,
        f"Execution backend '{backend_name}' does not yet support multi-output full runs",
        code=APIErrorCode.MULTI_OUTPUT_UNSUPPORTED,
        retryable=False,
    )


def _controller_regions_for_run(
        deps, graph, execution_target: str | None, output_target: str | None,
        sizes: dict, multi_output: bool) -> list:
    """Plan once through the controller's public ownership seam and enforce output capability."""
    regions = deps.controller.plan_for_run(graph, execution_target, sizes=sizes)
    if multi_output and output_target is not None and regions:
        # RunController still has a singular final-publication state machine.  Treat it as the actual
        # owner only when it will really split; collapsed plans continue through the selected runner.
        _require_backend_run_output_support(deps.controller, graph, output_target, deps)
    return regions


def _require_admitted_input_manifest_transport(
        runner, controller, controller_regions: list, uid: str) -> None:
    """Reject an exact-revision run before any selected execution owner can allocate resources."""
    if controller_regions:
        try:
            probe = getattr(controller, "supports_admitted_input_manifests", None)
            supported = bool(probe(controller_regions, uid)) if callable(probe) else False
        except Exception:  # noqa: BLE001 - a broken controller/backend probe fails closed
            supported = False
        owner = controller
    else:
        supported = backend_supports_admitted_input_manifests(runner)
        owner = runner
    if supported:
        return
    try:
        owner_name = str(getattr(owner, "name", "unknown"))
    except Exception:  # noqa: BLE001 - diagnostic metadata must not escape the admission error
        owner_name = "unknown"
    owner_name = owner_name.replace("\n", " ").replace("\r", " ")[:120]
    raise HTTPException(
        400,
        f"Execution transport '{owner_name}' does not support admitted exact-revision input "
        "manifests; no run identity, worker, artifact, or remote job was allocated",
    )


@router.post("/graph/compile", response_model=CompilePlan)
def compile_graph(req: CompileRequest, uid: str = Depends(current_user)) -> CompilePlan:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    invalid = _invalid_graph(req.graph, deps, req.target_node_id)
    if invalid:
        error, acyclic = invalid
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=acyclic, error=error)
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)


@router.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest, uid: str = Depends(current_user)) -> SampleResult:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.node_id)
    manifest: list[dict[str, str]] | None = None
    preview_graph = req.graph
    pinned = any(
        isinstance(node.data.get("config"), dict)
        and node.data["config"].get("datasetRef") is not None
        for node in _local_run_source_nodes(req.graph, req.node_id)
    )
    try:
        if req.input_manifest is not None:
            manifest = req.input_manifest
        else:
            manifest = _resolve_local_run_manifest(req.graph, req.node_id, deps)
        preview_graph = _bind_local_run_manifest(req.graph, manifest, deps, req.node_id)
    except APIError:
        if req.input_manifest is not None or pinned:
            return SampleResult(
                not_previewable=True,
                reason=("selected revision is unavailable" if pinned
                        else "retained preview input revision is unavailable; refresh to latest"),
            )
        # Preview-only paths for unversioned/ad-hoc adapters retain their existing bounded behavior.
        # They simply cannot promise preview-to-run reuse until the Source has provider revision facts.
        manifest = None
        preview_graph = req.graph
    _reject_invalid(preview_graph, deps, req.node_id)
    port_id = _inspection_port(preview_graph, req.node_id, req.port_id, deps)
    k = req.k if req.k is not None else settings.preview_k
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            result = SampleResult(**kb.preview(
                preview_graph, req.node_id, k, max(0, req.offset), port_id))
            result.input_manifest = manifest
            return result
        except Exception as e:  # noqa: BLE001 — kernel unreachable / spawn timeout → a clean error, not a raw 500
            return SampleResult(error=True, reason=f"kernel unavailable: {type(e).__name__}: {e}")
    result = preview_node(preview_graph, req.node_id, k,
                          deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs,
                          offset=max(0, req.offset), storage=deps.storage, port_id=port_id)
    result.input_manifest = manifest
    return result


@router.post("/run/input-drift", response_model=InputDrift)
def input_drift(req: InputDriftRequest, uid: str = Depends(current_user)) -> InputDrift:
    """Report moved Source heads and #125 compatibility without replacing preview inputs."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    _reject_invalid(req.graph, deps, req.target_node_id)
    return _input_drift(req.graph, req.target_node_id, req.input_manifest, deps)


@router.post("/run/profile", response_model=ProfileResult)
def run_profile(req: PreviewRequest, uid: str = Depends(current_user)) -> ProfileResult:
    """Bounded, interactive column statistics over a preview sample.

    Whole-dataset profiles scan the full relation and therefore go through ``/run/profile-job`` instead of
    silently occupying this synchronous preview route.
    """
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.node_id)
    graph, manifest = _inspection_manifest_graph(
        req.graph, req.node_id, req.input_manifest, deps)
    _reject_invalid(graph, deps, req.node_id)
    port_id = _inspection_port(graph, req.node_id, req.port_id, deps)
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            result = ProfileResult(**kb.profile(
                graph, req.node_id, full=False, port_id=port_id))
        except Exception as e:  # noqa: BLE001 — kernel unreachable → a clean error, not a raw 500
            result = ProfileResult(error=True, reason=f"kernel unavailable: {type(e).__name__}: {e}")
    else:
        result = profile_node(graph, req.node_id, deps.resolve_adapter, deps.registry,
                              deps.node_builders, deps.node_specs, full=False,
                              storage=deps.storage, port_id=port_id)
    result.input_manifest = manifest
    return result


def _profile_job_estimate(graph, node_id: str, deps) -> RunEstimate:
    """Estimate the whole-dataset scan and normalize its admission contract.

    The normal local runner intentionally lets an entirely unknown job fail fast. A profile is different:
    it will scan whatever relation the node resolves to, so unknown cost requires explicit confirmation.
    Known-small requires every execution-cone size to be known and all known row/byte signals to remain
    under their gates. Partial known values still drive large-cost admission internally, but are not
    presented on the wire as if they described the complete scan.
    """
    plan = compiler.compile_plan(graph, node_id, deps.registry, deps.node_specs, deps.node_ir)
    rows, byts, sizes = _metadata_only_cone_size(graph, node_id, deps)
    backend = deps.kernel_backend() or deps.runner
    estimate = backend.estimate(plan, rows, byts)
    # A tiny/known terminal output does not make the whole scan known. In particular, metric/aggregate
    # output can be one row while its source has no metadata count. Any unknown node in the executable
    # cone conservatively keeps admission behind confirmation.
    rows_complete = bool(sizes) and all(
        size.rows is not None and size.confidence != "unknown" for size in sizes.values()
    )
    bytes_complete = byts is not None
    unknown = not rows_complete or not bytes_complete
    breakdown = estimate.breakdown or "size unknown"
    if unknown and "some cone sizes unknown" not in breakdown:
        breakdown = f"{breakdown} · some cone sizes unknown"
    if "whole-dataset profile" not in breakdown:
        breakdown = f"{breakdown} · whole-dataset profile"
    updates = {
        "needs_confirm": bool(estimate.needs_confirm or unknown),
        "breakdown": breakdown,
        # Partial known values still enter the backend's admission gate, but the response only exposes a
        # signal that describes the complete cone. In particular, unknown width must not erase an exact
        # metadata row count, nor may the estimator's internal default width become an asserted byte size.
        "rows": rows if rows_complete else None,
        "bytes": byts if bytes_complete else None,
    }
    return estimate.model_copy(update=updates)


def _profile_plan_digest(graph, node_id: str, port_id: str, deps) -> str:
    import uuid
    from hub.profile_identity import profile_plan_digest
    from hub.storage import source_read_scope

    with source_read_scope(
            deps.storage, graph_mod.execution_source_uris(graph, node_id),
            owner=f"profile-identity:{uuid.uuid4().hex}"):
        return profile_plan_digest(graph, node_id, port_id, deps.resolve_adapter)


@router.post("/run/profile-estimate", response_model=ProfileEstimate)
def estimate_full_profile(req: ProfileEstimateRequest,
                          uid: str = Depends(current_user)) -> ProfileEstimate:
    """Preflight a whole-dataset profile without starting any work."""
    import uuid
    from hub.storage import source_read_scope

    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    _reject_invalid(req.graph, deps, req.node_id)
    port_id = _inspection_port(req.graph, req.node_id, req.port_id, deps)
    _require_full_profile_containment(req.graph, req.node_id, deps)
    graph, manifest = _inspection_manifest_graph(
        req.graph, req.node_id, req.input_manifest, deps)
    _reject_invalid(graph, deps, req.node_id)
    # Pin one managed-source generation across both observations. Their internal scopes remain useful
    # in direct-call paths, while this outer lease prevents size and digest from describing two different
    # generations if retention races the endpoint between those calls.
    with source_read_scope(
            deps.storage, graph_mod.execution_source_uris(graph, req.node_id),
            owner=f"profile-preflight:{uuid.uuid4().hex}"):
        estimate = _profile_job_estimate(graph, req.node_id, deps)
        digest = _profile_plan_digest(graph, req.node_id, port_id, deps)
    return ProfileEstimate(
        **estimate.model_dump(), target_port_id=port_id, plan_digest=digest,
        input_manifest=manifest)


@router.post("/run/profile-identity", response_model=ProfileIdentity)
def current_profile_identity(req: ProfileIdentityRequest,
                             uid: str = Depends(current_user)) -> ProfileIdentity:
    """Mint the current profile identity for stale-safe canvas reopen recovery."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    _reject_invalid(req.graph, deps, req.node_id)
    port_id = _inspection_port(req.graph, req.node_id, req.port_id, deps)
    _require_full_profile_containment(req.graph, req.node_id, deps)
    graph, manifest = _inspection_manifest_graph(
        req.graph, req.node_id, req.input_manifest, deps)
    _reject_invalid(graph, deps, req.node_id)
    return ProfileIdentity(
        target_port_id=port_id,
        plan_digest=_profile_plan_digest(graph, req.node_id, port_id, deps),
        input_manifest=manifest,
    )


def _require_full_profile_containment(graph, node_id: str, deps) -> None:
    """Fail closed where the local profile supervisor cannot contain arbitrary workload code."""
    if os.name != "posix":
        raise HTTPException(
            501, "full profiles require POSIX process-group containment on this backend")
    if not auth.auth_enabled():
        return
    from hub.executors.preview import _CODE_CELL_KINDS

    cone = graph_mod.upstream_chain(graph, node_id)
    code_nodes = [node for node in cone if node.type in _CODE_CELL_KINDS]
    plugin_nodes = [node for node in cone if node.type not in deps.builtin_kinds]
    if code_nodes:
        raise HTTPException(
            403,
            "full profiling Python/control-flow cells are disabled in multi-user mode "
            "until a stronger workload-containment backend is configured",
        )
    if plugin_nodes:
        raise HTTPException(
            403,
            "full profiling custom plugin nodes is unsupported in multi-user mode because "
            "shared deployments require stronger workload containment",
        )
    # A built-in source node can still dispatch arbitrary Python/native plugin code through its resolved
    # DatasetAdapter. Only the exact core implementations are trusted under process-group containment;
    # subclasses may override scan/fingerprint and therefore remain third-party execution hooks.
    from hub.ir import resolve_config
    from hub.plugins.adapters import DuckDBAdapter, LanceAdapter

    core_adapter_types = {DuckDBAdapter, LanceAdapter}
    for source in (node for node in cone if node.type == "source"):
        uri = str(resolve_config(source).get("uri") or "")
        if not uri:
            continue
        adapter = deps.resolve_adapter(uri)
        if type(adapter) not in core_adapter_types:
            raise HTTPException(
                403,
                "full profiling a third-party dataset adapter is unsupported in multi-user mode because "
                "shared deployments require stronger workload containment",
            )


@router.post("/run/profile-job", response_model=RunStatus)
def run_full_profile(req: ProfileJobRequest, uid: str = Depends(current_user)) -> RunStatus:
    """Queue a whole-dataset profile with durable ownership, cancellation, and recovery semantics."""
    import uuid

    from hub.observability import (
        AuditAction, AuditOutcome, emit_audit, error_class, get_request_id,
    )
    from hub.storage import source_read_scope

    request_id = get_request_id()
    status: RunStatus | None = None
    run_id: str | None = None
    owner = None
    auth_canvas: str | None = None
    try:
        if auth.auth_enabled():
            cid, role = _require_graph_read_access(req.graph, uid)
            assert cid is not None and role is not None
            if role not in _RUN_MUTATE_ROLES:
                raise HTTPException(403, f"canvas '{cid}' requires owner or editor to start a full profile")
            auth_canvas = cid
        deps = get_deps()
        operational_canvas = str(getattr(req.graph, "id", None) or "canvas")
        submission_id = str(req.submission_id)
        port_id = _inspection_port(req.graph, req.node_id, req.port_id, deps)
        try:
            existing = metadb.lookup_profile_submission(
                submission_id, uid, auth_canvas, operational_canvas,
                req.node_id, port_id, req.plan_digest,
            )
        except metadb.ProfileSubmissionConflict as exc:
            raise HTTPException(409, str(exc)) from exc

        owner = deps.kernel_backend()
        if existing is not None and not existing.should_dispatch:
            # A consumed or terminal submission is adopted from its immutable durable binding before
            # consulting mutable source state. Response-loss replay therefore survives source updates.
            run_id = existing.run_id
            status = RunStatus(**existing.status)
        else:
            graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
            _reject_invalid(req.graph, deps, req.node_id)
            # Port selection is validated before source acquisition or run-id allocation. Immutable
            # replay above adopts only the exact stored node/port/digest identity.
            _require_full_profile_containment(req.graph, req.node_id, deps)
            graph, manifest = _inspection_manifest_graph(
                req.graph, req.node_id, req.input_manifest, deps)
            _reject_invalid(graph, deps, req.node_id)

            # Keep managed inputs pinned from identity minting until the kernel-side process runner has
            # synchronously claimed its own exact-generation leases. This closes the fingerprint->dispatch
            # retirement gap while still letting the child own the long-lived execution leases.
            with source_read_scope(
                    deps.storage, graph_mod.execution_source_uris(graph, req.node_id),
                    owner=f"profile-submit:{uuid.uuid4().hex}"):
                authoritative_digest = _profile_plan_digest(
                    graph, req.node_id, port_id, deps)
                if req.plan_digest != authoritative_digest:
                    if existing is None or not existing.should_dispatch:
                        raise HTTPException(
                            409, "profile preflight is stale; estimate the current graph again")
                    assert existing.admission_token is not None
                    outcome, settled = metadb.settle_profile_submission_failure(
                        existing.run_id, existing.admission_token, uid, auth_canvas,
                        canvas_id=operational_canvas, target_node_id=req.node_id,
                        target_port_id=port_id,
                        plan_digest=req.plan_digest,
                        attempt_order=existing.attempt_order,
                        reason="profile source changed before kernel admission",
                    )
                    if outcome not in ("discarded", "admitted") or settled is None:
                        raise RuntimeError(
                            "stale profile submission identity changed during reconciliation")
                    run_id = existing.run_id
                    status = RunStatus(**settled)
                else:
                    estimate = _profile_job_estimate(graph, req.node_id, deps)
                    if estimate.needs_confirm and not req.confirmed:
                        raise HTTPException(
                            409, "full profile needs confirmation (large or unknown whole-dataset scan)")
                    if owner is None:
                        raise HTTPException(503, "canvas execution kernel is unavailable")

                    try:
                        reservation = metadb.preallocate_or_adopt_profile_run_owner(
                            submission_id, uid, auth_canvas, operational_canvas,
                            req.node_id, port_id, authoritative_digest,
                            input_manifest=manifest, request_id=request_id,
                        )
                    except metadb.ProfileSubmissionConflict as exc:
                        raise HTTPException(409, str(exc)) from exc
                    run_id = reservation.run_id
                    if not reservation.should_dispatch:
                        status = RunStatus(**reservation.status)
                    else:
                        preallocation_token = reservation.admission_token
                        attempt_order = reservation.attempt_order
                        assert preallocation_token is not None
                        keepalive_stop = threading.Event()

                        def _renew_preallocation() -> None:
                            interval = max(1.0, metadb.RUN_PREALLOCATION_TTL_SECONDS / 3)
                            while not keepalive_stop.wait(interval):
                                try:
                                    if not metadb.renew_run_preallocation(
                                            run_id, preallocation_token):
                                        return
                                except Exception:
                                    logging.getLogger("hub").exception(
                                        "profile preallocation lease renewal failed")

                        keepalive = threading.Thread(
                            target=_renew_preallocation, daemon=True,
                            name=f"profile-preallocation-{run_id}",
                        )

                        def _settle_submission_failure() -> tuple[str, dict | None]:
                            last_error: Exception | None = None
                            for retry in range(3):
                                try:
                                    return metadb.settle_profile_submission_failure(
                                        run_id, preallocation_token, uid, auth_canvas,
                                        canvas_id=operational_canvas,
                                        target_node_id=req.node_id,
                                        target_port_id=port_id,
                                        plan_digest=authoritative_digest,
                                        attempt_order=attempt_order,
                                    )
                                except Exception as exc:  # bounded metadata outage reconciliation
                                    last_error = exc
                                    if retry < 2:
                                        time.sleep(0.05 * (retry + 1))
                            raise RuntimeError(
                                "could not reconcile profile submission after kernel command failure"
                            ) from last_error

                        returned: RunStatus | None = None
                        try:
                            try:
                                keepalive.start()
                            except Exception:
                                outcome, admitted = _settle_submission_failure()
                                if outcome not in ("discarded", "admitted") or admitted is None:
                                    raise RuntimeError(
                                        "profile preallocation changed before submission started")
                                status = RunStatus(**admitted)
                            else:
                                try:
                                    import inspect
                                    profile_kwargs = {
                                        "run_id": run_id,
                                        "admission_token": preallocation_token,
                                        "request_id": request_id,
                                    }
                                    if "input_manifest" in inspect.signature(
                                            owner.profile_job).parameters:
                                        profile_kwargs["input_manifest"] = manifest
                                    returned = owner.profile_job(
                                        graph, req.node_id, port_id, authoritative_digest,
                                        **profile_kwargs,
                                    )
                                    if (returned.run_id != run_id
                                            or returned.job_type != "profile"
                                            or returned.target_node_id != req.node_id
                                            or returned.target_port_id != port_id
                                            or returned.plan_digest != authoritative_digest
                                            or returned.profile_attempt_order != attempt_order):
                                        raise RuntimeError(
                                            "execution kernel did not preserve its prebound profile identity")
                                except Exception:
                                    # Wait for a racing consume commit, then adopt it; otherwise retain
                                    # one stable failed logical submission without launching a new run.
                                    outcome, admitted = _settle_submission_failure()
                                    if outcome not in ("discarded", "admitted") or admitted is None:
                                        raise RuntimeError(
                                            "profile submission identity changed during reconciliation")
                                else:
                                    admitted = metadb.admitted_profile_run_status(
                                        run_id, uid, auth_canvas,
                                        canvas_id=operational_canvas,
                                        target_node_id=req.node_id,
                                        target_port_id=port_id,
                                        plan_digest=authoritative_digest,
                                        attempt_order=attempt_order,
                                    )
                                    if admitted is None:
                                        outcome, admitted = _settle_submission_failure()
                                        if outcome != "admitted" or admitted is None:
                                            raise RuntimeError(
                                                "execution kernel did not consume profile admission")
                                    # A proven no-child failure is retained while exact terminal DB
                                    # publication retries; return it immediately without losing identity.
                                    if (returned is not None
                                            and returned.status in ("done", "failed", "cancelled")
                                            and admitted.get("status") in ("queued", "running")):
                                        admitted = returned.model_dump()
                                status = RunStatus(**admitted)
                        finally:
                            keepalive_stop.set()
                            if keepalive.ident is not None:
                                keepalive.join(timeout=1.0)

        assert run_id is not None and status is not None
        if owner is not None:
            deps.run_index[status.run_id] = owner
        deps.run_owner[status.run_id] = uid
        while len(deps.run_index) > _RUN_INDEX_MAX:
            deps.run_index.pop(next(iter(deps.run_index)))
        while len(deps.run_owner) > _RUN_INDEX_MAX:
            deps.run_owner.pop(next(iter(deps.run_owner)))
    except Exception as exc:
        run_id = run_id or getattr(status, "run_id", None)
        emit_audit(
            AuditAction.JOB_SUBMIT, AuditOutcome.FAILURE,
            principal_id=uid, resource_type="run", resource_id=run_id,
            run_id=run_id, request_id=request_id,
            attrs={"job_type": "profile", "error_class": error_class(exc)},
        )
        raise
    assert status is not None
    emit_audit(
        AuditAction.JOB_SUBMIT, AuditOutcome.SUCCESS,
        principal_id=uid, resource_type="run", resource_id=status.run_id,
        run_id=status.run_id, request_id=request_id,
        attrs={"job_type": "profile", "placement": str(status.placement or "local")[:32]},
    )
    return status


@router.post("/graph/schema")
def graph_schema(req: CompileRequest, uid: str = Depends(current_user)) -> dict:
    """Per-node, per-output-port metadata columns for editor inspection and suggestions."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.target_node_id)
    if req.input_manifest is not None and req.target_node_id is None:
        raise HTTPException(400, "schema inputManifest requires targetNodeId")
    graph = req.graph
    if req.input_manifest is not None:
        graph, _manifest = _inspection_manifest_graph(
            req.graph, req.target_node_id, req.input_manifest, deps)
        _reject_invalid(graph, deps, req.target_node_id)
    try:
        return schema_for_graph_ports(graph, deps.resolve_adapter, deps.registry,
                                      deps.node_builders, deps.node_specs, storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))


@router.post("/graph/estimate")
def graph_estimate(req: CompileRequest, uid: str = Depends(current_user)) -> dict:
    """Per-node output-SIZE estimate (rows + confidence) for the card size hint — see hub.estimate.
    Conservative + honest: an unknown count comes back rows=null so the UI shows nothing, not a guess."""
    _require_graph_read_access(req.graph, uid)
    from hub.estimate import estimate_sizes
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    _reject_invalid(req.graph, deps, req.target_node_id)
    try:
        sizes = estimate_sizes(
            req.graph, deps.resolve_adapter, actuals=_actuals_for(req.graph, deps),
            storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — a hint must never 500
        return {}
    return {nid: {"rows": s.rows, "confidence": s.confidence} for nid, s in sizes.items()}


@router.post("/graph/plan")
def graph_plan(req: CompileRequest, uid: str = Depends(current_user)) -> dict:
    """The execution plan for a target: the regions it splits into, each with backend + boundary tier +
    estimated size — the UI 'run plan' preview that makes cost-based placement + tiering visible. A plain
    graph is one 'default' region (runs locally); placement (a cluster backend / engine label / checkpoint)
    splits it. Never 500s."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    _reject_invalid(req.graph, deps, req.target_node_id)
    if not req.target_node_id:
        return {"regions": []}
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)
    try:
        regions = deps.controller.plan_summary(req.graph, req.target_node_id)
        plan = compiler.compile_plan(
            req.graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)
        runner = _route_by_capability(
            deps, deps.pick_runner(plan, uid), req.graph, req.target_node_id)
        warning = _destination_credential_preflight(deps, runner, plan, req.graph)
        if warning is not None and regions:
            region = regions[-1]
            region["preflight"] = [*(region.get("preflight") or []), warning]
        return {"regions": regions}
    except ManagedSourceReadError as e:
        return {"regions": [], "error": str(e)}
    except Exception as e:  # noqa: BLE001 — a preview must never 500
        return {"regions": [], "error": f"{type(e).__name__}: {e}"}


@router.post("/graph/join-analysis", response_model=JoinAnalysis)
def join_analysis(req: CompileRequest, uid: str = Depends(current_user)) -> JoinAnalysis:
    """Catalog-driven join hints for a join node (target_node_id): ranked key suggestions for its
    two inputs (cardinality from measured/grain-derived key uniqueness) + a fan-out warning."""
    _require_graph_read_access(req.graph, uid)
    from hub import relationships as rel
    deps = get_deps()
    _reject_invalid(req.graph, deps, req.target_node_id)
    if not req.target_node_id:
        return JoinAnalysis(note="no join node selected")
    try:
        cols = schema_for_graph(req.graph, deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, storage=deps.storage)
        return rel.analyze_join(
            req.graph, req.target_node_id, cols, deps.catalog, deps.resolve_adapter,
            storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------------------------- #
# Destinations (save/open "places") — local + pluggable object-store backends
# --------------------------------------------------------------------------- #
class BrowseRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    destination_id: str
    path: str = ""


@router.get("/destinations")
def list_destinations() -> dict:
    ws = get_deps().workspace
    return {"destinations": destinations.presets(ws), "backends": destinations.backend_kinds()}


@router.post("/destinations/browse")
def browse_destination(req: BrowseRequest) -> dict:
    return destinations.browse(get_deps().workspace, req.destination_id, req.path)


class MkdirRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    destination_id: str
    path: str = ""
    name: str


@router.post("/destinations/mkdir")
def mkdir_destination(req: MkdirRequest) -> dict:
    return destinations.mkdir(get_deps().workspace, req.destination_id, req.path, req.name)


# --------------------------------------------------------------------------- #
# Agent (optional LLM planner — key stays in the kernel, never the browser)
# --------------------------------------------------------------------------- #
class AgentRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    outcome: str
    graph: dict = {}


@router.get("/agent")
def agent_get_status() -> dict:
    return agent_status()


@router.post("/agent")
def agent_act(req: AgentRequest, uid: str = Depends(current_user)) -> dict:
    # The agent's preview/validate tools can execute the caller-supplied graph, so apply the same
    # read boundary as the explicit graph-analysis routes before checking provider availability.
    _require_graph_read_access(req.graph, uid)
    st = agent_status()
    if not st["available"]:
        return st
    try:
        from hub.observability import get_request_id
        out = run_agent(
            req.outcome,
            req.graph,
            get_deps(),
            principal_id=uid,
            request_id=get_request_id(),
        )
    except AgentCredentialError:
        # The Cred may be deleted/rotated after the preflight status read. Preserve the same stable,
        # non-secret contract instead of turning the race into a 502 with resolver details.
        return agent_credential_error_status()
    except Exception as e:  # noqa: BLE001
        raise APIError(
            502,
            f"agent error: {type(e).__name__}: {e}",
            code=APIErrorCode.UPSTREAM_AGENT_FAILURE,
            retryable=True,
        )
    return {"available": True, **out}


def _actuals_for(graph, deps) -> dict[str, int]:  # noqa: ARG001 — deps kept for signature symmetry
    """Measured per-node rows from the last successful run, kept ONLY for nodes still 'latest' — an
    edited (now 'stale') node's old count would mislead the estimate. Lets a not-yet-run downstream node
    inherit a real upstream count instead of 'unknown'. Best-effort: any hiccup → no actuals."""
    from hub import metadb
    try:
        a = metadb.latest_actuals(getattr(graph, "id", None))
        if not a:
            return {}
        latest = {n.id for n in graph.nodes
                  if (n.data.get("status") if isinstance(n.data, dict) else getattr(n.data, "status", None)) == "latest"}
        return {k: v for k, v in a.items() if k in latest}
    except Exception:  # noqa: BLE001
        return {}


def _cone_size(req_graph, target_node_id, deps) -> "tuple[int | None, int | None, dict]":
    """The largest data volume this run moves — the MAX estimated rows AND bytes across the target's cone
    (source counts + a downstream sample's smaller output). Uses hub.estimate so the confirm-gate, the
    placement policy, and the UI hint all share ONE estimator: also returns the full per-node `sizes` so
    the caller can hand THIS schema+actual-aware estimate to the RunController's placement (else placement
    would re-estimate with coarse default widths and the measured vector/decimal widths would be inert
    there). (None, None, {}) when nothing is countable — the gate then errs toward NOT blocking (an
    uncountable source can't be scanned → fails fast anyway)."""
    from hub.estimate import estimate_sizes
    try:  # per-node schemas sharpen the byte width (else a flat default/row makes the byte gate meaningless)
        schemas = schema_for_graph(req_graph, deps.resolve_adapter, deps.registry,
                                   deps.node_builders, deps.node_specs, storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — schema inference is best-effort; fall back to default widths
        schemas = None
    try:
        sizes = estimate_sizes(req_graph, deps.resolve_adapter, target=target_node_id, schemas=schemas,
                               actuals=_actuals_for(req_graph, deps), storage=deps.storage)
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — a bad estimate must not block the gate
        return None, None, {}
    rows = [s.rows for s in sizes.values() if s.rows is not None]
    byts = [s.bytes for s in sizes.values() if s.bytes is not None]
    return (max(rows) if rows else None), (max(byts) if byts else None), sizes


def _metadata_only_cone_size(
        req_graph, target_node_id, deps) -> "tuple[int | None, int | None, dict]":
    """Admission-safe sizing that never asks a third-party adapter to build a relation.

    ``schema_for_graph`` uses ``scan(limit=0)`` to let DuckDB infer relational schemas. That is cheap for
    the core adapters but is not a portable metadata contract: an eager warehouse/HF adapter may load its
    whole table before applying the outer limit. Whole-dataset profile preflight must therefore use only
    explicit metadata capabilities from ``estimate_sizes``. ``metadata_count`` can establish row bounds,
    but core has no bounded metadata-schema SPI yet. The estimator's schema-less 64-byte fallback is useful
    for ordinary placement hints, not authoritative enough for profile admission, so bytes stay unknown and
    the profile admission policy conservatively requires confirmation.
    """
    from hub.estimate import estimate_sizes
    try:
        sizes = estimate_sizes(
            req_graph, deps.resolve_adapter, target=target_node_id, schemas=None,
            storage=deps.storage,
        )
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception:  # noqa: BLE001 — metadata uncertainty is an explicit unknown admission cost
        return None, None, {}
    rows = [size.rows for size in sizes.values() if size.rows is not None]
    return (max(rows) if rows else None), None, sizes


def _route_by_capability(deps, chosen, graph, target_node_id: str | None = None):
    """Route a declared requirement to region placement or explicit whole-graph admission.

    ``place`` remains the region seam. ``accepts_whole_graph`` lets a durable backend own a pinned graph
    without claiming non-durable region orchestration. If neither seam accepts, retain the chosen
    backend for the existing soft-requirement/local-simulation behavior.
    """
    nodes = graph_mod.upstream_chain(graph, target_node_id) if target_node_id else None
    req = placement.graph_requires(graph, deps.node_specs, nodes=nodes)
    if not (req.cpu or req.gpu or req.gpu_type or req.mem or req.labels):  # no requirement → leave choice
        return chosen

    def _can_place(r):
        return hasattr(r, "place") and r.place(req) is not None

    def _accepts_whole_graph(r):
        accepts = getattr(r, "accepts_whole_graph", None)
        return callable(accepts) and bool(accepts(req))

    if _can_place(chosen) or _accepts_whole_graph(chosen):
        return chosen
    return next(
        (r for r in deps.runners if _can_place(r) or _accepts_whole_graph(r)), chosen
    )


def _require_satisfiable_hard_requirements(deps, graph, target_node_id: str | None = None) -> None:
    """Reject a GPU/label pin when no registered backend can actually honor it.

    CPU and memory remain local-engine hints: the existing out-of-core runner can time-share and spill.
    GPU, GPU type, and placement labels instead promise a capability local execution does not have, so
    silently falling back would make a plugin's declared contract untrue.
    """
    nodes = graph_mod.upstream_chain(graph, target_node_id) if target_node_id else None
    req = placement.graph_requires(graph, deps.node_specs, nodes=nodes)
    if not (req.gpu or req.gpu_type or req.labels):
        return

    def accepts(r) -> bool:
        try:
            if hasattr(r, "place") and r.place(req) is not None:
                return True
            whole = getattr(r, "accepts_whole_graph", None)
            return callable(whole) and bool(whole(req))
        except Exception:  # a broken placement probe cannot authorize an unsupported run
            return False

    if any(accepts(r) for r in deps.runners):
        return
    parts = []
    if req.gpu or req.gpu_type:
        parts.append(f"{req.gpu or 1}×{req.gpu_type or 'gpu'}")
    parts.extend(f"{key}={value}" for key, value in (req.labels or {}).items())
    raise HTTPException(400, "no registered backend can satisfy required resources: " + " · ".join(parts))


def _destination_credential_preflight(deps, runner, plan, graph) -> str | None:
    from hub.backends import destination_credential_error
    return destination_credential_error(runner, plan, graph, deps.workspace)


def _require_destination_credential_preflight(deps, runner, plan, graph) -> None:
    message = _destination_credential_preflight(deps, runner, plan, graph)
    if message is not None:
        raise HTTPException(400, message)


@router.post("/run/estimate", response_model=RunEstimate)
def run_estimate(req: EstimateRequest, uid: str = Depends(current_user)) -> RunEstimate:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.target_node_id)
    graph = (_bind_local_run_manifest(req.graph, req.input_manifest, deps, req.target_node_id)
             if req.input_manifest is not None else req.graph)
    _reject_invalid(graph, deps, req.target_node_id)
    plan = compiler.compile_plan(graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    _require_satisfiable_hard_requirements(deps, graph, req.target_node_id)
    output_target = _run_output_preflight(plan, req.target_node_id)
    runner = _route_by_capability(
        deps, deps.pick_runner(plan, uid), graph, req.target_node_id)
    multi_output = False
    if output_target is not None:
        multi_output = _require_backend_run_output_support(
            runner, graph, output_target, deps)
    _require_destination_credential_preflight(deps, runner, plan, graph)
    rows, byts, sizes = _cone_size(graph, req.target_node_id, deps)
    if multi_output:
        _controller_regions_for_run(
            deps, graph, req.target_node_id, output_target, sizes, multi_output=True)
    est = runner.estimate(plan, rows, byts)
    return est


class RunNeedsConfirm(Exception):
    """The confirm gate tripped (large/unknown size) and the caller didn't pass confirmed=True. Carries
    the estimate so the caller can surface estRows/reason. HTTP maps it to 409; the MCP tool returns a
    needsConfirm result. Raising (not returning) keeps `start_run` a single 'started, here's the owner'
    contract for both surfaces."""

    def __init__(self, estimate: RunEstimate):
        super().__init__("run needs confirmation")
        self.estimate = estimate


def start_run(deps, graph, target_node_id: str | None, uid: str, confirmed: bool = False,
              submission_id: str | None = None,
              input_manifest: list[dict[str, str]] | None = None):
    """Start a run — the ONE code path behind both POST /run and the MCP run_canvas tool, so a run an
    agent launches is placed, gated, and owned exactly like one the browser launches. Resolves source
    refs, rejects an invalid/cyclic graph (HTTPException), sizes + gates the run (RunNeedsConfirm), then
    hands to the RunController (placement-splitting) or the base runner and records the owner in
    run_index. Returns (status, owner); poll the owner via _status_or_lost / cancel via run_index."""
    # A run is a mutation of a saved canvas's operational state/history, so auth mode requires a REAL
    # reachable canvas plus owner/editor (viewer is read-only). Only open single-user mode keeps ad-hoc
    # graph execution. Authorize before resolving or compiling so an invented/private id cannot make the
    # server touch caller-selected sources through POST /run after the read routes have been closed.
    auth_canvas = None
    if auth.auth_enabled():
        cid, role = _require_graph_read_access(graph, uid)
        assert cid is not None and role is not None  # auth mode returns one authoritative role read
        if role not in _RUN_MUTATE_ROLES:
            raise HTTPException(403, f"canvas '{cid}' requires owner or editor to run")
        auth_canvas = cid  # a real writable canvas → all collaborators may observe this run
    # Capture caller intent before catalog references are resolved or private exact-revision bindings are
    # attached. Kernel and isolated-local transports mint an id when a non-browser caller has none; the
    # browser-supplied id remains stable across response-loss retries.
    intent_sha256 = _local_run_intent_sha256(graph, target_node_id, input_manifest)
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(graph, deps, target_node_id)
    if input_manifest is not None:
        # Bind before validation/compile/estimate so schema checks and execution see the same exact
        # population as the preview, even if latest moved after the preview was rendered.
        graph = _bind_local_run_manifest(graph, input_manifest, deps, target_node_id)
    _reject_invalid(graph, deps, target_node_id)
    plan = compiler.compile_plan(graph, target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    _require_satisfiable_hard_requirements(deps, graph, target_node_id)
    output_target = _run_output_preflight(plan, target_node_id)
    runner = _route_by_capability(
        deps, deps.pick_runner(plan, uid), graph, target_node_id
    )  # honor requirements only in the target's executable cone
    multi_output = False
    if output_target is not None:
        multi_output = _require_backend_run_output_support(
            runner, graph, output_target, deps)
    _require_destination_credential_preflight(deps, runner, plan, graph)
    rows, byts, sizes = _cone_size(graph, target_node_id, deps)
    controller_regions = (_controller_regions_for_run(
        deps, graph, target_node_id, output_target, sizes, multi_output=True)
        if multi_output else None)
    est = runner.estimate(plan, rows, byts)
    if est.needs_confirm and not confirmed:
        raise RunNeedsConfirm(est)
    if controller_regions is None:
        controller_regions = deps.controller.plan_for_run(
            graph, target_node_id, sizes=sizes)
    if input_manifest is not None:
        _require_admitted_input_manifest_transport(
            runner, deps.controller, controller_regions, uid)
    from hub.observability import (
        AuditAction, AuditOutcome, emit_audit, get_request_id, invoke_backend_run,
    )
    request_id = get_request_id()
    # Admission is shared only by the built-in local transports. Optional/plugin backends and placed
    # controller regions own separate contracts and remain outside this issue.
    from hub.kernel_backend import KernelBackend
    from hub.subprocess_runner import SubprocessRunner

    transport_requires_admission = isinstance(runner, (KernelBackend, SubprocessRunner))
    if transport_requires_admission and submission_id is None:
        submission_id = str(uuid.uuid4())
    local_admission = bool(
        not controller_regions
        and (transport_requires_admission or (submission_id and runner is deps.runner))
    )
    dispatch_graph = graph
    dispatch_manifest: list[dict[str, str]] | None = None
    prebound_local_run_id: str | None = None
    if local_admission:
        assert submission_id is not None
        manifest = (input_manifest if input_manifest is not None
                    else _resolve_local_run_manifest(graph, target_node_id, deps))
        operational_canvas = auth_canvas or (str(getattr(graph, "id", "") or "") or None)
        if operational_canvas is None:
            raise RuntimeError("local run admission requires a persisted canvas")
        prebound_local_run_id, _created = metadb.admit_local_run_inputs(
            uid=uid, canvas_id=operational_canvas, submission_id=str(submission_id),
            target_node_id=target_node_id, intent_sha256=str(intent_sha256), manifest=manifest,
        )
        persisted = metadb.local_run_input_manifest(prebound_local_run_id)
        if persisted is None:
            raise RuntimeError("local run admission was not persisted")
        dispatch_manifest = persisted
        dispatch_graph = _bind_local_run_manifest(graph, persisted, deps, target_node_id)
        if isinstance(runner, KernelBackend):
            # A newly spawned kernel runs boot recovery before serving. Start it only after admission and
            # exact reopen, but before the queued dispatch claim exists; otherwise that boot recovery can
            # correctly mistake the still-unstamped queued row for a dead prior-hub run.
            runner._ensure_kernel(operational_canvas)
        claimed_status, should_dispatch = metadb.claim_local_run_dispatch(
            run_id=prebound_local_run_id, uid=uid, auth_canvas_id=auth_canvas,
            request_id=request_id,
        )
        if not should_dispatch:
            return RunStatus(**claimed_status), deps.runner
    # a run that splits across placement regions (a placed node / checkpoint / fan-out) is owned by the
    # RunController; a single default region returns None → the base runner, exactly as before. Hand it the
    # schema+actual-aware `sizes` we just computed so cost-based placement routes on the SAME measured
    # widths the gate saw — not a second, coarse re-estimate.
    overall = deps.controller.run(
        dispatch_graph, target_node_id, uid, sizes=sizes, request_id=request_id,
        regions=controller_regions)
    identity_prebound = False
    if overall is not None:
        status, owner = overall, deps.controller
    else:
        preallocate = getattr(runner, "preallocate_run_id", None)
        if callable(preallocate):
            run_id = str(preallocate())
            if not run_id:
                raise RuntimeError("execution backend returned an empty preallocated run id")
            operational_canvas = auth_canvas
            if operational_canvas is None:
                graph_id = str(getattr(graph, "id", "") or "")
                if graph_id and metadb.canvas_exists(graph_id):
                    operational_canvas = graph_id
            # This commit is the authorization boundary: external artifacts, workload identity, and
            # submission are forbidden until the logical run has an authoritative principal.
            token = metadb.preallocate_run_owner(
                run_id, uid, auth_canvas, operational_canvas_id=operational_canvas
            )
            keepalive_stop = threading.Event()

            def _renew_preallocation() -> None:
                interval = max(1.0, metadb.RUN_PREALLOCATION_TTL_SECONDS / 3)
                while not keepalive_stop.wait(interval):
                    try:
                        if not metadb.renew_run_preallocation(run_id, token):
                            return
                    except Exception:  # the final bind/finish transaction remains authoritative
                        logging.getLogger("hub").exception(
                            "run preallocation lease renewal failed")

            keepalive = threading.Thread(
                target=_renew_preallocation, daemon=True,
                name=f"run-preallocation-{run_id}",
            )
            try:
                keepalive.start()
                status = runner.run(
                    plan, graph, target_node_id, est.placement, run_id=run_id
                )
                if status.run_id != run_id:
                    raise RuntimeError("execution backend did not preserve its prebound run id")
                if not metadb.finish_run_preallocation(
                        run_id, token, status.model_dump()):
                    raise RuntimeError("execution backend lost its prebound run identity")
                identity_prebound = True
            except BaseException:
                # Exact-token cleanup is a no-op once a backend binding exists. If metadata is
                # temporarily unavailable, boot/periodic recovery expires the same DB-clock lease.
                try:
                    metadb.discard_run_preallocation(
                        run_id, token, uid, auth_canvas)
                except Exception:
                    logging.getLogger("hub").exception(
                        "run preallocation cleanup deferred to recovery")
                raise
            finally:
                keepalive_stop.set()
                if keepalive.ident is not None:
                    keepalive.join(timeout=1.0)
        else:
            try:
                status = invoke_backend_run(
                    runner, plan, dispatch_graph, target_node_id, est.placement,
                    run_id=prebound_local_run_id, request_id=request_id,
                    input_manifest=dispatch_manifest)
            except Exception as exc:
                if prebound_local_run_id is None:
                    raise
                try:
                    # LocalRunner records this receipt before it starts the worker. A missing receipt
                    # therefore proves this invocation did not create a worker; an existing receipt is
                    # an ambiguous response-loss outcome and must be adopted without another dispatch.
                    status = runner.status(prebound_local_run_id)
                except KeyError:
                    metadb.fail_claimed_local_run_dispatch(
                        prebound_local_run_id, f"{type(exc).__name__}: {exc}")
                    raise exc from None
            if prebound_local_run_id is not None and status.run_id != prebound_local_run_id:
                raise RuntimeError("local execution backend did not preserve its admitted run id")
        owner = runner
    if request_id and not status.request_id:
        status.request_id = request_id
    deps.run_index[status.run_id] = owner  # so status/cancel/ws reach the right owner
    deps.run_owner[status.run_id] = uid  # fast in-process creator lookup; auth-mode runs are canvas-bound
    if auth.auth_enabled() and not identity_prebound:
        # Prebound external runs already committed this before allocation; avoid a second bind after a
        # fast terminal publication may have pruned detail and installed the permanent identity fence.
        metadb.bind_run_owner(status.run_id, uid, auth_canvas, request_id=request_id)
    elif not auth.auth_enabled() and request_id:
        # Open mode has no durable owner bind; still stamp request_id for OPS-01 correlation.
        metadb.bind_run_request_id(status.run_id, request_id, canvas_id=auth_canvas or getattr(graph, "id", None))
    emit_audit(AuditAction.JOB_SUBMIT, AuditOutcome.SUCCESS, principal_id=uid,
               resource_type="run", resource_id=status.run_id, run_id=status.run_id,
               request_id=request_id,
               attrs={"placement": str(status.placement or "local")[:32]})
    # bound both (insertion-ordered) so they can't grow for the process lifetime — the runners
    # themselves only retain the last _MAX_RUNS, and _status_or_lost already tolerates a missing id.
    while len(deps.run_index) > _RUN_INDEX_MAX:
        deps.run_index.pop(next(iter(deps.run_index)))
    while len(deps.run_owner) > _RUN_INDEX_MAX:
        deps.run_owner.pop(next(iter(deps.run_owner)))
    return status, owner


@router.post("/run", response_model=RunStatus)
def run(req: RunRequest, uid: str = Depends(current_user)) -> RunStatus:
    try:
        status, _ = start_run(
            get_deps(), req.graph, req.target_node_id, uid, req.confirmed,
            str(req.submission_id) if req.submission_id is not None else None,
            req.input_manifest,
        )
    except RunNeedsConfirm:
        raise HTTPException(409, "run needs confirmation (large or unknown size — a full pass)")
    return status


def _run_read_access(run_id: str, uid: str | None) -> bool:
    """Whether `uid` may observe this run.

    Open mode is one trusted user. In auth mode, the creator or any current collaborator on the REAL
    canvas may read status/output. An ad-hoc run remains private to its creator, and a later
    canvas that reuses the graph id cannot claim it. A compact terminal fence preserves the same policy
    after bounded RunState detail is pruned.
    """
    if not auth.auth_enabled():
        return True
    if not uid:
        return False
    creator, auth_canvas = metadb.run_auth(run_id)
    if creator is not None:
        if creator == uid:
            return True
        return bool(auth_canvas and metadb.canvas_role(auth_canvas, uid) is not None)
    # Missing creator identity falls back to the operational canvas grant.
    cid = metadb.run_canvas_id(run_id)
    if cid:
        return metadb.canvas_role(cid, uid) is not None
    retained = metadb.terminal_run_identity(run_id)
    if retained is not None:
        creator, auth_canvas, cid = retained
        if creator is not None:
            if creator == uid:
                return True
            return bool(auth_canvas and metadb.canvas_role(auth_canvas, uid) is not None)
        return bool(cid and metadb.canvas_role(cid, uid) is not None)
    # Only the short window before a durable bind lands may trust process-local ownership. A retained
    # fence, including an identity-cleared fence after canvas deletion, is authoritative over this cache.
    return get_deps().run_owner.get(run_id) == uid


def _run_mutate_access(run_id: str, uid: str | None) -> bool:
    """Whether `uid` may cancel this run.

    A real-canvas run follows the caller's CURRENT canvas role: owner/editor may mutate; viewer may
    only observe. A legacy ad-hoc run has no canvas role, so its creator remains its sole operator.
    Rows without durable creator metadata fall back to the persisted canvas role. The in-process owner
    is trusted only before either durable RunState identity or a terminal fence exists.
    """
    if not auth.auth_enabled():
        return True
    if not uid:
        return False
    creator, auth_canvas = metadb.run_auth(run_id)
    if creator is not None:
        if auth_canvas:
            return metadb.canvas_role(auth_canvas, uid) in _RUN_MUTATE_ROLES
        return creator == uid
    cid = metadb.run_canvas_id(run_id)
    if cid:
        return metadb.canvas_role(cid, uid) in _RUN_MUTATE_ROLES
    retained = metadb.terminal_run_identity(run_id)
    if retained is not None:
        creator, auth_canvas, cid = retained
        if creator is not None:
            if auth_canvas:
                return metadb.canvas_role(auth_canvas, uid) in _RUN_MUTATE_ROLES
            return creator == uid
        return bool(cid and metadb.canvas_role(cid, uid) in _RUN_MUTATE_ROLES)
    return get_deps().run_owner.get(run_id) == uid


def _require_run_read_access(run_id: str, uid: str) -> None:
    if not _run_read_access(run_id, uid):  # 404, not 403 — don't reveal that someone else's run id exists
        raise HTTPException(404, f"run '{run_id}' not found")


def _require_run_mutate_access(run_id: str, uid: str) -> None:
    if _run_mutate_access(run_id, uid):
        return
    # A viewer can already enumerate this shared run through status/history, so distinguish read-only
    # from not-found. A stranger still gets 404 and learns nothing about the run id.
    if _run_read_access(run_id, uid):
        raise HTTPException(403, f"run '{run_id}' requires canvas owner or editor to cancel")
    raise HTTPException(404, f"run '{run_id}' not found")


def _full_result_export_name(value: str | None, extension: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "result")).strip("._-")
    stem = (stem or "result")[:100]
    for suffix in _EXPORT_MEDIA_TYPES:
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)].rstrip("._-") or "result"
            break
    return f"{stem}-full-result{extension}"


def _request_user(uid: str, user_id: str | None) -> str:
    """Honor iframe-compatible user selection only in trusted open mode.

    Auth mode always ignores the query value and keeps using the signed same-origin session resolved by
    ``current_user``. Open mode has no tenant boundary; preserving the selected dev user merely keeps
    request ownership/diagnostics consistent when a hidden iframe cannot send ``X-DP-User``.
    """
    return uid if auth.auth_enabled() or not user_id else user_id


def _durable_run_outputs(run_id: str) -> list[RunOutput]:
    """Resolve a run's outputs, falling back from its runner to bounded durable history.

    A live runner is freshest. If it no longer owns the id *or its status plane is temporarily
    unavailable*, RunState is authoritative while retained; a RunState miss then falls back to
    RunRecord. The history row's own ``id`` is never queried.
    """
    status_error: Exception | None = None
    try:
        status = _runner_for(run_id).status(run_id)
    except (KeyError, OSError, BackendStatusUnavailable) as exc:
        # Do not catch generic RuntimeError: backends use it for integrity and configuration drift.
        # Only the explicit availability contract may fall back to already-durable metadata.
        status_error = exc
        persisted = metadb.get_run_state(run_id)
        if persisted is not None:
            try:
                status = RunStatus(**persisted)
            except ValueError as exc:
                raise HTTPException(409, "durable run output metadata is invalid") from exc
        else:
            try:
                snapshots = metadb.get_run_record_outputs(run_id)
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            if snapshots is None:
                if not isinstance(status_error, KeyError):
                    raise HTTPException(
                        503, "run status is temporarily unavailable and no durable output is retained",
                    ) from status_error
                raise HTTPException(404, "run output not found")
            try:
                return [RunOutput.model_validate(snapshot) for snapshot in snapshots]
            except ValueError as exc:
                raise HTTPException(409, "durable run output metadata is invalid") from exc
    return list(status.outputs)


def _durable_run_output(run_id: str, node_id: str, port_id: str) -> RunOutput:
    """Resolve one output by logical run/port identity."""
    outputs = _durable_run_outputs(run_id)
    output = next((candidate for candidate in outputs
                   if candidate.node_id == node_id and candidate.port_id == port_id), None)
    if output is None:
        raise HTTPException(404, "run output not found")
    return output


def _committed_run_output(
        run_id: str, node_id: str, port_id: str, *, result_only: bool) -> RunOutput:
    output = _durable_run_output(run_id, node_id, port_id)
    if output.outcome != "committed" or not output.uri:
        raise HTTPException(409, "run output is not a committed materialized artifact")
    if result_only and output.publication_kind != "result":
        raise HTTPException(409, "run output is a catalog publication, not a native result artifact")
    return output


def _object_attempt_member(uri: str) -> tuple[str, int, int] | None:
    """Resolve a committed object-attempt root to its sole shard and manifest row count.

    Returns ``None`` for an ordinary object/file URI. More than one shard is a valid dataset but cannot
    be represented as one native stream or by the current single-relation interactive adapter contract.
    """
    from hub.handoff import is_attempt_uri, read_manifest, validate_shards
    from hub.plugins.adapters import is_object_uri

    if not is_object_uri(uri) or not is_attempt_uri(uri):
        return None
    manifest = read_manifest(uri)
    if manifest is None:
        raise FileNotFoundError("committed object result manifest is missing or invalid")
    if not validate_shards(uri, manifest):
        raise FileNotFoundError("committed object result inventory no longer matches its manifest")
    shards = manifest["shards"]
    if len(shards) != 1:
        raise _ExportNotAcceptable(
            f"native single-stream access is unavailable for a {len(shards):,}-shard result"
        )
    shard = shards[0]
    return f"{uri.rstrip('/')}/{shard['path']}", int(shard["size"]), int(manifest["rows"])


def _prepare_full_result_stream(
        uri: str, storage, owner: str
        ) -> tuple[_ExportResources, object, int | None, str, int | None]:
    """Open one committed native member before headers and retain its lifecycle read fence."""
    from hub import paths
    from hub.plugins.adapters import is_object_uri, object_fs

    stack = contextlib.ExitStack()
    try:
        guards = stack.enter_context(source_read_scope(storage, [uri], owner=owner))
        local = paths.local_path(uri)
        expected_size: int | None = None
        target_uri = uri
        if local is not None:
            if not os.path.isfile(local):
                if os.path.exists(local):
                    raise _ExportNotAcceptable(
                        "native full-result export requires one file, not a directory")
                raise FileNotFoundError(local)
            extension = os.path.splitext(local)[1].lower()
            if extension not in _EXPORT_MEDIA_TYPES:
                raise _ExportNotAcceptable(
                    "native full-result export requires a Parquet, Arrow, CSV, TSV, or JSON file")
            guard = next((candidate for candidate in guards
                          if getattr(candidate, "uri", None) == local), None)
            if guard is not None and callable(getattr(guard, "artifact_fileno", None)):
                fd = os.dup(guard.artifact_fileno())
                os.lseek(fd, 0, os.SEEK_SET)
                stream = stack.enter_context(os.fdopen(fd, "rb"))
            else:
                stream = stack.enter_context(open(local, "rb"))
            size = os.fstat(stream.fileno()).st_size
            return _ExportResources(stack), stream, size, extension, None

        if not is_object_uri(uri):
            raise _ExportNotAcceptable(
                "native full-result export requires one local or object-store file")
        member = _object_attempt_member(uri)
        manifest_rows: int | None = None
        if member is not None:
            target_uri, expected_size, manifest_rows = member
        extension = os.path.splitext(target_uri.rstrip("/"))[1].lower()
        if extension not in _EXPORT_MEDIA_TYPES:
            raise _ExportNotAcceptable(
                "native full-result export requires a Parquet, Arrow, CSV, TSV, or JSON file")
        filesystem, path = object_fs(target_uri)
        info = filesystem.get_file_info(path)
        import pyarrow.fs as pafs

        if info.type != pafs.FileType.File:
            raise FileNotFoundError(target_uri)
        if expected_size is not None and info.size != expected_size:
            raise FileNotFoundError("committed object result shard size no longer matches its manifest")
        stream = stack.enter_context(filesystem.open_input_file(path))
        return (_ExportResources(stack), stream, info.size if info.size >= 0 else None,
                extension, manifest_rows)
    except BaseException:
        try:
            stack.close()
        except Exception:  # preserve the preparation failure as the primary signal
            logging.getLogger("hub").exception("full-result export preparation rollback failed")
        raise


def _full_result_chunks(stream, resources: _ExportResources) -> Iterator[bytes]:
    try:
        while chunk := stream.read(_EXPORT_CHUNK_BYTES):
            yield chunk
    finally:
        resources.close()


def _export_headers(filename: str | None, extension: str, size: int | None) -> dict[str, str]:
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{_full_result_export_name(filename, extension)}"'),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Data-Scope": "full-result",
    }
    if size is not None:
        headers["Content-Length"] = str(size)
    return headers


def _open_run_result_export(
        run_id: str, node_id: str, port_id: str, filename: str | None,
        uid: str) -> tuple[_ExportResources, object, str, dict[str, str]]:
    _require_run_read_access(run_id, uid)
    output = _committed_run_output(run_id, node_id, port_id, result_only=True)
    owner = f"export:{run_id}:{uuid.uuid4().hex}"
    try:
        resources, stream, size, extension, manifest_rows = _prepare_full_result_stream(
            output.uri or "", get_deps().storage, owner)
    except _ExportNotAcceptable as exc:
        raise HTTPException(406, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, "full-result artifact access denied") from exc
    except (FileNotFoundError, ManagedSourceReadError) as exc:
        raise HTTPException(410, "full-result artifact is missing or expired") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(409, f"full-result artifact is not exportable: {exc}") from exc
    if (manifest_rows is not None and output.rows is not None
            and manifest_rows != output.rows):
        resources.close()
        raise HTTPException(409, "run output row metadata does not match its committed manifest")
    return resources, stream, _EXPORT_MEDIA_TYPES[extension], _export_headers(
        filename, extension, size)


@router.post("/run/{run_id}/sample", response_model=SampleResult)
def sample_run_output(
        run_id: str, req: RunOutputSampleRequest,
        uid: str = Depends(current_user)) -> SampleResult:
    """Read one bounded page from an authorized durable output without accepting a caller-owned URI."""
    _require_run_read_access(run_id, uid)
    output = _committed_run_output(
        run_id, req.node_id, req.port_id, result_only=False)
    uri = output.uri or ""
    preview_rows = min(
        _RUN_OUTPUT_SAMPLE_ROW_BUDGET,
        req.offset + req.k + 1,
    )
    from hub import db
    from hub.executors.engine import _table_to_rows
    from hub.plugins.adapters import BoundedPreviewUnsupported, relation_columns

    try:
        with source_read_scope(
                get_deps().storage, [uri], owner=f"run-sample:{run_id}:{uuid.uuid4().hex}"):
            member = _object_attempt_member(uri)
            target_uri = member[0] if member is not None else uri
            manifest_rows = member[2] if member is not None else None
            adapter = get_deps().resolve_adapter(target_uri)
            preview_scan = getattr(adapter, "preview_scan", None)
            if not callable(preview_scan):
                return SampleResult(
                    not_previewable=True,
                    reason=(f"source adapter '{getattr(adapter, 'name', type(adapter).__name__)}' "
                            "does not guarantee a bounded preview"),
                )
            with db.run_scope():
                try:
                    relation = preview_scan(target_uri, None, limit=preview_rows)
                except BoundedPreviewUnsupported as exc:
                    return SampleResult(not_previewable=True, reason=str(exc))
                columns = relation_columns(relation)
                page = _table_to_rows(
                    relation.limit(req.k + 1, req.offset).to_arrow_table())
                rows = page[:req.k]
                metadata_count = getattr(adapter, "metadata_count", None)
                try:
                    metadata_rows = (
                        metadata_count(target_uri) if callable(metadata_count) else None)
                except Exception:  # exact-count uncertainty must never trigger a full scan
                    metadata_rows = None
    except _ExportNotAcceptable as exc:
        return SampleResult(
            not_previewable=True,
            reason=(f"This committed result has multiple storage shards. {exc}. "
                    "Use a native multi-file reader or compact the result first."),
        )
    except PermissionError as exc:
        raise HTTPException(403, "run output artifact access denied") from exc
    except (FileNotFoundError, ManagedSourceReadError) as exc:
        raise HTTPException(410, "run output artifact is missing or expired") from exc
    except Exception as exc:  # noqa: BLE001 - data failures are explicit SampleResult errors
        return SampleResult(error=True, reason=f"{type(exc).__name__}: {exc}")

    # A result output's rows are its complete materialized cardinality. A catalog/write output may be an
    # append, where output.rows is only the mutation count, so it must use artifact metadata instead.
    known_totals = {
        int(total) for total in (
            output.rows if output.publication_kind == "result" else None,
            manifest_rows,
            metadata_rows,
        ) if total is not None
    }
    if len(known_totals) > 1:
        raise HTTPException(409, "run output row metadata is inconsistent")
    exact_total = next(iter(known_totals), None)
    page_end = req.offset + len(rows)
    if exact_total is not None and rows and page_end > exact_total:
        raise HTTPException(409, "run output page exceeds its exact row metadata")
    window_end = min(exact_total, _RUN_OUTPUT_SAMPLE_ROW_BUDGET) \
        if exact_total is not None else None
    capped = page_end >= _RUN_OUTPUT_SAMPLE_ROW_BUDGET and (
        exact_total is None or exact_total > page_end)
    if capped:
        has_more: bool | None = False
    elif len(page) > req.k:
        has_more = True
    elif window_end is not None:
        has_more = window_end > page_end
    else:
        # A short bounded adapter batch is not an EOF contract. Without metadata or a look-ahead row,
        # another page is genuinely unknown rather than false.
        has_more = None
    complete = (
        req.offset == 0 and exact_total is not None and page_end == exact_total
        and has_more is False and not capped)
    completeness = (
        "complete" if complete else
        "capped" if capped else
        "page" if exact_total is not None or has_more is True else
        "unknown"
    )
    truncated = False if complete else (
        req.offset > 0 or exact_total is None or exact_total > page_end)
    return SampleResult(
        columns=columns,
        rows=rows,
        row_count=exact_total,
        has_more=has_more,
        truncated=truncated,
        completeness=completeness,
        row_limit=_RUN_OUTPUT_SAMPLE_ROW_BUDGET if capped else None,
        limit_reason="interactive-row-budget" if capped else None,
        limit_scope="result-window" if capped else None,
        wire=output.wire,
    )


@router.get(
    "/run/{run_id}/export",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "Complete native result artifact. The runtime preserves its Parquet, Arrow, CSV, TSV, "
                "or JSON media type and streams without conversion."),
            "content": _EXPORT_OPENAPI_CONTENT,
            "headers": _EXPORT_OPENAPI_HEADERS,
        },
    },
)
def export_run_result(
        run_id: str,
        node_id: str = Query(alias="nodeId", min_length=1, max_length=256),
        port_id: str = Query(alias="portId", min_length=1, max_length=128),
        filename: str | None = Query(default=None, max_length=160),
        user_id: str | None = Query(default=None, alias="userId", max_length=256),
        uid: str = Depends(current_user)) -> StreamingResponse:
    """Stream the complete, already-materialized native artifact without buffering or conversion."""
    resources, stream, media_type, headers = _open_run_result_export(
        run_id, node_id, port_id, filename, _request_user(uid, user_id))
    return _OwnedStreamingResponse(
        _full_result_chunks(stream, resources),
        resources=resources,
        media_type=media_type,
        headers=headers,
    )


@router.head(
    "/run/{run_id}/export",
    response_class=Response,
    responses={
        200: {
            "description": (
                "Full-result export preflight. Performs the same authorization, output resolution, "
                "manifest validation, and native member open as GET; returns its media, filename, and "
                "size headers without a response body."),
            "headers": _EXPORT_OPENAPI_HEADERS,
        },
    },
)
def preflight_run_result_export(
        run_id: str,
        node_id: str = Query(alias="nodeId", min_length=1, max_length=256),
        port_id: str = Query(alias="portId", min_length=1, max_length=128),
        filename: str | None = Query(default=None, max_length=160),
        user_id: str | None = Query(default=None, alias="userId", max_length=256),
        uid: str = Depends(current_user)) -> Response:
    """Authorize and open the exact native member so preflight reports every GET error before download."""
    resources, _stream, media_type, headers = _open_run_result_export(
        run_id, node_id, port_id, filename, _request_user(uid, user_id))
    resources.close()
    return Response(status_code=200, media_type=media_type, headers=headers)


def _runner_for(run_id: str, *, fallback: bool = True):
    """Resolve an in-process owner, including a durable backend reconstructed after restart."""
    deps = get_deps()
    owner = deps.run_index.get(run_id)
    if owner is None:
        binding = metadb.backend_job(run_id)
        if binding:
            owner = next(
                (runner for runner in deps.runners
                 if getattr(runner, "durable_backend", None) == binding.get("backend")),
                None,
            )
            if owner is not None:
                try:
                    owner.status(run_id)  # lazy reattach if startup recovery missed a just-created row
                except KeyError:
                    owner = None
                else:
                    deps.run_index[run_id] = owner
    return owner if owner is not None else (deps.runner if fallback else None)


def _pruned_terminal_status(run_id: str) -> RunStatus | None:
    """Project the permanent terminal fence when bounded status detail has been pruned."""
    terminal = metadb.terminal_run_status(run_id)
    if terminal not in ("done", "failed", "cancelled"):
        return None
    return RunStatus(
        run_id=run_id,
        status=terminal,
        progress=1.0 if terminal == "done" else None,
        error=(
            "Run failed (code=terminal_details_pruned)"
            if terminal == "failed" else None
        ),
    )


def _status_or_lost(run_id: str) -> RunStatus:
    """This run's status, resolved in order: (1) the owning runner's in-memory status (freshest — this
    instance ran it); (2) the shared DB (run_states) — so ANOTHER stateless web instance, or this one
    after a restart, can still answer; (3) a synthetic terminal status. Returning terminal instead of a
    404 lets the client resolve the node cleanly instead of exhausting its retries and stranding it."""
    try:
        return _runner_for(run_id).status(run_id)
    except KeyError:
        from hub import metadb
        persisted = metadb.get_run_state(run_id)
        if persisted is not None:
            return RunStatus(**persisted)
        terminal = _pruned_terminal_status(run_id)
        if terminal is not None:
            return terminal
        return RunStatus(run_id=run_id, status="failed",
                         error="run not found — it was evicted or the kernel restarted")


try:  # a malformed DP_STALL_S must degrade to the default, not crash the whole app at import
    _STALL_S = float(os.environ.get("DP_STALL_S", "120"))  # a running run with no step completed for this long
except ValueError:
    _STALL_S = 120.0


@router.get("/run/{run_id}", response_model=RunStatus)
def run_status(run_id: str, uid: str = Depends(current_user)) -> RunStatus:
    _require_run_read_access(run_id, uid)  # status carries row counts, paths in errors, output names
    st = _status_or_lost(run_id)
    if st.status in ("queued", "running") and metadb.run_stalled(run_id, _STALL_S):
        st = st.model_copy(update={"stalled": True})  # copy — don't mutate the runner's live object
    return st


@router.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str, uid: str = Depends(current_user)) -> RunStatus:
    from hub.observability import AuditAction, AuditOutcome, emit_audit, get_request_id
    _require_run_mutate_access(run_id, uid)  # only owner/editor may disrupt a shared canvas run
    deps = get_deps()
    owner = _runner_for(run_id, fallback=False)
    if owner is not None:
        status = owner.cancel(run_id)  # this instance ran it → cancel in-process
        emit_audit(AuditAction.JOB_CANCEL, AuditOutcome.SUCCESS, principal_id=uid,
                   resource_type="run", resource_id=run_id, run_id=run_id,
                   request_id=get_request_id(),
                   attrs={"status": str(getattr(status, "status", "unknown"))[:32]})
        return status
    # A durable plugin may be absent after restart. Keep the authorized cancel intent in SQL so a
    # recovered plugin can issue the remote stop later; never fake a terminal acknowledgement here.
    binding = metadb.backend_job(run_id)
    if binding is not None:
        persisted = metadb.get_run_state(run_id)
        if persisted is not None and persisted.get("status") in ("queued", "running"):
            metadb.request_backend_cancel(run_id)
            # A terminal publication racing the request remains authoritative over cancellation.
            current = metadb.get_run_state(run_id)
            if current is not None:
                status = RunStatus(**current)
            else:
                terminal = _pruned_terminal_status(run_id)
                status = terminal if terminal is not None else RunStatus(**persisted)
            emit_audit(AuditAction.JOB_CANCEL, AuditOutcome.SUCCESS, principal_id=uid,
                       resource_type="run", resource_id=run_id, run_id=run_id,
                       request_id=get_request_id(),
                       attrs={"status": str(getattr(status, "status", "unknown"))[:32]})
            return status
    # A finished run needs no cancel: return its full persisted detail, or the compact fence only when
    # that detail was genuinely pruned — never let the fence shadow a still-present terminal RunState.
    persisted = metadb.get_run_state(run_id)
    if persisted is not None and persisted.get("status") in ("done", "failed", "cancelled"):
        return RunStatus(**persisted)
    if persisted is None:
        terminal = _pruned_terminal_status(run_id)
        if terminal is not None:
            return terminal
    # not owned here (the hub restarted, or another stateless instance accepted the run) — route via the
    # DB-backed kernel backend, which resolves the owning kernel from run_states and cancels it (or
    # returns the last-known persisted status). Mirrors _status_or_lost so cancel never 404s a live run.
    kb = deps.kernel_backend()
    if kb is not None:
        status = kb.cancel(run_id)
        emit_audit(AuditAction.JOB_CANCEL, AuditOutcome.SUCCESS, principal_id=uid,
                   resource_type="run", resource_id=run_id, run_id=run_id,
                   request_id=get_request_id(),
                   attrs={"status": str(getattr(status, "status", "unknown"))[:32]})
        return status
    if persisted is not None:
        return RunStatus(**persisted)
    raise HTTPException(404, f"run '{run_id}' not found")
