from __future__ import annotations

import json
import os
import shutil
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from hub import handoff, metadb
from hub.models import Graph, RunStatus
from hub.subrun import _parent_attested_source_uris


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    """Keep adversarial lifecycle rows isolated from the rest of the kernel suite."""
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'managed-source-lifecycle.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _source_graph(uri: str) -> Graph:
    return Graph.model_validate({
        "id": "managed-source-contract",
        "version": 1,
        "nodes": [{
            "id": "source",
            "type": "source",
            "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }],
        "edges": [],
    })


def _joined_source_graph(left_uri: str, right_uri: str) -> Graph:
    return Graph.model_validate({
        "id": "managed-source-join",
        "version": 1,
        "nodes": [
            {"id": "left", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": left_uri}}},
            {"id": "right", "type": "source", "position": {"x": 0, "y": 100},
             "data": {"config": {"uri": right_uri}}},
            {"id": "join", "type": "join", "position": {"x": 200, "y": 50},
             "data": {"config": {"on": "value"}}},
        ],
        "edges": [
            {"id": "left-join", "source": "left", "target": "join",
             "targetHandle": "left"},
            {"id": "right-join", "source": "right", "target": "join",
             "targetHandle": "right"},
        ],
    })


def _identity(logical_uri: str, namespace: str, generation: int,
              attempt_id: str, *, kind: str = "region") -> tuple[str, dict]:
    uri = handoff.physical_attempt_uri(
        logical_uri, namespace, generation, attempt_id)
    return uri, {
        "attemptId": attempt_id,
        "generation": generation,
        "storageNamespace": namespace,
        "logicalUri": logical_uri,
        "kind": kind,
    }


def _inventory(uri: str) -> list[dict]:
    parsed = urlsplit(uri)
    root = f"{parsed.netloc}/{parsed.path.lstrip('/').rstrip('/')}"
    members = []
    for key, is_commit in (
            (f"{root}/part-00000.parquet", False),
            (handoff._object_manifest_path(root), True)):
        members.append({
            "member_id": handoff._member_id("unversioned_object", key, "null"),
            "key": key,
            "member_type": "unversioned_object",
            "size": 10,
            "etag": "managed-source-test",
            "version_id": None,
            "upload_id": None,
            "is_latest": True,
            "is_commit": is_commit,
        })
    return members


def _committed_region(logical_uri: str) -> dict:
    token = uuid.uuid4().hex
    handle = metadb.allocate_object_attempt(
        logical_uri=logical_uri,
        kind="region",
        run_id=f"managed-source-{token}",
        allocation_key=f"managed-source:{token}",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
        write_lease_seconds=30,
    )
    metadb.record_object_attempt_commit(handle["uri"], _inventory(handle["uri"]))
    return handle


def _attempt_state(uri: str) -> str:
    with metadb.session() as session:
        return session.get(metadb.ObjectAttempt, uri).state


def test_child_accepts_only_the_exact_parent_attestation():
    logical = "s3://managed-source-contract/data/input.parquet"
    uri, identity = _identity(logical, "installation", 7, "a" * 32)
    job = {
        "target": "source",
        "managedSourceAttempts": {uri: identity},
        "managedLocalSources": {},
    }

    assert _parent_attested_source_uris(job, _source_graph(uri)) == frozenset({uri})


@pytest.mark.parametrize("case,match", [
    ("missing", "contract is missing"),
    ("extra", "does not match the graph"),
    ("malformed", "contract is malformed"),
    ("descendant", "exact attempt root"),
])
def test_child_rejects_unattested_or_aliased_managed_sources(case, match):
    logical = "s3://managed-source-contract/data/input.parquet"
    uri, identity = _identity(logical, "installation", 3, "b" * 32)
    graph_uri = uri
    job = {"target": "source"}

    if case == "extra":
        other_uri, other_identity = _identity(
            "s3://managed-source-contract/data/other.parquet",
            "installation", 4, "c" * 32)
        job["managedSourceAttempts"] = {
            uri: identity,
            other_uri: other_identity,
        }
    elif case == "malformed":
        job["managedSourceAttempts"] = {
            uri: {"logicalUri": logical, "kind": "region"},
        }
    elif case == "descendant":
        graph_uri = f"{uri}/part-00000.parquet"
        job["managedSourceAttempts"] = {uri: identity}

    with pytest.raises(RuntimeError, match=match):
        _parent_attested_source_uris(job, _source_graph(graph_uri))


def test_parent_attestation_atomically_pins_replaced_generation_against_gc(tmp_path):
    from hub.subprocess_runner import SubprocessRunner

    logical = f"s3://managed-source-pin/{uuid.uuid4().hex}/input.parquet"
    old = _committed_region(logical)
    replacement = _committed_region(logical)
    cache_key = f"managed-source-pointer-{uuid.uuid4().hex}"
    metadb.put_result(cache_key, {"uri": old["uri"], "rows": 1})
    assert _attempt_state(old["uri"]) == "published"

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    bundle = runner._claim_source_leases(
        _source_graph(old["uri"]), "source", "attested-reader")
    try:
        attestation = bundle["attempts"][old["uri"]]
        assert attestation == {
            "attemptId": old["attempt_id"],
            "generation": old["generation"],
            "storageNamespace": old["storage_namespace"],
            "logicalUri": logical,
            "kind": "region",
        }
        with metadb.session() as session:
            leases = list(session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == old["uri"],
                metadb.ObjectAttemptLease.lease_type == "read",
            )))
        assert len(leases) == 1

        metadb.put_result(cache_key, {"uri": replacement["uri"], "rows": 1})
        assert _attempt_state(old["uri"]) == "superseded"
        assert old["uri"] not in {
            item["uri"] for item in metadb.object_attempt_gc_batch(0, 0)
        }
    finally:
        bundle["stack"].close()

    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == old["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []
    assert old["uri"] in {
        item["uri"] for item in metadb.object_attempt_gc_batch(0, 0)
    }


@pytest.mark.parametrize("case,match", [
    ("unknown", "no lifecycle ownership row"),
    ("committed", "state=committed"),
    ("superseded", "state=superseded"),
    ("descendant", "exact attempt root"),
])
def test_parent_rejects_unattestable_managed_source_before_dispatch(
        tmp_path, case, match):
    from hub.subprocess_runner import SubprocessRunner

    logical = f"s3://managed-source-reject/{uuid.uuid4().hex}/input.parquet"
    if case == "unknown":
        uri = handoff.physical_attempt_uri(
            logical, metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    else:
        old = _committed_region(logical)
        uri = old["uri"]
        if case in ("superseded", "descendant"):
            replacement = _committed_region(logical)
            cache_key = f"managed-source-reject-{uuid.uuid4().hex}"
            metadb.put_result(cache_key, {"uri": old["uri"]})
            if case == "superseded":
                metadb.put_result(cache_key, {"uri": replacement["uri"]})
            else:
                uri = f"{old['uri']}/part-00000.parquet"

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    with pytest.raises(FileNotFoundError, match=match):
        runner._claim_source_leases(_source_graph(uri), "source", f"reject-{case}")


def test_parent_deduplicates_sources_and_releases_partial_claim_on_failure(tmp_path):
    from hub.subprocess_runner import SubprocessRunner

    logical = f"s3://managed-source-partial/{uuid.uuid4().hex}/input.parquet"
    published = _committed_region(logical)
    cache_key = f"managed-source-partial-{uuid.uuid4().hex}"
    metadb.put_result(cache_key, {"uri": published["uri"]})
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))

    duplicate = runner._claim_source_leases(
        _joined_source_graph(published["uri"], published["uri"]), "join", "duplicate")
    try:
        assert list(duplicate["attempts"]) == [published["uri"]]
        with metadb.session() as session:
            assert session.scalar(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == published["uri"],
                metadb.ObjectAttemptLease.lease_type == "read",
            )) is not None
    finally:
        duplicate["stack"].close()

    unknown = handoff.physical_attempt_uri(
        f"s3://managed-source-partial/{uuid.uuid4().hex}/missing.parquet",
        metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    with pytest.raises(FileNotFoundError, match="no lifecycle ownership row"):
        runner._claim_source_leases(
            _joined_source_graph(published["uri"], unknown), "join", "partial")
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == published["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []


def test_read_lease_thread_start_failure_rolls_back_atomic_claim(monkeypatch):
    logical = f"s3://managed-source-thread/{uuid.uuid4().hex}/input.parquet"
    published = _committed_region(logical)
    metadb.put_result(f"managed-source-thread-{uuid.uuid4().hex}", {
        "uri": published["uri"],
    })

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(handoff.threading, "Thread", BrokenThread)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        with handoff.managed_read_lease(published["uri"], owner="broken-thread"):
            pass
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == published["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []


def test_raw_managed_read_rejects_attempt_member_uri():
    logical = f"s3://managed-source-member/{uuid.uuid4().hex}/input.parquet"
    published = _committed_region(logical)
    metadb.put_result(f"managed-source-member-{uuid.uuid4().hex}", {
        "uri": published["uri"],
    })
    with pytest.raises(FileNotFoundError, match="exact attempt root"):
        with handoff.managed_read_lease(
                f"{published['uri']}/part-00000.parquet", owner="member-reader"):
            pass


@pytest.mark.parametrize("publication", ["managed-sink", "unmanaged-sink", "object-result"])
def test_source_lease_lost_after_child_done_blocks_every_publication(
        tmp_path, monkeypatch, publication):
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    run_id = f"lost-source-{publication}"
    events: list[str] = []
    publication_calls: list[str] = []
    output_uri = (
        "s3://managed-source-loss/results/out.attempt-parent"
        if publication != "unmanaged-sink" else str(tmp_path / "out.csv"))
    output_table = None if publication == "object-result" else "out"
    job_dir = tmp_path / publication
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child",
        status="done",
        placement="local",
        per_node=[],
        output_uri=output_uri,
        output_table=output_table,
    ).model_dump()))

    class Guard:
        checks = 0

        def check(self):
            self.checks += 1
            if self.checks == 2:
                raise FileNotFoundError("SECRET_SOURCE_LEASE_SENTINEL")

    guard = Guard()

    class Stack:
        closed = False

        def close(self):
            assert events == ["reaped"]
            self.closed = True
            events.append("released")

    stack = Stack()

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            events.append("reaped")
            return 0

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            publication_calls.append("register")
            return {"uri": output_uri, "name": "out", "version": "v1"}

        @staticmethod
        def get_table(_uri):
            publication_calls.append("readback")
            return {"uri": output_uri, "name": "out", "version": "v1"}

    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=Catalog())
    process = FinishedProcess()
    runner.runs[run_id] = RunStatus(
        run_id=run_id, status="running", placement="local", per_node=[])
    runner._procs[run_id] = process
    runner._source_leases[run_id] = {
        "stack": stack,
        "guards": [guard],
        "attempts": {"source": {}},
    }

    if publication == "managed-sink":
        runner._object_sinks[run_id] = {"write": {
            "uri": output_uri,
            "logical_uri": "s3://managed-source-loss/results/out.parquet",
            "name": "out",
            "parents": [],
        }}
        monkeypatch.setattr(
            runner, "_publish_object_sinks",
            lambda *_args: publication_calls.append("managed-publish"))
        monkeypatch.setattr(
            runner, "_discard_object_sinks",
            lambda _sinks: publication_calls.append("managed-discard"))
    elif publication == "unmanaged-sink":
        runner._sink_contracts[run_id] = {"write": {
            "logical_uri": output_uri,
            "published_uri": output_uri,
            "name": "out",
            "parents": [],
        }}
    else:
        runner._object_results[run_id] = {
            "uri": output_uri,
            "cache_key": None,
            "run_state_owner": True,
        }
        monkeypatch.setattr(
            subprocess_runner, "_safe_abandon_attempt",
            lambda *_args, **_kwargs: publication_calls.append("result-abandon"))

    monkeypatch.setattr(
        handoff, "prepare_attempt_commit",
        lambda *_args, **_kwargs: publication_calls.append("prepare"))
    runner._watch(
        run_id, process, str(status_file), str(job_dir),
        Graph.model_validate({
            "id": "lost-source", "version": 1, "nodes": [], "edges": [],
        }), None)

    final = runner.status(run_id)
    assert final.status == "failed"
    assert final.error == "managed source lease was lost during execution"
    assert "SECRET_SOURCE_LEASE_SENTINEL" not in final.error
    assert final.output_uri is None and final.output_table is None
    assert guard.checks == 2
    assert stack.closed and events == ["reaped", "released"]
    assert "prepare" not in publication_calls
    assert "managed-publish" not in publication_calls
    assert "register" not in publication_calls and "readback" not in publication_calls
    if publication == "managed-sink":
        assert publication_calls == ["managed-discard"]
    elif publication == "object-result":
        assert publication_calls == ["result-abandon"]
    else:
        assert publication_calls == []


