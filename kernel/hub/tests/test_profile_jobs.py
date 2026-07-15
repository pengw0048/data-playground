"""Whole-dataset profile jobs must behave like durable, cancellable execution jobs."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import threading
import time
import types

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hub.models import RunEstimate, RunStatus


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


def test_profile_preflight_never_calls_ordinary_adapter_data_paths(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app

    deps = get_deps()
    ordinary_calls: list[str] = []
    fingerprints: list[str] = []

    class EagerFullRunOnlyAdapter:
        """Models an adapter that materializes before applying any ordinary scan limit."""

        name = "eager-full-run-only"

        def fingerprint(self, uri: str) -> str:
            fingerprints.append(uri)
            return "metadata-revision-1"

        def scan(self, *_args, **_kwargs):
            ordinary_calls.append("scan")
            raise AssertionError("profile preflight must not call scan(limit=0)")

        def schema(self, *_args, **_kwargs):
            ordinary_calls.append("schema")
            raise AssertionError("profile preflight must not call ordinary schema")

        def count(self, *_args, **_kwargs):
            ordinary_calls.append("count")
            raise AssertionError("profile preflight must not call potentially full-scanning count")

    adapter = EagerFullRunOnlyAdapter()
    monkeypatch.setattr(deps, "resolve_adapter", lambda _uri: adapter)
    monkeypatch.setattr(deps, "kernel_backend", lambda: None)
    graph = {
        "id": "metadata-only-profile-preflight", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": "eager://whole-dataset"}},
        }],
        "edges": [],
    }

    response = TestClient(app).post("/api/run/profile-estimate", json={
        "graph": graph, "nodeId": "source",
    })

    assert response.status_code == 200, response.text
    assert response.json()["needsConfirm"] is True
    assert ordinary_calls == []
    assert fingerprints == ["eager://whole-dataset"]


def test_profile_estimate_holds_one_source_scope_across_size_and_digest(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes
    from hub import storage as storage_module

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    active = 0
    observed: list[tuple[str, tuple[str, ...]]] = []

    @contextlib.contextmanager
    def pinned_scope(_storage, uris, owner):
        nonlocal active
        active += 1
        observed.append((owner, tuple(uris)))
        try:
            yield
        finally:
            active -= 1

    def estimate(*_args):
        assert active == 1, "size must be observed under the endpoint's generation lease"
        return RunEstimate(rows=10, bytes=100, placement="local", needs_confirm=False)

    def digest(*_args):
        assert active == 1, "digest must share the size observation's generation lease"
        return _digest("same-managed-generation")

    monkeypatch.setattr(storage_module, "source_read_scope", pinned_scope)
    monkeypatch.setattr(run_routes, "_profile_job_estimate", estimate)
    monkeypatch.setattr(run_routes, "_profile_plan_digest", digest)
    response = TestClient(app).post("/api/run/profile-estimate", json={
        "graph": {
            "id": "same-generation-profile", "version": 1,
            "nodes": [{
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": uri}},
            }],
            "edges": [],
        },
        "nodeId": "source",
    })

    assert response.status_code == 200, response.text
    assert response.json()["planDigest"] == _digest("same-managed-generation")
    assert active == 0
    assert len(observed) == 1
    assert observed[0][0].startswith("profile-preflight:")
    assert observed[0][1] == (uri,)


def test_profile_preflight_and_identity_never_recursively_fingerprint_a_directory(
        tmp_path, monkeypatch):
    from hub.main import app

    directory = tmp_path / "partitioned-dataset"
    (directory / "day=2026-07-15").mkdir(parents=True)
    monkeypatch.setattr(
        "hub.plugins.adapters.os.walk",
        lambda *_args, **_kwargs: pytest.fail("profile fingerprint recursively enumerated a directory"),
    )
    body = {
        "graph": {
            "id": "bounded-directory-fingerprint", "version": 1,
            "nodes": [{
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": str(directory)}},
            }],
            "edges": [],
        },
        "nodeId": "source",
    }
    client = TestClient(app)

    estimate = client.post("/api/run/profile-estimate", json=body)
    identity = client.post("/api/run/profile-identity", json=body)

    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["needsConfirm"] is True
    assert identity.status_code == 200, identity.text
    assert identity.json()["planDigest"] == estimate.json()["planDigest"]


@pytest.mark.parametrize(
    ("target_kind", "target_config"),
    [
        ("metric", {"agg": "count"}),
        ("aggregate", {"groupBy": "id", "aggs": "count(*) AS n"}),
    ],
)
def test_profile_admission_keeps_unknown_sources_unknown_despite_stale_actuals(
        monkeypatch, target_kind, target_config):
    from hub.deps import get_deps
    from hub.models import Graph
    from hub.routers import runs as run_routes

    deps = get_deps()
    actual_reads = 0

    class UnknownCsvAdapter:
        def metadata_count(self, _uri):
            return None

        def fingerprint(self, _uri):
            return "csv-revision"

    def stale_actuals(*_args):
        nonlocal actual_reads
        actual_reads += 1
        return {"source": 1, "target": 1}

    monkeypatch.setattr(deps, "resolve_adapter", lambda _uri: UnknownCsvAdapter())
    monkeypatch.setattr(deps, "kernel_backend", lambda: None)
    monkeypatch.setattr(run_routes, "_actuals_for", stale_actuals)
    graph = Graph.model_validate({
        "id": f"unknown-source-{target_kind}", "version": 1,
        "nodes": [
            {
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": "unknown://large.csv"}},
            },
            {
                "id": "target", "type": target_kind, "position": {"x": 0, "y": 0},
                "data": {"config": target_config},
            },
        ],
        "edges": [{
            "id": "source-target", "source": "source", "target": "target",
            "data": {"wire": "dataset"},
        }],
    })

    estimate = run_routes._profile_job_estimate(graph, "target", deps)

    assert estimate.needs_confirm is True
    assert estimate.rows is None and estimate.bytes is None
    assert "some cone sizes unknown" in (estimate.breakdown or "")
    assert actual_reads == 0, "unbound historical actuals must not erase unknown source work"


def test_profile_admission_preserves_known_rows_but_requires_confirmation_for_unknown_width(
        monkeypatch):
    from hub.deps import get_deps
    from hub.models import Graph
    from hub.routers import runs as run_routes

    deps = get_deps()

    class KnownAdapter:
        def metadata_count(self, _uri):
            return 10

        def fingerprint(self, _uri):
            return "known-revision"

    monkeypatch.setattr(deps, "resolve_adapter", lambda _uri: KnownAdapter())
    monkeypatch.setattr(deps, "kernel_backend", lambda: None)
    graph = Graph.model_validate({
        "id": "known-small-profile", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": "known://small.parquet"}},
        }],
        "edges": [],
    })

    estimate = run_routes._profile_job_estimate(graph, "source", deps)

    assert estimate.rows == 10
    assert estimate.bytes is None
    assert estimate.needs_confirm is True
    assert "some cone sizes unknown" in (estimate.breakdown or "")


def test_profile_metadata_count_does_not_fabricate_bytes_or_bypass_direct_api_admission(
        monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    ordinary_calls: list[str] = []
    dispatches = 0

    class MillionRowMetadataAdapter:
        def metadata_count(self, _uri):
            return 1_000_000

        def fingerprint(self, _uri):
            return "million-row-metadata-revision"

        def scan(self, *_args, **_kwargs):
            ordinary_calls.append("scan")
            raise AssertionError("metadata-only preflight called scan")

        def schema(self, *_args, **_kwargs):
            ordinary_calls.append("schema")
            raise AssertionError("metadata-only preflight called schema")

        def count(self, *_args, **_kwargs):
            ordinary_calls.append("count")
            raise AssertionError("metadata-only preflight called count")

    class Owner:
        def estimate(self, plan, rows, byts=None):
            return deps.runner.estimate(plan, rows, byts)

        def profile_job(self, *_args, **_kwargs):
            nonlocal dispatches
            dispatches += 1
            raise AssertionError("unconfirmed profile reached the execution owner")

    graph = {
        "id": "profile-million-row-metadata", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": "metadata://million-rows"}},
        }],
        "edges": [],
    }
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, graph["id"]) is None:
            db.add(run_routes.metadb.Canvas(
                id=graph["id"], owner_id="local", name="Million-row metadata profile",
                version=1, doc="{}",
            ))
    monkeypatch.setattr(deps, "resolve_adapter", lambda _uri: MillionRowMetadataAdapter())
    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    client = TestClient(app)

    preflight = client.post("/api/run/profile-estimate", json={
        "graph": graph, "nodeId": "source",
    })

    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["rows"] == 1_000_000
    assert preflight.json()["bytes"] is None
    assert preflight.json()["needsConfirm"] is True
    rejected = client.post("/api/run/profile-job", json={
        "graph": graph,
        "nodeId": "source",
        "planDigest": preflight.json()["planDigest"],
        "submissionId": "00000000-0000-4000-8000-000000000020",
    })
    assert rejected.status_code == 409, rejected.text
    assert dispatches == 0
    assert ordinary_calls == []


def test_profile_union_estimate_sums_every_metadata_known_input(monkeypatch):
    from hub.deps import get_deps
    from hub.models import Graph
    from hub.routers import runs as run_routes

    deps = get_deps()
    counts = {"metadata://left": 3_000_000, "metadata://right": 3_000_000}

    class KnownAdapter:
        def metadata_count(self, uri):
            return counts[uri]

        def fingerprint(self, uri):
            return f"revision:{uri}"

    monkeypatch.setattr(deps, "resolve_adapter", lambda _uri: KnownAdapter())
    monkeypatch.setattr(deps, "kernel_backend", lambda: None)
    graph = Graph.model_validate({
        "id": "known-union-profile", "version": 1,
        "nodes": [
            {
                "id": "left", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": "metadata://left"}},
            },
            {
                "id": "right", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": "metadata://right"}},
            },
            {
                "id": "union", "type": "union", "position": {"x": 0, "y": 0},
                "data": {"config": {"mode": "all", "align": "name"}},
            },
        ],
        "edges": [
            {"id": "left-union", "source": "left", "target": "union"},
            {"id": "right-union", "source": "right", "target": "union"},
        ],
    })

    estimate = run_routes._profile_job_estimate(graph, "union", deps)

    assert estimate.rows == 6_000_000
    assert estimate.bytes is None
    assert estimate.needs_confirm is True


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
    submitted_run_ids: list[str] = []
    audits: list[tuple[object, object, dict]] = []

    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, graph["id"]) is None:
            db.add(run_routes.metadb.Canvas(
                id=graph["id"], owner_id="local", name="Profile admission",
                version=1, doc="{}",
            ))

    class Owner:
        def profile_job(self, _graph, node_id, plan_digest, *, run_id, admission_token,
                        request_id=None):
            durable = run_routes.metadb.get_run_state(run_id)
            assert durable is not None and durable["job_type"] == "profile"
            won, queued = run_routes.metadb.consume_profile_run_preallocation(
                run_id, admission_token, canvas_id=graph["id"], kernel_id="profile-http-kernel",
                target_node_id=node_id, plan_digest=plan_digest,
            )
            assert won
            submissions.append(request_id)
            submitted_run_ids.append(run_id)
            return RunStatus(**queued)

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
    monkeypatch.setattr(
        run_routes, "_profile_plan_digest", lambda *_args: _digest("plan-http"))
    client = TestClient(app)
    submission_id = "00000000-0000-4000-8000-000000000001"

    malformed = client.post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": "raw-graph-identity",
        "submissionId": submission_id,
    })
    assert malformed.status_code == 422
    invalid_submission = client.post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": _digest("plan-http"),
        "submissionId": "not-a-v4-uuid",
    })
    assert invalid_submission.status_code == 422
    assert submissions == []

    request_id = "req_profile_http_01"
    body = {
        "graph": graph, "nodeId": "source", "planDigest": _digest("plan-http"),
        "submissionId": submission_id,
    }
    rejected = client.post(
        "/api/run/profile-job", json=body, headers={"X-Request-Id": request_id},
    )
    assert rejected.status_code == 409
    assert submissions == []

    spoofed = client.post(
        "/api/run/profile-job",
        json={**body, "planDigest": "0" * 64, "confirmed": True},
        headers={"X-Request-Id": request_id},
    )
    assert spoofed.status_code == 409
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
    monkeypatch.setattr(
        run_routes, "_profile_plan_digest", lambda *_args: _digest("plan-small"))
    small = client.post("/api/run/profile-job", json={
        **body, "planDigest": _digest("plan-small"),
        "submissionId": "00000000-0000-4000-8000-000000000002",
    }, headers={"X-Request-Id": request_id})
    assert small.status_code == 200, small.text
    assert submissions == [request_id, request_id]
    assert [outcome.value for _, outcome, _ in audits] == [
        "failure", "failure", "success", "success",
    ]
    for _action, _outcome, event in audits:
        assert event["request_id"] == request_id
        assert event["attrs"].get("job_type") == "profile"
        assert "graph" not in event["attrs"] and "profile" not in event["attrs"]
    for run_id in submitted_run_ids:
        deps.run_index.pop(run_id, None)
        deps.run_owner.pop(run_id, None)


def test_profile_submission_failure_discards_the_exact_unconsumed_identity(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-submit-failure"
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Profile submit failure",
                version=1, doc="{}",
            ))

    captured: list[str] = []

    class Owner:
        def profile_job(self, _graph, _node_id, _plan_digest, *, run_id, admission_token,
                        request_id=None):
            assert admission_token and request_id
            assert run_routes.metadb.get_run_state(run_id) is not None
            captured.append(run_id)
            raise OSError("kernel unavailable before admission")

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: _digest("submit"))
    client = TestClient(app, raise_server_exceptions=False)
    body = {
        "graph": graph, "nodeId": "source", "planDigest": _digest("submit"),
        "submissionId": "00000000-0000-4000-8000-000000000003",
    }
    response = client.post(
        "/api/run/profile-job",
        json=body,
        headers={"X-Request-Id": "req_profile_submit_failure"},
    )
    replay = client.post("/api/run/profile-job", json=body)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert replay.status_code == 200
    assert replay.json()["runId"] == response.json()["runId"]
    assert replay.json()["status"] == "failed"
    assert len(captured) == 1
    assert run_routes.metadb.get_run_state(captured[0])["status"] == "failed"
    assert run_routes.metadb.terminal_run_status(captured[0]) == "failed"


def test_profile_route_adopts_a_response_lost_after_kernel_admission(monkeypatch):
    from hub.deps import get_deps
    from hub.kernel import ProfileJobBody, _dispatch_profile_job
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-response-lost-route"
    plan_digest = _digest("response-lost-route")
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Response lost profile",
                version=1, doc="{}",
            ))
    admitted: list[tuple[str, int]] = []
    child_spawns = 0

    class KernelProfileRunner:
        def run(self, _graph, node_id, *, plan_digest, profile_attempt_order, run_id,
                request_id=None):
            nonlocal child_spawns
            child_spawns += 1
            return RunStatus(
                run_id=run_id, status="queued", job_type="profile",
                target_node_id=node_id, plan_digest=plan_digest,
                profile_attempt_order=profile_attempt_order, request_id=request_id,
            )

    profile_runner = KernelProfileRunner()
    admission_lock = threading.Lock()

    class Owner:
        def profile_job(self, _graph, node_id, digest, *, run_id, admission_token,
                        request_id=None):
            returned = _dispatch_profile_job(
                body=ProfileJobBody(
                    run_id=run_id, admission_token=admission_token,
                    graph=_graph.model_dump(by_alias=True), node_id=node_id,
                    plan_digest=digest, request_id=request_id,
                ),
                kernel_canvas=canvas_id, kernel_id="response-lost-kernel",
                profile_runner=profile_runner, profile_admission_lock=admission_lock,
                metadata=run_routes.metadb,
            )
            admitted.append((run_id, returned.profile_attempt_order))
            raise OSError("kernel command response was lost after child admission")

        def cancel(self, _run_id):  # pragma: no cover - runner interface parity
            raise AssertionError("unexpected cancel")

    owner = Owner()
    monkeypatch.setattr(deps, "kernel_backend", lambda: owner)
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: plan_digest)

    response = TestClient(app).post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": plan_digest,
        "submissionId": "00000000-0000-4000-8000-000000000004",
    })

    assert response.status_code == 200, response.text
    assert response.json()["runId"] == admitted[0][0]
    assert response.json()["status"] == "queued"
    assert child_spawns == 1
    assert run_routes.metadb.get_run_state(admitted[0][0])["status"] == "queued"

    run_id, attempt_order = admitted[0]
    run_routes.metadb.save_run_state(
        run_id, RunStatus(
            run_id=run_id, status="failed", job_type="profile",
            target_node_id="source", plan_digest=plan_digest,
            profile_attempt_order=attempt_order,
        ).model_dump(),
        canvas_id=canvas_id, kernel_id="response-lost-kernel",
    )
    deps.run_index.pop(run_id, None)
    deps.run_owner.pop(run_id, None)


def test_profile_route_returns_retained_terminal_before_db_retry_converges(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-retained-terminal-route"
    plan_digest = _digest("retained-terminal-route")
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Retained terminal profile",
                version=1, doc="{}",
            ))
    admitted: list[tuple[str, int]] = []

    class Owner:
        def profile_job(self, _graph, node_id, digest, *, run_id, admission_token,
                        request_id=None):
            won, queued = run_routes.metadb.consume_profile_run_preallocation(
                run_id, admission_token, canvas_id=canvas_id,
                kernel_id="retained-terminal-kernel", target_node_id=node_id,
                plan_digest=digest,
            )
            assert won
            attempt_order = queued["profile_attempt_order"]
            admitted.append((run_id, attempt_order))
            return RunStatus(
                run_id=run_id, status="failed", job_type="profile",
                target_node_id=node_id, plan_digest=digest,
                profile_attempt_order=attempt_order,
                error="source lease unavailable before spawn",
            )

        def cancel(self, _run_id):  # pragma: no cover - runner interface parity
            raise AssertionError("unexpected cancel")

    owner = Owner()
    monkeypatch.setattr(deps, "kernel_backend", lambda: owner)
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: plan_digest)

    response = TestClient(app).post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": plan_digest,
        "submissionId": "00000000-0000-4000-8000-000000000005",
    })

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    run_id, attempt_order = admitted[0]
    assert response.json()["runId"] == run_id
    # The runner's retry has not published yet, so the durable row can still be queued at response time.
    assert run_routes.metadb.get_run_state(run_id)["status"] == "queued"

    run_routes.metadb.save_run_state(
        run_id, RunStatus(
            run_id=run_id, status="failed", job_type="profile",
            target_node_id="source", plan_digest=plan_digest,
            profile_attempt_order=attempt_order,
        ).model_dump(),
        canvas_id=canvas_id, kernel_id="retained-terminal-kernel",
    )
    deps.run_index.pop(run_id, None)
    deps.run_owner.pop(run_id, None)


def test_same_submission_replay_adopts_after_source_identity_changes(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-idempotent-adopt"
    plan_digest = _digest("idempotent-adopt")
    submission_id = "00000000-0000-4000-8000-000000000006"
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Idempotent profile",
                version=1, doc="{}",
            ))
    dispatches = 0

    class Owner:
        def profile_job(self, _graph, node_id, digest, *, run_id, admission_token,
                        request_id=None):
            nonlocal dispatches
            dispatches += 1
            won, queued = run_routes.metadb.consume_profile_run_preallocation(
                run_id, admission_token, canvas_id=canvas_id,
                kernel_id="idempotent-adopt-kernel", target_node_id=node_id,
                plan_digest=digest,
            )
            assert won
            return RunStatus(**queued)

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: plan_digest)
    body = {
        "graph": graph, "nodeId": "source", "planDigest": plan_digest,
        "submissionId": submission_id,
    }
    client = TestClient(app)
    first = client.post("/api/run/profile-job", json=body)
    assert first.status_code == 200, first.text

    def current_source_must_not_be_consulted(*_args):
        raise AssertionError("consumed replay consulted mutable source identity")

    monkeypatch.setattr(run_routes, "_profile_plan_digest", current_source_must_not_be_consulted)
    replay = client.post("/api/run/profile-job", json=body)
    wrong_digest = client.post("/api/run/profile-job", json={
        **body, "planDigest": _digest("different-idempotent-plan"),
    })
    wrong_node = client.post("/api/run/profile-job", json={
        **body, "nodeId": "different-node",
    })

    assert replay.status_code == 200, replay.text
    assert replay.json()["runId"] == first.json()["runId"]
    assert replay.json()["profileAttemptOrder"] == first.json()["profileAttemptOrder"]
    assert wrong_digest.status_code == 409
    assert wrong_node.status_code == 409
    assert dispatches == 1
    deps.run_index.pop(first.json()["runId"], None)
    deps.run_owner.pop(first.json()["runId"], None)


def test_unconsumed_submission_replays_only_for_the_same_current_digest(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-unconsumed-replay"
    old_digest = _digest("unconsumed-old")
    submission_id = "00000000-0000-4000-8000-000000000007"
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Unconsumed profile",
                version=1, doc="{}",
            ))
    reservation = run_routes.metadb.preallocate_or_adopt_profile_run_owner(
        submission_id, "local", None, canvas_id, "source", old_digest,
    )
    assert reservation.should_dispatch and reservation.admission_token is not None
    dispatches = 0

    class Owner:
        def profile_job(self, *_args, **_kwargs):
            nonlocal dispatches
            dispatches += 1
            raise AssertionError("stale unconsumed submission reached the kernel")

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: _digest("changed"))
    response = TestClient(app).post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": old_digest,
        "submissionId": submission_id,
    })

    assert response.status_code == 200, response.text
    assert response.json()["runId"] == reservation.run_id
    assert response.json()["status"] == "failed"
    assert "source changed" in response.json()["error"]
    assert dispatches == 0
    durable = run_routes.metadb.get_run_state(reservation.run_id)
    assert durable is not None and durable["status"] == "failed"
    assert run_routes.metadb.terminal_run_status(reservation.run_id) == "failed"
    assert run_routes.metadb.latest_profile_jobs(canvas_id) == []


def test_unconsumed_submission_with_unchanged_digest_reuses_token_and_order(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-unconsumed-same-source"
    plan_digest = _digest("unconsumed-same-source")
    submission_id = "00000000-0000-4000-8000-000000000010"
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Unconsumed same profile",
                version=1, doc="{}",
            ))
    reservation = run_routes.metadb.preallocate_or_adopt_profile_run_owner(
        submission_id, "local", None, canvas_id, "source", plan_digest,
    )
    observed: list[tuple[str, str, int]] = []

    class Owner:
        def profile_job(self, _graph, node_id, digest, *, run_id, admission_token,
                        request_id=None):
            assert run_id == reservation.run_id
            assert admission_token == reservation.admission_token
            won, queued = run_routes.metadb.consume_profile_run_preallocation(
                run_id, admission_token, canvas_id=canvas_id,
                kernel_id="unconsumed-same-kernel", target_node_id=node_id,
                plan_digest=digest,
            )
            assert won
            observed.append((run_id, admission_token, queued["profile_attempt_order"]))
            return RunStatus(**queued)

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: plan_digest)
    response = TestClient(app).post("/api/run/profile-job", json={
        "graph": graph, "nodeId": "source", "planDigest": plan_digest,
        "submissionId": submission_id,
    })

    assert response.status_code == 200, response.text
    assert observed == [(
        reservation.run_id, reservation.admission_token, reservation.attempt_order,
    )]
    assert response.json()["runId"] == reservation.run_id
    assert response.json()["profileAttemptOrder"] == reservation.attempt_order
    deps.run_index.pop(reservation.run_id, None)
    deps.run_owner.pop(reservation.run_id, None)


def test_concurrent_same_submission_spawns_one_profile_attempt(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-concurrent-submission"
    plan_digest = _digest("concurrent-submission")
    submission_id = "00000000-0000-4000-8000-000000000011"
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Concurrent profile",
                version=1, doc="{}",
            ))
    child_spawns = 0
    owner_calls = 0
    owner_lock = threading.Lock()

    class Owner:
        def profile_job(self, _graph, node_id, digest, *, run_id, admission_token,
                        request_id=None):
            nonlocal child_spawns, owner_calls
            with owner_lock:
                owner_calls += 1
                won, queued = run_routes.metadb.consume_profile_run_preallocation(
                    run_id, admission_token, canvas_id=canvas_id,
                    kernel_id="concurrent-submission-kernel", target_node_id=node_id,
                    plan_digest=digest,
                )
                if won:
                    child_spawns += 1
                    time.sleep(0.05)
                return RunStatus(**queued)

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: plan_digest)
    body = {
        "graph": graph, "nodeId": "source", "planDigest": plan_digest,
        "submissionId": submission_id,
    }
    barrier = threading.Barrier(2)
    responses = []

    def submit() -> None:
        barrier.wait(timeout=5)
        responses.append(TestClient(app).post("/api/run/profile-job", json=body))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(responses) == 2
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["runId"] for response in responses}) == 1
    assert len({response.json()["profileAttemptOrder"] for response in responses}) == 1
    assert child_spawns == 1
    assert owner_calls in (1, 2)
    run_id = responses[0].json()["runId"]
    deps.run_index.pop(run_id, None)
    deps.run_owner.pop(run_id, None)


def test_profile_submission_binding_survives_terminal_detail_compaction(tmp_path):
    with _isolated_metadata(tmp_path / "profile-submission-compaction.db") as metadb:
        canvas_id = "profile-submission-compaction"
        submission_id = "00000000-0000-4000-8000-000000000008"
        plan_digest = _digest("submission-compaction")
        _create_profile_canvas(metadb, canvas_id)
        reservation = metadb.preallocate_or_adopt_profile_run_owner(
            submission_id, "profile-owner", canvas_id, canvas_id,
            "node", plan_digest,
        )
        won, _queued = metadb.consume_profile_run_preallocation(
            reservation.run_id, reservation.admission_token,
            canvas_id=canvas_id, kernel_id="profile-kernel",
            target_node_id="node", plan_digest=plan_digest,
        )
        assert won
        metadb.save_run_state(
            reservation.run_id,
            _profile_status(
                reservation.run_id, "done", "submission-compaction",
                reservation.attempt_order,
            ),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        with metadb.session() as db:
            db.delete(db.get(metadb.RunState, reservation.run_id))
            latest = db.get(metadb.ProfileJobLatest, (canvas_id, "node", plan_digest))
            assert latest is not None
            db.delete(latest)

        replay = metadb.preallocate_or_adopt_profile_run_owner(
            submission_id, "profile-owner", canvas_id, canvas_id,
            "node", plan_digest,
        )
        assert replay.run_id == reservation.run_id
        assert replay.attempt_order == reservation.attempt_order
        assert replay.status["status"] == "done"
        assert replay.status["job_type"] == "profile"
        assert replay.status["target_node_id"] == "node"
        assert replay.status["plan_digest"] == plan_digest
        assert not replay.should_dispatch and replay.admission_token is None
        with metadb.session() as db:
            retention = db.get(metadb.ProfileJobRetention, canvas_id)
            assert retention.next_attempt_order == 2


def test_same_submission_with_different_profile_identity_is_conflict(tmp_path):
    with _isolated_metadata(tmp_path / "profile-submission-conflict.db") as metadb:
        canvas_id = "profile-submission-conflict"
        submission_id = "00000000-0000-4000-8000-000000000009"
        plan_digest = _digest("submission-conflict")
        _create_profile_canvas(metadb, canvas_id)
        metadb.preallocate_or_adopt_profile_run_owner(
            submission_id, "profile-owner", canvas_id, canvas_id,
            "node", plan_digest,
        )

        with pytest.raises(metadb.ProfileSubmissionConflict):
            metadb.lookup_profile_submission(
                submission_id, "profile-owner", canvas_id, canvas_id,
                "other-node", plan_digest,
            )
        with pytest.raises(metadb.ProfileSubmissionConflict):
            metadb.lookup_profile_submission(
                submission_id, "profile-owner", canvas_id, canvas_id,
                "node", _digest("different-plan"),
            )


def test_existing_submission_adoption_does_not_reverse_projection_lock_order(
        tmp_path, monkeypatch):
    with _isolated_metadata(tmp_path / "profile-submission-lock-order.db") as metadb:
        canvas_id = "profile-submission-lock-order"
        submission_id = "00000000-0000-4000-8000-000000000013"
        plan_digest = _digest("submission-lock-order")
        _create_profile_canvas(metadb, canvas_id)
        first = metadb.preallocate_or_adopt_profile_run_owner(
            submission_id, "profile-owner", canvas_id, canvas_id,
            "node", plan_digest,
        )

        def retention_lock_must_not_run(*_args, **_kwargs):
            raise AssertionError("existing adoption acquired ProfileJobRetention before RunState")

        monkeypatch.setattr(metadb, "_lock_profile_retention", retention_lock_must_not_run)
        replay = metadb.preallocate_or_adopt_profile_run_owner(
            submission_id, "profile-owner", canvas_id, canvas_id,
            "node", plan_digest,
        )

        assert replay.run_id == first.run_id
        assert replay.attempt_order == first.attempt_order
        assert replay.admission_token == first.admission_token


def test_concurrent_sqlite_profile_reservations_converge_before_insert(tmp_path):
    with _isolated_metadata(tmp_path / "profile-submission-sqlite-race.db") as metadb:
        canvas_id = "profile-submission-sqlite-race"
        submission_id = "00000000-0000-4000-8000-000000000015"
        plan_digest = _digest("submission-sqlite-race")
        _create_profile_canvas(metadb, canvas_id)
        barrier = threading.Barrier(2)
        reservations = []
        failures = []

        def reserve() -> None:
            try:
                barrier.wait(timeout=5)
                reservations.append(metadb.preallocate_or_adopt_profile_run_owner(
                    submission_id, "profile-owner", canvas_id, canvas_id,
                    "node", plan_digest,
                ))
            except BaseException as exc:  # noqa: BLE001 - surface thread failures
                failures.append(exc)

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert len(reservations) == 2
        assert len({reservation.run_id for reservation in reservations}) == 1
        assert len({reservation.admission_token for reservation in reservations}) == 1
        assert len({reservation.attempt_order for reservation in reservations}) == 1
        with metadb.session() as session:
            retention = session.get(metadb.ProfileJobRetention, canvas_id)
            assert retention.next_attempt_order == 2


@pytest.mark.parametrize("restricted_kind", ["transform", "custom-profile-plugin"])
def test_shared_mode_full_profile_rejects_code_and_plugin_cones(
        monkeypatch, restricted_kind):
    from hub.deps import get_deps
    from hub.models import Graph
    from hub.routers import runs as run_routes

    deps = get_deps()
    graph = Graph.model_validate({
        "id": "profile-shared-containment",
        "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {}}},
            {"id": "restricted", "type": restricted_kind, "data": {"config": {}}},
        ],
        "edges": [{
            "id": "source-restricted", "source": "source", "target": "restricted",
            "sourceHandle": "data", "targetHandle": "data",
        }],
    })
    monkeypatch.setattr(run_routes.auth, "auth_enabled", lambda: True)

    with pytest.raises(HTTPException) as rejected:
        run_routes._require_full_profile_containment(graph, "restricted", deps)

    assert rejected.value.status_code == 403
    if restricted_kind == "custom-profile-plugin":
        assert "custom plugin" in str(rejected.value.detail)
    else:
        assert "Python/control-flow" in str(rejected.value.detail)


def test_shared_mode_full_profile_rejects_third_party_source_adapters(monkeypatch):
    from hub.deps import get_deps
    from hub.models import Graph
    from hub.plugins.adapters import DuckDBAdapter
    from hub.routers import runs as run_routes

    class ThirdPartyAdapter(DuckDBAdapter):
        name = "third-party"

    real = get_deps()
    deps = types.SimpleNamespace(
        builtin_kinds=real.builtin_kinds,
        resolve_adapter=lambda _uri: ThirdPartyAdapter(),
    )
    graph = Graph.model_validate({
        "id": "profile-custom-adapter-containment",
        "version": 1,
        "nodes": [{
            "id": "source", "type": "source",
            "data": {"config": {"uri": "third-party://dataset"}},
        }],
        "edges": [],
    })
    monkeypatch.setattr(run_routes.auth, "auth_enabled", lambda: True)

    with pytest.raises(HTTPException) as rejected:
        run_routes._require_full_profile_containment(graph, "source", deps)

    assert rejected.value.status_code == 403
    assert "third-party dataset adapter" in str(rejected.value.detail)

    monkeypatch.setattr(run_routes.auth, "auth_enabled", lambda: False)
    run_routes._require_full_profile_containment(graph, "source", deps)


def test_consumed_replay_bypasses_new_dispatch_containment_gate(monkeypatch):
    from hub.deps import get_deps
    from hub.main import app
    from hub.routers import runs as run_routes

    deps = get_deps()
    uri = deps.catalog.get_table("tbl_events").uri
    canvas_id = "profile-containment-adopt"
    plan_digest = _digest("containment-adopt")
    submission_id = "00000000-0000-4000-8000-000000000012"
    graph = {"id": canvas_id, "version": 1, "nodes": [{
        "id": "source", "type": "source", "position": {"x": 0, "y": 0},
        "data": {"title": "source", "config": {"uri": uri}},
    }], "edges": []}
    with run_routes.metadb.session() as db:
        if db.get(run_routes.metadb.Canvas, canvas_id) is None:
            db.add(run_routes.metadb.Canvas(
                id=canvas_id, owner_id="local", name="Containment adopt",
                version=1, doc="{}",
            ))

    class Owner:
        def profile_job(self, _graph, node_id, digest, *, run_id, admission_token,
                        request_id=None):
            won, queued = run_routes.metadb.consume_profile_run_preallocation(
                run_id, admission_token, canvas_id=canvas_id,
                kernel_id="containment-adopt-kernel", target_node_id=node_id,
                plan_digest=digest,
            )
            assert won
            return RunStatus(**queued)

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: plan_digest)
    body = {
        "graph": graph, "nodeId": "source", "planDigest": plan_digest,
        "submissionId": submission_id,
    }
    client = TestClient(app)
    first = client.post("/api/run/profile-job", json=body)
    assert first.status_code == 200, first.text

    def dispatch_gate_must_not_run(*_args):
        raise AssertionError("consumed replay entered new-dispatch containment")

    monkeypatch.setattr(run_routes, "_require_full_profile_containment", dispatch_gate_must_not_run)
    monkeypatch.setattr(run_routes, "_profile_plan_digest", dispatch_gate_must_not_run)
    replay = client.post("/api/run/profile-job", json=body)

    assert replay.status_code == 200, replay.text
    assert replay.json()["runId"] == first.json()["runId"]
    deps.run_index.pop(first.json()["runId"], None)
    deps.run_owner.pop(first.json()["runId"], None)


def _profile_status(run_id: str, state: str, plan: str, attempt_order: int) -> dict:
    return RunStatus(
        run_id=run_id, status=state, job_type="profile", target_node_id="node",
        plan_digest=_digest(plan), profile_attempt_order=attempt_order,
    ).model_dump()


def _create_profile_canvas(metadb, canvas_id: str, owner: str = "profile-owner") -> None:
    with metadb.session() as db:
        if db.get(metadb.User, owner) is None:
            db.add(metadb.User(id=owner, name="Profile owner"))
        db.add(metadb.Canvas(
            id=canvas_id, owner_id=owner, name="Profile canvas", version=1, doc="{}",
        ))


def _admit_profile(metadb, canvas_id: str, run_id: str, plan: str) -> int:
    token, attempt_order = metadb.preallocate_profile_run_owner(
        run_id, "profile-owner", canvas_id, canvas_id, "node", _digest(plan),
    )
    won, _status = metadb.consume_profile_run_preallocation(
        run_id, token, canvas_id=canvas_id, kernel_id="profile-kernel",
        target_node_id="node", plan_digest=_digest(plan),
    )
    assert won
    return attempt_order


def test_profile_preallocation_is_monotonic_token_fenced_and_idempotent(tmp_path):
    with _isolated_metadata(tmp_path / "profile-admission.db") as metadb:
        canvas_id = "profile-admission-order"
        _create_profile_canvas(metadb, canvas_id)
        token_a, order_a = metadb.preallocate_profile_run_owner(
            "profile-a", "profile-owner", canvas_id, canvas_id, "node", _digest("plan"),
        )
        token_b, order_b = metadb.preallocate_profile_run_owner(
            "profile-b", "profile-owner", canvas_id, canvas_id, "node", _digest("plan"),
        )
        assert (order_a, order_b) == (1, 2)

        won, queued = metadb.consume_profile_run_preallocation(
            "profile-a", token_a, canvas_id=canvas_id, kernel_id="kernel-a",
            target_node_id="node", plan_digest=_digest("plan"),
        )
        assert won and queued["profile_attempt_order"] == order_a
        replay_won, replay = metadb.consume_profile_run_preallocation(
            "profile-a", token_a, canvas_id=canvas_id, kernel_id="kernel-a",
            target_node_id="node", plan_digest=_digest("plan"),
        )
        assert not replay_won and replay["run_id"] == "profile-a"
        replay_won, _ = metadb.consume_profile_run_preallocation(
            "profile-a", "consumed-capability", canvas_id=canvas_id, kernel_id="kernel-a",
            target_node_id="node", plan_digest=_digest("plan"),
        )
        assert not replay_won
        with pytest.raises(RuntimeError, match="different kernel"):
            metadb.consume_profile_run_preallocation(
                "profile-a", token_a, canvas_id=canvas_id, kernel_id="kernel-b",
                target_node_id="node", plan_digest=_digest("plan"),
            )
        assert metadb.admitted_profile_run_status(
            "profile-a", "profile-owner", canvas_id,
            canvas_id=canvas_id, target_node_id="node", plan_digest=_digest("plan"),
            attempt_order=order_a,
        ) is not None
        assert metadb.admitted_profile_run_status(
            "profile-a", "wrong-owner", canvas_id,
            canvas_id=canvas_id, target_node_id="node", plan_digest=_digest("plan"),
            attempt_order=order_a,
        ) is None

        assert metadb.discard_run_preallocation(
            "profile-b", token_b, "profile-owner", canvas_id)


def test_profile_submission_settlement_adopts_a_racing_token_consume(
        tmp_path, monkeypatch):
    with _isolated_metadata(tmp_path / "profile-settlement-race.db") as metadb:
        canvas_id = "profile-settlement-race"
        run_id = "profile-response-lost"
        plan_digest = _digest("plan")
        _create_profile_canvas(metadb, canvas_id)
        token, attempt_order = metadb.preallocate_profile_run_owner(
            run_id, "profile-owner", canvas_id, canvas_id, "node", plan_digest,
        )
        settlement_at_owner_lock = threading.Event()
        allow_settlement = threading.Event()
        original_lock = metadb._lock_existing_run_identity

        def delayed_owner_lock(db, candidate_run_id):
            if threading.current_thread().name == "profile-settlement":
                settlement_at_owner_lock.set()
                assert allow_settlement.wait(timeout=5)
            return original_lock(db, candidate_run_id)

        monkeypatch.setattr(metadb, "_lock_existing_run_identity", delayed_owner_lock)
        result: list[tuple[str, dict | None]] = []
        failures: list[BaseException] = []

        def settle() -> None:
            try:
                result.append(metadb.settle_profile_submission_failure(
                    run_id, token, "profile-owner", canvas_id,
                    canvas_id=canvas_id, target_node_id="node",
                    plan_digest=plan_digest, attempt_order=attempt_order,
                ))
            except BaseException as exc:  # noqa: BLE001 - surface thread failures
                failures.append(exc)

        thread = threading.Thread(target=settle, name="profile-settlement")
        thread.start()
        assert settlement_at_owner_lock.wait(timeout=5)
        won, _ = metadb.consume_profile_run_preallocation(
            run_id, token, canvas_id=canvas_id, kernel_id="profile-kernel",
            target_node_id="node", plan_digest=plan_digest,
        )
        assert won
        allow_settlement.set()
        thread.join(timeout=5)

        assert not thread.is_alive() and failures == []
        assert result[0][0] == "admitted"
        assert result[0][1]["run_id"] == run_id
        with metadb.session() as db:
            assert db.get(metadb.RunState, run_id).kernel_id == "profile-kernel"


def test_preallocated_profile_status_requires_exact_identity_and_kernel(tmp_path):
    with _isolated_metadata(tmp_path / "profile-status-fence.db") as metadb:
        canvas_id = "profile-status-fence"
        _create_profile_canvas(metadb, canvas_id)
        attempt_order = _admit_profile(metadb, canvas_id, "profile-fenced", "plan")

        with pytest.raises(metadb.RunStatePublicationRejected, match="preallocated identity"):
            metadb.save_run_state(
                "profile-fenced",
                RunStatus(run_id="profile-fenced", status="running").model_dump(),
                canvas_id=canvas_id, kernel_id="profile-kernel",
            )
        with pytest.raises(metadb.RunStatePublicationRejected, match="different kernel"):
            metadb.save_run_state(
                "profile-fenced",
                _profile_status("profile-fenced", "running", "plan", attempt_order),
                canvas_id=canvas_id, kernel_id="other-kernel",
            )
        with pytest.raises(metadb.RunStatePublicationRejected, match="preallocated identity"):
            metadb.save_run_state(
                "profile-fenced",
                _profile_status("profile-fenced", "running", "other-plan", attempt_order),
                canvas_id=canvas_id, kernel_id="profile-kernel",
            )

        metadb.save_run_state(
            "profile-fenced",
            _profile_status("profile-fenced", "running", "plan", attempt_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        assert metadb.get_run_state("profile-fenced")["status"] == "running"


def test_profile_preallocation_requires_a_real_locked_canvas(tmp_path):
    with _isolated_metadata(tmp_path / "profile-missing-canvas.db") as metadb:
        with pytest.raises(RuntimeError, match="canvas does not exist"):
            metadb.preallocate_profile_run_owner(
                "profile-missing", "profile-owner", None, "missing-canvas",
                "node", _digest("plan"),
            )
        assert metadb.get_run_state("profile-missing") is None
        with metadb.session() as db:
            assert db.get(metadb.ProfileJobRetention, "missing-canvas") is None


def test_kernel_rejects_a_wrong_canvas_profile_before_consume_or_spawn(tmp_path):
    from hub.kernel import ProfileJobBody, _dispatch_profile_job

    with _isolated_metadata(tmp_path / "profile-wrong-kernel-canvas.db") as metadb:
        canvas_id = "profile-right-canvas"
        wrong_canvas = "profile-wrong-canvas"
        run_id = "profile-wrong-canvas-run"
        plan_digest = _digest("plan")
        _create_profile_canvas(metadb, canvas_id)
        _create_profile_canvas(metadb, wrong_canvas)
        token, _attempt_order = metadb.preallocate_profile_run_owner(
            run_id, "profile-owner", canvas_id, canvas_id, "node", plan_digest,
        )

        class Runner:
            calls = 0

            def run(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("wrong-canvas profile reached the runner")

        runner = Runner()
        body = ProfileJobBody(
            run_id=run_id, admission_token=token,
            graph={
                "id": wrong_canvas, "version": 1,
                "nodes": [{"id": "node", "type": "source", "data": {"config": {}}}],
                "edges": [],
            },
            node_id="node", plan_digest=plan_digest,
        )
        with pytest.raises(RuntimeError, match="kernel canvas"):
            _dispatch_profile_job(
                body=body, kernel_canvas=canvas_id, kernel_id="profile-kernel",
                profile_runner=runner, profile_admission_lock=threading.Lock(),
                metadata=metadb,
            )

        assert runner.calls == 0
        with metadb.session() as db:
            state = db.get(metadb.RunState, run_id)
            assert state.preallocation_token == token and state.kernel_id is None
        assert metadb.latest_profile_jobs(canvas_id) == []
        assert metadb.latest_profile_jobs(wrong_canvas) == []


def test_profile_cancel_waits_for_consume_to_runner_registration(tmp_path):
    from hub.kernel import (
        ProfileJobBody, _cancel_with_profile_admission, _dispatch_profile_job,
    )

    with _isolated_metadata(tmp_path / "profile-cancel-registration.db") as metadb:
        canvas_id = "profile-cancel-registration"
        run_id = "profile-cancel-registration-run"
        plan_digest = _digest("plan")
        _create_profile_canvas(metadb, canvas_id)
        token, attempt_order = metadb.preallocate_profile_run_owner(
            run_id, "profile-owner", canvas_id, canvas_id, "node", plan_digest,
        )
        admission_lock = threading.Lock()
        registration_window = threading.Event()
        allow_registration = threading.Event()

        class ProfileRunner:
            def __init__(self):
                self.runs: dict[str, RunStatus] = {}

            def run(self, *_args, **_kwargs):
                registration_window.set()
                assert allow_registration.wait(timeout=5)
                status = RunStatus(
                    run_id=run_id, status="running", job_type="profile",
                    target_node_id="node", plan_digest=plan_digest,
                    profile_attempt_order=attempt_order,
                )
                self.runs[run_id] = status
                return status

            def status(self, candidate):
                if candidate not in self.runs:
                    raise KeyError(candidate)
                return self.runs[candidate]

            def cancel(self, candidate):
                current = self.status(candidate)
                cancelled = current.model_copy(update={"status": "cancelled"})
                self.runs[candidate] = cancelled
                return cancelled

        class RunRunner:
            def cancel(self, candidate):
                raise AssertionError(f"ordinary runner received profile cancel {candidate}")

        profile_runner = ProfileRunner()
        body = ProfileJobBody(
            run_id=run_id, admission_token=token,
            graph={
                "id": canvas_id, "version": 1,
                "nodes": [{"id": "node", "type": "source", "data": {"config": {}}}],
                "edges": [],
            },
            node_id="node", plan_digest=plan_digest,
        )
        start_result: list[object] = []
        cancel_result: list[object] = []

        starter = threading.Thread(target=lambda: start_result.append(
            _dispatch_profile_job(
                body=body, kernel_canvas=canvas_id, kernel_id="profile-kernel",
                profile_runner=profile_runner, profile_admission_lock=admission_lock,
                metadata=metadb,
            )))
        starter.start()
        assert registration_window.wait(timeout=5)
        canceller = threading.Thread(target=lambda: cancel_result.append(
            _cancel_with_profile_admission(
                RunRunner(), profile_runner, run_id, metadb.get_run_state(run_id),
                admission_lock, lambda: metadb.get_run_state(run_id),
            )))
        canceller.start()
        time.sleep(0.05)
        assert canceller.is_alive(), "cancel crossed the consume-to-registration mutex"

        allow_registration.set()
        starter.join(timeout=5)
        canceller.join(timeout=5)

        assert not starter.is_alive() and not canceller.is_alive()
        assert getattr(start_result[0], "status") == "running"
        assert getattr(cancel_result[0], "status") == "cancelled"


def test_pre_spawn_failure_retries_terminal_persistence_while_kernel_is_live(
        tmp_path, monkeypatch):
    from hub.kernel import ProfileJobBody, _dispatch_profile_job
    from hub.profile_jobs import ProfileProcessRunner

    with _isolated_metadata(tmp_path / "profile-pre-spawn-persistence.db") as metadb:
        canvas_id = "profile-pre-spawn-persistence"
        run_id = "profile-pre-spawn-persistence-run"
        kernel_id = "profile-live-kernel"
        plan_digest = _digest("plan")
        _create_profile_canvas(metadb, canvas_id)
        metadb.claim_kernel(canvas_id, kernel_id, "kernel-token")
        token, attempt_order = metadb.preallocate_profile_run_owner(
            run_id, "profile-owner", canvas_id, canvas_id, "node", plan_digest,
        )
        runner = ProfileProcessRunner(
            str(tmp_path / "workspace"), str(tmp_path / "data"),
        )
        runner.publication_retry_wait = lambda _delay: None

        def fail_before_spawn(*_args, **_kwargs):
            raise OSError("source lease unavailable before spawn")

        monkeypatch.setattr(runner, "_claim_source_leases", fail_before_spawn)
        terminal_persisted = threading.Event()

        class TransientMetadata:
            saves = 0

            @staticmethod
            def consume_profile_run_preallocation(*args, **kwargs):
                return metadb.consume_profile_run_preallocation(*args, **kwargs)

            @classmethod
            def save_run_state(cls, *args, **kwargs):
                cls.saves += 1
                if cls.saves == 1:
                    raise OSError("transient metadata write failure")
                metadb.save_run_state(*args, **kwargs)
                terminal_persisted.set()

        body = ProfileJobBody(
            run_id=run_id, admission_token=token,
            graph={
                "id": canvas_id, "version": 1,
                "nodes": [{"id": "node", "type": "source", "data": {"config": {}}}],
                "edges": [],
            },
            node_id="node", plan_digest=plan_digest,
        )
        returned = _dispatch_profile_job(
            body=body, kernel_canvas=canvas_id, kernel_id=kernel_id,
            profile_runner=runner, profile_admission_lock=threading.Lock(),
            metadata=TransientMetadata,
        )

        assert getattr(returned, "status") == "failed"
        assert runner.status(run_id).status == "failed"
        assert run_id not in runner._procs
        assert terminal_persisted.wait(timeout=5)
        assert TransientMetadata.saves >= 2
        assert metadb.get_kernel(canvas_id)["kernel_id"] == kernel_id
        assert metadb.get_run_state(run_id)["status"] == "failed"
        recovered = metadb.latest_profile_jobs(canvas_id)[0]
        assert recovered["status"] == "failed"
        assert recovered["profile_attempt_order"] == attempt_order


def test_profile_status_rejects_a_wrong_projection_canvas(tmp_path):
    with _isolated_metadata(tmp_path / "profile-wrong-status-canvas.db") as metadb:
        canvas_id = "profile-status-right-canvas"
        wrong_canvas = "profile-status-wrong-canvas"
        run_id = "profile-status-wrong-canvas-run"
        _create_profile_canvas(metadb, canvas_id)
        _create_profile_canvas(metadb, wrong_canvas)
        attempt_order = _admit_profile(metadb, canvas_id, run_id, "plan")

        with pytest.raises(metadb.RunStatePublicationRejected, match="different canvas"):
            metadb.save_run_state(
                run_id, _profile_status(run_id, "running", "plan", attempt_order),
                canvas_id=wrong_canvas, kernel_id="profile-kernel",
            )

        assert metadb.latest_profile_jobs(wrong_canvas) == []
        assert metadb.latest_profile_jobs(canvas_id)[0]["status"] == "queued"


def test_profile_status_cannot_bypass_preallocation_or_advance_the_projection(tmp_path):
    with _isolated_metadata(tmp_path / "profile-direct-save.db") as metadb:
        canvas_id = "profile-direct-save"
        _create_profile_canvas(metadb, canvas_id)
        with pytest.raises(metadb.RunStatePublicationRejected, match="preallocated"):
            metadb.save_run_state(
                "forged-profile",
                _profile_status("forged-profile", "running", "plan", 1),
                canvas_id=canvas_id,
            )
        assert metadb.get_run_state("forged-profile") is None
        assert metadb.latest_profile_jobs(canvas_id) == []
        with metadb.session() as db:
            assert db.get(metadb.ProfileJobRetention, canvas_id) is None


def test_latest_profile_projection_survives_detail_pruning_and_unrelated_churn(
        tmp_path, monkeypatch):
    with _isolated_metadata(tmp_path / "profile-retention.db") as metadb:
        monkeypatch.setattr(metadb, "_RUN_STATE_MAX", 1)
        canvas_id = "profile-recovery-order"
        _create_profile_canvas(metadb, canvas_id)
        old_order = _admit_profile(metadb, canvas_id, "profile-old-retry", "plan-a")
        metadb.save_run_state(
            "profile-old-retry", _profile_status(
                "profile-old-retry", "running", "plan-a", old_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        new_order = _admit_profile(metadb, canvas_id, "profile-new-retry", "plan-a")
        metadb.save_run_state(
            "profile-new-retry", _profile_status(
                "profile-new-retry", "running", "plan-a", new_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        metadb.save_run_state(
            "profile-new-retry", _profile_status(
                "profile-new-retry", "done", "plan-a", new_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        # The old scan finishes later. Global detail retention keeps old and prunes newer, but the
        # independent projection must continue to retain the newer retry's terminal document.
        metadb.save_run_state(
            "profile-old-retry", _profile_status(
                "profile-old-retry", "done", "plan-a", old_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
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


def test_dead_kernel_reaper_terminalizes_the_profile_projection(tmp_path):
    with _isolated_metadata(tmp_path / "profile-dead-kernel.db") as metadb:
        canvas_id = "profile-dead-kernel"
        run_id = "profile-owned-by-dead-kernel"
        _create_profile_canvas(metadb, canvas_id)
        attempt_order = _admit_profile(metadb, canvas_id, run_id, "dead-plan")
        running = _profile_status(run_id, "running", "dead-plan", attempt_order)
        metadb.save_run_state(
            run_id, running, canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        assert metadb.latest_profile_jobs(canvas_id)[0]["status"] == "running"

        assert metadb.reap_orphaned_runs(only_kernel_runs=True) == 1

        durable = metadb.get_run_state(run_id)
        recovered = metadb.latest_profile_jobs(canvas_id)
        assert durable is not None and durable["status"] == "failed"
        assert recovered[0]["run_id"] == run_id
        assert recovered[0]["status"] == "failed"
        assert "kernel is gone" in recovered[0]["error"]

        # A late zombie update from the fenced kernel cannot regress either durable view.
        with pytest.raises(
            metadb.RunStatePublicationRejected, match="permanent terminal fence race",
        ):
            metadb.save_run_state(
                run_id, running, canvas_id=canvas_id, kernel_id="profile-kernel",
            )
        assert metadb.get_run_state(run_id)["status"] == "failed"
        assert metadb.latest_profile_jobs(canvas_id)[0]["status"] == "failed"


def test_expired_unconsumed_profile_reservation_never_enters_projection(tmp_path):
    with _isolated_metadata(tmp_path / "profile-expired-reservation.db") as metadb:
        canvas_id = "profile-expired-reservation"
        run_id = "profile-never-admitted"
        _create_profile_canvas(metadb, canvas_id)
        metadb.preallocate_profile_run_owner(
            run_id, "profile-owner", canvas_id, canvas_id, "node", _digest("plan"),
        )
        with metadb.session() as db:
            state = db.get(metadb.RunState, run_id)
            state.preallocation_expires_at = (
                metadb._db_now(db) - datetime.timedelta(seconds=1)
            )
        assert metadb.latest_profile_jobs(canvas_id) == []

        assert metadb.reap_orphaned_runs(only_kernel_runs=True) == 1

        assert metadb.get_run_state(run_id)["status"] == "failed"
        assert metadb.latest_profile_jobs(canvas_id) == []


def test_profile_projection_watermark_prevents_evicted_identity_resurrection(
        tmp_path, monkeypatch):
    with _isolated_metadata(tmp_path / "profile-watermark.db") as metadb:
        monkeypatch.setattr(metadb, "_PROFILE_LATEST_MAX", 2)
        canvas_id = "profile-watermark"
        _create_profile_canvas(metadb, canvas_id)
        orders = []
        for index in range(3):
            run_id = f"profile-plan-{index}"
            attempt_order = _admit_profile(metadb, canvas_id, run_id, f"plan-{index}")
            orders.append(attempt_order)
            metadb.save_run_state(
                run_id, _profile_status(
                    run_id, "running", f"plan-{index}", attempt_order),
                canvas_id=canvas_id, kernel_id="profile-kernel",
            )

        recovered = metadb.latest_profile_jobs(canvas_id)
        assert {item["plan_digest"] for item in recovered} == {
            _digest("plan-1"), _digest("plan-2"),
        }
        # A delayed status from the evicted run sees an absent identity. The retained cutoff rejects it;
        # neither RunState detail nor the worker's memory can recreate a projection below the watermark.
        metadb.save_run_state(
            "profile-plan-0", _profile_status(
                "profile-plan-0", "running", "plan-0", orders[0]),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        assert {item["plan_digest"] for item in metadb.latest_profile_jobs(canvas_id)} == {
            _digest("plan-1"), _digest("plan-2"),
        }


def test_concurrent_same_plan_updates_keep_newer_submission_on_sqlite(tmp_path):
    with _isolated_metadata(tmp_path / "profile-concurrent.db") as metadb:
        canvas_id = "profile-concurrent"
        _create_profile_canvas(metadb, canvas_id)
        old_order = _admit_profile(
            metadb, canvas_id, "profile-concurrent-old", "same-plan")
        metadb.save_run_state(
            "profile-concurrent-old",
            _profile_status(
                "profile-concurrent-old", "running", "same-plan", old_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        new_order = _admit_profile(
            metadb, canvas_id, "profile-concurrent-new", "same-plan")
        metadb.save_run_state(
            "profile-concurrent-new",
            _profile_status(
                "profile-concurrent-new", "running", "same-plan", new_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        barrier = threading.Barrier(3)
        failures: list[BaseException] = []

        def finish(run_id: str) -> None:
            try:
                barrier.wait(timeout=2)
                metadb.save_run_state(
                    run_id, _profile_status(
                        run_id, "done", "same-plan",
                        old_order if run_id.endswith("old") else new_order),
                    canvas_id=canvas_id, kernel_id="profile-kernel",
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
        _create_profile_canvas(metadb, canvas_id)
        attempt_order = _admit_profile(
            metadb, canvas_id, "profile-delete-run", "delete-plan")
        metadb.save_run_state(
            "profile-delete-run",
            _profile_status(
                "profile-delete-run", "running", "delete-plan", attempt_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        with pytest.raises(metadb.ActiveBackendJobsError, match="active run"):
            metadb.delete_canvas_cascade(canvas_id)
        metadb.save_run_state(
            "profile-delete-run",
            _profile_status(
                "profile-delete-run", "done", "delete-plan", attempt_order),
            canvas_id=canvas_id, kernel_id="profile-kernel",
        )
        metadb.delete_canvas_cascade(canvas_id)
        assert metadb.latest_profile_jobs(canvas_id) == []
        with metadb.session() as db:
            assert db.get(metadb.ProfileJobRetention, canvas_id) is None
