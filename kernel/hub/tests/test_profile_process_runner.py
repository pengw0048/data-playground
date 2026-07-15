"""Process-lifecycle proofs for whole-dataset profile jobs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time

import duckdb
import pytest

from hub.models import Graph
from hub.profile_jobs import ProfileProcessRunner


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _graph(uri: str | None = None) -> Graph:
    config = {"uri": uri} if uri is not None else {}
    return Graph.model_validate({
        "id": "profile-process",
        "version": 1,
        "nodes": [{"id": "source", "type": "source", "data": {"config": config}}],
        "edges": [],
    })


@contextlib.contextmanager
def _isolated_metadata(path):
    from hub import metadb
    from hub.settings import settings

    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = f"sqlite:///{path}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _wait(runner: ProfileProcessRunner, run_id: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status(run_id)
        if status.status in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError("profile process did not reach a terminal state")


def _wait_for(path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise AssertionError(f"child side effect was not observed: {path}")


def _wait_for_supervisor_cleanup(runner: ProfileProcessRunner, run_id: str) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if run_id not in runner._procs:
            return
        time.sleep(0.01)
    raise AssertionError(f"profile supervisor retained a reaped child: {run_id}")


def _fake_subrun(monkeypatch, source: str):
    """Replace only the one-shot command while retaining a real OS Popen/process."""
    real_popen = subprocess.Popen
    processes = []
    environments = []

    def popen(command, **kwargs):
        assert command[1:3] == ["-m", "hub.subrun"]
        environments.append(dict(kwargs.get("env") or {}))
        proc = real_popen(
            [sys.executable, "-c", source, command[-1]],
            **kwargs,
        )
        processes.append(proc)
        return proc

    monkeypatch.setattr("hub.subprocess_runner.subprocess.Popen", popen)
    return processes, environments


def _runner(tmp_path, *, deadline_s: float = 10.0) -> ProfileProcessRunner:
    return ProfileProcessRunner(
        str(tmp_path / "workspace"),
        str(tmp_path / "data"),
        deadline_s=deadline_s,
    )


def test_full_profile_roundtrips_through_real_subrun_and_reaps(tmp_path):
    source = tmp_path / "rows.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES (1,'a'),(2,'b'),(3,NULL)) t(x,y)) "
        f"TO '{source}' (FORMAT PARQUET)"
    )
    with _isolated_metadata(tmp_path / "parent.db"):
        runner = _runner(tmp_path)
        started = runner.run(
            _graph(str(source)),
            "source",
            plan_digest=_digest("real-profile"),
            profile_attempt_order=7,
            run_id="profile-real-child",
            request_id="request-real-child",
        )
        final = _wait(runner, started.run_id)

    assert final.status == "done"
    assert final.profile is not None and not final.profile.sampled
    assert final.profile.row_count == 3
    assert [column.name for column in final.profile.columns] == ["x", "y"]
    assert final.total_rows == final.rows_processed == 3
    assert final.target_node_id == "source"
    assert final.plan_digest == _digest("real-profile")
    assert final.profile_attempt_order == 7
    assert final.request_id == "request-real-child"
    _wait_for_supervisor_cleanup(runner, started.run_id)
    assert started.run_id not in runner._procs


def test_managed_source_parent_lease_spans_real_child_reap_without_child_reacquire(
        tmp_path, monkeypatch):
    """The disposable child DB cannot acquire this source; success proves parent-preclaimed use."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub import metadb
    from hub.storage import LocalStorage

    workspace = tmp_path / "managed-workspace"
    storage = LocalStorage(str(workspace / "outputs"))
    producer_run = "managed-profile-source-producer"
    acquired = []
    processes = []
    job_files = []
    real_popen = subprocess.Popen
    original_acquire = storage.acquire_result_read

    def acquire(uri, owner):
        guard = original_acquire(uri, owner)
        acquired.append(guard)
        return guard

    storage.acquire_result_read = acquire

    def lingering_subrun(command, **kwargs):
        assert command[1:3] == ["-m", "hub.subrun"]
        job_files.append(command[-1])
        proc = real_popen([
            sys.executable,
            "-c",
            ("import sys,time; from hub.subrun import main; "
             "code=main(); time.sleep(0.5); raise SystemExit(code)"),
            command[-1],
        ], **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr("hub.subprocess_runner.subprocess.Popen", lingering_subrun)
    try:
        with _isolated_metadata(tmp_path / "managed-parent.db"):
            uri = storage.begin_result("managed-profile-source", producer_run)
            pq.write_table(pa.table({"value": [1, 2, 3, 4]}), uri)
            storage.commit_result(uri, producer_run)
            runner = ProfileProcessRunner(
                str(workspace), str(tmp_path / "data"), storage=storage, deadline_s=10)
            started = runner.run(
                _graph(uri),
                "source",
                plan_digest=_digest("managed-source"),
                profile_attempt_order=1,
                run_id="profile-managed-source",
            )

            deadline = time.monotonic() + 4
            child_done = False
            while time.monotonic() < deadline:
                try:
                    child_done = json.load(open(
                        json.load(open(job_files[0]))["statusFile"]
                    ))["status"] == "done"
                except (IndexError, OSError, KeyError, TypeError, ValueError):
                    pass
                if child_done:
                    break
                time.sleep(0.01)
            assert child_done and processes[0].poll() is None
            assert acquired and metadb.local_result_read_active(
                uri, storage.namespace_id, acquired[0].reader_id)
            assert runner.status(started.run_id).status in ("queued", "running")

            final = _wait(runner, started.run_id)
            assert final.status == "done"
            assert final.profile is not None and final.profile.row_count == 4
            assert processes[0].returncode == 0
            assert not metadb.local_result_read_active(
                uri, storage.namespace_id, acquired[0].reader_id)
            storage.abort_result(uri, producer_run)
    finally:
        storage.close()


def test_child_terminal_is_private_until_exit_and_parent_identity_wins(tmp_path, monkeypatch):
    marker = tmp_path / "terminal-child-side-effect"
    monkeypatch.setenv("DP_DATABASE_URL", "postgresql://control-plane/metadata")
    monkeypatch.setenv("DP_AUTH_SECRET", "must-not-reach-profile-child")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-profile-child")
    script = f"""
import json, os, sys, time
job = json.load(open(sys.argv[1]))
payload = {{
    "run_id": "forged-child", "status": "done", "job_type": "run",
    "target_node_id": "forged-target", "rows_processed": 4, "total_rows": 4,
    "ms": 1, "placement": "distributed", "per_node": [], "progress": 1.0,
    "output_uri": "/forged", "output_table": "forged",
    "profile": {{"columns": [], "row_count": 4, "sampled": False,
                "not_previewable": False, "error": False}},
    "plan_digest": {('0' * 64)!r}, "profile_attempt_order": 999,
    "request_id": "forged-request"
}}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
for _ in range(10):
    with open({str(marker)!r}, "a") as output:
        output.write("x"); output.flush(); os.fsync(output.fileno())
    time.sleep(0.05)
"""
    processes, environments = _fake_subrun(monkeypatch, script)
    runner = _runner(tmp_path)
    terminal_observations = []

    def observe(_graph, status):
        if status.status in ("done", "failed", "cancelled"):
            terminal_observations.append(processes[0].poll())

    runner.on_status = observe
    started = runner.run(
        _graph(),
        "source",
        plan_digest=_digest("parent-plan"),
        profile_attempt_order=3,
        run_id="profile-terminal-gate",
        request_id="parent-request",
    )
    _wait_for(marker)
    assert runner.status(started.run_id).status in ("queued", "running")
    assert processes[0].poll() is None

    final = _wait(runner, started.run_id)
    side_effect_size = marker.stat().st_size
    time.sleep(0.12)

    assert final.status == "done" and final.profile is not None
    assert final.run_id == "profile-terminal-gate"
    assert final.target_node_id == "source"
    assert final.plan_digest == _digest("parent-plan")
    assert final.profile_attempt_order == 3
    assert final.request_id == "parent-request"
    assert final.placement == "local"
    assert final.output_uri is final.output_table is None
    assert terminal_observations and all(code is not None for code in terminal_observations)
    assert processes[0].poll() == 0
    assert marker.stat().st_size == side_effect_size
    assert "DP_DATABASE_URL" not in environments[0]
    assert "DP_AUTH_SECRET" not in environments[0]
    assert "UNRELATED_SECRET" not in environments[0]
    assert environments[0]["DP_AUTH_MODE"] == "1"


def test_cancel_kills_and_reaps_noninterruptible_profile_before_terminal(
        tmp_path, monkeypatch):
    marker = tmp_path / "cancel-child-side-effect"
    script = f"""
import json, os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
job = json.load(open(sys.argv[1]))
payload = {{"run_id": "child", "status": "running", "job_type": "profile",
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": []}}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
while True:
    with open({str(marker)!r}, "a") as output:
        output.write("x"); output.flush(); os.fsync(output.fileno())
    time.sleep(0.02)
"""
    processes, _environments = _fake_subrun(monkeypatch, script)
    monkeypatch.setattr("hub.subprocess_runner._CANCEL_GRACE_S", 0.01)
    runner = _runner(tmp_path)
    terminal_observations = []
    runner.on_status = lambda _graph, status: terminal_observations.append(
        (status.status, processes[0].poll()))

    started = runner.run(
        _graph(),
        "source",
        plan_digest=_digest("cancel-plan"),
        profile_attempt_order=1,
        run_id="profile-hard-cancel",
    )
    _wait_for(marker)
    returned = runner.cancel(started.run_id)
    assert returned.status in ("queued", "running")
    assert not runner.cancel_acknowledged(started.run_id)

    final = _wait(runner, started.run_id)
    side_effect_size = marker.stat().st_size
    time.sleep(0.12)

    assert final.status == "cancelled" and final.profile is None
    assert runner.cancel_acknowledged(started.run_id)
    assert processes[0].returncode is not None
    _wait_for_supervisor_cleanup(runner, started.run_id)
    assert started.run_id not in runner._procs
    assert marker.stat().st_size == side_effect_size
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)
    terminal_codes = [code for state, code in terminal_observations
                      if state in ("done", "failed", "cancelled")]
    assert terminal_codes and all(code is not None for code in terminal_codes)


