"""Immutable bounded fan-out plan, four DB slots, and held LocalResult evidence."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from hub import bounded_fanout as fanout
from hub import metadb
from hub.storage import LocalStorage

from hub.tests.test_linear_checkpoint_admission import (  # noqa: F401
    _identity, _metadata_schema, _submit)
from hub.tests.test_linear_checkpoint_commit import _parquet_bytes, _reserve, _commit


def _storage(tmp_path) -> LocalStorage:
    return LocalStorage(str(tmp_path / "outputs"))


def _committed_parent(tmp_path, rows: int = 5):
    store = _storage(tmp_path)
    ctx = _reserve(_identity(), store)
    evidence = _commit(store, ctx, _parquet_bytes(rows))
    return store, ctx, evidence


def _plan_for(ctx, owner: str | None = None):
    return fanout.create_or_reopen_plan(
        parent_task_id=ctx["task_id"], parent_attempt_id=ctx["attempt_id"],
        owner_token=owner or ctx["owner"])


@pytest.fixture(autouse=True)
def _reset_fanout_state():
    """Isolate the global 4-slot pool + plans/units/result-owners between tests so the suite is safe
    on a shared (PostgreSQL) database, not only a fresh per-module SQLite file."""
    yield
    from sqlalchemy import delete, update
    with metadb.session() as s:
        s.execute(update(fanout.BoundedFanoutSlot).values(
            holder_attempt_id=None, claim_token=None, lease_until=None))
        s.execute(delete(fanout.BoundedFanoutUnitAttempt))
        s.execute(delete(fanout.BoundedFanoutUnit))
        s.execute(delete(fanout.BoundedFanoutPlan))
        s.execute(delete(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind.in_(
                (fanout.CHILD_OWNER, fanout.GATHER_OWNER))))


@pytest.mark.parametrize("rows,expected", [
    (0, [(0, 0)]),
    (1, [(0, 1)]),
    (3, [(0, 1), (1, 2), (2, 3)]),
    (4, [(0, 1), (1, 2), (2, 3), (3, 4)]),
    (5, [(0, 2), (2, 3), (3, 4), (4, 5)]),
    (11, [(0, 3), (3, 6), (6, 9), (9, 11)]),
])
def test_canonical_partition_coverage(rows, expected):
    ranges = fanout.partition_ranges(rows)
    assert ranges == expected
    assert ranges[0][0] == 0 and ranges[-1][1] == rows
    for i in range(1, len(ranges)):
        assert ranges[i][0] == ranges[i - 1][1]


def test_plan_replay_conflict_and_checkpoint_rejection(tmp_path):
    store, ctx, _evidence = _committed_parent(tmp_path, rows=5)
    plan = _plan_for(ctx)
    assert plan["partition_count"] == 4
    assert plan["ranges"] == [[0, 2], [2, 3], [3, 4], [4, 5]]
    assert sum(1 for u in plan["units"] if u["kind"] == "child") == 4
    assert sum(1 for u in plan["units"] if u["kind"] == "gather") == 1
    replay = _plan_for(ctx)
    assert replay["plan_digest"] == plan["plan_digest"]
    assert replay["units"] == plan["units"]

    with pytest.raises(ValueError, match="unsupported operation"):
        fanout.create_or_reopen_plan(
            parent_task_id=ctx["task_id"], parent_attempt_id=ctx["attempt_id"],
            owner_token=ctx["owner"], operation_id="other")
    with pytest.raises(ValueError, match="unsupported"):
        fanout.create_or_reopen_plan(
            parent_task_id=ctx["task_id"], parent_attempt_id=ctx["attempt_id"],
            owner_token=ctx["owner"], requested_partitions=3)

    with metadb.session() as s:
        attempt = s.get(metadb.DurableTaskAttempt, ctx["attempt_id"])
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=120)
    with pytest.raises(RuntimeError, match="stale or missing"):
        _plan_for(ctx)

    pending = _reserve(_identity(), store)
    with pytest.raises(RuntimeError, match="committed checkpoint"):
        fanout.create_or_reopen_plan(
            parent_task_id=pending["task_id"], parent_attempt_id=pending["attempt_id"],
            owner_token=pending["owner"])


def test_claim_heartbeat_finish_pause_expiry_and_max_four_slots(tmp_path):
    _store, ctx, _evidence = _committed_parent(tmp_path, rows=5)
    plan = _plan_for(ctx)
    children = [u for u in plan["units"] if u["kind"] == "child"]
    gather = next(u for u in plan["units"] if u["kind"] == "gather")

    with pytest.raises(RuntimeError, match="unclaimable"):
        fanout.claim_unit(
            parent_task_id=ctx["task_id"], unit_id=gather["unit_id"],
            parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])

    claims = [
        fanout.claim_unit(
            parent_task_id=ctx["task_id"], unit_id=child["unit_id"],
            parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
        for child in children
    ]
    assert {c["slot_number"] for c in claims} == {0, 1, 2, 3}
    with metadb.session() as s:
        held = list(s.scalars(select(fanout.BoundedFanoutSlot).where(
            fanout.BoundedFanoutSlot.holder_attempt_id.is_not(None))))
        assert len(held) == 4

    # A second parent with identical content digest still cannot mint a fifth slot.
    _store2, ctx2, _ = _committed_parent(tmp_path / "other", rows=5)
    plan2 = _plan_for(ctx2)
    assert plan2["plan_digest"] == plan["plan_digest"]
    child2 = next(u for u in plan2["units"] if u["kind"] == "child")
    with pytest.raises(RuntimeError, match="no free"):
        fanout.claim_unit(
            parent_task_id=ctx2["task_id"], unit_id=child2["unit_id"],
            parent_attempt_id=ctx2["attempt_id"], owner_token=ctx2["owner"])

    first = claims[0]
    assert fanout.heartbeat_attempt(
        attempt_id=first["attempt_id"], claim_token=first["claim_token"],
        owner_token=ctx["owner"]) is True
    assert fanout.heartbeat_attempt(
        attempt_id=first["attempt_id"], claim_token="deadbeef",
        owner_token=ctx["owner"]) is False

    fanout.pause_plan(
        parent_task_id=ctx["task_id"], parent_attempt_id=ctx["attempt_id"],
        owner_token=ctx["owner"])
    with pytest.raises(RuntimeError, match="paused|stale or fenced"):
        fanout.fail_attempt(
            attempt_id=first["attempt_id"], claim_token=first["claim_token"],
            owner_token=ctx["owner"])
    fanout.resume_plan(
        parent_task_id=ctx["task_id"], parent_attempt_id=ctx["attempt_id"],
        owner_token=ctx["owner"])
    fanout.fail_attempt(
        attempt_id=first["attempt_id"], claim_token=first["claim_token"],
        owner_token=ctx["owner"], diagnostic="boom")

    second = claims[1]
    with metadb.session() as s:
        attempt = s.get(fanout.BoundedFanoutUnitAttempt, second["attempt_id"])
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=60)
        slot = s.get(fanout.BoundedFanoutSlot, (fanout.SLOT_SCOPE, second["slot_number"]))
        slot.lease_until = attempt.lease_until
    replacement = fanout.claim_unit(
        parent_task_id=ctx["task_id"], unit_id=second["unit_id"],
        parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    assert replacement["attempt_id"] != second["attempt_id"]
    assert replacement["claim_token"] != second["claim_token"]
    assert fanout.heartbeat_attempt(
        attempt_id=second["attempt_id"], claim_token=second["claim_token"],
        owner_token=ctx["owner"]) is False
    with pytest.raises(RuntimeError, match="stale or fenced"):
        fanout.fail_attempt(
            attempt_id=second["attempt_id"], claim_token=second["claim_token"],
            owner_token=ctx["owner"])

    again = fanout.claim_unit(
        parent_task_id=ctx["task_id"], unit_id=second["unit_id"],
        parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    assert again["attempt_id"] == replacement["attempt_id"]
    assert again["claim_token"] == replacement["claim_token"]


def test_child_and_gather_evidence_lifecycle(tmp_path):
    store, ctx, _evidence = _committed_parent(tmp_path, rows=3)
    plan = _plan_for(ctx)
    children = [u for u in plan["units"] if u["kind"] == "child"]
    gather = next(u for u in plan["units"] if u["kind"] == "gather")
    assert plan["ranges"] == [[0, 1], [1, 2], [2, 3]]

    for child in children:
        claim = fanout.claim_unit(
            parent_task_id=ctx["task_id"], unit_id=child["unit_id"],
            parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
        candidate = fanout.reserve_unit_artifact(store, attempt_id=claim["attempt_id"])
        content = _parquet_bytes(child["range_end"] - child["range_start"])
        # Wrong rows rejected even when bytes are valid parquet.
        with pytest.raises(RuntimeError, match="rows"):
            fanout.commit_unit_evidence(
                store, attempt_id=claim["attempt_id"], claim_token=claim["claim_token"],
                owner_token=ctx["owner"], candidate=candidate, content=_parquet_bytes(2))
        candidate = fanout.reserve_unit_artifact(store, attempt_id=claim["attempt_id"])
        plan = fanout.commit_unit_evidence(
            store, attempt_id=claim["attempt_id"], claim_token=claim["claim_token"],
            owner_token=ctx["owner"], candidate=candidate, content=content)

    gclaim = fanout.claim_unit(
        parent_task_id=ctx["task_id"], unit_id=gather["unit_id"],
        parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    g_candidate = fanout.reserve_unit_artifact(store, attempt_id=gclaim["attempt_id"])
    with pytest.raises(RuntimeError, match="rows"):
        fanout.commit_unit_evidence(
            store, attempt_id=gclaim["attempt_id"], claim_token=gclaim["claim_token"],
            owner_token=ctx["owner"], candidate=g_candidate, content=_parquet_bytes(2))
    g_candidate = fanout.reserve_unit_artifact(store, attempt_id=gclaim["attempt_id"])
    plan = fanout.commit_unit_evidence(
        store, attempt_id=gclaim["attempt_id"], claim_token=gclaim["claim_token"],
        owner_token=ctx["owner"], candidate=g_candidate, content=_parquet_bytes(3))
    assert all(u["status"] == "done" for u in plan["units"])
    assert fanout.restore_audit()

    done_child = next(u for u in plan["units"] if u["kind"] == "child")
    before = fanout.retry_unit(
        parent_task_id=ctx["task_id"], unit_id=done_child["unit_id"],
        parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    assert next(u["status"] for u in before["units"]
                if u["unit_id"] == done_child["unit_id"]) == "done"


def test_canvas_delete_purges_plan_and_owners(tmp_path):
    store, ctx, _ = _committed_parent(tmp_path, rows=2)
    plan = _plan_for(ctx)
    child = next(u for u in plan["units"] if u["kind"] == "child")
    claim = fanout.claim_unit(
        parent_task_id=ctx["task_id"], unit_id=child["unit_id"],
        parent_attempt_id=ctx["attempt_id"], owner_token=ctx["owner"])
    candidate = fanout.reserve_unit_artifact(store, attempt_id=claim["attempt_id"])
    fanout.commit_unit_evidence(
        store, attempt_id=claim["attempt_id"], claim_token=claim["claim_token"],
        owner_token=ctx["owner"], candidate=candidate,
        content=_parquet_bytes(child["range_end"] - child["range_start"]))
    uri = candidate["uri"]
    with metadb.session() as s:
        task = s.get(metadb.DurableTask, ctx["task_id"])
        canvas_id = task.canvas_id
        task.status = "done"
        attempt = s.get(metadb.DurableTaskAttempt, ctx["attempt_id"])
        attempt.status = "done"
        attempt.lease_until = metadb._now() - datetime.timedelta(seconds=1)
    metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as s:
        assert s.get(fanout.BoundedFanoutPlan, ctx["task_id"]) is None
        owners = list(s.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.uri == uri)))
        assert owners == []
    report = {row["parent_task_id"]: row for row in fanout.restore_audit()}
    assert ctx["task_id"] not in report
