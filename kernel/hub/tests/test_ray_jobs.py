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
from hub.models import (CatalogPublicationReceipt, Graph, GraphNode, ResourceSpec, RunBackendRef,
                        RunStatus)


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
            self.jobs[submission_id] = {"status": "RUNNING", "message": None, "logs": ""}
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

    def put(self, submission_id: str, status: str, message: str | None = None, logs: str = "") -> None:
        with self.lock:
            self.jobs[submission_id] = {"status": status, "message": message, "logs": logs}

    def set_status(self, submission_id: str, status: str) -> None:
        with self.lock:
            self.jobs[submission_id]["status"] = status


class CountingCatalog:
    def __init__(self):
        self.calls: list[dict] = []
        self.keys: set[str] = set()
        self.usage_calls: list[dict] = []
        self.usage_keys: set[str] = set()
        self.lock = threading.Lock()

    @staticmethod
    def resolve_ref(ref: str) -> str:
        return ref

    def register_output(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)

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


@pytest.fixture
def jobs_config(monkeypatch, tmp_path):
    metadb.init_db()  # standalone module execution does not import test_kernel's app bootstrap
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
    return workspace, data


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
    deps.catalog = catalog or CountingCatalog()
    module = _load_dp_ray()
    client = client or FakeJobsClient()
    store = store or MemoryArtifacts()

    class TestRayRunner(module.RayRunner):
        """Direct unit calls stand in for the API, which normally prebinds the authorized principal."""
        def _make_jobs_artifacts(self, run_id, *args, **kwargs):
            if metadb.run_auth(run_id) == (None, None):
                metadb.bind_run_owner(run_id, metadb.DEFAULT_USER_ID, None)
            return super()._make_jobs_artifacts(run_id, *args, **kwargs)

        def _source_unsupported_reason(self, _ir):
            # Lifecycle tests use a fake S3 URI and never execute data. #77's real source preflight is
            # covered by test_ray_compat; bypass only the unavailable object listing in this fake harness.
            return None

    runner = TestRayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=recover
    )
    return module, deps, runner, client, store


def _wait(runner, run_id: str, terminal=True, timeout=4.0):
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


def _retire_inert_test_run(status) -> None:
    """Keep disabled-supervisor fixtures from leaking active SQL rows into later recovery tests."""
    doc = status.model_dump() if hasattr(status, "model_dump") else dict(status)
    doc.update(status="failed", error="test fixture retired without a live supervisor")
    metadb.save_run_state(status.run_id, doc)


def _materialize_bound_job(runner, status) -> dict:
    """Stand in for the disabled supervisor in tests that exercise lower-level submission fences."""
    assert status.backend_ref is not None
    return runner._read_or_materialize_job_artifact(status.backend_ref, status)


def _as_v2_job(module, current_ref: dict, current: dict, run_id: str) -> tuple[dict, dict]:
    legacy = {key: value for key, value in current.items() if key != "sink_contracts"}
    legacy["contract_version"] = 2
    legacy["attempt_id"] = module._job_attempt_id(legacy)
    legacy["submission_id"] = module._jobs_submission_id(run_id, legacy["attempt_id"])
    base = f"{legacy['artifact_prefix']}/{legacy['submission_id']}"
    legacy["job_uri"] = f"{base}/job.dpjob"
    legacy["result_uri"] = f"{base}/result.dpresult"
    legacy["envelope_sha256"] = module._job_envelope_sha256(legacy)
    legacy_ref = {
        **current_ref,
        "attempt_id": legacy["attempt_id"],
        "submission_id": legacy["submission_id"],
        "job_uri": legacy["job_uri"],
        "result_uri": legacy["result_uri"],
    }
    return legacy_ref, legacy


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
    second = runner.run(plan, graph, "write", "distributed", run_id=run_id)
    assert first.backend_ref == second.backend_ref
    ref = first.backend_ref
    assert ref is not None
    assert ref.attempt_id == module._job_attempt_id(store.read(ref.job_uri))
    _wait_submitted(client, ref.submission_id)
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
    assert len(runner.catalog.calls) == 1


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
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, physical_uri)
        assert attempt is not None and attempt.state == "writing"

    _write_success(store, status, output_uri=logical_uri)
    with pytest.raises(module.ArtifactContractError, match="hash-bound job sinks"):
        runner._validate_job_result(job, store.read(ref.result_uri))

    _complete(store, client, status, output_uri=physical_uri)
    final = _wait(runner, status.run_id)
    assert final.status == "done" and final.output_uri == physical_uri
    assert [call["uri"] for call in runner.catalog.calls] == [physical_uri]


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


