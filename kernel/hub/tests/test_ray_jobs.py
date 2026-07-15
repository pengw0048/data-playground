"""Deterministic Ray Jobs lifecycle tests; no live Ray cluster required."""

from __future__ import annotations

import importlib.util
import datetime
import json
import os
import shlex
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from hub import metadb
from hub.compiler import compile_plan
from hub.deps import Deps
from hub.models import CatalogPublicationReceipt, Graph, GraphNode, ResourceSpec, RunStatus

_RAY_JOBS_BACKEND = "ray-jobs"


def _load_dp_ray():
    source = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_ray" / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"dp_ray_jobs_test_{uuid.uuid4().hex}", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryArtifacts:
    def __init__(self):
        self.values: dict[str, dict] = {}
        self.lock = threading.Lock()

    def write(self, uri: str, value: dict) -> None:
        with self.lock:
            self.values[uri] = dict(value)

    def read(self, uri: str) -> dict:
        with self.lock:
            if uri not in self.values:
                raise FileNotFoundError(uri)
            return dict(self.values[uri])


class FlakyResultArtifacts(MemoryArtifacts):
    def __init__(self):
        super().__init__()
        self.fail_result_reads = False

    def read(self, uri: str) -> dict:
        if self.fail_result_reads and uri.endswith(".dpresult"):
            raise ConnectionError("object store temporarily unavailable")
        return super().read(uri)


class FlakyJobArtifacts(MemoryArtifacts):
    def __init__(self):
        super().__init__()
        self.fail_job_reads = False
        self.fail_job_writes = 0

    def read(self, uri: str) -> dict:
        if self.fail_job_reads and uri.endswith(".dpjob"):
            raise ConnectionError("job object store temporarily unavailable")
        return super().read(uri)

    def write(self, uri: str, value: dict) -> None:
        if self.fail_job_writes and uri.endswith(".dpjob"):
            self.fail_job_writes -= 1
            raise ConnectionError("job object store write interrupted")
        super().write(uri, value)


class CountedFlakyArtifacts(MemoryArtifacts):
    """Fails a fixed number of job and result reads, so a specific outage window can be reproduced."""
    def __init__(self):
        super().__init__()
        self.job_read_failures = 0
        self.result_read_failures = 0

    def read(self, uri: str) -> dict:
        if uri.endswith(".dpjob") and self.job_read_failures > 0:
            self.job_read_failures -= 1
            raise ConnectionError("job object store transiently unavailable")
        if uri.endswith(".dpresult") and self.result_read_failures > 0:
            self.result_read_failures -= 1
            raise ConnectionError("result object store transiently unavailable")
        return super().read(uri)


class FakeJobsClient:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.submit_calls: list[dict] = []
        self.stop_calls: list[str] = []
        self.status_calls: list[str] = []
        self.log_calls: list[str] = []
        self.events: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, _address: str):
        return self

    def get_job_status(self, submission_id: str):
        with self.lock:
            self.status_calls.append(submission_id)
            self.events.append("status")
            if submission_id not in self.jobs:
                raise RuntimeError(f"job {submission_id} does not exist")
            return self.jobs[submission_id]["status"]

    def list_jobs(self):
        with self.lock:
            self.events.append("list")
            return {job_id: dict(info) for job_id, info in self.jobs.items()}

    def submit_job(self, **kwargs):
        submission_id = kwargs["submission_id"]
        with self.lock:
            self.events.append("submit")
            self.submit_calls.append(kwargs)
            if submission_id in self.jobs:
                raise RuntimeError("submission id already exists")
            self.jobs[submission_id] = {
                "status": "RUNNING", "message": None, "logs": "",
                "metadata": dict(kwargs.get("metadata") or {}),
            }
        return submission_id

    def stop_job(self, submission_id: str):
        with self.lock:
            self.stop_calls.append(submission_id)
            self.jobs[submission_id]["status"] = "STOPPED"
        return True

    def get_job_info(self, submission_id: str):
        with self.lock:
            return dict(self.jobs[submission_id])

    def get_job_logs(self, submission_id: str):
        with self.lock:
            self.log_calls.append(submission_id)
            return self.jobs[submission_id].get("logs", "")

    def put(self, submission_id: str, status: str, message: str | None = None, logs: str = "",
            metadata: dict[str, str] | None = None) -> None:
        with self.lock:
            self.jobs[submission_id] = {
                "status": status, "message": message, "logs": logs,
                "metadata": dict(metadata or {}),
            }

    def set_status(self, submission_id: str, status: str) -> None:
        with self.lock:
            self.jobs[submission_id]["status"] = status


class CountingCatalog:
    def __init__(self):
        self.calls: list[dict] = []
        self.keys: set[str] = set()
        self.managed_uris: set[str] = set()
        self.usage_calls: list[dict] = []
        self.usage_keys: set[str] = set()
        self.lock = threading.Lock()

    @staticmethod
    def resolve_ref(ref: str) -> str:
        return ref

    def register_output(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)

    def publish_managed_output(self, **kwargs):
        """Lifecycle persistence is covered separately; keep Jobs publication idempotent here."""
        with self.lock:
            if kwargs["uri"] not in self.managed_uris:
                self.managed_uris.add(kwargs["uri"])
                self.calls.append(kwargs)
        with metadb.session() as session:
            attempt = session.get(metadb.ObjectAttempt, kwargs["uri"])
            assert attempt is not None
            if attempt.state == "writing":
                attempt.state = "published"
                attempt.published_at = metadb._now()
        return {"uri": kwargs["uri"]}

    def register_output_idempotent(self, idempotency_key: str, **kwargs):
        with self.lock:
            if idempotency_key in self.keys:
                return CatalogPublicationReceipt(
                    idempotency_key=idempotency_key, uri=kwargs["uri"], version=kwargs.get("version")
                )
            self.keys.add(idempotency_key)
            self.calls.append({"idempotency_key": idempotency_key, **kwargs})
            metadb.catalog_upsert_entry(kwargs["uri"], kwargs["name"], {
                "id": kwargs["name"], "name": kwargs["name"], "uri": kwargs["uri"],
                "version": kwargs.get("version"), "columns": [], "tags": [],
            })
            metadb.catalog_record_output_publication(
                idempotency_key, kwargs["uri"], kwargs.get("version")
            )
            return CatalogPublicationReceipt(
                idempotency_key=idempotency_key, uri=kwargs["uri"], version=kwargs.get("version")
            )

    def record_usage_idempotent(self, idempotency_key: str, parents: list[str]):
        with self.lock:
            if idempotency_key in self.usage_keys:
                return False
            self.usage_keys.add(idempotency_key)
            self.usage_calls.append({"idempotency_key": idempotency_key, "parents": parents})
            return True

    @staticmethod
    def unregister(uri: str) -> bool:
        metadb.catalog_delete_entry(uri)
        return True


class RecoveringCatalog(CountingCatalog):
    def __init__(self):
        super().__init__()
        self.available = False

    def register_output_idempotent(self, idempotency_key: str, **kwargs):
        if not self.available:
            raise ConnectionError("catalog temporarily unavailable")
        return super().register_output_idempotent(idempotency_key, **kwargs)

    def publish_managed_output(self, **kwargs):
        if not self.available:
            raise ConnectionError("catalog temporarily unavailable")
        return super().publish_managed_output(**kwargs)


class PersistingManagedCatalog(CountingCatalog):
    """Provider-free fake that still exercises the durable managed catalog transaction."""

    def publish_managed_output(self, **kwargs):
        uri = kwargs["uri"]
        with metadb.session() as session:
            attempt = session.get(metadb.ObjectAttempt, uri)
            assert attempt is not None
            if attempt.state == "writing":
                attempt.state = "committed"
        metadb.catalog_upsert_entry(
            uri, kwargs["name"], {
                "id": kwargs["name"], "name": kwargs["name"], "uri": uri,
                "version": kwargs.get("version"), "columns": [], "tags": [],
            },
            parents=kwargs.get("parents"), pipeline=kwargs.get("pipeline"),
        )
        with self.lock:
            if uri not in self.managed_uris:
                self.managed_uris.add(uri)
                self.calls.append(kwargs)
        return {"uri": uri}


@pytest.fixture
def jobs_config(monkeypatch, tmp_path):
    from hub import handoff

    metadb.init_db()  # standalone module execution does not import test_kernel's app bootstrap
    # These lifecycle tests use fake S3 URIs and no provider. Allocation/identity still exercise the SQL
    # registry; exact provider ownership and core publication are covered by test_object_lifecycle.
    monkeypatch.setattr(handoff, "ensure_storage_namespace_claim", lambda *_args, **_kwargs: None)

    def commit_fake_sink(uri: str) -> None:
        with metadb.session() as session:
            attempt = session.get(metadb.ObjectAttempt, str(uri).rstrip("/"))
            if attempt is None or attempt.kind != "sink":
                raise RuntimeError("test sink has no lifecycle identity")
            if attempt.state == "writing":
                attempt.state = "committed"

    monkeypatch.setattr(handoff, "prepare_attempt_commit", commit_fake_sink)
    monkeypatch.setenv("DP_RAY_JOBS_ADDRESS", "http://ray-head:8265")
    monkeypatch.setenv("DP_RAY_JOBS_ENTRYPOINT", "python /opt/dataplay/dp_ray/_driver.py")
    monkeypatch.setenv("DP_RAY_JOBS_CODE_REF", "sha256:dp-ray-test-image")
    monkeypatch.setenv("DP_RAY_JOBS_CLUSTER_REF", "test-ray-cluster")
    monkeypatch.setenv("DP_RAY_JOBS_WORKSPACE", "/opt/dataplay/kernel")
    monkeypatch.setenv("DP_RAY_JOBS_DATA_DIR", "/opt/dataplay/kernel/data")
    monkeypatch.setenv("DP_STORAGE_URL", "s3://shared/outputs")
    monkeypatch.setenv("DP_RAY_JOBS_ARTIFACT_PREFIX", "s3://shared/control/ray-jobs")
    monkeypatch.setenv("DP_RAY_JOBS_POLL_S", "0.01")
    monkeypatch.setenv("DP_RAY_JOBS_CANCEL_TIMEOUT_S", "0.2")
    monkeypatch.setenv("DP_RAY_JOBS_RESULT_TIMEOUT_S", "0.2")
    monkeypatch.setenv("DP_RAY_JOBS_SUBMISSION_LEASE_S", "5")
    monkeypatch.setenv("DP_RAY_JOBS_PUBLICATION_LEASE_S", "5")
    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    _scrub_leaked_ray_jobs()
    yield workspace, data
    _scrub_leaked_ray_jobs()


def _graph(name: str = "jobs_out", source: str = "s3://shared/input.parquet") -> Graph:
    return Graph.model_validate({
        "id": f"canvas_{uuid.uuid4().hex}",
        "version": 1,
        "nodes": [
            {"id": "src", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": source}}},
            {"id": "map", "type": "transform", "position": {"x": 0, "y": 0},
             "data": {"config": {"mode": "map", "code": "def fn(row): return row"}}},
            {"id": "write", "type": "write", "position": {"x": 0, "y": 0},
             "data": {"config": {"filename": f"{name}.parquet", "writeMode": "overwrite"}}},
        ],
        "edges": [
            {"id": "a", "source": "src", "target": "map", "data": {"wire": "dataset"}},
            {"id": "b", "source": "map", "target": "write", "data": {"wire": "dataset"}},
        ],
    })


def _runner(jobs_config, client=None, store=None, recover=False, catalog=None):
    workspace, data = jobs_config
    deps = Deps(str(workspace), str(data))
    if catalog is not None:
        deps.catalog = catalog
    else:
        class FakeOutputAdapter:
            @staticmethod
            def schema(_uri):
                return []

            @staticmethod
            def count(_uri):
                return 3

            @staticmethod
            def fingerprint(_uri):
                return "ray-jobs-test-output"

        # Keep exact core catalog authority while replacing only the fake-S3 data probe in this unit
        # harness. Object lifecycle, event receipts, lineage, and pointers still use real metadata SQL.
        deps.catalog.resolve = lambda _uri: FakeOutputAdapter()
    module = _load_dp_ray()
    client = client or FakeJobsClient()
    store = store or MemoryArtifacts()

    class TestRayRunner(module.RayRunner):
        """Direct unit calls stand in for the API, which normally prebinds the authorized principal."""
        def run(self, plan, graph, target_node_id, placement, run_id=None):
            run_id = run_id or self.preallocate_run_id()
            with metadb.session() as session:
                if session.get(metadb.Canvas, graph.id) is None:
                    session.add(metadb.Canvas(
                        id=graph.id, owner_id=metadb.DEFAULT_USER_ID,
                        name="Ray Jobs test", version=graph.version, doc="{}",
                    ))
            token = None
            if metadb.run_auth(run_id) == (None, None):
                token = metadb.preallocate_run_owner(
                    run_id, metadb.DEFAULT_USER_ID, None
                )
            try:
                status = super().run(
                    plan, graph, target_node_id, placement, run_id=run_id
                )
            except Exception:
                if token is not None:
                    metadb.discard_run_preallocation(
                        run_id, token, metadb.DEFAULT_USER_ID, None
                    )
                raise
            if token is not None:
                assert metadb.finish_run_preallocation(
                    run_id, token, status.model_dump()
                )
            return status

        def _make_jobs_artifacts(self, run_id, *args, **kwargs):
            graph = args[0] if args else None
            if graph is not None and getattr(graph, "id", None):
                with metadb.session() as session:
                    if session.get(metadb.Canvas, graph.id) is None:
                        session.add(metadb.Canvas(
                            id=graph.id, owner_id=metadb.DEFAULT_USER_ID,
                            name="Ray Jobs test", version=graph.version, doc="{}",
                        ))
            if metadb.run_auth(run_id) == (None, None):
                metadb.preallocate_run_owner(run_id, metadb.DEFAULT_USER_ID, None)
            return super()._make_jobs_artifacts(run_id, *args, **kwargs)

        def _source_unsupported_reason(self, *_args):
            # Lifecycle tests use a fake S3 URI and never execute data. #77's real source preflight is
            # covered by test_ray_compat; bypass only the unavailable object listing in this fake harness.
            return None

    runner = TestRayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=recover
    )
    return module, deps, runner, client, store


