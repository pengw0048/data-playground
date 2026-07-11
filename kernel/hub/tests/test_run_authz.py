"""P0-AUTH-02: run-object authorization.

With auth enabled, a run's status/cancel/output must be reachable ONLY by the run's creator or by
someone with a role on the run's canvas — never by any other authenticated account. These checks are
no-ops in open mode (single trusted user), so the rest of the suite (which runs open) is unaffected.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from hub import auth, metadb
from hub.deps import get_deps
from hub.main import app

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
    """Two users (A owns a private canvas, B is a stranger) with auth turned on for the test only."""
    with metadb.session() as s:
        for uid, name in [("authz_a", "A"), ("authz_b", "B")]:
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


def _start_run(hdr: dict, canvas_id: str):
    g = {"id": canvas_id, "version": 1,
         "nodes": [{"id": "s", "type": "source", "position": {"x": 0, "y": 0},
                    "data": {"title": "s", "config": {"uri": _uri("events")}}}],
         "edges": []}
    return client.post("/api/run", json={"graph": g, "targetNodeId": "s", "confirmed": True}, headers=hdr)


def test_run_status_and_cancel_are_owner_only(authed):
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


def test_adhoc_run_is_private_to_its_creator(authed):
    # a graph id that names no saved canvas is the caller's own ad-hoc workspace — allowed, and owned
    r = _start_run(_hdr("authz_a"), "authz_adhoc_never_saved")
    assert r.status_code == 200, r.text
    rid = r.json()["runId"]
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_a")).status_code == 200
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_b")).status_code == 404


def test_adhoc_run_cannot_be_hijacked_by_claiming_its_canvas_id(authed):
    # the run's canvas_id is client-supplied and shares the canvas-id namespace; a stranger must NOT be
    # able to POST a canvas with the ad-hoc run's id to retroactively "own" (and read) the run.
    rid = _start_run(_hdr("authz_a"), "authz_adhoc_claimable").json()["runId"]
    claim = client.post("/api/canvas", json={"id": "authz_adhoc_claimable", "name": "claim"},
                        headers=_hdr("authz_b"))
    assert claim.status_code == 200  # B does own a NEW canvas by that id now...
    # ...but the run was authorized against no real canvas, so B still cannot reach it
    assert client.get(f"/api/run/{rid}", headers=_hdr("authz_b")).status_code == 404
    assert client.post(f"/api/run/{rid}/cancel", headers=_hdr("authz_b")).status_code == 404


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
