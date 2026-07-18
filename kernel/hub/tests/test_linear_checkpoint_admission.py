from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import os
import re
import uuid

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from hub import metadb


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('linear-checkpoints') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _identity() -> dict:
    suffix = uuid.uuid4().hex
    uid, canvas_id, submission = (
        f"checkpoint-user-{suffix}", f"checkpoint-canvas-{suffix}", str(uuid.uuid4()).upper())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Checkpoint researcher"))
        session.flush()
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=uid, name="Checkpoint admission", doc="{}"))
    graph = {
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "checkpoint", "type": "write", "data": {
                "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
            {"id": "final", "type": "write", "data": {
                "title": "final", "config": {"filename": "final.parquet"}}},
        ],
        "edges": [{"id": "edge", "source": "checkpoint", "target": "final",
                   "sourceHandle": "out", "targetHandle": "in"}],
    }
    key = f"write:{task_id}"
    intent = {
        "destination": {
            "logicalUri": f"/tmp/{suffix}/final.parquet", "name": "final",
            "provider": "managed-local-file"},
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "final", "provenance": "run",
            "fieldMappings": []}, "parents": []},
    }
    return {
        "uid": uid, "canvas_id": canvas_id, "submission_id": submission,
        "final_target_node_id": "final", "checkpoint_id": f"cp:{suffix}",
        "checkpoint_node_id": "checkpoint", "output_port_id": "out",
        "task_intent_sha256": "a" * 64, "graph_prefix_sha256": "b" * 64,
        "input_manifest_sha256": hashlib.sha256(b"[]").hexdigest(),
        "graph_doc": graph, "input_manifest": [], "write_intent": intent,
    }


def _submit(values: dict):
    return metadb.submit_linear_checkpoint_task(**values)


def _claim_and_reserve(values: dict, tmp_path, *, owner="lease-owner", writer=None):
    admission, _created = _submit(values)
    task = metadb.claim_linear_checkpoint_task(admission["task_id"], owner)
    assert task is not None
    attempt = task["attempts"][-1]
    kwargs = {
        "task_id": admission["task_id"], "attempt_id": attempt["id"],
        "owner_token": owner, "namespace_id": uuid.uuid4().hex,
        "storage_root": str(tmp_path / ".dp-results"),
        "writer_token": writer or uuid.uuid4().hex, "lock_token": uuid.uuid4().hex,
    }
    return admission, attempt, kwargs, metadb.reserve_linear_checkpoint_candidate(**kwargs)


def test_complete_admission_replays_canonically_and_is_product_hidden():
    values = _identity()
    first, created = _submit(values)
    reordered = {**values, "graph_doc": {
        "edges": values["graph_doc"]["edges"], "nodes": values["graph_doc"]["nodes"],
        "version": 1, "id": values["canvas_id"]}}
    replay, replay_created = _submit(reordered)

    assert created is True and replay_created is False
    assert replay == first
    assert first["submission_id"] == values["submission_id"].lower()
    assert first["graph_doc"]["nodes"] and first["write_intent"]["destination"]
    assert first["final_target_node_id"] == "final"
    assert first["checkpoint_node_id"] == "checkpoint"
    # Product readers now surface this kind (#414); the generic managed-local claim path still excludes it.
    assert metadb.durable_task(first["task_id"]) is not None
    assert metadb.durable_task_auth(first["task_id"]) == (values["uid"], values["canvas_id"])
    assert metadb.claim_durable_task(first["task_id"], "generic-worker") is None
    assert first["task_id"] not in metadb.recoverable_durable_task_ids()
    assert first["task_id"] in metadb.recoverable_linear_checkpoint_task_ids()
    assert metadb.list_workspace_runs(values["uid"], run_id=first["task_id"])["items"]

    changes = [
        {"final_target_node_id": "checkpoint"}, {"checkpoint_node_id": "final"},
        {"output_port_id": "other"}, {"checkpoint_id": f"other:{uuid.uuid4().hex}"},
        {"task_intent_sha256": "c" * 64}, {"graph_prefix_sha256": "d" * 64},
        {"write_intent": {**values["write_intent"], "destination": {
            **values["write_intent"]["destination"], "name": "changed"}}},
    ]
    for change in changes:
        with pytest.raises(metadb.DurableTaskSubmissionConflict):
            _submit({**values, **change})


def test_strict_documents_digests_identities_and_size(monkeypatch):
    base = _identity()
    invalid = [
        {"graph_doc": {**base["graph_doc"], "extra": True}},
        {"write_intent": {**base["write_intent"], "extra": True}},
        {"input_manifest": [{"node_id": "x"}]},
        {"checkpoint_id": "bad checkpoint"}, {"output_port_id": " bad"},
        {"task_intent_sha256": "A" * 64},
        {"input_manifest_sha256": "0" * 64},
    ]
    for change in invalid:
        with pytest.raises((ValueError, RuntimeError)):
            _submit({**base, **change})
    monkeypatch.setattr(metadb, "_DURABLE_TASK_DOC_MAX_BYTES", 8)
    with pytest.raises(ValueError, match="bounded"):
        _submit(_identity())


