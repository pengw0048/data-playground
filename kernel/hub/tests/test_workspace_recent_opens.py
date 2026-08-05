"""Personal Workspace recently-opened observations (#1341)."""

from __future__ import annotations

import datetime
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from hub import metadb
from hub.main import app


@pytest.fixture
def workspace_scope():
    metadb.migrate_db()
    token = uuid.uuid4().hex
    canvas_id = f"workspace-canvas-{token}"
    uri = f"file:///workspace-{token}.parquet"
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Original canvas", version=7,
            doc=json.dumps({"id": canvas_id, "name": "Original canvas", "version": 7,
                            "nodes": [], "edges": []}),
        ))
    metadb.catalog_upsert_entry(uri, "Original dataset", {
        "id": f"tbl_{token}", "name": "Original dataset", "uri": uri, "version": "v1",
    })
    dataset_id = metadb.workspace_builtin_dataset_identity(uri)
    try:
        yield {"canvas_id": canvas_id, "uri": uri, "dataset_id": dataset_id}
    finally:
        with metadb.session() as session:
            canvas_ids = list(session.scalars(select(metadb.Canvas.id).where(
                (metadb.Canvas.id == canvas_id)
                | metadb.Canvas.name.like(f"workspace-{token}%")
            )))
            open_refs = [f"canvas:{cid}" for cid in canvas_ids]
            if open_refs:
                session.execute(delete(metadb.WorkspaceActorOpen).where(
                    metadb.WorkspaceActorOpen.resource_ref.in_(open_refs)))
            session.execute(delete(metadb.WorkspaceActorOpen).where(
                metadb.WorkspaceActorOpen.user_id == f"recent-actor-{token}"))
            current_dataset_ids = list(session.scalars(select(metadb.CatalogEntry.registration_id).where(
                metadb.CatalogEntry.uri == uri)))
            placement_ids = list(session.scalars(select(metadb.WorkspacePlacement.id).where(
                (metadb.WorkspacePlacement.target_id.in_([canvas_id, dataset_id, *current_dataset_ids, *canvas_ids]))
                | metadb.WorkspacePlacement.name.like(f"workspace-{token}%"))))
            if placement_ids:
                session.execute(delete(metadb.WorkspacePlacement).where(
                    metadb.WorkspacePlacement.id.in_(placement_ids)))
            remaining = {row.id for row in session.scalars(select(metadb.WorkspaceContainer).where(
                metadb.WorkspaceContainer.name.like(f"workspace-{token}%")))}
            while remaining:
                leaves = list(session.scalars(select(metadb.WorkspaceContainer).where(
                    metadb.WorkspaceContainer.id.in_(remaining),
                    ~metadb.WorkspaceContainer.id.in_(select(metadb.WorkspaceContainer.parent_id).where(
                        metadb.WorkspaceContainer.parent_id.is_not(None))),
                )))
                assert leaves, "test cleanup found a Workspace container cycle"
                for container in leaves:
                    session.delete(container)
                    remaining.remove(container.id)
                session.flush()
            if canvas_ids:
                session.execute(delete(metadb.Canvas).where(metadb.Canvas.id.in_(canvas_ids)))
            session.execute(delete(metadb.User).where(metadb.User.id == f"recent-actor-{token}"))
        metadb.catalog_delete_entry(uri)


def _freeze_canvas_updated(canvas_id: str, at: datetime.datetime) -> None:
    with metadb.session() as session:
        session.execute(update(metadb.Canvas).where(
            metadb.Canvas.id == canvas_id).values(updated_at=at))


def _canvas_updated_at(canvas_id: str) -> datetime.datetime:
    with metadb.session() as session:
        value = session.get(metadb.Canvas, canvas_id).updated_at
    assert value is not None
    return value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value