def test_child_cannot_self_report_parent_cancellation(tmp_path, monkeypatch):
    script = """
import json, os, sys
job = json.load(open(sys.argv[1]))
payload = {"run_id": "child", "status": "cancelled", "job_type": "profile",
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": []}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
"""
    processes, _environments = _fake_subrun(monkeypatch, script)
    runner = _runner(tmp_path)
    started = runner.run(
        _graph(),
        "source",
        plan_digest=_digest("forged-cancel"),
        profile_attempt_order=1,
        run_id="profile-forged-cancel",
    )
    final = _wait(runner, started.run_id)

    assert final.status == "failed"
    assert final.error == "profile process reported cancellation without a parent request"
    assert final.profile is None
    assert final.per_node[0].status == "failed"
    assert processes[0].returncode == 0


@pytest.mark.parametrize("mode", ["deadline", "crash"])
def test_deadline_and_crash_reap_before_failed_status(tmp_path, monkeypatch, mode):
    if mode == "deadline":
        script = """
import json, os, sys, time
job = json.load(open(sys.argv[1]))
payload = {"run_id": "child", "status": "running", "job_type": "profile",
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": []}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
while True: time.sleep(1)
"""
        deadline = 0.05
    else:
        script = "import sys; sys.exit(7)"
        deadline = 10.0
    processes, _environments = _fake_subrun(monkeypatch, script)
    runner = _runner(tmp_path, deadline_s=deadline)
    terminal_observations = []
    runner.on_status = lambda _graph, status: terminal_observations.append(
        (status.status, processes[0].poll()))

    started = runner.run(
        _graph(),
        "source",
        plan_digest=_digest(mode),
        profile_attempt_order=1,
        run_id=f"profile-{mode}",
    )
    final = _wait(runner, started.run_id)

    assert final.status == "failed" and final.profile is None
    assert ("deadline" in (final.error or "") if mode == "deadline"
            else "without a valid terminal status" in (final.error or ""))
    assert processes[0].returncode is not None
    _wait_for_supervisor_cleanup(runner, started.run_id)
    assert started.run_id not in runner._procs
    terminal_codes = [code for state, code in terminal_observations if state == "failed"]
    assert terminal_codes and all(code is not None for code in terminal_codes)


