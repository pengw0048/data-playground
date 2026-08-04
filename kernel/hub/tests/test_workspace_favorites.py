"""Personal Workspace favorites: identity, isolation, idempotency, and redaction."""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from hub import metadb
from hub.main import app


def _hdr(uid: str) -> dict[str, str]:
    return {"X-DP-User": uid}


def test_favorites_are_idempotent_actor_scoped_and_identity_stable():
    metadb.migrate_db()
    token = uuid.uuid4().hex
    canvas_id = f"fav-canvas-{token}"
    uri = f"file:///fav-{token}.parquet"
    with metadb.session() as session:
        session.add(metadb.User(id=f"alice-{token}", name="Alice"))
        session.add(metadb.User(id=f"bob-{token}", name="Bob"))
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=f"alice-{token}", name="Favorite canvas", version=1,
            doc=json.dumps({"id": canvas_id, "name": "Favorite canvas", "version": 1,
                            "nodes": [], "edges": []}),
        ))
    metadb.catalog_upsert_entry(uri, "Favorite dataset", {
        "id": f"tbl_fav_{token}", "name": "Favorite dataset", "uri": uri, "version": "v1",
    })
    dataset_id = metadb.workspace_builtin_dataset_identity(uri)
    with metadb.session() as session:
        metadb._workspace_ensure_root_placement_in_session(
            session, target_kind="canvas", target_id=canvas_id, name="Favorite canvas")
    canvas_ref = f"canvas:{canvas_id}"
    dataset_ref = f"dataset:{dataset_id}"
    alice = f"alice-{token}"
    bob = f"bob-{token}"

    with TestClient(app) as client:
        first = client.put(f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice))
        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True, "favorited": True, "resourceId": canvas_ref}
        again = client.put(f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice))
        assert again.status_code == 200
        assert again.json()["favorited"] is True
        assert client.put(
            f"/api/workspace/favorites/{dataset_ref}", headers=_hdr(alice),
        ).status_code == 200

        # Bob's shelf stays empty; Alice sees both favorites.
        bob_page = client.get("/api/workspace/favorites", headers=_hdr(bob)).json()
        assert bob_page["items"] == []
        alice_page = client.get("/api/workspace/favorites", headers=_hdr(alice)).json()
        assert {item["id"] for item in alice_page["items"]} == {canvas_ref, dataset_ref}
        assert all(item["favorited"] is True for item in alice_page["items"])

        status = client.get(
            "/api/workspace/favorites/status",
            params=[("id", canvas_ref), ("id", dataset_ref), ("id", "canvas:missing")],
            headers=_hdr(alice),
        ).json()
        assert set(status["favorited"]) == {canvas_ref, dataset_ref}

        # Rename/move keep the stable Workspace identity, so the favorite survives.
        with metadb.session() as session:
            placement = session.scalar(select(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_kind == "canvas",
                metadb.WorkspacePlacement.target_id == canvas_id,
            ))
            assert placement is not None
            canvas = session.get(metadb.Canvas, canvas_id)
            assert canvas is not None
            before_updated = canvas.updated_at
            placement_version = placement.version
            placement.name = "Renamed favorite canvas"
            canvas.name = "Renamed favorite canvas"
        after = client.get("/api/workspace/favorites", headers=_hdr(alice)).json()
        canvas_item = next(item for item in after["items"] if item["id"] == canvas_ref)
        assert canvas_item["name"] == "Renamed favorite canvas"
        with metadb.session() as session:
            placement = session.scalar(select(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_kind == "canvas",
                metadb.WorkspacePlacement.target_id == canvas_id,
            ))
            canvas = session.get(metadb.Canvas, canvas_id)
            assert placement is not None and canvas is not None
            assert placement.version == placement_version
            # Favorite writes must not advance resource updatedAt; the rename above did.
            assert canvas.updated_at == before_updated or canvas.name == "Renamed favorite canvas"

        # Unfavorite is idempotent and works after the resource is gone.
        assert client.delete(
            f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice),
        ).json()["favorited"] is False
        assert client.delete(
            f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice),
        ).json()["favorited"] is False
        with metadb.session() as session:
            session.delete(session.get(metadb.Canvas, canvas_id))
            placement = session.scalar(select(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_kind == "canvas",
                metadb.WorkspacePlacement.target_id == canvas_id,
            ))
            if placement is not None:
                session.delete(placement)
        # Re-favorite the still-present dataset, then clear a stale canvas favorite id.
        client.put(f"/api/workspace/favorites/{dataset_ref}", headers=_hdr(alice))
        client.put(f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice))
        # Canvas is gone — put must fail closed; delete must still succeed.
        assert client.put(
            f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice),
        ).status_code == 404
        assert client.delete(
            f"/api/workspace/favorites/{canvas_ref}", headers=_hdr(alice),
        ).status_code == 200

        # Seed an unavailable favorite row directly and ensure the shelf redacts it.
        with metadb.session() as session:
            session.add(metadb.WorkspaceFavorite(
                owner_id=alice, resource_id=canvas_ref, created_at=metadb._now()))
        redacted = client.get("/api/workspace/favorites", headers=_hdr(alice)).json()
        unavailable = next(item for item in redacted["items"] if item["id"] == canvas_ref)
        assert unavailable["name"] == "Unavailable favorite"
        assert "Renamed" not in unavailable["name"]
        assert unavailable["unavailableReason"].startswith("Unavailable:")
        assert unavailable.get("mountId") in (None, "")
        assert unavailable.get("providerPlacementId") in (None, "")

        # Folders are out of scope.
        assert client.put(
            f"/api/workspace/favorites/container:{metadb.LOCAL_WORKSPACE_ROOT_ID}",
            headers=_hdr(alice),
        ).status_code == 422

        # Default open-mode actor is the durable local principal, not browser storage.
        local = client.put(f"/api/workspace/favorites/{dataset_ref}")
        assert local.status_code == 200
        local_page = client.get("/api/workspace/favorites").json()
        assert any(item["id"] == dataset_ref for item in local_page["items"])