def test_workspace_recent_open_orders_without_touching_updated_at(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-recent-open")
    first = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-recent-a",
    )
    second = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-recent-b",
    )
    first_updated = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    second_updated = datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc)
    _freeze_canvas_updated(first["id"], first_updated)
    _freeze_canvas_updated(second["id"], second_updated)
    first_ref = f"canvas:{first['id']}"
    second_ref = f"canvas:{second['id']}"

    t0 = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone.utc)
    t1 = datetime.datetime(2026, 3, 1, 12, 5, tzinfo=datetime.timezone.utc)
    metadb.workspace_record_open(metadb.DEFAULT_USER_ID, first_ref, at=t0)
    metadb.workspace_record_open(metadb.DEFAULT_USER_ID, second_ref, at=t1)

    page = metadb.workspace_browse(
        folder["id"], uid=metadb.DEFAULT_USER_ID, limit=50,
        sort="opened", order="desc", kinds={"canvas"})
    assert [item["id"] for item in page["items"]] == [second_ref, first_ref]
    assert page["items"][0]["lastOpenedAt"].startswith("2026-03-01T12:05:00")
    assert page["items"][1]["lastOpenedAt"].startswith("2026-03-01T12:00:00")

    assert _canvas_updated_at(first["id"]) == first_updated
    assert _canvas_updated_at(second["id"]) == second_updated

    recent = metadb.workspace_recent(metadb.DEFAULT_USER_ID, limit=10)
    assert [item["id"] for item in recent["items"][:2]] == [second_ref, first_ref]


def test_workspace_open_coalesce_cap_and_actor_isolation(workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    other_id = f"recent-actor-{token}"
    with metadb.session() as session:
        session.add(metadb.User(id=other_id, name="Other"))
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-open-cap")
    canvas = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-open-cap",
    )
    ref = f"canvas:{canvas['id']}"
    t0 = datetime.datetime(2026, 4, 1, 10, 0, tzinfo=datetime.timezone.utc)
    first = metadb.workspace_record_open(metadb.DEFAULT_USER_ID, ref, at=t0)
    again = metadb.workspace_record_open(
        metadb.DEFAULT_USER_ID, ref,
        at=t0 + datetime.timedelta(seconds=30))
    assert first["coalesced"] is False
    assert again["coalesced"] is True
    assert again["lastOpenedAt"] == first["lastOpenedAt"]

    metadb.set_visibility(canvas["id"], "workspace")
    metadb.workspace_record_open(other_id, ref, at=t0 + datetime.timedelta(hours=1))
    local_recent = metadb.workspace_recent(metadb.DEFAULT_USER_ID, limit=10)
    other_recent = metadb.workspace_recent(other_id, limit=10)
    assert any(item["id"] == ref for item in local_recent["items"])
    assert any(item["id"] == ref for item in other_recent["items"])
    local_opened = next(
        item["lastOpenedAt"] for item in local_recent["items"] if item["id"] == ref)
    other_opened = next(
        item["lastOpenedAt"] for item in other_recent["items"] if item["id"] == ref)
    assert local_opened != other_opened

    monkeypatch.setattr(metadb, "_WORKSPACE_OPEN_MAX_PER_ACTOR", 3)
    for index in range(4):
        extra = metadb.workspace_create_canvas_action(
            uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
            expected_container_version=folder["version"],
            name=f"workspace-{token}-open-cap-{index}",
        )
        metadb.workspace_record_open(
            metadb.DEFAULT_USER_ID, f"canvas:{extra['id']}",
            at=t0 + datetime.timedelta(minutes=index + 2))
    with metadb.session() as session:
        count = session.scalar(select(func.count()).select_from(metadb.WorkspaceActorOpen).where(
            metadb.WorkspaceActorOpen.user_id == metadb.DEFAULT_USER_ID))
        assert count == 3
        assert session.get(metadb.WorkspaceActorOpen, (metadb.DEFAULT_USER_ID, ref)) is None


