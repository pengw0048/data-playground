from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from types import SimpleNamespace

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import func, select

from hub import durable_tasks, local_run_inputs, metadb
from hub.deps import Deps
from hub.local_run_inputs import LOCAL_FILE_INPUT_PROVIDER
from hub.local_writes import write_managed_local_file
from hub.models import Graph, RunStatus
from hub.routers import runs
from hub.routers.runs import (
    _inject_write_intent, _local_run_intent_sha256, _resolve_local_run_manifest,
    _write_admission_for_graph,
)


@pytest.fixture(scope="module", autouse=True)
def _metadata_schema(tmp_path_factory):
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = os.environ.get("DP_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('durable-tasks') / 'metadata.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@pytest.fixture
def task_identity():
    suffix = uuid.uuid4().hex
    uid, canvas_id = f"task-user-{suffix}", f"task-canvas-{suffix}"
    submission = str(uuid.uuid4())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Task researcher"))
        session.flush()
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=uid, name="Durable write", doc="{}"))
    graph = {
        "id": canvas_id, "version": 1,
        "nodes": [{"id": "write", "type": "write", "data": {
            "title": "result", "config": {"filename": "result.parquet"},
        }}], "edges": [],
    }
    intent = {
        "destination": {
            "logicalUri": f"/tmp/{suffix}/result.parquet", "name": "result",
            "provider": "managed-local-file",
        },
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "write", "provenance": "run",
            "fieldMappings": [],
        }, "parents": []},
    }
    return uid, canvas_id, submission, task_id, graph, intent


def _submit(identity):
    uid, canvas_id, submission, _task_id, graph, intent = identity
    return metadb.submit_durable_local_write_task(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        target_node_id="write", intent_sha256="a" * 64,
        graph_doc=graph, input_manifest=[],
        write_intent=intent,
    )


def _write_ordinary_source(path, values: list[int]) -> None:
    table = pa.table({"value": values})
    if path.suffix == ".parquet":
        pq.write_table(table, path)
    elif path.suffix == ".csv":
        import pyarrow.csv as csv
        csv.write_csv(table, path)
    elif path.suffix == ".json":
        path.write_text("".join(json.dumps({"value": value}) + "\n" for value in values))
    elif path.suffix == ".arrow":
        import pyarrow.ipc as ipc
        with ipc.new_file(path, table.schema) as writer:
            writer.write_table(table)
    else:  # pragma: no cover - test helper contract
        raise AssertionError(path.suffix)


def _ordinary_task_context(tmp_path, suffix: str = ".parquet"):
    token = uuid.uuid4().hex
    workspace = tmp_path / f"workspace-{token}"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True)
    source = data_dir / f"ordinary{suffix}"
    _write_ordinary_source(source, [1, 2])
    deps = Deps(str(workspace), str(data_dir), maintain_storage=False)
    deps.catalog._add(name=f"ordinary-{token}", uri=str(source), strict_probe=True)
    uid, canvas_id = f"ordinary-user-{token}", f"ordinary-canvas-{token}"
    graph = Graph.model_validate({
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {
                "config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "data": {
                "title": f"ordinary-output-{token}", "config": {
                    "filename": f"ordinary-output-{token}.parquet",
                    "writeMode": "overwrite",
                }}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Ordinary task researcher"))
        session.flush()
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=uid, name="Ordinary durable task",
            doc=json.dumps(graph.model_dump(by_alias=True, mode="json"))))
    return deps, graph, source, uid


def _start_without_dispatch(deps, graph, uid, submission, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(
        durable_tasks, "dispatch", lambda task_id, _deps: dispatched.append(str(task_id)))
    status, owner = runs.start_run(
        deps, graph, "write", uid, confirmed=True, submission_id=submission)
    assert owner is None
    return status, dispatched


def _task_input_ref(task_id: str):
    with metadb.session() as session:
        return session.scalar(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "durable_task",
            metadb.LocalResultReference.owner_key == task_id,
        ))


