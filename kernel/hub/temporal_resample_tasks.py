from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from hub import metadb
from hub.compound_datasets import CompoundManifestError

from hub.compound_fixture import (
    FixtureUnavailable, _MEMBER_DIGESTS, _MEMBERS, _TARGET, _manifest, fixture_uri,
)
from hub.deps import get_deps
from hub.local_writes import write_managed_local_file
from hub.models import (
    ExactDatasetRef, RunStatus, TemporalResampleOutputReceiptV1, TemporalResampleTaskResponseV1,
    TemporalResampleWriteRequestV1, WriteIntent,
)
from hub.plugins.adapters import RevisionPermissionLost, RevisionProviderOffline, RevisionUnavailable
from hub.routers.dataset_views import _defined_relation, _open_exact, _stored_definition
from hub.storage import ManagedSourceReadError
from hub.sqlpolicy import quote_identifier
from hub.temporal_evidence import EvidenceWindow, _source_bounds
from hub.temporal_publication import register_parent
from hub.temporal_resample_diagnostics import (
    TemporalResampleDiagnosticCode, temporal_failure_retryable,
)
from hub.temporal_resample import (
    DatasetViewIdentity, FieldSelection, FixedGridTarget, PointObservation, ResampleWindow,
    TemporalResampleError, TemporalResampleSpecV1, _materialization_row,
    _output_schema, build_resample_candidate, preflight_fixed_grid,
)


_active_lock = threading.Lock()
_active: dict[str, threading.Thread] = {}
_MAX_POINTS = 10_000


class TemporalResampleAdmissionError(ValueError):
    """One redacted, stable failure category for exact temporal inputs."""

    def __init__(self, code: TemporalResampleDiagnosticCode):
        self.code = code
        super().__init__(code)


class _TemporalCandidateChanged(RuntimeError):
    """The recomputed exact inputs no longer match durable admission."""


_ADMISSION_CODES = frozenset({
    TemporalResampleDiagnosticCode.PERMISSION_DENIED,
    TemporalResampleDiagnosticCode.PROVIDER_OFFLINE,
    TemporalResampleDiagnosticCode.REVISION_UNAVAILABLE,
    TemporalResampleDiagnosticCode.INPUT_PARTIAL,
    TemporalResampleDiagnosticCode.INPUT_CORRUPT,
    TemporalResampleDiagnosticCode.INPUT_TRUNCATED,
    TemporalResampleDiagnosticCode.INPUT_DUPLICATE,
    TemporalResampleDiagnosticCode.INPUT_SAMPLED,
    TemporalResampleDiagnosticCode.MISSING_FIELD,
    TemporalResampleDiagnosticCode.SPEC_INVALID,
})


def _admission(code: TemporalResampleDiagnosticCode | str) -> TemporalResampleAdmissionError:
    try:
        diagnostic = TemporalResampleDiagnosticCode(code)
    except ValueError as exc:
        raise RuntimeError("unknown temporal admission code") from exc
    if diagnostic not in _ADMISSION_CODES:
        raise RuntimeError("diagnostic code is not valid during temporal admission")
    return TemporalResampleAdmissionError(diagnostic)


def _authority(request: TemporalResampleWriteRequestV1):
    root = Path(get_deps().data_dir).resolve(strict=False) / _TARGET
    if not root.is_dir() or root.is_symlink():
        raise _admission("temporal_revision_unavailable")
    members: dict[str, tuple[str, str]] = {}
    for member_id in _MEMBERS:
        binding = metadb.catalog_revision_binding_for_uri(fixture_uri(member_id))
        if (binding is None or binding.get("uri") != fixture_uri(member_id)
                or not isinstance(binding.get("dataset_id"), str)):
            raise _admission("temporal_revision_unavailable")
        members[member_id] = (str(binding["dataset_id"]), _MEMBER_DIGESTS[member_id])
    manifest = _manifest(root, members)
    if (manifest.ref.dataset_id, manifest.ref.revision_id) != (
            request.compound_dataset_id, request.compound_revision_id):
        raise _admission("temporal_revision_unavailable")
    return manifest


def _view(uid: str, view_id: str, *, member, stream, index, start: int, end: int):
    view = _stored_definition(uid, view_id)
    if (view.dataset_ref.dataset_id, view.dataset_ref.revision_id) != (
            member.dataset_id, member.revision_id):
        raise _admission("temporal_spec_invalid")
    window = view.temporal_window
    if view.sampling.kind != "all":
        raise _admission("temporal_input_sampled")
    if window is None or int(window.start_tick) > start or int(window.end_tick) < end:
        raise _admission("temporal_input_partial")
    if window.time_domain != stream.clock.id:
        raise _admission("temporal_spec_invalid")
    if window.time_field != index.tick_field:
        raise _admission("temporal_missing_field")
    return view