def test_ray_jobs_compatibility_sink_keeps_logical_adapter_uri(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    graph.nodes[-1].data["config"]["filename"] = "jobs_compat.csv"
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(
        plan, graph, "write", "distributed",
        run_id=f"run_jobs_compat_sink_{uuid.uuid4().hex}",
    )
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    job = store.read(ref.job_uri)
    logical_uri = job["sink_targets"]["write"]
    assert logical_uri.endswith("jobs_compat.csv")
    wrong_attempt_uri = module._attempt_handoff_uri(
        logical_uri, ref.attempt_id, scope="write"
    )

    _write_success(
        store, status, output_uri=wrong_attempt_uri, output_name="jobs_compat"
    )
    with pytest.raises(module.ArtifactContractError, match="hash-bound job sinks"):
        runner._validate_job_result(job, store.read(ref.result_uri))

    _complete(
        store, client, status, output_uri=logical_uri, output_name="jobs_compat"
    )
    final = _wait(runner, status.run_id)
    assert final.status == "done" and final.output_uri == logical_uri
    assert [call["uri"] for call in runner.catalog.calls] == [logical_uri]


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
    client.set_status(ref.submission_id, "SUCCEEDED")
    final = _wait(runner, status.run_id)
    assert final.status == "failed" and final.output_uri is None and final.output_table is None
    assert runner.catalog.calls == []
    assert store.read(ref.result_uri)["outputs"] == [{
        "step_id": "write", "name": "jobs_out", "uri": physical_uri,
        "logical_uri": job["sink_targets"]["write"],
    }]


def test_official_jobs_client_pins_explicit_api_address_over_ray_address(jobs_config, monkeypatch):
    _module, deps, runner, _client, _store = _runner(jobs_config)
    observed = {}

    class FakeOfficialClient:
        def __init__(self, address):
            observed["argument"] = address
            observed["api"] = os.environ.get("RAY_API_SERVER_ADDRESS")
            observed["ray"] = os.environ.get("RAY_ADDRESS")

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

    assert isinstance(client, FakeOfficialClient)
    assert observed == {"argument": runner.jobs_address, "api": runner.jobs_address, "ray": "auto"}
    assert "RAY_API_SERVER_ADDRESS" not in os.environ


def test_ray_jobs_recognizes_preexisting_duplicate_without_second_submit(jobs_config):
    _module, deps, runner, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_duplicate_{uuid.uuid4().hex}"
    ref, _job = runner._make_jobs_artifacts(
        run_id, graph, "write", sink_targets={"write": "s3://shared/outputs/jobs_out.parquet"},
        requires=ResourceSpec().model_dump(),
    )
    client.put(ref["submission_id"], "RUNNING")

    status = runner.run(plan, graph, "write", "distributed", run_id=run_id)
    _wait_submitted(client, ref["submission_id"])
    time.sleep(0.05)
    assert client.submit_calls == []
    _complete(store, client, status)
    assert _wait(runner, run_id).status == "done"


def test_duplicate_run_id_reattaches_stored_envelope_instead_of_escaping(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    runner._ensure_jobs_supervisor = lambda _run_id: None
    first_graph = _graph(name="first")
    first_plan = compile_plan(
        first_graph, "write", deps.registry, deps.node_specs, deps.node_ir
    )
    run_id = f"run_jobs_duplicate_payload_{uuid.uuid4().hex}"
    first = runner.run(first_plan, first_graph, "write", "distributed", run_id=run_id)
    first_payload = metadb.backend_job_artifact_payload(run_id)

    second_graph = _graph(name="second")
    second_plan = compile_plan(
        second_graph, "write", deps.registry, deps.node_specs, deps.node_ir
    )
    second = runner.run(second_plan, second_graph, "write", "distributed", run_id=run_id)

    assert second is first and second.backend_ref == first.backend_ref
    assert metadb.backend_job_artifact_payload(run_id) == first_payload
    assert client.submit_calls == []
    _retire_inert_test_run(first)


def test_ray_jobs_cancel_waits_for_stopped_acknowledgement(jobs_config):
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_cancel_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)

    final = runner.cancel(status.run_id)

    assert final.status == "cancelled"
    assert client.stop_calls == [ref.submission_id]
    assert runner.cancel_acknowledged(status.run_id) is True
    assert metadb.get_run_state(status.run_id)["status"] == "cancelled"


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
    assert submitted["submission_state"] == "submitted" and submitted["cancel_requested"] is True
    requested, _control, state = runner._cancel_control_state(status, ref, submitted)
    assert requested is True and state == "STOPPED"
    assert client.submit_calls and client.stop_calls == [ref.submission_id]
    runner._publish_cancelled_binding(status, metadb.backend_job(status.run_id))


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
    client.put(ref.submission_id, "RUNNING")
    client.events.clear()

    assert runner._ensure_job_submitted(client, job) == "RUNNING"
    assert client.events[0] == "status"
    assert "submit" not in client.events
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "submitted" and binding["submission_owner"] is None
    _retire_inert_test_run(status)


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
    assert runner._resume_quarantined_job(status, ref, binding) is False
    assert client.stop_calls == [ref.submission_id]
    binding = metadb.backend_job(status.run_id)
    assert binding["submission_state"] == "stop_fenced"
    assert runner._resume_quarantined_job(status, ref, binding) is True
    assert status.status == "failed" and "tampered envelope" in (status.error or "")


def test_ray_jobs_restart_reattaches_and_catalog_publication_has_one_winner(jobs_config):
    catalog = CountingCatalog()
    module, deps, original, client, store = _runner(jobs_config, catalog=catalog)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"run_jobs_restart_{uuid.uuid4().hex}"
    original._ensure_jobs_supervisor = lambda _run_id: None  # submitting hub dies after durable handoff
    queued = original.run(plan, graph, "write", "distributed", run_id=run_id)
    ref = queued.backend_ref
    assert ref is not None
    client.put(ref.submission_id, "RUNNING")  # the submit reached Ray before the hub process disappeared
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
    assert _wait(recovered_a, run_id).status == "done"
    assert _wait(recovered_b, run_id).status == "done"
    assert len(catalog.calls) == 1
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
    with metadb.session() as session:
        cleared_at = session.get(metadb.RunState, status.run_id).updated_at
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
    assert (restarted.status(status.run_id).error or "").count("Ray Jobs recovery blocked:") == 1
    assert status.run_id not in supervised
    _retire_inert_test_run(restarted.status(status.run_id))


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


def test_ray_jobs_failure_uses_official_info_and_keeps_logs_out_of_shared_status(jobs_config, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "job-secret-value")
    _module, deps, runner, client, _store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_failure_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    _wait_submitted(client, ref.submission_id)
    client.put(ref.submission_id, "FAILED", message="entrypoint exited 17",
               logs="traceback: job-secret-value")

    final = _wait(runner, status.run_id)

    assert final.status == "failed"
    assert "entrypoint exited 17" in (final.error or "")
    assert "traceback" not in (final.error or "") and "job-secret-value" not in (final.error or "")
    assert client.log_calls == []
    assert runner.logs(status.run_id) == "traceback: [REDACTED]"
    assert client.log_calls == [ref.submission_id]
    assert runner.catalog.calls == []


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
    with metadb.session() as session:
        session.get(metadb.RunBackendJob, status.run_id).job_doc = None  # nullable upgrade compatibility

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)

    assert _wait(recovered, status.run_id).status == "done"
    assert client.submit_calls == [] and len(recovered.catalog.calls) == 1


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
    client.put(ref.submission_id, "RUNNING")
    job = _materialize_bound_job(original, status)
    job["workspace"] = "/attacker-controlled"
    store.write(ref.job_uri, job)

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    final = _wait(recovered, status.run_id)

    assert final.status == "failed" and "invalid Ray Jobs artifact" in (final.error or "")
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
        def _source_unsupported_reason(self, _ir):
            return None

    runner = UnboundRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=False
    )
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    run_id = f"unbound-ray-run-{uuid.uuid4().hex}"

    with pytest.raises(RuntimeError, match="durable run owner"):
        runner.run(plan, graph, "write", "distributed", run_id=run_id)

    assert store.values == {} and client.submit_calls == []
    assert metadb.backend_job(run_id) is None


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
    assert final.status == "failed" and "unknown=unexpected" in (final.error or "")


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
    client.put(ref.submission_id, "RUNNING")
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
    ref_a, job_a = runner._make_jobs_artifacts("semantic-run", graph, "write", sink_targets={
        "write": "s3://shared/outputs/jobs_out.parquet",
    })
    assert job_a["semantic_env"]["DP_MEMORY_LIMIT"] == "4GB"
    assert job_a["semantic_env"]["DP_RAY_GPU_BATCH_ROWS"] == str(module._GPU_BATCH_ROWS_DEFAULT)
    assert "AWS_SECRET_ACCESS_KEY" not in job_a["semantic_env"]

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "rotated-secret")
    ref_b, job_b = runner._make_jobs_artifacts("semantic-run", graph, "write", sink_targets={
        "write": "s3://shared/outputs/jobs_out.parquet",
    })
    assert ref_a["attempt_id"] == ref_b["attempt_id"]
    assert job_a["envelope_sha256"] == job_b["envelope_sha256"]
    launch_env = module._ray_jobs_env(job_a)
    assert launch_env["DP_MEMORY_LIMIT"] == "4GB"
    assert launch_env["AWS_SECRET_ACCESS_KEY"] == "rotated-secret"

    monkeypatch.setenv("DP_RAY_GPU_BATCH_ROWS", "8192")
    ref_c, job_c = runner._make_jobs_artifacts("semantic-run", graph, "write", sink_targets={
        "write": "s3://shared/outputs/jobs_out.parquet",
    })
    assert ref_c["attempt_id"] != ref_a["attempt_id"]
    assert job_c["semantic_env"]["DP_RAY_GPU_BATCH_ROWS"] == "8192"


