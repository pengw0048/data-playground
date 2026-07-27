"""Effective canvas-role precedence across metadata, HTTP, and collaboration boundaries."""

from __future__ import annotations

from contextlib import contextmanager
import json
import threading
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from hub import auth, metadb
from hub.main import app


OWNER_ID = "effective_role_owner"
USER_ID = "effective_role_user"
AUTH_SECRET = "effective-role-test-secret-0123456789"


def _doc(canvas_id: str, name: str = "role test") -> dict:
    return {"id": canvas_id, "name": name, "version": 1, "nodes": [], "edges": []}


@contextmanager
def _canvas(canvas_id: str, visibility: str, explicit_role: str | None = None) -> Iterator[None]:
    metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as s:
        for uid, name in ((OWNER_ID, "Role Owner"), (USER_ID, "Role User")):
            if s.get(metadb.User, uid) is None:
                s.add(metadb.User(id=uid, name=name))
        s.add(metadb.Canvas(
            id=canvas_id,
            owner_id=OWNER_ID,
            name="role test",
            version=1,
            doc=json.dumps(_doc(canvas_id)),
            visibility=visibility,
        ))
    if explicit_role is not None:
        metadb.share_canvas(canvas_id, USER_ID, explicit_role)
    try:
        yield
    finally:
        metadb.delete_canvas_cascade(canvas_id)


def _listed_role(user_id: str, canvas_id: str) -> str | None:
    row = next((item for item in metadb.list_canvases_for(user_id) if item["id"] == canvas_id), None)
    return row["role"] if row is not None else None


@pytest.mark.parametrize(
    ("visibility", "explicit_role", "expected_role"),
    [
        ("private", None, None),
        ("workspace", None, "editor"),
        ("workspace_view", None, "viewer"),
        ("private", "editor", "editor"),
        ("private", "viewer", "viewer"),
        ("workspace", "editor", "editor"),
        ("workspace", "viewer", "viewer"),
        ("workspace_view", "editor", "editor"),
        ("workspace_view", "viewer", "viewer"),
    ],
)
def test_effective_role_matrix_is_identical_in_lookup_and_list(
    visibility: str, explicit_role: str | None, expected_role: str | None,
):
    canvas_id = f"role_matrix_{visibility}_{explicit_role or 'none'}"
    with _canvas(canvas_id, visibility, explicit_role):
        assert metadb.canvas_role(canvas_id, USER_ID) == expected_role
        assert _listed_role(USER_ID, canvas_id) == expected_role


@pytest.mark.parametrize("visibility", ["private", "workspace", "workspace_view"])
def test_owner_role_wins_over_visibility_and_an_explicit_share(visibility: str):
    canvas_id = f"role_owner_{visibility}"
    with _canvas(canvas_id, visibility):
        # Even a malformed/redundant collaborator row for the owner cannot lower or replace ownership.
        metadb.share_canvas(canvas_id, OWNER_ID, "viewer")
        assert metadb.canvas_role(canvas_id, OWNER_ID) == "owner"
        assert _listed_role(OWNER_ID, canvas_id) == "owner"


def test_workspace_explicit_viewer_is_read_only_in_list_put_and_collab(monkeypatch):
    """The broad workspace baseline must not silently override this user's explicit viewer grant."""
    canvas_id = "role_workspace_explicit_viewer"
    with _canvas(canvas_id, "workspace", "viewer"):
        monkeypatch.setenv("DP_AUTH_SECRET", AUTH_SECRET)
        owner_headers = {"Cookie": f"dp_session={auth.sign(OWNER_ID)}"}
        viewer_headers = {"Cookie": f"dp_session={auth.sign(USER_ID)}"}
        # Entering TestClient once gives both websocket sessions the same ASGI portal/event loop.
        # Without this context, each nested websocket_connect() can create its own portal; a relay
        # that yields for role revalidation may then never reach the peer on Linux CI.
        with TestClient(app) as client:
            listed = client.get("/api/canvas", headers=viewer_headers)
            assert listed.status_code == 200
            row = next(item for item in listed.json() if item["id"] == canvas_id)
            assert row["role"] == "viewer"

            update = client.put(
                f"/api/canvas/{canvas_id}",
                json=_doc(canvas_id, "viewer must not write"),
                headers=viewer_headers,
            )
            assert update.status_code == 403

            owner_ws_headers = {"cookie": owner_headers["Cookie"]}
            viewer_ws_headers = {"cookie": viewer_headers["Cookie"]}
            with client.websocket_connect(f"/ws/collab/{canvas_id}", headers=owner_ws_headers) as owner_ws:
                plan = owner_ws.receive_json()
                assert plan["type"] == "server" and plan["mode"] == "seed"
                owner_ws.send_json({
                    "type": "yjs", "seed": True, "requestId": plan["requestId"], "update": "seed",
                })
                owner_ws.send_json({"type": "sync-ready", "requestId": plan["requestId"]})
                assert owner_ws.receive_json() == {
                    "type": "server", "event": "room-state", "mode": "ready",
                }
                with client.websocket_connect(f"/ws/collab/{canvas_id}", headers=viewer_ws_headers) as viewer_ws:
                    assert viewer_ws.receive_json()["mode"] == "sync"
                    viewer_ws.send_json({"clientId": "viewer", "type": "yjs", "update": "AAAA"})
                    viewer_ws.send_json({"clientId": "viewer", "type": "presence", "name": "Viewer"})
                    # Presence remains visible, but the preceding Yjs write is dropped by the same
                    # effective viewer role used by list_canvas and put_canvas.
                    assert owner_ws.receive_json()["type"] == "presence"


