"""Process-lifecycle proofs for whole-dataset profile jobs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import types

import pyarrow as pa
import pytest

from hub.models import Graph, RunStatus
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
        yield metadb
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


def _wait_until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


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


def _admit_profile(metadb, graph: Graph, run_id: str, plan_digest: str, *,
                   live_kernel: bool = True) -> int:
    with metadb.session() as db:
        if db.get(metadb.User, "profile-owner") is None:
            db.add(metadb.User(id="profile-owner", name="Profile owner"))
        if db.get(metadb.Canvas, graph.id) is None:
            db.add(metadb.Canvas(
                id=graph.id, owner_id="profile-owner", name="Profile process",
                version=1, doc="{}",
            ))
    if live_kernel:
        claimed = metadb.claim_kernel(graph.id, "profile-kernel", "profile-test-token")
        assert claimed["won"] is True
    token, attempt_order = metadb.preallocate_profile_run_owner(
        run_id, "profile-owner", graph.id, graph.id, "source", "out", plan_digest,
    )
    won, _status = metadb.consume_profile_run_preallocation(
        run_id, token, canvas_id=graph.id, kernel_id="profile-kernel",
        target_node_id="source", target_port_id="out", plan_digest=plan_digest,
    )
    assert won
    return attempt_order


def test_full_profile_roundtrips_through_real_subrun_and_reaps(tmp_path):
    lance = pytest.importorskip("lance")
    source = tmp_path / "rows.lance"
    lance.write_dataset(pa.table({"x": [1, 2, 3], "y": ["a", "b", None]}), source)
    graph = _graph(str(source))
    graph.nodes[0].data["config"]["_input_revision_id"] = "1"
    input_manifest = [{
        "node_id": "source", "dataset_id": "dataset-source",
        "revision_id": "1", "provider": "lance",
        "resolved_at": "2026-07-16T00:00:00Z",
    }]
    with _isolated_metadata(tmp_path / "parent.db"):
        runner = _runner(tmp_path)
        started = runner.run(
            graph,
            "source",
            plan_digest=_digest("real-profile"),
            profile_attempt_order=7,
            run_id="profile-real-child",
            request_id="request-real-child",
            input_manifest=input_manifest,
        )
        final = _wait(runner, started.run_id)

    assert final.status == "done", final.error
    assert final.profile is not None and not final.profile.sampled
    assert final.profile.row_count == 3
    assert [column.name for column in final.profile.columns] == ["x", "y"]
    assert final.total_rows is None and final.rows_processed == 3
    assert final.target_node_id == "source"
    assert final.plan_digest == _digest("real-profile")
    assert final.profile_attempt_order == 7
    assert final.request_id == "request-real-child"
    assert final.profile.input_manifest == input_manifest
    _wait_for_supervisor_cleanup(runner, started.run_id)
    assert started.run_id not in runner._procs


def test_live_profile_heartbeats_prevent_false_stall_without_fake_progress(
        tmp_path, monkeypatch):
    script = """
import json, os, sys, time
job = json.load(open(sys.argv[1]))
payload = {"run_id": "child", "status": "running", "job_type": "profile",
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": [],
           "progress": None}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