@pytest.mark.parametrize("suffix", (".parquet", ".csv", ".json", ".arrow"))
def test_ordinary_local_formats_are_task_owned_before_dispatch(
        tmp_path, monkeypatch, suffix):
    deps, graph, source, uid = _ordinary_task_context(tmp_path, suffix)
    submission = str(uuid.uuid4())
    status, dispatched = _start_without_dispatch(
        deps, graph, uid, submission, monkeypatch)

    task = metadb.durable_task(status.run_id)
    assert task is not None and task["status"] == "queued"
    assert len(task["attempts"]) == 1
    assert task["input_manifest"][0]["provider"] == LOCAL_FILE_INPUT_PROVIDER
    assert task["graph_doc"]["nodes"][0]["data"]["config"]["uri"] == str(source)
    ref = _task_input_ref(task["id"])
    assert ref is not None
    jobs = metadb.list_workspace_runs(uid, run_id=task["id"])
    assert jobs["items"][0]["inputManifest"] == task["input_manifest"]
    assert ref.uri not in json.dumps(task["graph_doc"])
    assert ref.uri not in json.dumps(jobs["items"][0]["inputManifest"])
    with metadb.session() as session:
        artifact = session.get(metadb.LocalResultArtifact, ref.uri)
        assert artifact is not None and artifact.state == "ready"
        assert (artifact.writer_run_id, artifact.writer_token) == (None, None)
    assert dispatched == [task["id"]]

    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_durable_task(task["id"], "cancel-before-dispatch") is None
    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.prune_results(limit=20)
    assert source.exists()
    deps.storage.close()


def test_ordinary_local_source_admits_replace_task(tmp_path, monkeypatch):
    deps, graph, _source, uid = _ordinary_task_context(tmp_path)
    create = _write_admission_for_graph(
        deps, graph, "write", uid, str(uuid.uuid4()))
    assert create.intent is not None and create.intent.mode == "create"
    write_managed_local_file(
        storage=deps.storage, catalog=deps.catalog, intent=create.intent,
        write_artifact=lambda uri: pq.write_table(pa.table({"value": [0]}), uri),
    )

    status, _dispatched = _start_without_dispatch(
        deps, graph, uid, str(uuid.uuid4()), monkeypatch)
    task = metadb.durable_task(status.run_id)
    assert task is not None
    assert task["write_intent"]["mode"] == "replace"
    assert task["input_manifest"][0]["provider"] == LOCAL_FILE_INPUT_PROVIDER
    assert _task_input_ref(task["id"]) is not None
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_durable_task(task["id"], "cancel-replace") is None
    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def test_response_loss_replay_adopts_task_after_source_deletion_without_resolution(
        tmp_path, monkeypatch):
    deps, graph, source, uid = _ordinary_task_context(tmp_path)
    submission = str(uuid.uuid4())
    replay_graph = graph.model_copy(deep=True)
    dispatched: list[str] = []
    monkeypatch.setattr(
        durable_tasks, "dispatch", lambda task_id, _deps: dispatched.append(str(task_id)))
    first, _owner = runs.start_run(
        deps, graph, "write", uid, confirmed=True, submission_id=submission)
    task = metadb.durable_task(first.run_id)
    assert task is not None
    artifact_uri = _task_input_ref(task["id"]).uri
    source.unlink()

    def source_access_is_a_bug(_uri):
        raise AssertionError("response-loss replay touched the mutable Source")

    monkeypatch.setattr(deps.catalog, "resolve_ref", source_access_is_a_bug)
    replay, _owner = runs.start_run(
        deps, replay_graph, "write", uid,
        confirmed=True, submission_id=submission)

    adopted = metadb.durable_task(replay.run_id)
    assert replay.run_id == first.run_id
    assert adopted is not None and len(adopted["attempts"]) == 1
    assert _task_input_ref(adopted["id"]).uri == artifact_uri
    assert dispatched == [task["id"], task["id"]]
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_durable_task(task["id"], "cancel-replay") is None
    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def test_supplied_snapshot_manifest_keeps_task_graph_logical(tmp_path, monkeypatch):
    deps, graph, source, uid = _ordinary_task_context(tmp_path)
    candidates: list[dict[str, str]] = []
    manifest = _resolve_local_run_manifest(
        graph, "write", deps,
        materialize_local_files=True,
        local_file_candidates=candidates,
    )
    admission_id, created = metadb.admit_local_run_inputs(
        uid=uid, canvas_id=str(graph.id), submission_id=str(uuid.uuid4()),
        target_node_id="write", intent_sha256="f" * 64, manifest=manifest,
        local_file_candidates=candidates,
    )
    assert created
    local_run_inputs.finalize_local_file_candidates(
        deps.storage, candidates, admission_id)
    artifact_uri = metadb.local_file_input_revision_artifact(
        manifest[0]["dataset_id"], manifest[0]["revision_id"])
    assert artifact_uri is not None

    monkeypatch.setattr(durable_tasks, "dispatch", lambda *_args: None)
    status, _owner = runs.start_run(
        deps, graph, "write", uid, confirmed=True,
        submission_id=str(uuid.uuid4()), input_manifest=manifest)

    task = metadb.durable_task(status.run_id)
    assert task is not None
    persisted_graph = json.dumps(task["graph_doc"])
    assert task["graph_doc"]["nodes"][0]["data"]["config"]["uri"] == str(source)
    assert artifact_uri not in persisted_graph
    assert "_input_artifact_uri" not in persisted_graph
    jobs = metadb.list_workspace_runs(uid, run_id=task["id"])
    assert artifact_uri not in json.dumps(jobs["items"])

    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_durable_task(task["id"], "cancel-supplied-manifest") is None
    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def test_restarted_worker_reads_snapshot_after_source_mutation(tmp_path, monkeypatch):
    deps, graph, source, uid = _ordinary_task_context(tmp_path)
    status, _dispatched = _start_without_dispatch(
        deps, graph, uid, str(uuid.uuid4()), monkeypatch)
    workspace, data_dir = deps.workspace, deps.data_dir
    deps.storage.close()
    moved_source = source.with_name("admitted-original.parquet")
    source.rename(moved_source)
    _write_ordinary_source(moved_source, [99])

    restarted = Deps(workspace, data_dir, maintain_storage=False)
    durable_tasks._worker(status.run_id, restarted)
    task = metadb.durable_task(status.run_id)
    assert task is not None and task["status"] == "done", task
    receipt = task["output_receipt"]
    assert receipt is not None
    assert pq.read_table(receipt["publication"]["artifactUri"])["value"].to_pylist() == [1, 2]
    assert task["graph_doc"]["nodes"][0]["data"]["config"]["uri"] == str(source)
    restarted.storage.close()