def _wait(runner, run_id: str, terminal=True, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status(run_id)
        if (status.status in ("done", "failed", "cancelled")) == terminal:
            return status
        time.sleep(0.01)
    raise AssertionError(f"run did not reach expected state: {runner.status(run_id)}")


def _wait_submitted(client: FakeJobsClient, submission_id: str, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with client.lock:
            if submission_id in client.jobs:
                return
        time.sleep(0.01)
    raise AssertionError(f"job {submission_id} was not submitted")


def _workload_metadata(status) -> dict[str, str]:
    ref = status.backend_ref
    assert ref is not None
    return {
        "dataplay_run_id": status.run_id,
        "dataplay_attempt_id": ref.attempt_id,
        "dataplay_code_ref": ref.code_ref,
    }


def _retire_inert_test_run(status) -> None:
    """Keep disabled-supervisor fixtures from leaking active SQL rows into later recovery tests."""
    run_id = status.run_id if hasattr(status, "run_id") else status["run_id"]
    doc = status.model_dump() if hasattr(status, "model_dump") else dict(status)
    doc.update(run_id=run_id, status="failed", error="test fixture retired without a live supervisor")
    metadb.save_run_state(run_id, doc)


def _scrub_leaked_ray_jobs() -> None:
    """Terminalize reattachable Ray Jobs rows so recover=True cannot pick up earlier tests."""
    for ref, doc in list(metadb.active_backend_jobs(_RAY_JOBS_BACKEND)):
        run_id = str(ref.get("run_id") or doc.get("run_id") or "")
        if not run_id:
            continue
        payload = dict(doc)
        payload.setdefault("run_id", run_id)
        payload.setdefault("placement", "distributed")
        payload.setdefault("per_node", [])
        _retire_inert_test_run(payload)


def _wait_publication_state(run_id: str, state: str, *, timeout: float = 10.0,
                            runner=None) -> dict:
    deadline = time.monotonic() + timeout
    binding = metadb.backend_job(run_id)
    while binding.get("publication_state") != state and time.monotonic() < deadline:
        if runner is not None:
            runner.status(run_id)
        time.sleep(0.01)
        binding = metadb.backend_job(run_id)
    assert binding.get("publication_state") == state
    return binding


def _wait_run_state_updated_at_stable(run_id: str, *, stable_s: float = 0.12,
                                      timeout: float = 2.0) -> datetime.datetime:
    """Return an updated_at that stayed unchanged for stable_s (same-state poll quiescence)."""
    deadline = time.monotonic() + timeout
    last_at: datetime.datetime | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        with metadb.session() as session:
            current = session.get(metadb.RunState, run_id).updated_at
        if last_at is not None and current == last_at:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_s:
                return current
        else:
            stable_since = None
        last_at = current
        time.sleep(0.01)
    raise AssertionError(f"RunState.updated_at for {run_id} did not stabilize within {timeout}s")


def _wait_control_calls_stable(client: FakeJobsClient, expected: tuple[int, int, int],
                               *, timeout: float = 2.0, stable_s: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        current = (
            len(client.status_calls), len(client.submit_calls), len(client.stop_calls)
        )
        if current == expected:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_s:
                return
        else:
            stable_since = None
        time.sleep(0.01)
    current = (len(client.status_calls), len(client.submit_calls), len(client.stop_calls))
    raise AssertionError(f"control calls did not stabilize at {expected}: last={current}")


def _publish_test_backend_done(status) -> None:
    ref = status.backend_ref
    assert ref is not None
    payload = metadb.backend_job_artifact_payload(status.run_id)
    assert payload is not None
    job = json.loads(payload)
    assert job["sink_contracts"] == {} and job["materialize_uri"] is None
    owner = f"test-terminal-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        status.run_id, ref.attempt_id, owner, 30
    ) == "claimed"
    terminal = status.model_copy(deep=True)
    terminal.status, terminal.progress, terminal.error = "done", 1.0, None
    terminal.rows_processed = terminal.total_rows = 0
    terminal.output_uri = terminal.output_table = None
    for node in terminal.per_node:
        node.status = "done"
        node.error = None
    validated_result = {
        "contract_version": job["contract_version"],
        "attempt_id": ref.attempt_id,
        "submission_id": ref.submission_id,
        "envelope_sha256": job["envelope_sha256"],
        "status": "done",
        "rows": 0,
        "error": None,
        "output_uri": None,
        "output_table": None,
        "outputs": [],
    }
    assert metadb.begin_backend_publication_effects(
        status.run_id, ref.attempt_id, owner, terminal.model_dump(),
        validated_result, {}, [], None,
    ) == "started"
    assert metadb.finish_backend_publication(
        status.run_id, ref.attempt_id, owner, terminal.model_dump()
    ) is True


def _stage_raw_test_backend_failure(
        run_id: str, ref: dict, owner: str, status_doc: dict, **updates) -> dict:
    """Stage a terminal for raw metadata tests that intentionally have no job/sink artifacts."""
    terminal = RunStatus.model_validate({
        **status_doc,
        "status": "failed",
        "error": "test terminal failure",
        "backend_ref": {
            "backend": ref["backend"],
            "cluster_ref": ref["cluster_ref"],
            "submission_id": ref["submission_id"],
            "attempt_id": ref["attempt_id"],
            "job_uri": ref["job_uri"],
            "result_uri": ref["result_uri"],
            "code_ref": ref["code_ref"],
            "durable": True,
        },
        **updates,
    }).model_dump()
    assert metadb.begin_backend_publication_effects(
        run_id, ref["attempt_id"], owner, terminal,
        None, {}, [], None,
    ) == "started"
    return terminal


def _preallocate_backend_test_run(run_id: str) -> str:
    """Mirror the router lease required before a backend binding can own durable effects."""
    return metadb.preallocate_run_owner(run_id, metadb.DEFAULT_USER_ID, None)


def _materialize_bound_job(runner, status) -> dict:
    """Stand in for the disabled supervisor in tests that exercise lower-level submission fences."""
    assert status.backend_ref is not None
    return runner._read_or_materialize_job_artifact(status.backend_ref, status)


def _published_source_attempt(module) -> dict:
    logical_uri = f"s3://shared/sources/source-{uuid.uuid4().hex}.parquet"
    source_run = f"source-run-{uuid.uuid4().hex}"
    scope = "source"
    handle = module.allocate_attempt(
        logical_uri=logical_uri, kind="region", run_id=source_run,
        allocation_key=module._attempt_allocation_key(
            logical_uri, source_run, "region", scope
        ),
        uri_factory=lambda namespace, generation, attempt_id: module._attempt_handoff_uri(
            logical_uri, source_run, scope=scope, namespace=namespace,
            generation=generation, attempt_id=attempt_id,
        ),
    )
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, handle["uri"])
        assert attempt is not None and attempt.state == "writing"
        attempt.state = "published"
        attempt.published_at = metadb._now()
        for lease in session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == handle["uri"])):
            session.delete(lease)
    return handle


def _write_success(store: MemoryArtifacts, status, rows=3, output_uri: str | None = None,
                   output_name: str = "jobs_out"):
    ref = status.backend_ref
    assert ref is not None
    try:
        job = store.read(ref.job_uri)
    except FileNotFoundError:
        payload = metadb.backend_job_artifact_payload(status.run_id)
        assert payload is not None
        job = json.loads(payload)
        store.write(ref.job_uri, job)
    contract = job["sink_contracts"]["write"]
    logical_uri = contract["logical_uri"]
    output_uri = output_uri or contract["physical_uri"]
    store.write(ref.result_uri, {
        "contract_version": job["contract_version"],
        "attempt_id": ref.attempt_id,
        "submission_id": ref.submission_id,
        "envelope_sha256": job["envelope_sha256"],
        "status": "done",
        "rows": rows,
        "error": None,
        "output_uri": output_uri,
        "output_table": output_name,
        "outputs": [{"step_id": "write", "name": output_name, "uri": output_uri,
                     "logical_uri": logical_uri}],
    })


def _complete(store: MemoryArtifacts, client: FakeJobsClient, status, rows=3,
              output_uri: str | None = None, output_name: str = "jobs_out"):
    ref = status.backend_ref
    assert ref is not None
    _write_success(store, status, rows, output_uri=output_uri, output_name=output_name)
    client.set_status(ref.submission_id, "SUCCEEDED")


def test_ray_jobs_submit_is_deterministic_and_excludes_metadata_secrets(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_submit_{uuid.uuid4().hex}"

    first = runner.run(plan, graph, "write", "distributed", run_id=run_id)
    ref = first.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    # ``run`` durably binds the job and hands materialization/submission to an asynchronous supervisor.
    # Observing the submit is the synchronization barrier: the supervisor must write the job artifact
    # before constructing the submit request, while an immediate read after ``run`` races that handoff.
    assert ref.attempt_id == module._job_attempt_id(store.read(ref.job_uri))
    assert len(client.submit_calls) == 1
    submit = client.submit_calls[0]
    entrypoint_args = shlex.split(submit["entrypoint"])[-4:]
    assert entrypoint_args == [ref.job_uri, ref.attempt_id, ref.submission_id,
                               store.read(ref.job_uri)["envelope_sha256"]]
    env = submit["runtime_env"]["env_vars"]
    assert "DP_DATABASE_URL" not in env and "DP_AUTH_SECRET" not in env
    assert "PATH" not in env and "PYTHONPATH" not in env  # remote image owns its interpreter/code paths
    assert env["DP_CANVAS_PIP_DEPS"] == "0"
    assert env["DP_RAY_JOB_MODE"] == "1"
    job = store.read(ref.job_uri)
    assert "created_by" not in job and "auth_canvas_id" not in job
    assert "dataplay_created_by" not in submit["metadata"]
    assert "dataplay_auth_canvas_id" not in submit["metadata"]
    persisted = metadb.get_run_state(run_id)
    assert persisted and persisted["backend_ref"]["submission_id"] == ref.submission_id

    _complete(store, client, first)
    final = _wait(runner, run_id)
    assert final.status == "done" and final.total_rows == 3
    assert metadb.catalog_get(final.output_uri) is not None


def test_ray_jobs_worker_direct_sink_publishes_attempt_uri(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_direct_sink_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    job = store.read(ref.job_uri)
    logical_uri = job["sink_targets"]["write"]
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    assert physical_uri != logical_uri
    handle = module.lookup_attempt(
        logical_uri=logical_uri, kind="sink", run_id=status.run_id,
        allocation_key=module._attempt_allocation_key(
            logical_uri, status.run_id, "sink", "write"
        ),
    )
    assert handle is not None and handle["uri"] == physical_uri
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, physical_uri)
        assert attempt is not None and attempt.state == "writing"

    _write_success(store, status, output_uri=logical_uri)
    with pytest.raises(module.ArtifactContractError, match="hash-bound job sinks"):
        runner._validate_job_result(job, store.read(ref.result_uri))

    _complete(store, client, status, output_uri=physical_uri)
    final = _wait(runner, status.run_id)
    assert final.status == "done" and final.output_uri == physical_uri
    assert metadb.catalog_get(physical_uri)["uri"] == physical_uri


def test_ray_jobs_pins_exact_managed_source_generation_until_terminal(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    source = _published_source_attempt(module)
    graph = _graph(source=source["uri"])
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)

    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_source_pin_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    job = store.read(ref.job_uri)

    assert job["source_attempts"] == [source["uri"]]
    assert metadb.backend_source_pins(status.run_id) == [{
        "uri": source["uri"], "generation": source["generation"],
    }]
    runner._validate_jobs_source_pins(job)

    _complete(store, client, status)
    assert _wait(runner, status.run_id).status == "done"
    assert metadb.backend_source_pins(status.run_id) == []
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, source["uri"]).state == "superseded"


def test_ray_jobs_source_pin_mismatch_quarantines_without_submit(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    source = _published_source_attempt(module)
    graph = _graph(source=source["uri"])
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_source_pin_mismatch_{uuid.uuid4().hex}",
    )
    job = _materialize_bound_job(original, status)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    with metadb.session() as session:
        refs = list(session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.ref_type == "backend_source",
            metadb.ObjectAttemptRef.ref_key.startswith(f"{status.run_id}:", autoescape=True),
        )))
        assert len(refs) == 1
        session.delete(refs[0])

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    final = _wait(recovered, status.run_id)

    assert final.status == "failed"
    assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    assert client.submit_calls == [] and client.stop_calls == []
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, physical_uri).state == "abandoned"


def test_ray_jobs_rejects_multiple_sinks_before_object_attempt_allocation(
        jobs_config, monkeypatch):
    module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    allocation_calls = []
    monkeypatch.setattr(
        module, "allocate_attempt",
        lambda **kwargs: allocation_calls.append(kwargs) or {"uri": "unexpected"},
    )
    monkeypatch.setattr(runner, "_resolve_sink_targets", lambda _ir: {
        "write": "s3://shared/outputs/one.parquet",
        "second": "s3://shared/outputs/two.parquet",
    })

    with pytest.raises(RuntimeError, match="multiple Ray write sinks"):
        runner.run(
            plan, graph, "write", "distributed",
            run_id=f"run_jobs_two_sinks_{uuid.uuid4().hex}",
        )

    assert allocation_calls == []


def test_ray_jobs_prebind_failure_atomically_discards_allocated_attempt(
        jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_prebind_failure_{uuid.uuid4().hex}"

    def _unavailable(_address):
        raise ConnectionError("Ray Jobs endpoint unavailable")

    runner._jobs_client_factory = _unavailable
    with pytest.raises(ConnectionError, match="endpoint unavailable"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    assert metadb.backend_job(run_id) is None
    with metadb.session() as session:
        attempts = list(session.scalars(select(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == run_id
        )))
        assert len(attempts) == 1 and attempts[0].state == "abandoned"


def test_ray_jobs_expired_preallocation_cannot_create_sink_attempt(
        jobs_config, monkeypatch):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_expired_preallocation_{uuid.uuid4().hex}"
    collect_sources = runner._jobs_source_attempts

    def _expire_before_allocation(bound_graph, target):
        sources = collect_sources(bound_graph, target)
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            assert state is not None and state.preallocation_token is not None
            state.preallocation_expires_at = (
                metadb._db_now(session) - datetime.timedelta(seconds=1)
            )
        return sources

    monkeypatch.setattr(runner, "_jobs_source_attempts", _expire_before_allocation)
    with pytest.raises(RuntimeError, match="live unbound run preallocation"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    assert metadb.backend_job(run_id) is None
    assert metadb.get_run_state(run_id) is None
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == run_id
        ))) == []
        assert session.get(metadb.RunTerminalFence, run_id).status == "failed"


def test_ray_jobs_postbind_failure_retains_attempt_for_recovery(
        jobs_config, monkeypatch):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_postbind_failure_{uuid.uuid4().hex}"

    monkeypatch.setattr(
        runner, "_install_jobs_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("crash after durable bind")
        ),
    )
    with pytest.raises(RuntimeError, match="after durable bind"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    binding = metadb.backend_job(run_id)
    assert binding is not None
    with metadb.session() as session:
        attempt = session.scalar(select(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == run_id
        ))
        assert attempt is not None and attempt.state == "writing"
        physical_uri = attempt.uri

    # Retire the deliberately supervisor-less fixture without weakening the assertion above.
    assert metadb.mark_object_attempt_terminal(physical_uri)
    assert metadb.claim_backend_publication(
        run_id, binding["attempt_id"], "test-retire", 10
    ) == "claimed"
    terminal = metadb.get_run_state(run_id)
    terminal.update(status="failed", error="test fixture retired")
    assert metadb.begin_backend_publication_effects(
        run_id, binding["attempt_id"], "test-retire", terminal,
        None, {}, catalog_effects=[], usage_effect=None,
    ) == "started"
    assert metadb.finish_backend_publication(
        run_id, binding["attempt_id"], "test-retire", terminal
    )


def test_ray_jobs_preallocated_run_id_uses_full_uuid_entropy(jobs_config):
    _module, _deps, runner, _client, _store = _runner(jobs_config)

    run_id = runner.preallocate_run_id()

    prefix, value = run_id.split("_", 1)
    assert prefix == "run" and len(value) == 32 and int(value, 16) >= 0


def test_v3_result_validation_uses_frozen_sink_contract_after_adapter_drift(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_frozen_result_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    job = store.read(ref.job_uri)
    _write_success(store, status)

    def _drifted_adapter(_uri):
        raise RuntimeError("adapter registry changed after submission")

    runner.resolve_adapter = _drifted_adapter
    assert runner._validate_job_result(job, store.read(ref.result_uri))["status"] == "done"
    client.set_status(ref.submission_id, "SUCCEEDED")
    assert _wait(runner, status.run_id).status == "done"


def test_driver_dispatch_uses_frozen_sink_writer_and_physical_uri(jobs_config):
    _module, _deps, runner, _client, _store = _runner(jobs_config)
    step = types.SimpleNamespace(id="write", op="write", inputs=[["src"]])
    ir = types.SimpleNamespace(steps=[step])
    observed = {}

    def _commit(_step, _datasets, target_uri, **kwargs):
        observed.update(target_uri=target_uri, **kwargs)
        return 2, kwargs["attempt_uri"], "frozen"

    runner._commit = _commit
    result = runner._run_ir_sync(
        ir, _graph(), "write", sink_targets={"write": "s3://wrong/dynamic.parquet"},
        sink_contracts={"write": {
            "name": "frozen", "logical_uri": "s3://shared/frozen.parquet",
            "physical_uri": "s3://shared/frozen.attempt-run-write.parquet",
            "writer": "worker-direct-parquet",
        }},
    )

    assert result["status"] == "done"
    assert observed["target_uri"] == "s3://shared/frozen.parquet"
    assert observed["attempt_uri"] == "s3://shared/frozen.attempt-run-write.parquet"
    assert observed["writer"] == "worker-direct-parquet"


def test_ray_jobs_rejects_compatibility_sink_before_object_attempt_allocation(
        jobs_config, monkeypatch):
    module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    graph.nodes[-1].data["config"]["filename"] = "jobs_compat.csv"
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    allocation_calls = []
    monkeypatch.setattr(
        module, "allocate_attempt",
        lambda **kwargs: allocation_calls.append(kwargs) or {"uri": "unexpected"},
    )

    with pytest.raises(RuntimeError, match="supports only built-in.*Parquet sinks"):
        runner.run(
            plan, graph, "write", "distributed",
            run_id=f"run_jobs_compat_sink_{uuid.uuid4().hex}",
        )

    assert allocation_calls == []


def test_failed_jobs_result_keeps_private_partial_output_evidence(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_partial_failure_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    job = store.read(ref.job_uri)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    _write_success(store, status, output_uri=physical_uri)
    failed = store.read(ref.result_uri)
    failed.update(
        status="failed", error="later execution step failed",
        rows=0, output_uri=None, output_table=None,
    )

    tampered = dict(failed)
    tampered["outputs"] = [{
        "step_id": "write", "name": "jobs_out", "uri": job["sink_targets"]["write"],
        "logical_uri": job["sink_targets"]["write"],
    }]
    with pytest.raises(module.ArtifactContractError, match="partial outputs"):
        runner._validate_job_result(job, tampered)

    store.write(ref.result_uri, failed)
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, physical_uri)
        attempt.state = "committed"
        attempt.terminal_proof_at = metadb._now()
    client.set_status(ref.submission_id, "SUCCEEDED")
    final = _wait(runner, status.run_id)
    assert final.status == "failed" and final.output_uri is None and final.output_table is None
    assert final.error == "Ray execution failed (RemoteExecutionError; code=ray_execution_failed)"
    assert store.read(ref.result_uri)["outputs"] == [{
        "step_id": "write", "name": "jobs_out", "uri": physical_uri,
        "logical_uri": job["sink_targets"]["write"],
    }]
    with metadb.session() as session:
        assert session.get(metadb.CatalogEntry, physical_uri) is None
        assert session.get(
            metadb.CatalogPublicationEvent,
            f"ray-jobs:{ref.attempt_id}:write",
        ) is None
        assert session.get(
            metadb.CatalogPublicationEvent,
            f"ray-jobs:{ref.attempt_id}",
        ) is None
        assert session.get(metadb.ObjectAttempt, physical_uri).state == "abandoned"


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_negative_effect_stage_requires_exact_sinks_and_durable_derive_authority(
        jobs_config, terminal):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"negative-effects-{terminal}-{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = json.loads(metadb.backend_job_artifact_payload(status.run_id))
    sink_uri = job["sink_contracts"]["write"]["physical_uri"]
    owner = f"negative-owner-{uuid.uuid4().hex}"
    assert metadb.claim_backend_publication(
        status.run_id, ref.attempt_id, owner, 30) == "claimed"

    candidate = status.model_copy(deep=True)
    runner._apply_job_result(candidate, {
        "status": terminal, "rows": 0, "outputs": [],
        "error": "RemoteJobFailed: private remote details" if terminal == "failed" else None,
    })
    candidate.output_uri = "s3://private/remote-output"
    with pytest.raises(ValueError, match="cannot expose output identity"):
        metadb.begin_backend_publication_effects(
            status.run_id, ref.attempt_id, owner, candidate.model_dump(),
            None, {"write": sink_uri}, catalog_effects=[], usage_effect=None,
        )
    candidate.output_uri = None
    with pytest.raises(RuntimeError, match="exactly cover all active bound sinks"):
        metadb.begin_backend_publication_effects(
            status.run_id, ref.attempt_id, owner, candidate.model_dump(),
            None, {}, catalog_effects=[], usage_effect=None,
        )

    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "pending"
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, sink_uri)
        leases = list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == sink_uri,
            metadb.ObjectAttemptLease.lease_type.in_(("write", "publish")),
        )))
        assert attempt.state == "writing" and len(leases) == 2

    if terminal == "failed":
        assert metadb.request_backend_quarantine(status.run_id, "test quarantine") is True
    else:
        assert metadb.request_backend_cancel(status.run_id) is True
    assert metadb.begin_backend_publication_effects(
        status.run_id, ref.attempt_id, owner, candidate.model_dump(),
        None, None, catalog_effects=[], usage_effect=None,
    ) == "started"
    effects = metadb.backend_publication_effects(status.run_id, ref.attempt_id)
    assert effects is not None and list(effects["sink_attempts"].values()) == [sink_uri]
    assert effects["validated_result"] is None
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, sink_uri).state == "abandoned"
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == sink_uri,
            metadb.ObjectAttemptLease.lease_type.in_(("write", "publish")),
        ))) == []


