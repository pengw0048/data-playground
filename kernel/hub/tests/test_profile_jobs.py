"""Whole-dataset profile jobs must behave like durable, cancellable execution jobs."""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from hub.models import Graph, ProfileResult, RunEstimate, RunStatus
from hub.profile_jobs import ProfileJobRunner


def _graph() -> Graph:
    return Graph.model_validate({"id": "profile-canvas", "version": 1, "nodes": [], "edges": []})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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

    started = runner.run(
        _graph(), "node", plan_digest=_digest("revision-a"), run_id="profile-1",
        request_id="req_profile_1",
    )
    assert started.job_type == "profile"
    assert started.plan_digest == _digest("revision-a")
    assert started.request_id == "req_profile_1"

    final = _wait(runner, "profile-1")
    assert final.status == "done"
    assert final.profile and final.profile.row_count == 42 and not final.profile.sampled
    assert final.total_rows == final.rows_processed == 42
    assert [status.status for status in seen] == ["queued", "running", "done"]


def test_cancelling_full_profile_interrupts_its_own_scope(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class Scope:
        interrupted = False

        def interrupt(self):
            self.interrupted = True
            release.set()

    scope = Scope()

    def slow(*_args, scope_callback=None, **_kwargs):
        assert scope_callback is not None
        scope_callback(scope)
        entered.set()
        assert release.wait(2)
        raise RuntimeError("DuckDB query interrupted")

    monkeypatch.setattr("hub.profile_jobs.profile_node", slow)
    runner = ProfileJobRunner(None, {})
    started = runner.run(
        _graph(), "node", plan_digest=_digest("revision-a"), run_id="profile-cancel",
    )
    assert entered.wait(2)
    runner.cancel(started.run_id)
    assert scope.interrupted is True
    final = _wait(runner, started.run_id)
    assert final.status == "cancelled"
    assert final.profile is None


def test_stateless_second_hub_routes_cancel_to_owning_kernel_scope(monkeypatch):
    """The receiving hub has no run_index entry; cancellation still reaches the actual DuckDB scope."""
    from hub import kernel, kernel_backend, metadb
    from hub.routers import runs as run_routes

    entered = threading.Event()
    release = threading.Event()
    durable: dict[str, dict] = {}

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
    owner = ProfileJobRunner(None, {})
    owner.on_status = lambda _graph, status: durable.update({
        status.run_id: status.model_dump(),
    })
    started = owner.run(
        _graph(), "node", plan_digest=_digest("revision-a"), run_id="profile-cross-hub",
    )
    assert entered.wait(2)

    # A second hub reconstructs only KernelBackend. Its fake transport stands in for the real kernel
    # command channel and deliberately owns no ProfileJobRunner memory itself.
    receiving_backend = kernel_backend.KernelBackend(base=None, spawner=None)
    monkeypatch.setattr(metadb, "get_run_state", lambda rid: durable.get(rid))
    monkeypatch.setattr(metadb, "backend_job", lambda _rid: None)
    monkeypatch.setattr(metadb, "kernel_for_run", lambda rid: {
        "endpoint": "owning-kernel", "token": "token", "kernel_id": "kernel-1",
    } if rid == started.run_id else None)

    class OrdinaryRunOwner:
        def cancel(self, run_id):
            raise AssertionError(f"profile was misrouted to the ordinary run owner: {run_id}")

    def post(_endpoint, path, _token, body, **_kwargs):
        assert path == "/cancel"
        return kernel._cancel_owned_run(
            OrdinaryRunOwner(), owner, body["run_id"], durable[body["run_id"]],
        ).model_dump()

    monkeypatch.setattr(kernel_backend, "_post", post)

    class ReceivingDeps:
        run_index = {}
        run_owner = {}

        @staticmethod
        def kernel_backend():
            return receiving_backend

    monkeypatch.setattr(run_routes, "get_deps", lambda: ReceivingDeps())
    returned = run_routes.run_cancel(started.run_id, uid="local")

    assert returned.run_id == started.run_id
    assert scope.interrupted is True
    release.set()
    assert _wait(owner, started.run_id).status == "cancelled"


def test_profile_admission_is_enforced_for_direct_http_call(monkeypatch):
    from hub import observability
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    graph = {"id": "profile-admission", "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    submissions: list[str | None] = []
    audits: list[tuple[object, object, dict]] = []

    class Owner:
        def profile_job(self, _graph, node_id, plan_digest, request_id=None):
            submissions.append(request_id)
            return RunStatus(
                run_id=f"profile-http-{len(submissions)}", status="queued", job_type="profile",
                target_node_id=node_id, plan_digest=plan_digest, request_id=request_id,
            )

        def cancel(self, run_id):  # pragma: no cover - runner interface parity
            raise AssertionError(run_id)

    owner = Owner()
    monkeypatch.setattr(deps, "kernel_backend", lambda: owner)
    monkeypatch.setattr(observability, "emit_audit", lambda action, outcome, **kwargs: audits.append(
        (action, outcome, kwargs)
    ))
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=None, bytes=None, placement="local", needs_confirm=True,
    ))
    client = TestClient(app)

    malformed = client.post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": "raw-graph-identity",
    })
    assert malformed.status_code == 422
    assert submissions == []

    request_id = "req_profile_http_01"
    body = {"graph": graph, "nodeId": "source", "planDigest": _digest("plan-http")}
    rejected = client.post(
        "/api/run/profile-job", json=body, headers={"X-Request-Id": request_id},
    )
    assert rejected.status_code == 409
    assert submissions == []

    admitted = client.post(
        "/api/run/profile-job", json={**body, "confirmed": True},
        headers={"X-Request-Id": request_id},
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["jobType"] == "profile"
    assert admitted.json()["requestId"] == request_id
    assert submissions == [request_id]

    # The server contract explicitly permits a known-small scan without confirmation.
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    small = client.post("/api/run/profile-job", json={
        **body, "planDigest": _digest("plan-small"),
    }, headers={"X-Request-Id": request_id})
    assert small.status_code == 200, small.text
    assert submissions == [request_id, request_id]
    assert [outcome.value for _, outcome, _ in audits] == ["failure", "success", "success"]
    for _action, _outcome, event in audits:
        assert event["request_id"] == request_id
        assert event["attrs"].get("job_type") == "profile"
        assert "graph" not in event["attrs"] and "profile" not in event["attrs"]
    for run_id in ("profile-http-1", "profile-http-2"):
        deps.run_index.pop(run_id, None)
        deps.run_owner.pop(run_id, None)


def _profile_status(run_id: str, state: str, plan: str) -> dict:
    return RunStatus(
        run_id=run_id, status=state, job_type="profile", target_node_id="node",
        plan_digest=_digest(plan),
    ).model_dump()


def test_latest_profile_projection_survives_detail_pruning_and_unrelated_churn(
        tmp_path, monkeypatch):
    with _isolated_metadata(tmp_path / "profile-retention.db") as metadb:
        monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 1)
        canvas_id = "profile-recovery-order"
        metadb.save_run_state(
            "profile-old-retry", _profile_status("profile-old-retry", "running", "plan-a"),
            canvas_id=canvas_id,
        )
        time.sleep(0.002)
        metadb.save_run_state(
            "profile-new-retry", _profile_status("profile-new-retry", "running", "plan-a"),
            canvas_id=canvas_id,
        )
        metadb.save_run_state(
            "profile-new-retry", _profile_status("profile-new-retry", "done", "plan-a"),
            canvas_id=canvas_id,
        )
        # The old scan finishes later. Global detail retention keeps old and prunes newer, but the
        # independent projection must continue to retain the newer retry's terminal document.
        metadb.save_run_state(
            "profile-old-retry", _profile_status("profile-old-retry", "done", "plan-a"),
            canvas_id=canvas_id,
        )
        assert metadb.get_run_state("profile-new-retry") is None
        assert metadb.get_run_state("profile-old-retry")["status"] == "done"
        recovered = metadb.latest_profile_jobs(canvas_id)
        assert len(recovered) == 1
        assert recovered[0]["run_id"] == "profile-new-retry"

        for index in range(3):
            metadb.save_run_state(
                f"unrelated-{index}",
                RunStatus(run_id=f"unrelated-{index}", status="done").model_dump(),
                canvas_id=f"unrelated-canvas-{index}",
            )
        assert metadb.latest_profile_jobs(canvas_id)[0]["run_id"] == "profile-new-retry"


