"""Hidden restart-safe lifecycle for one immutable DatasetView distribution report."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import exists, or_, select, update

from hub import metadb
from hub.models import DatasetViewDefinitionV1, to_camel


_REPORT_DOC_MAX_BYTES = 256 * 1024
_REPORT_CONTENT_MAX_DEPTH = 16
_REPORT_CONTENT_MAX_ITEMS = 10_000
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DistributionReportSubmissionConflict(RuntimeError):
    pass


class DistributionReportUnavailable(RuntimeError):
    pass


class DistributionReportIntentV1(BaseModel):
    """One strict owner-scoped request against an exact immutable DatasetView snapshot."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    submission_id: str = Field(min_length=1, max_length=128)
    dataset_view_id: str = Field(min_length=1, max_length=32)
    view_definition_sha256: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    computation_version: str = Field(min_length=1, max_length=64)
    max_attempts: int = Field(default=3, ge=1, le=3)

    @field_validator("submission_id", "dataset_view_id", "computation_version")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("distribution report identities must be bounded canonical tokens")
        return value


def _bounded_json(value: Any, *, depth: int = 0) -> int:
    if depth > _REPORT_CONTENT_MAX_DEPTH:
        raise ValueError("distribution report content is nested too deeply")
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("distribution report content requires finite numbers")
        return 1
    if isinstance(value, list):
        count = 1
        for item in value:
            count += _bounded_json(item, depth=depth + 1)
            if count > _REPORT_CONTENT_MAX_ITEMS:
                raise ValueError("distribution report content has too many values")
        return count
    if isinstance(value, dict):
        count = 1
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256 or "\x00" in key:
                raise ValueError("distribution report content keys must be bounded strings")
            count += _bounded_json(item, depth=depth + 1)
            if count > _REPORT_CONTENT_MAX_ITEMS:
                raise ValueError("distribution report content has too many values")
        return count
    raise ValueError("distribution report content must be JSON data")


class DistributionReportDocumentV1(BaseModel):
    """Bounded algorithm-owned content sealed to one report admission."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    report_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    task_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    dataset_view_id: str = Field(min_length=1, max_length=32)
    view_definition_sha256: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    computation_version: str = Field(min_length=1, max_length=64)
    content: dict[str, Any]

    @model_validator(mode="after")
    def validate_content(self) -> "DistributionReportDocumentV1":
        _bounded_json(self.content)
        return self


def _canonical(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(by_alias=True, mode="json"),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def _view_definition_digest(view: DatasetViewDefinitionV1) -> str:
    payload = view.model_dump(by_alias=True, mode="json")
    payload.pop("definitionSha256")
    if payload.get("temporalWindow") is None:
        payload.pop("temporalWindow", None)
    return _sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _task_id(owner_id: str, submission_id: str) -> str:
    return _sha256(f"distribution-report-task-v1\x00{owner_id}\x00{submission_id}")


def _report_id(task_id: str) -> str:
    return _sha256(f"distribution-report-envelope-v1\x00{task_id}")[:32]


def _locked_attempts(s, task_id: str) -> list[metadb.DurableTaskAttempt]:
    return list(s.scalars(select(metadb.DurableTaskAttempt).where(
        metadb.DurableTaskAttempt.task_id == task_id,
    ).order_by(metadb.DurableTaskAttempt.attempt_number).with_for_update()))


def _latest_attempt(
        s, task_id: str,
        attempts: list[metadb.DurableTaskAttempt] | None = None,
) -> metadb.DurableTaskAttempt | None:
    locked = attempts if attempts is not None else _locked_attempts(s, task_id)
    return locked[-1] if locked else None


def _locked_report_rows(
        s, task_id: str,
) -> tuple[
    metadb.DurableTask | None,
    list[metadb.DurableTaskAttempt],
    metadb.DistributionReportEnvelope | None,
]:
    """Read one report version in the lock order shared by every terminal writer."""
    task = metadb._lock_durable_task_for_write(s, str(task_id))
    if task is None:
        return None, [], None
    attempts = _locked_attempts(s, task.id)
    row = s.get(
        metadb.DistributionReportEnvelope, task.id,
        with_for_update=True, populate_existing=True)
    return task, attempts, row


def _validate_revision_hold(
        s, row: metadb.DistributionReportEnvelope, view: DatasetViewDefinitionV1,
) -> None:
    """Prove the report still owns the frozen view's one exact core revision."""
    revision = s.get(
        metadb.ManagedLocalFileRevision, view.dataset_ref.revision_id,
        with_for_update=True, populate_existing=True)
    artifact = (s.get(
        metadb.LocalResultArtifact, revision.artifact_uri,
        with_for_update=True, populate_existing=True)
        if revision is not None else None)
    references = list(s.scalars(select(metadb.LocalResultReference).where(
        metadb.LocalResultReference.owner_kind == "distribution_report",
        metadb.LocalResultReference.owner_key == row.report_id,
    ).order_by(metadb.LocalResultReference.uri).with_for_update()))
    if (
        revision is None
        or revision.logical_id != view.dataset_ref.dataset_id
        or artifact is None
        or artifact.state != "ready"
        or len(references) != 1
        or references[0].uri != revision.artifact_uri
    ):
        raise DistributionReportUnavailable(
            "distribution report exact revision hold is unavailable")