def test_official_jobs_client_pins_explicit_api_address_over_ray_address(jobs_config, monkeypatch):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    observed = {}

    class FakeOfficialClient:
        def __init__(self, address):
            observed["argument"] = address
            observed["api"] = os.environ.get("RAY_API_SERVER_ADDRESS")
            observed["ray"] = os.environ.get("RAY_ADDRESS")

        def _do_request(self, method, endpoint, **kwargs):
            observed["request"] = (method, endpoint, kwargs)
            return "bounded-response"

    ray_module = types.ModuleType("ray")
    jobs_module = types.ModuleType("ray.job_submission")
    jobs_module.JobSubmissionClient = FakeOfficialClient
    ray_module.job_submission = jobs_module
    monkeypatch.setitem(sys.modules, "ray", ray_module)
    monkeypatch.setitem(sys.modules, "ray.job_submission", jobs_module)
    monkeypatch.setenv("RAY_ADDRESS", "auto")
    monkeypatch.delenv("RAY_API_SERVER_ADDRESS", raising=False)
    runner._jobs_client_factory = None

    client = runner._jobs_client()
    response = client._do_request("GET", "/api/jobs/test")

    assert isinstance(client, FakeOfficialClient)
    assert response == "bounded-response"
    assert observed == {
        "argument": runner.jobs_address,
        "api": runner.jobs_address,
        "ray": "auto",
        "request": (
            "GET", "/api/jobs/test", {"timeout": runner._jobs_request_timeout_s}
        ),
    }
    assert "RAY_API_SERVER_ADDRESS" not in os.environ


def test_ray_jobs_recognizes_preexisting_duplicate_without_second_submit(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_duplicate_{uuid.uuid4().hex}"
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed", run_id=run_id)
    ref = status.backend_ref
    assert ref is not None
    client.put(ref.submission_id, "RUNNING", metadata=_workload_metadata(status))

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    _wait_submitted(client, ref.submission_id)
    time.sleep(0.05)
    assert client.submit_calls == []
    _complete(store, client, status)
    assert _wait(recovered, run_id).status == "done"


def test_duplicate_run_id_is_rejected_before_new_backend_artifacts(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    first_graph = _graph(name="first")
    first_plan = compile_plan(
        first_graph, "write", deps.registry, deps.node_specs, deps.node_ir
    )
    run_id = f"run_jobs_duplicate_payload_{uuid.uuid4().hex}"
    first = runner.run(first_plan, first_graph, "write", "distributed", run_id=run_id)
    first_payload = metadb.backend_job_artifact_payload(run_id)

    with pytest.raises(RuntimeError, match="already allocated"):
        _preallocate_backend_test_run(run_id)

    assert metadb.backend_job_artifact_payload(run_id) == first_payload
    assert client.submit_calls == []
    _retire_inert_test_run(first)


def test_ray_jobs_cancel_waits_for_stopped_acknowledgement(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_cancel_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    physical_uri = store.read(ref.job_uri)["sink_contracts"]["write"]["physical_uri"]

    final = runner.cancel(status.run_id)

    assert final.status == "cancelled"
    assert client.stop_calls == [ref.submission_id]
    assert runner.cancel_acknowledged(status.run_id) is True
    assert metadb.get_run_state(status.run_id)["status"] == "cancelled"
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, physical_uri).state == "abandoned"


def test_ray_jobs_cancel_timeout_never_claims_a_live_job_is_cancelled(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_cancel_timeout_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)

    def _unacknowledged_stop(submission_id: str):
        client.stop_calls.append(submission_id)  # the cluster accepts the request but remains RUNNING
        return True

    client.stop_job = _unacknowledged_stop
    started = time.monotonic()
    still_live = runner.cancel(status.run_id)

    assert time.monotonic() - started >= 0.18
    assert still_live.status == "running" and runner.cancel_acknowledged(status.run_id) is False
    assert "not acknowledged" in (still_live.error or "")
    client.set_status(ref.submission_id, "STOPPED")  # settle the daemon supervisor before leaving the test
    assert _wait(runner, status.run_id).status == "cancelled"


def test_cancel_converges_when_publication_wins_before_cancel_cas(jobs_config, monkeypatch):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "map", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "map", "distributed",
        run_id=f"run_jobs_cancel_cas_lost_{uuid.uuid4().hex}",
    )
    original_request = metadb.request_backend_cancel
    outcomes: list[bool] = []

    def _publish_then_request(run_id: str) -> bool:
        assert run_id == status.run_id
        _publish_test_backend_done(status)
        outcomes.append(original_request(run_id))
        return outcomes[-1]

    monkeypatch.setattr(metadb, "request_backend_cancel", _publish_then_request)
    final = runner.cancel(status.run_id)

    assert outcomes == [False]
    assert final.status == "done" and final.error is None
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"
    assert binding["cancel_requested"] is False
    assert runner._settled[status.run_id].is_set()
    assert status.run_id not in runner._cancel
    assert client.stop_calls == []


def test_cancel_converges_when_publication_wins_during_local_wait(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "map", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "map", "distributed",
        run_id=f"run_jobs_cancel_wait_race_{uuid.uuid4().hex}",
    )

    class PublishingWait:
        def __init__(self):
            self.set_called = False
            self.wait_called = False

        def wait(self, _timeout=None):
            self.wait_called = True
            _publish_test_backend_done(status)
            return False  # another supervisor published; this process never signalled its local event

        def set(self):
            self.set_called = True

        def is_set(self):
            return self.set_called

    local_settled = PublishingWait()
    runner._settled[status.run_id] = local_settled
    final = runner.cancel(status.run_id)

    assert local_settled.wait_called and local_settled.is_set()
    assert final.status == "done" and final.error is None
    assert metadb.get_run_state(status.run_id)["status"] == "done"
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"
    assert binding["cancel_requested"] is True
    assert status.run_id not in runner._cancel
    assert client.stop_calls == []


def test_cancel_before_sql_submission_claim_forbids_submit(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_cancel_before_claim_{uuid.uuid4().hex}")
    job = _materialize_bound_job(runner, status)

    assert metadb.request_backend_cancel(status.run_id) is True
    assert runner._ensure_job_submitted(client, job) == "CANCEL_REQUESTED"
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "queued"
    assert binding["cancel_requested"] is True
    assert client.submit_calls == []
    runner._publish_cancelled_binding(status, binding)


def test_sql_submission_claim_linearizes_before_concurrent_cancel(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_claim_before_cancel_{uuid.uuid4().hex}")
    ref = status.backend_ref
    job = _materialize_bound_job(runner, status)
    entered_submit, release_submit = threading.Event(), threading.Event()
    original_submit = client.submit_job

    def _paused_submit(**kwargs):
        entered_submit.set()
        assert release_submit.wait(2), "test did not release the linearized submit"
        return original_submit(**kwargs)

    client.submit_job = _paused_submit
    outcome: list[str] = []
    thread = threading.Thread(target=lambda: outcome.append(runner._ensure_job_submitted(client, job)))
    thread.start()
    assert entered_submit.wait(2)
    claimed = metadb.backend_job(status.run_id)
    assert claimed["submission_state"] == "submitting" and claimed["submission_owner"]

    assert metadb.request_backend_cancel(status.run_id) is True
    # The submit already linearized. It is allowed to finish, but cancellation must then observe and stop it.
    release_submit.set()
    thread.join(2)
    assert not thread.is_alive() and outcome == ["PENDING"]
    submitted = metadb.backend_job(status.run_id)
    assert submitted["submission_state"] == "stopping" and submitted["cancel_requested"] is True
    requested, _control, state = runner._cancel_control_state(status, ref, submitted)
    assert requested is True and state == "STOPPED"
    assert client.submit_calls and client.stop_calls == [ref.submission_id]
    runner._publish_cancelled_binding(status, metadb.backend_job(status.run_id))


def test_blocked_submit_stops_renewing_and_allows_durable_takeover(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    runner._max_lease_hold_s = 0.05
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_bounded_submit_lease_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    entered_submit, release_submit = threading.Event(), threading.Event()
    original_submit = client.submit_job

    def _blocked_submit(**kwargs):
        entered_submit.set()
        assert release_submit.wait(2), "test did not release the blocked submit"
        return original_submit(**kwargs)

    client.submit_job = _blocked_submit
    outcome: list[str] = []
    thread = threading.Thread(
        target=lambda: outcome.append(runner._ensure_job_submitted(client, job))
    )
    thread.start()
    assert entered_submit.wait(2)
    time.sleep(0.1)
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )
    time.sleep(0.3)

    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "replacement-owner", 5
    ) == "claimed"
    release_submit.set()
    thread.join(2)
    assert not thread.is_alive() and outcome == ["PENDING"]
    assert metadb.backend_job(status.run_id)["submission_state"] == "submitted"

    assert metadb.request_backend_cancel(status.run_id) is True
    requested, _control, state = runner._cancel_control_state(
        status, ref, metadb.backend_job(status.run_id)
    )
    assert requested is True and state == "STOPPED"
    runner._publish_cancelled_binding(status, metadb.backend_job(status.run_id), job)


def test_submit_timeout_keeps_fence_until_late_job_is_stopped(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_late_accept_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    release_late, accepted = threading.Event(), threading.Event()
    original_submit = client.submit_job

    def _timeout_then_accept(**kwargs):
        def _late_accept():
            assert release_late.wait(2)
            original_submit(**kwargs)
            accepted.set()

        threading.Thread(target=_late_accept).start()
        raise TimeoutError("response timed out while request remained in flight")

    client.submit_job = _timeout_then_accept
    with pytest.raises(TimeoutError, match="remained in flight"):
        runner._ensure_job_submitted(client, job)
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "submitting", "ambiguous submit must retain its fence"

    assert metadb.request_backend_cancel(status.run_id) is True
    requested, _control, state = runner._cancel_control_state(status, ref, metadb.backend_job(status.run_id))
    assert requested is True and state == "SUBMITTING"
    assert client.stop_calls == []

    release_late.set()
    assert accepted.wait(2)
    requested, _control, state = runner._cancel_control_state(status, ref, metadb.backend_job(status.run_id))
    assert requested is True and state == "STOPPED"
    assert client.stop_calls == [ref.submission_id]
    runner._publish_cancelled_binding(status, metadb.backend_job(status.run_id))


def test_recovery_queries_ray_before_reclaiming_expired_submission_claim(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_expired_claim_{uuid.uuid4().hex}")
    ref = status.backend_ref
    job = _materialize_bound_job(runner, status)
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "dead-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        row = session.get(metadb.RunBackendJob, status.run_id)
        row.submission_lease_until = metadb._now() - datetime.timedelta(seconds=1)
    client.put(ref.submission_id, "RUNNING", metadata={
        "dataplay_run_id": status.run_id,
        "dataplay_attempt_id": ref.attempt_id,
        "dataplay_code_ref": ref.code_ref,
    })
    client.events.clear()

    assert runner._ensure_job_submitted(client, job) == "RUNNING"
    assert client.events[0] == "status"
    assert "submit" not in client.events
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "submitted" and binding["submission_owner"] is None
    _retire_inert_test_run(status)


def test_submission_and_terminal_publication_claims_are_mutually_exclusive(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)

    def bound(label: str):
        runner._ensure_jobs_supervisor = lambda _run_id: None
        status = runner.run(
            plan, graph, "write", "distributed",
            run_id=f"run_jobs_claim_exclusion_{label}_{uuid.uuid4().hex}",
        )
        assert status.backend_ref is not None
        return status, _materialize_bound_job(runner, status)

    def publish_failed(status, owner: str) -> None:
        ref = status.backend_ref
        assert ref is not None
        assert metadb.request_backend_quarantine(status.run_id, "test cleanup") is True
        candidate = status.model_copy(deep=True)
        runner._apply_job_result(candidate, {
            "status": "failed", "rows": 0, "outputs": [], "error": "test cleanup",
        })
        assert metadb.begin_backend_publication_effects(
            status.run_id, ref.attempt_id, owner, candidate.model_dump(),
            None, None, catalog_effects=[], usage_effect=None,
        ) == "started"
        assert metadb.finish_backend_publication(
            status.run_id, ref.attempt_id, owner, candidate.model_dump()) is True

    submit_first, _job = bound("submit_first")
    submit_ref = submit_first.backend_ref
    assert metadb.claim_backend_submission_after_missing(
        submit_first.run_id, submit_ref.attempt_id, "submit-owner", 30) == "claimed"
    assert metadb.claim_backend_publication(
        submit_first.run_id, submit_ref.attempt_id, "publication-loser", 30
    ) == "submission"
    assert metadb.backend_job(submit_first.run_id)["publication_state"] == "pending"
    assert metadb.note_backend_submission_observed(
        submit_first.run_id, submit_ref.attempt_id) is True
    assert metadb.claim_backend_publication(
        submit_first.run_id, submit_ref.attempt_id, "publication-winner", 30
    ) == "claimed"
    publish_failed(submit_first, "publication-winner")

    publication_first, publication_job = bound("publication_first")
    publication_ref = publication_first.backend_ref
    assert metadb.claim_backend_publication(
        publication_first.run_id, publication_ref.attempt_id, "publication-owner", 30
    ) == "claimed"
    assert runner._ensure_job_submitted(client, publication_job) == "SUBMITTING"
    assert client.submit_calls == []
    assert metadb.backend_job(publication_first.run_id)["submission_state"] == "queued"
    publish_failed(publication_first, "publication-owner")


def test_expired_publication_owner_cannot_revive_after_submission_claim(jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_expired_publication_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    assert metadb.claim_backend_publication(
        status.run_id, ref.attempt_id, "stale-publisher", 5
    ) == "claimed"
    with metadb.session() as session:
        row = session.get(metadb.RunBackendJob, status.run_id)
        row.publication_lease_until = metadb._db_now(session) - datetime.timedelta(seconds=1)

    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "submission-winner", 30
    ) == "claimed"
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "submitting"
    assert binding["publication_state"] == "pending"
    with metadb.session() as session:
        assert session.get(metadb.RunBackendJob, status.run_id).publication_owner is None
    assert metadb.renew_backend_publication(
        status.run_id, ref.attempt_id, "stale-publisher", 30
    ) is False
    assert metadb.begin_backend_publication_effects(
        status.run_id, ref.attempt_id, "stale-publisher",
        status.model_copy(update={"status": "failed"}).model_dump(),
        None, {}, [], None,
    ) == "busy"
    assert metadb.note_backend_submission_observed(
        status.run_id, ref.attempt_id) is True
    runner._publish_job_result(
        job, None, status.target_node_id, status,
        {"status": "failed", "error": "test cleanup", "rows": 0, "outputs": []},
        artifact_error=True,
    )


def test_remote_observation_invalidates_pre_effects_publisher_but_not_effects_winner(
        jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None

    pending = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_observation_pending_{uuid.uuid4().hex}",
    )
    pending_ref = pending.backend_ref
    assert pending_ref is not None
    pending_job = _materialize_bound_job(runner, pending)
    assert metadb.note_backend_submission_observed(
        pending.run_id, pending_ref.attempt_id) is True
    assert metadb.claim_backend_publication(
        pending.run_id, pending_ref.attempt_id, "stale-publisher", 30
    ) == "claimed"
    assert metadb.note_backend_submission_observed(
        pending.run_id, pending_ref.attempt_id) is True
    binding = metadb.backend_job(pending.run_id)
    assert binding["submission_state"] == "submitted"
    with metadb.session() as session:
        assert session.get(metadb.RunBackendJob, pending.run_id).publication_owner is None
    assert metadb.renew_backend_publication(
        pending.run_id, pending_ref.attempt_id, "stale-publisher", 30
    ) is False

    effects = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_observation_effects_{uuid.uuid4().hex}",
    )
    effects_ref = effects.backend_ref
    assert effects_ref is not None
    assert metadb.note_backend_submission_observed(
        effects.run_id, effects_ref.attempt_id) is True
    assert metadb.request_backend_quarantine(effects.run_id, "terminal winner") is True
    assert metadb.claim_backend_publication(
        effects.run_id, effects_ref.attempt_id, "effects-winner", 30
    ) == "claimed"
    terminal = effects.model_copy(deep=True)
    terminal.status, terminal.error = "failed", "terminal winner"
    assert metadb.begin_backend_publication_effects(
        effects.run_id, effects_ref.attempt_id, "effects-winner",
        terminal.model_dump(), None, None, [], None,
    ) == "started"
    assert metadb.note_backend_submission_observed(
        effects.run_id, effects_ref.attempt_id) is False
    binding = metadb.backend_job(effects.run_id)
    assert binding["publication_state"] == "effects_started"
    with metadb.session() as session:
        assert session.get(metadb.RunBackendJob, effects.run_id).publication_owner == \
            "effects-winner"
    runner._publish_job_result(
        pending_job, None, pending.target_node_id, pending,
        {"status": "failed", "error": "test cleanup", "rows": 0, "outputs": []},
        artifact_error=True,
    )
    assert metadb.finish_backend_publication(
        effects.run_id, effects_ref.attempt_id, "effects-winner",
        terminal.model_dump(),
    ) is True


@pytest.mark.parametrize("remote_winner", ["fence", "workload"])
def test_valid_result_fences_expired_uncertain_submit_before_publication(
        jobs_config, remote_winner):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_result_reconcile_{remote_winner}_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    _write_success(store, status)
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-submit-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        row = session.get(metadb.RunBackendJob, status.run_id)
        row.submission_lease_until = metadb._db_now(session) - datetime.timedelta(seconds=1)

    if remote_winner == "workload":
        def workload_wins(**_kwargs):
            client.put(ref.submission_id, "RUNNING", metadata={
                "dataplay_run_id": status.run_id,
                "dataplay_attempt_id": ref.attempt_id,
                "dataplay_code_ref": ref.code_ref,
            })
            raise RuntimeError("submission id already exists")

        client.submit_job = workload_wins

    control, ready = runner._drive_result_reconciliation(
        status, ref, metadb.backend_job(status.run_id))
    assert control is client and ready is True
    assert client.stop_calls == [ref.submission_id]
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == (
        "submitted" if remote_winner == "workload" else "result_fenced"
    )
    assert binding["cancel_requested"] is False
    if remote_winner == "fence":
        assert client.submit_calls[-1]["metadata"]["dataplay_stop_fence"] == \
            "result-reconciliation"

    runner._publish_reconciled_result(job, graph, "write", status)
    assert status.status == "done"
    assert metadb.backend_job(status.run_id)["publication_state"] == "published"


def test_reconciled_result_corruption_uses_durable_quarantine(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_result_reconcile_corrupt_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    _write_success(store, status)
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-submit-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )

    control, ready = runner._drive_result_reconciliation(
        status, ref, metadb.backend_job(status.run_id)
    )
    assert control is client and ready is True
    corrupt = store.read(ref.result_uri)
    corrupt["unexpected"] = "field"
    store.write(ref.result_uri, corrupt)

    runner._publish_reconciled_result(job, graph, "write", status)

    assert status.status == "failed"
    assert status.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"
    assert "artifact_contract_invalid" in binding["quarantine_reason"]
    with metadb.session() as session:
        assert session.get(metadb.CatalogEntry, physical_uri) is None
        assert session.get(metadb.ObjectAttempt, physical_uri).state == "abandoned"


def test_result_candidate_is_reobserved_when_remote_submit_settles_first(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_result_reobserve_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    _write_success(store, status)
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-submit-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )
    stale_missing_binding = metadb.backend_job(status.run_id)

    client.put(ref.submission_id, "RUNNING", metadata={
        "dataplay_run_id": status.run_id,
        "dataplay_attempt_id": ref.attempt_id,
        "dataplay_code_ref": ref.code_ref,
    })
    assert metadb.note_backend_submission_observed(
        status.run_id, ref.attempt_id) is True
    control, ready = runner._drive_result_reconciliation(
        status, ref, stale_missing_binding)

    assert control is client and ready is False
    assert client.submit_calls == [] and client.stop_calls == []
    assert metadb.backend_job(status.run_id)["submission_state"] == "submitted"

    client.set_status(ref.submission_id, "SUCCEEDED")
    runner._publish_job_result(
        job, graph, "write", status, runner._terminal_result_if_present(job))
    assert status.status == "done"


def test_result_reconciliation_initiated_from_state_loop_converges(jobs_config):
    # When the state-establishment loop initiates reconciliation (binding still "submitting", job
    # artifact readable, Ray metadata lost, valid result present), it must keep re-driving the fence
    # across iterations. Otherwise the next iteration finds the loop's own stop fence and validates it
    # as a live workload forever — a permanent wedge on a healthy cluster and database.
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_reconcile_state_loop_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _materialize_bound_job(runner, status)  # the job artifact is readable, so loop 1 exits with a job
    _write_success(store, status)            # a valid hash-bound result is present
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-submit-owner", 5) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1))
    assert metadb.backend_job(status.run_id)["submission_state"] == "submitting"
    assert ref.submission_id not in client.jobs  # authoritative Ray metadata loss

    # Real Ray's stop is asynchronous: the reconciliation fence stays live for at least one poll after
    # stop, so the first drive returns not-ready and the loop must re-drive on the next iteration.
    def async_stop(submission_id: str) -> bool:
        with client.lock:
            client.stop_calls.append(submission_id)
        return True

    client.stop_job = async_stop

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True)
    deadline = time.monotonic() + 5
    while (metadb.backend_job(status.run_id)["submission_state"]
           not in ("result_stop_fenced", "result_fenced", "published")
           and time.monotonic() < deadline):
        time.sleep(0.01)
    # The state loop claimed its own stop fence; let it settle and require the loop to converge.
    client.set_status(ref.submission_id, "STOPPED")
    final = _wait(recovered, status.run_id)
    assert final.status == "done"
    assert metadb.backend_job(status.run_id)["submission_state"] == "result_fenced"


def test_result_reconciliation_survives_uncertain_fence_submission(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_result_reconcile_restart_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _materialize_bound_job(runner, status)
    _write_success(store, status)
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-submit-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )

    original_submit = client.submit_job
    client.submit_job = lambda **_kwargs: (_ for _ in ()).throw(
        TimeoutError("result fence response lost"))
    control, ready = runner._drive_result_reconciliation(
        status, ref, metadb.backend_job(status.run_id))
    assert control is client and ready is False
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "result_fencing"
    assert binding["cancel_requested"] is False

    client.submit_job = original_submit
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )
    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    final = _wait(recovered, status.run_id)
    assert final.status == "done"
    assert metadb.backend_job(status.run_id)["submission_state"] == "result_fenced"


@pytest.mark.parametrize("failure_mode", ["none", "quarantine", "corrupt_sql"])
def test_settled_result_fence_survives_crash_before_publication(
        jobs_config, failure_mode):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_result_settled_restart_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _materialize_bound_job(runner, status)
    _write_success(store, status)
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-submit-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )

    control, ready = runner._drive_result_reconciliation(
        status, ref, metadb.backend_job(status.run_id))
    assert control is client and ready is True
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "result_fenced"
    assert binding["publication_state"] == "pending"
    if failure_mode == "quarantine":
        assert metadb.request_backend_quarantine(
            status.run_id, "settled result fence quarantine"
        ) is True
    elif failure_mode == "corrupt_sql":
        with metadb.session() as session:
            session.get(metadb.RunBackendJob, status.run_id).job_doc = "{corrupt"
    control_calls = (
        len(client.status_calls), len(client.submit_calls), len(client.stop_calls)
    )

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    final = _wait(recovered, status.run_id)

    assert final.status == ("done" if failure_mode == "none" else "failed")
    if failure_mode != "none":
        assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    assert metadb.backend_job(status.run_id)["publication_state"] == "published"
    assert (
        len(client.status_calls), len(client.submit_calls), len(client.stop_calls)
    ) == control_calls