while not os.path.exists(job["cancelFile"]): time.sleep(0.01)
"""
    _processes, _environments = _fake_subrun(monkeypatch, script)
    monkeypatch.setenv("DP_PROFILE_HEARTBEAT_S", "0.05")
    monkeypatch.setattr("hub.subprocess_runner._CANCEL_GRACE_S", 0.01)

    with _isolated_metadata(tmp_path / "profile-heartbeat.db") as metadb:
        graph = _graph()
        run_id = "profile-heartbeat"
        digest = _digest("heartbeat")
        attempt_order = _admit_profile(metadb, graph, run_id, digest)
        runner = _runner(tmp_path)
        running_publications = 0
        transient_failed = False

        def persist(observed_graph, status):
            nonlocal running_publications, transient_failed
            if status.status == "running":
                running_publications += 1
                if running_publications == 2 and not transient_failed:
                    transient_failed = True
                    raise OSError("transient heartbeat metadata outage")
            metadb.save_run_state(
                status.run_id, status.model_dump(), canvas_id=observed_graph.id,
                kernel_id="profile-kernel",
            )

        runner.on_status = persist
        started = runner.run(
            graph, "source", plan_digest=digest,
            profile_attempt_order=attempt_order, run_id=run_id,
        )
        try:
            _wait_until(
                lambda: running_publications >= 4,
                "unchanged running profile did not publish durable heartbeats",
            )
            # The real hub reaper runs in a process-global background thread. Prove deterministically
            # that this kernel-owned fixture has the live lease its durable RunState claims, instead of
            # depending on whether the 30-second reaper wakes during this isolated-metadata window.
            assert metadb.reap_orphaned_runs(only_kernel_runs=True) == 0
            time.sleep(0.12)

            durable = metadb.get_run_state(run_id)
            assert durable is not None
            assert durable["status"] == "running", durable.get("error")
            assert durable["progress"] is None
            assert metadb.run_stalled(run_id, 0.18) is False
            assert transient_failed is True
        finally:
            runner.cancel(started.run_id)
            cancelled = _wait(runner, started.run_id)
            _wait_until(
                lambda: (persisted := metadb.get_run_state(run_id)) is not None
                and persisted["status"] == "cancelled",
                "cancelled profile did not reach durable RunState",
            )
            _wait_until(
                lambda: run_id in runner._terminal_publication_done,
                "cancelled profile terminal publisher did not settle",
            )
            _wait_for_supervisor_cleanup(runner, started.run_id)
        assert cancelled.status == "cancelled"
        metadb.drop_kernel(graph.id, "profile-kernel")


def test_full_profile_fails_closed_without_posix_group_containment(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    monkeypatch.setattr("hub.profile_jobs.os.name", "nt")

    with pytest.raises(RuntimeError, match="POSIX process-group containment"):
        runner.run(
            _graph(), "source", plan_digest=_digest("non-posix"),
            profile_attempt_order=1, run_id="profile-non-posix",
        )

    assert runner.runs == {}
    assert runner._procs == {}


def test_reaped_terminal_retries_transient_db_publication_to_projection(
        tmp_path, monkeypatch):
    script = """