@pytest.mark.parametrize(
    "table", ("durable_tasks", "durable_task_attempts", "durable_checkpoints"))
def test_admission_failure_after_each_insert_rolls_back_all_state(table):
    values = _identity()
    task_id = metadb.local_run_submission_id(
        values["uid"], values["canvas_id"], values["submission_id"])

    def fail_after_insert(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith(f"insert into {table}"):
            raise RuntimeError(f"injected failure after {table}")

    event.listen(metadb.engine(), "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            _submit(values)
    finally:
        event.remove(metadb.engine(), "after_cursor_execute", fail_after_insert)
    with metadb.session() as session:
        assert session.get(metadb.DurableTask, task_id) is None
        assert session.get(metadb.DurableCheckpoint, task_id) is None
        assert session.scalar(select(func.count()).select_from(
            metadb.DurableTaskAttempt).where(
                metadb.DurableTaskAttempt.task_id == task_id)) == 0


def test_reservation_is_db_only_exact_and_replay_safe(tmp_path):
    values = _identity()
    admission, attempt, kwargs, binding = _claim_and_reserve(values, tmp_path)
    root = kwargs["storage_root"]

    assert not os.path.exists(root)
    assert binding == metadb.linear_checkpoint_candidate(admission["task_id"])
    assert binding["attempt_id"] == attempt["id"]
    assert binding["namespace_id"] == kwargs["namespace_id"]
    assert re.fullmatch(r"__result_checkpoint_[0-9a-f]{64}\.parquet",
                        os.path.basename(binding["uri"]))
    assert metadb.reserve_linear_checkpoint_candidate(**kwargs) == binding
    for changed in (
        {"writer_token": uuid.uuid4().hex}, {"namespace_id": uuid.uuid4().hex},
        {"lock_token": uuid.uuid4().hex},
    ):
        with pytest.raises(RuntimeError, match="changed exact authority"):
            metadb.reserve_linear_checkpoint_candidate(**{**kwargs, **changed})
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.LocalResultArtifact).where(
                metadb.LocalResultArtifact.writer_run_id == admission["task_id"])) == 1

    with metadb.session() as session:
        row = session.get(metadb.DurableCheckpoint, admission["task_id"])
        original = row.candidate_generation
        row.candidate_generation = "f" * 64 if original != "f" * 64 else "e" * 64
    with pytest.raises(RuntimeError, match="disagrees"):
        metadb.linear_checkpoint_candidate(admission["task_id"])
    with metadb.session() as session:
        session.get(metadb.DurableCheckpoint, admission["task_id"]).candidate_generation = original


def test_candidate_attempt_must_belong_to_checkpoint_task(tmp_path):
    values = _identity()
    admission, _attempt, _kwargs, binding = _claim_and_reserve(values, tmp_path)
    other, _ = _submit(_identity())

    with metadb.session() as session:
        task = session.get(metadb.DurableTask, admission["task_id"])
        checkpoint = session.get(metadb.DurableCheckpoint, admission["task_id"])
        old_artifact = session.get(metadb.LocalResultArtifact, binding["uri"])
        generation = metadb._linear_checkpoint_generation(
            task, checkpoint, other["attempt_id"])
        basename = f"{metadb._LOCAL_RESULT_PREFIX}checkpoint_{generation}.parquet"
        uri = os.path.join(old_artifact.storage_root, basename)
        session.add(metadb.LocalResultArtifact(
            uri=uri,
            namespace_id=old_artifact.namespace_id,
            storage_root=old_artifact.storage_root,
            lock_name=f"{basename[:-len('.parquet')]}.lock",
            lock_token=old_artifact.lock_token,
            lock_protected=old_artifact.lock_protected,
            state="writing",
            writer_run_id=task.id,
            writer_token=old_artifact.writer_token,
            created_at=old_artifact.created_at,
        ))
        session.flush()
        checkpoint.candidate_uri = uri
        checkpoint.candidate_generation = generation
        checkpoint.candidate_attempt_id = other["attempt_id"]
        session.flush()
        session.delete(old_artifact)

    with pytest.raises(RuntimeError, match="different task"):
        metadb.linear_checkpoint_candidate(admission["task_id"])