def test_expired_submission_reclaim_and_cancel_have_one_sql_winner(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None

    def _expired_claim(label: str):
        status = runner.run(
            plan, graph, "write", "distributed",
            run_id=f"run_jobs_expired_{label}_{uuid.uuid4().hex}",
        )
        ref = status.backend_ref
        assert ref is not None
        assert metadb.claim_backend_submission_after_missing(
            status.run_id, ref.attempt_id, f"old-{label}", 5
        ) == "claimed"
        with metadb.session() as session:
            row = session.get(metadb.RunBackendJob, status.run_id)
            row.submission_lease_until = metadb._now() - datetime.timedelta(seconds=1)
        return status, ref

    cancelled, cancelled_ref = _expired_claim("cancel-wins")
    assert metadb.request_backend_cancel(cancelled.run_id) is True
    assert metadb.claim_backend_submission_after_missing(
        cancelled.run_id, cancelled_ref.attempt_id, "new-after-cancel", 5
    ) == "cancelled"
    binding = metadb.backend_job(cancelled.run_id)
    assert binding["submission_state"] == "submitting"
    assert binding["submission_owner"] == "old-cancel-wins"
    requested, _control, state = runner._cancel_control_state(cancelled, cancelled_ref, binding)
    assert requested is True and state == "STOPPED"
    binding = metadb.backend_job(cancelled.run_id)
    assert binding["submission_state"] == "stop_fenced"
    assert client.submit_calls[-1]["entrypoint"] == "sleep 86400"
    assert client.stop_calls == [cancelled_ref.submission_id]
    with client.lock:
        client.jobs.pop(cancelled_ref.submission_id)
    requested, _control, state = runner._cancel_control_state(
        cancelled, cancelled_ref, metadb.backend_job(cancelled.run_id)
    )
    assert requested is True and state == "STOPPED", "accepted stop fence makes absence terminal evidence"
    runner._publish_cancelled_binding(cancelled, metadb.backend_job(cancelled.run_id))

    reclaimed, reclaimed_ref = _expired_claim("reclaim-wins")
    assert metadb.claim_backend_submission_after_missing(
        reclaimed.run_id, reclaimed_ref.attempt_id, "new-before-cancel", 5
    ) == "claimed"
    assert metadb.request_backend_cancel(reclaimed.run_id) is True
    binding = metadb.backend_job(reclaimed.run_id)
    assert binding["submission_state"] == "submitting"
    assert binding["submission_owner"] == "new-before-cancel"
    assert binding["cancel_requested"] is True
    requested, _control, state = runner._cancel_control_state(reclaimed, reclaimed_ref, binding)
    assert requested is True and state == "SUBMITTING", "the unexpired CAS winner must settle first"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, reclaimed.run_id).submission_lease_until = (
            metadb._now() - datetime.timedelta(seconds=1)
        )
    requested, _control, state = runner._cancel_control_state(
        reclaimed, reclaimed_ref, metadb.backend_job(reclaimed.run_id)
    )
    assert requested is True and state == "STOPPED"
    runner._publish_cancelled_binding(reclaimed, metadb.backend_job(reclaimed.run_id))


@pytest.mark.parametrize("remote_winner", ["workload", "fence"])
def test_stop_control_blocks_publication_until_remote_terminal(
        jobs_config, remote_winner):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_stop_barrier_{remote_winner}_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)

    if remote_winner == "workload":
        client.put(
            ref.submission_id, "RUNNING", metadata=_workload_metadata(status))
        assert metadb.note_backend_submission_observed(
            status.run_id, ref.attempt_id) is True
    else:
        assert metadb.claim_backend_submission_after_missing(
            status.run_id, ref.attempt_id, "crashed-submit-owner", 5
        ) == "claimed"
        with metadb.session() as session:
            session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
                metadb._db_now(session) - datetime.timedelta(seconds=1)
            )
    assert metadb.request_backend_cancel(status.run_id) is True

    stop_entered, release_stop = threading.Event(), threading.Event()
    original_stop = client.stop_job

    def paused_stop(submission_id: str):
        stop_entered.set()
        assert release_stop.wait(2), "test did not release the remote stop"
        return original_stop(submission_id)

    client.stop_job = paused_stop
    outcome: list[tuple[bool, object | None, str | None]] = []
    thread = threading.Thread(target=lambda: outcome.append(
        runner._cancel_control_state(
            status, ref, metadb.backend_job(status.run_id))))
    thread.start()
    assert stop_entered.wait(2)

    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == (
        "stopping" if remote_winner == "workload" else "fence_stopping"
    )
    assert metadb.claim_backend_publication(
        status.run_id, ref.attempt_id, "stale-publisher", 30
    ) == "submission"

    release_stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert outcome and outcome[0][0] is True and outcome[0][2] == "STOPPED"
    assert metadb.backend_job(status.run_id)["submission_state"] == (
        "submitted" if remote_winner == "workload" else "stop_fenced"
    )
    runner._publish_cancelled_binding(
        status, metadb.backend_job(status.run_id), job)
    assert status.status == "cancelled"


def test_cancel_fence_timeout_still_stops_its_late_remote_job(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_late_fence_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._now() - datetime.timedelta(seconds=1)
        )
    assert metadb.request_backend_cancel(status.run_id) is True

    release_late, accepted = threading.Event(), threading.Event()
    original_submit = client.submit_job

    def _timeout_then_accept(**kwargs):
        def _late_accept():
            assert release_late.wait(2)
            original_submit(**kwargs)
            accepted.set()

        threading.Thread(target=_late_accept).start()
        raise TimeoutError("cancel fence response lost")

    client.submit_job = _timeout_then_accept
    requested, _control, state = runner._cancel_control_state(
        status, ref, metadb.backend_job(status.run_id)
    )
    assert requested is True and state == "FENCING"
    assert metadb.backend_job(status.run_id)["submission_state"] == "fencing"

    release_late.set()
    assert accepted.wait(2)
    requested, _control, state = runner._cancel_control_state(
        status, ref, metadb.backend_job(status.run_id)
    )
    assert requested is True and state == "STOPPED"
    assert client.stop_calls == [ref.submission_id]
    runner._publish_cancelled_binding(status, metadb.backend_job(status.run_id))