def test_profile_projection_watermark_prevents_evicted_identity_resurrection(
        tmp_path, monkeypatch):
    with _isolated_metadata(tmp_path / "profile-watermark.db") as metadb:
        monkeypatch.setattr(metadb, "_PROFILE_LATEST_MAX", 2)
        canvas_id = "profile-watermark"
        for index in range(3):
            run_id = f"profile-plan-{index}"
            metadb.save_run_state(
                run_id, _profile_status(run_id, "running", f"plan-{index}"),
                canvas_id=canvas_id,
            )
            time.sleep(0.002)

        recovered = metadb.latest_profile_jobs(canvas_id)
        assert {item["plan_digest"] for item in recovered} == {
            _digest("plan-1"), _digest("plan-2"),
        }
        # A delayed status from the evicted run sees an absent identity. The retained cutoff rejects it;
        # neither RunState detail nor the worker's memory can recreate a projection below the watermark.
        metadb.save_run_state(
            "profile-plan-0", _profile_status("profile-plan-0", "running", "plan-0"),
            canvas_id=canvas_id,
        )
        assert {item["plan_digest"] for item in metadb.latest_profile_jobs(canvas_id)} == {
            _digest("plan-1"), _digest("plan-2"),
        }


def test_concurrent_same_plan_updates_keep_newer_submission_on_sqlite(tmp_path):
    with _isolated_metadata(tmp_path / "profile-concurrent.db") as metadb:
        canvas_id = "profile-concurrent"
        metadb.save_run_state(
            "profile-concurrent-old",
            _profile_status("profile-concurrent-old", "running", "same-plan"),
            canvas_id=canvas_id,
        )
        time.sleep(0.002)
        metadb.save_run_state(
            "profile-concurrent-new",
            _profile_status("profile-concurrent-new", "running", "same-plan"),
            canvas_id=canvas_id,
        )
        barrier = threading.Barrier(3)
        failures: list[BaseException] = []

        def finish(run_id: str) -> None:
            try:
                barrier.wait(timeout=2)
                metadb.save_run_state(
                    run_id, _profile_status(run_id, "done", "same-plan"),
                    canvas_id=canvas_id,
                )
            except BaseException as exc:  # noqa: BLE001 - thread failures must reach the assertion
                failures.append(exc)

        threads = [
            threading.Thread(target=finish, args=("profile-concurrent-old",)),
            threading.Thread(target=finish, args=("profile-concurrent-new",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=5)
        assert not failures
        assert all(not thread.is_alive() for thread in threads)
        assert metadb.latest_profile_jobs(canvas_id)[0]["run_id"] == "profile-concurrent-new"


def test_canvas_delete_blocks_active_profile_then_removes_projection(tmp_path):
    with _isolated_metadata(tmp_path / "profile-canvas-delete.db") as metadb:
        canvas_id = "profile-delete"
        with metadb.session() as db:
            db.add(metadb.User(id="profile-owner", name="Profile owner"))
            db.add(metadb.Canvas(
                id=canvas_id, owner_id="profile-owner", name="Profile canvas",
                version=1, doc="{}",
            ))
        metadb.save_run_state(
            "profile-delete-run",
            _profile_status("profile-delete-run", "running", "delete-plan"),
            canvas_id=canvas_id,
        )
        with pytest.raises(metadb.ActiveBackendJobsError, match="active run"):
            metadb.delete_canvas_cascade(canvas_id)
        metadb.save_run_state(
            "profile-delete-run",
            _profile_status("profile-delete-run", "done", "delete-plan"),
            canvas_id=canvas_id,
        )
        metadb.delete_canvas_cascade(canvas_id)
        assert metadb.latest_profile_jobs(canvas_id) == []
        with metadb.session() as db:
            assert db.get(metadb.ProfileJobRetention, canvas_id) is None
