"""Graph-read and run-action authorization.

With auth enabled, caller-supplied graph analysis requires a real readable saved canvas; status/output
are readable by every collaborator, while submit/cancel require an owner/editor role. Unrelated users
cannot enumerate either canvas or run objects. Open mode remains a single trusted user.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub import auth, metadb
from hub.deps import get_deps
from hub.main import app
from hub.mcp import build_http_server

client = TestClient(app)

SECRET = "unit-test-secret-not-in-weak-list-0123456789"


def _uri(name: str) -> str:
    return get_deps().catalog.get_table(f"tbl_{name}").uri


def _hdr(uid: str) -> dict:
    # a Cookie HEADER (not the client cookie jar) so requests stay stateless and independent — the jar's
    # per-request persistence is ambiguous and leaks a session across tests
    return {"Cookie": f"dp_session={auth.sign(uid)}"}


@pytest.fixture
def authed(monkeypatch):
    """An owner, editor, viewer, and stranger with auth turned on for the test only."""
    with metadb.session() as s:
        for uid, name in [("authz_a", "Owner"), ("authz_editor", "Editor"),
                          ("authz_viewer", "Viewer"), ("authz_b", "Stranger")]:
            if s.get(metadb.User, uid) is None:
                s.add(metadb.User(id=uid, name=name))
    # reset the canvas to owned-by-A each time so tests are order-independent (the module shares one DB,
    # and some tests delete / re-claim this id)
    metadb.delete_canvas_cascade("authz_canvas")
    with metadb.session() as s:
        s.add(metadb.Canvas(id="authz_canvas", owner_id="authz_a", name="c"))
    monkeypatch.setenv("DP_AUTH_SECRET", SECRET)
    client.cookies.clear()
    yield
    client.cookies.clear()


def _graph(canvas_id: str) -> dict:
    return {"id": canvas_id, "version": 1,
            "nodes": [{"id": "s", "type": "source", "position": {"x": 0, "y": 0},
                       "data": {"title": "s", "config": {"uri": _uri("events")}}}],
            "edges": []}


def _analysis_graph(canvas_id: str) -> dict:
    """A real two-source graph exercises rows, schema, estimates, plans, and join hints."""
    uri = _uri("events")
    return {"id": canvas_id, "name": "authz analysis", "version": 1, "nodes": [
        {"id": "left", "type": "source", "position": {"x": 0, "y": 0},
         "data": {"title": "left", "config": {"uri": uri}}},
        {"id": "right", "type": "source", "position": {"x": 0, "y": 100},
         "data": {"title": "right", "config": {"uri": uri}}},
        {"id": "join", "type": "join", "position": {"x": 200, "y": 0},
         "data": {"title": "join", "config": {"on": "id", "how": "inner"}}},
    ], "edges": [
        {"id": "left-join", "source": "left", "target": "join", "targetHandle": "a",
         "data": {"wire": "dataset"}},
        {"id": "right-join", "source": "right", "target": "join", "targetHandle": "b",
         "data": {"wire": "dataset"}},
    ]}


_GRAPH_READ_ENDPOINTS = [
    ("compile", "/api/graph/compile"),
    ("preview", "/api/run/preview"),
    ("profile", "/api/run/profile"),
    ("schema", "/api/graph/schema"),
    ("graph-estimate", "/api/graph/estimate"),
    ("plan", "/api/graph/plan"),
    ("join-analysis", "/api/graph/join-analysis"),
    ("run-estimate", "/api/run/estimate"),
    ("profile-estimate", "/api/run/profile-estimate"),
    ("profile-identity", "/api/run/profile-identity"),
]


def _graph_read_body(case: str, canvas_id: str) -> dict:
    graph = _analysis_graph(canvas_id)
    if case == "preview":
        return {"graph": graph, "nodeId": "left", "k": 2}
    if case == "profile":
        return {"graph": graph, "nodeId": "left"}
    if case in ("profile-estimate", "profile-identity"):
        return {"graph": graph, "nodeId": "left"}
    return {"graph": graph, "targetNodeId": "join"}


def _start_run(hdr: dict, canvas_id: str):
    return client.post("/api/run", json={"graph": _graph(canvas_id), "targetNodeId": "s", "confirmed": True},
                       headers=hdr)


def _share_editor_and_viewer() -> None:
    metadb.share_canvas("authz_canvas", "authz_editor", "editor")
    metadb.share_canvas("authz_canvas", "authz_viewer", "viewer")


def _bind_missing_backend_run(run_id: str) -> None:
    metadb.preallocate_run_owner(run_id, "authz_a", "authz_canvas")
    metadb.bind_backend_job(run_id, {
        "backend": "missing-durable-authz-test",
        "cluster_ref": "test-cluster",
        "attempt_id": f"attempt-{run_id}",
        "submission_id": f"submission-{run_id}",
        "job_uri": f"s3://test-control/{run_id}.dpjob",
        "result_uri": f"s3://test-control/{run_id}.dpresult",
        "control_address": "http://missing-control:8265",
    }, {
        "run_id": run_id, "status": "queued", "placement": "distributed", "per_node": [],
    }, canvas_id="authz_canvas")


def _delete_backend_test_run(run_id: str) -> None:
    with metadb.session() as session:
        job = session.get(metadb.RunBackendJob, run_id)
        state = session.get(metadb.RunState, run_id)
        if job is not None:
            session.delete(job)
        if state is not None:
            session.delete(state)


def _wait_for_terminal(run_id: str) -> dict:
    for _ in range(200):
        response = client.get(f"/api/run/{run_id}", headers=_hdr("authz_a"))
        assert response.status_code == 200, response.text
        status = response.json()
        if status["status"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.05)
    pytest.fail(f"run '{run_id}' did not finish")


def _mcp_tool(uid: str, name: str, arguments: dict) -> dict:
    server = build_http_server(uid)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return response["result"]


def test_private_run_is_hidden_from_a_stranger(authed):
    r = _start_run(_hdr("authz_a"), "authz_canvas")
    assert r.status_code == 200, r.text
    rid = r.json()["runId"]
    # the owner can read + cancel its own run
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_a")).status_code == 200
    # a stranger gets 404 (not 403 — don't confirm the id exists) for both status and cancel
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_b")).status_code == 404
    assert client.post(f"/api/run/{rid}/cancel", headers=_hdr("authz_b")).status_code == 404


def test_run_creation_is_bound_to_an_authorized_canvas(authed):
    # B cannot launch a run against A's private canvas (would attribute the run/history to A's canvas)
    r = _start_run(_hdr("authz_b"), "authz_canvas")
    assert r.status_code == 404, r.text


def test_auth_mode_rejects_invented_graph_for_estimate_and_submit(authed, monkeypatch):
    """POST /run cannot bypass the read-route guard with the same invented graph id."""
    invented = "authz_adhoc_never_saved"
    graph = _graph(invented)
    touched: list[str] = []
    monkeypatch.setattr(get_deps().catalog, "resolve_ref",
                        lambda ref: touched.append(str(ref)) or ref)
    estimate = client.post("/api/run/estimate", json={
        "graph": graph, "targetNodeId": "s",
    }, headers=_hdr("authz_a"))
    submit = client.post("/api/run", json={
        "graph": graph, "targetNodeId": "s", "confirmed": True,
    }, headers=_hdr("authz_a"))
    assert estimate.status_code == submit.status_code == 404
    assert touched == []


@pytest.mark.parametrize("canvas_id", ["authz_canvas", "authz_invented"], ids=["private", "invented"])
@pytest.mark.parametrize(("case", "path"), _GRAPH_READ_ENDPOINTS, ids=[c[0] for c in _GRAPH_READ_ENDPOINTS])
def test_graph_reads_reject_before_source_resolution(authed, monkeypatch, canvas_id, case, path):
    """A stranger/private id and an invented id fail before catalog or adapter data access."""
    body = _graph_read_body(case, canvas_id)  # build while the real catalog methods are still installed
    touched: list[str] = []

    def touched_resolver(ref):
        touched.append(f"resolve:{ref}")
        raise AssertionError("source ref resolved before graph authorization")

    def touched_adapter(uri):
        touched.append(f"adapter:{uri}")
        raise AssertionError("data adapter resolved before graph authorization")

    deps = get_deps()
    monkeypatch.setattr(deps.catalog, "resolve_ref", touched_resolver)
    monkeypatch.setattr(deps, "resolve_adapter", touched_adapter)
    response = client.post(path, json=body, headers=_hdr("authz_b"))
    assert response.status_code == 404, response.text
    assert touched == []


@pytest.mark.parametrize("uid", ["authz_a", "authz_editor", "authz_viewer"],
                         ids=["owner", "editor", "viewer"])
@pytest.mark.parametrize(("case", "path"), _GRAPH_READ_ENDPOINTS, ids=[c[0] for c in _GRAPH_READ_ENDPOINTS])
def test_graph_reads_allow_every_canvas_read_role(authed, monkeypatch, uid, case, path):
    _share_editor_and_viewer()
    monkeypatch.setattr(get_deps(), "chosen_backend", lambda _uid=None: "local-out-of-core")
    response = client.post(path, json=_graph_read_body(case, "authz_canvas"), headers=_hdr(uid))
    assert response.status_code == 200, response.text
    payload = response.json()
    if case == "compile":
        assert {s["nodeId"] for s in payload["steps"]} == {"left", "right", "join"}
    elif case == "preview":
        assert payload["rows"]
    elif case == "profile":
        assert payload["columns"]
    elif case == "schema":
        assert payload["left"] and payload["join"]
    elif case == "graph-estimate":
        assert "left" in payload and "join" in payload
    elif case == "plan":
        assert payload["regions"]
    elif case == "join-analysis":
        assert "suggestions" in payload
    elif case == "run-estimate":
        assert payload["placement"] == "local"
    elif case == "profile-estimate":
        assert len(payload["planDigest"]) == 64
    elif case == "profile-identity":
        assert len(payload["planDigest"]) == 64


def test_agent_graph_is_authorized_before_provider_or_data_access(authed, monkeypatch):
    """The optional graph-aware agent is another caller-supplied graph execution surface."""
    import hub.routers.runs as runs

    monkeypatch.setattr(runs, "agent_status", lambda: (_ for _ in ()).throw(
        AssertionError("agent provider checked before graph authorization")))
    response = client.post("/api/agent", json={
        "outcome": "preview this", "graph": _analysis_graph("authz_invented"),
    }, headers=_hdr("authz_b"))
    assert response.status_code == 404


def test_deleting_a_canvas_severs_its_runs(authed):
    # a run bound to a REAL canvas must not survive the canvas's deletion into a reusable id namespace:
    # if B later re-creates a canvas with the freed id, B must NOT inherit A's old runs.
    rid = _start_run(_hdr("authz_a"), "authz_canvas").json()["runId"]
    # wait for the run to reach a terminal state BEFORE deleting — else the still-running run's async
    # status-persist can re-create the run_states row after the cascade delete (a mid-run delete is a
    # separate lifecycle concern, ARCH-10). Deterministic: no race between delete and persist.
    for _ in range(200):
        if client.get(f"/api/run/{rid}", headers=_hdr("authz_a")).json()["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_a")).status_code == 200
    metadb.delete_canvas_cascade("authz_canvas")
    claim = client.post("/api/canvas", json={"id": "authz_canvas", "name": "reclaim"},
                        headers=_hdr("authz_b"))
    assert claim.status_code == 200  # B owns a NEW canvas by that id
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_b")).status_code == 404  # but not A's old run


def test_shared_editor_can_read_the_runs_of_a_shared_canvas(authed):
    # authorization is by role on the canvas, not just creator: share the canvas with B as editor
    metadb.share_canvas("authz_canvas", "authz_b", "editor")
    try:
        rid = _start_run(_hdr("authz_a"), "authz_canvas").json()["runId"]
        assert client.get(f"/api/run/{rid}", headers=_hdr("authz_b")).status_code == 200
    finally:
        metadb.unshare_canvas("authz_canvas", "authz_b")


@pytest.mark.parametrize(("uid", "expected"), [
    ("authz_a", 200),
    ("authz_editor", 200),
    ("authz_viewer", 403),
    ("authz_b", 404),
])
def test_run_submit_requires_canvas_write_role(authed, uid, expected):
    """Submitting a canvas run is a mutation: owner/editor may; viewer/stranger may not."""
    _share_editor_and_viewer()
    response = _start_run(_hdr(uid), "authz_canvas")
    assert response.status_code == expected, response.text


def test_profile_job_submit_and_recovery_use_read_vs_mutate_roles(authed, monkeypatch):
    from hub.models import RunEstimate, RunStatus
    from hub.routers import runs as run_routes

    _share_editor_and_viewer()
    deps = get_deps()
    graph = _graph("authz_canvas")
    digest = "a" * 64
    dispatched: list[str] = []

    class Owner:
        def profile_job(self, _graph_doc, node_id, port_id, plan_digest, *, run_id, admission_token,
                        request_id=None):
            won, queued = metadb.consume_profile_run_preallocation(
                    run_id, admission_token, canvas_id="authz_canvas",
                    kernel_id="authz-profile-kernel", target_node_id=node_id,
                    target_port_id=port_id,
                    plan_digest=plan_digest,
            )
            assert won
            dispatched.append(run_id)
            return RunStatus(**queued)

    monkeypatch.setattr(deps, "kernel_backend", lambda: Owner())
    monkeypatch.setattr(run_routes, "_profile_job_estimate", lambda *_args: RunEstimate(
        rows=10, bytes=100, placement="local", needs_confirm=False,
    ))
    monkeypatch.setattr(run_routes, "_profile_plan_digest", lambda *_args: digest)

    def submit(uid: str, submission_id: str):
        return client.post("/api/run/profile-job", json={
            "graph": graph, "nodeId": "s", "planDigest": digest,
            "submissionId": submission_id,
        }, headers=_hdr(uid))

    run_id: str | None = None
    try:
        viewer = submit("authz_viewer", "00000000-0000-4000-8000-000000000031")
        stranger = submit("authz_b", "00000000-0000-4000-8000-000000000032")
        editor = submit("authz_editor", "00000000-0000-4000-8000-000000000033")

        assert viewer.status_code == 403
        assert stranger.status_code == 404
        assert editor.status_code == 200, editor.text
        run_id = editor.json()["runId"]
        assert dispatched == [run_id]
        latest = client.get("/api/canvas/authz_canvas/profile-jobs", headers=_hdr("authz_viewer"))
        hidden = client.get("/api/canvas/authz_canvas/profile-jobs", headers=_hdr("authz_b"))
        assert latest.status_code == 200
        assert any(item["runId"] == run_id for item in latest.json())
        assert hidden.status_code == 404
    finally:
        if run_id is not None:
            status = metadb.get_run_state(run_id)
            if status is not None:
                status.update({"status": "cancelled", "progress": None})
                metadb.save_run_state(
                    run_id, status, canvas_id="authz_canvas", kernel_id="authz-profile-kernel",
                )
            deps.run_index.pop(run_id, None)
            deps.run_owner.pop(run_id, None)


def test_viewer_profile_identity_rejects_third_party_adapter_before_fingerprint(
        authed, monkeypatch):
    _share_editor_and_viewer()
    fingerprint_calls: list[str] = []

    class ThirdPartyAdapter:
        name = "third-party-identity"

        def fingerprint(self, uri: str) -> str:
            fingerprint_calls.append(uri)
            raise AssertionError("shared-mode recovery must not execute third-party fingerprint code")

    monkeypatch.setattr(get_deps(), "resolve_adapter", lambda _uri: ThirdPartyAdapter())
    graph = {
        "id": "authz_canvas", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": "third-party://recovery-source"}},
        }],
        "edges": [],
    }

    response = client.post(
        "/api/run/profile-identity", json={"graph": graph, "nodeId": "source"},
        headers=_hdr("authz_viewer"),
    )

    assert response.status_code == 403
    assert "third-party dataset adapter" in response.json()["detail"]
    assert fingerprint_calls == []


def test_run_read_and_cancel_use_different_role_policies(authed):
    """All collaborators may inspect a run, but only owner/editor may cancel it."""
    _share_editor_and_viewer()
    response = _start_run(_hdr("authz_a"), "authz_canvas")
    assert response.status_code == 200, response.text
    run_id = response.json()["runId"]

    for uid, expected in [("authz_a", 200), ("authz_editor", 200),
                          ("authz_viewer", 200), ("authz_b", 404)]:
        assert client.get(f"/api/run/{run_id}", headers=_hdr(uid)).status_code == expected

    # A viewer already knows the shared run exists, so 403 is honest; a stranger still gets a
    # non-enumerating 404. Editors and owners retain their existing cancellation behavior.
    assert client.post(f"/api/run/{run_id}/cancel", headers=_hdr("authz_viewer")).status_code == 403
    assert client.post(f"/api/run/{run_id}/cancel", headers=_hdr("authz_b")).status_code == 404
    assert client.post(f"/api/run/{run_id}/cancel", headers=_hdr("authz_editor")).status_code == 200
    assert client.post(f"/api/run/{run_id}/cancel", headers=_hdr("authz_a")).status_code == 200


@pytest.mark.parametrize(("uid", "expected"), [
    ("authz_a", 200),
    ("authz_editor", 200),
    ("authz_viewer", 403),
    ("authz_b", 404),
])
def test_missing_durable_plugin_preserves_authorized_cancel_intent(authed, uid, expected):
    """Missing plugin recovery must retain cancellation without weakening canvas authorization."""
    _share_editor_and_viewer()
    run_id = f"missing_durable_cancel_{uid}"
    _bind_missing_backend_run(run_id)
    try:
        response = client.post(f"/api/run/{run_id}/cancel", headers=_hdr(uid))
        assert response.status_code == expected, response.text
        binding = metadb.backend_job(run_id)
        assert binding is not None
        assert binding["cancel_requested"] is (expected == 200)
        if expected == 200:
            assert response.json()["status"] == "queued", "remote stop has not been acknowledged"
            assert metadb.get_run_state(run_id)["status"] == "queued"
    finally:
        _delete_backend_test_run(run_id)


def test_status_projects_terminal_fence_after_bounded_detail_is_pruned():
    from hub.routers import runs as run_routes

    run_id = "pruned_terminal_status_authz"
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "per_node": [], "progress": 1.0,
    })
    try:
        with metadb.session() as session:
            session.delete(session.get(metadb.RunState, run_id))
        status = run_routes._status_or_lost(run_id)
        assert (status.status, status.progress, status.error) == ("done", 1.0, None)
    finally:
        with metadb.session() as session:
            fence = session.get(metadb.RunTerminalFence, run_id)
            if fence is not None:
                session.delete(fence)


def test_fast_terminal_before_owner_bind_backfills_retained_identity(authed):
    """A tiny local run may finish before start_run persists its creator after runner.run returns."""
    from hub.routers import runs as run_routes

    _share_editor_and_viewer()
    run_id = "fast_terminal_before_owner_bind_authz"
    with metadb.session() as session:
        session.add(metadb.RunState(
            run_id=run_id, canvas_id="authz_canvas", status="running",
            doc='{"run_id":"fast_terminal_before_owner_bind_authz","status":"running"}',
        ))
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "per_node": [], "progress": 1.0,
    }, canvas_id="authz_canvas")
    assert metadb.terminal_run_identity(run_id) == (None, None, "authz_canvas")

    try:
        metadb.bind_run_owner(run_id, "authz_editor", "authz_canvas")
        metadb.bind_run_owner(run_id, "authz_editor", "authz_canvas")
        assert metadb.terminal_run_identity(run_id) == (
            "authz_editor", "authz_canvas", "authz_canvas",
        )
        with metadb.session() as session:
            session.delete(session.get(metadb.RunState, run_id))
        get_deps().run_owner.pop(run_id, None)
        metadb.unshare_canvas("authz_canvas", "authz_editor")

        # Read access follows the immutable creator after detail pruning. Mutate access still follows
        # the creator's current role on the real canvas, so revoking the editor share remains effective.
        assert run_routes._run_read_access(run_id, "authz_editor") is True
        assert run_routes._run_mutate_access(run_id, "authz_editor") is False
        response = client.get(f"/api/run/{run_id}", headers=_hdr("authz_editor"))
        assert response.status_code == 200 and response.json()["status"] == "done"
    finally:
        get_deps().run_owner.pop(run_id, None)
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            if state is not None:
                session.delete(state)
            if fence is not None:
                session.delete(fence)


def test_pruned_terminal_fence_preserves_run_authorization(authed):
    """Bounded status retention must not revoke the creator or current canvas collaborators."""
    _share_editor_and_viewer()
    run_id = "pruned_terminal_identity_authz"
    metadb.bind_run_owner(run_id, "authz_a", "authz_canvas")
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "per_node": [], "progress": 1.0,
    }, canvas_id="authz_canvas")
    try:
        with metadb.session() as session:
            session.delete(session.get(metadb.RunState, run_id))
        get_deps().run_owner.pop(run_id, None)

        assert metadb.terminal_run_identity(run_id) == (
            "authz_a", "authz_canvas", "authz_canvas",
        )
        for uid, expected in [
            ("authz_a", 200), ("authz_editor", 200),
            ("authz_viewer", 200), ("authz_b", 404),
        ]:
            response = client.get(f"/api/run/{run_id}", headers=_hdr(uid))
            assert response.status_code == expected, response.text
            if expected == 200:
                assert response.json()["status"] == "done"
        for uid, expected in [
            ("authz_a", 200), ("authz_editor", 200),
            ("authz_viewer", 403), ("authz_b", 404),
        ]:
            response = client.post(f"/api/run/{run_id}/cancel", headers=_hdr(uid))
            assert response.status_code == expected, response.text

        # Deleting the authorization canvas preserves only the opaque resurrection fence. Reusing its
        # ID must not transfer retained-run access to the replacement owner, and stale process-local
        # ownership must not override the identity-cleared durable fence.
        get_deps().run_owner[run_id] = "authz_a"
        metadb.delete_canvas_cascade("authz_canvas")
        claim = client.post("/api/canvas", json={"id": "authz_canvas", "name": "replacement"},
                            headers=_hdr("authz_b"))
        assert claim.status_code == 200, claim.text
        assert metadb.terminal_run_status(run_id) == "done"
        assert metadb.terminal_run_identity(run_id) == (None, None, None)
        assert client.get(f"/api/run/{run_id}", headers=_hdr("authz_a")).status_code == 404
        assert client.get(f"/api/run/{run_id}", headers=_hdr("authz_b")).status_code == 404
    finally:
        get_deps().run_owner.pop(run_id, None)
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            if state is not None:
                session.delete(state)
            if fence is not None:
                session.delete(fence)


def test_pruned_legacy_terminal_fence_preserves_canvas_roles(authed):
    """Pre-owner-metadata runs retain their existing best-effort canvas authorization."""
    _share_editor_and_viewer()
    run_id = "pruned_legacy_terminal_identity_authz"
    with metadb.session() as session:
        session.add(metadb.RunState(
            run_id=run_id, canvas_id="authz_canvas", status="running",
            doc='{"run_id":"pruned_legacy_terminal_identity_authz","status":"running"}',
        ))
    metadb.save_run_state(run_id, {
        "run_id": run_id, "status": "done", "per_node": [], "progress": 1.0,
    }, canvas_id="authz_canvas")
    try:
        with metadb.session() as session:
            session.delete(session.get(metadb.RunState, run_id))
        get_deps().run_owner.pop(run_id, None)
        assert metadb.terminal_run_identity(run_id) == (None, None, "authz_canvas")

        for uid, expected in [
            ("authz_a", 200), ("authz_editor", 200),
            ("authz_viewer", 200), ("authz_b", 404),
        ]:
            assert client.get(f"/api/run/{run_id}", headers=_hdr(uid)).status_code == expected
        for uid, expected in [
            ("authz_a", 200), ("authz_editor", 200),
            ("authz_viewer", 403), ("authz_b", 404),
        ]:
            assert client.post(f"/api/run/{run_id}/cancel", headers=_hdr(uid)).status_code == expected
    finally:
        with metadb.session() as session:
            state = session.get(metadb.RunState, run_id)
            fence = session.get(metadb.RunTerminalFence, run_id)
            if state is not None:
                session.delete(state)
            if fence is not None:
                session.delete(fence)


def test_missing_plugin_cancel_projects_terminal_when_publication_prunes_detail(monkeypatch):
    from hub.routers import runs as run_routes

    run_id = "pruned_terminal_cancel_authz"
    live = {"run_id": run_id, "status": "running", "per_node": []}
    observations = iter((live, None))
    monkeypatch.setattr(run_routes, "_require_run_mutate_access", lambda *_args: None)
    monkeypatch.setattr(run_routes, "_runner_for", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(metadb, "backend_job", lambda _run_id: {"backend": "missing"})
    monkeypatch.setattr(metadb, "get_run_state", lambda _run_id: next(observations))
    monkeypatch.setattr(metadb, "request_backend_cancel", lambda _run_id: False)
    monkeypatch.setattr(metadb, "terminal_run_status", lambda _run_id: "done")

    status = run_routes.run_cancel(run_id, uid="authz_a")

    assert (status.status, status.progress, status.error) == ("done", 1.0, None)


def test_cancel_projects_terminal_when_publication_prunes_binding_before_lookup(monkeypatch):
    from hub.routers import runs as run_routes

    run_id = "pruned_terminal_cancel_before_binding_authz"
    monkeypatch.setattr(run_routes, "_require_run_mutate_access", lambda *_args: None)
    monkeypatch.setattr(run_routes, "_runner_for", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(metadb, "backend_job", lambda _run_id: None)
    monkeypatch.setattr(metadb, "get_run_state", lambda _run_id: None)
    monkeypatch.setattr(metadb, "terminal_run_status", lambda _run_id: "done")

    status = run_routes.run_cancel(run_id, uid="authz_a")

    assert (status.status, status.progress, status.error) == ("done", 1.0, None)


def test_viewer_can_read_completed_output_history_mcp_and_websocket(authed):
    """Read-only collaborators keep every run observation surface, including MCP and the status WS."""
    _share_editor_and_viewer()
    response = _start_run(_hdr("authz_a"), "authz_canvas")
    assert response.status_code == 200, response.text
    run_id = response.json()["runId"]
    owner_status = _wait_for_terminal(run_id)
    assert owner_status["status"] == "done"
    assert len(owner_status["outputs"]) == 1
    owner_output = owner_status["outputs"][0]
    assert owner_output["outcome"] == "committed" and owner_output["uri"]

    viewer_status = client.get(f"/api/run/{run_id}", headers=_hdr("authz_viewer"))
    assert viewer_status.status_code == 200
    assert viewer_status.json()["outputs"] == owner_status["outputs"]
    history = client.get("/api/canvas/authz_canvas/runs", headers=_hdr("authz_viewer"))
    assert history.status_code == 200
    assert any(row["status"] == "done" and row["targetNodeId"] == "s" for row in history.json())

    export_params = {"nodeId": owner_output["nodeId"], "portId": owner_output["portId"]}
    viewer_export = client.get(
        f"/api/run/{run_id}/export", params=export_params, headers=_hdr("authz_viewer"),
    )
    assert viewer_export.status_code == 200
    assert viewer_export.headers["x-data-scope"] == "full-result"
    assert client.head(
        f"/api/run/{run_id}/export", params=export_params, headers=_hdr("authz_viewer"),
    ).status_code == 200
    stranger_export = client.get(
        f"/api/run/{run_id}/export",
        params={**export_params, "userId": "authz_viewer"}, headers=_hdr("authz_b"),
    )
    assert stranger_export.status_code == 404
    assert client.head(
        f"/api/run/{run_id}/export",
        params={**export_params, "userId": "authz_viewer"}, headers=_hdr("authz_b"),
    ).status_code == 404
    sample_body = {**export_params, "k": 2, "offset": 0}
    viewer_sample = client.post(
        f"/api/run/{run_id}/sample", json=sample_body, headers=_hdr("authz_viewer"),
    )
    assert viewer_sample.status_code == 200
    assert viewer_sample.json()["rows"]
    stranger_sample = client.post(
        f"/api/run/{run_id}/sample", json=sample_body, headers=_hdr("authz_b"),
    )
    assert stranger_sample.status_code == 404

    mcp_status = _mcp_tool("authz_viewer", "run_status", {"runId": run_id})
    assert mcp_status.get("isError") is not True
    assert mcp_status["structuredContent"]["outputs"] == owner_status["outputs"]
    mcp_sample = _mcp_tool("authz_viewer", "sample_result", {"runId": run_id, "limit": 1})
    assert mcp_sample.get("isError") is not True and len(mcp_sample["structuredContent"]["rows"]) == 1

    with client.websocket_connect(f"/ws/run/{run_id}", headers={"cookie": _hdr("authz_viewer")["Cookie"]}) as ws:
        assert ws.receive_json()["runId"] == run_id
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/run/{run_id}", headers={"cookie": _hdr("authz_b")["Cookie"]}):
            pass
    assert exc.value.code == 1008


def test_mcp_reuses_submit_and_cancel_mutation_policy(authed):
    _share_editor_and_viewer()
    saved = client.put("/api/canvas/authz_canvas", json=_graph("authz_canvas"), headers=_hdr("authz_a"))
    assert saved.status_code == 200, saved.text
    viewer_submit = _mcp_tool("authz_viewer", "run_canvas",
                              {"canvasId": "authz_canvas", "nodeId": "s", "confirm": True})
    assert viewer_submit["isError"] is True and "owner or editor" in viewer_submit["content"][0]["text"]

    run_id = _start_run(_hdr("authz_a"), "authz_canvas").json()["runId"]
    viewer_cancel = _mcp_tool("authz_viewer", "cancel_run", {"runId": run_id})
    assert viewer_cancel["isError"] is True and "owner or editor" in viewer_cancel["content"][0]["text"]
    editor_cancel = _mcp_tool("authz_editor", "cancel_run", {"runId": run_id})
    assert editor_cancel.get("isError") is not True


def test_mcp_graph_reads_use_the_authorized_saved_document(authed, monkeypatch):
    """MCP accepts a canvas id, not a caller-supplied graph; `_get_doc` enforces the same read roles."""
    _share_editor_and_viewer()
    monkeypatch.setattr(get_deps(), "chosen_backend", lambda _uid=None: "local-out-of-core")
    saved = client.put("/api/canvas/authz_canvas", json=_analysis_graph("authz_canvas"),
                       headers=_hdr("authz_a"))
    assert saved.status_code == 200, saved.text

    viewer_preview = _mcp_tool("authz_viewer", "preview_node",
                               {"canvasId": "authz_canvas", "nodeId": "left", "limit": 2})
    viewer_validate = _mcp_tool("authz_viewer", "validate_canvas", {"canvasId": "authz_canvas"})
    assert viewer_preview.get("isError") is not True
    assert viewer_preview["structuredContent"]["rows"]
    assert viewer_validate.get("isError") is not True

    for canvas_id in ("authz_canvas", "authz_invented"):
        denied = _mcp_tool("authz_b", "preview_node",
                           {"canvasId": canvas_id, "nodeId": "left", "limit": 1})
        assert denied["isError"] is True and "not found" in denied["content"][0]["text"]
