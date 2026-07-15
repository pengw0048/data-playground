"""Effective canvas-role precedence across metadata, HTTP, and collaboration boundaries."""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

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
                assert owner_ws.receive_json() == {"type": "room-state", "peerCount": 0}
                with client.websocket_connect(f"/ws/collab/{canvas_id}", headers=viewer_ws_headers) as viewer_ws:
                    assert viewer_ws.receive_json() == {"type": "room-state", "peerCount": 1}
                    viewer_ws.send_json({"clientId": "viewer", "type": "yjs", "update": "AAAA"})
                    viewer_ws.send_json({"clientId": "viewer", "type": "presence", "name": "Viewer"})
                    # Presence remains visible, but the preceding Yjs write is dropped by the same
                    # effective viewer role used by list_canvas and put_canvas.
                    assert owner_ws.receive_json()["type"] == "presence"
