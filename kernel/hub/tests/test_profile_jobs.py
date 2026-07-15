"""Exact profile jobs must behave like durable, cancellable execution jobs."""

from __future__ import annotations

import threading
import time

from hub.models import Graph, ProfileResult
from hub.profile_jobs import ProfileJobRunner


def _graph() -> Graph:
    return Graph.model_validate({"id": "profile-canvas", "version": 1, "nodes": [], "edges": []})


def _wait(runner: ProfileJobRunner, run_id: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = runner.status(run_id)
        if status.status in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError("profile job did not reach a terminal state")


def test_full_profile_is_a_queued_job_with_a_durable_result(monkeypatch):
    seen = []

    def complete(*_args, **_kwargs):
        return ProfileResult(row_count=42, sampled=False)

    monkeypatch.setattr("hub.profile_jobs.profile_node", complete)
    runner = ProfileJobRunner(None, {})
    runner.on_status = lambda _graph, status: seen.append(status.model_copy(deep=True))

    started = runner.run(_graph(), "node", plan_identity="revision-a", run_id="profile-1")
    assert started.job_type == "profile"
    assert started.plan_identity == "revision-a"

    final = _wait(runner, "profile-1")
    assert final.status == "done"
    assert final.profile and final.profile.row_count == 42 and not final.profile.sampled
    assert final.total_rows == final.rows_processed == 42
    assert [status.status for status in seen] == ["queued", "running", "done"]


def test_cancelling_full_profile_interrupts_its_exact_scope(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class Scope:
        interrupted = False

        def interrupt(self):
            self.interrupted = True

    scope = Scope()

    def slow(*_args, scope_callback=None, **_kwargs):
        assert scope_callback is not None
        scope_callback(scope)
        entered.set()
        assert release.wait(2)
        return ProfileResult(row_count=3, sampled=False)

    monkeypatch.setattr("hub.profile_jobs.profile_node", slow)
    runner = ProfileJobRunner(None, {})
    started = runner.run(_graph(), "node", plan_identity="revision-a", run_id="profile-cancel")
    assert entered.wait(2)
    runner.cancel(started.run_id)
    assert scope.interrupted is True
    release.set()

    final = _wait(runner, started.run_id)
    assert final.status == "cancelled"
    assert final.profile is None