def _points(*, view, member, index, episode_id: str, start: int, end: int,
            selected: tuple[str, ...]) -> tuple[PointObservation, ...]:
    fields = (index.observation_id_field, index.episode_id_field, index.tick_field, *selected)
    if index.tick_field is None:
        raise _admission("temporal_missing_field")
    if len(set(fields)) != len(fields):
        raise _admission("temporal_spec_invalid")
    try:
        with _open_exact(ExactDatasetRef(kind="exact", dataset_id=member.dataset_id,
                                         revision_id=member.revision_id), operation="temporal-resample") as source:
            relation = _defined_relation(source, view)
            if set(fields) - set(relation.columns):
                raise _admission("temporal_missing_field")
            episode, tick = quote_identifier(index.episode_id_field), quote_identifier(index.tick_field)
            literal = "'" + episode_id.replace("'", "''") + "'"
            rows = relation.filter(
                f"{episode} = {literal} AND {tick} >= {start} AND {tick} < {end}").project(
                    ", ".join(quote_identifier(field) for field in fields)).order(
                        f"{tick}, {quote_identifier(index.observation_id_field)}").limit(
                            _MAX_POINTS + 1).to_arrow_table().to_pylist()
    except TemporalResampleAdmissionError:
        raise
    except (RevisionPermissionLost, PermissionError) as exc:
        raise _admission("temporal_permission_denied") from exc
    except (RevisionProviderOffline, ConnectionError, TimeoutError) as exc:
        raise _admission("temporal_provider_offline") from exc
    except (RevisionUnavailable, ManagedSourceReadError) as exc:
        raise _admission("temporal_revision_unavailable") from exc
    except Exception as exc:
        raise _admission("temporal_input_corrupt") from exc
    if len(rows) > _MAX_POINTS:
        raise _admission("temporal_input_truncated")
    points: list[PointObservation] = []
    seen: set[str] = set()
    for row in rows:
        try:
            observation_id, tick_value = row[index.observation_id_field], row[index.tick_field]
            if not isinstance(observation_id, str) or not observation_id:
                raise _admission("temporal_input_corrupt")
            if observation_id in seen:
                raise _admission("temporal_input_duplicate")
            if type(tick_value) is not int:
                raise _admission("temporal_input_corrupt")
            seen.add(observation_id)
            values = {field: row[field] for field in selected}
            points.append(PointObservation(observation_id, tick_value, values))
        except TemporalResampleAdmissionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise _admission("temporal_input_corrupt") from exc
    return tuple(points)