def test_unreaped_supervisor_retains_managed_source_bundle(tmp_path, monkeypatch):
    from hub.subprocess_runner import SubprocessRunner

    run_id = "unreaped-managed-source"
    job_dir = tmp_path / "unreaped-job"
    job_dir.mkdir()

    class Process:
        returncode = None

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

        @staticmethod
        def wait(timeout=None):
            raise OSError("reap proof unavailable")

    class Stack:
        closed = False

        def close(self):
            self.closed = True

    stack = Stack()
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    process = Process()
    runner.runs[run_id] = RunStatus(
        run_id=run_id, status="running", placement="local", per_node=[])
    runner._procs[run_id] = process
    runner._cancel_files[run_id] = str(job_dir / "cancel.requested")
    runner._source_leases[run_id] = {
        "stack": stack,
        "guards": [object()],
        "attempts": {"s3://source.attempt-parent": {}},
    }
    retries = []
    monkeypatch.setattr(
        runner, "_watch_inner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("watcher bug")))
    monkeypatch.setattr(
        runner, "_schedule_watch_retry",
        lambda *_args, **_kwargs: retries.append("retry"))

    try:
        runner._watch(
            run_id, process, str(job_dir / "status.json"), str(job_dir),
            Graph.model_validate({
                "id": "unreaped", "version": 1, "nodes": [], "edges": [],
            }), None)

        assert runner.status(run_id).status == "running"
        assert runner.status(run_id).stalled is True
        assert retries == ["retry"]
        assert runner._procs[run_id] is process
        assert runner._source_leases[run_id]["stack"] is stack
        assert stack.closed is False
        assert job_dir.is_dir()
    finally:
        runner._procs.clear()
        runner._cancel_files.clear()
        runner._release_source_leases(run_id)
        runner.runs.clear()
        shutil.rmtree(job_dir, ignore_errors=True)