def test_canvas_put_expected_version_prevents_stale_or_deleted_draft_overwrite(monkeypatch):
    canvas_id = "canvas_draft_expected_version"
    with _canvas(canvas_id, "private"):
        monkeypatch.setenv("DP_AUTH_SECRET", AUTH_SECRET)
        owner_headers = {"Cookie": f"dp_session={auth.sign(OWNER_ID)}"}
        with TestClient(app) as client:
            first = client.put(
                f"/api/canvas/{canvas_id}?expectedVersion=1",
                json=_doc(canvas_id, "first edit"),
                headers=owner_headers,
            )
            assert first.status_code == 200
            assert first.json() == {"ok": True, "id": canvas_id, "version": 2}
            assert client.get(f"/api/canvas/{canvas_id}", headers=owner_headers).json() == {
                **_doc(canvas_id, "first edit"), "version": 2,
            }

            stale = client.put(
                f"/api/canvas/{canvas_id}?expectedVersion=1",
                json=_doc(canvas_id, "stale offline edit"),
                headers=owner_headers,
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "conflict"
            assert client.get(f"/api/canvas/{canvas_id}", headers=owner_headers).json()["name"] == "first edit"

            metadb.delete_canvas_cascade(canvas_id)
            deleted = client.put(
                f"/api/canvas/{canvas_id}?expectedVersion=2",
                json={**_doc(canvas_id, "deleted offline edit"), "version": 2},
                headers=owner_headers,
            )
            assert deleted.status_code == 409
            assert deleted.json()["code"] == "conflict"


def test_concurrent_unconditional_canvas_puts_advance_distinct_sqlite_versions(monkeypatch):
    from hub.routers import workspace

    if not metadb._is_sqlite_database():
        pytest.skip("SQLite writer-reservation regression")
    canvas_id = "canvas_concurrent_unconditional_versions"
    first_read = threading.Event()
    second_read = threading.Event()
    first_done = threading.Event()
    results: dict[str, dict | Exception] = {}
    real_get = Session.get

    def coordinated_get(session, entity, identity, **kwargs):
        row = real_get(session, entity, identity, **kwargs)
        if entity is metadb.Canvas and identity == canvas_id and kwargs.get("with_for_update"):
            if threading.current_thread().name == "canvas-put-first":
                first_read.set()
                second_read.wait(timeout=0.25)
            elif threading.current_thread().name == "canvas-put-second":
                second_read.set()
                assert first_done.wait(timeout=5)
        return row

    monkeypatch.setattr(Session, "get", coordinated_get)
    monkeypatch.setattr(metadb, "snapshot_canvas", lambda *_args, **_kwargs: None)

    def put(label: str) -> None:
        try:
            results[label] = workspace.put_canvas(
                canvas_id,
                {**_doc(canvas_id, label), "version": 0},
                expected_version=None,
                uid=OWNER_ID,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the result assertions below
            results[label] = exc
        finally:
            if label == "first":
                first_done.set()

    with _canvas(canvas_id, "private"):
        first = threading.Thread(target=put, args=("first",), name="canvas-put-first")
        second = threading.Thread(target=put, args=("second",), name="canvas-put-second")
        first.start()
        assert first_read.wait(timeout=5)
        second.start()
        for thread in (first, second):
            thread.join(timeout=10)
            assert not thread.is_alive()

        assert sorted(
            result["version"] for result in results.values() if isinstance(result, dict)
        ) == [2, 3]
        assert all(isinstance(result, dict) for result in results.values())
        with metadb.session() as session:
            persisted = session.get(metadb.Canvas, canvas_id)
            assert persisted is not None
            assert persisted.version == json.loads(persisted.doc)["version"] == 3


def test_raw_canvas_create_and_unconditional_put_use_authoritative_identity_and_version(monkeypatch):
    seed_canvas_id = "canvas_raw_identity_seed"
    created_id = None
    with _canvas(seed_canvas_id, "private"):
        monkeypatch.setenv("DP_AUTH_SECRET", AUTH_SECRET)
        owner_headers = {"Cookie": f"dp_session={auth.sign(OWNER_ID)}"}
        with TestClient(app) as client:
            created = client.post(
                "/api/canvas",
                json={"name": "raw create", "version": 99, "nodes": [], "edges": []},
                headers=owner_headers,
            )
            assert created.status_code == 200
            created_id = created.json()["id"]
            assert created.json() == {
                "ok": True, "id": created_id, "version": 1, "created": True,
            }
            assert client.get(f"/api/canvas/{created_id}", headers=owner_headers).json() == {
                "id": created_id, "name": "raw create", "version": 1, "nodes": [], "edges": [],
            }

            updated = client.put(
                f"/api/canvas/{created_id}",
                json={
                    "id": "wrong-client-id",
                    "name": "unconditional update",
                    "version": 0,
                    "nodes": [],
                    "edges": [],
                },
                headers=owner_headers,
            )
            assert updated.status_code == 200
            assert updated.json() == {
                "ok": True, "id": created_id, "version": 2,
            }
            assert client.get(f"/api/canvas/{created_id}", headers=owner_headers).json() == {
                "id": created_id,
                "name": "unconditional update",
                "version": 2,
                "nodes": [],
                "edges": [],
            }
    if created_id is not None:
        metadb.delete_canvas_cascade(created_id)