def candidate_for_request(uid: str, request: TemporalResampleWriteRequestV1):
    try:
        manifest = _authority(request)
        streams = {item.id: item for item in manifest.streams}
        bindings = {(item.episode_id, item.stream_id): item for item in manifest.bindings}
        source, target = streams[request.source_stream_id], streams[request.target_stream_id]
        source_binding = bindings[(request.episode_id, request.source_stream_id)]
        target_binding = bindings.get((request.episode_id, request.target_stream_id))
        if source_binding.state != "present":
            raise _admission("temporal_input_partial")
        if not source_binding.member_id or source_binding.observation_index is None:
            raise _admission("temporal_spec_invalid")
        if request.fixed_grid is None and (
                target_binding is None or target_binding.state != "present"
                or not target_binding.member_id or target_binding.observation_index is None):
            raise _admission("temporal_input_partial")
        members = {item.id: item for item in manifest.members}
        if request.output_member_id in members:
            raise _admission("temporal_spec_invalid")
        mapping = next(item for item in manifest.clock_mappings if (
            item.source_clock_id, item.target_clock_id) == (source.clock.id, target.clock.id))
        if request.fixed_grid is None:
            assert request.window is not None
            time_domain = request.window.time_domain
            start, end = int(request.window.start_tick), int(request.window.end_tick)
        else:
            time_domain = request.fixed_grid.time_domain
            start, end = int(request.fixed_grid.start_tick), int(request.fixed_grid.end_tick)
        if time_domain != target.clock.time_domain:
            raise _admission("temporal_spec_invalid")
        if (request.fixed_grid is None and target_binding is not None
                and request.window is not None
                and request.window.time_field != target_binding.observation_index.tick_field):
            raise _admission("temporal_missing_field")
        source_start, source_end = _source_bounds(mapping, EvidenceWindow(target.clock.id, start, end))
        source_view = _view(uid, request.source_view_id, member=members[source_binding.member_id],
                            stream=source, index=source_binding.observation_index,
                            start=source_start, end=source_end)
        target_view = None
        if request.fixed_grid is None:
            assert target_binding is not None and target_binding.member_id is not None
            assert target_binding.observation_index is not None and request.target_view_id is not None
            target_view = _view(uid, request.target_view_id, member=members[target_binding.member_id],
                                stream=target, index=target_binding.observation_index, start=start, end=end)
        selected = tuple(FieldSelection(item.field, item.unit) for item in request.selected_fields)
        spec = TemporalResampleSpecV1(
            compound_dataset_id=manifest.ref.dataset_id, compound_revision_id=manifest.ref.revision_id,
            episode_id=request.episode_id, source_stream_id=request.source_stream_id,
            target_stream_id=request.target_stream_id, output_stream_id=request.output_stream_id,
            source_view=DatasetViewIdentity(source_view.dataset_ref.dataset_id, source_view.dataset_ref.revision_id,
                                            source_view.id, source_view.definition_sha256, source_view.semantic_sha256),
            target_view=(None if target_view is None else DatasetViewIdentity(
                target_view.dataset_ref.dataset_id, target_view.dataset_ref.revision_id,
                target_view.id, target_view.definition_sha256, target_view.semantic_sha256)),
            mapping=mapping, window=ResampleWindow(time_domain, start, end),
            tolerance_ticks=int(request.tolerance_ticks), selected_fields=selected,
            candidate_cap=request.candidate_cap, output_cap=request.output_cap,
            fixed_grid=(None if request.fixed_grid is None else FixedGridTarget(
                request.fixed_grid.target_clock_id, int(request.fixed_grid.rate_numerator),
                int(request.fixed_grid.rate_denominator), int(request.fixed_grid.phase_tick))))
        if spec.fixed_grid is not None:
            # This runs before either DatasetView is scanned, so impossible grids
            # cannot consume a read budget or create a durable publication attempt.
            spec = preflight_fixed_grid(manifest, spec)
        source_points = _points(view=source_view, member=members[source_binding.member_id],
                                index=source_binding.observation_index, episode_id=request.episode_id,
                                start=source_start, end=source_end,
                                selected=tuple(item.field for item in selected))
        if spec.fixed_grid is not None:
            target_points = ()
        else:
            assert target_view is not None and target_binding is not None
            assert target_binding.member_id is not None and target_binding.observation_index is not None
            target_points = _points(
                view=target_view, member=members[target_binding.member_id],
                index=target_binding.observation_index, episode_id=request.episode_id,
                start=start, end=end, selected=())
        candidate = build_resample_candidate(manifest, spec, source_points, target_points)
        expected = metadb._temporal_expected_output_schema_for(manifest, candidate.spec)
        write = WriteIntent.model_validate(request.write_intent)
        if [(item.name, item.type) for item in write.expected_schema] != expected:
            raise _admission("temporal_spec_invalid")
        return manifest, candidate
    except (FixtureUnavailable, CompoundManifestError, KeyError, StopIteration, TemporalResampleError) as exc:
        raise _admission("temporal_spec_invalid") from exc


def _table(manifest, candidate) -> pa.Table:
    schema = pa.schema([pa.field(item["name"], pa.type_for_alias(item["type"]), nullable=item["nullable"])
                        for item in _output_schema(manifest, candidate.spec)])
    return pa.Table.from_pylist([_materialization_row(candidate.spec, row) for row in candidate.rows], schema=schema)


def _status(task_id: str, *, phase: str, progress: float) -> dict:
    doc = RunStatus(run_id=task_id, status="running", target_node_id="temporal-resample",
                    progress=progress).model_dump()
    doc["temporal_resample_phase"] = phase
    return doc