def test_task_admission_rollback_leaves_no_task_mapping_or_reference(tmp_path, monkeypatch):
    deps, graph, source, uid = _ordinary_task_context(tmp_path)
    submission = str(uuid.uuid4())
    task_id = metadb.durable_task_submission_id(uid, str(graph.id), submission)
    with metadb.session() as session:
        before = (
            session.scalar(select(func.count()).select_from(metadb.LocalFileInputRevision)),
            session.scalar(select(func.count()).select_from(metadb.LocalResultReference)),
        )
    original_sync = metadb.sync_local_result_owner

    def fail_task_owner(session, owner_kind, owner_key, *values):
        if owner_kind == "durable_task":
            raise RuntimeError("injected Task owner failure")
        return original_sync(session, owner_kind, owner_key, *values)

    monkeypatch.setattr(metadb, "sync_local_result_owner", fail_task_owner)
    monkeypatch.setattr(durable_tasks, "dispatch", lambda *_args: None)
    with pytest.raises(RuntimeError, match="injected Task owner failure"):
        runs.start_run(
            deps, graph, "write", uid, confirmed=True, submission_id=submission)

    assert metadb.durable_task(task_id) is None
    with metadb.session() as session:
        after = (
            session.scalar(select(func.count()).select_from(metadb.LocalFileInputRevision)),
            session.scalar(select(func.count()).select_from(metadb.LocalResultReference)),
        )
        assert session.scalar(select(func.count()).select_from(
            metadb.LocalResultReference).where(
                metadb.LocalResultReference.owner_kind == "durable_task",
                metadb.LocalResultReference.owner_key == task_id)) == 0
    assert after == before
    assert source.exists()
    deps.storage.close()