def test_v2_job_and_result_artifacts_remain_readable(jobs_config):
    module, deps, runner, client, store = _runner(jobs_config)
    assert module._legacy_v2_attempt_handoff_uri(
        "s3://shared/outputs/jobs_out.parquet", "legacy-attempt-0123456789", scope="write"
    ) == (
        "s3://shared/outputs/jobs_out.attempt-legacy-attempt-0123456789-"
        "b6d8061796617499c93052a67be732d8"
    )
    run_id = f"legacy-v2-{uuid.uuid4().hex}"
    graph = _graph()
    current_ref, current = runner._make_jobs_artifacts(
        run_id, graph, "write",
        sink_targets={"write": "s3://shared/outputs/jobs_out.parquet"},
    )
    legacy_ref, legacy = _as_v2_job(module, current_ref, current, run_id)
    ref = RunBackendRef.model_validate(legacy_ref)
    status = RunStatus(run_id=run_id, status="queued", backend_ref=ref)

    runner._validate_job_artifact_integrity(ref, status, legacy)
    logical_uri = legacy["sink_targets"]["write"]
    physical_uri = module._legacy_v2_attempt_handoff_uri(
        logical_uri, legacy["attempt_id"], scope="write"
    )
    assert physical_uri != module._attempt_handoff_uri(
        logical_uri, legacy["attempt_id"], scope="write"
    )
    result = {
        "contract_version": 2,
        "attempt_id": legacy["attempt_id"],
        "submission_id": legacy["submission_id"],
        "envelope_sha256": legacy["envelope_sha256"],
        "status": "done",
        "rows": 3,
        "error": None,
        "output_uri": physical_uri,
        "output_table": "jobs_out",
        "outputs": [{"step_id": "write", "name": "jobs_out", "uri": physical_uri}],
    }
    store.write(legacy["job_uri"], legacy)
    store.write(legacy["result_uri"], result)
    metadb.bind_backend_job(
        run_id, legacy_ref, status.model_dump(), canvas_id=graph.id,
        job_payload=module.canonical_json(legacy),
    )
    deps.resolve_adapter = lambda _uri: (_ for _ in ()).throw(
        RuntimeError("legacy result validation must not consult the current adapter registry")
    )
    recovered = module.RayRunner(
        deps, jobs_client_factory=client, artifact_store=store, recover=True
    )

    assert _wait(recovered, run_id).status == "done"
    assert client.submit_calls == [] and client.stop_calls == []
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, physical_uri)
        assert attempt is not None and attempt.state == "published"
    assert metadb.catalog_get(physical_uri)["uri"] == physical_uri