def _worker(task_id: str, deps) -> None:
    token = f"{uuid.uuid4().hex}:{threading.get_ident()}"
    try:
        claimed = metadb.claim_temporal_resample_task(task_id, token)
        if claimed is None:
            return
        attempt_id = str(claimed["attempts"][-1]["id"])
        request = TemporalResampleWriteRequestV1.model_validate(claimed["temporal_resample_request"])
        try:
            prior = metadb.temporal_resample_task_result(task_id)
            if prior is not None:
                return
            if not metadb.update_temporal_resample_task_phase(
                    task_id, attempt_id, token, phase="recomputing", status_doc=_status(task_id, phase="recomputing", progress=.2)):
                return
            manifest, candidate = candidate_for_request(claimed["owner_id"], request)
            if candidate.digest != claimed["temporal_resample_candidate_sha256"]:
                raise _TemporalCandidateChanged
            if not metadb.heartbeat_durable_task(task_id, attempt_id, token) or metadb.durable_task_attempt_should_stop(task_id, attempt_id, token):
                raise RuntimeError("cancelled")
            if not metadb.update_temporal_resample_task_phase(
                    task_id, attempt_id, token, phase="publishing", status_doc=_status(task_id, phase="publishing", progress=.8)):
                return
            register_parent(owner_id=claimed["owner_id"], manifest=manifest)
            table = _table(manifest, candidate)
            context = metadb.TemporalResamplePublicationContext(
                owner_id=claimed["owner_id"], parent_manifest=manifest, candidate=candidate,
                output_member_id=request.output_member_id, output_revision_id=uuid.uuid4().hex,
                task_id=task_id, attempt_id=attempt_id, owner_token=token)
            write_managed_local_file(
                storage=deps.storage, catalog=deps.catalog, intent=WriteIntent.model_validate(request.write_intent),
                write_artifact=lambda uri: pq.write_table(table, uri),
                before_publish=lambda: _publish_fence(task_id, attempt_id, token), temporal_publication=context)
        except BaseException as exc:
            prior = metadb.temporal_resample_task_result(task_id)
            if prior is not None:
                return
            elif str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(task_id, attempt_id, token):
                status = RunStatus(run_id=task_id, status="cancelled", target_node_id="temporal-resample").model_dump()
            else:
                code = _worker_diagnostic(exc)
                status = RunStatus(run_id=task_id, status="failed", target_node_id="temporal-resample",
                                   error=code).model_dump()
            metadb.finish_durable_task_attempt(task_id, attempt_id, token, status)
    finally:
        with _active_lock:
            if _active.get(task_id) is threading.current_thread():
                _active.pop(task_id, None)


def _publish_fence(task_id: str, attempt_id: str, token: str) -> None:
    if (not metadb.heartbeat_durable_task(task_id, attempt_id, token)
            or metadb.durable_task_attempt_should_stop(task_id, attempt_id, token)):
        raise RuntimeError("cancelled")


def _worker_diagnostic(exc: BaseException) -> TemporalResampleDiagnosticCode:
    """Keep retained task errors useful but independent of provider/path exception text."""
    if isinstance(exc, TemporalResampleAdmissionError):
        if exc.code == TemporalResampleDiagnosticCode.REVISION_UNAVAILABLE:
            return TemporalResampleDiagnosticCode.EXACT_REVISION_LOST
        return exc.code
    if isinstance(exc, _TemporalCandidateChanged):
        return TemporalResampleDiagnosticCode.EXACT_REVISION_LOST
    if isinstance(exc, metadb.ManagedLocalWriteConflict):
        reason = str(exc)
        if "stale" in reason or "parent is not current" in reason:
            return TemporalResampleDiagnosticCode.STALE_PARENT
        return TemporalResampleDiagnosticCode.PUBLICATION_KEY_CONFLICT
    return TemporalResampleDiagnosticCode.PUBLICATION_FAILED


def dispatch(task_id: str, deps) -> None:
    with _active_lock:
        active = _active.get(str(task_id))
        if active is not None and active.is_alive():
            return
        thread = threading.Thread(target=_worker, args=(str(task_id), deps), daemon=True,
                                  name=f"dp-temporal-resample-{str(task_id)[-12:]}")
        _active[str(task_id)] = thread
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_temporal_resample_task_ids():
        dispatch(task_id, deps)


def public_task(task: dict) -> TemporalResampleTaskResponseV1:
    result = task.get("temporal_resample_result") or {}
    receipt = result.get("receipt")
    public_receipt = None
    if receipt is not None:
        # Receipt persistence retains the provider publication for replay and
        # catalog consistency.  The public temporal API intentionally exposes
        # only immutable output facts, never locator or idempotency material.
        public_receipt = TemporalResampleOutputReceiptV1.model_validate({
            key: receipt[key] for key in ("datasetId", "revisionId", "parentHead", "head", "rows", "bytes", "schema", "durable")
            if key in receipt
        })
    attempts_remain = task["retry_count"] + 1 < task["max_attempts"]
    can_retry = attempts_remain and (
        task["status"] == "cancelled"
        or (task["status"] == "failed" and temporal_failure_retryable(task.get("error")))
    )
    return TemporalResampleTaskResponseV1(
        taskId=task["id"], status=task["status"], cancelRequested=task["cancel_requested"],
        canRetry=can_retry,
        diagnosticCode=(TemporalResampleDiagnosticCode(task["error"])
                        if task["status"] == "failed" and task.get("error") is not None else None),
        receipt=public_receipt, evidence=result.get("evidence"),
        childDatasetId=(result.get("child") or {}).get("datasetId"),
        childRevisionId=(result.get("child") or {}).get("revisionId"),
    )