def _envelope_doc(
        s, task: metadb.DurableTask, row: metadb.DistributionReportEnvelope,
        *, attempts: list[metadb.DurableTaskAttempt] | None = None,
) -> dict:
    try:
        intent = DistributionReportIntentV1.model_validate_json(row.intent_doc)
        view = DatasetViewDefinitionV1.model_validate_json(row.view_snapshot_doc)
        report = (DistributionReportDocumentV1.model_validate_json(row.report_doc)
                  if row.report_doc is not None else None)
    except ValueError as exc:
        raise DistributionReportUnavailable("distribution report envelope is corrupt") from exc
    intent_payload = _canonical(intent)
    if (
        task.task_kind != "distribution_report"
        or task.canvas_id is not None
        or task.target_node_id is not None
        or task.dataset_view_id != row.dataset_view_id
        or task.execution_manifest_sha256 is not None
        or any(value is not None for value in (
            task.graph_doc, task.input_manifest, task.write_intent))
        or task.id != _task_id(task.owner_id, intent.submission_id)
        or task.submission_id != intent.submission_id
        or row.report_id != _report_id(task.id)
        or task.intent_sha256 != row.intent_sha256
        or row.intent_sha256 != _sha256(intent_payload)
        or intent.dataset_view_id != row.dataset_view_id
        or intent.view_definition_sha256 != row.view_definition_sha256
        or intent.computation_version != row.computation_version
        or intent.max_attempts != task.max_attempts
        or view.id != row.dataset_view_id
        or view.creator_id != task.owner_id
        or view.definition_sha256 != row.view_definition_sha256
        or _view_definition_digest(view) != row.view_definition_sha256
        or view.retention_owner != row.revision_retention_owner
        or row.revision_retention_owner != "core"
    ):
        raise DistributionReportUnavailable("distribution report admission is corrupt")
    if report is not None and (
        task.status != "done"
        or report.report_id != row.report_id
        or report.task_id != task.id
        or report.dataset_view_id != row.dataset_view_id
        or report.view_definition_sha256 != row.view_definition_sha256
        or report.computation_version != row.computation_version
    ):
        raise DistributionReportUnavailable("distribution report terminal document is corrupt")
    if (task.status == "done") != (report is not None):
        raise DistributionReportUnavailable("distribution report and Task terminal truth disagree")
    terminal = task.status in metadb._TERMINAL_RUN
    if terminal != (task.completed_at is not None) or terminal != (row.completed_at is not None):
        raise DistributionReportUnavailable("distribution report terminal facts disagree")
    _validate_revision_hold(s, row, view)
    task_doc = metadb._durable_task_doc(
        s, task, include_admission=False, attempts=attempts)
    if not task_doc["attempts"] or (
            task.status == "done" and task_doc["attempts"][-1]["status"] != "done"):
        raise DistributionReportUnavailable("distribution report Attempt truth disagrees")
    for key in ("created_at", "updated_at", "completed_at"):
        task_doc[key] = metadb._inbox_stamp(task_doc[key])
    for attempt in task_doc["attempts"]:
        for key in ("lease_until", "heartbeat_at", "started_at", "completed_at"):
            attempt[key] = metadb._inbox_stamp(attempt.get(key))
    return {
        "schema_version": 1,
        "report_id": row.report_id,
        "task": task_doc,
        "intent": intent.model_dump(by_alias=True, mode="json"),
        "view_snapshot": view.model_dump(by_alias=True, mode="json"),
        "revision_retention_owner": row.revision_retention_owner,
        "report": report.model_dump(by_alias=True, mode="json") if report else None,
        "created_at": metadb._inbox_stamp(row.created_at),
        "updated_at": metadb._inbox_stamp(row.updated_at),
        "completed_at": metadb._inbox_stamp(row.completed_at),
    }