def test_lost_fence_response_with_instant_success_persists_fence_winner(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_instant_fence_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._now() - datetime.timedelta(seconds=1)
        )
    assert metadb.request_backend_cancel(status.run_id) is True
    original_submit = client.submit_job

    def _accepted_then_response_lost(**kwargs):
        original_submit(**kwargs)
        client.set_status(kwargs["submission_id"], "SUCCEEDED")
        raise TimeoutError("fence response lost after remote completion")

    client.submit_job = _accepted_then_response_lost
    requested, _control, state = runner._cancel_control_state(
        status, ref, metadb.backend_job(status.run_id)
    )

    assert requested is True and state == "STOPPED"
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "stop_fenced"
    runner._publish_cancelled_binding(status, binding)
    assert status.status == "cancelled" and "result" not in (status.error or "")


def test_fence_submit_race_persists_delayed_workload_winner_from_metadata(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_workload_winner_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-owner", 5
    ) == "claimed"
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._now() - datetime.timedelta(seconds=1)
        )
    assert metadb.request_backend_cancel(status.run_id) is True

    def _delayed_workload_wins(**kwargs):
        client.put(
            kwargs["submission_id"], "RUNNING",
            metadata={
                "dataplay_run_id": status.run_id,
                "dataplay_attempt_id": ref.attempt_id,
                "dataplay_code_ref": ref.code_ref,
            },
        )
        raise RuntimeError("submission id already exists")

    client.submit_job = _delayed_workload_wins
    requested, _control, state = runner._cancel_control_state(
        status, ref, metadb.backend_job(status.run_id)
    )

    assert requested is True and state == "STOPPED"
    assert metadb.backend_job(status.run_id)["submission_state"] == "submitted"
    assert client.stop_calls == [ref.submission_id]
    runner._publish_cancelled_binding(status, metadb.backend_job(status.run_id))


def test_quarantine_fences_an_expired_submit_before_terminal_failure(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_quarantine_fence_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    assert metadb.claim_backend_submission_after_missing(
        status.run_id, ref.attempt_id, "crashed-owner", 5
    ) == "claimed"
    assert metadb.request_backend_quarantine(status.run_id, "tampered envelope") is True
    binding = metadb.backend_job(status.run_id)
    assert runner._resume_quarantined_job(status, ref, binding) is False
    assert "already-linearized submit" in (status.error or "")
    assert client.submit_calls == [] and client.stop_calls == []

    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).submission_lease_until = (
            metadb._now() - datetime.timedelta(seconds=1)
        )

    binding = metadb.backend_job(status.run_id)
    assert runner._resume_quarantined_job(status, ref, binding) is True
    assert client.stop_calls == [ref.submission_id]
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "stop_fenced"
    assert status.status == "failed"
    assert status.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"


def test_quarantine_race_exits_stale_done_publication_and_converges_failed(
        jobs_config, monkeypatch):
    module, deps, runner, _client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_quarantine_publish_race_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)

    _write_success(store, status)
    result = runner._validate_job_result(job, store.read(ref.result_uri))
    output_uri = result["outputs"][0]["uri"]
    original_begin = metadb.begin_backend_publication_effects
    begin_calls = 0

    def quarantine_before_begin(run_id, attempt_id, owner, *args, **kwargs):
        nonlocal begin_calls
        begin_calls += 1
        if begin_calls > 1:
            raise AssertionError("stale done publisher retried after quarantine")
        assert metadb.request_backend_quarantine(
            run_id, "artifact corruption won publication race") is True
        return original_begin(run_id, attempt_id, owner, *args, **kwargs)

    monkeypatch.setattr(
        metadb, "begin_backend_publication_effects", quarantine_before_begin)
    runner._publish_job_result(
        job, graph, "write", status, result,
    )

    assert begin_calls == 1
    assert status.status in ("queued", "running")
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "pending"
    assert binding["quarantine_reason"] == "artifact corruption won publication race"
    with metadb.session() as session:
        assert session.get(metadb.CatalogEntry, output_uri) is None
        assert session.get(
            metadb.CatalogPublicationEvent,
            f"ray-jobs:{ref.attempt_id}:write",
        ) is None

    monkeypatch.setattr(metadb, "begin_backend_publication_effects", original_begin)
    with metadb.session() as session:
        row = session.get(metadb.RunBackendJob, status.run_id)
        row.publication_lease_until = (
            metadb._db_now(session) - datetime.timedelta(seconds=1)
        )
    runner._publish_job_result(
        job, None, status.target_node_id, status,
        {"status": "failed", "error": "artifact corruption", "rows": 0, "outputs": []},
        artifact_error=True,
    )
    assert status.status == "failed"
    assert status.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    assert metadb.backend_job(status.run_id)["publication_state"] == "published"
    with metadb.session() as session:
        assert session.get(metadb.CatalogEntry, output_uri) is None
        assert session.get(
            metadb.CatalogPublicationEvent,
            f"ray-jobs:{ref.attempt_id}:write",
        ) is None


@pytest.mark.parametrize("prune_terminal_detail", [False, True])
def test_late_quarantine_converges_terminal_winner_without_remote_stop(
        jobs_config, monkeypatch, prune_terminal_detail):
    module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_late_quarantine_{prune_terminal_detail}_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 0 if prune_terminal_detail else 1000)
    runner._publish_job_result(
        job, None, status.target_node_id, status,
        {"status": "failed", "error": "canonical failure", "rows": 0, "outputs": []},
        artifact_error=True,
    )
    assert status.status == "failed"

    # Emulate a stale supervisor that observed corruption before learning a terminal publication won.
    status.status, status.error = "queued", "stale corruption observer"
    client.put(ref.submission_id, "RUNNING", metadata=_workload_metadata(status))
    assert runner._quarantine_invalid_job(
        status, ref, module.ArtifactContractError("late corrupt artifact")) is True

    assert status.status == "failed"
    assert client.stop_calls == []
    binding = metadb.backend_job(status.run_id)
    if prune_terminal_detail:
        assert binding is None
    else:
        assert binding["publication_state"] == "published"
        assert binding["quarantine_reason"] is None


def test_ray_jobs_restart_reattaches_and_catalog_publication_has_one_winner(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_restart_{uuid.uuid4().hex}"
    original._ensure_jobs_supervisor = lambda _run_id: None  # submitting hub dies after durable handoff
    queued = original.run(plan, graph, "write", "distributed", run_id=run_id)
    ref = queued.backend_ref
    assert ref is not None
    client.put(
        ref.submission_id, "RUNNING", metadata=_workload_metadata(queued)
    )  # the submit reached Ray before the hub process disappeared
    # Durable external jobs are hub-routed by backend binding, never rebound to an unrelated canvas kernel.
    metadb.save_run_state(run_id, queued.model_dump(), canvas_id=graph.id)
    metadb.reap_orphaned_runs()  # boot-time reaping must preserve a DB-bound reattachable external job
    assert metadb.get_run_state(run_id)["status"] == "queued"

    completions: list[str] = []
    recovered_a = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    recovered_b = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    recovered_a.on_complete = recovered_b.on_complete = lambda _g, _t, st: completions.append(st.run_id)
    assert run_id in recovered_a.runs and run_id in recovered_b.runs
    assert deps.run_index[run_id] is recovered_b  # restart restored status/cancel routing, not just polling
    assert client.submit_calls == []

    _complete(store, client, queued)
    final = _wait(recovered_a, run_id)
    assert final.status == "done"
    assert _wait(recovered_b, run_id).status == "done"
    assert metadb.catalog_get(final.output_uri) is not None
    with metadb.session() as session:
        events = list(session.scalars(select(metadb.CatalogPublicationEvent).where(
            metadb.CatalogPublicationEvent.event_key
            == f"ray-jobs:{ref.attempt_id}:write"
        )))
        assert len(events) == 1
    # Jobs publication records required SQL state/history inside the winner transaction; it must not
    # call the generic on_complete hook (which would duplicate history and can swallow failures).
    assert completions == []
    binding = metadb.backend_job(run_id)
    assert binding and binding["publication_state"] == "published"
    assert metadb.get_run_state(run_id)["status"] == "done"
    metadb.drop_kernel(graph.id, "new-kernel")


def test_same_state_control_recovery_clears_durable_error_without_write_churn(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_error_clear_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    deadline = time.monotonic() + 2
    while runner.status(status.run_id).status != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.status(status.run_id).status == "running"

    # Freeze the next Jobs poll so the test observes the persisted error before the same RUNNING state
    # succeeds again. Recovery must clear that stale DB/UI error even though the visible label is unchanged.
    with client.lock:
        runner._persist_jobs_live_error(status, "Ray control temporarily unavailable")
        assert metadb.get_run_state(status.run_id)["error"] == "Ray control temporarily unavailable"

    deadline = time.monotonic() + 2
    while metadb.get_run_state(status.run_id)["error"] is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert metadb.get_run_state(status.run_id)["error"] is None
    cleared_at = _wait_run_state_updated_at_stable(status.run_id)
    time.sleep(0.08)  # several 10ms same-state polls must not rewrite the full RunState document
    with metadb.session() as session:
        assert session.get(metadb.RunState, status.run_id).updated_at == cleared_at

    _complete(store, client, status)
    assert _wait(runner, status.run_id).status == "done"


def test_jobs_stall_clock_uses_successful_control_observations_not_error_writes(jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_control_clock_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    old = metadb._now() - datetime.timedelta(minutes=5)

    with metadb.session() as session:
        state = session.get(metadb.RunState, status.run_id)
        job = session.get(metadb.RunBackendJob, status.run_id)
        state.updated_at = old
        job.updated_at = old
        job.last_control_observed_at = metadb._now()
    assert metadb.run_stalled(status.run_id, 60) is False

    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).last_control_observed_at = old
    runner._persist_jobs_live_error(status, "repeated control failure")
    assert metadb.run_stalled(status.run_id, 60) is True, (
        "fresh error text must not make a dead control plane look healthy"
    )

    assert metadb.note_backend_control_observed(status.run_id, ref.attempt_id, 0) is True
    assert metadb.run_stalled(status.run_id, 60) is False
    observed = metadb.backend_job(status.run_id)["last_control_observed_at"]
    assert metadb.note_backend_control_observed(status.run_id, ref.attempt_id) is False
    assert metadb.backend_job(status.run_id)["last_control_observed_at"] == observed
    _retire_inert_test_run(status)


@pytest.mark.parametrize("malformed_kind", ["invalid-json", "invalid-shape"])
def test_malformed_recovery_row_is_durably_visible_without_replay(
        jobs_config, monkeypatch, caplog, malformed_kind):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_malformed_{malformed_kind}_{uuid.uuid4().hex}",
    )
    with metadb.session() as session:
        state = session.get(metadb.RunState, status.run_id)
        state.status = "running"
        state.doc = (
            "{not-json" if malformed_kind == "invalid-json" else json.dumps({
                "run_id": status.run_id,
                "status": "running",
                "per_node": "not-a-list",
            })
        )

    supervised: list[str] = []
    monkeypatch.setattr(
        module.RayRunner, "_ensure_jobs_supervisor",
        lambda _self, run_id: supervised.append(run_id),
    )
    caplog.set_level("WARNING", logger="hub")
    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )

    blocked = recovered.status(status.run_id)
    binding = metadb.backend_job(status.run_id)
    persisted = metadb.get_run_state(status.run_id)
    assert status.run_id not in supervised
    assert blocked.status == "running" and "recovery blocked" in (blocked.error or "")
    assert persisted["status"] == "running" and persisted["error"] == blocked.error
    assert binding["recovery_blocked_reason"] in blocked.error
    assert len(binding["recovery_blocked_reason"]) <= 2000
    assert client.submit_calls == [] and client.stop_calls == []
    warning = next(
        record for record in caplog.records
        if record.getMessage().startswith("ray_jobs_recovery_blocked run_id=")
        and getattr(record, "dataplay_run_id", None) == status.run_id
    )
    assert warning.dataplay_backend == "ray-jobs" and warning.dataplay_error_type

    cancelled = recovered.cancel(status.run_id)
    assert cancelled.status == "running" and "cancellation recorded" in (cancelled.error or "")
    assert metadb.backend_job(status.run_id)["cancel_requested"] is True
    assert client.submit_calls == [] and client.stop_calls == []

    # Repeated restarts retain one bounded diagnosis instead of recursively wrapping it.
    restarted = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    assert (restarted.status(status.run_id).error or "").count("Ray Jobs recovery blocked (") == 1
    assert status.run_id not in supervised
    _retire_inert_test_run(restarted.status(status.run_id))


def test_recovery_blocked_cancel_refreshes_terminal_before_writing_intent(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "map", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "map", "distributed",
        run_id=f"recovery-blocked-terminal-{uuid.uuid4().hex}",
    )
    with metadb.session() as session:
        state = session.get(metadb.RunState, status.run_id)
        state.status, state.doc = "running", "{not-json"

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    assert status.run_id in recovered._recovery_blocked
    assert metadb.backend_job(status.run_id)["cancel_requested"] is False

    _publish_test_backend_done(status)
    final = recovered.cancel(status.run_id)

    assert final.status == "done" and final.error is None
    assert metadb.backend_job(status.run_id)["cancel_requested"] is False
    assert status.run_id not in recovered._recovery_blocked
    assert recovered._settled[status.run_id].is_set()
    assert recovered.status(status.run_id).status == "done"


def test_recovery_blocked_cancel_converges_when_publication_wins_cancel_cas(
        jobs_config, monkeypatch):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "map", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "map", "distributed",
        run_id=f"recovery-blocked-cancel-cas-{uuid.uuid4().hex}",
    )
    with metadb.session() as session:
        state = session.get(metadb.RunState, status.run_id)
        state.status, state.doc = "running", "{not-json"

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    assert status.run_id in recovered._recovery_blocked
    original_request = metadb.request_backend_cancel
    outcomes: list[bool] = []

    def _publish_then_request(run_id: str) -> bool:
        assert run_id == status.run_id
        _publish_test_backend_done(status)
        outcomes.append(original_request(run_id))
        return outcomes[-1]

    monkeypatch.setattr(metadb, "request_backend_cancel", _publish_then_request)
    final = recovered.cancel(status.run_id)

    assert outcomes == [False]
    assert final.status == "done" and final.error is None
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"


def test_staged_effects_recover_from_sql_after_artifacts_and_catalog_change(
        jobs_config, monkeypatch):
    module, deps, original_runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = original_runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_effect_recovery_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)

    real_apply = metadb.catalog_apply_managed_publication
    release_effects = threading.Event()

    def pause_effects(effect_plan):
        if not release_effects.is_set():
            raise ConnectionError("hold staged catalog effect for restart")
        return real_apply(effect_plan)

    monkeypatch.setattr(metadb, "catalog_apply_managed_publication", pause_effects)
    _complete(store, client, status)
    _wait_publication_state(
        status.run_id, "effects_started", runner=original_runner,
    )

    with store.lock:
        store.values[ref.job_uri] = {"corrupt": True}
        store.values.pop(ref.result_uri, None)
    with metadb.session() as session:
        session.get(metadb.RunState, status.run_id).doc = "{malformed"
    deps.catalog = CountingCatalog()  # staged SQL replay must not consult the replacement provider
    control_calls = (len(client.status_calls), len(client.submit_calls), len(client.stop_calls))
    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True)
    _wait_control_calls_stable(client, control_calls)

    release_effects.set()
    final = _wait(recovered, status.run_id)

    assert final.status == "done"
    assert metadb.catalog_get(final.output_uri) is not None
    assert (len(client.status_calls), len(client.submit_calls), len(client.stop_calls)) == control_calls


def test_staged_negative_plan_rejects_canonical_binding_tamper_and_missing_doc(jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"staged-binding-tamper-{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    owner = f"tamper-owner-{uuid.uuid4().hex}"
    assert metadb.request_backend_cancel(status.run_id) is True
    assert metadb.claim_backend_publication(
        status.run_id, ref.attempt_id, owner, 30) == "claimed"
    candidate = status.model_copy(deep=True)
    runner._apply_job_result(candidate, {
        "status": "cancelled", "rows": 0, "outputs": [], "error": None,
    })
    assert metadb.begin_backend_publication_effects(
        status.run_id, ref.attempt_id, owner, candidate.model_dump(),
        None, None, catalog_effects=[], usage_effect=None,
    ) == "started"

    with metadb.session() as session:
        row = session.get(metadb.RunBackendJob, status.run_id)
        valid_publication_doc = row.publication_doc
        staged = json.loads(row.publication_doc)
        staged["terminal_status"]["backend_ref"]["submission_id"] = "wrong-submission"
        row.publication_doc = json.dumps(
            staged, sort_keys=True, separators=(",", ":"), default=str)
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_effects"] is None
    assert "state/doc pairing is invalid" in binding["_recovery_error"]
    with pytest.raises(RuntimeError, match="durable binding"):
        metadb.backend_publication_effects(status.run_id, ref.attempt_id)

    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).publication_doc = None
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_effects"] is None
    assert "effects_started publication" in binding["_recovery_error"]

    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).publication_doc = valid_publication_doc
    assert metadb.finish_backend_publication(
        status.run_id, ref.attempt_id, owner, candidate.model_dump()
    ) is True