def test_source_lease_loss_after_child_done_is_resanitized(tmp_path, monkeypatch):
    script = """
import json, os, sys, time
job = json.load(open(sys.argv[1]))
payload = {
    "run_id": "child", "status": "done", "job_type": "profile",
    "rows_processed": 9, "total_rows": 9, "ms": 1, "placement": "local",
    "per_node": [], "progress": 1.0,
    "profile": {"columns": [], "row_count": 9, "sampled": False,
                "not_previewable": False, "error": False}
}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
time.sleep(5)
"""
    processes, _environments = _fake_subrun(monkeypatch, script)
    runner = _runner(tmp_path)
    checks = 0

    def lose_lease(_run_id):
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise RuntimeError("lease expired")

    runner._check_source_leases = lose_lease
    started = runner.run(
        _graph(),
        "source",
        plan_digest=_digest("lease-loss"),
        profile_attempt_order=1,
        run_id="profile-source-lease-loss",
    )
    final = _wait(runner, started.run_id)

    assert final.status == "failed"
    assert final.error == "managed source lease was lost during execution"
    assert final.profile is None
    assert final.rows_processed == 0 and final.total_rows is None
    assert len(final.per_node) == 1 and final.per_node[0].status == "failed"
    assert final.per_node[0].error == final.error
    assert processes[0].returncode is not None
    _wait_for_supervisor_cleanup(runner, started.run_id)
    assert started.run_id not in runner._procs


def test_fixed_run_id_is_idempotent_only_for_the_same_parent_identity(tmp_path, monkeypatch):
    script = """
import json, os, sys, time
job = json.load(open(sys.argv[1]))
payload = {"run_id": "child", "status": "running", "job_type": "profile",
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": []}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
time.sleep(0.5)
"""
    _processes, _environments = _fake_subrun(monkeypatch, script)
    runner = _runner(tmp_path)
    kwargs = {
        "plan_digest": _digest("fixed-id"),
        "profile_attempt_order": 2,
        "run_id": "profile-fixed-id",
        "request_id": "request-fixed-id",
    }
    first = runner.run(_graph(), "source", **kwargs)
    assert runner.run(_graph(), "source", **kwargs) is first

    with pytest.raises(ValueError, match="different identity"):
        runner.run(_graph(), "source", **{
            **kwargs,
            "plan_digest": _digest("different-plan"),
        })
    with pytest.raises(ValueError, match="positive integer"):
        runner.run(
            _graph(), "source", plan_digest=_digest("invalid-order"),
            profile_attempt_order=0,
        )
    with pytest.raises(ValueError, match="positive integer"):
        runner.run(
            _graph(), "source", plan_digest=_digest("bool-order"),
            profile_attempt_order=True,
        )
    runner.cancel(first.run_id)
    assert _wait(runner, first.run_id).status == "cancelled"