def admit_distribution_report(
    *, owner_id: str, intent: DistributionReportIntentV1 | dict,
) -> tuple[dict, bool]:
    """Atomically create the envelope, Task, first Attempt, and exact revision hold."""
    owner_id = str(owner_id)
    parsed = DistributionReportIntentV1.model_validate(intent)
    intent_doc = _canonical(parsed)
    intent_sha = _sha256(intent_doc)
    task_id, report_id = _task_id(owner_id, parsed.submission_id), None
    with metadb.session() as s:
        if s.get_bind().dialect.name == "sqlite":
            owner = s.execute(update(metadb.User).where(
                metadb.User.id == owner_id).values(name=metadb.User.name))
            if owner.rowcount != 1:
                raise DistributionReportUnavailable("report owner is unavailable")
        else:
            if s.get(metadb.User, owner_id, with_for_update=True) is None:
                raise DistributionReportUnavailable("report owner is unavailable")
        task = s.get(metadb.DurableTask, task_id, with_for_update=True)
        attempts = _locked_attempts(s, task_id) if task is not None else []
        row = s.get(metadb.DistributionReportEnvelope, task_id, with_for_update=True)
        if task is not None or row is not None:
            if task is None or row is None:
                raise DistributionReportSubmissionConflict(
                    "distribution report admission is incomplete")
            if task.owner_id != owner_id or row.intent_sha256 != intent_sha:
                raise DistributionReportSubmissionConflict(
                    "distribution report submission changed its immutable intent")
            return _envelope_doc(s, task, row, attempts=attempts), False
        view_row = s.get(metadb.DatasetView, parsed.dataset_view_id, with_for_update=True)
        if view_row is None:
            raise DistributionReportUnavailable("DatasetView is unavailable")
        if view_row.owner_id != owner_id or view_row.deleted_at is not None:
            raise DistributionReportUnavailable("DatasetView is unavailable")
        try:
            view = DatasetViewDefinitionV1.model_validate_json(view_row.definition_doc)
        except ValueError as exc:
            raise DistributionReportUnavailable("DatasetView snapshot is corrupt") from exc
        if (
            view.id != view_row.id
            or view.creator_id != owner_id
            or view.definition_sha256 != view_row.definition_sha256
            or view.definition_sha256 != parsed.view_definition_sha256
            or _view_definition_digest(view) != view.definition_sha256
        ):
            raise DistributionReportUnavailable("DatasetView snapshot identity is corrupt")
        if view.retention_owner != "core":
            raise DistributionReportUnavailable(
                "DatasetView does not expose a core-owned exact revision hold")
        now = metadb._durable_task_db_now(s)
        report_id = _report_id(task_id)
        task = metadb.DurableTask(
            id=task_id, owner_id=owner_id, canvas_id=None,
            dataset_view_id=view.id, submission_id=parsed.submission_id,
            intent_sha256=intent_sha, target_node_id=None,
            task_kind="distribution_report", execution_manifest_sha256=None,
            backend_kind="local", graph_doc=None, input_manifest=None, write_intent=None,
            status="queued", status_doc=json.dumps(
                metadb._task_status_doc(task_id, None), default=str),
            max_attempts=parsed.max_attempts, created_at=now, updated_at=now)
        attempt = metadb.DurableTaskAttempt(
            id=uuid.uuid4().hex, task_id=task_id, attempt_number=1,
            execution_manifest_sha256=None, status="queued", created_at=now)
        row = metadb.DistributionReportEnvelope(
            task_id=task_id, report_id=report_id, dataset_view_id=view.id,
            intent_sha256=intent_sha, intent_doc=intent_doc,
            view_definition_sha256=view.definition_sha256,
            view_snapshot_doc=_canonical(view),
            computation_version=parsed.computation_version,
            revision_retention_owner="core", created_at=now, updated_at=now)
        # These ORM rows deliberately have no relationships: their contract is expressed by
        # explicit foreign keys and immutable identities.  Flush the parent first so PostgreSQL
        # never schedules the envelope INSERT ahead of its DurableTask parent.
        s.add(task)
        s.flush()
        s.add_all((attempt, row))
        s.flush()
        metadb.sync_local_result_owner(
            s, "distribution_report", report_id, row.view_snapshot_doc)
        return _envelope_doc(s, task, row, attempts=[attempt]), True


def distribution_report(*, owner_id: str, task_id: str) -> dict | None:
    with metadb.session() as s:
        task, attempts, row = _locked_report_rows(s, str(task_id))
        if task is None and row is None:
            return None
        if task is None or row is None or task.owner_id != str(owner_id):
            return None
        try:
            return _envelope_doc(s, task, row, attempts=attempts)
        except DistributionReportUnavailable:
            if task.status not in metadb._TERMINAL_RUN:
                attempt = _latest_attempt(s, task.id, attempts)
                if attempt is None:
                    raise DistributionReportUnavailable(
                        "distribution report has no Attempt")
                _terminal_failure(
                    s, task, attempt, row,
                    code="distribution_report_snapshot_invalid")
                return None
            raise


