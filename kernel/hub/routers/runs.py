"""Compile / preview / schema / estimate / run / cancel, plus destinations and the agent —
the execution routes (and where a run writes). Split out of main.py; all authed at include time.
"""

from __future__ import annotations

import contextlib
import copy
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

from hub import auth, compiler, db, destinations, metadb, placement, workspace_providers
from hub import graph as graph_mod
from hub.agent import AgentCredentialError, agent_credential_error_status, agent_status, run_agent
from hub.api_errors import APIError, APIErrorCode
from hub.backends import (
    BackendStatusUnavailable, DatasetRevisionAdapter,
    backend_supports_admitted_input_manifests, backend_supports_named_multi_output_runs,
)
from hub.deps import get_deps
from hub.executors.engine import declared_schema
from hub.executors.preview import PREVIEW_SCAN, preview_node
from hub.executors.profile import profile_node
from hub.executors.schema import schema_for_graph, schema_for_graph_ports
from hub.execution_manifest import (
    ExecutionManifestError,
    build_execution_manifest,
    execution_manifest_accepts_graph_replay,
    execution_manifest_admission,
)
from hub.local_run_inputs import LocalRunInputError
from hub.plugins.adapters import (
    RevisionPermissionLost,
    RevisionProviderOffline,
    revision_adapter_for_uri,
)
from hub.run_outputs import (
    UnsupportedRunOutputs, expected_run_outputs, preflight_run_output_target,
    require_single_run_output,
)
from hub.run_parameters import ParameterResolutionError, resolve_graph_parameters
from hub.security import current_user
from hub.settings import settings
from hub.storage import ManagedSourceReadError, source_read_scope
from hub.models import (
    CompilePlan,
    CompileRequest,
    ColumnSchema,
    EstimateRequest,
    ExactDatasetRef,
    Graph,
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
    WriteAdmission,
    WriteAdmissionRequest,
    WriteDestination,
    WriteIntent,
    WritePartitionExpectation,
    WriteProvenance,
    WriteReceipt,
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


def _resolve_parameters(
        graph: Graph, bindings, target: str | None, deps, *, freeze_latest: bool = True) -> Graph:
    try:
        resolved, _canonical = resolve_graph_parameters(
            graph, bindings, target, deps, freeze_latest=freeze_latest)
        return resolved
    except ParameterResolutionError as exc:
        raise APIError(
            422, str(exc), code=APIErrorCode.VALIDATION_ERROR, retryable=False) from exc


def _target_execution_graph(graph: Graph, target: str | None) -> Graph:
    """Restrict one internal inspection pass to its Section-aware execution cone."""
    if target is None:
        return graph
    scoped = graph.model_copy(deep=True)
    all_nodes = list(scoped.nodes)
    roots = graph_mod.upstream_chain(scoped, target)
    selected = {node.id for node in graph_mod.execution_nodes(scoped, roots)}
    scoped.nodes = [node for node in all_nodes if node.id in selected]
    scoped.edges = [
        edge for edge in scoped.edges
        if edge.source in selected and edge.target in selected
    ]
    return scoped


def _admitted_execution_manifest(*args, **kwargs) -> tuple[str, str]:
    try:
        return build_execution_manifest(*args, **kwargs)
    except ExecutionManifestError as exc:
        raise APIError(
            400, str(exc), code=APIErrorCode.INVALID_REQUEST, retryable=False,
        ) from exc


def _resume_durable_task(deps, task: dict) -> RunStatus:
    """Dispatch one already-admitted graph-backed Task through its existing lifecycle."""
    task_id = str(task["id"])
    if task["task_kind"] == "managed_local_write":
        from hub.durable_tasks import dispatch
        dispatch(task_id, deps)
    elif task["task_kind"] == "external_wait":
        from hub.external_wait_tasks import recover
        recover(deps)
    elif task["task_kind"] == "linear_checkpoint_write":
        from hub.linear_checkpoint_tasks import dispatch
        dispatch(task_id, deps)
    elif task["task_kind"] == "bounded_fanout_write":
        from hub.bounded_fanout_tasks import dispatch
        dispatch(task_id, deps)
    else:  # pragma: no cover - schema constraint owns the closed task-kind set
        raise RuntimeError("durable task kind is unsupported")
    current = metadb.durable_task(task_id, include_admission=False)
    if current is None:
        raise RuntimeError("durable task disappeared during replay")
    return RunStatus.model_validate(current["status_doc"])


def _validate_retained_manifest_replay(
        sha256: str, graph, target_node_id: str | None,
        input_manifest: list[dict[str, str]] | None,
        write_intent: WriteIntent | None) -> tuple[dict, str]:
    """Compare one canonical retry intent with retained exact admission without mutable reads."""
    retained = metadb.execution_manifest(sha256)
    if retained is None:
        raise RuntimeError("durable task execution manifest is unavailable")
    payload = json.dumps(
        retained["document"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    try:
        if not execution_manifest_accepts_graph_replay(
                sha256, payload, graph,
                target_node_id=target_node_id, target_port_id=None):
            raise metadb.DurableTaskSubmissionConflict(
                "submission is bound to a different execution manifest")
        admission = execution_manifest_admission(sha256, payload)
    except ExecutionManifestError as exc:
        raise RuntimeError("durable task execution manifest is invalid") from exc
    retained_inputs = [{key: item[key] for key in (
        "node_id", "dataset_id", "revision_id", "provider")}
        for item in admission["input_manifest"]]
    supplied_inputs = [{key: item[key] for key in (
        "node_id", "dataset_id", "revision_id", "provider")}
        for item in input_manifest or []]
    if input_manifest is not None and supplied_inputs != retained_inputs:
        raise metadb.DurableTaskSubmissionConflict(
            "submission is bound to different admitted inputs")
    retained_write = (
        WriteIntent.model_validate(admission["write_intent"])
        if admission["write_intent"] is not None else None
    )
    if write_intent is not None and write_intent != retained_write:
        raise metadb.DurableTaskSubmissionConflict(
            "submission is bound to a different write intent")
    return admission, payload


def _adopt_manifest_durable_task(
        deps, task: dict, graph, target_node_id: str | None,
        input_manifest: list[dict[str, str]] | None,
        write_intent: WriteIntent | None) -> RunStatus:
    """Validate response-loss replay from retained semantics before any mutable admission work."""
    sha256 = task.get("execution_manifest_sha256")
    if not isinstance(sha256, str):
        raise RuntimeError("durable task has no execution manifest")
    _admission, _payload = _validate_retained_manifest_replay(
        sha256, graph, target_node_id, input_manifest, write_intent)

    return _resume_durable_task(deps, task)


def _local_run_intent_sha256(
        graph, target_node_id: str | None,
        input_manifest: list[dict[str, str]] | None = None,
        write_intent: WriteIntent | None = None) -> str:
    """Hash caller intent before source resolution so a retry cannot be retargeted by a moved head."""
    doc = graph.model_dump(mode="json")
    if write_intent is not None:
        # Canvas run status is operational UI evidence, not write intent. A response-loss retry changes
        # it from running to failed before replaying the same frozen write submission.
        for node in doc.get("nodes", []):
            data = node.get("data") if isinstance(node, dict) else None
            if isinstance(data, dict):
                data.pop("status", None)
    payload = json.dumps(
        {"graph": doc, "target_node_id": target_node_id, "input_manifest": input_manifest,
         "write_intent": (write_intent.model_dump(by_alias=True, mode="json")
                          if write_intent is not None else None)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _external_wait_request(deps, graph, target_node_id: str | None):
    """Recognize only one installed external result feeding one built-in Write."""
    external = [node for node in graph.nodes if node.type in getattr(deps, "external_wait_nodes", {})]
    if not external:
        return None
    target = next((node for node in graph.nodes if node.id == target_node_id), None)
    edge = graph.edges[0] if len(graph.edges) == 1 else None
    if (len(graph.nodes) != 2 or len(external) != 1 or target is None or target.type != "write"
            or edge is None or edge.source != external[0].id or edge.target != target.id
            or edge.source_handle not in (None, "out") or edge.target_handle not in (None, "in")):
        raise HTTPException(409, "external-wait tasks require exactly one fixture-to-Write edge")
    cfg = external[0].data.get("config", {}) if isinstance(external[0].data, dict) else {}
    if (not isinstance(cfg, dict)
            or set(cfg) - {"operation", "documentJson", "outputSchema"}
            or not isinstance(cfg.get("outputSchema"), list) or not cfg["outputSchema"]):
        raise HTTPException(409, "external-wait node configuration is not supported")
    from hub.external_wait import ExternalWaitSubmitRequest
    try:
        return ExternalWaitSubmitRequest(
            provider_kind=deps.external_wait_nodes[external[0].type],
            idempotency_key="admission", operation=cfg.get("operation", "conformance.success"),
            document_json=cfg.get("documentJson", "{}"))
    except ValueError as exc:
        raise HTTPException(409, "external-wait node configuration is invalid") from exc


def _node_config(node) -> dict:
    data = node.data if isinstance(node.data, dict) else {}
    cfg = data.get("config", {})
    return cfg if isinstance(cfg, dict) else {}


def _node_bypassed_or_disabled(node) -> bool:
    data = node.data if isinstance(node.data, dict) else {}
    return bool(data.get("bypassed")) or bool(data.get("disabled"))


def _bounded_fanout_write_shape(graph, target_node_id: str | None):
    """Return (source, checkpoint_select, identity_select, write) for the exact four-node route.

    Non-four-node graphs return None so the three-node linear route can handle them. A four-node
    graph with checkpoint:true that does not match the exact topology/config is rejected with 409.
    """
    checkpoint_nodes = [
        node for node in graph.nodes if _node_config(node).get("checkpoint") is True]
    if not checkpoint_nodes:
        return None
    if len(graph.nodes) != 4 or len(graph.edges) != 3:
        return None
    by_id = {node.id: node for node in graph.nodes}
    write = by_id.get(target_node_id)
    if (write is None or write.type != "write" or len(checkpoint_nodes) != 1):
        raise HTTPException(
            409,
            "bounded fan-out tasks require exactly "
            "Source -> Select(checkpoint) -> Select(*) -> Write")
    checkpoint_select = checkpoint_nodes[0]
    if checkpoint_select.type != "select":
        raise HTTPException(409, "bounded fan-out requires checkpoint:true on a Select node")
    ck_cfg = _node_config(checkpoint_select)
    if ck_cfg != {"select": "*", "checkpoint": True}:
        raise HTTPException(
            409, "bounded fan-out checkpoint Select requires exact "
            "{\"select\":\"*\",\"checkpoint\":true}")
    edges = list(graph.edges)
    write_in = next((edge for edge in edges if edge.target == write.id), None)
    if write_in is None:
        raise HTTPException(409, "bounded fan-out Write requires one inbound edge")
    identity_select = by_id.get(write_in.source)
    if identity_select is None or identity_select.type != "select":
        raise HTTPException(409, "bounded fan-out requires identity Select before Write")
    from hub.identity_projection import validate_identity_select_config
    try:
        validate_identity_select_config(_node_config(identity_select))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    identity_in = next((edge for edge in edges if edge.target == identity_select.id), None)
    if identity_in is None or identity_in.source != checkpoint_select.id:
        raise HTTPException(
            409, "bounded fan-out requires checkpoint Select feeding identity Select")
    checkpoint_in = next((edge for edge in edges if edge.target == checkpoint_select.id), None)
    if checkpoint_in is None:
        raise HTTPException(409, "bounded fan-out checkpoint Select requires one inbound edge")
    source = by_id.get(checkpoint_in.source)
    if source is None or source.type != "source":
        raise HTTPException(409, "bounded fan-out tasks require exactly one built-in Source")
    for edge in (checkpoint_in, identity_in, write_in):
        if (edge.source_handle not in (None, "out")
                or edge.target_handle not in (None, "in")):
            raise HTTPException(
                409, "bounded fan-out edges must be source:out -> target:in")
    expected = {source.id, checkpoint_select.id, identity_select.id, write.id}
    if {node.id for node in graph.nodes} != expected:
        raise HTTPException(409, "bounded fan-out tasks reject extra nodes")
    if any(_node_bypassed_or_disabled(node)
           for node in (source, checkpoint_select, identity_select, write)):
        raise HTTPException(409, "bounded fan-out tasks reject disabled or bypassed nodes")
    return source, checkpoint_select, identity_select, write


def _linear_checkpoint_shape(graph, target_node_id: str | None):
    """Return (source, select, write) for the exact three-node route, else None.

    Any checkpoint flag on a non-matching shape is rejected before allocation.
    """
    checkpoint_nodes = [
        node for node in graph.nodes if _node_config(node).get("checkpoint") is True]
    if not checkpoint_nodes:
        return None
    by_id = {node.id: node for node in graph.nodes}
    write = by_id.get(target_node_id)
    if (len(graph.nodes) != 3 or len(graph.edges) != 2 or write is None or write.type != "write"
            or len(checkpoint_nodes) != 1):
        raise HTTPException(
            409, "linear checkpoint tasks require exactly Source -> Select(checkpoint) -> Write")
    select = checkpoint_nodes[0]
    if select.type != "select":
        raise HTTPException(409, "linear checkpoint requires checkpoint:true on the Select node")
    edges = list(graph.edges)
    select_in = next((edge for edge in edges if edge.target == select.id), None)
    write_in = next((edge for edge in edges if edge.target == write.id), None)
    if (select_in is None or write_in is None or write_in.source != select.id
            or select_in.source_handle not in (None, "out")
            or select_in.target_handle not in (None, "in")
            or write_in.source_handle not in (None, "out")
            or write_in.target_handle not in (None, "in")):
        raise HTTPException(
            409, "linear checkpoint tasks require source:out -> select:in and select:out -> write:in")
    source = by_id.get(select_in.source)
    if source is None or source.type != "source":
        raise HTTPException(409, "linear checkpoint tasks require exactly one built-in Source")
    other_ids = {node.id for node in graph.nodes} - {source.id, select.id, write.id}
    if other_ids:
        raise HTTPException(409, "linear checkpoint tasks reject extra nodes")
    if any(_node_bypassed_or_disabled(node) for node in (source, select, write)):
        raise HTTPException(409, "linear checkpoint tasks reject disabled or bypassed nodes")
    # Reject unsupported Write modes and Select extras early via later admission; keep shape only here.
    return source, select, write


def _local_run_source_nodes(graph, target_node_id: str | None):
    """Return execution-cone Sources in graph order; duplicate node ids are rejected upstream."""
    cone = graph_mod.upstream_chain(graph, target_node_id) if target_node_id else graph.nodes
    return [node for node in cone if node.type == "source"]


def _source_supports_automatic_local_admission(node, deps) -> bool:
    """Keep no-submission auto-admission to exact providers and supported single local files."""
    data = node.data if isinstance(node.data, dict) else {}
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    uri = str(config.get("uri") or "")
    from hub import workspace_providers
    provider_dataset_id = workspace_providers.provider_dataset_identity(uri) if uri else None
    if (not uri or (provider_dataset_id is None
                    and metadb.catalog_revision_binding_for_uri(uri) is None)):
        return False
    try:
        scan_adapter = deps.resolve_adapter(uri)
        revision_adapter = revision_adapter_for_uri(uri, deps.resolve_adapter)
        from hub.local_run_inputs import supports_local_file_snapshot
        exact = (workspace_providers.provider_dataset_supports_exact(revision_adapter)
                 if provider_dataset_id is not None
                 else isinstance(revision_adapter, DatasetRevisionAdapter))
        return (exact
                or supports_local_file_snapshot(uri, scan_adapter))
    except Exception:
        return False


def _resolve_local_run_manifest(
        graph, target_node_id: str | None, deps, *, materialize_local_files: bool = False,
        local_file_candidates: list[dict[str, str]] | None = None,
        preview_limit: int | None = None,
        ) -> list[dict[str, str]]:
    """Resolve every local-run Source once through its registered exact-revision provider."""
    resolved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest: list[dict[str, str]] = []
    for node in _local_run_source_nodes(graph, target_node_id):
        cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        uri = str(cfg.get("uri") or "")
        from hub import workspace_providers
        try:
            provider_dataset_id = (
                workspace_providers.provider_dataset_identity(uri) if uri else None)
            binding = (
                metadb.catalog_revision_binding_for_uri(uri)
                if provider_dataset_id is None else None)
            adapter = revision_adapter_for_uri(uri, deps.resolve_adapter)
        except PermissionError as exc:
            raise APIError(
                403, "permission to read the provider dataset was lost",
                code=APIErrorCode.PERMISSION_DENIED, retryable=False,
            ) from exc
        except workspace_providers.ProviderDatasetGone as exc:
            raise APIError(
                410, "local_run_input_revision_unavailable",
                code=APIErrorCode.RESOURCE_GONE, retryable=False,
            ) from exc
        except workspace_providers.ProviderDatasetOffline as exc:
            raise APIError(
                503, "provider dataset is offline",
                code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True,
            ) from exc
        except workspace_providers.ProviderDatasetUnavailable as exc:
            raise APIError(
                409, ("provider dataset binding is unavailable; install or restore a compatible "
                      "provider and dataset adapter"),
                code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED, retryable=False,
            ) from exc
        if binding is None and provider_dataset_id is None:
            raise APIError(410, "local_run_input_revision_unavailable",
                           code=APIErrorCode.RESOURCE_GONE, retryable=False)
        dataset_ref = cfg.get("datasetRef")
        try:
            exact = (workspace_providers.provider_dataset_supports_exact(adapter)
                     if provider_dataset_id is not None
                     else isinstance(adapter, DatasetRevisionAdapter))
            if not exact:
                if provider_dataset_id is not None:
                    raise LocalRunInputError(
                        "provider dataset is mutable-only and cannot enter an immutable run manifest")
                if isinstance(dataset_ref, dict) or not materialize_local_files:
                    raise RuntimeError("source has no provider-native exact revision")
                from hub.local_run_inputs import (
                    LOCAL_FILE_INPUT_PROVIDER,
                    snapshot_local_file_input,
                )
                revision_id, candidate = snapshot_local_file_input(
                    uri=uri,
                    config=cfg if isinstance(cfg, dict) else {},
                    dataset_id=str(binding["dataset_id"]),
                    adapter=adapter,
                    storage=deps.storage,
                )
                if candidate is not None:
                    if local_file_candidates is None:
                        raise RuntimeError("local file snapshot candidate has no admission owner")
                    local_file_candidates.append(candidate)
                provider = LOCAL_FILE_INPUT_PROVIDER
            elif isinstance(dataset_ref, dict):
                dataset_id, revision_id = dataset_ref_identity(dataset_ref)
                current_dataset_id = (provider_dataset_id
                                      if provider_dataset_id is not None
                                      else str(binding["dataset_id"]))
                if current_dataset_id != dataset_id:
                    raise ValueError("selected dataset identity does not match the current registration")
                with db.base_guard():
                    if preview_limit is None:
                        adapter.open_revision(uri, revision_id)
                    else:
                        preview_revision = getattr(adapter, "preview_revision", None)
                        if not callable(preview_revision):
                            raise LocalRunInputError(
                                "exact input revision has no bounded preview capability")
                        preview_revision(uri, revision_id, limit=preview_limit)
                provider = str(getattr(adapter, "name", "") or "")
            else:
                resolved = adapter.resolve_revision(uri)
                revision_id = str(resolved.get("revision_id") or "")
                provider = str(getattr(adapter, "name", "") or "")
        except LocalRunInputError as exc:
            raise APIError(
                409, str(exc), code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED,
                retryable=False,
            ) from exc
        except (PermissionError, RevisionPermissionLost) as exc:
            raise APIError(
                403, "permission to read an exact input revision was lost",
                code=APIErrorCode.PERMISSION_DENIED, retryable=False,
            ) from exc
        except (RevisionProviderOffline, ConnectionError, TimeoutError, OSError) as exc:
            raise APIError(
                503, "exact input revision provider is offline",
                code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True,
            ) from exc
        except Exception as exc:  # missing pins and provider errors never permit a fallback to head
            raise APIError(410, "local_run_input_revision_unavailable",
                           code=APIErrorCode.RESOURCE_GONE, retryable=False) from exc
        if not revision_id or not provider:
            raise APIError(410, "local_run_input_revision_unavailable",
                           code=APIErrorCode.RESOURCE_GONE, retryable=False)
        manifest.append({
            "node_id": str(node.id),
            "dataset_id": (provider_dataset_id
                           if provider_dataset_id is not None else str(binding["dataset_id"])),
            "revision_id": revision_id, "provider": provider, "resolved_at": resolved_at,
        })
    return manifest


def _bind_local_run_manifest(
        graph, manifest: list[dict[str, str]], deps, target_node_id: str | None = None,
        *, preview_limit: int | None = None):
    """Reopen persisted exact bindings and attach them only to the dispatch copy of a graph."""
    from hub.local_run_inputs import LocalRunInputError, bind_manifest

    try:
        return bind_manifest(
            graph, target_node_id, manifest, deps.resolve_adapter, preview_limit=preview_limit)
    except (PermissionError, RevisionPermissionLost) as exc:
        raise APIError(
            403, "permission to read an exact input revision was lost",
            code=APIErrorCode.PERMISSION_DENIED, retryable=False,
        ) from exc
    except workspace_providers.ProviderDatasetGone as exc:
        raise APIError(
            410, "local_run_input_revision_unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False,
        ) from exc
    except (RevisionProviderOffline, ConnectionError, TimeoutError, OSError,
            workspace_providers.ProviderDatasetOffline) as exc:
        raise APIError(
            503, "exact input revision provider is offline",
            code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True,
        ) from exc
    except workspace_providers.ProviderDatasetUnavailable as exc:
        raise APIError(
            409, ("provider dataset binding is unavailable; install or restore a compatible "
                  "provider and dataset adapter"),
            code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED, retryable=False,
        ) from exc
    except LocalRunInputError as exc:
        if "mutable-only" in str(exc):
            raise APIError(
                409, "provider dataset is mutable-only and cannot enter an immutable run manifest",
                code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED, retryable=False,
            ) from exc
        unavailable = "unavailable" in str(exc)
        raise APIError(
            410 if unavailable else 409,
            "local_run_input_revision_unavailable" if unavailable
            else "local_run_input_manifest_does_not_match_graph",
            code=APIErrorCode.RESOURCE_GONE if unavailable else APIErrorCode.INVALID_REQUEST,
            retryable=False,
        ) from exc


def _runner_supports_managed_local_write_intents(deps, runner) -> bool:
    """Whether a managed-local create/replace admitted for this routed runner can be published.

    Managed-local create/replace is owned by the certified durable-Task lifecycle regardless of the
    selected execution backend, so this is a pure capability check: the default per-canvas kernel and
    the in-process local writer both route publication through that one durable owner.
    """
    supports = getattr(runner, "supports_managed_local_write_intents", None)
    if not callable(supports):
        return False
    try:
        return bool(supports())
    except Exception:
        return False


def _resolve_write_sink_or_typed_error(spec, deps) -> str:
    """Resolve a Write sink URI, mapping an unknown/unresolvable destination to a typed 4xx so no run
    claim is ever created for a destination that cannot exist."""
    from hub.sinks import preflight_sink
    try:
        return preflight_sink(spec, deps.workspace, deps.storage, deps.resolve_adapter)
    except ValueError as exc:
        raise APIError(
            400, str(exc), code=APIErrorCode.INVALID_REQUEST, retryable=False) from exc


def _preflight_write_target_destination(deps, graph, node_id: str) -> None:
    """Reject an unknown Write destination with a typed 4xx before a run claim exists, on paths that
    skip write admission (direct API / MCP callers supply no submissionId)."""
    from hub.sinks import SinkSpec
    node = next((candidate for candidate in graph.nodes if candidate.id == node_id), None)
    if node is None or node.type != "write":
        return
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    spec = SinkSpec.from_config(
        cfg, node.data.get("title") if isinstance(node.data, dict) else None)
    _resolve_write_sink_or_typed_error(spec, deps)


def _kernel_run_provably_undispatched(runner, run_id: str, status) -> bool:
    """True when a raised local dispatch left the kernel claim behind with no execution owner, so it
    must be terminalized rather than adopted as an ambiguous response loss."""
    from hub.kernel_backend import KernelBackend
    return (isinstance(runner, KernelBackend) and status.status == "queued"
            and metadb.run_kernel_id(run_id) is None
            and metadb.backend_job(run_id) is None)


def _write_admission_for_graph(
        deps, graph, node_id: str, uid: str, submission_id: str,
        supplied: WriteIntent | None = None, *, direct_local: bool = False) -> WriteAdmission:
    """Resolve one metadata-only Write card contract without allocating an artifact."""
    from hub.plugins.catalog import InMemoryCatalog, lineage_for_output
    from hub.sinks import (
        SinkSpec, is_core_managed_local_file_sink,
        is_core_managed_local_lance_append_sink,
    )

    node = next((candidate for candidate in graph.nodes if candidate.id == node_id), None)
    if node is None or node.type != "write":
        raise HTTPException(400, f"node '{node_id}' is not a write")
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    spec = SinkSpec.from_config(
        cfg, node.data.get("title") if isinstance(node.data, dict) else None)
    logical_uri = _resolve_write_sink_or_typed_error(spec, deps)
    adapter = deps.resolve_adapter(logical_uri)
    managed_file = (
        is_core_managed_local_file_sink(spec, logical_uri, adapter, deps.storage)
        and type(deps.catalog) is InMemoryCatalog
    )
    lance_candidate = (
        is_core_managed_local_lance_append_sink(spec, logical_uri, adapter)
        and type(deps.catalog) is InMemoryCatalog
    )
    managed = managed_file or lance_candidate
    pick_runner = getattr(deps, "pick_runner", None)
    if managed and callable(pick_runner) and not direct_local:
        plan = compiler.compile_plan(
            graph, node_id, deps.registry, getattr(deps, "node_specs", {}),
            getattr(deps, "node_ir", {}))
        # #391's typed publisher is the in-process local consumer. Other bundled/plugin transports
        # retain their provider-neutral sink contract and must not be labelled create/replace.
        runner = _route_by_capability(deps, pick_runner(plan, uid), graph, node_id)
        managed = _runner_supports_managed_local_write_intents(deps, runner)
        controller = getattr(deps, "controller", None)
        plan_for_run = getattr(controller, "plan_for_run", None)
        if managed and callable(plan_for_run):
            _rows, _byts, sizes = _cone_size(graph, node_id, deps)
            try:
                managed = not bool(plan_for_run(graph, node_id, sizes=sizes))
            except Exception:
                managed = False
    lance_binding = None
    lance_table = None
    if managed and lance_candidate:
        lance_binding = metadb.catalog_revision_binding_for_uri(logical_uri)
        if lance_binding is not None:
            try:
                lance_table = deps.catalog.get_table(logical_uri)
            except KeyError:
                lance_binding = None
        managed = lance_binding is not None and lance_table is not None
    partitions = [WritePartitionExpectation(field=field.strip()) for field in
                  spec.partition_by.split(",") if field.strip()]
    provider_name = str(getattr(adapter, "name", "") or "provider-neutral")
    if not managed:
        if supplied is not None:
            raise HTTPException(
                409, "the selected destination uses provider-neutral sink semantics; "
                "discard the managed-local admission and retry")
        return WriteAdmission(
            node_id=node_id,
            managed=False,
            destination=logical_uri,
            mode=spec.mode,
            provider=provider_name,
            partitions=partitions,
        )
    try:
        if direct_local:
            # Prefer an explicit declared upstream schema (external-wait fixtures). Fall back to the
            # graph schema so Source -> Select(checkpoint) -> Write can still admit create/replace.
            upstream = next(
                (candidate for candidate in graph.nodes if candidate.id != node_id), None)
            schema = declared_schema(upstream) if upstream is not None else None
            if schema is None:
                schemas = schema_for_graph(
                    graph, deps.resolve_adapter, deps.registry,
                    getattr(deps, "node_builders", {}), getattr(deps, "node_specs", {}),
                    storage=deps.storage)
                schema = schemas.get(node_id)
        else:
            schemas = schema_for_graph(
                graph, deps.resolve_adapter, deps.registry, getattr(deps, "node_builders", {}),
                getattr(deps, "node_specs", {}), storage=deps.storage)
            schema = schemas.get(node_id)
    except ManagedSourceReadError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        schema = None
    if schema is None:
        return WriteAdmission(
            node_id=node_id,
            managed=True,
            destination=logical_uri,
            mode=("append" if lance_candidate
                  else "replace" if supplied and supplied.mode == "replace" else "create"),
            provider=("managed-local-lance" if lance_candidate else "managed-local-file"),
            partitions=partitions,
            blocker=("input schema is not available from bounded metadata; "
                     "declare the upstream output schema before running"),
        )

    run_id = metadb.local_run_submission_id(
        str(uid), str(getattr(graph, "id", "") or "") or None, str(submission_id))
    lineage = lineage_for_output(graph, run_id, node_id)
    parents = metadb.catalog_lineage_parent_tokens(
        graph_mod.all_upstream_publication_uris(graph, node_id))
    provenance = WriteProvenance(publication=lineage, parents=parents)
    normalized_schema = [ColumnSchema.model_validate(column) for column in schema]
    if lance_candidate:
        if lance_binding is None or lance_table is None:  # narrowed above; keeps typing explicit
            raise RuntimeError("managed local Lance admission lost its catalog binding")
        if supplied is not None and (
                supplied.mode != "append"
                or supplied.destination.provider != "managed-local-lance"
                or supplied.destination.logical_uri != logical_uri
                or supplied.destination.dataset_id != str(lance_binding["dataset_id"])
                or supplied.destination.name != lance_table.name
                or supplied.expected_schema != normalized_schema
                or supplied.idempotency_key != lineage.idempotency_key
                or supplied.provenance != provenance
                or supplied.partitions):
            raise HTTPException(409, "write admission does not match the submitted graph")
        intent = supplied
        if intent is not None:
            try:
                recovered = metadb.catalog_admit_managed_local_lance_write(
                    intent.model_dump(by_alias=True, mode="json"))
            except metadb.ManagedLocalWriteConflict as exc:
                raise HTTPException(
                    409, "write admission is stale; re-admit the current destination head and retry"
                ) from exc
            if recovered is not None:
                receipt = WriteReceipt.model_validate(recovered)
                return WriteAdmission(
                    node_id=node_id, managed=True, destination=logical_uri,
                    mode="append", provider="managed-local-lance",
                    expected_schema=intent.expected_schema, expected_head=intent.expected_head,
                    intent=intent, recovered_receipt=receipt,
                )
        try:
            resolved = adapter.resolve_revision(logical_uri)
            revision_id = str(resolved["revision_id"])
            destination_detail = adapter.revision_detail(
                logical_uri, revision_id, preview_limit=1)
            destination_schema = [
                ColumnSchema.model_validate(column)
                for column in destination_detail["columns"]
            ]
        except Exception as exc:
            if supplied is not None:
                raise HTTPException(
                    409, "write admission cannot reopen the frozen Lance destination head"
                ) from exc
            return WriteAdmission(
                node_id=node_id, managed=True, destination=logical_uri,
                mode="append", provider="managed-local-lance",
                expected_schema=normalized_schema,
                blocker="the existing Lance destination head is not available",
            )
        if intent is not None:
            expected = intent.expected_head
            if (expected is None or expected.revision_id != revision_id
                    or expected.dataset_id != str(lance_binding["dataset_id"])):
                raise HTTPException(
                    409, "write admission is stale; re-admit the current destination head and retry")
        if destination_schema != normalized_schema:
            return WriteAdmission(
                node_id=node_id, managed=True, destination=logical_uri,
                mode="append", provider="managed-local-lance",
                expected_schema=normalized_schema,
                expected_head=ExactDatasetRef(
                    kind="exact", dataset_id=str(lance_binding["dataset_id"]),
                    revision_id=revision_id),
                blocker="input schema is incompatible with the existing Lance destination",
            )
        intent = intent or WriteIntent(
            destination=WriteDestination(
                logical_uri=logical_uri,
                name=lance_table.name,
                dataset_id=str(lance_binding["dataset_id"]),
                provider="managed-local-lance",
            ),
            mode="append",
            expected_schema=normalized_schema,
            expected_head=ExactDatasetRef(
                kind="exact", dataset_id=str(lance_binding["dataset_id"]),
                revision_id=revision_id),
            idempotency_key=lineage.idempotency_key,
            provenance=provenance,
        )
        try:
            recovered = metadb.catalog_admit_managed_local_lance_write(
                intent.model_dump(by_alias=True, mode="json"))
        except metadb.ManagedLocalWriteConflict as exc:
            raise HTTPException(
                409, "write admission is stale; re-admit the current destination head and retry"
            ) from exc
        return WriteAdmission(
            node_id=node_id, managed=True, destination=logical_uri,
            mode="append", provider="managed-local-lance",
            expected_schema=intent.expected_schema, expected_head=intent.expected_head,
            intent=intent,
            recovered_receipt=(WriteReceipt.model_validate(recovered)
                               if recovered is not None else None),
        )

    head = metadb.catalog_managed_local_write_head(logical_uri)
    replacing = bool(
        head is not None and head.get("state") == "active" and head.get("revision_id"))
    if not replacing and metadb.catalog_managed_local_write_unmanaged_conflict(
            logical_uri, spec.name):
        raise HTTPException(
            409,
            f"the destination name '{spec.name}' is already registered as an unmanaged output; "
            "rename this Write or unregister the existing catalog entry, then retry")
    expected_head = (ExactDatasetRef(
        kind="exact",
        dataset_id=str(head["dataset_id"]),
        revision_id=str(head["revision_id"]),
    ) if replacing else None)
    intent = supplied or WriteIntent(
        destination=WriteDestination(
            logical_uri=logical_uri,
            name=spec.name,
            dataset_id=(str(head["dataset_id"]) if replacing else None),
        ),
        mode=("replace" if replacing else "create"),
        expected_schema=normalized_schema,
        expected_head=expected_head,
        idempotency_key=lineage.idempotency_key,
        partitions=partitions,
        provenance=provenance,
    )
    if supplied is not None and (
            intent.destination.logical_uri != logical_uri
            or intent.destination.name != spec.name
            or intent.expected_schema != normalized_schema):
        raise HTTPException(409, "write admission does not match the submitted graph")
    if supplied is not None and (
            intent.idempotency_key != lineage.idempotency_key
            or intent.provenance != provenance
            or intent.partitions != partitions):
        raise HTTPException(409, "write admission does not match the submitted graph")
    try:
        recovered = metadb.catalog_admit_managed_local_write(
            intent.model_dump(by_alias=True, mode="json"))
    except metadb.ManagedLocalWriteConflict as exc:
        raise HTTPException(
            409, "write admission is stale; re-admit the current destination head and retry") from exc
    receipt = WriteReceipt.model_validate(recovered) if recovered is not None else None
    return WriteAdmission(
        node_id=node_id,
        managed=True,
        destination=logical_uri,
        mode=intent.mode,
        provider="managed-local-file",
        expected_schema=intent.expected_schema,
        partitions=intent.partitions,
        expected_head=intent.expected_head,
        intent=intent,
        recovered_receipt=receipt,
    )


def _inject_write_intent(graph, node_id: str, intent: WriteIntent) -> None:
    node = next(candidate for candidate in graph.nodes if candidate.id == node_id)
    cfg = dict(node.data.get("config", {}))
    cfg["_admittedWriteIntent"] = intent.model_dump(by_alias=True, mode="json")
    node.data["config"] = cfg


def _provider_inspection_graph(graph, target_node_id: str | None, deps):
    """Attach request-local provider reads for preview/profile only, with stable error semantics."""
    try:
        return workspace_providers.provider_dataset_inspection_graph(
            graph, target_node_id, deps.resolve_adapter)
    except PermissionError as exc:
        raise APIError(
            403, "permission to read the provider dataset was lost",
            code=APIErrorCode.PERMISSION_DENIED, retryable=False,
        ) from exc
    except workspace_providers.ProviderDatasetGone as exc:
        raise APIError(
            410, "provider dataset was deleted; relink it explicitly",
            code=APIErrorCode.RESOURCE_GONE, retryable=False,
        ) from exc
    except workspace_providers.ProviderDatasetOffline as exc:
        raise APIError(
            503, "provider dataset is offline",
            code=APIErrorCode.SERVICE_UNAVAILABLE, retryable=True,
        ) from exc
    except workspace_providers.ProviderDatasetUnavailable as exc:
        raise APIError(
            409, ("provider dataset binding is unavailable; install or restore a compatible "
                  "provider and dataset adapter"),
            code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED, retryable=False,
        ) from exc


def _has_private_provider_inspection(graph) -> bool:
    return any(
        isinstance(node.data.get("config"), dict)
        and bool(node.data["config"].get("_input_provider_preview_uri"))
        for node in graph.nodes
    )


def _provider_inspection_failure_reason(
        graph, reason: str, *, runtime_error: bool = False) -> str:
    if runtime_error and _has_private_provider_inspection(graph):
        # Adapter relations may defer physical I/O until DuckDB materializes them, after the Source
        # lowering frame has returned. At that boundary an arbitrary runtime error cannot be attributed
        # safely to the Source or a downstream node, so never echo provider-owned physical details.
        return "provider dataset inspection failed"
    for node in graph.nodes:
        config = node.data.get("config") if isinstance(node.data, dict) else None
        physical_uri = (
            config.get("_input_provider_preview_uri") if isinstance(config, dict) else None)
        if isinstance(physical_uri, str) and physical_uri and physical_uri in reason:
            return "provider dataset inspection failed"
    return reason


def _inspection_manifest_graph(
        graph, target_node_id: str | None, supplied: list[dict[str, str]] | None, deps,
        *, allow_mutable_provider: bool = False,
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
        provider_source = any(
            workspace_providers.is_provider_dataset_uri(str(
                node.data.get("config", {}).get("uri") or ""))
            for node in _local_run_source_nodes(graph, target_node_id)
        )
        if not provider_source:
            return graph, None
        if allow_mutable_provider:
            return _provider_inspection_graph(graph, target_node_id, deps), None
        raise


@router.post("/run/write-admission", response_model=WriteAdmission)
def write_admission(
        req: WriteAdmissionRequest, uid: str = Depends(current_user)) -> WriteAdmission:
    """Certify one default-local Write card without creating or mutating an artifact."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    graph = _resolve_parameters(req.graph, req.parameter_bindings, req.node_id, deps)
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)
    if req.input_manifest is not None:
        graph = _bind_local_run_manifest(graph, req.input_manifest, deps, req.node_id)
    _reject_invalid(graph, deps, req.node_id)
    return _write_admission_for_graph(
        deps, graph, req.node_id, uid, str(req.submission_id))


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
    cid = graph.get("id") if isinstance(graph, dict) else getattr(graph, "id", None)
    cid = str(cid or "")
    if not auth.auth_enabled():
        retained_canvas = cid if cid and metadb.canvas_role(cid, uid) is not None else None
        try:
            metadb.require_promoted_transform_use(
                uid, graph, canvas_id=retained_canvas)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return None, None
    role = metadb.canvas_role(cid, uid) if cid else None
    if role is None:
        raise APIError(
            404,
            f"canvas '{cid}' not found",
            code=APIErrorCode.CANVAS_NOT_FOUND,
            retryable=False,
        )
    try:
        metadb.require_promoted_transform_use(uid, graph, canvas_id=cid)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
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
    graph = _resolve_parameters(req.graph, req.parameter_bindings, req.target_node_id, deps)
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    invalid = _invalid_graph(graph, deps, req.target_node_id)
    if invalid:
        error, acyclic = invalid
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=acyclic, error=error)
    return compiler.compile_plan(graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)


@router.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest, uid: str = Depends(current_user)) -> SampleResult:
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    req.graph = _resolve_parameters(req.graph, req.parameter_bindings, req.node_id, deps)
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
            manifest = _resolve_local_run_manifest(
                req.graph, req.node_id, deps, preview_limit=PREVIEW_SCAN)
        preview_graph = _bind_local_run_manifest(
            req.graph, manifest, deps, req.node_id, preview_limit=PREVIEW_SCAN)
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
        try:
            preview_graph = _provider_inspection_graph(req.graph, req.node_id, deps)
        except APIError as exc:
            return SampleResult(not_previewable=True, reason=str(exc.detail))
    _reject_invalid(preview_graph, deps, req.node_id)
    port_id = _inspection_port(preview_graph, req.node_id, req.port_id, deps)
    k = req.k if req.k is not None else settings.preview_k
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            result = SampleResult(**kb.preview(
                preview_graph, req.node_id, k, max(0, req.offset), port_id))
            result.input_manifest = manifest
            if result.error or result.not_previewable:
                result.reason = _provider_inspection_failure_reason(
                    preview_graph, result.reason or "provider dataset inspection failed",
                    runtime_error=result.error,
                )
            return result
        except Exception as e:  # noqa: BLE001 — kernel unreachable / spawn timeout → a clean error, not a raw 500
            return SampleResult(error=True, reason=_provider_inspection_failure_reason(
                preview_graph, f"kernel unavailable: {type(e).__name__}: {e}",
                runtime_error=True,
            ))
    result = preview_node(preview_graph, req.node_id, k,
                          deps.resolve_adapter, deps.registry, deps.node_builders, deps.node_specs,
                          offset=max(0, req.offset), storage=deps.storage, port_id=port_id)
    result.input_manifest = manifest
    if result.error or result.not_previewable:
        result.reason = _provider_inspection_failure_reason(
            preview_graph, result.reason or "provider dataset inspection failed",
            runtime_error=result.error,
        )
    return result


@router.post("/run/input-drift", response_model=InputDrift)
def input_drift(req: InputDriftRequest, uid: str = Depends(current_user)) -> InputDrift:
    """Report moved Source heads and #125 compatibility without replacing preview inputs."""
    _require_graph_read_access(req.graph, uid)
    deps = get_deps()
    req.graph = _resolve_parameters(
        req.graph, req.parameter_bindings, req.target_node_id, deps)
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
    req.graph = _resolve_parameters(req.graph, req.parameter_bindings, req.node_id, deps)
    graph_mod.resolve_source_refs(req.graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(req.graph, deps, req.node_id)
    graph, manifest = _inspection_manifest_graph(
        req.graph, req.node_id, req.input_manifest, deps,
        allow_mutable_provider=True,
    )
    _reject_invalid(graph, deps, req.node_id)
    port_id = _inspection_port(graph, req.node_id, req.port_id, deps)
    if deps.chosen_backend(uid) == "kernel" and (kb := deps.kernel_backend()):
        try:
            result = ProfileResult(**kb.profile(
                graph, req.node_id, full=False, port_id=port_id))
        except Exception as e:  # noqa: BLE001 — kernel unreachable → a clean error, not a raw 500
            result = ProfileResult(error=True, reason=_provider_inspection_failure_reason(
                graph, f"kernel unavailable: {type(e).__name__}: {e}",
                runtime_error=True,
            ))
    else:
        result = profile_node(graph, req.node_id, deps.resolve_adapter, deps.registry,
                              deps.node_builders, deps.node_specs, full=False,
                              storage=deps.storage, port_id=port_id)
    result.input_manifest = manifest
    if result.error or result.not_previewable:
        result.reason = _provider_inspection_failure_reason(
            graph, result.reason or "provider dataset inspection failed",
            runtime_error=result.error,
        )
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
    req.graph = _resolve_parameters(req.graph, req.parameter_bindings, req.node_id, deps)
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
    req.graph = _resolve_parameters(req.graph, req.parameter_bindings, req.node_id, deps)
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
        req.graph = _resolve_parameters(
            req.graph, req.parameter_bindings, req.node_id, deps)
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
            # consulting mutable source state. Compare only against its retained exact definition: this
            # catches a retargeted graph while preserving response-loss replay after source/runtime moves.
            retained_sha256 = metadb.execution_manifest_sha256_for_run(existing.run_id)
            if retained_sha256 is not None:
                retained = metadb.execution_manifest(retained_sha256)
                if retained is None:
                    raise RuntimeError("profile execution manifest is unavailable")
                retained_payload = json.dumps(
                    retained["document"], sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True,
                )
                try:
                    matches = execution_manifest_accepts_graph_replay(
                        retained_sha256,
                        retained_payload,
                        req.graph,
                        target_node_id=req.node_id,
                        target_port_id=port_id,
                    )
                except ExecutionManifestError as exc:
                    raise RuntimeError("profile execution manifest is invalid") from exc
                if not matches:
                    raise HTTPException(
                        409,
                        "profile submission id is already bound to a different execution manifest",
                    )
            # Legacy/pruned identities without a retained manifest remain non-reconstructable. They are
            # adopted without synthesizing a definition from the current Canvas.
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

                    execution_sha256, execution_doc = _admitted_execution_manifest(
                        req.graph,
                        target_node_id=req.node_id,
                        target_port_id=port_id,
                        input_manifest=manifest,
                        write_intent=None,
                        deps=deps,
                    )
                    graph._execution_manifest_sha256 = execution_sha256
                    graph._execution_manifest_doc = execution_doc

                    try:
                        reservation = metadb.preallocate_or_adopt_profile_run_owner(
                            submission_id, uid, auth_canvas, operational_canvas,
                            req.node_id, port_id, authoritative_digest,
                            input_manifest=manifest,
                            execution_manifest_sha256=execution_sha256,
                            execution_manifest_doc=execution_doc,
                            request_id=request_id,
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
    req.graph = _resolve_parameters(
        req.graph, req.parameter_bindings, req.target_node_id, deps)
    if req.input_manifest is not None and req.target_node_id is None:
        raise HTTPException(400, "schema inputManifest requires targetNodeId")
    _reject_invalid(req.graph, deps, req.target_node_id)
    graph = _target_execution_graph(req.graph, req.target_node_id)
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(graph, deps, req.target_node_id)
    if req.input_manifest is not None:
        graph, _manifest = _inspection_manifest_graph(
            graph, req.target_node_id, req.input_manifest, deps)
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
    req.graph = _resolve_parameters(
        req.graph, req.parameter_bindings, req.target_node_id, deps)
    _reject_invalid(req.graph, deps, req.target_node_id)
    graph = _target_execution_graph(req.graph, req.target_node_id)
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)
    _reject_invalid(graph, deps, req.target_node_id)
    try:
        sizes = estimate_sizes(
            graph, deps.resolve_adapter, actuals=_actuals_for(graph, deps),
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
    req.graph = _resolve_parameters(
        req.graph, req.parameter_bindings, req.target_node_id, deps)
    _reject_invalid(req.graph, deps, req.target_node_id)
    graph = _target_execution_graph(req.graph, req.target_node_id)
    _reject_invalid(graph, deps, req.target_node_id)
    if not req.target_node_id:
        return {"regions": []}
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)
    try:
        regions = deps.controller.plan_summary(graph, req.target_node_id)
        plan = compiler.compile_plan(
            graph, req.target_node_id, deps.registry, deps.node_specs, deps.node_ir)
        runner = _route_by_capability(
            deps, deps.pick_runner(plan, uid), graph, req.target_node_id)
        warning = _destination_credential_preflight(deps, runner, plan, graph)
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
    req.graph = _resolve_parameters(
        req.graph, req.parameter_bindings, req.target_node_id, deps)
    _reject_invalid(req.graph, deps, req.target_node_id)
    graph = _target_execution_graph(req.graph, req.target_node_id)
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)
    _reject_invalid(graph, deps, req.target_node_id)
    if not req.target_node_id:
        return JoinAnalysis(note="no join node selected")
    try:
        cols = schema_for_graph(graph, deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, storage=deps.storage)
        return rel.analyze_join(
            graph, req.target_node_id, cols, deps.catalog, deps.resolve_adapter,
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
    req.graph = _resolve_parameters(
        req.graph, req.parameter_bindings, req.target_node_id, deps)
    if _external_wait_request(deps, req.graph, req.target_node_id) is not None:
        if req.input_manifest is not None:
            raise HTTPException(409, "external-wait tasks do not accept input manifests")
        return RunEstimate(placement="local", needs_confirm=False)
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
              input_manifest: list[dict[str, str]] | None = None,
              write_intent: WriteIntent | None = None,
              parameter_bindings=None):
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
    else:
        # Open mode still has selected dev-user ownership. Keep ad-hoc execution, but do not let it
        # bypass promoted Transform ownership or an already-retained shared-Canvas capability.
        _require_graph_read_access(graph, uid)
    bindings = parameter_bindings or []
    submitted_target = next((node for node in graph.nodes if node.id == target_node_id), None)
    if write_intent is not None and (submitted_target is None or submitted_target.type != "write"):
        raise HTTPException(400, "writeIntent requires a Write target")
    if write_intent is not None and submission_id is None:
        raise HTTPException(400, "writeIntent requires a submissionId")
    graph_canvas = str(getattr(graph, "id", "") or "") or None
    operational_canvas = auth_canvas or (
        graph_canvas if graph_canvas is not None and metadb.canvas_exists(graph_canvas) else None)

    # Response-loss replay compares canonical typed intent before resolving a mutable latest head. A
    # retained manifest already owns the exact revision; asking its provider again would turn an
    # idempotent retry into a new availability dependency. Fresh submissions still take the normal
    # resolver path below and freeze latest before any allocation.
    replay_graph: Graph | None = None

    def canonical_replay_graph() -> Graph:
        nonlocal replay_graph
        if replay_graph is None:
            replay_graph = _resolve_parameters(
                graph, bindings, target_node_id, deps, freeze_latest=False)
        return replay_graph

    retained_local_admission: dict | None = None
    retained_local_manifest: dict | None = None
    retained_local_execution_doc: str | None = None
    if submission_id is not None:
        if operational_canvas is not None:
            task_id = metadb.durable_task_submission_id(
                uid, operational_canvas, submission_id)
            existing_task = metadb.durable_task(task_id)
            if (existing_task is not None
                    and existing_task.get("execution_manifest_sha256") is not None):
                try:
                    status = _adopt_manifest_durable_task(
                        deps, existing_task, canonical_replay_graph(), target_node_id,
                        input_manifest, write_intent)
                except metadb.DurableTaskSubmissionConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                return status, None

        local_run_id = metadb.local_run_submission_id(
            uid, operational_canvas, submission_id)
        retained_local_admission = metadb.local_run_input_admission(local_run_id)
        retained_sha256 = (
            retained_local_admission.get("execution_manifest_sha256")
            if retained_local_admission is not None else None)
        if isinstance(retained_sha256, str):
            try:
                retained_local_manifest, retained_local_execution_doc = (
                    _validate_retained_manifest_replay(
                        retained_sha256, canonical_replay_graph(), target_node_id,
                        input_manifest, write_intent)
                )
            except metadb.DurableTaskSubmissionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            current = metadb.get_run_state(local_run_id)
            if current is not None:
                return RunStatus.model_validate(current), _runner_for(
                    local_run_id, deps=deps)

    if retained_local_manifest is None:
        graph = _resolve_parameters(graph, bindings, target_node_id, deps)
        intent_graph = graph.model_copy(deep=True)
    else:
        graph = canonical_replay_graph()
        graph._parameter_bindings = copy.deepcopy(
            retained_local_manifest.get("parameters") or [])
        intent_graph = graph.model_copy(deep=True)
        if input_manifest is None:
            assert retained_local_admission is not None
            input_manifest = list(retained_local_admission["manifest"])
        if write_intent is None and retained_local_manifest.get("write_intent") is not None:
            write_intent = WriteIntent.model_validate(
                retained_local_manifest["write_intent"])

    # Capture caller intent before catalog references are resolved or private exact-revision bindings are
    # attached. Kernel and isolated-local transports mint an id when a non-browser caller has none; the
    # browser-supplied id remains stable across response-loss retries.
    target = next((node for node in graph.nodes if node.id == target_node_id), None)
    # A response-loss retry adopts its already-frozen Task before touching a Source, destination, or
    # schema. Canonical rows compare the retained manifest; pre-0022 rows keep their original digest
    # and frozen triple without being silently upgraded or reinterpreted.
    if (submission_id is not None and target is not None and target.type == "write"
            and operational_canvas is not None and metadb.canvas_exists(operational_canvas)):
        task_id = metadb.durable_task_submission_id(uid, operational_canvas, submission_id)
        existing_task = metadb.durable_task(task_id)
        if (existing_task is not None
                and existing_task.get("execution_manifest_sha256") is not None):
            try:
                status = _adopt_manifest_durable_task(
                    deps, existing_task, intent_graph, target_node_id,
                    input_manifest, write_intent)
            except metadb.DurableTaskSubmissionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            return status, None
        if existing_task is not None:
            try:
                frozen_intent = WriteIntent.model_validate(existing_task["write_intent"])
                if write_intent is not None and write_intent != frozen_intent:
                    raise metadb.DurableTaskSubmissionConflict(
                        "durable task submission does not match its frozen admission")
                if existing_task["task_kind"] == "external_wait":
                    if input_manifest is not None:
                        raise metadb.DurableTaskSubmissionConflict(
                            "durable task submission does not match its frozen admission")
                    request = _external_wait_request(deps, intent_graph, target_node_id)
                    if request is None:
                        raise metadb.DurableTaskSubmissionConflict(
                            "durable task submission does not match its frozen admission")
                    semantic = _local_run_intent_sha256(
                        intent_graph, target_node_id, write_intent=frozen_intent)
                    replay_sha256 = hashlib.sha256(
                        f"{semantic}\0{request.model_dump_json()}".encode()).hexdigest()
                else:
                    replay_sha256 = _local_run_intent_sha256(
                        intent_graph, target_node_id, input_manifest, frozen_intent)
                if replay_sha256 != existing_task.get("intent_sha256"):
                    raise metadb.DurableTaskSubmissionConflict(
                        "durable task submission does not match its frozen admission")
            except metadb.DurableTaskSubmissionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            return _resume_durable_task(deps, existing_task), None
    # Every provider Source must cross exact admission before any backend/controller may allocate.
    # This guard is backend-wide and independent of a caller-supplied submissionId: mutable-only
    # providers can still serve bounded previews, but they never reach a runner.
    try:
        source_nodes = _local_run_source_nodes(graph, target_node_id)
    except (KeyError, graph_mod.CycleError):
        _reject_invalid(graph, deps, target_node_id)
        raise
    provider_sources = [
        node for node in source_nodes
        if workspace_providers.is_provider_dataset_uri(str(
            node.data.get("config", {}).get("uri") or ""))
    ]
    if provider_sources:
        _reject_invalid(graph, deps, target_node_id)
        if input_manifest is None:
            input_manifest = _resolve_local_run_manifest(graph, target_node_id, deps)
        else:
            _bind_local_run_manifest(graph, input_manifest, deps, target_node_id)

    external_request = _external_wait_request(deps, graph, target_node_id)
    if external_request is not None:
        if input_manifest is not None or submission_id is None:
            raise HTTPException(
                409, "external-wait tasks require a submissionId and no inputs")
        admission = _write_admission_for_graph(
            deps, graph, str(target_node_id), uid, str(submission_id),
            supplied=write_intent, direct_local=True)
        if (not admission.managed or admission.intent is None
                or admission.intent.mode not in ("create", "replace")
                or admission.intent.destination.provider != "managed-local-file"
                or admission.intent.partitions):
            raise HTTPException(409, admission.blocker or "external-wait Write is not managed-local")
        operational_canvas = auth_canvas or str(getattr(graph, "id", "") or "")
        if not operational_canvas or not metadb.canvas_exists(operational_canvas):
            raise HTTPException(409, "durable external waits require a saved canvas")
        execution_sha256, execution_doc = _admitted_execution_manifest(
            intent_graph, target_node_id=target_node_id, target_port_id=None,
            input_manifest=[], write_intent=admission.intent, deps=deps)
        task, _created = metadb.submit_durable_external_wait_task(
            uid=uid, canvas_id=operational_canvas, submission_id=str(submission_id),
            target_node_id=str(target_node_id), intent_sha256=execution_sha256,
            graph_doc=intent_graph.model_dump(by_alias=True, mode="json"),
            provider_kind=external_request.provider_kind,
            operation=external_request.operation, document_json=external_request.document_json,
            write_intent=admission.intent.model_dump(by_alias=True, mode="json"),
            execution_manifest_sha256=execution_sha256,
            execution_manifest_doc=execution_doc)
        from hub.external_wait_tasks import recover
        recover(deps)
        return RunStatus.model_validate(task["status_doc"]), None
    # checkpoint:true routes to a durable task only with a submissionId, else it stays the in-run region-split marker.
    fanout_shape = (
        _bounded_fanout_write_shape(graph, target_node_id) if submission_id is not None else None)
    if fanout_shape is not None:
        _source, checkpoint_select, _identity_select, write = fanout_shape
        graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)
        _reject_invalid(graph, deps, target_node_id)
        if input_manifest is not None:
            graph = _bind_local_run_manifest(graph, input_manifest, deps, target_node_id)
        _reject_invalid(graph, deps, target_node_id)
        admission = _write_admission_for_graph(
            deps, graph, str(write.id), uid, str(submission_id),
            supplied=write_intent, direct_local=True)
        if (not admission.managed or admission.intent is None
                or admission.intent.mode not in ("create", "replace")
                or admission.intent.destination.provider != "managed-local-file"
                or admission.intent.partitions):
            raise HTTPException(
                409, admission.blocker or "bounded fan-out Write is not managed-local create/replace")
        operational_canvas = auth_canvas or str(getattr(graph, "id", "") or "")
        if not operational_canvas or not metadb.canvas_exists(operational_canvas):
            raise HTTPException(409, "bounded fan-out tasks require a saved canvas")
        manifest = (input_manifest if input_manifest is not None
                    else _resolve_local_run_manifest(graph, write.id, deps))
        if len(manifest) != 1:
            raise HTTPException(409, "bounded fan-out tasks require exactly one Source revision")
        stable_manifest = [{
            **item, "resolved_at": "1970-01-01T00:00:00+00:00",
        } for item in manifest]
        execution_sha256, execution_doc = _admitted_execution_manifest(
            intent_graph, target_node_id=write.id, target_port_id=None,
            input_manifest=stable_manifest, write_intent=admission.intent, deps=deps)
        manifest_admission = execution_manifest_admission(execution_sha256, execution_doc)
        from hub.linear_checkpoint_tasks import checkpoint_identity, graph_prefix_sha256
        task_id = metadb.durable_task_submission_id(
            uid, operational_canvas, str(submission_id))
        port_id = "out"
        manifest_payload = json.dumps(
            manifest_admission["input_manifest"], sort_keys=True, separators=(",", ":"))
        try:
            task, _created = metadb.submit_linear_checkpoint_task(
                uid=uid, canvas_id=operational_canvas, submission_id=str(submission_id),
                final_target_node_id=str(write.id),
                checkpoint_id=checkpoint_identity(task_id, checkpoint_select.id, port_id),
                checkpoint_node_id=str(checkpoint_select.id), output_port_id=port_id,
                task_intent_sha256=execution_sha256,
                graph_prefix_sha256=graph_prefix_sha256(
                    Graph.model_validate(manifest_admission["graph_doc"]), checkpoint_select.id),
                input_manifest_sha256=hashlib.sha256(manifest_payload.encode()).hexdigest(),
                graph_doc=intent_graph.model_dump(by_alias=True, mode="json"),
                input_manifest=stable_manifest,
                write_intent=admission.intent.model_dump(by_alias=True, mode="json"),
                execution_manifest_sha256=execution_sha256,
                execution_manifest_doc=execution_doc,
                task_kind="bounded_fanout_write")
        except metadb.DurableTaskSubmissionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        from hub.bounded_fanout_tasks import dispatch as dispatch_fanout
        dispatch_fanout(task["task_id"], deps)
        status = metadb.durable_task(task["task_id"], include_admission=False)
        assert status is not None
        return RunStatus.model_validate(status["status_doc"]), None
    linear_shape = (
        _linear_checkpoint_shape(graph, target_node_id) if submission_id is not None else None)
    if linear_shape is not None:
        _source, select, write = linear_shape
        graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)
        _reject_invalid(graph, deps, target_node_id)
        if input_manifest is not None:
            graph = _bind_local_run_manifest(graph, input_manifest, deps, target_node_id)
        _reject_invalid(graph, deps, target_node_id)
        admission = _write_admission_for_graph(
            deps, graph, str(write.id), uid, str(submission_id),
            supplied=write_intent, direct_local=True)
        if (not admission.managed or admission.intent is None
                or admission.intent.mode not in ("create", "replace")
                or admission.intent.destination.provider != "managed-local-file"
                or admission.intent.partitions):
            raise HTTPException(
                409, admission.blocker or "linear checkpoint Write is not managed-local create/replace")
        operational_canvas = auth_canvas or str(getattr(graph, "id", "") or "")
        if not operational_canvas or not metadb.canvas_exists(operational_canvas):
            raise HTTPException(409, "linear checkpoint tasks require a saved canvas")
        manifest = (input_manifest if input_manifest is not None
                    else _resolve_local_run_manifest(graph, write.id, deps))
        if len(manifest) != 1:
            raise HTTPException(409, "linear checkpoint tasks require exactly one Source revision")
        # Wall-clock resolved_at must not fork the durable identity across response-loss replays.
        stable_manifest = [{
            **item, "resolved_at": "1970-01-01T00:00:00+00:00",
        } for item in manifest]
        execution_sha256, execution_doc = _admitted_execution_manifest(
            intent_graph, target_node_id=write.id, target_port_id=None,
            input_manifest=stable_manifest, write_intent=admission.intent, deps=deps)
        manifest_admission = execution_manifest_admission(execution_sha256, execution_doc)
        from hub.linear_checkpoint_tasks import checkpoint_identity, graph_prefix_sha256
        task_id = metadb.durable_task_submission_id(
            uid, operational_canvas, str(submission_id))
        port_id = "out"
        manifest_payload = json.dumps(
            manifest_admission["input_manifest"], sort_keys=True, separators=(",", ":"))
        try:
            task, _created = metadb.submit_linear_checkpoint_task(
                uid=uid, canvas_id=operational_canvas, submission_id=str(submission_id),
                final_target_node_id=str(write.id),
                checkpoint_id=checkpoint_identity(task_id, select.id, port_id),
                checkpoint_node_id=str(select.id), output_port_id=port_id,
                task_intent_sha256=execution_sha256,
                graph_prefix_sha256=graph_prefix_sha256(
                    Graph.model_validate(manifest_admission["graph_doc"]), select.id),
                input_manifest_sha256=hashlib.sha256(manifest_payload.encode()).hexdigest(),
                graph_doc=intent_graph.model_dump(by_alias=True, mode="json"),
                input_manifest=stable_manifest,
                write_intent=admission.intent.model_dump(by_alias=True, mode="json"),
                execution_manifest_sha256=execution_sha256,
                execution_manifest_doc=execution_doc)
        except metadb.DurableTaskSubmissionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        from hub.linear_checkpoint_tasks import dispatch as dispatch_linear
        dispatch_linear(task["task_id"], deps)
        status = metadb.durable_task(task["task_id"], include_admission=False)
        assert status is not None
        return RunStatus.model_validate(status["status_doc"]), None
    graph_mod.resolve_source_refs(graph, deps.catalog.resolve_ref)  # source may name a catalog table (F50)
    _reject_invalid(graph, deps, target_node_id)
    # Keep the durable graph on logical Source URIs. A supplied exact manifest binds private
    # _input_* execution fields below; those belong only on the schema/plan/worker copy and must never
    # cross into Task, Jobs, receipt, or lineage persistence.
    durable_graph = graph.model_copy(deep=True)
    if input_manifest is not None:
        # Bind before validation/compile/estimate so schema checks and execution see the same exact
        # population as the preview, even if latest moved after the preview was rendered.
        graph = _bind_local_run_manifest(graph, input_manifest, deps, target_node_id)
    _reject_invalid(graph, deps, target_node_id)
    write_admission = None
    effective_write_intent = write_intent
    if target is not None and target.type == "write" and submission_id is not None:
        write_admission = _write_admission_for_graph(
            deps, graph, target_node_id, uid, submission_id, supplied=write_intent)
        if write_admission.managed:
            if write_admission.blocker or write_admission.intent is None:
                raise HTTPException(409, write_admission.blocker or "write admission failed")
            effective_write_intent = write_admission.intent
            _inject_write_intent(graph, target_node_id, write_admission.intent)
            _inject_write_intent(durable_graph, target_node_id, write_admission.intent)
    elif target is not None and target.type == "write":
        # No submissionId (direct API / MCP), so the admission gate above was skipped; still reject an
        # unknown destination with a typed 4xx before the dispatch claim exists.
        _preflight_write_target_destination(deps, graph, str(target_node_id))
    intent_sha256 = _local_run_intent_sha256(
        intent_graph, target_node_id, input_manifest, effective_write_intent)
    plan = compiler.compile_plan(graph, target_node_id, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    _require_satisfiable_hard_requirements(deps, graph, target_node_id)
    output_target = _run_output_preflight(plan, target_node_id)
    runner = _route_by_capability(
        deps, deps.pick_runner(plan, uid), graph, target_node_id
    )  # honor requirements only in the target's executable cone
    if (write_admission is not None and write_admission.managed
            and not _runner_supports_managed_local_write_intents(deps, runner)):
        raise HTTPException(
            409, "the selected execution backend cannot consume the managed-local write admission; "
            "discard it and retry with local-out-of-core")
    multi_output = False
    if output_target is not None:
        multi_output = _require_backend_run_output_support(
            runner, graph, output_target, deps)
    _require_destination_credential_preflight(deps, runner, plan, graph)
    rows, byts, sizes = _cone_size(graph, target_node_id, deps)
    managed_write = bool(write_admission is not None and write_admission.managed)
    controller_regions = (_controller_regions_for_run(
        deps, graph, target_node_id, output_target, sizes, multi_output=multi_output)
        if multi_output or managed_write else None)
    if managed_write and controller_regions:
        raise HTTPException(
            409, "the selected execution owner cannot consume the managed-local write admission; "
            "discard it and retry with an in-process local plan")
    est = runner.estimate(plan, rows, byts)
    if est.needs_confirm and not confirmed:
        raise RunNeedsConfirm(est)
    if (managed_write and effective_write_intent is not None
            and effective_write_intent.mode in ("create", "replace")):
        # This one consumer transfers ownership to a durable Task before any worker dispatch —
        # including the default per-canvas kernel, whose managed-local create/replace is now admitted
        # and published by this same durable owner. Append, provider-neutral, placed, subprocess, and
        # Ray paths retain their current lifecycle. The stable local submission id also remains the
        # Write provenance identity.
        operational_canvas = auth_canvas or (str(getattr(graph, "id", "") or "") or None)
        if operational_canvas is None or not metadb.canvas_exists(operational_canvas):
            raise HTTPException(409, "durable managed-local writes require a saved canvas")
        assert submission_id is not None
        task_id = metadb.durable_task_submission_id(
            uid, operational_canvas, str(submission_id))
        prior = metadb.durable_task(task_id)
        prior_manifest = (
            prior["input_manifest"]
            if prior is not None and prior.get("task_kind") == "managed_local_write" else None)
        for admission_attempt in range(2):
            candidates: list[dict[str, str]] = []
            try:
                manifest = (input_manifest if input_manifest is not None
                            else prior_manifest if prior_manifest is not None
                            else _resolve_local_run_manifest(
                                graph, target_node_id, deps,
                                materialize_local_files=True,
                                local_file_candidates=candidates,
                            ))
                execution_sha256, execution_doc = _admitted_execution_manifest(
                    intent_graph, target_node_id=target_node_id, target_port_id=None,
                    input_manifest=manifest, write_intent=effective_write_intent, deps=deps)
                task, _created = metadb.submit_durable_local_write_task(
                    uid=uid, canvas_id=operational_canvas,
                    submission_id=str(submission_id),
                    target_node_id=str(target_node_id), intent_sha256=execution_sha256,
                    graph_doc=intent_graph.model_dump(by_alias=True, mode="json"),
                    input_manifest=manifest,
                    write_intent=effective_write_intent.model_dump(
                        by_alias=True, mode="json"),
                    execution_manifest_sha256=execution_sha256,
                    execution_manifest_doc=execution_doc,
                    local_file_candidates=candidates,
                )
                break
            except metadb.LocalFileInputAdmissionRetry as exc:
                if (input_manifest is not None or prior_manifest is not None
                        or admission_attempt > 0):
                    raise APIError(
                        409, "ordinary local input changed during exact admission; retry",
                        code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED,
                        retryable=True,
                    ) from exc
            finally:
                from hub.local_run_inputs import (
                    finalize_durable_task_local_file_candidates,
                )
                if candidates:
                    finalize_durable_task_local_file_candidates(
                        deps.storage, candidates, task_id)
        from hub.durable_tasks import dispatch
        dispatch(task["id"], deps)
        return RunStatus.model_validate(task["status_doc"]), None
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
    local_sources = _local_run_source_nodes(graph, target_node_id)
    auto_admittable_local_sources = bool(local_sources) and all(
        _source_supports_automatic_local_admission(node, deps)
        for node in local_sources)
    local_runner_admission = bool(
        runner is deps.runner
        and (submission_id is not None or auto_admittable_local_sources))
    built_in_local_transport = transport_requires_admission or local_runner_admission
    if built_in_local_transport and not controller_regions and submission_id is None:
        submission_id = str(uuid.uuid4())
    local_admission = bool(
        not controller_regions
        and built_in_local_transport
    )
    dispatch_graph = graph
    dispatch_manifest: list[dict[str, str]] | None = None
    prebound_local_run_id: str | None = None
    if local_admission:
        assert submission_id is not None
        graph_canvas = (str(getattr(graph, "id", "") or "") or None)
        operational_canvas = auth_canvas or (
            graph_canvas if graph_canvas is not None and metadb.canvas_exists(graph_canvas) else None)
        if isinstance(runner, KernelBackend) and operational_canvas is None:
            raise HTTPException(409, "kernel runs require a saved canvas")
        prebound_local_run_id = metadb.local_run_submission_id(
            uid, operational_canvas, str(submission_id))
        retained_sha256 = (
            retained_local_admission.get("execution_manifest_sha256")
            if retained_local_admission is not None else None)
        reuse_retained = bool(
            retained_local_admission is not None
            and retained_local_admission.get("run_id") == prebound_local_run_id
            and isinstance(retained_sha256, str)
            and retained_local_execution_doc is not None
        )
        # A retry already compared with retained exact admission reuses its original bytes instead
        # of re-hashing deliberately unresolved latest intent under the same submission id.
        persisted = (list(retained_local_admission["manifest"])
                     if reuse_retained and retained_local_admission is not None else None)
        execution_sha256 = retained_sha256 if reuse_retained else None
        execution_doc = retained_local_execution_doc if reuse_retained else None
        prior_manifest = (None if reuse_retained
                          else metadb.local_run_input_manifest(prebound_local_run_id))
        for admission_attempt in (() if reuse_retained else range(2)):
            candidates: list[dict[str, str]] = []
            try:
                manifest = (input_manifest if input_manifest is not None
                            else prior_manifest if prior_manifest is not None
                            else _resolve_local_run_manifest(
                                graph, target_node_id, deps,
                                materialize_local_files=True,
                                local_file_candidates=candidates,
                            ))
                execution_sha256, execution_doc = _admitted_execution_manifest(
                    intent_graph,
                    target_node_id=target_node_id,
                    target_port_id=None,
                    input_manifest=manifest,
                    write_intent=effective_write_intent,
                    deps=deps,
                )
                prebound_local_run_id, _created = metadb.admit_local_run_inputs(
                    uid=uid, canvas_id=operational_canvas, submission_id=str(submission_id),
                    target_node_id=target_node_id, intent_sha256=str(intent_sha256), manifest=manifest,
                    execution_manifest_sha256=execution_sha256,
                    execution_manifest_doc=execution_doc,
                    local_file_candidates=candidates,
                )
                break
            except metadb.LocalFileInputAdmissionRetry as exc:
                # A caller-supplied or already-persisted manifest must never be rebound to a different
                # artifact here. Only a fresh server-resolved ordinary file may be snapshotted again.
                if (input_manifest is not None or prior_manifest is not None
                        or admission_attempt > 0):
                    raise APIError(
                        409, "ordinary local input changed during exact admission; retry",
                        code=APIErrorCode.LOCAL_RUN_INPUT_BINDING_FAILED,
                        retryable=True,
                    ) from exc
            finally:
                from hub.local_run_inputs import finalize_local_file_candidates
                if candidates:
                    finalize_local_file_candidates(
                        deps.storage, candidates, prebound_local_run_id)
        if persisted is None:
            persisted = metadb.local_run_input_manifest(prebound_local_run_id)
        if persisted is None or execution_sha256 is None or execution_doc is None:
            raise RuntimeError("local run admission was not persisted")
        dispatch_manifest = persisted
        graph._execution_manifest_sha256 = execution_sha256
        graph._execution_manifest_doc = execution_doc
        dispatch_graph = _bind_local_run_manifest(graph, persisted, deps, target_node_id)
        if isinstance(runner, KernelBackend):
            # A newly spawned kernel runs boot recovery before serving. Start it only after admission and
            # exact reopen, but before the queued dispatch claim exists; otherwise that boot recovery can
            # correctly mistake the still-unstamped queued row for a dead prior-hub run.
            assert operational_canvas is not None
            runner._ensure_kernel(operational_canvas)
        # Install the selected owner before the durable dispatch claim becomes visible. A concurrent
        # response-loss replay can observe that claim immediately; without this ordering it would route
        # the same run through the fallback runner until the first call returned from dispatch.
        prior_owner = deps.run_index.get(prebound_local_run_id)
        if prior_owner is None:
            deps.run_index[prebound_local_run_id] = runner
        try:
            claimed_status, should_dispatch = metadb.claim_local_run_dispatch(
                run_id=prebound_local_run_id, uid=uid, auth_canvas_id=auth_canvas,
                request_id=request_id,
            )
        except BaseException:
            if prior_owner is None and deps.run_index.get(prebound_local_run_id) is runner:
                deps.run_index.pop(prebound_local_run_id, None)
            raise
        if not should_dispatch:
            if prior_owner is None and deps.run_index.get(prebound_local_run_id) is runner:
                deps.run_index.pop(prebound_local_run_id, None)
            return RunStatus(**claimed_status), _runner_for(
                prebound_local_run_id, deps=deps)
    if graph._execution_manifest_sha256 is None:
        execution_sha256, execution_doc = _admitted_execution_manifest(
            intent_graph,
            target_node_id=target_node_id,
            target_port_id=None,
            input_manifest=input_manifest,
            write_intent=effective_write_intent,
            deps=deps,
        )
        graph._execution_manifest_sha256 = execution_sha256
        graph._execution_manifest_doc = execution_doc
        dispatch_graph._execution_manifest_sha256 = execution_sha256
        dispatch_graph._execution_manifest_doc = execution_doc
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
                run_id, uid, auth_canvas, operational_canvas_id=operational_canvas,
                execution_manifest_sha256=graph._execution_manifest_sha256,
                execution_manifest_doc=graph._execution_manifest_doc,
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
                # A missing in-process receipt proves no worker ran; the DB-backed kernel instead
                # returns the queued claim, so an unclaimed kernel run is also treated as pre-dispatch.
                status = None
                try:
                    status = runner.status(prebound_local_run_id)
                except KeyError:
                    pass
                if status is None or _kernel_run_provably_undispatched(
                        runner, prebound_local_run_id, status):
                    metadb.fail_claimed_local_run_dispatch(
                        prebound_local_run_id, f"{type(exc).__name__}: {exc}")
                    if deps.run_index.get(prebound_local_run_id) is runner:
                        deps.run_index.pop(prebound_local_run_id, None)
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
            req.write_intent,
            req.parameter_bindings,
        )
    except RunNeedsConfirm:
        raise HTTPException(409, "run needs confirmation (large or unknown size — a full pass)")
    except metadb.DurableTaskSubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
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
    task_auth = metadb.durable_task_auth(run_id)
    if task_auth is not None:
        creator, canvas_id = task_auth
        if canvas_id is None:
            return creator == uid
        return creator == uid or metadb.canvas_role(canvas_id, uid) is not None
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
    task_auth = metadb.durable_task_auth(run_id)
    if task_auth is not None:
        creator, canvas_id = task_auth
        if canvas_id is None:
            return creator == uid
        return (creator == uid and metadb.canvas_role(canvas_id, uid) is not None) \
            or metadb.canvas_role(canvas_id, uid) in _RUN_MUTATE_ROLES
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
    # Opaque checkpoint client key from Jobs never carries a storage URI; resolve via SQL + auth.
    if str(node_id).startswith("checkpoint:"):
        task_id = str(node_id).split(":", 1)[1]
        if task_id != str(run_id):
            raise HTTPException(404, "run output not found")
        resolved = metadb.resolve_checkpoint_full_result(run_id)
        if resolved is None:
            raise HTTPException(404, "run output not found")
        return RunOutput(
            node_id=resolved["node_id"], port_id=resolved["port_id"],
            wire="dataset", publication_kind="result", outcome="committed",
            uri=resolved["uri"], rows=resolved["rows"],
        )
    outputs: list[RunOutput] = []
    try:
        outputs = _durable_run_outputs(run_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    output = next((candidate for candidate in outputs
                   if candidate.node_id == node_id and candidate.port_id == port_id), None)
    if output is not None:
        return output
    resolved = metadb.resolve_checkpoint_full_result(run_id)
    if (resolved is not None and node_id == resolved["node_id"]
            and port_id == resolved["port_id"]):
        return RunOutput(
            node_id=resolved["node_id"], port_id=resolved["port_id"],
            wire="dataset", publication_kind="result", outcome="committed",
            uri=resolved["uri"], rows=resolved["rows"],
        )
    raise HTTPException(404, "run output not found")


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


def _runner_for(run_id: str, *, fallback: bool = True, deps=None):
    """Resolve an in-process owner, including a durable backend reconstructed after restart."""
    deps = deps or get_deps()
    owner = deps.run_index.get(run_id)
    if owner is None and metadb.run_kernel_id(run_id) is not None:
        kernel_backend = getattr(deps, "kernel_backend", lambda: None)()
        if kernel_backend is not None:
            owner = kernel_backend
            deps.run_index[run_id] = owner
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
    task = metadb.durable_task(run_id, include_admission=False)
    if task is not None:
        return RunStatus.model_validate(task["status_doc"])
    try:
        return _runner_for(run_id).status(run_id)
    except KeyError:
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
    if (metadb.durable_task_auth(run_id) is None
            and st.status in ("queued", "running") and metadb.run_stalled(run_id, _STALL_S)):
        st = st.model_copy(update={"stalled": True})  # copy — don't mutate the runner's live object
    return st


@router.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str, uid: str = Depends(current_user)) -> RunStatus:
    from hub.observability import AuditAction, AuditOutcome, emit_audit, get_request_id
    _require_run_mutate_access(run_id, uid)  # only owner/editor may disrupt a shared canvas run
    deps = get_deps()
    if metadb.durable_task_auth(run_id) is not None:
        from hub.durable_tasks import request_cancel
        task = request_cancel(run_id)
        if task is None:  # ownership was checked above; deletion can race only as a not-found outcome
            raise HTTPException(404, f"run '{run_id}' not found")
        return RunStatus.model_validate(task["status_doc"])
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


class DurableTaskRetryRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    action_id: uuid.UUID


@router.post("/run/{run_id}/retry", response_model=RunStatus)
def run_retry(
        run_id: str, req: DurableTaskRetryRequest,
        uid: str = Depends(current_user)) -> RunStatus:
    """Explicitly create the next bounded attempt for a durable Task."""
    _require_run_mutate_access(run_id, uid)
    if metadb.durable_task_auth(run_id) is None:
        raise HTTPException(409, "only durable tasks support retry")
    from hub.durable_tasks import retry
    try:
        task = retry(run_id, str(req.action_id), get_deps())
    except KeyError as exc:
        raise HTTPException(404, f"run '{run_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RunStatus.model_validate(task["status_doc"])