def test_fresh_task_retries_one_lost_snapshot_mapping(tmp_path, monkeypatch):
    deps, graph, _source, uid = _ordinary_task_context(tmp_path)
    submission = str(uuid.uuid4())
    original_submit = metadb.submit_durable_local_write_task
    attempts = 0

    def lose_first_mapping(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise metadb.LocalFileInputAdmissionRetry("injected reclaim winner")
        return original_submit(**kwargs)

    monkeypatch.setattr(metadb, "submit_durable_local_write_task", lose_first_mapping)
    status, _dispatched = _start_without_dispatch(
        deps, graph, uid, submission, monkeypatch)
    task = metadb.durable_task(status.run_id)
    assert attempts == 2
    assert task is not None and len(task["attempts"]) == 1
    assert _task_input_ref(task["id"]) is not None
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_durable_task(task["id"], "cancel-retried") is None
    metadb.delete_canvas_cascade(str(graph.id))
    deps.storage.close()


def _exercise_concurrent_ordinary_task_admission(tmp_path, monkeypatch):
    deps, graph, source, uid = _ordinary_task_context(tmp_path)
    submission = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    snapshot = local_run_inputs.snapshot_local_file_input
    candidates: list[dict[str, str]] = []
    candidate_lock = threading.Lock()

    def concurrent_snapshot(**kwargs):
        barrier.wait(timeout=5)
        result = snapshot(**kwargs)
        if result[1] is not None:
            with candidate_lock:
                candidates.append(result[1])
        return result

    monkeypatch.setattr(local_run_inputs, "snapshot_local_file_input", concurrent_snapshot)
    monkeypatch.setattr(durable_tasks, "dispatch", lambda *_args: None)

    def submit(_index):
        return runs.start_run(
            deps, graph.model_copy(deep=True), "write", uid,
            confirmed=True, submission_id=submission)[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, range(2)))
    assert statuses[0].model_dump() == statuses[1].model_dump()
    task = metadb.durable_task(statuses[0].run_id)
    assert task is not None and len(task["attempts"]) == 1
    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.LocalResultReference).where(
            metadb.LocalResultReference.owner_kind == "durable_task",
            metadb.LocalResultReference.owner_key == task["id"])))
        assert len(refs) == 1
        ref = refs[0]
        artifact = session.get(metadb.LocalResultArtifact, ref.uri)
        assert artifact is not None and artifact.state == "ready"
        assert (artifact.writer_run_id, artifact.writer_token) == (None, None)
        assert set(session.scalars(select(metadb.LocalResultArtifact.uri).where(
            metadb.LocalResultArtifact.uri.in_({
                candidate["artifact_uri"] for candidate in candidates
            })))) == {ref.uri}
    source.unlink()
    assert pq.read_table(ref.uri)["value"].to_pylist() == [1, 2]
    assert metadb.request_durable_task_cancel(task["id"]) is not None
    assert metadb.claim_durable_task(task["id"], "cancel-concurrent") is None
    metadb.delete_canvas_cascade(str(graph.id))
    assert _task_input_ref(task["id"]) is None
    deps.storage.prune_results(limit=20)
    assert metadb.local_file_input_revision_for_artifact(ref.uri) is None
    deps.storage.close()


def test_sqlite_concurrent_first_submit_has_one_task_attempt_and_reference(
        tmp_path, monkeypatch):
    if os.environ.get("DP_TEST_DATABASE_URL"):
        pytest.skip("SQLite contract")
    _exercise_concurrent_ordinary_task_admission(tmp_path, monkeypatch)