def test_reservation_update_failure_rolls_back_flushed_artifact(tmp_path):
    values = _identity()
    admission, _ = _submit(values)
    claimed = metadb.claim_linear_checkpoint_task(admission["task_id"], "rollback-owner")
    attempt_id = claimed["attempts"][-1]["id"]

    def fail_checkpoint_update(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("update durable_checkpoints"):
            raise RuntimeError("injected checkpoint binding failure")

    event.listen(metadb.engine(), "before_cursor_execute", fail_checkpoint_update)
    try:
        with pytest.raises(RuntimeError, match="injected checkpoint binding failure"):
            metadb.reserve_linear_checkpoint_candidate(
                task_id=admission["task_id"], attempt_id=attempt_id,
                owner_token="rollback-owner", namespace_id=uuid.uuid4().hex,
                storage_root=str(tmp_path / ".dp-results"),
                writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
    finally:
        event.remove(metadb.engine(), "before_cursor_execute", fail_checkpoint_update)

    assert metadb.linear_checkpoint_candidate(admission["task_id"]) is None
    with metadb.session() as session:
        assert session.scalar(select(func.count()).select_from(
            metadb.LocalResultArtifact).where(
                metadb.LocalResultArtifact.writer_run_id == admission["task_id"])) == 0


def test_stale_owner_and_new_attempt_cannot_rebind_old_candidate(tmp_path):
    stale = _identity()
    admission, _ = _submit(stale)
    claimed = metadb.claim_linear_checkpoint_task(admission["task_id"], "stale-owner")
    attempt = claimed["attempts"][-1]
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, attempt["id"]).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    stale_kwargs = {
        "task_id": admission["task_id"], "attempt_id": attempt["id"],
        "owner_token": "stale-owner", "namespace_id": uuid.uuid4().hex,
        "storage_root": str(tmp_path / "stale" / ".dp-results"),
        "writer_token": uuid.uuid4().hex, "lock_token": uuid.uuid4().hex}
    with pytest.raises(RuntimeError, match="stale or fenced"):
        metadb.reserve_linear_checkpoint_candidate(**stale_kwargs)
    assert metadb.linear_checkpoint_candidate(admission["task_id"]) is None

    values = _identity()
    admitted, old_attempt, kwargs, original = _claim_and_reserve(
        values, tmp_path / "transfer")
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, old_attempt["id"]).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    reclaimed = metadb.claim_linear_checkpoint_task(admitted["task_id"], "new-owner")
    new_attempt = reclaimed["attempts"][-1]
    with pytest.raises(RuntimeError, match="changed exact authority"):
        metadb.reserve_linear_checkpoint_candidate(**{
            **kwargs, "attempt_id": new_attempt["id"], "owner_token": "new-owner",
            "writer_token": uuid.uuid4().hex})
    assert metadb.linear_checkpoint_candidate(admitted["task_id"]) == original


def test_concurrent_reservations_choose_one_exact_writer(tmp_path):
    values = _identity()
    admission, _ = _submit(values)
    claimed = metadb.claim_linear_checkpoint_task(admission["task_id"], "race-owner")
    attempt_id = claimed["attempts"][-1]["id"]
    namespace, lock_token = uuid.uuid4().hex, uuid.uuid4().hex

    def reserve(writer):
        return metadb.reserve_linear_checkpoint_candidate(
            task_id=admission["task_id"], attempt_id=attempt_id,
            owner_token="race-owner", namespace_id=namespace,
            storage_root=str(tmp_path / ".dp-results"),
            writer_token=writer, lock_token=lock_token)

    writers = [uuid.uuid4().hex for _ in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reserve, writer) for writer in writers]
    successes = [future.result() for future in futures if future.exception() is None]
    assert len(successes) == 1
    assert successes[0]["writer_token"] in writers
    assert metadb.linear_checkpoint_candidate(admission["task_id"]) == successes[0]


def test_checkpoint_constraints_pair_candidate_and_inode_state():
    values = _identity()
    admission, _ = _submit(values)
    task_id = admission["task_id"]
    with pytest.raises(IntegrityError):
        with metadb.session() as session:
            session.get(metadb.DurableCheckpoint, task_id).candidate_generation = "1" * 64
    with pytest.raises(IntegrityError):
        with metadb.session() as session:
            session.get(metadb.DurableCheckpoint, task_id).candidate_dev = 1
    with pytest.raises(IntegrityError):
        with metadb.session() as session:
            row = session.get(metadb.DurableCheckpoint, task_id)
            row.candidate_dev, row.candidate_ino = -1, 2
    # A pending checkpoint cannot carry committed device/inode identity: that is committed-only
    # evidence, and a non-committed row must not hold half of it (0013 committed-evidence guard).
    with pytest.raises(IntegrityError):
        with metadb.session() as session:
            row = session.get(metadb.DurableCheckpoint, task_id)
            row.candidate_dev, row.candidate_ino = 1, 2
    assert metadb.linear_checkpoint_admission(task_id)["phase"] == "pending"