def test_v2_replay_preclaims_legacy_direct_candidate_despite_adapter_drift(jobs_config):
    module, deps, runner, client, _store = _runner(jobs_config)
    run_id = f"legacy-v2-replay-{uuid.uuid4().hex}"
    graph = _graph()
    current_ref, current = runner._make_jobs_artifacts(
        run_id, graph, "write",
        sink_targets={"write": "s3://shared/outputs/jobs_out.parquet"},
    )
    legacy_ref, legacy = _as_v2_job(module, current_ref, current, run_id)
    ref = RunBackendRef.model_validate(legacy_ref)
    status = RunStatus(run_id=run_id, status="queued", backend_ref=ref)
    metadb.bind_backend_job(
        run_id, legacy_ref, status.model_dump(), canvas_id=graph.id,
        job_payload=module.canonical_json(legacy),
    )
    runner.resolve_adapter = lambda _uri: (_ for _ in ()).throw(
        RuntimeError("current adapter drift must not suppress the legacy write claim")
    )

    runner._prepare_jobs_submission(legacy)
    logical_uri = legacy["sink_targets"]["write"]
    physical_uri = module._legacy_v2_attempt_handoff_uri(
        logical_uri, legacy["attempt_id"], scope="write"
    )
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, physical_uri)
        assert attempt is not None and attempt.state == "writing"
    assert runner._ensure_job_submitted(client, legacy) == "PENDING"
    assert client.submit_calls[0]["submission_id"] == legacy["submission_id"]
    _retire_inert_test_run(status)


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
    metadb.bind_backend_job(run_id, ref, status_doc, canvas_id=graph.id)
    assert metadb.claim_backend_publication(run_id, "attempt", "owner", 10) == "claimed"
    result = {**status_doc, "status": "done", "total_rows": 7, "output_uri": "s3://b/out"}
    assert metadb.finish_backend_publication(run_id, "attempt", "owner", result) is True
    assert metadb.backend_job(run_id)["publication_state"] == "published"
    assert metadb.get_run_state(run_id)["status"] == "done"
    history = metadb.list_runs(graph.id)
    assert len(history) == 1 and history[0]["runId"] == run_id and history[0]["rows"] == 7

    rollback_id = f"atomic_rollback_{uuid.uuid4().hex}"
    rollback_ref = {**ref, "submission_id": f"submission-{uuid.uuid4().hex}"}
    metadb.bind_backend_job(rollback_id, rollback_ref, {**status_doc, "run_id": rollback_id}, canvas_id=graph.id)
    assert metadb.claim_backend_publication(rollback_id, "attempt", "owner", 10) == "claimed"
    monkeypatch.setattr(metadb, "_upsert_run_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("history write failed")
    ))
    with pytest.raises(RuntimeError, match="history write failed"):
        metadb.finish_backend_publication(
            rollback_id, "attempt", "owner", {**result, "run_id": rollback_id}
        )
    assert metadb.backend_job(rollback_id)["publication_state"] == "pending"
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
        metadb.bind_backend_job(run_id, ref, status_doc, canvas_id=graph.id)
        assert metadb.claim_backend_publication(run_id, ref["attempt_id"], "owner", 10) == "claimed"
        assert metadb.finish_backend_publication(
            run_id, ref["attempt_id"], "owner",
            {**status_doc, "status": "done", "total_rows": index + 1},
        ) is True
        time.sleep(0.002)

    assert metadb.get_run_state(run_ids[0]) is None
    assert metadb.backend_job(run_ids[0]) is None
    assert all(metadb.get_run_state(run_id)["status"] == "done" for run_id in run_ids[1:])
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
        metadb.bind_backend_job(run_ids[0], refs[0], {
            "run_id": run_ids[0], "status": "queued", "per_node": [],
        })


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
    metadb.bind_backend_job(run_id, ref, status)
    assert metadb.claim_backend_publication(run_id, "attempt", "owner", 10) == "claimed"
    assert metadb.finish_backend_publication(
        run_id, "attempt", "owner", {**status, "status": "done", "total_rows": 1},
    ) is True

    assert metadb.get_run_state(run_id) is None and metadb.backend_job(run_id) is None
    with metadb.session() as session:
        assert session.get(metadb.RunTerminalFence, run_id).status == "done"
        assert session.scalar(select(metadb.RunRecord.id).where(
            metadb.RunRecord.run_id == run_id
        )) is None

    metadb.save_run_state(run_id, {**status, "error": "stale"})
    assert metadb.get_run_state(run_id) is None
    with pytest.raises(metadb.TerminalRunIdError, match="already terminal"):
        metadb.bind_run_owner(run_id, "local", None)
    with pytest.raises(metadb.TerminalRunIdError, match="already terminal"):
        metadb.bind_backend_job(run_id, ref, {**status, "status": "queued"})


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
    assert metadb.get_run_state(status.run_id) is None
    with metadb.session() as session:
        assert session.get(metadb.RunTerminalFence, status.run_id).status == "done"