@pytest.mark.skipif(
    not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_concurrent_first_submit_has_one_task_attempt_and_reference(
        tmp_path, monkeypatch):
    _exercise_concurrent_ordinary_task_admission(tmp_path, monkeypatch)


def test_running_cancel_retains_input_until_canvas_cleanup(tmp_path, monkeypatch):
    deps, graph, source, uid = _ordinary_task_context(tmp_path)
    status, _dispatched = _start_without_dispatch(
        deps, graph, uid, str(uuid.uuid4()), monkeypatch)
    claimed = metadb.claim_durable_task(status.run_id, "running-owner")
    assert claimed is not None
    attempt = claimed["attempts"][-1]
    assert metadb.request_durable_task_cancel(status.run_id) is not None
    cancelled = RunStatus(
        run_id=status.run_id, status="cancelled", target_node_id="write")
    assert metadb.finish_durable_task_attempt(
        status.run_id, attempt["id"], "running-owner", cancelled.model_dump())
    ref = _task_input_ref(status.run_id)
    assert ref is not None
    assert all(
        uri != ref.uri for uri, _token, _lock in
        metadb.claim_local_result_reclaims(deps.storage.namespace_id, limit=20))

    metadb.delete_canvas_cascade(str(graph.id))
    assert _task_input_ref(status.run_id) is None
    deps.storage.prune_results(limit=20)
    assert metadb.local_file_input_revision_for_artifact(ref.uri) is None
    assert source.exists()
    deps.storage.close()


def test_submission_is_atomic_idempotent_and_projects_into_jobs(task_identity):
    first, created = _submit(task_identity)
    replay, replay_created = _submit(task_identity)

    assert created is True and replay_created is False
    assert replay["id"] == first["id"] and len(replay["attempts"]) == 1
    operational = json.loads(json.dumps(task_identity[4]))
    operational["nodes"][0]["data"]["status"] = "failed"
    adopted, adopted_created = metadb.submit_durable_local_write_task(
        uid=task_identity[0], canvas_id=task_identity[1], submission_id=task_identity[2],
        target_node_id="write", intent_sha256="a" * 64, graph_doc=operational,
        input_manifest=[], write_intent=task_identity[5])
    assert adopted_created is False
    assert adopted["id"] == first["id"]
    assert "status" not in adopted["graph_doc"]["nodes"][0]["data"]
    page = metadb.list_workspace_runs(task_identity[0], run_id=first["id"])
    assert len(page["items"]) == 1
    assert page["items"][0]["taskId"] == first["id"]
    assert page["items"][0]["inputManifest"] == []
    assert page["items"][0]["taskAttempts"][0]["status"] == "queued"

    changed = dict(task_identity[5])
    changed["destination"] = {**changed["destination"], "name": "different"}
    with pytest.raises(RuntimeError, match="frozen admission"):
        metadb.submit_durable_local_write_task(
            uid=task_identity[0], canvas_id=task_identity[1],
            submission_id=task_identity[2], target_node_id="write",
            intent_sha256="b" * 64, graph_doc=task_identity[4],
            input_manifest=[], write_intent=changed)


def test_concurrent_claim_has_one_owner_and_late_owner_is_fenced(task_identity):
    task, _ = _submit(task_identity)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(
            lambda owner: metadb.claim_durable_task(task["id"], owner),
            ("owner-a", "owner-b")))
    winners = [item for item in claimed if item is not None]
    assert len(winners) == 1
    current = winners[0]["attempts"][-1]
    winner = current["owner_token"]
    loser = "owner-b" if winner == "owner-a" else "owner-a"
    assert metadb.heartbeat_durable_task(task["id"], current["id"], loser) is False

    with metadb.session() as session:
        row = session.get(metadb.DurableTaskAttempt, current["id"], with_for_update=True)
        row.lease_until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    expired = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="expired owner")
    assert metadb.heartbeat_durable_task(task["id"], current["id"], winner) is False
    assert metadb.finish_durable_task_attempt(
        task["id"], current["id"], winner, expired.model_dump()) is False
    reclaimed = metadb.claim_durable_task(task["id"], "owner-c")
    assert reclaimed is not None
    assert [item["status"] for item in reclaimed["attempts"]] == ["fenced", "running"]
    stale = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="late owner")
    assert metadb.finish_durable_task_attempt(
        task["id"], current["id"], winner, stale.model_dump()) is False
    assert metadb.durable_task(task["id"])["status"] == "running"


def test_cancel_is_idempotent_and_retry_action_is_bounded(task_identity):
    task, _ = _submit(task_identity)
    claimed = metadb.claim_durable_task(task["id"], "owner")
    attempt = claimed["attempts"][-1]
    assert metadb.request_durable_task_cancel(task["id"])["cancel_requested"] is True
    assert metadb.request_durable_task_cancel(task["id"])["cancel_requested"] is True
    cancelled = RunStatus(
        run_id=task["id"], status="cancelled", target_node_id="write")
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt["id"], "owner", cancelled.model_dump()) is True

    action = str(uuid.uuid4())
    retried = metadb.retry_durable_task(task["id"], action)
    replayed = metadb.retry_durable_task(task["id"], action)
    assert len(retried["attempts"]) == len(replayed["attempts"]) == 2
    second = retried["attempts"][-1]
    claimed = metadb.claim_durable_task(task["id"], "owner-2")
    failed = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="boom")
    assert metadb.finish_durable_task_attempt(
        task["id"], second["id"], "owner-2", failed.model_dump()) is True
    third = metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    assert len(third["attempts"]) == 3
    with pytest.raises(ValueError, match="only a failed"):
        metadb.retry_durable_task(task["id"], str(uuid.uuid4()))