def test_workspace_open_api_requires_authorized_success(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-open-api")
    canvas = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-open-api",
    )
    ref = f"canvas:{canvas['id']}"
    frozen = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    _freeze_canvas_updated(canvas["id"], frozen)

    with TestClient(app) as client:
        denied = client.post("/api/workspace/resources/canvas:missing-open/opened")
        assert denied.status_code == 404

        folder_open = client.post(
            f"/api/workspace/resources/container:{folder['id']}/opened")
        assert folder_open.status_code == 422

        ok = client.post(f"/api/workspace/resources/{ref}/opened")
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["resourceId"] == ref
        assert body["coalesced"] is False

        browse = client.get(
            f"/api/workspace/containers/{folder['id']}",
            params=[("sort", "opened"), ("order", "desc"), ("kind", "canvas")],
        )
        assert browse.status_code == 200, browse.text
        items = browse.json()["items"]
        assert items[0]["id"] == ref
        assert items[0]["lastOpenedAt"]
        assert items[0]["updatedAt"].startswith("2026-05-01T00:00:00")

        recent = client.get("/api/workspace/recent", params={"limit": 10})
        assert recent.status_code == 200, recent.text
        assert recent.json()["items"][0]["id"] == ref
        assert recent.json()["queryCapabilities"]["sort"] == ["opened"]

    assert _canvas_updated_at(canvas["id"]) == frozen

    renamed = metadb.workspace_update_placement(
        canvas["resource"]["placementId"], expected_version=canvas["resource"]["version"],
        name=f"workspace-{token}-open-api-renamed")
    assert f"canvas:{renamed['targetId']}" == ref
    assert metadb.workspace_recent(metadb.DEFAULT_USER_ID, limit=5)["items"][0]["id"] == ref

    metadb.workspace_delete_placement(
        renamed["id"], expected_version=renamed["version"])
    metadb.delete_canvas_cascade(canvas["id"])
    sanitized = metadb.workspace_recent(metadb.DEFAULT_USER_ID, limit=10)
    assert all(item["id"] != ref for item in sanitized["items"])
    with metadb.session() as session:
        assert session.get(metadb.WorkspaceActorOpen, (metadb.DEFAULT_USER_ID, ref)) is None


def test_workspace_open_move_preserves_observation(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    source = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-open-move-src")
    dest = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-open-move-dst")
    canvas = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=source["id"],
        expected_container_version=source["version"],
        name=f"workspace-{token}-open-move",
    )
    ref = f"canvas:{canvas['id']}"
    metadb.workspace_record_open(
        metadb.DEFAULT_USER_ID, ref,
        at=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc))
    moved = metadb.workspace_update_placement(
        canvas["resource"]["placementId"], expected_version=canvas["resource"]["version"],
        container_id=dest["id"])
    assert f"canvas:{moved['targetId']}" == ref
    page = metadb.workspace_browse(
        dest["id"], uid=metadb.DEFAULT_USER_ID, limit=10,
        sort="opened", order="desc", kinds={"canvas"})
    assert page["items"][0]["id"] == ref
    assert page["items"][0]["lastOpenedAt"]


def test_workspace_recent_scans_past_a_bounded_window_of_revoked_rows(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-recent-revoked")
    canvas = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-recent-valid",
    )
    valid_ref = f"canvas:{canvas['id']}"
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    metadb.workspace_record_open(metadb.DEFAULT_USER_ID, valid_ref, at=base)
    stale_refs = [f"canvas:missing-{token}-{index}" for index in range(17)]
    try:
        with metadb.session() as session:
            session.add_all([
                metadb.WorkspaceActorOpen(
                    user_id=metadb.DEFAULT_USER_ID,
                    resource_ref=resource_ref,
                    last_opened_at=base + datetime.timedelta(minutes=index + 1),
                )
                for index, resource_ref in enumerate(stale_refs)
            ])

        recent = metadb.workspace_recent(metadb.DEFAULT_USER_ID, limit=1)

        assert [item["id"] for item in recent["items"]] == [valid_ref]
        assert recent["hasMore"] is False
        with metadb.session() as session:
            assert session.scalar(select(func.count()).select_from(
                metadb.WorkspaceActorOpen).where(
                    metadb.WorkspaceActorOpen.resource_ref.in_(stale_refs))) == 0
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspaceActorOpen).where(
                metadb.WorkspaceActorOpen.resource_ref.in_(stale_refs)))