def test_catalog_is_required_and_retried_before_terminal_publication(jobs_config):
    catalog = RecoveringCatalog()
    _module, deps, runner, client, store = _runner(jobs_config, catalog=catalog)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_catalog_retry_{uuid.uuid4().hex}")
    _wait_submitted(client, status.backend_ref.submission_id)
    _complete(store, client, status)
    deadline = time.monotonic() + 1
    while "waiting for catalog" not in (runner.status(status.run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.status(status.run_id).status in ("queued", "running")
    assert metadb.backend_job(status.run_id)["publication_state"] == "pending"
    catalog.available = True
    assert _wait(runner, status.run_id).status == "done"
    assert len(catalog.calls) == 1


def test_default_catalog_persist_failure_cannot_publish_terminal_done(
        jobs_config, monkeypatch):
    workspace, data = jobs_config
    catalog = Deps(str(workspace), str(data)).catalog

    def _unresolvable(_uri):
        raise RuntimeError("test catalog does not inspect the object store")

    catalog.resolve = _unresolvable
    monkeypatch.setattr(catalog, "_object_stat_sig", lambda _uri: "")
    _module, deps, runner, client, store = _runner(jobs_config, catalog=catalog)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_catalog_receipt_{uuid.uuid4().hex}")
    _wait_submitted(client, status.backend_ref.submission_id)

    original = metadb.catalog_upsert_entry

    def _swallowed_before_this_fix(*_args, **_kwargs):
        raise ConnectionError("forced durable catalog write failure")

    monkeypatch.setattr(metadb, "catalog_upsert_entry", _swallowed_before_this_fix)
    _complete(store, client, status)
    deadline = time.monotonic() + 1
    while "waiting for catalog" not in (runner.status(status.run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)

    job = store.read(status.backend_ref.job_uri)
    logical_uri = job["sink_targets"]["write"]
    output_uri = job["sink_contracts"]["write"]["physical_uri"]
    assert runner.status(status.run_id).status in ("queued", "running")
    assert metadb.backend_job(status.run_id)["publication_state"] == "pending"
    assert metadb.catalog_get(output_uri) is None
    assert metadb.catalog_get(logical_uri) is None

    monkeypatch.setattr(metadb, "catalog_upsert_entry", original)
    assert _wait(runner, status.run_id).status == "done"
    assert metadb.catalog_get(output_uri)["uri"] == output_uri
    with metadb.session() as session:
        receipt = session.get(
            metadb.CatalogPublicationEvent,
            f"ray-jobs:{status.backend_ref.attempt_id}:write",
        )
        assert receipt and receipt.effect_type == "output" and receipt.uri == output_uri


def test_driver_rejects_independent_binding_mismatch_before_importing_ray(jobs_config, tmp_path):
    import json
    import subprocess

    module, _deps, runner, _client, _store = _runner(jobs_config)
    _ref, job = runner._make_jobs_artifacts("driver-binding", _graph(), "write", sink_targets={
        "write": "s3://shared/outputs/jobs_out.parquet",
    })
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