def test_concurrent_retry_same_action_creates_one_bounded_attempt(task_identity):
    task, _ = _submit(task_identity)
    claimed = metadb.claim_durable_task(task["id"], "failed-owner")
    first = claimed["attempts"][-1]
    failed = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="retry me")
    assert metadb.finish_durable_task_attempt(
        task["id"], first["id"], "failed-owner", failed.model_dump())

    action = str(uuid.uuid4())
    barrier = threading.Barrier(8)

    def retry_once(_index):
        barrier.wait()
        return metadb.retry_durable_task(task["id"], action)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(retry_once, range(8)))
    attempt_ids = {result["attempts"][-1]["id"] for result in results}
    assert len(attempt_ids) == 1
    assert {len(result["attempts"]) for result in results} == {2}
    assert len(metadb.durable_task(task["id"])["attempts"]) == 2

    second = metadb.claim_durable_task(task["id"], "second-owner")["attempts"][-1]
    assert metadb.finish_durable_task_attempt(
        task["id"], second["id"], "second-owner", failed.model_dump())
    third = metadb.retry_durable_task(task["id"], str(uuid.uuid4()))
    third_attempt = metadb.claim_durable_task(task["id"], "third-owner")["attempts"][-1]
    assert len(third["attempts"]) == 3
    assert metadb.finish_durable_task_attempt(
        task["id"], third_attempt["id"], "third-owner", failed.model_dump())
    with pytest.raises(ValueError, match="retry limit is exhausted"):
        metadb.retry_durable_task(task["id"], str(uuid.uuid4()))


def _fake_worker_deps():
    return SimpleNamespace(
        resolve_adapter=lambda _uri: None, registry=object(), catalog=object(),
        workspace="/tmp", node_builders={}, node_specs={}, node_ir={}, storage=None,
    )