import json, os, sys
job = json.load(open(sys.argv[1]))
payload = {
    "run_id": "child", "status": "done", "job_type": "profile",
    "rows_processed": 2, "ms": 1, "placement": "local",
    "per_node": [], "progress": 1.0,
    "profile": {"columns": [], "row_count": 2, "sampled": False, "completeness": "complete",
                "not_previewable": False, "error": False}
}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
"""
    _processes, _environments = _fake_subrun(monkeypatch, script)
    with _isolated_metadata(tmp_path / "terminal-retry.db") as metadb:
        graph = _graph()
        run_id = "profile-terminal-retry"
        plan_digest = _digest("terminal-retry")
        attempt_order = _admit_profile(metadb, graph, run_id, plan_digest)
        runner = _runner(tmp_path)
        runner.publication_retry_wait = lambda _delay: None
        persisted = threading.Event()
        completed = threading.Event()
        terminal_calls = 0

        def save_status(observed_graph, status):
            nonlocal terminal_calls
            if status.status in ("done", "failed", "cancelled"):
                terminal_calls += 1
                if terminal_calls == 1:
                    raise OSError("transient terminal metadata failure")
            metadb.save_run_state(
                status.run_id, status.model_dump(), canvas_id=observed_graph.id,
                kernel_id="profile-kernel",
            )
            if status.status in ("done", "failed", "cancelled"):
                persisted.set()

        runner.on_status = save_status
        runner.on_complete = lambda *_args: completed.set()
        started = runner.run(
            graph, "source", plan_digest=plan_digest,
            profile_attempt_order=attempt_order, run_id=run_id,
        )
        final = _wait(runner, started.run_id)
        assert persisted.wait(timeout=5)
        assert completed.wait(timeout=5)

        assert final.status == "done"
        assert terminal_calls >= 2
        assert metadb.get_run_state(run_id)["status"] == "done"
        recovered = metadb.latest_profile_jobs(graph.id)[0]
        assert recovered["run_id"] == run_id and recovered["status"] == "done"
        assert recovered["profile_attempt_order"] == attempt_order


def test_definitive_terminal_rejection_is_not_retried_or_completed(
        tmp_path, monkeypatch):
    from hub.metadb import RunStatePublicationRejected

    runner = _runner(tmp_path)
    runner.publication_retry_wait = lambda _delay: pytest.fail(
        "definitive rejection must not retry")
    publication_calls = 0
    completion_calls = 0

    def reject(_graph, _status):
        nonlocal publication_calls
        publication_calls += 1
        raise RunStatePublicationRejected("owner fence rejected terminal")

    def complete(*_args):
        nonlocal completion_calls
        completion_calls += 1

    runner.on_complete = complete
    status = runner.retain_terminal_failure(
        _graph(),
        RunStatus(
            run_id="profile-rejected", status="failed", job_type="profile",
            target_node_id="source", target_port_id="out",
            plan_digest=_digest("rejected"),
            profile_attempt_order=1, placement="local", per_node=[],
            error="pre-spawn failure",
        ),
        reject,
    )
    _wait_until(
        lambda: status.run_id not in runner._terminal_persistence_pending,
        "definitively rejected terminal remained pending",
    )
    runner._emit(_graph(), status)
    time.sleep(0.02)

    assert publication_calls == 1
    assert completion_calls == 0


def test_pending_terminal_survives_bounded_eviction_until_publication(
        tmp_path, monkeypatch):
    monkeypatch.setattr("hub.subprocess_runner._MAX_RUNS", 2)
    runner = _runner(tmp_path)
    publication_entered = threading.Event()
    publication_release = threading.Event()

    def blocked_publication(_graph, _status):
        publication_entered.set()
        assert publication_release.wait(timeout=5)

    terminal = RunStatus(
        run_id="profile-pending-eviction", status="failed", job_type="profile",
        target_node_id="source", target_port_id="out",
        plan_digest=_digest("pending-eviction"),
        profile_attempt_order=1, placement="local", per_node=[], error="failed",
    )
    retained = runner.retain_terminal_failure(_graph(), terminal, blocked_publication)
    assert publication_entered.wait(timeout=5)
    with runner._lock:
        for index in range(4):
            run_id = f"live-{index}"
            runner.runs[run_id] = RunStatus(
                run_id=run_id, status="running", placement="local", per_node=[])
        runner._evict()
        assert runner.runs[retained.run_id].model_dump() == retained.model_dump()
        assert retained.run_id in runner._profile_identities
        assert retained.run_id in runner._terminal_persistence_pending

    publication_release.set()
    _wait_until(
        lambda: retained.run_id not in runner._terminal_persistence_pending,
        "published terminal remained pending",
    )
    _wait_until(
        lambda: retained.run_id not in runner.runs,
        "published terminal did not resume bounded eviction",
    )


@pytest.mark.parametrize("mode", ["done", "failed", "cancelled"])
def test_profile_terminals_enter_existing_history_once(tmp_path, monkeypatch, mode):
    from sqlalchemy import func, select

    from hub import metadb
    from hub.deps import _persist_run

    if mode == "done":
        script = """