def test_recovery_blocked_cancel_converges_when_publication_wins_after_cancel_cas(
        jobs_config, monkeypatch):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "map", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "map", "distributed",
        run_id=f"recovery-blocked-after-cancel-cas-{uuid.uuid4().hex}",
    )
    with metadb.session() as session:
        state = session.get(metadb.RunState, status.run_id)
        state.status, state.doc = "running", "{not-json"

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    assert status.run_id in recovered._recovery_blocked
    original_request = metadb.request_backend_cancel
    outcomes: list[bool] = []

    def _request_then_publish(run_id: str) -> bool:
        assert run_id == status.run_id
        outcomes.append(original_request(run_id))
        _publish_test_backend_done(status)
        return outcomes[-1]

    monkeypatch.setattr(metadb, "request_backend_cancel", _request_then_publish)
    final = recovered.cancel(status.run_id)

    assert outcomes == [True]
    assert final.status == "done" and final.error is None
    assert "cancellation recorded" not in (final.error or "")
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"
    assert binding["cancel_requested"] is True
    assert status.run_id not in recovered._recovery_blocked
    assert recovered._settled[status.run_id].is_set()
    assert client.submit_calls == [] and client.stop_calls == []


def test_postgres_recovery_blocked_caller_converges_after_terminal_publication(
        jobs_config, monkeypatch):
    if metadb.engine().dialect.name != "postgresql":
        pytest.skip("requires a real PostgreSQL metadata database")

    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "map", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "map", "distributed",
        run_id=f"pg-recovery-blocked-terminal-{uuid.uuid4().hex}",
    )
    with metadb.session() as session:
        state = session.get(metadb.RunState, status.run_id)
        state.status, state.doc = "running", "{not-json"

    marked = threading.Event()
    release_caller = threading.Event()
    original_mark = metadb.mark_backend_recovery_blocked

    def _mark_then_pause(*args, **kwargs):
        result = original_mark(*args, **kwargs)
        if str(args[0] if args else kwargs.get("run_id")) != status.run_id:
            return result
        assert result is True
        marked.set()
        if not release_caller.wait(timeout=10):
            raise TimeoutError("recovery caller was not released")
        return result

    monkeypatch.setattr(metadb, "mark_backend_recovery_blocked", _mark_then_pause)
    recovered: list = []
    errors: list[BaseException] = []

    def _recover() -> None:
        try:
            recovered.append(module.RayRunner(
                deps, jobs_client_factory=client, artifact_store=store, recover=True
            ))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    caller = threading.Thread(target=_recover, name="pg-ray-recovery-caller")
    caller.start()
    try:
        assert marked.wait(timeout=5), "recovery marker did not commit before the barrier"
        _publish_test_backend_done(status)
        assert metadb.terminal_run_status(status.run_id) == "done"
        assert metadb.get_run_state(status.run_id)["status"] == "done"
    finally:
        release_caller.set()
    caller.join(timeout=10)

    assert not caller.is_alive() and errors == [] and len(recovered) == 1
    replacement = recovered[0]
    assert status.run_id in replacement._recovery_blocked, (
        "test did not install the stale caller-side diagnostic after terminal publication"
    )
    final = replacement.status(status.run_id)
    assert final.status == "done" and final.error is None
    assert status.run_id not in replacement._recovery_blocked
    assert replacement._settled[status.run_id].is_set()
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"
    assert binding["cancel_requested"] is False


def test_malformed_backend_result_blocks_only_its_own_recovery_row(jobs_config, monkeypatch):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    bad = original.run(
        plan, graph, "write", "distributed",
        run_id=f"bad-result-row-{uuid.uuid4().hex}",
    )
    good = original.run(
        plan, graph, "write", "distributed",
        run_id=f"good-result-row-{uuid.uuid4().hex}",
    )
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, bad.run_id).result_doc = "{not-json"

    supervised: list[str] = []
    monkeypatch.setattr(
        module.RayRunner, "_ensure_jobs_supervisor",
        lambda _self, run_id: supervised.append(run_id),
    )
    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )

    assert good.run_id in supervised and bad.run_id not in supervised
    assert recovered.status(bad.run_id).error == (
        "Ray Jobs recovery blocked (code=recovery_blocked,type=ValueError)"
    )
    assert recovered.status(good.run_id).status == "queued"
    _retire_inert_test_run(recovered.status(bad.run_id))
    _retire_inert_test_run(recovered.status(good.run_id))


