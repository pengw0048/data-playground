"""Deterministic Ray Jobs lifecycle tests; no live Ray cluster required."""

from __future__ import annotations

import importlib.util
import os
import shlex
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

from hub import metadb
from hub.compiler import compile_plan
from hub.deps import Deps
from hub.models import Graph, ResourceSpec


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


class FakeJobsClient:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.submit_calls: list[dict] = []
        self.stop_calls: list[str] = []
        self.status_calls: list[str] = []
        self.log_calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, _address: str):
        return self

    def get_job_status(self, submission_id: str):
        with self.lock:
            self.status_calls.append(submission_id)
            if submission_id not in self.jobs:
                raise RuntimeError(f"job {submission_id} does not exist")
            return self.jobs[submission_id]["status"]

    def list_jobs(self):
        with self.lock:
            return {job_id: dict(info) for job_id, info in self.jobs.items()}

    def submit_job(self, **kwargs):
        submission_id = kwargs["submission_id"]
        with self.lock:
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
        self.lock = threading.Lock()

    def register_output(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)

    def register_output_idempotent(self, idempotency_key: str, **kwargs):
        with self.lock:
            if idempotency_key in self.keys:
                return
            self.keys.add(idempotency_key)
            self.calls.append({"idempotency_key": idempotency_key, **kwargs})


class RecoveringCatalog(CountingCatalog):
    def __init__(self):
        super().__init__()
        self.available = False

    def register_output_idempotent(self, idempotency_key: str, **kwargs):
        if not self.available:
            raise ConnectionError("catalog temporarily unavailable")
        super().register_output_idempotent(idempotency_key, **kwargs)


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
    runner = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=recover)
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


def _write_success(store: MemoryArtifacts, status, rows=3):
    ref = status.backend_ref
    assert ref is not None
    job = store.read(ref.job_uri)
    output_uri = job["sink_targets"]["write"]
    store.write(ref.result_uri, {
        "contract_version": job["contract_version"],
        "attempt_id": ref.attempt_id,
        "submission_id": ref.submission_id,
        "envelope_sha256": job["envelope_sha256"],
        "status": "done",
        "rows": rows,
        "error": None,
        "output_uri": output_uri,
        "output_table": "jobs_out",
        "outputs": [{"step_id": "write", "name": "jobs_out", "uri": output_uri}],
    })


def _complete(store: MemoryArtifacts, client: FakeJobsClient, status, rows=3):
    ref = status.backend_ref
    assert ref is not None
    _write_success(store, status, rows)
    client.set_status(ref.submission_id, "SUCCEEDED")


def test_ray_jobs_submit_is_deterministic_idempotent_and_excludes_metadata_identity(jobs_config):
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
    persisted = metadb.get_run_state(run_id)
    assert persisted and persisted["backend_ref"]["submission_id"] == ref.submission_id

    _complete(store, client, first)
    final = _wait(runner, run_id)
    assert final.status == "done" and final.total_rows == 3
    assert len(runner.catalog.calls) == 1


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
    assert client.submit_calls == [] and len(recovered.catalog.calls) == 1


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
    client.put(ref.submission_id, "RUNNING")
    monkeypatch.setenv("DP_RAY_JOBS_CLUSTER_REF", "different-cluster")

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    deadline = time.monotonic() + 2
    while "configuration drift" not in (recovered.status(status.run_id).error or "") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recovered.status(status.run_id).status == "queued"
    assert client.submit_calls == [] and client.stop_calls == []

    monkeypatch.setenv("DP_RAY_JOBS_CLUSTER_REF", "test-ray-cluster")
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
    job = store.read(ref.job_uri)
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


def test_recovery_without_local_jobs_config_stays_live_but_durable_cancel_works(jobs_config, monkeypatch):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_no_config_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    client.put(ref.submission_id, "RUNNING")
    for key in (
        "DP_RAY_JOBS_ADDRESS", "DP_RAY_JOBS_ENTRYPOINT", "DP_RAY_JOBS_CODE_REF",
        "DP_RAY_JOBS_CLUSTER_REF", "DP_RAY_JOBS_WORKSPACE", "DP_RAY_JOBS_DATA_DIR",
        "DP_STORAGE_URL", "DP_RAY_JOBS_ARTIFACT_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)

    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)
    deadline = time.monotonic() + 1
    while "configuration unavailable" not in (recovered.status(status.run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recovered.status(status.run_id).status == "queued"
    assert client.stop_calls == [] and client.submit_calls == []

    final = recovered.cancel(status.run_id)
    assert final.status == "cancelled"
    assert client.stop_calls == [ref.submission_id]
    assert metadb.backend_job(status.run_id)["cancel_requested"] is True


def test_cancel_stops_from_sql_while_job_artifact_is_missing_and_never_submits(jobs_config):
    module, deps, original, client, store = _runner(jobs_config)
    graph = _graph()
    plan = compile_plan(graph, "write", deps.registry, deps.node_specs, deps.node_ir)
    original._ensure_jobs_supervisor = lambda _run_id: None
    status = original.run(plan, graph, "write", "distributed",
                          run_id=f"run_jobs_missing_cancel_{uuid.uuid4().hex}")
    ref = status.backend_ref
    assert ref is not None
    with store.lock:
        store.values.pop(ref.job_uri)
    client.put(ref.submission_id, "RUNNING")
    recovered = module.RayRunner(deps, jobs_client_factory=client, artifact_store=store, recover=True)

    deadline = time.monotonic() + 1
    while "artifact missing" not in (recovered.status(status.run_id).error or "") \
            and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recovered.cancel(status.run_id).status == "cancelled"
    assert client.stop_calls == [ref.submission_id]
    assert client.submit_calls == []


def test_jobs_semantic_environment_is_frozen_while_credentials_rotate(jobs_config, monkeypatch):
    module, _deps, runner, _client, _store = _runner(jobs_config)
    graph = _graph()
    monkeypatch.setenv("DP_MEMORY_LIMIT", "4GB")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "old-secret")
    ref_a, job_a = runner._make_jobs_artifacts("semantic-run", graph, "write", sink_targets={
        "write": "s3://shared/outputs/jobs_out.parquet",
    })
    assert job_a["semantic_env"]["DP_MEMORY_LIMIT"] == "4GB"
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
    job = store.read(status.backend_ref.job_uri)
    assert job["requires"]["gpu_type"] == "a100"
    assert job["requires"]["labels"] == {"engine": "ray", "pool": "a100"}
    assert module._ray_opts(job["requires"]) == {
        "num_gpus": 1.0, "resources": {"a100": 0.001},
    }
    assert client.submit_calls == []

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

    status = runner.run(plan, graph, "write", "distributed",
                        run_id=f"run_jobs_live_{uuid.uuid4().hex}")
    final = _wait(runner, status.run_id, timeout=120)

    assert final.status == "done", final.error
    assert final.total_rows == 3 and final.output_uri and final.output_uri.startswith("s3://dpray/")