import json, os, sys
job = json.load(open(sys.argv[1]))
payload = {
    "run_id": "child", "status": "done", "job_type": "profile",
    "rows_processed": 3, "ms": 1, "placement": "local",
    "per_node": [], "progress": 1.0,
    "profile": {"columns": [], "row_count": 3, "sampled": False, "completeness": "complete",
                "not_previewable": False, "error": False}
}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
"""
    elif mode == "failed":
        script = "import sys; sys.exit(9)"
    else:
        script = "import time; time.sleep(10)"
        monkeypatch.setattr("hub.subprocess_runner._CANCEL_GRACE_S", 0.01)
    _processes, _environments = _fake_subrun(monkeypatch, script)

    with _isolated_metadata(tmp_path / f"history-{mode}.db"):
        graph = _graph()
        run_id = f"profile-history-{mode}"
        plan_digest = _digest(f"history-{mode}")
        attempt_order = _admit_profile(metadb, graph, run_id, plan_digest)
        runner = _runner(tmp_path)
        from hub.observability import drain_sinks, register_sink_delivery
        telemetry = []
        deps = types.SimpleNamespace(telemetry_sinks=[
            register_sink_delivery(telemetry.append, kind="telemetry"),
        ])
        runner.on_status = lambda observed_graph, status: metadb.save_run_state(
            status.run_id, status.model_dump(), canvas_id=observed_graph.id,
            kernel_id="profile-kernel",
        )
        runner.on_complete = lambda g, target, status: _persist_run(
            deps, g, target, status)
        started = runner.run(
            graph, "source", plan_digest=plan_digest,
            profile_attempt_order=attempt_order, run_id=run_id,
        )
        if mode == "cancelled":
            runner.cancel(started.run_id)
        final = _wait(runner, started.run_id)
        _wait_until(
            lambda: started.run_id in runner._completion_done,
            "profile completion did not reach history",
        )

        with metadb.session() as session:
            records = session.scalar(select(func.count()).select_from(
                metadb.RunRecord).where(
                    metadb.RunRecord.canvas_id == graph.id,
                    metadb.RunRecord.run_id == run_id,
                ))
            record = session.scalar(select(metadb.RunRecord).where(
                metadb.RunRecord.canvas_id == graph.id,
                metadb.RunRecord.run_id == run_id,
            ))
        assert records == 1
        assert record is not None and record.status == mode
        if mode == "done":
            assert record.profile is not None
            assert json.loads(record.profile)["row_count"] == 3
        else:
            assert record.profile is None
        assert drain_sinks()
        assert len(telemetry) == 1 and telemetry[0]["status"] == mode
        assert final.status == mode


def test_profile_history_retry_is_idempotent_after_commit_unknown(tmp_path, monkeypatch):
    from sqlalchemy import func, select

    from hub import metadb
    from hub.deps import _persist_run

    with _isolated_metadata(tmp_path / "history-retry.db"):
        graph = _graph()
        run_id = "profile-history-retry"
        plan_digest = _digest("history-retry")
        attempt_order = _admit_profile(metadb, graph, run_id, plan_digest)
        runner = _runner(tmp_path)
        runner.publication_retry_wait = lambda _delay: None
        from hub.observability import drain_sinks, register_sink_delivery
        telemetry = []
        deps = types.SimpleNamespace(telemetry_sinks=[
            register_sink_delivery(telemetry.append, kind="telemetry"),
        ])
        real_record_run = metadb.record_run
        history_calls = 0

        def commit_unknown(**kwargs):
            nonlocal history_calls
            history_calls += 1
            result = real_record_run(**kwargs)
            if history_calls == 1:
                raise OSError("history commit acknowledgement lost")
            return result

        monkeypatch.setattr(metadb, "record_run", commit_unknown)
        runner.on_complete = lambda g, target, status: _persist_run(
            deps, g, target, status)
        status = RunStatus(
            run_id=run_id, status="failed", job_type="profile",
            target_node_id="source", target_port_id="out", plan_digest=plan_digest,
            profile_attempt_order=attempt_order, placement="local", per_node=[],
            error="pre-spawn failure",
        )
        runner.retain_terminal_failure(
            graph, status,
            lambda observed_graph, observed: metadb.save_run_state(
                observed.run_id, observed.model_dump(), canvas_id=observed_graph.id,
                kernel_id="profile-kernel",
            ),
        )
        _wait_until(
            lambda: run_id in runner._completion_done,
            "profile history retry did not complete",
        )

        with metadb.session() as session:
            records = session.scalar(select(func.count()).select_from(
                metadb.RunRecord).where(
                    metadb.RunRecord.canvas_id == graph.id,
                    metadb.RunRecord.run_id == run_id,
                ))
        assert history_calls == 2
        assert records == 1
        assert drain_sinks()
        assert len(telemetry) == 1 and telemetry[0]["run_id"] == run_id


def test_supervisor_exception_retries_failed_terminal_publication(
        tmp_path, monkeypatch):
    script = "import time; time.sleep(10)"
    processes, _environments = _fake_subrun(monkeypatch, script)
    with _isolated_metadata(tmp_path / "supervisor-terminal-retry.db") as metadb:
        graph = _graph()
        run_id = "profile-supervisor-terminal-retry"
        plan_digest = _digest("supervisor-terminal-retry")
        attempt_order = _admit_profile(metadb, graph, run_id, plan_digest)
        runner = _runner(tmp_path)
        runner.publication_retry_wait = lambda _delay: None
        persisted = threading.Event()
        terminal_calls = 0

        def crash_supervisor(*_args, **_kwargs):
            raise RuntimeError("simulated profile supervisor fault")

        monkeypatch.setattr(runner, "_watch_inner", crash_supervisor)

        def save_status(observed_graph, status):
            nonlocal terminal_calls
            if status.status in ("done", "failed", "cancelled"):
                terminal_calls += 1
                if terminal_calls == 1:
                    raise OSError("transient supervisor terminal metadata failure")
            metadb.save_run_state(
                status.run_id, status.model_dump(), canvas_id=observed_graph.id,
                kernel_id="profile-kernel",
            )
            if status.status in ("done", "failed", "cancelled"):
                persisted.set()

        runner.on_status = save_status
        started = runner.run(
            graph, "source", plan_digest=plan_digest,
            profile_attempt_order=attempt_order, run_id=run_id,
        )
        final = _wait(runner, started.run_id)
        assert persisted.wait(timeout=5)

        assert final.status == "failed"
        assert final.error == "execution supervisor failed"
        assert terminal_calls >= 2
        assert processes[0].returncode is not None
        assert metadb.get_run_state(run_id)["status"] == "failed"
        assert metadb.latest_profile_jobs(graph.id)[0]["status"] == "failed"


def test_late_profile_terminal_rejected_after_dead_kernel_fence(
        tmp_path):
    from sqlalchemy import func, select

    from hub import metadb

    with _isolated_metadata(tmp_path / "late-terminal-fence.db"):
        graph = _graph()
        run_id = "profile-late-after-reaper"
        plan_digest = _digest("late-after-reaper")
        attempt_order = _admit_profile(
            metadb, graph, run_id, plan_digest, live_kernel=False)
        metadb.save_run_state(
            run_id,
            RunStatus(
                run_id=run_id, status="running", job_type="profile",
                target_node_id="source", target_port_id="out", plan_digest=plan_digest,
                profile_attempt_order=attempt_order, placement="local", per_node=[],
            ).model_dump(),
            canvas_id=graph.id, kernel_id="profile-kernel",
        )
        assert metadb.reap_orphaned_runs(only_kernel_runs=True) == 1
        assert metadb.get_run_state(run_id)["status"] == "failed"

        runner = _runner(tmp_path)
        completion_calls = 0

        def complete(*_args):
            nonlocal completion_calls
            completion_calls += 1

        runner.on_status = lambda observed_graph, status: metadb.save_run_state(
            status.run_id, status.model_dump(), canvas_id=observed_graph.id,
            kernel_id="profile-kernel",
        )
        runner.on_complete = complete
        late_done = RunStatus(
            run_id=run_id, status="done", job_type="profile",
            target_node_id="source", target_port_id="out", plan_digest=plan_digest,
            profile_attempt_order=attempt_order, placement="local", per_node=[],
        )
        runner._complete(graph, "source", late_done)
        runner._emit(graph, late_done)
        _wait_until(
            lambda: run_id in runner._terminal_publication_rejected,
            "late terminal did not observe the permanent fence rejection",
        )

        assert completion_calls == 0
        assert metadb.get_run_state(run_id)["status"] == "failed"
        assert metadb.latest_profile_jobs(graph.id)[0]["status"] == "failed"
        with metadb.session() as session:
            history = session.scalar(select(func.count()).select_from(
                metadb.RunRecord).where(metadb.RunRecord.run_id == run_id))
        assert history == 0


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
            terminal_lease_states = []
            terminal_observed = threading.Event()

            def observe_terminal(_graph, status):
                if status.status in ("done", "failed", "cancelled"):
                    with runner._lock:
                        terminal_lease_states.append(
                            status.run_id in runner._source_leases)
                    terminal_observed.set()

            runner.on_status = observe_terminal
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
            assert terminal_observed.wait(timeout=5)
            assert not any(terminal_lease_states)
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
    "outputs": [{{"node_id": "forged-target", "port_id": "forged-port",
                 "wire": "dataset", "publication_kind": "result",
                 "outcome": "committed", "uri": "/forged", "rows": 4}}],
    "profile": {{"columns": [], "row_count": 4, "sampled": False, "completeness": "complete",
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
    assert final.outputs == [] and final.total_rows is None
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group containment")
def test_cancel_stops_and_reaps_profile_grandchild_side_effects(tmp_path, monkeypatch):
    marker = tmp_path / "profile-grandchild-cancel-side-effect"
    grandchild = f"""
