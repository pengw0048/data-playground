"""Fenced bounded fan-out plan, units/attempts, slots, and held LocalResult evidence."""

import contextlib, datetime, hashlib, json, os, re, uuid
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column
from hub.metadb import (
    Base, DurableCheckpoint, DurableTask, DurableTaskAttempt, LocalResultArtifact,
    LocalResultReference, _CHECKPOINT_PARENT_KINDS, _durable_task_db_now,
    _linear_checkpoint_committed_doc, _lock_durable_task_for_write, _lock_local_result_registry,
    _now, session,
)
CHILD_OWNER = "durable_fanout_child"
GATHER_OWNER = "durable_fanout_gather"
SLOT_SCOPE = "bounded_fanout_v1"
OPERATION_ID = "identity_projection_v1"
REQUESTED_PARTITIONS = 4
_LEASE_S = 15
class BoundedFanoutPlan(Base):
    __tablename__ = "bounded_fanout_plans"
    parent_task_id: Mapped[str] = mapped_column(String, ForeignKey("durable_tasks.id"), primary_key=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_schema_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_partitions: Mapped[int] = mapped_column(Integer, nullable=False)
    partition_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ranges_json: Mapped[str] = mapped_column(Text, nullable=False)
    creating_attempt_id: Mapped[str] = mapped_column(String, ForeignKey("durable_task_attempts.id"), nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        CheckConstraint("operation_id = 'identity_projection_v1'", name="ck_bounded_fanout_plan_operation"),
        CheckConstraint("requested_partitions = 4", name="ck_bounded_fanout_plan_requested_partitions"),
        CheckConstraint("partition_count >= 1 AND partition_count <= 4", name="ck_bounded_fanout_plan_partition_count"),
        CheckConstraint("checkpoint_rows >= 0", name="ck_bounded_fanout_plan_rows"),
    )
class BoundedFanoutUnit(Base):
    __tablename__ = "bounded_fanout_units"
    unit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_task_id: Mapped[str] = mapped_column(String, ForeignKey("bounded_fanout_plans.parent_task_id"), nullable=False, index=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    partition_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    range_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    range_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    result_uri: Mapped[str | None] = mapped_column(Text, ForeignKey("local_result_artifacts.uri"), nullable=True)
    result_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_schema_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_dev: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_ino: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("parent_task_id", "partition_index", name="uq_bounded_fanout_unit_partition"),
        CheckConstraint("kind IN ('child','gather')", name="ck_bounded_fanout_unit_kind"),
    )
class BoundedFanoutUnitAttempt(Base):
    __tablename__ = "bounded_fanout_unit_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(64), ForeignKey("bounded_fanout_units.unit_id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_attempt_id: Mapped[str] = mapped_column(String, ForeignKey("durable_task_attempts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slot_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("unit_id", "attempt_number", name="uq_bounded_fanout_unit_attempt_number"),)
class BoundedFanoutSlot(Base):
    __tablename__ = "bounded_fanout_slots"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    slot_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder_attempt_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("bounded_fanout_unit_attempts.id"), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("holder_attempt_id", name="uq_bounded_fanout_slot_holder"),
        CheckConstraint("scope = 'bounded_fanout_v1'", name="ck_bounded_fanout_slot_scope"),
        CheckConstraint("slot_number >= 0 AND slot_number <= 3", name="ck_bounded_fanout_slot_number"),
    )
def partition_ranges(row_count: int) -> list[tuple[int, int]]:
    if row_count < 0:
        raise ValueError("checkpoint rows must be non-negative")
    if row_count == 0:
        return [(0, 0)]
    p = max(1, min(4, row_count))
    base, rem = divmod(row_count, p)
    out, start = [], 0
    for i in range(p):
        size = base + (1 if i < rem else 0)
        out.append((start, start + size))
        start += size
    return out
def _sha(value: str) -> str:
    value = str(value).lower()
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("fan-out digest is not SHA-256 hex")
    return value
def _plan_digest(*, evidence_digest, rows, schema_digest, operation_id, ranges) -> str:
    payload = json.dumps({
        "v": 1, "checkpoint_evidence_digest": evidence_digest, "rows": rows,
        "schema_digest": schema_digest, "operation_id": operation_id,
        "ranges": [[a, b] for a, b in ranges],
    }, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
def _child_id(parent, digest, part) -> str:
    return hashlib.sha256(f"fanout_child_v1\0{parent}\0{digest}\0{part}".encode()).hexdigest()[:32]
def _gather_id(parent, digest) -> str:
    return hashlib.sha256(f"fanout_gather_v1\0{parent}\0{digest}".encode()).hexdigest()[:32]
def _owner(unit: BoundedFanoutUnit) -> tuple[str, str]:
    if unit.kind == "gather":
        return GATHER_OWNER, f"{unit.parent_task_id}:{unit.plan_digest}"
    return CHILD_OWNER, f"{unit.parent_task_id}:{unit.plan_digest}:{int(unit.partition_index)}"
def _tz(value):
    if value is None:
        return None
    return (value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None
            else value.astimezone(datetime.timezone.utc))
def _fence_parent(s, parent_task_id, parent_attempt_id, owner_token):
    task = _lock_durable_task_for_write(s, parent_task_id)
    attempt = s.get(DurableTaskAttempt, parent_attempt_id, with_for_update=True)
    now = _durable_task_db_now(s)
    lease = _tz(attempt.lease_until if attempt is not None else None)
    if (task is None or attempt is None or task.task_kind not in _CHECKPOINT_PARENT_KINDS
            or attempt.task_id != task.id or task.status != "running" or task.cancel_requested
            or attempt.status != "running" or attempt.owner_token != str(owner_token)
            or lease is None or lease <= now):
        raise RuntimeError("parent TaskAttempt fence is stale or missing")
    return task, attempt, now
def _plan_doc(s, plan: BoundedFanoutPlan) -> dict:
    units = list(s.scalars(select(BoundedFanoutUnit).where(
        BoundedFanoutUnit.parent_task_id == plan.parent_task_id).order_by(
            BoundedFanoutUnit.kind.desc(), BoundedFanoutUnit.partition_index)))
    return {
        "parent_task_id": plan.parent_task_id, "plan_digest": plan.plan_digest,
        "checkpoint_id": plan.checkpoint_id,
        "checkpoint_evidence_digest": plan.checkpoint_evidence_digest,
        "checkpoint_rows": int(plan.checkpoint_rows),
        "checkpoint_schema_sha256": plan.checkpoint_schema_sha256,
        "operation_id": plan.operation_id, "requested_partitions": int(plan.requested_partitions),
        "partition_count": int(plan.partition_count), "ranges": json.loads(plan.ranges_json),
        "creating_attempt_id": plan.creating_attempt_id, "paused": bool(plan.paused),
        "units": [{
            "unit_id": u.unit_id, "kind": u.kind, "partition_index": u.partition_index,
            "range_start": int(u.range_start), "range_end": int(u.range_end), "status": u.status,
            "result_uri": u.result_uri,
            "result_rows": None if u.result_rows is None else int(u.result_rows),
            "result_bytes": None if u.result_bytes is None else int(u.result_bytes),
            "result_content_sha256": u.result_content_sha256,
            "result_schema_sha256": u.result_schema_sha256, "active_attempt_id": u.active_attempt_id,
        } for u in units],
    }
def _attempt_doc(a: BoundedFanoutUnitAttempt) -> dict:
    return {
        "attempt_id": a.id, "unit_id": a.unit_id, "attempt_number": int(a.attempt_number),
        "parent_attempt_id": a.parent_attempt_id, "status": a.status,
        "owner_token": a.owner_token, "claim_token": a.claim_token,
        "slot_number": a.slot_number, "lease_until": _tz(a.lease_until),
    }
def _slots(s) -> list[BoundedFanoutSlot]:
    rows = list(s.scalars(select(BoundedFanoutSlot).where(
        BoundedFanoutSlot.scope == SLOT_SCOPE).order_by(
            BoundedFanoutSlot.slot_number).with_for_update()))
    if len(rows) != 4 or [r.slot_number for r in rows] != [0, 1, 2, 3]:
        raise RuntimeError("bounded fan-out slot rows are corrupt")
    return rows
def _release_slot(s, attempt: BoundedFanoutUnitAttempt, now) -> None:
    if attempt.slot_number is None:
        return
    slot = s.get(BoundedFanoutSlot, (SLOT_SCOPE, attempt.slot_number), with_for_update=True)
    if (slot is not None and slot.holder_attempt_id == attempt.id
            and slot.claim_token == attempt.claim_token):
        slot.holder_attempt_id = slot.claim_token = slot.lease_until = None
    attempt.slot_number = attempt.claim_token = attempt.lease_until = None
    attempt.heartbeat_at = now
def create_or_reopen_plan(
        *, parent_task_id: str, parent_attempt_id: str, owner_token: str,
        operation_id: str = OPERATION_ID,
        requested_partitions: int = REQUESTED_PARTITIONS) -> dict:
    parent_task_id, parent_attempt_id = str(parent_task_id), str(parent_attempt_id)
    owner_token = str(owner_token)
    if operation_id != OPERATION_ID or int(requested_partitions) != REQUESTED_PARTITIONS:
        raise ValueError("fan-out plan rejects unsupported operation/partition contract")
    with session() as s:
        task, attempt, now = _fence_parent(s, parent_task_id, parent_attempt_id, owner_token)
        checkpoint = s.get(DurableCheckpoint, parent_task_id, with_for_update=True)
        if checkpoint is None or checkpoint.phase != "committed":
            raise RuntimeError("fan-out plan requires a committed checkpoint")
        _lock_local_result_registry(s)
        committed = _linear_checkpoint_committed_doc(s, task, checkpoint)
        evidence, schema = _sha(committed["content_sha256"]), _sha(committed["schema_sha256"])
        rows = int(committed["rows"])
        ranges = partition_ranges(rows)
        digest = _plan_digest(
            evidence_digest=evidence, rows=rows, schema_digest=schema,
            operation_id=operation_id, ranges=ranges)
        existing = s.get(BoundedFanoutPlan, parent_task_id, with_for_update=True)
        if existing is not None:
            if (existing.plan_digest != digest
                    or existing.checkpoint_id != committed["checkpoint_id"]
                    or existing.checkpoint_evidence_digest != evidence
                    or int(existing.checkpoint_rows) != rows
                    or existing.checkpoint_schema_sha256 != schema
                    or existing.operation_id != operation_id
                    or int(existing.requested_partitions) != requested_partitions):
                raise RuntimeError("fan-out plan replay conflicts with immutable plan")
            return _plan_doc(s, existing)
        plan = BoundedFanoutPlan(
            parent_task_id=parent_task_id, plan_digest=digest,
            checkpoint_id=committed["checkpoint_id"], checkpoint_evidence_digest=evidence,
            checkpoint_rows=rows, checkpoint_schema_sha256=schema, operation_id=operation_id,
            requested_partitions=requested_partitions, partition_count=len(ranges),
            ranges_json=json.dumps([[a, b] for a, b in ranges], separators=(",", ":")),
            creating_attempt_id=attempt.id, paused=False, created_at=now, updated_at=now)
        s.add(plan)
        for i, (a, b) in enumerate(ranges):
            s.add(BoundedFanoutUnit(
                unit_id=_child_id(parent_task_id, digest, i), parent_task_id=parent_task_id,
                plan_digest=digest, kind="child", partition_index=i, range_start=a, range_end=b,
                status="pending", created_at=now, updated_at=now))
        s.add(BoundedFanoutUnit(
            unit_id=_gather_id(parent_task_id, digest), parent_task_id=parent_task_id,
            plan_digest=digest, kind="gather", partition_index=None, range_start=0, range_end=rows,
            status="pending", created_at=now, updated_at=now))
        s.flush()
        return _plan_doc(s, plan)
def pause_plan(*, parent_task_id, parent_attempt_id, owner_token) -> dict:
    return _set_paused(parent_task_id, parent_attempt_id, owner_token, True)
def resume_plan(*, parent_task_id, parent_attempt_id, owner_token) -> dict:
    return _set_paused(parent_task_id, parent_attempt_id, owner_token, False)
def _set_paused(parent_task_id, parent_attempt_id, owner_token, paused: bool) -> dict:
    with session() as s:
        _fence_parent(s, str(parent_task_id), str(parent_attempt_id), str(owner_token))
        plan = s.get(BoundedFanoutPlan, str(parent_task_id), with_for_update=True)
        if plan is None:
            raise RuntimeError("fan-out plan does not exist")
        plan.paused = bool(paused)
        plan.updated_at = _durable_task_db_now(s)
        s.flush()
        return _plan_doc(s, plan)
def claim_unit(*, parent_task_id, unit_id, parent_attempt_id, owner_token) -> dict:
    parent_task_id, unit_id = str(parent_task_id), str(unit_id)
    parent_attempt_id, owner_token = str(parent_attempt_id), str(owner_token)
    with session() as s:
        task, parent_attempt, now = _fence_parent(
            s, parent_task_id, parent_attempt_id, owner_token)
        plan = s.get(BoundedFanoutPlan, parent_task_id, with_for_update=True)
        unit = s.get(BoundedFanoutUnit, unit_id, with_for_update=True)
        if plan is None or unit is None or unit.parent_task_id != parent_task_id:
            raise RuntimeError("fan-out unit is not part of the parent plan")
        if plan.paused or task.cancel_requested:
            raise RuntimeError("fan-out claims are paused or cancelled")
        if unit.status == "done":
            raise RuntimeError("fan-out unit already has valid evidence")
        if unit.kind == "gather":
            children = list(s.scalars(select(BoundedFanoutUnit).where(
                BoundedFanoutUnit.parent_task_id == parent_task_id,
                BoundedFanoutUnit.kind == "child").with_for_update()))
            if not children or any(c.status != "done" for c in children):
                raise RuntimeError("gather is unclaimable before all children validate")
        if unit.active_attempt_id:
            active = s.get(BoundedFanoutUnitAttempt, unit.active_attempt_id, with_for_update=True)
            if active is not None and active.status == "running":
                lease = _tz(active.lease_until)
                if (active.owner_token == owner_token and active.claim_token
                        and lease is not None and lease > now):
                    return _attempt_doc(active)
                if lease is not None and lease > now:
                    raise RuntimeError("fan-out unit is held by another unexpired claim")
                active.status, active.completed_at = "fenced", now
                _release_slot(s, active, now)
                unit.active_attempt_id = None
        free = next((
            slot for slot in _slots(s)
            if slot.holder_attempt_id is None
            or (lease := _tz(slot.lease_until)) is None or lease <= now
        ), None)
        if free is None:
            raise RuntimeError("no free bounded fan-out slot")
        if free.holder_attempt_id is not None:
            stale = s.get(BoundedFanoutUnitAttempt, free.holder_attempt_id, with_for_update=True)
            if stale is not None and stale.status == "running":
                stale.status, stale.completed_at = "fenced", now
                _release_slot(s, stale, now)
                su = s.get(BoundedFanoutUnit, stale.unit_id, with_for_update=True)
                if su is not None and su.active_attempt_id == stale.id:
                    su.active_attempt_id = None
                    if su.status == "claimed":
                        su.status = "pending"
        prior = s.scalar(select(func.max(BoundedFanoutUnitAttempt.attempt_number)).where(
            BoundedFanoutUnitAttempt.unit_id == unit_id)) or 0
        claim_token = uuid.uuid4().hex
        attempt = BoundedFanoutUnitAttempt(
            id=uuid.uuid4().hex, unit_id=unit_id, attempt_number=int(prior) + 1,
            parent_attempt_id=parent_attempt.id, status="running", owner_token=owner_token,
            claim_token=claim_token, slot_number=free.slot_number,
            lease_until=now + datetime.timedelta(seconds=_LEASE_S),
            heartbeat_at=now, created_at=now, started_at=now)
        s.add(attempt)
        s.flush()
        free.holder_attempt_id, free.claim_token, free.lease_until = (
            attempt.id, claim_token, attempt.lease_until)
        unit.status, unit.active_attempt_id = "claimed", attempt.id
        unit.updated_at = plan.updated_at = now
        s.flush()
        return _attempt_doc(attempt)
def heartbeat_attempt(*, attempt_id, claim_token, owner_token) -> bool:
    with session() as s:
        now = _durable_task_db_now(s)
        attempt = s.get(BoundedFanoutUnitAttempt, str(attempt_id), with_for_update=True)
        if (attempt is None or attempt.status != "running"
                or attempt.owner_token != str(owner_token)
                or attempt.claim_token != str(claim_token) or attempt.slot_number is None):
            return False
        lease = _tz(attempt.lease_until)
        if lease is None or lease <= now:
            return False
        slot = s.get(BoundedFanoutSlot, (SLOT_SCOPE, attempt.slot_number), with_for_update=True)
        if (slot is None or slot.holder_attempt_id != attempt.id
                or slot.claim_token != attempt.claim_token):
            return False
        until = now + datetime.timedelta(seconds=_LEASE_S)
        attempt.heartbeat_at = now
        attempt.lease_until = slot.lease_until = until
        return True
def _live_claim(s, attempt_id, claim_token, owner_token):
    now = _durable_task_db_now(s)
    attempt = s.get(BoundedFanoutUnitAttempt, str(attempt_id), with_for_update=True)
    if attempt is None:
        raise RuntimeError("fan-out attempt does not exist")
    unit = s.get(BoundedFanoutUnit, attempt.unit_id, with_for_update=True)
    plan = s.get(BoundedFanoutPlan, unit.parent_task_id, with_for_update=True) if unit else None
    parent = s.get(DurableTask, plan.parent_task_id, with_for_update=True) if plan else None
    lease = _tz(attempt.lease_until)
    if (unit is None or plan is None or parent is None or attempt.status != "running"
            or attempt.owner_token != str(owner_token) or attempt.claim_token != str(claim_token)
            or lease is None or lease <= now or attempt.slot_number is None or plan.paused
            or parent.cancel_requested):
        raise RuntimeError("fan-out claim token is stale or fenced")
    slot = s.get(BoundedFanoutSlot, (SLOT_SCOPE, attempt.slot_number), with_for_update=True)
    if (slot is None or slot.holder_attempt_id != attempt.id
            or slot.claim_token != attempt.claim_token):
        raise RuntimeError("fan-out claim token is stale or fenced")
    return plan, unit, attempt, now
def fail_attempt(*, attempt_id, claim_token, owner_token, diagnostic=None) -> dict:
    return _finish(attempt_id, claim_token, owner_token, "failed", diagnostic)
def cancel_attempt(*, attempt_id, claim_token, owner_token, diagnostic=None) -> dict:
    return _finish(attempt_id, claim_token, owner_token, "cancelled", diagnostic)
def _finish(attempt_id, claim_token, owner_token, status, diagnostic) -> dict:
    diag = None if diagnostic is None else str(diagnostic)[:2048]
    with session() as s:
        plan, unit, attempt, now = _live_claim(s, attempt_id, claim_token, owner_token)
        if unit.status == "done":
            raise RuntimeError("cannot fail a unit with committed evidence")
        attempt.status, attempt.diagnostic, attempt.completed_at = status, diag, now
        _release_slot(s, attempt, now)
        unit.status, unit.active_attempt_id = status, None
        unit.updated_at = plan.updated_at = now
        s.flush()
        return _attempt_doc(attempt)
def commit_unit_evidence_db(
        *, attempt_id, claim_token, owner_token, uri, namespace_id, writer_token,
        lock_token, rows, size_bytes, content_sha256, schema_sha256, dev, ino) -> dict:
    uri, namespace_id, writer_token = str(uri), str(namespace_id), str(writer_token)
    lock_token = str(lock_token) if lock_token is not None else None
    content_sha256, schema_sha256 = _sha(content_sha256), _sha(schema_sha256)
    rows, size_bytes, dev, ino = int(rows), int(size_bytes), int(dev), int(ino)
    if rows < 0 or size_bytes <= 0 or dev < 0 or ino < 0:
        raise ValueError("fan-out evidence shape is invalid")
    with session() as s:
        plan, unit, attempt, now = _live_claim(s, attempt_id, claim_token, owner_token)
        _lock_local_result_registry(s)
        if unit.status == "done":
            if (unit.result_uri != uri or int(unit.result_rows) != rows
                    or int(unit.result_bytes) != size_bytes
                    or unit.result_content_sha256 != content_sha256
                    or unit.result_schema_sha256 != schema_sha256
                    or int(unit.result_dev) != dev or int(unit.result_ino) != ino):
                raise RuntimeError("fan-out evidence replay changed committed truth")
            return _plan_doc(s, plan)
        if rows != int(unit.range_end) - int(unit.range_start):
            raise RuntimeError("fan-out evidence rows do not match the unit range")
        if schema_sha256 != plan.checkpoint_schema_sha256:
            raise RuntimeError("fan-out evidence schema does not match the checkpoint")
        if unit.kind == "gather":
            children = list(s.scalars(select(BoundedFanoutUnit).where(
                BoundedFanoutUnit.parent_task_id == plan.parent_task_id,
                BoundedFanoutUnit.kind == "child").with_for_update()))
            if (not children or any(c.status != "done" for c in children)
                    or rows != int(plan.checkpoint_rows)):
                raise RuntimeError("gather evidence requires validated child evidence")
        owner_kind, owner_key = _owner(unit)
        artifact = s.get(LocalResultArtifact, uri, with_for_update=True)
        if (artifact is None or artifact.namespace_id != namespace_id
                or artifact.state != "writing" or artifact.committed_at is not None
                or artifact.writer_run_id != attempt.id or artifact.writer_token != writer_token
                or bool(artifact.lock_protected) != bool(lock_token)
                or (lock_token is not None and artifact.lock_token != lock_token)):
            raise RuntimeError("fan-out artifact is not the current uncommitted writer")
        if s.scalar(select(LocalResultReference.uri).where(
                LocalResultReference.uri == uri).limit(1)) is not None:
            raise RuntimeError("fan-out artifact already has a reference before commit")
        s.add(LocalResultReference(uri=uri, owner_kind=owner_kind, owner_key=owner_key))
        artifact.state, artifact.committed_at = "ready", now
        artifact.writer_run_id = artifact.writer_token = None
        unit.status, unit.result_uri = "done", uri
        unit.result_rows, unit.result_bytes = rows, size_bytes
        unit.result_content_sha256, unit.result_schema_sha256 = content_sha256, schema_sha256
        unit.result_dev, unit.result_ino = dev, ino
        unit.active_attempt_id = None
        unit.updated_at = plan.updated_at = now
        attempt.status, attempt.completed_at = "done", now
        _release_slot(s, attempt, now)
        s.flush()
        return _plan_doc(s, plan)
def retry_unit(*, parent_task_id, unit_id, parent_attempt_id, owner_token) -> dict:
    with session() as s:
        _fence_parent(s, str(parent_task_id), str(parent_attempt_id), str(owner_token))
        plan = s.get(BoundedFanoutPlan, str(parent_task_id), with_for_update=True)
        unit = s.get(BoundedFanoutUnit, str(unit_id), with_for_update=True)
        if plan is None or unit is None or unit.parent_task_id != plan.parent_task_id:
            raise RuntimeError("fan-out unit is not part of the parent plan")
        if unit.status == "done":
            return _plan_doc(s, plan)
        _lock_local_result_registry(s)
        now = _durable_task_db_now(s)
        if unit.active_attempt_id:
            active = s.get(BoundedFanoutUnitAttempt, unit.active_attempt_id, with_for_update=True)
            if active is not None and active.status == "running":
                active.status, active.completed_at = "fenced", now
                _release_slot(s, active, now)
        if unit.result_uri is not None:
            owner_kind, owner_key = _owner(unit)
            for ref in s.scalars(select(LocalResultReference).where(
                    LocalResultReference.uri == unit.result_uri,
                    LocalResultReference.owner_kind == owner_kind,
                    LocalResultReference.owner_key == owner_key).with_for_update()):
                s.delete(ref)
        unit.status = "pending"
        unit.result_uri = unit.result_content_sha256 = unit.result_schema_sha256 = None
        unit.result_rows = unit.result_bytes = unit.result_dev = unit.result_ino = None
        unit.active_attempt_id = None
        unit.updated_at = plan.updated_at = now
        s.flush()
        return _plan_doc(s, plan)
def purge_for_delete(s, parent_task_ids: list[str]) -> None:
    if not parent_task_ids:
        return
    plans = list(s.scalars(select(BoundedFanoutPlan).where(
        BoundedFanoutPlan.parent_task_id.in_(parent_task_ids)).with_for_update()))
    if not plans:
        return
    units = list(s.scalars(select(BoundedFanoutUnit).where(
        BoundedFanoutUnit.parent_task_id.in_(parent_task_ids)).with_for_update()))
    unit_ids = [u.unit_id for u in units]
    attempts = list(s.scalars(select(BoundedFanoutUnitAttempt).where(
        BoundedFanoutUnitAttempt.unit_id.in_(unit_ids)).with_for_update())) if unit_ids else []
    now = _durable_task_db_now(s)
    for attempt in attempts:
        _release_slot(s, attempt, now)
    for unit in units:
        if unit.result_uri is None:
            continue
        owner_kind, owner_key = _owner(unit)
        s.get(LocalResultArtifact, unit.result_uri, with_for_update=True)
        for ref in s.scalars(select(LocalResultReference).where(
                LocalResultReference.uri == unit.result_uri,
                LocalResultReference.owner_kind == owner_kind,
                LocalResultReference.owner_key == owner_key).with_for_update()):
            s.delete(ref)
        unit.result_uri = unit.result_content_sha256 = unit.result_schema_sha256 = None
        unit.result_rows = unit.result_bytes = unit.result_dev = unit.result_ino = None
        unit.status, unit.active_attempt_id = "cancelled", None
    s.flush()
    for attempt in attempts:
        s.delete(attempt)
    if attempts:
        s.flush()
    for unit in units:
        s.delete(unit)
    if units:
        s.flush()
    for plan in plans:
        s.delete(plan)
    s.flush()
def restore_audit() -> list[dict]:
    report, live = [], set()
    with session() as s:
        slots = list(s.scalars(select(BoundedFanoutSlot).where(
            BoundedFanoutSlot.scope == SLOT_SCOPE).order_by(BoundedFanoutSlot.slot_number)))
        if len(slots) != 4 or [slot.slot_number for slot in slots] != [0, 1, 2, 3]:
            raise RuntimeError("restored bounded fan-out slots are corrupt")
        _lock_local_result_registry(s)
        for plan in s.scalars(select(BoundedFanoutPlan).order_by(BoundedFanoutPlan.parent_task_id)):
            task = s.get(DurableTask, plan.parent_task_id)
            if task is None or task.task_kind not in _CHECKPOINT_PARENT_KINDS:
                raise RuntimeError("restored fan-out plan has no owning parent task")
            units = list(s.scalars(select(BoundedFanoutUnit).where(
                BoundedFanoutUnit.parent_task_id == plan.parent_task_id).order_by(
                    BoundedFanoutUnit.kind.desc(), BoundedFanoutUnit.partition_index)))
            if (sum(1 for u in units if u.kind == "gather") != 1
                    or sum(1 for u in units if u.kind == "child") != int(plan.partition_count)):
                raise RuntimeError("restored fan-out plan unit set is incomplete")
            for unit in units:
                if unit.plan_digest != plan.plan_digest:
                    raise RuntimeError("restored fan-out unit digest mismatch")
                if unit.status != "done":
                    continue
                if unit.result_uri is None:
                    raise RuntimeError("restored fan-out done unit lacks evidence")
                artifact = s.get(LocalResultArtifact, unit.result_uri)
                owners = list(s.scalars(select(LocalResultReference).where(
                    LocalResultReference.uri == unit.result_uri)))
                owner_kind, owner_key = _owner(unit)
                if (artifact is None or artifact.state != "ready" or len(owners) != 1
                        or owners[0].owner_kind != owner_kind or owners[0].owner_key != owner_key):
                    raise RuntimeError("restored fan-out evidence owner is inconsistent")
                live.add((owner_kind, owner_key))
            report.append({
                "parent_task_id": plan.parent_task_id, "plan_digest": plan.plan_digest,
                "partition_count": int(plan.partition_count),
                "done_units": sum(1 for u in units if u.status == "done"),
            })
        for kind in (CHILD_OWNER, GATHER_OWNER):
            for ref in s.scalars(select(LocalResultReference).where(
                    LocalResultReference.owner_kind == kind).order_by(
                        LocalResultReference.owner_key)):
                if (ref.owner_kind, ref.owner_key) not in live:
                    raise RuntimeError("restored fan-out owner has no matching unit")
    return report
def reserve_unit_artifact(storage, *, attempt_id: str) -> dict:
    uri = storage.begin_result(f"fanout_{attempt_id[:12]}", attempt_id)
    lock_fd = storage.result_lock_fd(uri, attempt_id)
    return {
        "uri": uri, "namespace_id": storage.namespace_id, "storage_root": storage.result_root,
        "writer_token": storage._writer_token(uri, attempt_id),
        "lock_token": storage._read_lock_token(lock_fd) if lock_fd is not None else None,
        "attempt_id": attempt_id,
    }
def commit_unit_evidence(
        storage, *, attempt_id, claim_token, owner_token, candidate, content: bytes) -> dict:
    uri = candidate["uri"]
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    writer_fd = storage.result_lock_fd(uri, attempt_id)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    proof = storage.open_checkpoint_proof(uri, writer_fd)
    plan = None
    try:
        ev = proof.evidence
        proof.recheck()
        try:
            plan = commit_unit_evidence_db(
                attempt_id=attempt_id, claim_token=claim_token, owner_token=owner_token,
                uri=uri, namespace_id=candidate["namespace_id"],
                writer_token=candidate["writer_token"], lock_token=candidate["lock_token"],
                rows=ev["rows"], size_bytes=ev["bytes"], content_sha256=ev["content_sha256"],
                schema_sha256=ev["schema_sha256"], dev=ev["dev"], ino=ev["ino"])
        except Exception:
            with session() as s:
                unit = s.scalar(select(BoundedFanoutUnit).where(
                    BoundedFanoutUnit.result_uri == uri).limit(1))
            if unit is not None and unit.status == "done":
                with contextlib.suppress(Exception):
                    storage.release_result(uri, attempt_id)
            else:
                with contextlib.suppress(Exception):
                    storage.abort_result(uri, attempt_id)
            raise
    finally:
        proof.close()
    with contextlib.suppress(Exception):
        storage.release_result(uri, attempt_id)
    return plan