def test_ray_jobs_multi_sink_usage_is_one_event_per_run(jobs_config):
    catalog = CountingCatalog()
    _module, _deps, runner, _client, _store = _runner(jobs_config, catalog=catalog)
    left_source = "s3://shared/left-parent.parquet"
    right_source = "s3://shared/right-parent.parquet"
    graph = Graph.model_validate({
        "id": f"canvas_{uuid.uuid4().hex}", "version": 1,
        "nodes": [
            {"id": "left", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": left_source}}},
            {"id": "right", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": right_source}}},
            {"id": "join", "type": "join", "position": {"x": 0, "y": 0},
             "data": {"config": {"how": "inner", "on": "id"}}},
            {"id": "write-a", "type": "write", "position": {"x": 0, "y": 0},
             "data": {"config": {"filename": "a.parquet"}}},
            {"id": "write-b", "type": "write", "position": {"x": 0, "y": 0},
             "data": {"config": {"filename": "b.parquet"}}},
        ],
        "edges": [
            {"id": "left-join", "source": "left", "target": "join",
             "data": {"wire": "dataset", "inputName": "left"}},
            {"id": "right-join", "source": "right", "target": "join",
             "data": {"wire": "dataset", "inputName": "right"}},
            {"id": "a", "source": "join", "target": "write-a", "data": {"wire": "dataset"}},
            {"id": "b", "source": "join", "target": "write-b", "data": {"wire": "dataset"}},
        ],
    })
    result = {"outputs": [
        {"step_id": "write-a", "name": "a", "uri": "s3://shared/a.parquet"},
        {"step_id": "write-b", "name": "b", "uri": "s3://shared/b.parquet"},
    ]}

    runner._register_outputs(graph, result, {"attempt_id": "attempt-multi"})
    runner._register_outputs(graph, result, {"attempt_id": "attempt-multi"})

    assert len(catalog.calls) == 2, "each output remains independently idempotent"
    assert len(catalog.usage_calls) == 1
    assert catalog.usage_calls[0]["idempotency_key"] == "ray-jobs:attempt-multi"
    assert set(catalog.usage_calls[0]["parents"]) == {left_source, right_source}
    assert all(set(call["parents"]) == {left_source, right_source} for call in catalog.calls)


def test_ray_jobs_failure_never_shares_rotated_credentials_or_remote_text(
        jobs_config, monkeypatch, caplog):
    old_secret = "old-job-secret-value"
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "rotated-secret-value")
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_failure_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    client.put(
        ref.submission_id, "FAILED",
        message=f"entrypoint exited: AWS_SECRET_ACCESS_KEY={old_secret}",
        logs=f"traceback: {old_secret}",
        metadata=_workload_metadata(status),
    )

    final = _wait(runner, status.run_id)

    assert final.status == "failed"
    assert final.error == "Ray execution failed (RemoteJobFailed; code=ray_execution_failed)"
    persisted = metadb.get_run_state(status.run_id)
    assert old_secret not in json.dumps(persisted) and old_secret not in caplog.text
    assert client.log_calls == []
    assert runner.logs(status.run_id) == (
        "Ray diagnostics are available only through protected operator tooling"
    )
    assert client.log_calls == []
    with metadb.session() as session:
        assert session.get(
            metadb.CatalogPublicationEvent, f"ray-jobs:{ref.attempt_id}:write"
        ) is None


@pytest.mark.parametrize("failure_site", ["status", "list"])
def test_ray_jobs_discovery_exceptions_never_share_rotated_credentials(
        jobs_config, monkeypatch, caplog, failure_site):
    old_secret = f"retired-{failure_site}-control-secret"
    raw_phrase = f"raw {failure_site} response body"
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_{failure_site}_exception_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    deadline = time.monotonic() + 2
    while runner.status(status.run_id).status != "running" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runner.status(status.run_id).status == "running"

    original_status = client.get_job_status
    original_list = client.list_jobs

    def _secret_status_error(_submission_id):
        raise RuntimeError(f"{raw_phrase}: token={old_secret}")

    def _safe_status_error(_submission_id):
        raise ConnectionError("status request unavailable")

    def _secret_list_error():
        raise LookupError(f"{raw_phrase}: token={old_secret}")

    def _safe_list_error():
        raise ConnectionError("list request unavailable")

    monkeypatch.setattr(
        client, "get_job_status",
        _secret_status_error if failure_site == "status" else _safe_status_error,
    )
    monkeypatch.setattr(
        client, "list_jobs",
        _safe_list_error if failure_site == "status" else _secret_list_error,
    )
    caplog.set_level("WARNING", logger="hub")

    live_error = persisted_error = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        live_error = runner.status(status.run_id).error
        persisted = metadb.get_run_state(status.run_id)
        persisted_error = persisted.get("error") if persisted else None
        if live_error and persisted_error and "code=status_control_unavailable" in live_error:
            break
        time.sleep(0.002)

    monkeypatch.setattr(client, "get_job_status", original_status)
    monkeypatch.setattr(client, "list_jobs", original_list)
    _complete(store, client, status)
    assert _wait(runner, status.run_id).status == "done"

    expected = (
        "Ray status/control plane unavailable; retrying "
        "(code=status_control_unavailable,type=RuntimeError)"
    )
    assert live_error == expected
    assert persisted_error == expected
    shared = json.dumps({"live": live_error, "persisted": persisted_error}) + caplog.text
    assert old_secret not in shared
    assert raw_phrase not in shared


def test_ray_jobs_fails_before_submit_without_baked_code_or_shared_io(jobs_config, monkeypatch):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    monkeypatch.delenv("DP_RAY_JOBS_CODE_REF")
    with pytest.raises(RuntimeError, match="DP_RAY_JOBS_CODE_REF"):
        runner.run(plan, graph, "write", "distributed", run_id=f"run_jobs_bad_code_{uuid.uuid4().hex}")
    assert client.submit_calls == []

    monkeypatch.setenv("DP_RAY_JOBS_CODE_REF", "sha256:dp-ray-test-image")
    local_graph = _graph(source="/host-only/input.parquet")
    local_plan = compile_plan(local_graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    with pytest.raises(RuntimeError, match="only shared object-store"):
        runner.run(local_plan, local_graph, "write", "distributed",
                   run_id=f"run_jobs_bad_io_{uuid.uuid4().hex}")
    assert client.submit_calls == []


def test_ray_jobs_atomic_handoff_is_recoverable_without_status_hook(jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner.on_status = None
    runner._ensure_jobs_supervisor = lambda _run_id: None

    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_atomic_{uuid.uuid4().hex}")

    active = {doc[1]["run_id"] for doc in metadb.active_backend_jobs("ray-jobs")}
    assert status.run_id in active
    assert metadb.get_run_state(status.run_id)["backend_ref"]["attempt_id"] == status.backend_ref.attempt_id
    _retire_inert_test_run(status)


def test_sql_bound_envelope_recovers_crash_before_artifact_materialization(jobs_config):
    store = FlakyJobArtifacts()
    store.fail_job_writes = 10_000
    module, deps, original, client, store = _runner(jobs_config, store=store)
    graph = _graph()
    graph.nodes[0].data["config"]["note"] = "数据🙂"
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_pre_materialize_crash_{uuid.uuid4().hex}"

    status = original.run(plan, graph, "write", "distributed", run_id=run_id)
    assert status.status == "queued"
    assert original.runs[run_id] is status and deps.run_index[run_id] is original

    binding = metadb.backend_job(run_id)
    payload = metadb.backend_job_artifact_payload(run_id)
    assert binding and payload and binding["submission_state"] == "queued"
    durable_job = json.loads(payload)
    assert durable_job["run_id"] == run_id and durable_job["attempt_id"] == binding["attempt_id"]
    assert durable_job["graph"]["nodes"][0]["data"]["config"]["note"] == "数据🙂"
    serialized = payload.decode()
    assert "created_by" not in serialized and "auth_canvas_id" not in serialized
    assert "AWS_SECRET_ACCESS_KEY" not in serialized
    assert binding["job_uri"] not in store.values and client.submit_calls == []
    deadline = time.monotonic() + 2
    while "artifact unavailable" not in (original.status(run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert original.status(run_id).status in ("queued", "running")
    assert client.submit_calls == []

    store.fail_job_writes = 0
    _wait_submitted(client, binding["submission_id"])
    restored = store.read(binding["job_uri"])
    assert module.canonical_json(restored) == payload
    _complete(store, client, original.status(run_id))
    assert _wait(original, run_id).status == "done"


def test_status_retries_supervisor_after_thread_start_failure(jobs_config, monkeypatch):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original_start = threading.Thread.start
    failed = False

    def _fail_first_jobs_supervisor(thread):
        nonlocal failed
        if not failed and thread.name.startswith("dp-ray-job-"):
            failed = True
            raise RuntimeError("forced thread start failure")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", _fail_first_jobs_supervisor)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_thread_retry_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None and failed is True
    assert "code=supervisor_start_failed" in (status.error or "")
    assert status.run_id not in runner._supervising
    assert metadb.backend_job(status.run_id) is not None

    runner.status(status.run_id)
    _wait_submitted(client, ref.submission_id)
    _complete(store, client, status)
    assert _wait(runner, status.run_id).status == "done"


def test_bound_sql_artifact_recovers_before_first_submit(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_pre_submit_crash_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None and ref.job_uri not in store.values
    assert metadb.backend_job_artifact_payload(status.run_id) is not None
    assert client.submit_calls == []

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    _wait_submitted(client, ref.submission_id)
    assert metadb.backend_job_artifact_payload(status.run_id) == module.canonical_json(
        store.read(ref.job_uri)
    )
    assert [call["submission_id"] for call in client.submit_calls] == [ref.submission_id]
    _complete(store, client, recovered.status(status.run_id))
    assert _wait(recovered, status.run_id).status == "done"


def test_ray_jobs_terminal_artifact_wins_before_missing_job_replay(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_result_first_{uuid.uuid4().hex}")
    _write_success(store, status)

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)

    assert _wait(recovered, status.run_id).status == "done"
    final = recovered.status(status.run_id)
    assert client.submit_calls == [] and metadb.catalog_get(final.output_uri) is not None


def test_cancel_preserves_trusted_success_after_submitted_metadata_disappears(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_result_cancel_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    assert metadb.note_backend_submission_observed(status.run_id, ref.attempt_id) is True
    _write_success(store, status, rows=9)
    assert metadb.request_backend_cancel(status.run_id) is True

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    final = _wait(recovered, status.run_id)

    assert final.status == "done" and final.total_rows == 9
    assert all(call.get("submission_id") != ref.submission_id for call in client.submit_calls)
    assert ref.submission_id not in client.stop_calls


def test_ray_jobs_replays_same_attempt_after_authoritative_metadata_loss(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_metadata_loss_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    with client.lock:
        client.jobs.pop(ref.submission_id)

    deadline = time.monotonic() + 2
    while len(client.submit_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [call["submission_id"] for call in client.submit_calls] == [ref.submission_id, ref.submission_id]
    _complete(store, client, status)
    assert _wait(runner, status.run_id).status == "done"


def test_ray_jobs_done_result_wins_stop_race(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_stop_race_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    _write_success(store, status)
    client.set_status(ref.submission_id, "STOPPED")

    assert _wait(runner, status.run_id).status == "done"


def test_ray_jobs_storage_outage_after_succeeded_never_publishes_false_failure(jobs_config):
    store = FlakyResultArtifacts()
    _module, deps, runner, client, store = _runner(jobs_config, store=store)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_store_outage_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    _write_success(store, status)
    store.fail_result_reads = True
    client.set_status(ref.submission_id, "SUCCEEDED")

    deadline = time.monotonic() + 1
    while "temporarily unavailable" not in (runner.status(status.run_id).error or "") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.status(status.run_id).status in ("queued", "running")
    assert metadb.backend_job(status.run_id)["publication_state"] == "pending"

    store.fail_result_reads = False
    assert _wait(runner, status.run_id).status == "done"


def test_result_store_outage_during_cancel_stop_does_not_bury_a_success(jobs_config):
    # After a cancel reaches STOPPED, a transient result-store read failure is "unknown", not "absent":
    # the supervisor must not publish a terminal cancellation over a committed success. Once the store
    # recovers, the genuine success wins the cancel race.
    store = CountedFlakyArtifacts()
    _module, deps, runner, client, store = _runner(jobs_config, store=store)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_cancel_store_outage_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _materialize_bound_job(runner, status)  # job artifact present in the store
    _write_success(store, status)            # a committed, hash-bound success result is present
    client.put(ref.submission_id, "RUNNING", metadata=_workload_metadata(status))
    assert metadb.note_backend_submission_observed(status.run_id, ref.attempt_id) is True
    assert metadb.request_backend_cancel(status.run_id) is True
    runner._cancel[status.run_id].set()
    # Loop 1's cancel path sets state=STOPPED on the first (failed) job read; then the inter-loop result
    # read fails once — the exact window where a swallowed error would bury the success as cancelled.
    store.job_read_failures = 1
    store.result_read_failures = 1

    runner._supervise_jobs(status.run_id)

    final = runner.status(status.run_id)
    assert final.status == "done"
    assert store.result_read_failures == 0
    assert metadb.backend_job(status.run_id)["publication_state"] == "published"


def test_ray_jobs_configuration_drift_never_resubmits_to_another_cluster(jobs_config, monkeypatch):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_drift_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _materialize_bound_job(original, status)
    monkeypatch.setenv("DP_RAY_JOBS_CLUSTER_REF", "different-cluster")

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    deadline = time.monotonic() + 2
    while "changed" not in (recovered.status(status.run_id).error or "") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recovered.status(status.run_id).status == "queued"
    assert all(call.get("submission_id") != ref.submission_id for call in client.submit_calls)
    assert ref.submission_id not in client.stop_calls

    monkeypatch.setenv("DP_RAY_JOBS_CLUSTER_REF", "test-ray-cluster")
    _wait_submitted(client, ref.submission_id)
    _complete(store, client, status)
    assert _wait(recovered, status.run_id).status == "done"


def test_ray_jobs_tampered_job_is_stopped_before_failed_publication(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_tamper_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    client.put(ref.submission_id, "RUNNING", metadata=_workload_metadata(status))
    job = _materialize_bound_job(original, status)
    job["workspace"] = "/attacker-controlled"
    store.write(ref.job_uri, job)

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    final = _wait(recovered, status.run_id)

    assert final.status == "failed"
    assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    assert client.stop_calls == [ref.submission_id]
    assert metadb.backend_job(status.run_id)["publication_state"] == "published"


def test_ray_jobs_rejects_partitioned_remote_sink_and_durable_region_claim(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    graph.nodes[-1].data["config"]["partitionBy"] = "customer_id"
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)

    with pytest.raises(RuntimeError, match="partitionBy"):
        runner.run(plan, graph, "write", "distributed",
                   run_id=f"run_jobs_partition_{uuid.uuid4().hex}")
    with pytest.raises(RuntimeError, match="whole-graph"):
        runner.run_unit(graph, "write", "s3://shared/region/out.parquet")
    assert runner.place(type("Req", (), {"labels": {"engine": "ray"}})()) is None
    assert client.submit_calls == []


def test_ray_jobs_whole_graph_admission_is_separate_from_region_placement(jobs_config):
    from hub.routers.runs import _route_by_capability

    _module, deps, runner, client, _store = _runner(jobs_config)
    deps.runners.insert(0, runner)
    local = deps.runner
    plain = _graph()
    pinned = _graph()
    pinned.nodes[1].data["config"]["requires"] = {"labels": {"engine": "ray"}}

    assert _route_by_capability(deps, runner, plain) is runner  # selected Ray stays selected
    assert _route_by_capability(deps, local, pinned) is runner  # explicit pin becomes one durable Job
    assert _route_by_capability(deps, local, plain) is local    # no pin keeps the selected fallback
    assert runner.place(ResourceSpec(labels={"engine": "ray"})) is None  # regions remain unclaimed

    disconnected = _graph()
    disconnected.nodes.append(GraphNode.model_validate({
        "id": "other", "type": "transform", "position": {"x": 0, "y": 0},
        "data": {"config": {
            "mode": "map", "code": "def fn(row): return row",
            "requires": {"labels": {"engine": "ray"}},
        }},
    }))
    assert _route_by_capability(deps, local, disconnected, "write") is local

    unsupported = _graph()
    unsupported.nodes[1].data["config"]["requires"] = {"labels": {"engine": "ray"}}
    unsupported.nodes[-1].data["config"]["partitionBy"] = "customer_id"
    routed = _route_by_capability(deps, local, unsupported)
    plan = compile_plan(unsupported, "write", deps.registry, deps.node_specs, deps.node_ir)
    with pytest.raises(RuntimeError, match="partitionBy"):
        routed.run(plan, unsupported, "write", "distributed",
                   run_id=f"run_jobs_pinned_unsupported_{uuid.uuid4().hex}")
    assert client.submit_calls == []


def test_start_run_prebinds_authorized_identity_before_ray_side_effects(
        jobs_config, monkeypatch):
    from hub.routers import runs as runs_router

    _module, deps, runner, client, store = _runner(jobs_config)
    deps.runners.insert(0, runner)
    uid = f"ray-owner-{uuid.uuid4().hex}"
    graph = _graph()
    graph.nodes[1].data["config"]["requires"] = {"labels": {"engine": "ray"}}
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Ray Owner"))
        session.flush()
        session.add(metadb.Canvas(
            id=graph.id, owner_id=uid, name="authorized", version=1, doc="{}"
        ))
    monkeypatch.setattr(runs_router.auth, "auth_enabled", lambda: True)
    monkeypatch.setattr(runs_router, "_cone_size", lambda *_args, **_kwargs: (None, None, {}))

    observed: list[tuple[str, tuple[str | None, str | None], dict]] = []
    write = store.write

    def _write(uri, value):
        if uri.endswith("/job.dpjob"):
            observed.append(("artifact", metadb.run_auth(value["run_id"]), dict(value)))
        return write(uri, value)

    submit = client.submit_job

    def _submit(**kwargs):
        run_id = kwargs["metadata"]["dataplay_run_id"]
        observed.append(("submit", metadb.run_auth(run_id), dict(kwargs["metadata"])))
        return submit(**kwargs)

    monkeypatch.setattr(store, "write", _write)
    monkeypatch.setattr(client, "submit_job", _submit)

    status, owner = runs_router.start_run(deps, graph, "write", uid)
    assert owner is runner
    _wait_submitted(client, status.backend_ref.submission_id)
    assert [kind for kind, _auth, _doc in observed] == ["artifact", "submit"]
    assert all(auth == (uid, graph.id) for _kind, auth, _doc in observed)
    artifact, metadata = observed[0][2], observed[1][2]
    assert "created_by" not in artifact and "auth_canvas_id" not in artifact
    assert "dataplay_created_by" not in metadata and "dataplay_auth_canvas_id" not in metadata
    assert uid not in json.dumps(artifact) and uid not in json.dumps(metadata)

    _complete(store, client, status)
    assert _wait(runner, status.run_id).status == "done"


def test_ray_jobs_refuses_artifact_allocation_without_prebound_identity(jobs_config):
    module, deps, _runner_with_test_identity, client, store = _runner(jobs_config)

    class UnboundRunner(module.RayRunner):
        def _source_unsupported_reason(self, *_args):
            return None

    runner = UnboundRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=False
    )
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"unbound-ray-run-{uuid.uuid4().hex}"

    with pytest.raises(RuntimeError, match="live unbound run preallocation"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    assert store.values == {} and client.submit_calls == []
    assert metadb.backend_job(run_id) is None
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == run_id
        ))) == []


def test_oversized_sql_envelope_fails_before_binding_or_artifact_write(
        jobs_config, monkeypatch):
    from hub import job_artifacts

    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"oversized-ray-run-{uuid.uuid4().hex}"
    monkeypatch.setattr(job_artifacts, "JOB_SQL_ENVELOPE_MAX_BYTES", 128)

    with pytest.raises(ValueError, match="durable SQL job envelope"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    assert metadb.backend_job(run_id) is None
    assert store.values == {} and client.submit_calls == []


def test_canvas_delete_is_blocked_while_external_job_is_active(jobs_config):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    with metadb.session() as session:
        session.add(metadb.Canvas(id=graph.id, owner_id=metadb.DEFAULT_USER_ID,
                                  name="active", version=1, doc="{}"))
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_delete_{uuid.uuid4().hex}")

    with pytest.raises(metadb.ActiveBackendJobsError, match="cancel it"):
        metadb.delete_canvas_cascade(graph.id)
    metadb.save_run_state(status.run_id, status.model_copy(update={"status": "failed"}).model_dump(),
                          canvas_id=graph.id)
    metadb.delete_canvas_cascade(graph.id)


def test_early_result_never_beats_running_and_corruption_waits_for_stopped(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_early_result_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    _write_success(store, status)
    corrupt = store.read(ref.result_uri)
    corrupt["unexpected"] = "field"
    store.write(ref.result_uri, corrupt)

    def _accepted_but_live(submission_id: str):
        client.stop_calls.append(submission_id)
        return True

    client.stop_job = _accepted_but_live
    deadline = time.monotonic() + 1
    while not client.stop_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.status(status.run_id).status in ("queued", "running")
    assert metadb.backend_job(status.run_id)["publication_state"] == "pending"

    client.set_status(ref.submission_id, "STOPPED")
    final = _wait(runner, status.run_id)
    assert final.status == "failed"
    assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"


@pytest.mark.parametrize("remote_status", ["SUCCEEDED", "STOPPED"])
def test_terminal_remote_result_corruption_uses_durable_quarantine(
        jobs_config, remote_status):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_terminal_corrupt_result_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(original, status)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    _write_success(store, status)
    corrupt = store.read(ref.result_uri)
    corrupt["unexpected"] = "field"
    store.write(ref.result_uri, corrupt)
    client.put(ref.submission_id, remote_status, metadata=_workload_metadata(status))

    original_get_status = client.get_job_status
    original_list_jobs = client.list_jobs
    get_status_calls = 0
    list_failures = 0
    status_outage_injected = False
    fail_next_list = False

    def transient_get_status(submission_id: str):
        nonlocal get_status_calls, status_outage_injected, fail_next_list
        get_status_calls += 1
        binding = metadb.backend_job(status.run_id)
        if binding["quarantine_reason"] is not None and not status_outage_injected:
            status_outage_injected = True
            fail_next_list = True
            raise ConnectionError("transient status outage during quarantine")
        return original_get_status(submission_id)

    def transient_list_jobs():
        nonlocal list_failures, fail_next_list
        if fail_next_list:
            fail_next_list = False
            list_failures += 1
            raise ConnectionError("transient list outage during quarantine")
        return original_list_jobs()

    client.get_job_status = transient_get_status
    client.list_jobs = transient_list_jobs

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    # Poll SQL directly: calling runner.status() would itself restart a missing supervisor and hide a
    # liveness regression in the supervisor's normal retry tail.
    deadline = time.monotonic() + 2
    while metadb.backend_job(status.run_id)["publication_state"] != "published" \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert metadb.backend_job(status.run_id)["publication_state"] == "published"
    final = recovered.status(status.run_id)

    assert final.status == "failed"
    assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "published"
    assert "artifact_contract_invalid" in binding["quarantine_reason"]
    assert status_outage_injected and get_status_calls >= 3 and list_failures == 1
    assert client.submit_calls == [] and client.stop_calls == []
    with metadb.session() as session:
        assert session.get(metadb.CatalogEntry, physical_uri) is None
        assert session.get(metadb.ObjectAttempt, physical_uri).state == "abandoned"


def test_corrupt_result_flushed_during_cancel_stop_uses_durable_quarantine(jobs_config):
    # A driver that flushes an invalid terminal result inside the cancel stop window must go through
    # the same durable quarantine as any other corrupt result, not publish as a plain remote failure.
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_cancel_corrupt_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)

    original_stop = client.stop_job

    def stop_and_flush_corrupt(submission_id: str):
        _write_success(store, status)
        corrupt = store.read(ref.result_uri)
        corrupt["unexpected"] = "field"
        store.write(ref.result_uri, corrupt)
        return original_stop(submission_id)

    client.stop_job = stop_and_flush_corrupt

    runner.cancel(status.run_id)
    final = _wait(runner, status.run_id)
    assert final.status == "failed"
    assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    binding = metadb.backend_job(status.run_id)
    assert "artifact_contract_invalid" in binding["quarantine_reason"]


def test_trusted_result_wins_cancel_without_local_jobs_config_or_ray_metadata(jobs_config, monkeypatch):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_no_config_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _materialize_bound_job(original, status)
    _write_success(store, status)
    assert metadb.request_backend_cancel(status.run_id) is True
    for key in (
        "DP_RAY_JOBS_ADDRESS", "DP_RAY_JOBS_ENTRYPOINT", "DP_RAY_JOBS_CODE_REF",
        "DP_RAY_JOBS_CLUSTER_REF", "DP_RAY_JOBS_WORKSPACE", "DP_RAY_JOBS_DATA_DIR",
        "DP_STORAGE_URL", "DP_RAY_JOBS_ARTIFACT_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    final = _wait(recovered, status.run_id)
    assert final.status == "done"
    assert client.stop_calls == [] and client.submit_calls == []
    assert metadb.backend_job(status.run_id)["cancel_requested"] is True


def test_cancel_stops_from_sql_while_job_artifact_storage_is_unavailable(jobs_config):
    store = FlakyJobArtifacts()
    module, deps, original, client, store = _runner(jobs_config, store=store)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_missing_cancel_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    store.fail_job_reads = True
    client.put(ref.submission_id, "RUNNING", metadata=_workload_metadata(status))
    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)

    deadline = time.monotonic() + 1
    while "artifact unavailable" not in (recovered.status(status.run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recovered.cancel(status.run_id).status in ("queued", "running")
    assert client.stop_calls == [ref.submission_id]
    assert client.submit_calls == []
    store.fail_job_reads = False
    assert _wait(recovered, status.run_id).status == "cancelled"


def test_jobs_semantic_environment_is_frozen_while_credentials_rotate(jobs_config, monkeypatch):
    module, _deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    monkeypatch.setenv("DP_MEMORY_LIMIT", "4GB")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "old-secret")
    ref_a, job_a = runner._make_jobs_artifacts("semantic-run", graph, "write")
    assert job_a["semantic_env"]["DP_MEMORY_LIMIT"] == "4GB"
    assert job_a["semantic_env"]["DP_RAY_GPU_BATCH_ROWS"] == str(module._GPU_BATCH_ROWS_DEFAULT)
    assert "AWS_SECRET_ACCESS_KEY" not in job_a["semantic_env"]

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "rotated-secret")
    ref_b, job_b = runner._make_jobs_artifacts("semantic-run", graph, "write")
    assert ref_a["attempt_id"] == ref_b["attempt_id"]
    assert job_a["envelope_sha256"] == job_b["envelope_sha256"]
    launch_env = module._ray_jobs_env(job_a)
    assert launch_env["DP_MEMORY_LIMIT"] == "4GB"
    assert launch_env["AWS_SECRET_ACCESS_KEY"] == "rotated-secret"

    monkeypatch.setenv("DP_RAY_GPU_BATCH_ROWS", "8192")
    ref_c, job_c = runner._make_jobs_artifacts("semantic-run", graph, "write")
    assert ref_c["attempt_id"] != ref_a["attempt_id"]
    assert job_c["semantic_env"]["DP_RAY_GPU_BATCH_ROWS"] == "8192"


def test_unsupported_job_contract_is_quarantined_without_workload_replay(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(
        plan, graph, "write", "distributed",
        run_id=f"unsupported-contract-{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(original, status)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    job["contract_version"] = 2
    store.write(ref.job_uri, job)
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).job_doc = (
            module.canonical_json(job).decode("utf-8")
        )
    client.put(ref.submission_id, "RUNNING", metadata=_workload_metadata(status))

    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )
    final = _wait(recovered, status.run_id)

    assert final.status == "failed"
    assert final.error == "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
    assert client.submit_calls == [] and client.stop_calls == [ref.submission_id]
    with metadb.session() as session:
        assert session.get(metadb.ObjectAttempt, physical_uri).state == "abandoned"


def test_jobs_whole_graph_uses_target_cone_resources_and_forwards_ray_options(jobs_config, monkeypatch):
    monkeypatch.setenv("DP_RAY_GPUS", "2")
    monkeypatch.setenv("DP_RAY_GPU_TYPE", "a100")
    monkeypatch.setenv("DP_RAY_LABELS", "pool=a100")
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    graph.nodes[1].data["config"]["requires"] = {
        "gpuType": "a100", "labels": {"engine": "ray", "pool": "a100"},
    }
    graph.nodes.append(Graph.model_validate({
        "id": "unused", "version": 1,
        "nodes": [{"id": "unused", "type": "transform", "position": {"x": 0, "y": 0},
                   "data": {"config": {"mode": "map", "code": "def fn(row): return row",
                                       "requires": {"gpu": 99, "gpuType": "h100"}}}}],
        "edges": [],
    }).nodes[0])
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None

    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_resources_{uuid.uuid4().hex}")
    job = _materialize_bound_job(runner, status)
    assert job["requires"]["gpu_type"] == "a100"
    assert job["requires"]["labels"] == {"engine": "ray", "pool": "a100"}
    assert module._ray_opts(job["requires"]) == {
        "num_gpus": 1.0, "accelerator_type": "A100", "resources": {"a100": 0.001},
    }
    assert client.submit_calls == []
    _retire_inert_test_run(status)

    conflict = _graph()
    conflict.nodes[0].data["config"]["requires"] = {"gpuType": "h100"}
    conflict.nodes[1].data["config"]["requires"] = {"gpuType": "a100"}
    conflict_plan = compile_plan(conflict, "write", deps.registry, deps.node_specs, deps.node_ir)
    conflicted = runner.run(conflict_plan, conflict, "write", "distributed",
                            run_id=f"run_jobs_gpu_conflict_{uuid.uuid4().hex}")
    assert conflicted.status == "failed" and "multiple GPU types" in (conflicted.error or "")

    sort_graph = _graph()
    sort_graph.nodes[1].type = "sort"
    sort_graph.nodes[1].data["config"] = {
        "by": "id", "requires": {"gpuType": "a100", "labels": {"engine": "ray", "pool": "a100"}},
    }
    sort_plan = compile_plan(sort_graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    rejected = runner.run(sort_plan, sort_graph, "write", "distributed",
                          run_id=f"run_jobs_sort_{uuid.uuid4().hex}")
    assert rejected.status == "failed" and "sort cannot honor" in (rejected.error or "")


def test_backend_publication_atomically_updates_state_and_history(jobs_config, monkeypatch):
    graph = _graph()
    with metadb.session() as session:
        session.add(metadb.Canvas(id=graph.id, owner_id=metadb.DEFAULT_USER_ID,
                                  name="history", version=1, doc="{}"))
    run_id = f"atomic_publish_{uuid.uuid4().hex}"
    status_doc = {"run_id": run_id, "status": "running", "target_node_id": "write", "per_node": []}
    ref = {
        "backend": "ray-jobs", "cluster_ref": "cluster", "attempt_id": "attempt",
        "submission_id": f"submission-{uuid.uuid4().hex}", "job_uri": "s3://b/job",
        "result_uri": "s3://b/result", "code_ref": "sha256:test",
        "control_address": "http://ray:8265",
    }
    _preallocate_backend_test_run(run_id)
    metadb.bind_backend_job(run_id, ref, status_doc, canvas_id=graph.id)
    assert metadb.claim_backend_publication(run_id, "attempt", "owner", 10) == "claimed"
    result = _stage_raw_test_backend_failure(
        run_id, ref, "owner", status_doc, total_rows=7)
    assert metadb.finish_backend_publication(run_id, "attempt", "owner", result) is True
    assert metadb.backend_job(run_id)["publication_state"] == "published"
    assert metadb.get_run_state(run_id)["status"] == "failed"
    history = metadb.list_runs(graph.id)
    assert len(history) == 1 and history[0]["runId"] == run_id and history[0]["rows"] == 7

    rollback_id = f"atomic_rollback_{uuid.uuid4().hex}"
    rollback_ref = {**ref, "submission_id": f"submission-{uuid.uuid4().hex}"}
    _preallocate_backend_test_run(rollback_id)
    metadb.bind_backend_job(rollback_id, rollback_ref, {**status_doc, "run_id": rollback_id}, canvas_id=graph.id)
    assert metadb.claim_backend_publication(rollback_id, "attempt", "owner", 10) == "claimed"
    rollback_result = _stage_raw_test_backend_failure(
        rollback_id, rollback_ref, "owner", {**status_doc, "run_id": rollback_id},
        total_rows=7,
    )
    monkeypatch.setattr(metadb, "_upsert_run_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("history write failed")
    ))
    with pytest.raises(RuntimeError, match="history write failed"):
        metadb.finish_backend_publication(rollback_id, "attempt", "owner", rollback_result)
    assert metadb.backend_job(rollback_id)["publication_state"] == "effects_started"
    assert metadb.get_run_state(rollback_id)["status"] == "running"


def test_backend_publication_prunes_terminal_state_and_binding_but_keeps_history(jobs_config, monkeypatch):
    graph = _graph()
    with metadb.session() as session:
        session.add(metadb.Canvas(id=graph.id, owner_id=metadb.DEFAULT_USER_ID,
                                  name="retention", version=1, doc="{}"))
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 2)
    run_ids = []
    refs = []
    for index in range(3):
        run_id = f"retained_history_{index}_{uuid.uuid4().hex}"
        run_ids.append(run_id)
        status_doc = {"run_id": run_id, "status": "running", "target_node_id": "write", "per_node": []}
        ref = {
            "backend": "ray-jobs", "cluster_ref": "cluster", "attempt_id": f"attempt-{index}",
            "submission_id": f"submission-{uuid.uuid4().hex}", "job_uri": f"s3://b/{index}/job",
            "result_uri": f"s3://b/{index}/result", "code_ref": "sha256:test",
            "control_address": "http://ray:8265",
        }
        refs.append(ref)
        _preallocate_backend_test_run(run_id)
        metadb.bind_backend_job(run_id, ref, status_doc, canvas_id=graph.id)
        assert metadb.claim_backend_publication(run_id, ref["attempt_id"], "owner", 10) == "claimed"
        result = _stage_raw_test_backend_failure(
            run_id, ref, "owner", status_doc, total_rows=index + 1)
        assert metadb.finish_backend_publication(
            run_id, ref["attempt_id"], "owner", result,
        ) is True
        time.sleep(0.002)

    assert metadb.get_run_state(run_ids[0]) is None
    assert metadb.backend_job(run_ids[0]) is None
    assert all(metadb.get_run_state(run_id)["status"] == "failed" for run_id in run_ids[1:])
    assert all(metadb.backend_job(run_id)["publication_state"] == "published" for run_id in run_ids[1:])
    history_ids = {row["runId"] for row in metadb.list_runs(graph.id, limit=10)}
    assert set(run_ids) <= history_ids, "run_records survive terminal status/backend retention pruning"

    # A duplicate supervisor can finish after terminal detail was pruned. The compact permanent fence,
    # not optional/bounded history, prevents stale status or a duplicate bind from resurrecting it.
    metadb.save_run_state(run_ids[0], {
        "run_id": run_ids[0], "status": "running", "per_node": [], "error": "stale supervisor",
    })
    assert metadb.get_run_state(run_ids[0]) is None
    with pytest.raises(metadb.TerminalRunIdError, match="already terminal"):
        _preallocate_backend_test_run(run_ids[0])


def test_terminal_fence_survives_ad_hoc_pruning_without_run_history(jobs_config, monkeypatch):
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 0)
    run_id = f"adhoc_terminal_fence_{uuid.uuid4().hex}"
    ref = {
        "backend": "ray-jobs", "cluster_ref": "cluster", "attempt_id": "attempt",
        "submission_id": f"submission-{uuid.uuid4().hex}", "job_uri": "s3://b/job",
        "result_uri": "s3://b/result", "code_ref": "sha256:test",
        "control_address": "http://ray:8265",
    }
    status = {"run_id": run_id, "status": "running", "per_node": []}
    _preallocate_backend_test_run(run_id)
    metadb.bind_backend_job(run_id, ref, status)
    assert metadb.claim_backend_publication(run_id, "attempt", "owner", 10) == "claimed"
    result = _stage_raw_test_backend_failure(
        run_id, ref, "owner", status, total_rows=1)
    assert metadb.finish_backend_publication(
        run_id, "attempt", "owner", result,
    ) is True

    assert metadb.get_run_state(run_id) is None and metadb.backend_job(run_id) is None
    with metadb.session() as session:
        assert session.get(metadb.RunTerminalFence, run_id).status == "failed"
        assert session.scalar(select(metadb.RunRecord.id).where(
            metadb.RunRecord.run_id == run_id
        )) is None

    metadb.save_run_state(run_id, {**status, "error": "stale"})
    assert metadb.get_run_state(run_id) is None
    with pytest.raises(metadb.TerminalRunIdError, match="already terminal"):
        metadb.bind_run_owner(run_id, "local", None)
    with pytest.raises(metadb.TerminalRunIdError, match="already terminal"):
        _preallocate_backend_test_run(run_id)


def test_stale_supervisor_converges_after_terminal_detail_is_pruned(jobs_config, monkeypatch):
    module, deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"stale_terminal_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    job = _materialize_bound_job(runner, status)
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 0)
    runner._publish_job_result(
        job, None, status.target_node_id, status,
        {"status": "failed", "error": "canonical failure", "rows": 0, "outputs": []},
        artifact_error=True,
    )
    assert metadb.backend_job(status.run_id) is None

    runner._publish_job_result(
        job, graph, "write", status,
        {"status": "done", "rows": 1, "outputs": []},
    )
    assert status.status == "failed"

    status.status = "queued"  # emulate another stale loop that had not observed publication
    runner._supervising.add(status.run_id)
    runner._supervise_jobs(status.run_id)
    assert status.status == "failed"
    assert status.run_id not in runner._supervising


def test_fast_terminal_run_can_bind_owner_while_detail_still_exists(jobs_config):
    run_id = f"fast_terminal_owner_{uuid.uuid4().hex}"
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "failed", "per_node": [], "error": "pre-dispatch rejection",
    })

    metadb.bind_run_owner(run_id, "fast-user", None)

    with metadb.session() as session:
        state = session.get(metadb.RunState, run_id)
        assert state.status == "failed"
        assert state.created_by == "fast-user" and state.auth_canvas_id is None
        assert session.get(metadb.RunTerminalFence, run_id).status == "failed"


def test_prebound_fast_terminal_does_not_rebind_after_identity_fence(
        jobs_config, monkeypatch):
    from hub.models import RunEstimate, RunStatus
    from hub.routers import runs as runs_router

    _module, deps, _runner_obj, _client, _store = _runner(jobs_config)
    seen_auth = None

    class ImmediateBackend:
        name = "immediate-prebound"

        @staticmethod
        def can_run(_plan):
            return True

        @staticmethod
        def estimate(_plan, _rows, _byts=None):
            return RunEstimate(placement="local", needs_confirm=False)

        @staticmethod
        def preallocate_run_id():
            return f"run_fast_prebound_{uuid.uuid4().hex}"

        @staticmethod
        def run(_plan, _graph, _target, _placement, run_id=None):
            nonlocal seen_auth
            seen_auth = metadb.run_auth(run_id)
            status = RunStatus(run_id=run_id, status="done", placement="local", per_node=[])
            metadb.save_run_state(run_id, status.model_dump())
            return status

    backend = ImmediateBackend()
    deps.runners.insert(0, backend)
    previous = metadb.get_setting("backend", default="")
    metadb.set_setting("backend", backend.name)
    monkeypatch.setattr(runs_router.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs_router, "_cone_size", lambda *_args, **_kwargs: (None, None, {}))
    monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 0)
    try:
        status, owner = runs_router.start_run(deps, _graph(), "write", "fast-owner")
    finally:
        metadb.set_setting("backend", previous)

    assert owner is backend and status.status == "done"
    assert seen_auth == ("fast-owner", None)
    assert metadb.get_run_state(status.run_id)["status"] == "done"
    with metadb.session() as session:
        assert session.get(metadb.RunTerminalFence, status.run_id).status == "done"


def test_external_catalog_without_write_ahead_core_fails_before_allocation_or_submit(jobs_config):
    catalog = RecoveringCatalog()
    _module, deps, runner, client, store = _runner(jobs_config, catalog=catalog)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_catalog_admission_{uuid.uuid4().hex}"

    with pytest.raises(RuntimeError, match="write-ahead catalog"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    assert client.submit_calls == [] and store.values == {}
    assert metadb.backend_job(run_id) is None
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == run_id))) == []


def test_managed_catalog_persist_failure_cannot_publish_terminal_done(
        jobs_config, monkeypatch):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_catalog_receipt_{uuid.uuid4().hex}")
    _wait_submitted(client, status.backend_ref.submission_id)

    original = metadb.catalog_apply_managed_publication

    def _swallowed_before_this_fix(*_args, **_kwargs):
        raise ConnectionError("forced durable catalog write failure")

    monkeypatch.setattr(
        metadb, "catalog_apply_managed_publication", _swallowed_before_this_fix)
    _complete(store, client, status)
    deadline = time.monotonic() + 5
    while "waiting for staged effects" not in (runner.status(status.run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)

    job = store.read(status.backend_ref.job_uri)
    output_uri = job["sink_contracts"]["write"]["physical_uri"]
    assert runner.status(status.run_id).status in ("queued", "running")
    binding = metadb.backend_job(status.run_id)
    assert binding["publication_state"] == "effects_started"
    with metadb.session() as session:
        publication_owner = session.get(
            metadb.RunBackendJob, status.run_id).publication_owner
    assert publication_owner
    assert metadb.finish_backend_publication(
        status.run_id, status.backend_ref.attempt_id, publication_owner,
        binding["publication_effects"]["terminal_status"],
    ) is False
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, output_uri)
        logical = session.get(metadb.CatalogLogicalDataset, attempt.logical_id)
        assert session.get(metadb.CatalogEntry, output_uri) is None
        assert logical.current_uri != output_uri
        assert session.get(
            metadb.CatalogPublicationEvent,
            f"ray-jobs:{status.backend_ref.attempt_id}:write",
        ) is None

    monkeypatch.setattr(metadb, "catalog_apply_managed_publication", original)
    assert _wait(runner, status.run_id).status == "done"
    assert metadb.catalog_get(output_uri)["uri"] == output_uri
    receipt = metadb.catalog_managed_publication_receipt(output_uri)
    assert receipt is not None and receipt["uri"] == output_uri


def test_driver_rejects_independent_binding_mismatch_before_importing_ray(jobs_config, tmp_path):
    import json
    import subprocess

    module, _deps, runner, _client, _store = _runner(jobs_config)
    _ref, job = runner._make_jobs_artifacts("driver-binding", _graph(), "write")
    job_path = tmp_path / "job.dpjob"
    job["job_uri"] = str(job_path)
    job["envelope_sha256"] = module._job_envelope_sha256(job)
    job_path.write_text(json.dumps(job))
    driver = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_ray" / "_driver.py"
    proc = subprocess.run(
        [sys.executable, str(driver), str(job_path), job["attempt_id"], job["submission_id"], "wrong"],
        text=True, capture_output=True, timeout=10,
    )
    assert proc.returncode != 0
    assert "independently submitted execution binding" in proc.stderr


@pytest.mark.skipif(os.environ.get("DP_TEST_RAY_JOBS_LIVE") != "1",
                    reason="set DP_TEST_RAY_JOBS_LIVE=1 with a Ray Jobs endpoint + shared object store")
def test_ray_jobs_live_submission_round_trip(tmp_path):
    """Opt-in smoke test for the real Jobs API, image-baked driver, and shared S3 result contract."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.plugins.adapters import object_fs

    metadb.init_db()
    source = f"s3://dpray/live/input-{uuid.uuid4().hex}.parquet"
    fs, path = object_fs(source)
    with fs.open_output_stream(path) as stream:
        pq.write_table(pa.table({"id": [1, 2, 3]}), stream)

    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    deps = Deps(str(workspace), str(data))
    module = _load_dp_ray()
    runner = module.RayRunner(deps, recover=False)
    graph = _graph(name=f"jobs_live_{uuid.uuid4().hex}", source=source)
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)

    run_id = f"run_jobs_live_{uuid.uuid4().hex}"
    metadb.bind_run_owner(run_id, metadb.DEFAULT_USER_ID, None)
    status = runner.run(plan, graph, "write", "distributed", run_id=run_id)
    final = _wait(runner, status.run_id, timeout=120)

    assert final.status == "done", final.error
    assert final.total_rows == 3 and final.output_uri and final.output_uri.startswith("s3://dpray/")