import os, time
while True:
    with open({str(marker)!r}, "a") as output:
        output.write("x"); output.flush(); os.fsync(output.fileno())
    time.sleep(0.01)
"""
    script = f"""
import json, os, subprocess, sys, time
job = json.load(open(sys.argv[1]))
subprocess.Popen([sys.executable, "-c", {grandchild!r}])
payload = {{"run_id": "child", "status": "running", "job_type": "profile",
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": []}}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
while True: time.sleep(1)
"""
    processes, _environments = _fake_subrun(monkeypatch, script)
    monkeypatch.setattr("hub.subprocess_runner._CANCEL_GRACE_S", 0.01)
    runner = _runner(tmp_path)
    started = runner.run(
        _graph(), "source", plan_digest=_digest("grandchild-cancel"),
        profile_attempt_order=1, run_id="profile-grandchild-cancel",
    )
    _wait_for(marker)

    runner.cancel(started.run_id)
    final = _wait(runner, started.run_id)
    side_effect_size = marker.stat().st_size
    time.sleep(0.2)

    assert final.status == "cancelled"
    assert runner.cancel_acknowledged(started.run_id)
    assert processes[0].returncode is not None
    assert marker.stat().st_size == side_effect_size
    assert started.run_id not in runner._process_scopes


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group containment")
def test_direct_child_exit_tears_down_lingering_profile_descendants(tmp_path, monkeypatch):
    marker = tmp_path / "profile-grandchild-orphan-side-effect"
    grandchild = f"""
import os, time
while True:
    with open({str(marker)!r}, "a") as output:
        output.write("x"); output.flush(); os.fsync(output.fileno())
    time.sleep(0.01)
"""
    script = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {grandchild!r}])
time.sleep(0.1)
"""
    processes, _environments = _fake_subrun(monkeypatch, script)
    runner = _runner(tmp_path)
    started = runner.run(
        _graph(), "source", plan_digest=_digest("grandchild-orphan"),
        profile_attempt_order=1, run_id="profile-grandchild-orphan",
    )
    _wait_for(marker)

    final = _wait(runner, started.run_id)
    side_effect_size = marker.stat().st_size
    time.sleep(0.2)

    assert final.status == "failed"
    assert "without a valid terminal status" in (final.error or "")
    assert processes[0].returncode == 0
    assert marker.stat().st_size == side_effect_size
    assert started.run_id not in runner._process_scopes


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
    "rows_processed": 9, "ms": 1, "placement": "local",
    "per_node": [], "progress": 1.0,
    "profile": {"columns": [], "row_count": 9, "sampled": False, "completeness": "complete",
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
    second = runner.run(_graph(), "source", **kwargs)
    assert second is not first
    assert second.model_dump() == first.model_dump()

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