def _terminal_failure(
    s, task: metadb.DurableTask, attempt: metadb.DurableTaskAttempt,
    row: metadb.DistributionReportEnvelope, *, code: str,
) -> None:
    now = metadb._durable_task_db_now(s)
    error = code.replace("_", " ")
    attempt.status = "failed"
    attempt.error = error
    attempt.completed_at = now
    attempt.lease_until = now
    task.status = "failed"
    task.error = error
    task.completed_at = task.updated_at = now
    status = metadb._task_status_doc(task.id, None, "failed")
    status["error"] = error
    task.status_doc = json.dumps(status, default=str)
    row.report_doc = None
    row.updated_at = row.completed_at = now
    metadb._emit_durable_task_inbox_item(
        s, task=task, attempt=attempt, task_status="failed",
        diagnostic_code=code, now=now)


def claim_distribution_report(task_id: str, owner_token: str) -> dict | None:
    """Claim queued work or DB-time fence one expired owner under the #309 attempt bound."""
    if not _TOKEN_RE.fullmatch(str(owner_token)):
        raise ValueError("distribution report owner token is invalid")
    with metadb.session() as s:
        task, attempts, row = _locked_report_rows(s, str(task_id))
        if task is None or row is None or task.task_kind != "distribution_report":
            return None
        attempt = _latest_attempt(s, task.id, attempts)
        if attempt is None:
            raise DistributionReportUnavailable("distribution report has no Attempt")
        try:
            _envelope_doc(s, task, row, attempts=attempts)
        except DistributionReportUnavailable:
            if task.status not in metadb._TERMINAL_RUN:
                _terminal_failure(
                    s, task, attempt, row,
                    code="distribution_report_snapshot_invalid")
            return None
    if metadb._claim_durable_task_kind(
            str(task_id), str(owner_token), "distribution_report") is None:
        return None
    with metadb.session() as s:
        task, attempts, row = _locked_report_rows(s, str(task_id))
        if task is None or row is None:  # pragma: no cover - claim holds both identities
            raise DistributionReportUnavailable("distribution report disappeared after claim")
        try:
            return _envelope_doc(s, task, row, attempts=attempts)
        except DistributionReportUnavailable:
            if task.status not in metadb._TERMINAL_RUN:
                attempt = _latest_attempt(s, task.id, attempts)
                if attempt is None:  # pragma: no cover - claim guarantees one Attempt
                    raise DistributionReportUnavailable(
                        "distribution report has no Attempt")
                _terminal_failure(
                    s, task, attempt, row,
                    code="distribution_report_snapshot_invalid")
            return None


def heartbeat_distribution_report(task_id: str, attempt_id: str, owner_token: str) -> bool:
    with metadb.session() as s:
        task = s.get(metadb.DurableTask, str(task_id))
        if task is None or task.task_kind != "distribution_report":
            return False
    return metadb.heartbeat_durable_task(task_id, attempt_id, owner_token)


def distribution_report_should_stop(task_id: str, attempt_id: str, owner_token: str) -> bool:
    return metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token)