def test_worker_waits_for_actual_join_before_terminalizing(task_identity, monkeypatch):
    task, _ = _submit(task_identity)
    waits = []

    class FakeLocalRunner:
        def __init__(self, *_args, **_kwargs):
            self.on_status = self.on_complete = None

        def run(self, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
            return RunStatus(
                run_id=run_id, status="failed", target_node_id="write", error="worker failed")

        def wait_for_worker(self, _run_id, timeout=None):
            waits.append((timeout, metadb.durable_task(task["id"])["status"]))
            return len(waits) >= 2

        def cancel(self, _run_id):
            raise AssertionError("owned worker should not be cancelled")

    monkeypatch.setattr(durable_tasks, "LocalRunner", FakeLocalRunner)
    monkeypatch.setattr(
        durable_tasks.compiler, "compile_plan",
        lambda *_args, **_kwargs: SimpleNamespace(acyclic=True, error=None))

    durable_tasks._worker(task["id"], _fake_worker_deps())

    assert waits == [
        (durable_tasks._JOIN_POLL_SECONDS, "running"),
        (durable_tasks._JOIN_POLL_SECONDS, "running"),
    ]
    assert metadb.durable_task(task["id"])["status"] == "failed"


def test_worker_losing_lease_cancels_without_terminalizing(task_identity, monkeypatch):
    task, _ = _submit(task_identity)
    cancellations = []
    finishes = []

    class FakeLocalRunner:
        def __init__(self, *_args, **_kwargs):
            self.on_status = self.on_complete = None

        def run(self, _plan, _graph, _target, _placement, *, run_id, **_kwargs):
            return RunStatus(
                run_id=run_id, status="failed", target_node_id="write", error="late snapshot")

        def wait_for_worker(self, _run_id, timeout=None):
            return False

        def cancel(self, run_id):
            cancellations.append(run_id)

    monkeypatch.setattr(durable_tasks, "LocalRunner", FakeLocalRunner)
    monkeypatch.setattr(
        durable_tasks.compiler, "compile_plan",
        lambda *_args, **_kwargs: SimpleNamespace(acyclic=True, error=None))
    monkeypatch.setattr(metadb, "heartbeat_durable_task", lambda *_args: False)
    monkeypatch.setattr(
        metadb, "finish_durable_task_attempt",
        lambda *args, **kwargs: finishes.append((args, kwargs)))

    durable_tasks._worker(task["id"], _fake_worker_deps())

    assert cancellations == [task["id"]]
    assert finishes == []
    assert metadb.durable_task(task["id"])["status"] == "running"


def test_active_task_blocks_canvas_delete(task_identity):
    task, _ = _submit(task_identity)
    with pytest.raises(metadb.ActiveBackendJobsError, match="active durable task"):
        metadb.delete_canvas_cascade(task_identity[1])
    claimed = metadb.claim_durable_task(task["id"], "owner")
    attempt = claimed["attempts"][-1]
    failed = RunStatus(
        run_id=task["id"], status="failed", target_node_id="write", error="stopped")
    assert metadb.finish_durable_task_attempt(
        task["id"], attempt["id"], "owner", failed.model_dump())
    metadb.delete_canvas_cascade(task_identity[1])
    assert metadb.durable_task(task["id"]) is None


def test_committed_response_loss_restarts_and_reconciles_one_receipt(tmp_path):
    lance = pytest.importorskip("lance")
    uid, canvas_id = f"receipt-user-{uuid.uuid4().hex}", f"receipt-canvas-{uuid.uuid4().hex}"
    submission = str(uuid.uuid4())
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Receipt researcher"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Receipt recovery"))
    source = tmp_path / "source.lance"
    lance.write_dataset(pa.table({"value": [1, 2]}), str(source))
    deps = Deps(str(tmp_path), str(tmp_path / "data"), maintain_storage=False)
    deps.catalog._add(name="source", uri=str(source), strict_probe=True)
    graph = Graph.model_validate({
        "id": canvas_id, "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": str(source)}}},
            {"id": "write", "type": "write", "data": {"title": "durable", "config": {
                "filename": "durable.parquet", "writeMode": "overwrite",
            }}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    admission = _write_admission_for_graph(deps, graph, "write", uid, submission)
    assert admission.intent is not None and admission.intent.mode == "create"
    receipt = write_managed_local_file(
        storage=deps.storage, catalog=deps.catalog, intent=admission.intent,
        write_artifact=lambda uri: pq.write_table(pa.table({"value": [1, 2]}), uri),
    )
    frozen = graph.model_copy(deep=True)
    _inject_write_intent(frozen, "write", admission.intent)
    manifest = _resolve_local_run_manifest(frozen, "write", deps)
    task, _ = metadb.submit_durable_local_write_task(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        target_node_id="write",
        intent_sha256=_local_run_intent_sha256(graph, "write", write_intent=admission.intent),
        graph_doc=frozen.model_dump(by_alias=True, mode="json"),
        input_manifest=manifest,
        write_intent=admission.intent.model_dump(by_alias=True, mode="json"),
    )
    from hub.durable_tasks import dispatch
    dispatch(task["id"], deps)
    deadline = datetime.datetime.now().timestamp() + 10
    while datetime.datetime.now().timestamp() < deadline:
        observed = metadb.durable_task(task["id"])
        if observed["status"] in ("done", "failed", "cancelled"):
            break
        import time
        time.sleep(0.05)
    assert observed["status"] == "done", observed
    assert observed["output_receipt"]["revisionId"] == receipt.revision_id
    assert observed["output_receipt"]["publication"]["artifactUri"] \
        == receipt.publication.artifact_uri
    assert metadb.catalog_managed_local_write_head(
        admission.destination)["revision_id"] == receipt.revision_id
    deps.storage.close()


def test_real_hub_restart_recovers_committed_receipt_into_status_and_jobs(tmp_path):
    pytest.importorskip("lance")
    workspace = tmp_path / "workspace"
    data_dir = workspace / "data"
    workspace.mkdir()
    data_dir.mkdir()
    database_url = f"sqlite:///{workspace / 'metadata.db'}"
    env = {
        **os.environ,
        "DP_DATABASE_URL": database_url,
        "DP_WORKSPACE": str(workspace),
        "DP_DATA_DIR": str(data_dir),
        "DP_EXECUTION": "local-out-of-core",
        "DP_LOG_LEVEL": "warning",
    }
    setup = r'''
import datetime, json, os, uuid
import pyarrow as pa
import pyarrow.parquet as pq
import lance
from hub import metadb
from hub.deps import Deps
from hub.local_writes import write_managed_local_file
from hub.models import Graph
from hub.routers.runs import _inject_write_intent, _resolve_local_run_manifest, _write_admission_for_graph
from hub.routers.runs import _local_run_intent_sha256

metadb.init_db()
workspace, data_dir = os.environ["DP_WORKSPACE"], os.environ["DP_DATA_DIR"]
source = os.path.join(data_dir, "restart-source.lance")
lance.write_dataset(pa.table({"value": [1, 2]}), source)
deps = Deps(workspace, data_dir, maintain_storage=False)
deps.catalog._add(name="restart-source", uri=source, strict_probe=True)
canvas_id, submission = "restart-canvas", str(uuid.uuid4())
graph = Graph.model_validate({
    "id": canvas_id, "version": 1,
    "nodes": [
        {"id": "source", "type": "source", "data": {"config": {"uri": source}}},
        {"id": "write", "type": "write", "data": {"title": "Durable restart", "config": {
            "filename": "restart-result.parquet", "writeMode": "overwrite"}}},
    ],
    "edges": [{"id": "source-write", "source": "source", "target": "write"}],
})
with metadb.session() as session:
    session.add(metadb.Canvas(
        id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Restart acceptance",
        doc=json.dumps(graph.model_dump(by_alias=True, mode="json"))))
admission = _write_admission_for_graph(
    deps, graph, "write", metadb.DEFAULT_USER_ID, submission)
frozen = graph.model_copy(deep=True)
_inject_write_intent(frozen, "write", admission.intent)
manifest = _resolve_local_run_manifest(frozen, "write", deps)
task, _ = metadb.submit_durable_local_write_task(
    uid=metadb.DEFAULT_USER_ID, canvas_id=canvas_id, submission_id=submission,
    target_node_id="write",
    intent_sha256=_local_run_intent_sha256(graph, "write", write_intent=admission.intent),
    graph_doc=frozen.model_dump(by_alias=True, mode="json"),
    input_manifest=manifest,
    write_intent=admission.intent.model_dump(by_alias=True, mode="json"))
claimed = metadb.claim_durable_task(task["id"], "crashed-hub-owner")
receipt = write_managed_local_file(
    storage=deps.storage, catalog=deps.catalog, intent=admission.intent,
    write_artifact=lambda uri: pq.write_table(pa.table({"value": [1, 2]}), uri))
with metadb.session() as session:
    attempt = session.get(metadb.DurableTaskAttempt, claimed["attempts"][-1]["id"])
    lease_deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=6)
    attempt.lease_until = lease_deadline
print(json.dumps({"task_id": task["id"], "revision_id": receipt.revision_id,
                  "artifact_uri": receipt.publication.artifact_uri,
                  "lease_deadline": lease_deadline.timestamp()}))
deps.storage.close()
'''
    prepared = subprocess.run(
        [sys.executable, "-c", setup], env=env, text=True,
        capture_output=True, timeout=30,
    )
    assert prepared.returncode == 0, prepared.stderr
    expected = json.loads(prepared.stdout.strip().splitlines()[-1])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server_log_path = tmp_path / "hub.log"
    server_log = server_log_path.open("w+")
    server = subprocess.Popen(
        [sys.executable, "-m", "hub.cli", "--host", "127.0.0.1", "--port", str(port),
         "--workspace", str(workspace), "--data-dir", str(data_dir), "--no-open", "--no-seed"],
        env=env, text=True, stdout=server_log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}/api"

    def get_json(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=2) as response:
            return json.load(response)

    try:
        ready_deadline = time.monotonic() + 5
        while time.monotonic() < ready_deadline:
            try:
                if get_json("/livez")["ok"]:
                    break
            except Exception:
                time.sleep(0.05)
        else:
            raise AssertionError("restarted Hub did not become ready")
        assert time.time() < expected["lease_deadline"], \
            "test did not start the replacement Hub before the old lease expired"
        before_expiry = get_json(f"/jobs?run_id={expected['task_id']}")["items"][0]
        assert before_expiry["status"] == "running"
        assert [attempt["status"] for attempt in before_expiry["taskAttempts"]] == ["running"]

        deadline = time.monotonic() + 20
        observed = None
        last_error = None
        while time.monotonic() < deadline:
            if server.poll() is not None:
                break
            try:
                observed = get_json(f"/run/{expected['task_id']}")
                if observed["status"] in ("done", "failed", "cancelled"):
                    break
            except Exception as exc:
                last_error = repr(exc)
            time.sleep(0.1)
        server_log.flush()
        assert server.poll() is None, server_log_path.read_text()
        assert observed is not None and observed["status"] == "done", (
            observed, last_error, server_log_path.read_text()[-4000:])
        status_receipt = observed["outputs"][0]["writeReceipt"]
        assert status_receipt["revisionId"] == expected["revision_id"]
        assert status_receipt["publication"]["artifactUri"] == expected["artifact_uri"]

        jobs = get_json(f"/jobs?run_id={expected['task_id']}")
        assert len(jobs["items"]) == 1
        item = jobs["items"][0]
        assert item["status"] == "done"
        assert [attempt["status"] for attempt in item["taskAttempts"]] == ["fenced", "done"]
        assert item["outputReceipt"]["revisionId"] == expected["revision_id"]
        assert item["outputReceipt"]["publication"]["artifactUri"] == expected["artifact_uri"]
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log.close()