def _finish(
    *, task_id: str, attempt_id: str, owner_token: str,
    report: DistributionReportDocumentV1 | dict | None, failure_code: str | None,
) -> dict | None:
    parsed = DistributionReportDocumentV1.model_validate(report) if report is not None else None
    report_doc = _canonical(parsed) if parsed is not None else None
    if report_doc is not None and len(report_doc.encode()) > _REPORT_DOC_MAX_BYTES:
        raise ValueError("distribution report document exceeds the persisted size limit")
    with metadb.session() as s:
        task, attempts, row = _locked_report_rows(s, str(task_id))
        attempt = next((item for item in attempts if item.id == str(attempt_id)), None)
        if task is None or row is None or attempt is None or task.task_kind != "distribution_report":
            return None
        if task.status in metadb._TERMINAL_RUN:
            if (
                parsed is not None
                and attempt.task_id == task.id
                and attempt.owner_token == str(owner_token)
                and task.status == attempt.status == "done"
                and row.report_doc == report_doc
            ):
                return _envelope_doc(s, task, row, attempts=attempts)
            error = failure_code.replace("_", " ") if failure_code is not None else None
            if (
                parsed is None
                and failure_code is not None
                and attempt.task_id == task.id
                and attempt.owner_token == str(owner_token)
                and task.status == attempt.status == "failed"
                and task.error == attempt.error == error
                and row.report_doc is None
            ):
                return _envelope_doc(s, task, row, attempts=attempts)
            return None
        now = metadb._durable_task_db_now(s)
        lease = attempt.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=datetime.timezone.utc)
        if (
            attempt.task_id != task.id
            or attempt.owner_token != str(owner_token)
            or attempt.status != "running"
            or lease is None
            or lease <= now
        ):
            return None
        _envelope_doc(s, task, row, attempts=attempts)
        if failure_code is not None:
            _terminal_failure(s, task, attempt, row, code=failure_code)
            return _envelope_doc(s, task, row, attempts=attempts)
        assert parsed is not None and report_doc is not None
        if (
            parsed.report_id != row.report_id
            or parsed.task_id != task.id
            or parsed.dataset_view_id != row.dataset_view_id
            or parsed.view_definition_sha256 != row.view_definition_sha256
            or parsed.computation_version != row.computation_version
        ):
            raise ValueError("distribution report document changed its frozen admission")
        attempt.status = task.status = "done"
        attempt.error = task.error = None
        attempt.completed_at = now
        attempt.lease_until = now
        task.completed_at = task.updated_at = now
        task.status_doc = json.dumps(
            metadb._task_status_doc(task.id, None, "done"), default=str)
        row.report_doc = report_doc
        row.updated_at = row.completed_at = now
        metadb._emit_durable_task_inbox_item(
            s, task=task, attempt=attempt, task_status="done", now=now)
        return _envelope_doc(s, task, row, attempts=attempts)


def complete_distribution_report(
    *, task_id: str, attempt_id: str, owner_token: str,
    report: DistributionReportDocumentV1 | dict,
) -> dict | None:
    return _finish(
        task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
        report=report, failure_code=None)


def fail_distribution_report(
    *, task_id: str, attempt_id: str, owner_token: str,
) -> dict | None:
    return _finish(
        task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
        report=None, failure_code="distribution_report_computation_failed")


def request_distribution_report_cancel(*, owner_id: str, task_id: str) -> dict | None:
    with metadb.session() as s:
        task, attempts, row = _locked_report_rows(s, str(task_id))
        if task is None or row is None or task.owner_id != str(owner_id):
            return None
        if task.status not in metadb._TERMINAL_RUN:
            now = metadb._durable_task_db_now(s)
            task.cancel_requested = True
            task.updated_at = row.updated_at = now
            attempt = _latest_attempt(s, task.id, attempts)
            if attempt is not None and attempt.cancel_requested_at is None:
                attempt.cancel_requested_at = now
        return _envelope_doc(s, task, row, attempts=attempts)


def retry_distribution_report(
    *, owner_id: str, task_id: str, retry_request_id: str,
) -> dict:
    if not _TOKEN_RE.fullmatch(str(retry_request_id)):
        raise ValueError("distribution report retry request id is invalid")
    with metadb.session() as s:
        task = s.get(metadb.DurableTask, str(task_id))
        if (
            task is None
            or task.owner_id != str(owner_id)
            or task.task_kind != "distribution_report"
        ):
            raise KeyError(task_id)
    metadb.retry_durable_task(str(task_id), str(retry_request_id))
    reopened = distribution_report(owner_id=str(owner_id), task_id=str(task_id))
    if reopened is None:  # pragma: no cover - the Task cannot disappear between committed calls
        raise DistributionReportUnavailable("distribution report disappeared after retry")
    return reopened


def due_distribution_report_task_ids(limit: int = 100) -> list[str]:
    limit = max(1, min(int(limit), 100))
    with metadb.session() as s:
        now = metadb._durable_task_db_now(s)
        live_attempt = exists(select(metadb.DurableTaskAttempt.id).where(
            metadb.DurableTaskAttempt.task_id == metadb.DurableTask.id,
            metadb.DurableTaskAttempt.status == "running",
            metadb.DurableTaskAttempt.lease_until > now,
        ))
        rows = s.scalars(select(metadb.DurableTask.id).join(
            metadb.DistributionReportEnvelope,
            metadb.DistributionReportEnvelope.task_id == metadb.DurableTask.id,
        ).where(
            metadb.DurableTask.task_kind == "distribution_report",
            metadb.DurableTask.status.in_(("queued", "running")),
            or_(metadb.DurableTask.cancel_requested, ~live_attempt),
        ).order_by(
            metadb.DurableTask.created_at, metadb.DurableTask.id,
        ).limit(limit))
        return [str(task_id) for task_id in rows]
