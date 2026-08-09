"""Local Workspace storage invariants, independent of browse and UI delivery."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import cast

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, update

from hub import db, main as hub_main, metadb, workspace_providers
from hub.catalog_provider import (
    CatalogDatasetDetail,
    CatalogMount,
    CatalogResource,
    ProviderAncestors,
    ProviderCapabilities,
    ProviderDatasetDetailResult,
    ProviderPage,
    ProviderResourceResult,
    ProviderSearchPage,
)
from hub.main import app
from hub.deps import get_deps
from hub.executors.preview import preview_node
from hub.executors.profile import profile_node
from hub.plugins.adapters import DuckDBAdapter, RevisionProviderOffline, RevisionUnavailable


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
            current_dataset_ids = list(session.scalars(select(metadb.CatalogEntry.registration_id).where(
                metadb.CatalogEntry.uri == uri)))
            placement_ids = list(session.scalars(select(metadb.WorkspacePlacement.id).where(
                (metadb.WorkspacePlacement.target_id.in_([canvas_id, dataset_id, *current_dataset_ids]))
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
            session.execute(delete(metadb.Canvas).where(metadb.Canvas.id == canvas_id))
        metadb.catalog_delete_entry(uri)


def test_root_and_container_paths_are_stable_and_local(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    assert root == {
        "id": metadb.LOCAL_WORKSPACE_ROOT_ID, "parentId": None, "name": "Workspace",
        "ordinal": 0, "version": 1, "isRoot": True,
    }

    left = metadb.workspace_create_container(root["id"], f"workspace-{token}-left")
    right = metadb.workspace_create_container(root["id"], f"workspace-{token}-right")
    left_child = metadb.workspace_create_container(left["id"], f"workspace-{token}-same")
    right_child = metadb.workspace_create_container(right["id"], f"workspace-{token}-same")

    assert left_child["id"] != right_child["id"]
    moved = metadb.workspace_update_container(
        left["id"], expected_version=left["version"], name=f"workspace-{token}-renamed",
        parent_id=right["id"], ordinal=3,
    )
    assert moved["id"] == left["id"]
    assert moved["version"] == left["version"] + 1
    assert moved["parentId"] == right["id"]

    with pytest.raises(metadb.WorkspaceVersionConflict, match="version"):
        metadb.workspace_update_container(left["id"], expected_version=left["version"], ordinal=4)
    with pytest.raises(ValueError, match="own descendant"):
        metadb.workspace_update_container(
            right["id"], expected_version=right["version"], parent_id=left["id"])


def test_workspace_folder_actions_are_capability_gated_cas_safe_and_replayable(workspace_scope):
    root = metadb.local_workspace_root()
    request_id = str(uuid.uuid4())
    with TestClient(app) as client:
        created = client.post("/api/workspace/folders", json={
            "parentId": root["id"], "expectedParentVersion": root["version"],
            "name": "Research", "requestId": request_id,
        })
        assert created.status_code == 200, created.text
        folder = created.json()["resource"]
        assert folder["id"].startswith("container:")
        assert folder["canCreateFolder"] is True
        assert folder["canRenameFolder"] is True
        assert folder["canDeleteFolder"] is True

        replay = client.post("/api/workspace/folders", json={
            "parentId": root["id"], "expectedParentVersion": root["version"],
            "name": "Research", "requestId": request_id,
        })
        assert replay.status_code == 200, replay.text
        assert replay.json()["resource"]["id"] == folder["id"]
        conflict = client.post("/api/workspace/folders", json={
            "parentId": root["id"], "expectedParentVersion": root["version"],
            "name": "Different", "requestId": request_id,
        })
        assert conflict.status_code == 422

        folder_id = folder["id"].removeprefix("container:")
        canvas = metadb.workspace_create_canvas_action(
            uid=metadb.DEFAULT_USER_ID, container_id=folder_id,
            expected_container_version=folder["version"], name="Placed canvas")
        renamed = client.patch(f"/api/workspace/folders/{folder_id}", json={
            "expectedVersion": folder["version"], "name": "Renamed research",
        })
        assert renamed.status_code == 200, renamed.text
        renamed_folder = renamed.json()["resource"]
        assert renamed_folder["id"] == folder["id"]
        assert renamed_folder["name"] == "Renamed research"
        canvas_resolution = client.get(f"/api/workspace/resources/canvas:{canvas['id']}")
        assert canvas_resolution.status_code == 200, canvas_resolution.text
        assert canvas_resolution.json()["resource"]["parentId"] == folder["id"]
        stale = client.patch(f"/api/workspace/folders/{folder_id}", json={
            "expectedVersion": folder["version"], "name": "Stale rename",
        })
        assert stale.status_code == 409
        nonempty_delete = client.request("DELETE", f"/api/workspace/folders/{folder_id}", json={
            "expectedVersion": renamed_folder["version"],
        })
        assert nonempty_delete.status_code == 422

        empty = client.post("/api/workspace/folders", json={
            "parentId": root["id"], "expectedParentVersion": root["version"],
            "name": "Empty", "requestId": str(uuid.uuid4()),
        })
        assert empty.status_code == 200, empty.text
        empty_resource = empty.json()["resource"]
        empty_id = empty_resource["id"].removeprefix("container:")
        deleted = client.request("DELETE", f"/api/workspace/folders/{empty_id}", json={
            "expectedVersion": empty_resource["version"],
        })
        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/api/workspace/resources/{empty_resource['id']}").status_code == 404

        root_page = client.get(f"/api/workspace/containers/{root['id']}")
        assert root_page.status_code == 200, root_page.text
        root_resource = root_page.json()["container"]
        assert root_resource["canCreateFolder"] is True
        assert root_resource["canRenameFolder"] is False
        assert root_resource["canDeleteFolder"] is False


def test_delete_recreate_and_placement_moves_preserve_independent_targets(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root_id = metadb.local_workspace_root()["id"]
    first = metadb.workspace_create_container(root_id, f"workspace-{token}-recreate")
    metadb.workspace_delete_container(first["id"], expected_version=first["version"])
    replacement = metadb.workspace_create_container(root_id, f"workspace-{token}-recreate")
    assert replacement["id"] != first["id"]

    destination = metadb.workspace_create_container(root_id, f"workspace-{token}-destination")
    canvas_placement = metadb.workspace_create_placement(
        replacement["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-canvas",
    )

    with metadb.session() as session:
        canvas_before = session.get(metadb.Canvas, workspace_scope["canvas_id"])
        entry_before = session.get(metadb.CatalogEntry, workspace_scope["uri"])
        canvas_doc, canvas_version = canvas_before.doc, canvas_before.version
        entry_doc, registration_id = entry_before.doc, entry_before.registration_id

    moved = metadb.workspace_update_placement(
        canvas_placement["id"], expected_version=canvas_placement["version"],
        container_id=destination["id"], ordinal=9,
    )
    assert moved["id"] == canvas_placement["id"]
    assert moved["targetId"] == workspace_scope["canvas_id"]
    assert moved["containerId"] == destination["id"]

    with metadb.session() as session:
        canvas_after = session.get(metadb.Canvas, workspace_scope["canvas_id"])
        entry_after = session.get(metadb.CatalogEntry, workspace_scope["uri"])
        assert (canvas_after.doc, canvas_after.version) == (canvas_doc, canvas_version)
        assert (entry_after.doc, entry_after.registration_id) == (entry_doc, registration_id)

    with pytest.raises(metadb.WorkspaceVersionConflict, match="version"):
        metadb.workspace_update_placement(
            canvas_placement["id"], expected_version=canvas_placement["version"], ordinal=10)

    with pytest.raises(metadb.WorkspaceVersionConflict, match="version"):
        metadb.workspace_delete_placement(
            canvas_placement["id"], expected_version=canvas_placement["version"])
    metadb.workspace_delete_placement(moved["id"], expected_version=moved["version"])
    replacement_placement = metadb.workspace_create_placement(
        destination["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-canvas",
    )
    assert replacement_placement["id"] != canvas_placement["id"]

    metadb.delete_canvas_cascade(workspace_scope["canvas_id"])
    with metadb.session() as session:
        assert session.get(
            metadb.WorkspacePlacement, replacement_placement["id"]
        ) is None


def test_dataset_recreate_gets_a_new_workspace_target_identity(workspace_scope):
    uri = workspace_scope["uri"]
    original = workspace_scope["dataset_id"]
    with metadb.session() as session:
        row = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == original,
        ))
        assert row is not None
        placement_id = row.id
    metadb.catalog_delete_entry(uri)
    with metadb.session() as session:
        detached = session.get(metadb.WorkspacePlacement, placement_id)
        assert detached is not None and detached.target_id == original
    metadb.catalog_upsert_entry(uri, "Replacement dataset", {
        "id": f"tbl_recreated_{uuid.uuid4().hex}", "name": "Replacement dataset", "uri": uri,
        "version": "v2",
    })
    assert metadb.workspace_builtin_dataset_identity(uri) != original


def test_only_a_detached_dataset_placement_can_be_removed(workspace_scope):
    uri = workspace_scope["uri"]
    with metadb.session() as session:
        placement = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == workspace_scope["dataset_id"],
        ))
        assert placement is not None
        placement_id, placement_version = placement.id, placement.version

    with TestClient(app) as client:
        live = client.request(
            "DELETE", f"/api/workspace/placements/{placement_id}/detached-dataset",
            json={"expectedVersion": placement_version},
        )
        assert live.status_code == 422

        metadb.catalog_delete_entry(uri)
        removed = client.request(
            "DELETE", f"/api/workspace/placements/{placement_id}/detached-dataset",
            json={"expectedVersion": placement_version},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json() == {"ok": True, "placementId": placement_id}

    with metadb.session() as session:
        assert session.get(metadb.WorkspacePlacement, placement_id) is None


def test_historical_missing_canvas_placement_is_not_a_workspace_resource(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    container = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-orphaned-canvas"
    )
    metadb.workspace_create_placement(
        container["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-missing-canvas",
    )
    # Simulate a pre-fix database where Canvas deletion left its placement behind.
    with metadb.session() as session:
        session.execute(delete(metadb.Canvas).where(
            metadb.Canvas.id == workspace_scope["canvas_id"]
        ))

    assert metadb.workspace_browse(
        container["id"], uid=metadb.DEFAULT_USER_ID
    )["items"] == []
    with pytest.raises(KeyError, match="not found"):
        metadb.workspace_resolve(
            f"canvas:{workspace_scope['canvas_id']}", uid=metadb.DEFAULT_USER_ID
        )


def test_managed_logical_dataset_deep_link_resolves_current_workspace_placement(workspace_scope):
    logical_id = f"logical_{uuid.uuid4().hex}"
    logical_uri = f"managed://{uuid.uuid4().hex}/output.parquet"
    catalog_key = f"tbl_{uuid.uuid4().hex}"
    with metadb.session() as session:
        entry = session.get(metadb.CatalogEntry, workspace_scope["uri"])
        assert entry is not None
        entry.logical_id = logical_id
        session.add(metadb.CatalogLogicalDataset(
            logical_id=logical_id,
            catalog_key=catalog_key,
            logical_uri=logical_uri,
            current_uri=entry.uri,
            state="active",
        ))

    try:
        with TestClient(app) as client:
            resolved = client.get(f"/api/workspace/resources/dataset:{logical_id}")
        assert resolved.status_code == 200, resolved.text
        resource = resolved.json()["resource"]
        assert resource["id"] == f"dataset:{workspace_scope['dataset_id']}"
        assert resource["detached"] is False
    finally:
        with metadb.session() as session:
            entry = session.get(metadb.CatalogEntry, workspace_scope["uri"])
            if entry is not None:
                entry.logical_id = None
            logical = session.get(metadb.CatalogLogicalDataset, logical_id)
            if logical is not None:
                session.delete(logical)


def test_catalog_folder_projection_preserves_identity_and_tombstones_canvas_overlay(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    uri, dataset_id = workspace_scope["uri"], workspace_scope["dataset_id"]
    original = f"projection-{token}/daily"
    renamed = f"renamed-{token}/daily"
    metadb.catalog_set_metadata(uri, original, None, None, [])
    with metadb.session() as session:
        folder = session.scalar(select(metadb.CatalogFolder).where(
            metadb.CatalogFolder.path == original))
        assert folder is not None
        projection = session.scalar(select(metadb.WorkspaceContainer).where(
            metadb.WorkspaceContainer.catalog_folder_id == folder.id))
        dataset = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == dataset_id))
        assert projection is not None and dataset is not None
        assert dataset.container_id == projection.id
        folder_id, projection_id, projection_version = folder.id, projection.id, projection.version

    created = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=projection_id,
        expected_container_version=projection_version, name="Folder overlay")
    nested = metadb.workspace_create_container(projection_id, f"workspace-{token}-nested-overlay")
    recovery_cleanup = metadb.workspace_create_container(
        nested["id"], f"workspace-{token}-recovery-cleanup")
    metadb.catalog_folder_rename(original.rsplit("/", 1)[0], renamed.rsplit("/", 1)[0])
    with metadb.session() as session:
        renamed_folder = session.scalar(select(metadb.CatalogFolder).where(
            metadb.CatalogFolder.path == renamed))
        renamed_projection = session.get(metadb.WorkspaceContainer, projection_id)
        assert renamed_folder is not None and renamed_folder.id == folder_id
        assert renamed_projection is not None and renamed_projection.name == "daily"
        assert renamed_projection.catalog_folder_path == renamed
        assert session.get(metadb.WorkspacePlacement, created["resource"]["placementId"]).container_id == projection_id

    metadb.catalog_folder_delete(renamed)
    with metadb.session() as session:
        tombstone = session.get(metadb.WorkspaceContainer, projection_id)
        dataset = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == dataset_id))
        assert tombstone is not None and tombstone.catalog_folder_state == "detached"
        assert tombstone.catalog_folder_path == renamed
        assert tombstone.parent_id == metadb.LOCAL_WORKSPACE_ROOT_ID
        assert dataset is not None and dataset.container_id != projection_id
        with pytest.raises(ValueError, match="placed by the Catalog"):
            metadb.workspace_update_placement(
                dataset.id, expected_version=dataset.version,
                container_id=metadb.LOCAL_WORKSPACE_ROOT_ID)
        tombstone_version = tombstone.version

    with pytest.raises(ValueError, match="read-only Workspace tombstone"):
        metadb.workspace_create_canvas_action(
            uid=metadb.DEFAULT_USER_ID, container_id=projection_id,
            expected_container_version=tombstone_version, name="Blocked")
    with pytest.raises(ValueError, match="read-only Workspace tombstone"):
        metadb.workspace_create_container(nested["id"], f"workspace-{token}-blocked-child")
    with pytest.raises(ValueError, match="read-only Workspace tombstone"):
        metadb.workspace_update_placement(
            created["resource"]["placementId"],
            expected_version=created["resource"]["version"],
            container_id=nested["id"])
    with TestClient(app) as client:
        nested_resource = client.get(f"/api/workspace/resources/container:{nested['id']}")
        assert nested_resource.status_code == 200, nested_resource.text
        nested_dto = nested_resource.json()["resource"]
        assert nested_dto["canCreateFolder"] is False
        assert nested_dto["canRenameFolder"] is False
        assert nested_dto["canDeleteFolder"] is False
        assert nested_dto["folderMutationUnavailableReason"] == (
            "This Folder is below a detached Catalog folder and is not empty."
        )
        blocked_create = client.post("/api/workspace/folders", json={
            "parentId": nested["id"], "expectedParentVersion": nested["version"],
            "name": "Blocked", "requestId": str(uuid.uuid4()),
        })
        assert blocked_create.status_code == 422
        blocked_rename = client.patch(f"/api/workspace/folders/{nested['id']}", json={
            "expectedVersion": nested["version"], "name": "Blocked rename",
        })
        assert blocked_rename.status_code == 422
        sibling_id = f"workspace-{token}-blocked-sibling"
        blocked_sibling = client.post(
            "/api/canvas",
            params={"besideCanvasId": created["id"]},
            json={
                "id": sibling_id,
                "name": "Blocked sibling",
                "version": 1,
                "nodes": [],
                "edges": [],
            },
        )
        assert blocked_sibling.status_code == 422, blocked_sibling.text
        assert "read-only Workspace tombstone" in blocked_sibling.text
        with metadb.session() as session:
            assert session.get(metadb.Canvas, sibling_id) is None
        cleanup_resource = client.get(
            f"/api/workspace/resources/container:{recovery_cleanup['id']}")
        assert cleanup_resource.status_code == 200, cleanup_resource.text
        cleanup_dto = cleanup_resource.json()["resource"]
        assert cleanup_dto["canCreateFolder"] is False
        assert cleanup_dto["canRenameFolder"] is False
        assert cleanup_dto["canDeleteFolder"] is True
        cleaned = client.request("DELETE", f"/api/workspace/folders/{recovery_cleanup['id']}", json={
            "expectedVersion": recovery_cleanup["version"],
        })
        assert cleaned.status_code == 200, cleaned.text
    escaped = metadb.workspace_update_container(
        nested["id"], expected_version=nested["version"],
        parent_id=metadb.LOCAL_WORKSPACE_ROOT_ID)
    assert escaped["parentId"] == metadb.LOCAL_WORKSPACE_ROOT_ID
    moved = metadb.workspace_move_canvas_action(
        uid=metadb.DEFAULT_USER_ID, placement_id=created["resource"]["placementId"],
        expected_version=created["resource"]["version"],
        container_id=metadb.LOCAL_WORKSPACE_ROOT_ID, expected_container_version=1)
    assert moved["container"]["id"] == f"container:{metadb.LOCAL_WORKSPACE_ROOT_ID}"
    metadb.catalog_delete_entry(uri)
    metadb.catalog_upsert_entry(uri, "Recreated folder dataset", {
        "id": f"tbl_recreated_folder_{token}", "name": "Recreated folder dataset", "uri": uri,
        "folder": renamed, "version": "v2",
    })
    with metadb.session() as session:
        replacement_folder = session.scalar(select(metadb.CatalogFolder).where(
            metadb.CatalogFolder.path == renamed))
        replacement_projection = session.scalar(select(metadb.WorkspaceContainer).where(
            metadb.WorkspaceContainer.catalog_folder_id == replacement_folder.id)) if replacement_folder else None
        assert replacement_folder is not None and replacement_folder.id != folder_id
        assert replacement_projection is not None and replacement_projection.id != projection_id


def test_catalog_projection_partial_uniqueness_serializes_local_name_collisions(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    name = f"workspace-{token}-authority-collision"
    root_id = metadb.LOCAL_WORKSPACE_ROOT_ID
    metadb.catalog_folder_create(name)
    start = threading.Barrier(3)
    results = []

    def create_local_container():
        start.wait(timeout=5)
        try:
            results.append(metadb.workspace_create_container(root_id, name))
        except Exception as exc:  # noqa: BLE001 - assert the public conflict type below
            results.append(exc)

    threads = [threading.Thread(target=create_local_container) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        winners = [result for result in results if isinstance(result, dict)]
        conflicts = [result for result in results if isinstance(result, metadb.WorkspaceNameConflict)]
        assert len(winners) == len(conflicts) == 1
        with metadb.session() as session:
            siblings = list(session.scalars(select(metadb.WorkspaceContainer).where(
                metadb.WorkspaceContainer.parent_id == root_id,
                metadb.WorkspaceContainer.name == name)))
        assert len(siblings) == 2
        assert {row.catalog_folder_id is None for row in siblings} == {False, True}
    finally:
        for thread in threads:
            thread.join(timeout=10)
        with metadb.session() as session:
            local = session.scalar(select(metadb.WorkspaceContainer).where(
                metadb.WorkspaceContainer.parent_id == root_id,
                metadb.WorkspaceContainer.name == name,
                metadb.WorkspaceContainer.catalog_folder_id.is_(None)))
            if local is not None:
                session.delete(local)
        try:
            metadb.catalog_folder_delete(name)
        except ValueError:
            pass
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspaceContainer).where(
                metadb.WorkspaceContainer.catalog_folder_path == name))


def test_workspace_api_mixes_keyset_pages_resolves_ancestors_and_never_writes_catalog(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-api", ordinal=0)
    child = metadb.workspace_create_container(folder["id"], f"workspace-{token}-api-child", ordinal=0)
    second_child = metadb.workspace_create_container(
        folder["id"], f"workspace-{token}-api-child-two", ordinal=0)
    metadb.set_visibility(workspace_scope["canvas_id"], "workspace")
    dataset_id = workspace_scope["dataset_id"]
    canvas = metadb.workspace_create_placement(
        folder["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-canvas", ordinal=0)

    with TestClient(app) as client:
        detail = client.get(f"/api/catalog/tables/{dataset_id}", params={"registration": True})
        assert detail.status_code == 200
        assert detail.json()["name"] == "Original dataset"

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = metadb.engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        with TestClient(app) as client:
            first = client.get(f"/api/workspace/containers/{folder['id']}", params={"limit": 2})
            assert first.status_code == 200
            first_doc = first.json()
            second = client.get(f"/api/workspace/containers/{folder['id']}", params={
                "limit": 2, "cursor": first_doc["nextCursor"],
            })
            assert second.status_code == 200
            assert [item["id"] for item in first_doc["items"]] == [
                f"container:{child['id']}", f"container:{second_child['id']}",
            ]
            assert [item["id"] for item in second.json()["items"]] == [
                f"canvas:{workspace_scope['canvas_id']}"]
            assert first_doc["hasMore"] is True and second.json()["hasMore"] is False
            resolved = client.get(f"/api/workspace/resources/{canvas['targetKind']}:{canvas['targetId']}")
            assert resolved.status_code == 200
            assert [row["id"] for row in resolved.json()["ancestors"]] == [
                f"container:{root['id']}", f"container:{folder['id']}"
            ]
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert not any(statement.lstrip().startswith(("insert", "update", "delete"))
                   and "catalog_" in statement for statement in statements)


class _WorkspaceFixtureProvider:
    def __init__(self):
        self.list_calls = 0

    @staticmethod
    def _resources(mount_id: str) -> list[CatalogResource]:
        return [
            CatalogResource(placement_id="container-a", kind="container", name="shared"),
            CatalogResource(
                placement_id="dataset-a", dataset_id="dataset-a", kind="dataset", name="shared",
                uri=f"file:///{mount_id}.parquet"),
            CatalogResource(
                placement_id="nested-dataset", dataset_id="nested-dataset", kind="dataset",
                name="nested", parent_placement_id="container-a",
                uri=f"file:///{mount_id}-nested.parquet"),
        ]

    def list_children(self, mount, parent_placement_id, *, limit, cursor=None):
        self.list_calls += 1
        if mount.id == "a-slow":
            time.sleep(0.02)
        resources = sorted(
            (item for item in self._resources(mount.id)
             if item.parent_placement_id == parent_placement_id),
            key=lambda item: (item.name, item.placement_id),
        )
        start = int(cursor or 0)
        items = resources[start:start + limit]
        if mount.id == "b-partial":
            return ProviderPage(
                state="partial", items=items[:1], reason="provider returned a bounded subset")
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderPage(items=items, next_cursor=next_cursor)

    def resolve(self, mount, placement_id):
        item = next(
            (item for item in self._resources(mount.id) if item.placement_id == placement_id), None)
        return ProviderResourceResult(item=item) if item else ProviderResourceResult(
            state="unavailable", reason="resource not found", failure="not_found")

    def ancestors(self, mount, placement_id):
        if placement_id == "nested-dataset":
            return ProviderAncestors(items=[self._resources(mount.id)[0]])
        return ProviderAncestors()

    def dataset_detail(self, mount, dataset_id):
        item = next(
            (item for item in self._resources(mount.id) if item.dataset_id == dataset_id), None)
        if item is None:
            return ProviderDatasetDetailResult(
                state="unavailable", reason="dataset not found", failure="not_found")
        assert item.uri is not None and item.dataset_id is not None
        return ProviderDatasetDetailResult(item=CatalogDatasetDetail(
            dataset_id=item.dataset_id, uri=item.uri, columns=item.columns))

    def capabilities(self, _mount):
        return ProviderCapabilities(search=_mount.id != "e-unsupported")

    def search(self, mount, query, *, limit, cursor=None):
        if mount.id == "a-slow":
            time.sleep(0.02)
        tokens = query.casefold().split()
        resources = sorted(
            (item for item in self._resources(mount.id)
             if all(token in item.name.casefold() for token in tokens)),
            key=lambda item: (item.name.casefold(), item.kind, item.placement_id),
        )
        if mount.id == "f-overlimit":
            return ProviderSearchPage(items=resources[:limit + 1])
        if mount.id == "g-stuck":
            return ProviderSearchPage(items=resources[:1], next_cursor="same")
        start = int(cursor or 0)
        items = resources[start:start + limit]
        if mount.id == "b-partial":
            return ProviderSearchPage(
                state="partial", items=items[:1], reason="search snapshot is stale",
                freshness="stale")
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderSearchPage(items=items, next_cursor=next_cursor)


class _SparsePageWorkspaceProvider:
    """Neutral provider fixture with one truthful empty page before one matching item."""

    def __init__(self, *, repeat_cursor: bool = False):
        self.list_calls = 0
        self.repeat_cursor = repeat_cursor
        self.container = CatalogResource(
            placement_id="sparse-container", kind="container", name="Sparse container")
        self.dataset = CatalogResource(
            placement_id="sparse-dataset", dataset_id="sparse-dataset", kind="dataset",
            name="Sparse dataset", parent_placement_id=self.container.placement_id,
            uri="file:///sparse-dataset.parquet")

    def list_children(self, _mount, parent_placement_id, *, limit, cursor=None):
        self.list_calls += 1
        next_cursor = "same" if self.repeat_cursor else f"after:{parent_placement_id or 'root'}"
        if cursor is None or self.repeat_cursor:
            return ProviderPage(items=[], next_cursor=next_cursor)
        if cursor != next_cursor:
            return ProviderPage(state="unavailable", reason="unknown sparse cursor")
        item = self.container if parent_placement_id is None else self.dataset
        return ProviderPage(items=[item])

    def resolve(self, _mount, placement_id):
        item = next(
            (item for item in (self.container, self.dataset) if item.placement_id == placement_id),
            None,
        )
        return ProviderResourceResult(item=item) if item is not None else ProviderResourceResult(
            state="unavailable", reason="resource not found", failure="not_found")

    def ancestors(self, _mount, placement_id):
        return ProviderAncestors(items=[self.container] if placement_id == self.dataset.placement_id else [])

    def dataset_detail(self, _mount, dataset_id):
        if dataset_id != self.dataset.dataset_id:
            return ProviderDatasetDetailResult(
                state="unavailable", reason="dataset not found", failure="not_found")
        assert self.dataset.dataset_id is not None and self.dataset.uri is not None
        return ProviderDatasetDetailResult(item=CatalogDatasetDetail(
            dataset_id=self.dataset.dataset_id, uri=self.dataset.uri, columns=[]))

    def capabilities(self, _mount):
        return ProviderCapabilities(search=True)

    def search(self, _mount, _query, *, limit, cursor=None):
        if cursor is None:
            return ProviderSearchPage(items=[], next_cursor="after:search")
        if cursor != "after:search":
            return ProviderSearchPage(state="unavailable", reason="unknown sparse cursor")
        return ProviderSearchPage(items=[self.dataset])


def test_workspace_preserves_advancing_empty_provider_pages(workspace_scope, monkeypatch):
    root = metadb.local_workspace_root()
    mount_id = f"sparse-{uuid.uuid4().hex}"
    provider = _SparsePageWorkspaceProvider()
    local_browse = metadb.workspace_browse

    def empty_root_local(container_id, *args, **kwargs):
        page = local_browse(container_id, *args, **kwargs)
        return ({**page, "items": [], "nextCursor": None}
                if container_id == root["id"] else page)

    monkeypatch.setattr(workspace_providers.metadb, "workspace_browse", empty_root_local)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": root["id"],
    }]))

    with TestClient(app) as client:
        root_first = client.get(f"/api/workspace/containers/{root['id']}", params={"limit": 10})
        assert root_first.status_code == 200, root_first.text
        root_page = root_first.json()
        assert root_page["items"] == []
        assert root_page["completeness"] == "page"
        assert root_page["hasMore"] is True
        assert root_page["sources"][-1]["completeness"] == "page"
        assert provider.list_calls == 1  # Workspace must not drain sparse provider pages itself.

        root_second = client.get(f"/api/workspace/containers/{root['id']}", params={
            "limit": 10, "cursor": root_page["nextCursor"],
        })
        assert root_second.status_code == 200, root_second.text
        container = root_second.json()["items"][0]
        assert container["name"] == "Sparse container"

        external_first = client.get(
            f"/api/workspace/containers/{container['id'].removeprefix('container:')}",
            params={"limit": 10},
        )
        assert external_first.status_code == 200, external_first.text
        external_page = external_first.json()
        assert external_page["items"] == []
        assert external_page["completeness"] == "page"
        assert external_page["hasMore"] is True
        assert external_page["sources"][-1]["completeness"] == "page"

        external_second = client.get(
            f"/api/workspace/containers/{container['id'].removeprefix('container:')}",
            params={"limit": 10, "cursor": external_page["nextCursor"]},
        )
        assert external_second.status_code == 200, external_second.text
        assert [item["name"] for item in external_second.json()["items"]] == ["Sparse dataset"]

        search_first = client.get("/api/workspace/search", params={"q": "sparse", "limit": 10})
        assert search_first.status_code == 200, search_first.text
        search_page = search_first.json()
        sparse_group = next(group for group in search_page["groups"]
                            if group["source"]["mountId"] == mount_id)
        assert sparse_group["items"] == []
        assert sparse_group["source"]["completeness"] == "page"
        assert search_page["completeness"] == "page"
        assert search_page["hasMore"] is True

        search_second = client.get("/api/workspace/search", params={
            "q": "sparse", "limit": 10, "cursor": search_page["nextCursor"],
        })
        assert search_second.status_code == 200, search_second.text
        sparse_group = next(group for group in search_second.json()["groups"]
                            if group["source"]["mountId"] == mount_id)
        assert [item["name"] for item in sparse_group["items"]] == ["Sparse dataset"]


def test_workspace_rejects_reused_sparse_provider_cursor(workspace_scope, monkeypatch):
    root = metadb.local_workspace_root()
    mount_id = f"sparse-stuck-{uuid.uuid4().hex}"
    provider = _SparsePageWorkspaceProvider(repeat_cursor=True)
    local_browse = metadb.workspace_browse

    def empty_root_local(container_id, *args, **kwargs):
        page = local_browse(container_id, *args, **kwargs)
        return ({**page, "items": [], "nextCursor": None}
                if container_id == root["id"] else page)

    monkeypatch.setattr(workspace_providers.metadb, "workspace_browse", empty_root_local)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": root["id"],
    }]))

    with TestClient(app) as client:
        first = client.get(f"/api/workspace/containers/{root['id']}", params={"limit": 10})
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert first_page["items"] == [] and first_page["hasMore"] is True

        rejected = client.get(f"/api/workspace/containers/{root['id']}", params={
            "limit": 10, "cursor": first_page["nextCursor"],
        })
        assert rejected.status_code == 200, rejected.text
        rejected_page = rejected.json()
        assert rejected_page["hasMore"] is False
        assert rejected_page["sources"][-1]["completeness"] == "unavailable"
        assert rejected_page["sources"][-1]["error"] == "provider list result is invalid"
        assert provider.list_calls == 2


class _MultiPlacementWorkspaceProvider:
    """Mutable provider fixture with two occurrences of one canonical dataset."""

    def __init__(self):
        self.partial_parents: set[str] = set()
        self.dataset_ids = {"canonical-dataset"}
        self.ancestor_calls = 0
        self.search_calls = 0
        self.resources = {
            "left-parent": CatalogResource(
                placement_id="left-parent", kind="container", name="Left"),
            "right-parent": CatalogResource(
                placement_id="right-parent", kind="container", name="Right"),
            "left-occurrence": CatalogResource(
                placement_id="left-occurrence", dataset_id="canonical-dataset",
                kind="dataset", name="Shared left",
                parent_placement_id="left-parent",
                uri="file:///canonical-dataset.parquet",
                columns=[{"name": "value", "type": "int64"}],
            ),
            "right-occurrence": CatalogResource(
                placement_id="right-occurrence", dataset_id="canonical-dataset",
                kind="dataset", name="Shared right",
                parent_placement_id="right-parent",
                uri="file:///canonical-dataset.parquet",
                columns=[{"name": "value", "type": "int64"}],
            ),
        }

    def list_children(self, _mount, parent_placement_id, *, limit, cursor=None):
        resources = sorted(
            (item for item in self.resources.values()
             if item.parent_placement_id == parent_placement_id),
            key=lambda item: (item.name, item.placement_id),
        )
        if parent_placement_id in self.partial_parents:
            return ProviderPage(
                state="partial", items=[], reason="provider returned a partial snapshot")
        start = int(cursor or 0)
        items = resources[start:start + limit]
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderPage(items=items, next_cursor=next_cursor)

    def resolve(self, _mount, placement_id):
        item = self.resources.get(placement_id)
        return ProviderResourceResult(item=item) if item is not None else ProviderResourceResult(
            state="unavailable", reason="resource not found", failure="not_found")

    def ancestors(self, _mount, placement_id):
        self.ancestor_calls += 1
        item = self.resources.get(placement_id)
        if item is None or item.parent_placement_id is None:
            return ProviderAncestors()
        parent = self.resources.get(item.parent_placement_id)
        return ProviderAncestors(items=[parent] if parent is not None else [])

    def dataset_detail(self, _mount, dataset_id):
        if dataset_id not in self.dataset_ids:
            return ProviderDatasetDetailResult(
                state="unavailable", reason="dataset not found", failure="not_found")
        return ProviderDatasetDetailResult(item=CatalogDatasetDetail(
            dataset_id=dataset_id,
            uri="file:///canonical-dataset.parquet",
            columns=[{"name": "value", "type": "int64"}],
        ))

    def capabilities(self, _mount):
        return ProviderCapabilities(search=True)

    def search(self, _mount, query, *, limit, cursor=None):
        self.search_calls += 1
        resources = sorted(
            (item for item in self.resources.values()
             if item.kind == "dataset" and query.casefold() in item.name.casefold()),
            key=lambda item: item.placement_id,
        )
        start = int(cursor or 0)
        items = resources[start:start + limit]
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderSearchPage(items=items, next_cursor=next_cursor)


def test_unavailable_provider_items_preserve_identity_without_source_admission(
        workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    mount_id = f"item-availability-{token}"
    root = metadb.local_workspace_root()
    unavailable = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="fixture",
        container_id=root["id"],
        provider_placement_id="dataset-placement",
        kind="dataset",
        name="Cold dataset",
        parent_provider_placement_id="parent",
        provider_dataset_id="canonical-dataset",
        availability="unavailable",
        availability_reason="Metadata is still indexing",
    )
    canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert canonical is not None
    assert unavailable["referenceState"] == "current"
    assert unavailable["canonicalReferenceState"] == "provider_error"
    assert canonical["uri"] is None and canonical["columns"] is None
    assert metadb.workspace_provider_source_binding(unavailable["bindingId"]) is None
    mounted = workspace_providers._MountedProvider(
        CatalogMount(id=mount_id, provider="fixture"),
        root["id"],
        "",
    )
    public = workspace_providers._binding_resource(unavailable, mounted)
    assert public["unavailableReason"] == "Unavailable: Metadata is still indexing"
    assert public["referenceState"] == "current"
    assert public["canonicalReferenceState"] == "provider_error"

    crafted_uri = workspace_providers.provider_dataset_uri(
        mount_id, canonical["sourceBindingId"])
    with pytest.raises(
            workspace_providers.ProviderDatasetUnavailable,
            match="metadata is unavailable"):
        workspace_providers.provider_dataset_identity(crafted_uri)

    recovered = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="fixture",
        container_id=root["id"],
        provider_placement_id="dataset-placement",
        kind="dataset",
        name="Cold dataset",
        parent_provider_placement_id="parent",
        provider_dataset_id="canonical-dataset",
        uri="file:///cold.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )
    recovered_canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert recovered_canonical is not None
    assert recovered["bindingId"] == unavailable["bindingId"]
    assert recovered["referenceState"] == "current"
    assert recovered["canonicalReferenceState"] == "current"
    assert recovered_canonical["sourceBindingId"] == canonical["sourceBindingId"]
    assert metadb.workspace_provider_source_binding(recovered["bindingId"]) == {
        "mountId": mount_id,
        "sourceBindingId": canonical["sourceBindingId"],
    }

    unavailable_container = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="fixture",
        container_id=root["id"],
        provider_placement_id="unavailable-container",
        kind="container",
        name="Cold folder",
        availability="unsupported",
        availability_reason="This resource type cannot be browsed",
    )
    public_container = workspace_providers._binding_resource(
        unavailable_container, mounted)
    assert public_container["referenceState"] == "provider_error"
    assert public_container["unavailableReason"] == (
        "Unsupported: This resource type cannot be browsed")
    assert public_container["localPlacement"] is None
    healthy_container = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="fixture",
        container_id=root["id"],
        provider_placement_id="unavailable-container",
        kind="container",
        name="Cold folder",
    )
    assert workspace_providers._binding_resource(
        healthy_container, mounted)["localPlacement"] is not None
    degraded_again = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="fixture",
        container_id=root["id"],
        provider_placement_id="unavailable-container",
        kind="container",
        name="Cold folder",
        availability="unsupported",
        availability_reason="This resource type cannot be browsed",
    )
    assert workspace_providers._binding_resource(
        degraded_again, mounted)["localPlacement"] is None
    assert metadb.workspace_provider_reconcile_children(
        mount_id=mount_id,
        parent_provider_placement_id=None,
        seen_provider_placement_ids={"unavailable-container"},
    ) == []


def _local_filter_capabilities(*mount_ids: str) -> list[dict]:
    def entry(field: str, kind: str, options: list[dict] | None = None) -> dict:
        return {"field": field, "type": kind, "supported": True,
                "options": options or [], "reason": None}

    return [
        entry("name", "text"),
        entry("kind", "categorical"),
        entry("updated", "date_range"),
        entry("source", "categorical", [
            {"value": "local", "label": "Local"},
            *({"value": f"mount:{mount_id}", "label": mount_id} for mount_id in mount_ids),
        ]),
    ]


def _browse_provider_root_item(mount_id: str, provider_placement_id: str) -> dict:
    cursor = None
    while True:
        page = workspace_providers.browse(
            metadb.LOCAL_WORKSPACE_ROOT_ID,
            uid=metadb.DEFAULT_USER_ID,
            limit=50,
            cursor=cursor,
        )
        item = next((
            resource for resource in page["items"]
            if resource.get("mountId") == mount_id
            and resource.get("providerPlacementId") == provider_placement_id
        ), None)
        if item is not None:
            return item
        cursor = page["nextCursor"]
        if cursor is None:
            raise AssertionError("provider resource was not returned by Workspace browse")


def test_healthy_provider_dataset_degrades_and_recovers_same_canonical_generation(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    mount_id = f"degraded-recovery-{token}"
    provider = _WorkspaceFixtureProvider()
    resources = [CatalogResource(
        placement_id="dataset",
        dataset_id="canonical-dataset",
        kind="dataset",
        name="Dataset",
        uri="file:///stable.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )]
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id,
        "provider": "fixture",
    }]))

    healthy = _browse_provider_root_item(mount_id, "dataset")
    first_source = metadb.workspace_provider_source_binding(healthy["bindingId"])
    assert first_source is not None
    canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert canonical is not None
    assert canonical["referenceState"] == "current"
    canonical_columns = canonical["columns"]

    resources[0] = CatalogResource(
        placement_id="dataset",
        dataset_id="canonical-dataset",
        kind="dataset",
        name="Dataset",
        availability="unavailable",
        availability_reason="Metadata is temporarily unavailable",
    )
    degraded = _browse_provider_root_item(mount_id, "dataset")
    degraded_canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert degraded_canonical is not None
    assert degraded["id"] == healthy["id"]
    assert degraded["bindingId"] == healthy["bindingId"]
    assert degraded["referenceState"] == "current"
    assert degraded["canonicalReferenceState"] == "provider_error"
    assert "uri" not in degraded and "columns" not in degraded
    assert degraded_canonical["uri"] == "file:///stable.parquet"
    assert degraded_canonical["columns"] == canonical_columns
    assert degraded_canonical["sourceBindingId"] == canonical["sourceBindingId"]
    assert metadb.workspace_provider_source_binding(degraded["bindingId"]) is None
    with pytest.raises(
            workspace_providers.ProviderDatasetUnavailable,
            match="metadata is unavailable"):
        workspace_providers.provider_dataset_source(
            degraded["id"],
            uid=metadb.DEFAULT_USER_ID,
            resolve_physical=lambda _uri: object(),
        )

    resources[0] = CatalogResource(
        placement_id="dataset",
        dataset_id="canonical-dataset",
        kind="dataset",
        name="Dataset recovered",
        uri="file:///stable.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )
    recovered = _browse_provider_root_item(mount_id, "dataset")
    recovered_canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert recovered_canonical is not None
    assert recovered["id"] == healthy["id"]
    assert recovered["bindingId"] == healthy["bindingId"]
    assert recovered["referenceState"] == "current"
    assert recovered["canonicalReferenceState"] == "current"
    assert recovered_canonical["sourceBindingId"] == canonical["sourceBindingId"]
    assert metadb.workspace_provider_source_binding(recovered["bindingId"]) == first_source


def test_degraded_provider_dataset_rejects_conflicting_recovery_without_retargeting(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    mount_id = f"degraded-conflict-{token}"
    provider = _WorkspaceFixtureProvider()
    resources = [CatalogResource(
        placement_id="dataset",
        dataset_id="canonical-dataset",
        kind="dataset",
        name="Dataset",
        uri="file:///stable.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )]
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id,
        "provider": "fixture",
    }]))

    healthy = _browse_provider_root_item(mount_id, "dataset")
    canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert canonical is not None
    source_binding_id = canonical["sourceBindingId"]
    canonical_columns = canonical["columns"]

    resources[0] = CatalogResource(
        placement_id="dataset",
        dataset_id="canonical-dataset",
        kind="dataset",
        name="Dataset",
        availability="unavailable",
        availability_reason="Metadata is temporarily unavailable",
    )
    degraded = _browse_provider_root_item(mount_id, "dataset")
    assert degraded["canonicalReferenceState"] == "provider_error"

    resources[0] = CatalogResource(
        placement_id="dataset",
        dataset_id="canonical-dataset",
        kind="dataset",
        name="Dataset changed",
        uri="file:///retargeted.parquet",
        columns=[{"name": "other", "type": "string"}],
    )
    conflict = _browse_provider_root_item(mount_id, "dataset")
    conflicted_canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert conflicted_canonical is not None
    assert conflict["id"] == healthy["id"]
    assert conflict["bindingId"] == healthy["bindingId"]
    assert conflict["referenceState"] == "provider_error"
    assert conflict["canonicalReferenceState"] == "provider_error"
    assert "uri" not in conflict and "columns" not in conflict
    assert conflicted_canonical["uri"] == "file:///stable.parquet"
    assert conflicted_canonical["columns"] == canonical_columns
    assert conflicted_canonical["sourceBindingId"] == source_binding_id
    assert metadb.workspace_provider_source_binding(conflict["bindingId"]) is None
    with pytest.raises(
            workspace_providers.ProviderDatasetUnavailable,
            match="metadata is unavailable"):
        workspace_providers.provider_dataset_identity(
            workspace_providers.provider_dataset_uri(mount_id, source_binding_id))
    with pytest.raises(
            workspace_providers.ProviderDatasetUnavailable,
            match="metadata is unavailable"):
        workspace_providers.provider_dataset_source(
            conflict["id"],
            uid=metadb.DEFAULT_USER_ID,
            resolve_physical=lambda _uri: object(),
        )


def test_workspace_pages_keep_degraded_items_and_continue_enumeration(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    mount_id = f"degraded-page-{token}"
    parent = CatalogResource(
        placement_id="parent", kind="container", name="Remote folder")
    resources = [
        CatalogResource(
            placement_id="healthy-a",
            parent_placement_id="parent",
            dataset_id="healthy-a",
            kind="dataset",
            name="A healthy",
            uri="file:///healthy-a.parquet",
        ),
        CatalogResource(
            placement_id="cold-b",
            parent_placement_id="parent",
            dataset_id="cold-b",
            kind="dataset",
            name="B cold",
            availability="unavailable",
            availability_reason="Metadata is still indexing",
        ),
        CatalogResource(
            placement_id="healthy-c",
            parent_placement_id="parent",
            dataset_id="healthy-c",
            kind="dataset",
            name="C healthy",
            uri="file:///healthy-c.parquet",
        ),
    ]

    class Provider(_WorkspaceFixtureProvider):
        def _resources(self, _mount_id):
            return [parent, *resources]

        def ancestors(self, _mount, placement_id):
            if placement_id == "parent":
                return ProviderAncestors()
            return ProviderAncestors(items=[parent])

    provider = Provider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id,
        "provider": "fixture",
    }]))

    remote_parent = _browse_provider_root_item(mount_id, "parent")
    parent_identity = remote_parent["id"].split(":", 1)[1]
    first = workspace_providers.browse(
        parent_identity, uid=metadb.DEFAULT_USER_ID, limit=2)
    assert [item["name"] for item in first["items"]] == ["A healthy", "B cold"]
    assert first["hasMore"] is True
    assert first["completeness"] == "page"
    assert first["sources"][-1]["completeness"] == "page"
    unavailable = first["items"][1]
    assert unavailable["referenceState"] == "current"
    assert unavailable["canonicalReferenceState"] == "provider_error"
    assert unavailable["unavailableReason"] == "Unavailable: Metadata is still indexing"
    assert metadb.workspace_provider_source_binding(unavailable["bindingId"]) is None
    with pytest.raises(
            workspace_providers.ProviderDatasetUnavailable,
            match="metadata is unavailable"):
        workspace_providers.provider_dataset_source(
            unavailable["id"],
            uid=metadb.DEFAULT_USER_ID,
            resolve_physical=lambda _uri: object(),
        )

    second = workspace_providers.browse(
        parent_identity,
        uid=metadb.DEFAULT_USER_ID,
        limit=2,
        cursor=first["nextCursor"],
    )
    assert [item["name"] for item in second["items"]] == ["C healthy"]
    assert second["hasMore"] is False
    assert second["completeness"] == "complete"
    assert second["sources"][-1]["completeness"] == "complete"

    resources[1] = CatalogResource(
        placement_id="cold-b",
        parent_placement_id="parent",
        dataset_id="cold-b",
        kind="dataset",
        name="B recovered",
        uri="file:///cold-b.parquet",
    )
    recovered_page = workspace_providers.browse(
        parent_identity, uid=metadb.DEFAULT_USER_ID, limit=10)
    recovered = next(
        item for item in recovered_page["items"]
        if item["providerPlacementId"] == "cold-b")
    assert recovered["id"] == unavailable["id"]
    assert recovered["bindingId"] == unavailable["bindingId"]
    assert recovered["canonicalReferenceState"] == "current"
    assert recovered["unavailableReason"] is None


@pytest.mark.parametrize("mutation", ["deleted", "moved"])
def test_complete_provider_traversal_reconciles_all_paginated_occurrences(
        workspace_scope, monkeypatch, mutation):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-paged-reconciliation")
    mount_id = f"paged-reconciliation-{token}"
    provider = _WorkspaceFixtureProvider()
    resources = [
        CatalogResource(placement_id="a", kind="container", name="a"),
        CatalogResource(placement_id="b", kind="container", name="b"),
        CatalogResource(placement_id="c", kind="container", name="c"),
    ]
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))

    initial = workspace_providers.browse(
        folder["id"], uid=metadb.DEFAULT_USER_ID, limit=100)
    bindings = {
        item["providerPlacementId"]: item["bindingId"]
        for item in initial["items"] if item.get("mountId") == mount_id
    }
    assert set(bindings) == {"a", "b", "c"}

    if mutation == "deleted":
        resources[:] = [item for item in resources if item.placement_id != "b"]
    else:
        resources[1] = CatalogResource(
            placement_id="b", kind="container", name="b moved",
            parent_placement_id="a")

    seen: list[str] = []
    cursor = None
    while True:
        page = workspace_providers.browse(
            folder["id"], uid=metadb.DEFAULT_USER_ID, limit=1, cursor=cursor)
        seen.extend(
            item["providerPlacementId"] for item in page["items"]
            if item.get("mountId") == mount_id
        )
        cursor = page["nextCursor"]
        if cursor is None:
            assert page["completeness"] == "complete"
            assert page["sources"][-1]["completeness"] == "complete"
            break

    assert seen == ["a", "c"]
    unchanged = {
        placement_id: metadb.workspace_provider_binding(binding_id)
        for placement_id, binding_id in bindings.items()
        if placement_id != "b"
    }
    assert all(binding is not None and binding["referenceState"] == "current"
               and binding["parentProviderPlacementId"] is None
               for binding in unchanged.values())
    missing = metadb.workspace_provider_binding(bindings["b"])
    assert missing is not None
    if mutation == "deleted":
        assert missing["referenceState"] == "detached"
        assert missing["parentProviderPlacementId"] is None
    else:
        assert missing["referenceState"] == "current"
        assert missing["name"] == "b moved"
        assert missing["parentProviderPlacementId"] == "a"


def test_cold_provider_search_keeps_parent_path_unresolved_until_bounded_resolve(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-cold-search")
    mount_id = f"cold-search-{token}"
    provider = _MultiPlacementWorkspaceProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))

    cold = workspace_providers.search(
        "Shared", uid=metadb.DEFAULT_USER_ID, limit=100)
    matches = [
        item for group in cold["groups"] for item in group["items"]
        if item.get("mountId") == mount_id
    ]
    assert provider.search_calls == 1
    assert provider.ancestor_calls == 0
    assert {item["providerPlacementId"] for item in matches} == {
        "left-occurrence", "right-occurrence"}
    assert {item["parentProviderPlacementId"] for item in matches} == {
        "left-parent", "right-parent"}
    assert all(item["parentId"] is None and item["lastKnown"] is True for item in matches)
    with metadb.session() as session:
        cached = list(session.scalars(select(metadb.WorkspaceProviderBinding).where(
            metadb.WorkspaceProviderBinding.mount_id == mount_id)))
        assert {item.provider_placement_id for item in cached} == {
            "left-occurrence", "right-occurrence"}
        assert all(item.parent_binding_id is None for item in cached)

    assert metadb._engine is not None
    metadb._engine.dispose()
    metadb._engine = metadb._Session = None
    assert metadb.require_schema_at_head() == metadb.expected_schema_head()
    restarted = [
        metadb.workspace_provider_binding(item["bindingId"]) for item in matches
    ]
    assert {
        item["parentProviderPlacementId"] for item in restarted if item is not None
    } == {"left-parent", "right-parent"}
    assert all(item is not None and item["parentBindingId"] is None for item in restarted)

    resolved = [
        workspace_providers.resolve(item["id"], uid=metadb.DEFAULT_USER_ID)
        for item in matches
    ]
    assert provider.search_calls == 1
    assert provider.ancestor_calls == 2
    assert len({item["resource"]["parentId"] for item in resolved}) == 2
    assert {
        item["ancestors"][-1]["providerPlacementId"] for item in resolved
    } == {"left-parent", "right-parent"}


def test_provider_search_move_clears_stale_parent_until_resolve_hydrates_path(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-search-parent-move")
    mount_id = f"search-parent-move-{token}"
    provider = _MultiPlacementWorkspaceProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))

    root_page = workspace_providers.browse(
        folder["id"], uid=metadb.DEFAULT_USER_ID, limit=100)
    left_parent = next(
        item for item in root_page["items"]
        if item.get("providerPlacementId") == "left-parent")
    left_page = workspace_providers.browse(
        left_parent["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    original = next(
        item for item in left_page["items"]
        if item.get("providerPlacementId") == "left-occurrence")
    assert original["parentId"] == left_parent["id"]

    provider.resources["cold-parent"] = CatalogResource(
        placement_id="cold-parent", kind="container", name="Cold")
    provider.resources["left-occurrence"] = CatalogResource(
        placement_id="left-occurrence", dataset_id="canonical-dataset",
        kind="dataset", name="Shared moved cold",
        parent_placement_id="cold-parent",
        uri="file:///canonical-dataset.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )
    ancestor_calls = provider.ancestor_calls
    search = workspace_providers.search(
        "Shared moved cold", uid=metadb.DEFAULT_USER_ID, limit=100)
    moved = next(
        item for group in search["groups"] for item in group["items"]
        if item.get("mountId") == mount_id)
    assert provider.ancestor_calls == ancestor_calls
    assert moved["bindingId"] == original["bindingId"]
    assert moved["parentProviderPlacementId"] == "cold-parent"
    assert moved["parentId"] is None
    assert moved["lastKnown"] is True
    cached = metadb.workspace_provider_binding(original["bindingId"])
    assert cached is not None
    assert cached["parentProviderPlacementId"] == "cold-parent"
    assert cached["parentBindingId"] is None

    resolution = workspace_providers.resolve(
        moved["id"], uid=metadb.DEFAULT_USER_ID)
    assert provider.ancestor_calls == ancestor_calls + 1
    assert resolution["resource"]["parentId"] is not None
    assert resolution["resource"]["parentId"] != left_parent["id"]
    assert resolution["resource"]["lastKnown"] is False
    assert resolution["ancestors"][-1]["providerPlacementId"] == "cold-parent"
    hydrated = metadb.workspace_provider_binding(original["bindingId"])
    assert hydrated is not None
    cold_parent = metadb.workspace_provider_binding_for_placement(
        mount_id=mount_id, provider="fixture",
        provider_placement_id="cold-parent")
    assert cold_parent is not None
    assert hydrated["parentBindingId"] == cold_parent["bindingId"]

    provider.resources["left-occurrence"] = CatalogResource(
        placement_id="left-occurrence", dataset_id="canonical-dataset",
        kind="dataset", name="Shared moved root",
        uri="file:///canonical-dataset.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )
    rooted_search = workspace_providers.search(
        "Shared moved root", uid=metadb.DEFAULT_USER_ID, limit=100)
    rooted = next(
        item for group in rooted_search["groups"] for item in group["items"]
        if item.get("mountId") == mount_id)
    assert rooted["parentProviderPlacementId"] is None
    assert rooted["parentId"] == f"container:{folder['id']}"
    assert rooted["lastKnown"] is False
    rooted_cached = metadb.workspace_provider_binding(original["bindingId"])
    assert rooted_cached is not None
    assert rooted_cached["parentBindingId"] is None


def _workspace_provider_dataset_binding(workspace_scope, monkeypatch, provider):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-canonical-provider")
    mount_id = f"canonical-{token}"
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))
    page = workspace_providers.browse(
        folder["id"], uid=metadb.DEFAULT_USER_ID, limit=100)
    resource = next(
        item for item in page["items"] if item.get("resourceId") == "dataset-a")
    binding_id = resource["bindingId"]
    source = workspace_providers.resolve(resource["id"], uid=metadb.DEFAULT_USER_ID)[
        "canonicalSourceBinding"]
    assert source is not None
    return mount_id, binding_id, workspace_providers.provider_dataset_uri(
        source["mountId"], source["sourceBindingId"])


def test_provider_dataset_canonical_state_is_shared_across_placement_lifecycle(
        workspace_scope, tmp_path, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-occurrences")
    mount_id = f"occurrences-{token}"
    provider = _MultiPlacementWorkspaceProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))

    root_page = workspace_providers.browse(
        folder["id"], uid=metadb.DEFAULT_USER_ID, limit=100)
    containers = {
        item["providerPlacementId"]: item for item in root_page["items"]
        if item["kind"] == "container" and item.get("mountId") == mount_id
    }
    left_page = workspace_providers.browse(
        containers["left-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    right_page = workspace_providers.browse(
        containers["right-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    left = next(item for item in left_page["items"]
                if item.get("providerPlacementId") == "left-occurrence")
    right = next(item for item in right_page["items"]
                 if item.get("providerPlacementId") == "right-occurrence")
    assert left["bindingId"] != right["bindingId"]
    assert left["providerDatasetId"] == right["providerDatasetId"] == "canonical-dataset"
    assert left["parentId"] != right["parentId"]
    left_resolution = workspace_providers.resolve(
        left["id"], uid=metadb.DEFAULT_USER_ID)
    right_resolution = workspace_providers.resolve(
        right["id"], uid=metadb.DEFAULT_USER_ID)
    source_binding = left_resolution["canonicalSourceBinding"]
    assert source_binding == right_resolution["canonicalSourceBinding"]
    assert source_binding is not None
    assert len(source_binding["sourceBindingId"]) == 32
    assert "left-occurrence" not in json.dumps(source_binding)
    assert "right-occurrence" not in json.dumps(source_binding)
    assert "canonical-dataset" not in json.dumps(source_binding)
    assert "canonical-dataset.parquet" not in json.dumps(source_binding)
    path = tmp_path / "canonical.csv"
    path.write_text("value\n1\n")
    exact = _ExactFixtureAdapter(str(path))
    left_source = workspace_providers.provider_dataset_source(
        left["id"], uid=metadb.DEFAULT_USER_ID, resolve_physical=lambda _uri: exact)
    right_source = workspace_providers.provider_dataset_source(
        right["id"], uid=metadb.DEFAULT_USER_ID, resolve_physical=lambda _uri: exact)
    left_context = workspace_providers.provider_dataset_context(
        left["id"], uid=metadb.DEFAULT_USER_ID, resolve_physical=lambda _uri: exact)
    right_context = workspace_providers.provider_dataset_context(
        right["id"], uid=metadb.DEFAULT_USER_ID, resolve_physical=lambda _uri: exact)
    assert left_context == right_context
    assert left_context["sourceUri"] == left_source["data"]["config"]["uri"]
    assert left_context["sourceUri"].startswith("workspace-provider://")
    assert left_context["readMode"] == "exact"
    assert left_context["revisionId"] == "fixture-revision-1"
    assert [(column["name"], column["type"])
            for column in left_context["columns"]] == [("value", "int64")]
    assert "canonical-dataset.parquet" not in json.dumps(left_context)
    current_context = workspace_providers.provider_dataset_context(
        left["id"], uid=metadb.DEFAULT_USER_ID, resolve_physical=lambda _uri: object())
    assert current_context["datasetIdentity"] == left_context["datasetIdentity"]
    assert current_context["readMode"] == "current"
    assert current_context["revisionId"] is None
    assert current_context["committedAt"] is None
    left_config, right_config = left_source["data"]["config"], right_source["data"]["config"]
    assert left_config["uri"] == right_config["uri"]
    assert left_config["datasetRef"] == right_config["datasetRef"]
    assert left_config["providerResourceRef"] != right_config["providerResourceRef"]
    from hub.execution_manifest import _canonical_graph
    from hub.models import Graph
    from hub.plan_key import plan_hash
    left_graph = Graph.model_validate({
        "id": "canonical-provider", "version": 1,
        "nodes": [{**left_source, "id": "source", "data": {
            **left_source["data"], "title": "Left placement",
        }}], "edges": [],
    })
    right_graph = left_graph.model_copy(deep=True)
    right_graph.nodes[0].data["title"] = "Right placement"
    right_graph.nodes[0].data["config"] = right_config
    admitted = [{
        "node_id": "source", "dataset_id": left_config["datasetRef"]["datasetId"],
        "revision_id": left_config["datasetRef"]["revisionId"], "provider": exact.name,
        "resolved_at": "2026-07-23T00:00:00Z",
    }]
    assert plan_hash(left_graph, "source", lambda _uri: None) == plan_hash(
        right_graph, "source", lambda _uri: None)
    contained = left_graph.model_copy(deep=True)
    contained.nodes[0].parent_id = "section"
    renamed_contained = contained.model_copy(deep=True)
    renamed_contained.nodes[0].data["title"] = "Renamed callable input"
    assert plan_hash(contained, "source", lambda _uri: None) != plan_hash(
        renamed_contained, "source", lambda _uri: None)
    local_source = left_graph.model_copy(deep=True)
    local_source.nodes[0].data["config"]["uri"] = "file:///local.csv"
    renamed_local = local_source.model_copy(deep=True)
    renamed_local.nodes[0].data["title"] = "Renamed local input"
    assert plan_hash(local_source, "source", lambda _uri: None) != plan_hash(
        renamed_local, "source", lambda _uri: None)
    canonical = _canonical_graph(left_graph, admitted)
    assert canonical == _canonical_graph(right_graph, admitted)
    assert "title" not in canonical["nodes"][0]["data"]
    assert not {"providerResourceRef", "providerMountId", "providerName"} & set(
        canonical["nodes"][0]["data"]["config"])
    with metadb.session() as session:
        canonical_rows = list(session.scalars(select(metadb.WorkspaceProviderDataset).where(
            metadb.WorkspaceProviderDataset.mount_id == mount_id)))
        assert len(canonical_rows) == 1

    search = workspace_providers.search(
        "Shared", uid=metadb.DEFAULT_USER_ID, limit=100)
    matches = [
        item for group in search["groups"] for item in group["items"]
        if item.get("mountId") == mount_id
    ]
    assert {item["providerPlacementId"] for item in matches} == {
        "left-occurrence", "right-occurrence"}
    assert len({item["id"] for item in matches}) == 2
    assert len({item["parentId"] for item in matches}) == 2

    # An incomplete page cannot prove that an unseen occurrence moved or was deleted.
    provider.partial_parents.add("left-parent")
    partial = workspace_providers.browse(
        containers["left-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    assert partial["sources"][1]["completeness"] == "partial"
    assert metadb.workspace_provider_binding(left["bindingId"])["referenceState"] == "current"
    provider.partial_parents.clear()

    # A complete old-parent snapshot plus resolve proves a move and updates only placement facts.
    provider.resources["left-occurrence"] = CatalogResource(
        placement_id="left-occurrence", dataset_id="canonical-dataset",
        kind="dataset", name="Shared moved",
        parent_placement_id="right-parent",
        uri="file:///canonical-dataset.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )
    workspace_providers.browse(
        containers["left-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    moved = metadb.workspace_provider_binding(left["bindingId"])
    assert moved is not None
    assert moved["referenceState"] == "current"
    assert moved["parentProviderPlacementId"] == "right-parent"
    assert moved["providerDatasetId"] == "canonical-dataset"
    moved_page = workspace_providers.browse(
        containers["right-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    moved_resource = next(
        item for item in moved_page["items"]
        if item.get("providerPlacementId") == "left-occurrence")
    assert moved_resource["bindingId"] == left["bindingId"]
    assert moved_resource["name"] == "Shared moved"
    assert workspace_providers.resolve(
        moved_resource["id"], uid=metadb.DEFAULT_USER_ID
    )["canonicalSourceBinding"] == source_binding

    # A complete parent snapshot plus not-found resolve detaches only the deleted occurrence.
    del provider.resources["left-occurrence"]
    surviving_page = workspace_providers.browse(
        containers["right-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    assert {item["providerPlacementId"] for item in surviving_page["items"]
            if item["kind"] == "dataset"} == {"right-occurrence"}
    deleted = metadb.workspace_provider_binding(left["bindingId"])
    survivor = metadb.workspace_provider_binding(right["bindingId"])
    canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert deleted is not None and deleted["referenceState"] == "detached"
    assert survivor is not None and survivor["referenceState"] == "current"
    assert canonical is not None and canonical["referenceState"] == "current"
    assert workspace_providers.resolve(
        left["id"], uid=metadb.DEFAULT_USER_ID
    )["canonicalSourceBinding"] is None
    assert workspace_providers.resolve(
        right["id"], uid=metadb.DEFAULT_USER_ID
    )["canonicalSourceBinding"] == source_binding
    # The canonical Source never re-resolves its admission placement. A deleted occurrence cannot
    # disturb the URI admitted through it while another occurrence still proves the dataset.
    assert workspace_providers.provider_dataset_adapter(
        left_source["data"]["config"]["uri"], lambda _uri: object()).source_uri == (
            right_source["data"]["config"]["uri"])

    # Canonical outage/recovery is shared state and does not detach the surviving placement.
    dataset_detail = provider.dataset_detail
    monkeypatch.setattr(
        provider,
        "dataset_detail",
        lambda *_args, **_kwargs: ProviderDatasetDetailResult(
            state="unavailable", reason="provider offline", failure="offline"),
    )
    survivor_uri = workspace_providers.provider_dataset_uri(
        source_binding["mountId"], source_binding["sourceBindingId"])
    with pytest.raises(workspace_providers.ProviderDatasetOffline):
        workspace_providers.provider_dataset_adapter(survivor_uri, lambda _uri: object())
    assert metadb.workspace_provider_binding(right["bindingId"])["referenceState"] == "current"
    assert metadb.workspace_provider_dataset(
        mount_id=mount_id,
        provider_dataset_id="canonical-dataset",
    )["referenceState"] == "offline"
    monkeypatch.setattr(provider, "dataset_detail", dataset_detail)
    workspace_providers.provider_dataset_adapter(survivor_uri, lambda _uri: object())
    assert metadb.workspace_provider_dataset(
        mount_id=mount_id,
        provider_dataset_id="canonical-dataset",
    )["referenceState"] == "current"
    assert metadb.workspace_provider_source_binding(
        right["bindingId"]) == source_binding

    # Reopening the metadata engine preserves both the tombstone and shared canonical state.
    assert metadb._engine is not None
    metadb._engine.dispose()
    metadb._engine = metadb._Session = None
    assert metadb.require_schema_at_head() == metadb.expected_schema_head()
    assert metadb.workspace_provider_binding(left["bindingId"])["referenceState"] == "detached"
    assert metadb.workspace_provider_binding(right["bindingId"])["referenceState"] == "current"
    assert metadb.workspace_provider_dataset(
        mount_id=mount_id,
        provider_dataset_id="canonical-dataset",
    )["referenceState"] == "current"
    assert metadb.workspace_provider_source_binding(
        right["bindingId"]) == source_binding

    # Conflicting canonical facts fail closed without replacing the retained URI/schema evidence.
    provider.resources["right-occurrence"] = CatalogResource(
        placement_id="right-occurrence", dataset_id="canonical-dataset",
        kind="dataset", name="Shared right",
        parent_placement_id="right-parent",
        uri="file:///conflicting.parquet",
        columns=[{"name": "value", "type": "int64"}],
    )
    workspace_providers.browse(
        containers["right-parent"]["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID,
        limit=100,
    )
    conflicted = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert conflicted is not None
    assert conflicted["referenceState"] == "provider_error"
    assert conflicted["uri"] == "file:///canonical-dataset.parquet"
    assert conflicted["sourceBindingId"] == source_binding["sourceBindingId"]
    assert metadb.workspace_provider_source_binding(right["bindingId"]) is None

    # The mount-scoped placement key does not fork when operator configuration changes provider.
    provider_changed = metadb.workspace_provider_cache_resource(
        mount_id=mount_id,
        provider="different-provider",
        container_id=folder["id"],
        provider_placement_id="left-parent",
        kind="container",
        name="Left",
    )
    assert provider_changed["bindingId"] == containers["left-parent"]["bindingId"]
    assert provider_changed["referenceState"] == "provider_error"
    assert provider_changed["provider"] == "fixture"
    with metadb.session() as session:
        occurrences = list(session.scalars(select(metadb.WorkspaceProviderBinding).where(
            metadb.WorkspaceProviderBinding.mount_id == mount_id,
            metadb.WorkspaceProviderBinding.provider_placement_id == "left-parent",
            metadb.WorkspaceProviderBinding.active.is_(True),
        )))
        assert len(occurrences) == 1


def test_provider_source_binding_is_mount_scoped_and_canonical_tombstone_is_aba_fenced(
        workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    common = {
        "provider": "fixture",
        "container_id": root["id"],
        "kind": "dataset",
        "provider_dataset_id": f"dataset-{token}",
        "uri": f"secret://credentials@host/{token}/physical.parquet",
        "columns": [{"name": "sensitive_display_column", "type": "int64"}],
    }
    first = metadb.workspace_provider_cache_resource(
        **common,
        mount_id=f"mount-a-{token}",
        provider_placement_id=f"placement-left-{token}",
        name=f"Display left {token}",
    )
    second = metadb.workspace_provider_cache_resource(
        **common,
        mount_id=f"mount-a-{token}",
        provider_placement_id=f"placement-right-{token}",
        name=f"Display right {token}",
    )
    other_mount = metadb.workspace_provider_cache_resource(
        **common,
        mount_id=f"mount-b-{token}",
        provider_placement_id=f"placement-left-{token}",
        name=f"Display left {token}",
    )

    first_source = metadb.workspace_provider_source_binding(first["bindingId"])
    assert first_source == metadb.workspace_provider_source_binding(second["bindingId"])
    assert first_source is not None
    with metadb.session() as session:
        other_canonical = session.get(
            metadb.WorkspaceProviderDataset,
            (f"mount-b-{token}", f"dataset-{token}"),
        )
        assert other_canonical is not None
        # The database uniqueness boundary is mount-scoped, so equal opaque tokens across mounts
        # remain representable and the public evidence must retain its mount discriminator.
        other_canonical.source_binding_id = first_source["sourceBindingId"]
    other_source = metadb.workspace_provider_source_binding(other_mount["bindingId"])
    assert other_source is not None
    assert first_source["mountId"] != other_source["mountId"]
    assert first_source["sourceBindingId"] == other_source["sourceBindingId"]
    assert first_source != other_source
    assert first_source["mountId"] == f"mount-a-{token}"
    encoded = json.dumps(first_source, sort_keys=True)
    for forbidden in (
        "secret", "credentials", "physical", "sensitive_display_column",
        "placement-left", "placement-right", "Display",
    ):
        assert forbidden not in encoded

    canonical_before = metadb.workspace_provider_dataset(
        mount_id=f"mount-a-{token}",
        provider_dataset_id=f"dataset-{token}",
    )
    assert canonical_before is not None
    metadb.workspace_provider_mark_dataset(
        mount_id=f"mount-a-{token}",
        provider_dataset_id=f"dataset-{token}",
        state="detached",
        error="provider reused canonical identity",
    )
    reused = metadb.workspace_provider_cache_resource(
        **common,
        mount_id=f"mount-a-{token}",
        provider_placement_id=f"placement-reused-{token}",
        name=f"Reused display {token}",
    )
    canonical_after = metadb.workspace_provider_dataset(
        mount_id=f"mount-a-{token}",
        provider_dataset_id=f"dataset-{token}",
    )
    assert canonical_after is not None
    assert canonical_after["referenceState"] == "detached"
    assert canonical_after["sourceBindingId"] == canonical_before["sourceBindingId"]
    assert metadb.workspace_provider_source_binding(first["bindingId"]) is None
    assert metadb.workspace_provider_source_binding(second["bindingId"]) is None
    assert metadb.workspace_provider_source_binding(reused["bindingId"]) is None


def test_postgres_concurrent_placements_join_one_provider_source_binding(workspace_scope):
    if metadb._is_sqlite_database():
        pytest.skip("PostgreSQL canonical Source mint concurrency regression")
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    common = {
        "mount_id": f"concurrent-source-{token}",
        "provider": "fixture",
        "container_id": metadb.LOCAL_WORKSPACE_ROOT_ID,
        "kind": "dataset",
        "provider_dataset_id": f"canonical-{token}",
        "uri": f"fixture://canonical-{token}",
        "columns": [{"name": "value", "type": "int64"}],
    }
    start = threading.Barrier(3)
    results: list[dict | Exception] = []

    def cache(placement: str) -> None:
        start.wait(timeout=5)
        try:
            results.append(metadb.workspace_provider_cache_resource(
                **common,
                provider_placement_id=placement,
                name=placement,
            ))
        except Exception as exc:  # noqa: BLE001 - concurrent failures are asserted below
            results.append(exc)

    threads = [
        threading.Thread(target=cache, args=(f"left-{token}",)),
        threading.Thread(target=cache, args=(f"right-{token}",)),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(results) == 2
    assert all(isinstance(result, dict) for result in results), results
    bindings = [
        metadb.workspace_provider_source_binding(result["bindingId"])
        for result in results if isinstance(result, dict)
    ]
    assert bindings[0] is not None
    assert bindings[0] == bindings[1]
    with metadb.session() as session:
        canonical_rows = list(session.scalars(select(
            metadb.WorkspaceProviderDataset).where(
                metadb.WorkspaceProviderDataset.mount_id == common["mount_id"],
                metadb.WorkspaceProviderDataset.provider_dataset_id
                == common["provider_dataset_id"],
            )))
    assert len(canonical_rows) == 1


@pytest.mark.parametrize("conflicting_fact", ["uri", "columns"])
def test_provider_dataset_binding_rejects_conflicting_occurrence_and_detail_facts(
        workspace_scope, monkeypatch, conflicting_fact):
    provider = _WorkspaceFixtureProvider()
    mount_id, binding_id, uri = _workspace_provider_dataset_binding(
        workspace_scope, monkeypatch, provider)
    occurrence = next(
        item for item in provider._resources(mount_id) if item.placement_id == "dataset-a")
    assert occurrence.dataset_id is not None and occurrence.uri is not None
    detail = CatalogDatasetDetail(
        dataset_id=occurrence.dataset_id,
        uri=("file:///conflicting.parquet"
             if conflicting_fact == "uri" else occurrence.uri),
        columns=(
            [{"name": "conflicting", "type": "string"}]
            if conflicting_fact == "columns" else occurrence.columns
        ),
    )
    monkeypatch.setattr(
        provider, "dataset_detail",
        lambda *_args, **_kwargs: ProviderDatasetDetailResult(item=detail),
    )
    physical_calls: list[str] = []

    with pytest.raises(
            workspace_providers.ProviderDatasetUnavailable,
            match="conflicting canonical dataset facts"):
        workspace_providers.provider_dataset_adapter(
            uri, lambda physical_uri: physical_calls.append(physical_uri) or object())

    assert physical_calls == []
    binding = metadb.workspace_provider_binding(binding_id)
    assert binding is not None
    assert binding["referenceState"] == "current"
    assert binding["canonicalReferenceState"] == "provider_error"
    assert binding["active"] is True


def test_provider_dataset_detail_not_found_tombstones_canonical_source_generation(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-canonical-detail-tombstone")
    mount_id = f"canonical-detail-tombstone-{token}"
    provider = _MultiPlacementWorkspaceProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))
    root_page = workspace_providers.browse(
        folder["id"], uid=metadb.DEFAULT_USER_ID, limit=100)
    left_parent = next(
        item for item in root_page["items"]
        if item.get("providerPlacementId") == "left-parent")
    left_page = workspace_providers.browse(
        left_parent["id"].removeprefix("container:"),
        uid=metadb.DEFAULT_USER_ID, limit=100)
    resource = next(
        item for item in left_page["items"]
        if item.get("providerPlacementId") == "left-occurrence")
    before = workspace_providers.resolve(resource["id"], uid=metadb.DEFAULT_USER_ID)
    source_binding = before["canonicalSourceBinding"]
    assert source_binding is not None
    binding_id = resource["bindingId"]
    uri = workspace_providers.provider_dataset_uri(
        source_binding["mountId"], source_binding["sourceBindingId"])

    # The real fake provider loses its canonical dataset while its stale placement remains visible.
    provider.dataset_ids.remove("canonical-dataset")
    physical_calls: list[str] = []

    with pytest.raises(
            workspace_providers.ProviderDatasetGone,
            match="canonical provider dataset was deleted"):
        workspace_providers.provider_dataset_adapter(
            uri, lambda physical_uri: physical_calls.append(physical_uri) or object())

    degraded = metadb.workspace_provider_binding(binding_id)
    assert degraded is not None
    assert degraded["referenceState"] == "current"
    assert degraded["canonicalReferenceState"] == "detached"
    assert degraded["active"] is True
    assert physical_calls == []

    # A later passive resolve can observe the same provider dataset ID again, but it must not
    # revive the terminal canonical generation or make its old Source token available.
    provider.dataset_ids.add("canonical-dataset")
    reappeared = workspace_providers.resolve(
        resource["id"], uid=metadb.DEFAULT_USER_ID)
    assert reappeared["resource"]["bindingId"] == binding_id
    assert reappeared["resource"]["referenceState"] == "current"
    assert reappeared["resource"]["canonicalReferenceState"] == "detached"
    assert reappeared["resource"]["lastKnown"] is True
    assert reappeared["canonicalSourceBinding"] is None
    canonical = metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="canonical-dataset")
    assert canonical is not None
    assert canonical["referenceState"] == "detached"
    assert canonical["sourceBindingId"] == source_binding["sourceBindingId"]
    assert metadb.workspace_provider_source_binding(binding_id) is None


class _ExactFixtureAdapter:
    name = "fixture-exact"
    retention_owner = "provider"
    revision_selectors = frozenset({"exact", "latest"})

    def __init__(self, path: str):
        self.path = path
        self.failure: str | None = None
        self.head = "fixture-revision-1"
        self.open_calls: list[str] = []
        self.preview_calls: list[tuple[str, int]] = []

    def matches(self, _uri):
        return True

    def scan(self, _uri, columns=None, predicate=None, limit=None, options=None):
        return DuckDBAdapter().scan(
            self.path, columns=columns, predicate=predicate, limit=limit, options=options)

    def preview_scan(self, _uri, columns=None, limit=2000, options=None):
        return DuckDBAdapter().preview_scan(
            self.path, columns=columns, limit=limit, options=options)

    def schema(self, _uri):
        return DuckDBAdapter().schema(self.path)

    def count(self, _uri):
        return DuckDBAdapter().count(self.path)

    def fingerprint(self, _uri):
        return "fixture-metadata"

    def write(self, _uri, _rel, mode="overwrite"):
        del mode
        raise PermissionError("read-only fixture")

    def revision_history(self, _uri, *, limit, cursor=None):
        del limit, cursor
        return [self.resolve_revision(_uri)], None

    def resolve_revision(self, _uri, *, as_of=None):
        del as_of
        if self.failure == "permission":
            raise PermissionError("secret provider detail")
        if self.failure == "offline":
            raise RevisionProviderOffline("secret provider detail")
        return {
            "revision_id": self.head,
            "committed_at": datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
        }

    def open_revision(self, _uri, revision_id):
        self.open_calls.append(revision_id)
        if self.failure == "permission":
            raise PermissionError("secret provider detail")
        if self.failure == "offline":
            raise RevisionProviderOffline("secret provider detail")
        if revision_id != "fixture-revision-1":
            raise RevisionUnavailable("revision_unavailable")
        return self.scan(_uri)

    def preview_revision(self, _uri, revision_id, *, limit):
        self.preview_calls.append((revision_id, limit))
        if self.failure == "permission":
            raise PermissionError("secret provider detail")
        if self.failure == "offline":
            raise RevisionProviderOffline("secret provider detail")
        if revision_id != "fixture-revision-1":
            raise RevisionUnavailable("revision_unavailable")
        return self.preview_scan(_uri, limit=limit)

    def revision_detail(self, _uri, revision_id, *, preview_limit):
        del preview_limit
        relation = self.open_revision(_uri, revision_id)
        rows = int(relation.aggregate("count(*) AS n").fetchone()[0])
        return {
            "revision_id": revision_id,
            "columns": [],
            "row_count": rows,
            "preview_table": relation.limit(1).arrow(),
        }


def test_provider_dataset_use_exact_preview_and_mutable_run_rejection(
        workspace_scope, tmp_path, monkeypatch):
    path = tmp_path / "provider.csv"
    path.write_text("value\n1\n2\n")
    provider = _WorkspaceFixtureProvider()
    resource = CatalogResource(
        placement_id="dataset-a", dataset_id="dataset-a", kind="dataset",
        name="Provider observations", uri=str(path))
    resources = [resource]
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {"id": "provider-use", "provider": "fixture"},
    ]))
    deps = get_deps()
    exact_adapter = _ExactFixtureAdapter(str(path))
    monkeypatch.setattr(deps, "resolve_physical_adapter", lambda _uri: exact_adapter)
    normal_resolve_adapter = deps.resolve_adapter
    monkeypatch.setattr(
        deps, "resolve_adapter",
        lambda uri: exact_adapter if uri == str(path) else normal_resolve_adapter(uri),
    )

    with TestClient(app) as client:
        root = client.get(
            f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}",
            params={"limit": 50},
        ).json()
        provider_resource = next(
            (item for item in root["items"] if item.get("mountId") == "provider-use"), None)
        cursor = root["nextCursor"]
        while provider_resource is None and cursor is not None:
            page = client.get(
                f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}",
                params={"limit": 50, "cursor": cursor},
            ).json()
            provider_resource = next(
                (item for item in page["items"] if item.get("mountId") == "provider-use"), None)
            cursor = page["nextCursor"]
        assert provider_resource is not None
        created = client.post("/api/workspace/canvases", json={
            "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
            "expectedContainerVersion": root["container"]["version"],
            "name": "Provider exact",
            "providerDatasetRefs": [provider_resource["id"]],
        })
        assert created.status_code == 200, created.text
        graph = client.get(f"/api/canvas/{created.json()['id']}").json()
        assert len(graph["nodes"]) == 1
        source = graph["nodes"][0]
        assert source["type"] == "source"
        assert created.json()["nodeId"] == source["id"]
        config = source["data"]["config"]
        assert config["uri"].startswith("workspace-provider://")
        assert str(path) not in json.dumps(graph)
        assert config["providerReadMode"] == "exact"
        assert config["datasetRef"]["revisionId"] == "fixture-revision-1"
        dataset_id = config["datasetRef"]["datasetId"]
        capabilities = client.get(f"/api/catalog/tables/{dataset_id}/revisions/capabilities")
        assert capabilities.status_code == 200 and capabilities.json()["datasetViewSave"] is True
        history = client.get(f"/api/catalog/tables/{dataset_id}/revisions")
        assert history.status_code == 200 and history.json()["items"][0]["datasetId"] == dataset_id
        detail = client.get(f"/api/catalog/revisions/{dataset_id}/fixture-revision-1")
        assert detail.status_code == 200, detail.text
        assert detail.json()["summary"]["rowCount"] == 2
        missing_detail = client.get(f"/api/catalog/revisions/{dataset_id}/missing-revision")
        assert missing_detail.status_code == 410, missing_detail.text

        # The Canvas persists only the exact DatasetRef, not the private dispatch revision field.
        # Both card and run estimates must reuse that exact detail count through a registered
        # one-to-one map without consulting a mutable provider head.
        from hub import estimate as estimate_mod
        from hub.models import ColumnSchema
        from hub.plugins.processors import RegisteredProcessor

        processor_id = "fixture.provider-exact-estimate-map"
        deps.registry.register(RegisteredProcessor(
            id=processor_id,
            version="v1",
            title="Exact estimate map",
            mode="map",
            input_schema=[ColumnSchema(name="value", type="int")],
            output_schema=[ColumnSchema(name="value", type="int")],
            fn_factory=lambda _params: lambda row: row,
        ))
        estimate_graph = json.loads(json.dumps(graph))
        estimate_graph["nodes"].append({
            "id": "exact-estimate-map",
            "type": "transform",
            "position": {"x": 360, "y": 160},
            "data": {
                "title": "Exact estimate map",
                "status": "draft",
                "config": {
                    "source": "library",
                    "processor": processor_id,
                    "version": "v1",
                    "mode": "map",
                    "onError": "raise",
                },
            },
        })
        estimate_graph["edges"].append({
            "id": "source-exact-estimate-map",
            "source": source["id"],
            "target": "exact-estimate-map",
            "data": {"wire": "dataset"},
        })
        try:
            estimate_mod._COUNT_CACHE.clear()
            graph_estimate = client.post("/api/graph/estimate", json={
                "graph": estimate_graph,
                "targetNodeId": "exact-estimate-map",
            })
            assert graph_estimate.status_code == 200, graph_estimate.text
            assert graph_estimate.json()[source["id"]] == {
                "rows": 2, "confidence": "exact"}
            assert graph_estimate.json()["exact-estimate-map"] == {
                "rows": 2, "confidence": "exact"}

            estimate_mod._COUNT_CACHE.clear()
            run_estimate = client.post("/api/run/estimate", json={
                "graph": estimate_graph,
                "targetNodeId": "exact-estimate-map",
            })
            assert run_estimate.status_code == 200, run_estimate.text
            assert run_estimate.json()["rows"] == 2
            assert "unknown_population" not in run_estimate.json()["confirmationReasons"]
        finally:
            deps.registry._procs.pop(processor_id, None)

        view = client.post("/api/dataset-views", json={
            "submissionId": uuid.uuid4().hex, "name": "Provider exact view",
            "datasetRef": config["datasetRef"], "selectedColumns": ["value"],
            "predicate": None, "sampling": {"kind": "all"},
        })
        assert view.status_code == 201, view.text
        # Erasing the source placement is deliberately irrelevant to canonical preview/revision/view
        # reopening; only canonical dataset disappearance may make these unavailable.
        original_resolve = provider.resolve
        monkeypatch.setattr(provider, "resolve", lambda *_args, **_kwargs: ProviderResourceResult(
            state="unavailable", reason="placement deleted", failure="not_found"))
        assert client.get(f"/api/catalog/tables/{dataset_id}/revisions").status_code == 200
        assert client.post(f"/api/dataset-views/{view.json()['id']}/preview").status_code == 200
        monkeypatch.setattr(provider, "resolve", original_resolve)
        exact_adapter.open_calls.clear()
        exact_adapter.head = "fixture-revision-2"

        preview = client.post("/api/run/preview", json={
            "graph": graph, "nodeId": source["id"], "k": 10,
        })
        assert preview.status_code == 200, preview.text
        assert [row["value"] for row in preview.json()["rows"]] == [1, 2]
        assert exact_adapter.preview_calls == [
            ("fixture-revision-1", 2000),
            ("fixture-revision-1", 2000),
            ("fixture-revision-1", 2000),
        ]
        assert exact_adapter.open_calls == []
        assert preview.json()["inputManifest"][0] == {
            "node_id": source["id"],
            "dataset_id": config["datasetRef"]["datasetId"],
            "revision_id": "fixture-revision-1",
            "provider": "fixture-exact",
            "resolved_at": preview.json()["inputManifest"][0]["resolved_at"],
        }
        dispatched = False

        def reject_dispatch(*_args, **_kwargs):
            nonlocal dispatched
            dispatched = True
            raise AssertionError("provider source reached the runner before exact validation")

        monkeypatch.setattr(deps.runner, "run", reject_dispatch)
        run_index_before = set(deps.run_index)
        exact_adapter.failure = "permission"
        denied = client.post("/api/run", json={
            "graph": graph, "targetNodeId": source["id"],
            "inputManifest": preview.json()["inputManifest"],
        })
        assert denied.status_code == 403
        assert denied.json()["detail"] == (
            "You no longer have permission to read the pinned version of an input dataset.")
        exact_adapter.failure = "offline"
        offline = client.post("/api/run", json={
            "graph": graph, "targetNodeId": source["id"],
            "inputManifest": preview.json()["inputManifest"],
        })
        assert offline.status_code == 503
        assert offline.json()["detail"] == (
            "The data source for a pinned input version is offline. Try again once it is "
            "reachable.")
        assert dispatched is False
        assert set(deps.run_index) == run_index_before
        exact_adapter.failure = None

        def missing_provider_adapter(_uri):
            raise LookupError("secret package activation detail")

        monkeypatch.setattr(deps, "resolve_physical_adapter", missing_provider_adapter)
        unavailable = client.post("/api/run", json={
            "graph": graph, "targetNodeId": source["id"],
            "inputManifest": preview.json()["inputManifest"],
        })
        assert unavailable.status_code == 409, unavailable.text
        assert unavailable.json()["detail"] == (
            "This data source is unavailable because its plugin is missing. Install or restore "
            "the plugin, then try again.")
        assert "offline" not in unavailable.text
        assert "secret" not in unavailable.text
        assert dispatched is False
        assert set(deps.run_index) == run_index_before
        monkeypatch.setattr(deps, "resolve_physical_adapter", lambda _uri: exact_adapter)

        normal_resolve = provider.resolve
        monkeypatch.setattr(provider, "resolve", lambda *_args, **_kwargs: ProviderResourceResult(
            state="unavailable", reason="secret upstream tenant detail", failure="offline"))
        sanitized = client.post("/api/workspace/canvases", json={
            "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
            "expectedContainerVersion": root["container"]["version"],
            "name": "Provider unavailable",
            "providerDatasetRefs": [provider_resource["id"]],
        })
        assert sanitized.status_code == 503
        assert sanitized.json()["detail"] == (
            "This data source is offline. Try again once it is reachable.")
        assert "tenant" not in sanitized.text
        monkeypatch.setattr(provider, "resolve", normal_resolve)

        def missing_provider(_name):
            raise LookupError("secret package activation detail")

        monkeypatch.setattr(workspace_providers, "_load_provider", missing_provider)
        incompatible = client.post("/api/workspace/canvases", json={
            "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
            "expectedContainerVersion": root["container"]["version"],
            "name": "Provider incompatible",
            "providerDatasetRefs": [provider_resource["id"]],
        })
        assert incompatible.status_code == 409, incompatible.text
        assert incompatible.json()["detail"] == (
            "This data source is unavailable because its plugin is missing. Install or restore "
            "the plugin, then try again.")
        assert "secret" not in incompatible.text
        incompatible_add = client.post(
            f"/api/workspace/canvases/{created.json()['id']}/datasets",
            json={
                "expectedCanvasVersion": graph["version"],
                "providerDatasetRefs": [provider_resource["id"]],
                "requestId": "provider-add-check",
            },
        )
        assert incompatible_add.status_code == 409, incompatible_add.text
        assert incompatible_add.json()["detail"] == incompatible.json()["detail"]
        monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)

        invalid_item = CatalogResource.model_construct(
            id="dataset-a", kind="dataset", name="x" * 513, uri=str(path), columns=[])
        monkeypatch.setattr(provider, "resolve", lambda *_args, **_kwargs:
                            ProviderResourceResult.model_construct(
                                state="ready", item=invalid_item, reason=None, failure=None))
        malformed = client.post("/api/workspace/canvases", json={
            "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
            "expectedContainerVersion": root["container"]["version"],
            "name": "Provider malformed",
            "providerDatasetRefs": [provider_resource["id"]],
        })
        assert malformed.status_code == 409, malformed.text
        assert malformed.json()["detail"] == incompatible.json()["detail"]
        assert "x" * 513 not in malformed.text
        monkeypatch.setattr(provider, "resolve", normal_resolve)

        monkeypatch.setattr(deps, "resolve_physical_adapter", lambda _uri: DuckDBAdapter())
        monkeypatch.setattr(deps, "resolve_adapter", normal_resolve_adapter)
        mutable = client.post("/api/workspace/canvases", json={
            "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
            "expectedContainerVersion": root["container"]["version"],
            "name": "Provider mutable",
            "providerDatasetRefs": [provider_resource["id"]],
        })
        assert mutable.status_code == 200, mutable.text
        mutable_graph = client.get(f"/api/canvas/{mutable.json()['id']}").json()
        mutable_source = mutable_graph["nodes"][0]
        assert mutable_source["data"]["config"]["providerReadMode"] == "mutable"
        assert "datasetRef" not in mutable_source["data"]["config"]
        mutable_preview = client.post("/api/run/preview", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"], "k": 1,
        })
        assert mutable_preview.status_code == 200, mutable_preview.text
        assert mutable_preview.json()["rows"] == [{"value": 1}]

        decoded_private_path = "/private/provider-decoded-secret.csv"

        class _PathEchoingAdapter(DuckDBAdapter):
            def preview_scan(self, uri, columns=None, limit=2000, options=None):
                del uri, columns, limit, options
                # DuckDB materializes this only after Source lowering has returned. The route boundary
                # must still keep a provider-owned decoded path out of the API error envelope.
                return db.conn().sql(
                    f"select error('{decoded_private_path}') as provider_failure")

        monkeypatch.setattr(
            deps, "chosen_backend",
            lambda _uid=None, _requested=None: "local-out-of-core")
        path_echoing_adapter = _PathEchoingAdapter()
        monkeypatch.setattr(
            deps, "resolve_adapter",
            lambda uri: (path_echoing_adapter if uri == str(path)
                         else normal_resolve_adapter(uri)),
        )
        direct_preview_failure = client.post("/api/run/preview", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"], "k": 1,
        })
        assert direct_preview_failure.status_code == 200, direct_preview_failure.text
        assert direct_preview_failure.json()["reason"] == "provider dataset inspection failed"
        assert str(path) not in direct_preview_failure.text
        assert decoded_private_path not in direct_preview_failure.text
        direct_profile_failure = client.post("/api/run/profile", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"],
        })
        assert direct_profile_failure.status_code == 200, direct_profile_failure.text
        assert direct_profile_failure.json()["reason"] == "provider dataset inspection failed"
        assert str(path) not in direct_profile_failure.text
        assert decoded_private_path not in direct_profile_failure.text
        monkeypatch.setattr(deps, "resolve_adapter", normal_resolve_adapter)

        class _KernelPreview:
            echo_path = False
            transport_error: str | None = None
            not_previewable_reason: str | None = None

            def _child_resolve(self, uri):
                if uri == str(path):
                    return _PathEchoingAdapter() if self.echo_path else DuckDBAdapter()
                raise workspace_providers.ProviderDatasetUnavailable(
                    "provider mount config is absent from the kernel")

            def preview(self, private_graph, node_id, k, offset, port_id):
                if self.transport_error is not None:
                    raise RuntimeError(self.transport_error)
                private_config = private_graph.nodes[0].data["config"]
                assert private_config["uri"].startswith("workspace-provider://")
                assert private_config["_input_provider_preview_uri"] == str(path)
                assert private_config["cacheable"] is False
                return preview_node(
                    private_graph, node_id, k, self._child_resolve, deps.registry,
                    deps.node_builders, deps.node_specs, offset=offset,
                    storage=deps.storage, port_id=port_id,
                ).model_dump()

            def profile(self, private_graph, node_id, *, full, port_id):
                if self.transport_error is not None:
                    raise RuntimeError(self.transport_error)
                if self.not_previewable_reason is not None:
                    return {
                        "not_previewable": True,
                        "reason": self.not_previewable_reason,
                        "target_port_id": port_id,
                    }
                assert full is False
                assert private_graph.nodes[0].data["config"]["_input_provider_preview_uri"] == str(path)
                return profile_node(
                    private_graph, node_id, self._child_resolve, deps.registry,
                    deps.node_builders, deps.node_specs, full=False,
                    storage=deps.storage, port_id=port_id,
                ).model_dump()

        kernel_preview_backend = _KernelPreview()
        monkeypatch.setattr(
            deps, "chosen_backend", lambda _uid=None, _requested=None: "kernel")
        monkeypatch.setattr(deps, "kernel_backend", lambda: kernel_preview_backend)
        kernel_preview = client.post("/api/run/preview", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"], "k": 1,
        })
        assert kernel_preview.status_code == 200, kernel_preview.text
        assert kernel_preview.json()["rows"] == [{"value": 1}]
        assert str(path) not in kernel_preview.text
        kernel_profile = client.post("/api/run/profile", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"],
        })
        assert kernel_profile.status_code == 200, kernel_profile.text
        assert kernel_profile.json()["rowCount"] == 2
        assert str(path) not in kernel_profile.text

        kernel_preview_backend.echo_path = True
        kernel_preview_failure = client.post("/api/run/preview", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"], "k": 1,
        })
        assert kernel_preview_failure.status_code == 200, kernel_preview_failure.text
        assert kernel_preview_failure.json()["reason"] == "provider dataset inspection failed"
        assert str(path) not in kernel_preview_failure.text
        kernel_preview_backend.echo_path = False
        kernel_preview_backend.transport_error = f"kernel cannot read {path}"
        kernel_profile_failure = client.post("/api/run/profile", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"],
        })
        assert kernel_profile_failure.status_code == 200, kernel_profile_failure.text
        assert kernel_profile_failure.json()["reason"] == "provider dataset inspection failed"
        assert str(path) not in kernel_profile_failure.text
        kernel_preview_backend.transport_error = None
        kernel_preview_backend.not_previewable_reason = "downstream node is not previewable"
        downstream_failure = client.post("/api/run/profile", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"],
        })
        assert downstream_failure.status_code == 200, downstream_failure.text
        assert downstream_failure.json()["reason"] == "downstream node is not previewable"
        kernel_preview_backend.not_previewable_reason = None

        preallocations = 0
        original_preallocate = metadb.preallocate_or_adopt_profile_run_owner

        def track_preallocation(*args, **kwargs):
            nonlocal preallocations
            preallocations += 1
            return original_preallocate(*args, **kwargs)

        monkeypatch.setattr(
            metadb, "preallocate_or_adopt_profile_run_owner", track_preallocation)
        mutable_identity = client.post("/api/run/profile-identity", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"],
        })
        assert mutable_identity.status_code == 409, mutable_identity.text
        mutable_estimate = client.post("/api/run/profile-estimate", json={
            "graph": mutable_graph, "nodeId": mutable_source["id"],
        })
        assert mutable_estimate.status_code == 409, mutable_estimate.text
        mutable_profile_job = client.post("/api/run/profile-job", json={
            "graph": mutable_graph,
            "nodeId": mutable_source["id"],
            "planDigest": "0" * 64,
            "submissionId": "00000000-0000-4000-8000-000000000474",
        })
        assert mutable_profile_job.status_code == 409, mutable_profile_job.text
        assert preallocations == 0

        missing_binding_graph = json.loads(json.dumps(mutable_graph))
        missing_binding_graph["nodes"][0]["data"]["config"]["uri"] = (
            workspace_providers.provider_dataset_uri("provider-use", "0" * 32))
        missing_binding = client.post("/api/run", json={
            "graph": missing_binding_graph, "targetNodeId": mutable_source["id"],
        })
        assert missing_binding.status_code == 410, missing_binding.text
        malformed_binding_graph = json.loads(json.dumps(mutable_graph))
        malformed_binding_graph["nodes"][0]["data"]["config"]["uri"] = (
            "workspace-provider://malformed")
        malformed_binding = client.post("/api/run", json={
            "graph": malformed_binding_graph, "targetNodeId": mutable_source["id"],
        })
        assert malformed_binding.status_code == 409, malformed_binding.text
        assert dispatched is False
        rejected = client.post("/api/run", json={
            "graph": mutable_graph, "targetNodeId": mutable_source["id"],
        })
        assert rejected.status_code == 409, rejected.text
        assert "cannot pin an exact version" in rejected.json()["detail"]
        assert dispatched is False
        assert set(deps.run_index) == run_index_before
        fabricated = client.post("/api/run", json={
            "graph": mutable_graph, "targetNodeId": mutable_source["id"],
            "inputManifest": [{
                "node_id": mutable_source["id"],
                "dataset_id": workspace_providers.provider_dataset_identity(
                    mutable_source["data"]["config"]["uri"]),
                "revision_id": "fabricated-revision",
                "provider": "duckdb", "resolved_at": "2026-07-18T00:00:00Z",
            }],
        })
        assert fabricated.status_code == 409, fabricated.text
        assert "cannot pin an exact version" in fabricated.json()["detail"]
        assert dispatched is False
        assert set(deps.run_index) == run_index_before

        monkeypatch.setattr(provider, "resolve", lambda *_args, **_kwargs: ProviderResourceResult(
            state="unavailable", reason="secret deleted-resource detail", failure="not_found"))
        detached = client.get(f"/api/workspace/resources/{provider_resource['id']}")
        assert detached.status_code == 200, detached.text
        assert detached.json()["resource"]["referenceState"] == "detached"
        gone_use = client.post("/api/workspace/canvases", json={
            "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
            "expectedContainerVersion": root["container"]["version"],
            "name": "Provider gone",
            "providerDatasetRefs": [provider_resource["id"]],
        })
        assert gone_use.status_code == 410, gone_use.text
        assert gone_use.json()["detail"] == (
            "This data source was deleted. Link it again to keep using it.")
        gone_add = client.post(
            f"/api/workspace/canvases/{created.json()['id']}/datasets",
            json={
                "expectedCanvasVersion": graph["version"],
                "providerDatasetRefs": [provider_resource["id"]],
                "requestId": "provider-add-check",
            },
        )
        assert gone_add.status_code == 410, gone_add.text
        assert gone_add.json()["detail"] == gone_use.json()["detail"]
        monkeypatch.setattr(provider, "resolve", normal_resolve)
        gone = client.post("/api/run", json={
            "graph": graph, "targetNodeId": source["id"],
        })
        # The admission occurrence is detached, but the canonical dataset remains current. This
        # run now reaches normal exact-capability admission instead of substituting a 410.
        assert gone.status_code == 409, gone.text
        assert "cannot pin an exact version" in gone.json()["detail"]
        assert "secret" not in gone.text
        assert dispatched is False
        assert set(deps.run_index) == run_index_before


def test_canonical_provider_tombstone_closes_source_revision_and_dataset_view(
        workspace_scope, tmp_path, monkeypatch):
    path = tmp_path / "tombstone.csv"
    path.write_text("value\n1\n")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"canonical-tombstone-{workspace_scope['canvas_id'][-12:]}")
    provider = _WorkspaceFixtureProvider()
    resource = CatalogResource(
        placement_id="dataset-a", dataset_id="dataset-a", kind="dataset",
        name="Tombstone source", uri=str(path))
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: [resource])
    mount_id = "m" * 128
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))
    deps = get_deps()
    adapter = _ExactFixtureAdapter(str(path))
    monkeypatch.setattr(deps, "resolve_physical_adapter", lambda _uri: adapter)

    with TestClient(app) as client:
        page = client.get(f"/api/workspace/containers/{folder['id']}").json()
        resource_ref = next(
            item["id"] for item in page["items"] if item.get("mountId") == mount_id)
        created = client.post("/api/workspace/canvases", json={
            "containerId": folder["id"],
            "expectedContainerVersion": page["container"]["version"],
            "name": "Canonical tombstone", "providerDatasetRefs": [resource_ref],
        })
        assert created.status_code == 200, created.text
        graph = client.get(f"/api/canvas/{created.json()['id']}").json()
        config = graph["nodes"][0]["data"]["config"]
        dataset_id = config["datasetRef"]["datasetId"]
        assert len(dataset_id) > 128
        view = client.post("/api/dataset-views", json={
            "submissionId": uuid.uuid4().hex, "name": "Tombstone view",
            "datasetRef": config["datasetRef"], "selectedColumns": ["value"],
            "predicate": None, "sampling": {"kind": "all"},
        })
        assert view.status_code == 201, view.text

        monkeypatch.setattr(provider, "dataset_detail", lambda *_args, **_kwargs:
                            ProviderDatasetDetailResult(
                                state="unavailable", reason="deleted", failure="not_found"))
        with pytest.raises(workspace_providers.ProviderDatasetGone):
            deps.resolve_adapter(config["uri"])
        assert client.get(f"/api/catalog/tables/{dataset_id}/revisions").status_code == 410
        assert client.get(
            f"/api/catalog/revisions/{dataset_id}/fixture-revision-1").status_code == 410
        assert client.post(f"/api/dataset-views/{view.json()['id']}/preview").status_code == 410


def test_canonical_provider_token_recovery_and_detach_race(workspace_scope, monkeypatch):
    mount_id = f"token-{workspace_scope['canvas_id'][-12:]}"
    calls: list[str] = []

    class Provider:
        def dataset_detail(self, _mount, dataset_id):
            calls.append(dataset_id)
            return ProviderDatasetDetailResult(item=CatalogDatasetDetail(
                dataset_id=dataset_id, uri=f"fixture://{dataset_id}", columns=[]))

    provider = Provider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {"id": mount_id, "provider": "fixture"},
    ]))

    def cached(dataset_id: str):
        return metadb.workspace_provider_cache_resource(
            mount_id=mount_id, provider="fixture", container_id=metadb.LOCAL_WORKSPACE_ROOT_ID,
            provider_placement_id=f"placement-{dataset_id}", provider_dataset_id=dataset_id,
            uri=f"fixture://{dataset_id}", columns=[], kind="dataset", name=dataset_id)

    first = cached("permission")
    source = metadb.workspace_provider_source_binding(first["bindingId"])
    assert source is not None
    uri = workspace_providers.provider_dataset_uri(source["mountId"], source["sourceBindingId"])
    token = uri.removeprefix("workspace-provider://")
    assert workspace_providers._decode_source_identity_token(token) == (
        source["mountId"], source["sourceBindingId"])
    with pytest.raises(workspace_providers.ProviderDatasetUnavailable):
        workspace_providers._decode_source_identity_token(token + "=")
    alias_token = workspace_providers.provider_dataset_uri(
        "m", source["sourceBindingId"]).removeprefix("workspace-provider://")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    unused_bits = 4 if len(alias_token) % 4 == 2 else 2
    index = alphabet.index(alias_token[-1])
    alias = alias_token[:-1] + alphabet[(index & ~((1 << unused_bits) - 1)) | 1]
    with pytest.raises(workspace_providers.ProviderDatasetUnavailable):
        workspace_providers._decode_source_identity_token(alias)

    metadb.workspace_provider_mark_dataset(
        mount_id=mount_id, provider_dataset_id="permission", state="permission_lost", error=None)
    assert workspace_providers.provider_dataset_adapter(uri, lambda _uri: object()).source_uri == uri
    assert calls == ["permission"]
    assert metadb.workspace_provider_dataset(
        mount_id=mount_id, provider_dataset_id="permission")["referenceState"] == "current"

    raced = cached("race")
    race_source = metadb.workspace_provider_source_binding(raced["bindingId"])
    assert race_source is not None
    race_uri = workspace_providers.provider_dataset_uri(
        race_source["mountId"], race_source["sourceBindingId"])
    original_mark = metadb.workspace_provider_mark_dataset

    def detach_before_current(**kwargs):
        if kwargs["state"] == "current":
            original_mark(**{**kwargs, "state": "detached"})
        return original_mark(**kwargs)

    monkeypatch.setattr(metadb, "workspace_provider_mark_dataset", detach_before_current)
    with pytest.raises(workspace_providers.ProviderDatasetGone):
        workspace_providers.provider_dataset_adapter(race_uri, lambda _uri: object())


@pytest.mark.parametrize("config", [[], "", 0, False])
def test_workspace_rejects_falsy_non_object_mount_config(monkeypatch, config):
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {"id": "invalid-config", "provider": "fixture", "config": config},
    ]))

    mounts, invalid = workspace_providers._configured_mounts()

    assert mounts == []
    assert invalid


def test_workspace_isolates_secret_resolver_failures_without_exposing_details(
        workspace_scope, monkeypatch, caplog):
    from hub.secrets import register_resolver, unregister_resolver

    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    folder = metadb.workspace_create_container(
        metadb.LOCAL_WORKSPACE_ROOT_ID, f"workspace-{token}-resolver-failure")
    scheme = f"test-mount-{token}"
    sentinel = f"resolver-material-{token}"

    def failing_resolver(_reference: str) -> str:
        raise RuntimeError(sentinel)

    register_resolver(scheme, failing_resolver)
    try:
        monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
            {
                "id": f"bad-{token}",
                "provider": "fixture",
                "containerId": folder["id"],
                "config": {"credential": f"{scheme}:credential"},
            },
            {
                "id": f"healthy-{token}",
                "provider": "fixture",
                "containerId": folder["id"],
            },
        ]))
        provider = _WorkspaceFixtureProvider()
        monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)

        mounts, invalid = workspace_providers._configured_mounts()
        assert [mounted.mount.id for mounted in mounts] == [f"healthy-{token}"]
        assert invalid is True

        with TestClient(app) as client:
            response = client.get(f"/api/workspace/containers/{folder['id']}")
        assert response.status_code == 200, response.text
        page = response.json()
        assert any(item.get("resourceId") == "dataset-a" for item in page["items"])
        assert {source["id"] for source in page["sources"]} == {
            "local", f"mount:healthy-{token}", "configuration",
        }
        assert page["sources"][-1]["error"] == "catalog mount configuration is invalid"
        assert sentinel not in response.text
        assert sentinel not in caplog.text
    finally:
        unregister_resolver(scheme)


def test_workspace_composes_mounts_with_per_source_errors_stable_cursors_and_deep_links(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-providers")
    local_child = metadb.workspace_create_container(
        folder["id"], f"workspace-{token}-local-child")
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)

    def deterministic_list_children(_provider, mount, *args, **kwargs):
        kwargs.pop("timeout", None)
        if mount.id == "a-slow":
            return ProviderPage(state="unavailable", reason="deadline exceeded")
        return _provider.list_children(mount, *args, **kwargs)

    monkeypatch.setattr(
        workspace_providers, "bounded_list_children",
        deterministic_list_children,
    )
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {"id": "a-slow", "provider": "fixture", "containerId": folder["id"]},
        {"id": "b-partial", "provider": "fixture", "containerId": folder["id"]},
        {"id": "c-first", "provider": "fixture", "containerId": folder["id"]},
        {"id": "d-second", "provider": "fixture", "containerId": folder["id"]},
    ]))

    with TestClient(app) as client:
        response = client.get(
            f"/api/workspace/containers/{folder['id']}", params={"limit": 100})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["completeness"] == "partial"
        assert f"container:{local_child['id']}" in {item["id"] for item in page["items"]}
        statuses = {item["id"]: item for item in page["sources"]}
        assert statuses["local"]["completeness"] == "complete"
        assert statuses["mount:a-slow"] == {
            "id": "mount:a-slow", "kind": "provider", "mountId": "a-slow",
            "provider": "fixture", "completeness": "unavailable",
            "error": "deadline exceeded", "referenceState": None,
        }
        assert statuses["mount:b-partial"]["completeness"] == "partial"
        assert statuses["mount:b-partial"]["error"] == "provider returned a bounded subset"

        duplicates = [item for item in page["items"]
                      if item["name"] == "shared" and item.get("resourceId") == "dataset-a"]
        assert {item["mountId"] for item in duplicates} == {"c-first", "d-second"}
        assert len({item["id"] for item in duplicates}) == 2
        assert all(item["provider"] == "fixture" and item["source"] == "provider"
                   for item in duplicates)

        paged_ids: list[str] = []
        cursor = None
        while True:
            current = client.get(f"/api/workspace/containers/{folder['id']}", params={
                "limit": 2, **({"cursor": cursor} if cursor else {}),
            })
            assert current.status_code == 200, current.text
            document = current.json()
            paged_ids.extend(item["id"] for item in document["items"])
            cursor = document["nextCursor"]
            if cursor is None:
                break
        assert paged_ids == [item["id"] for item in page["items"]]
        assert len(paged_ids) == len(set(paged_ids))

        remote_container = next(item for item in page["items"]
                                if item.get("mountId") == "c-first"
                                and item.get("resourceId") == "container-a")
        remote_identity = remote_container["id"].split(":", 1)[1]
        nested = client.get(f"/api/workspace/containers/{remote_identity}")
        assert nested.status_code == 200, nested.text
        nested_resource = nested.json()["items"][0]
        resolved = client.get(f"/api/workspace/resources/{nested_resource['id']}")
        assert resolved.status_code == 200, resolved.text
        resolution = resolved.json()
        assert resolution["resource"]["id"] == nested_resource["id"]
        assert resolution["source"]["completeness"] == "complete"
        assert [item["id"] for item in resolution["ancestors"]] == [
            f"container:{root['id']}",
            f"container:{folder['id']}",
            f"container:{workspace_providers.mount_container_identity('c-first')}",
            remote_container["id"],
        ]

        reads_before_canvas_action = provider.list_calls
        created = client.post("/api/workspace/canvases", json={
            "containerId": folder["id"], "expectedContainerVersion": folder["version"],
            "name": "Provider write guard",
        })
        assert created.status_code == 200, created.text
        assert provider.list_calls == reads_before_canvas_action
        created_document = created.json()
        assert created_document["nodeId"] is None
        assert client.get(f"/api/canvas/{created_document['id']}").json()["nodes"] == []
        metadb.workspace_delete_placement(
            created_document["resource"]["placementId"], expected_version=1)
        metadb.delete_canvas_cascade(created_document["id"])


def test_workspace_configured_mount_point_cannot_be_deleted(workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-configured-provider-mount")
    provider = _WorkspaceFixtureProvider()
    provider_search_calls = 0
    provider_search = provider.search

    def counted_search(*args, **kwargs):
        nonlocal provider_search_calls
        provider_search_calls += 1
        return provider_search(*args, **kwargs)

    monkeypatch.setattr(provider, "search", counted_search)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    mount_config = json.dumps([{
        "id": "protected-folder", "provider": "fixture", "containerId": folder["id"],
    }])
    monkeypatch.setenv("DP_CATALOG_MOUNTS", mount_config)

    def assert_delete_fenced(resource):
        assert resource["id"] == f"container:{folder['id']}"
        assert resource["canCreateFolder"] is True
        assert resource["canRenameFolder"] is True
        assert resource["canDeleteFolder"] is False
        assert resource["folderMutationUnavailableReason"] == (
            "This Folder is configured as a provider mount point and cannot be deleted."
        )

    with TestClient(app) as client:
        parent = client.get(f"/api/workspace/containers/{root['id']}", params={"limit": 100})
        assert parent.status_code == 200, parent.text
        parent_row = next(
            item for item in parent.json()["items"]
            if item["id"] == f"container:{folder['id']}")
        assert_delete_fenced(parent_row)

        browsed = client.get(f"/api/workspace/containers/{folder['id']}")
        assert browsed.status_code == 200, browsed.text
        assert any(item.get("resourceId") == "dataset-a" for item in browsed.json()["items"])
        assert_delete_fenced(browsed.json()["container"])

        resolved = client.get(f"/api/workspace/resources/container:{folder['id']}")
        assert resolved.status_code == 200, resolved.text
        assert_delete_fenced(resolved.json()["resource"])

        searched = client.get("/api/workspace/search", params={"q": folder["name"]})
        assert searched.status_code == 200, searched.text
        local_group = next(
            group for group in searched.json()["groups"] if group["source"]["id"] == "local")
        search_row = next(
            item for item in local_group["items"]
            if item["id"] == f"container:{folder['id']}")
        assert_delete_fenced(search_row)

        provider_calls_before_delete = (provider.list_calls, provider_search_calls)
        engine = metadb._engine
        workspace_dml: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            normalized = " ".join(statement.lower().split())
            if (normalized.startswith(("insert", "update", "delete"))
                    and any(table in normalized for table in (
                        "workspace_containers",
                        "workspace_placements",
                        "workspace_folder_create_replays",
                    ))):
                workspace_dml.append(normalized)

        event.listen(engine, "before_cursor_execute", record)
        try:
            deleted = client.request("DELETE", f"/api/workspace/folders/{folder['id']}", json={
                "expectedVersion": folder["version"],
            })
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert deleted.status_code == 422
        assert (provider.list_calls, provider_search_calls) == provider_calls_before_delete
        assert not workspace_dml, f"configured mount delete emitted Workspace DML: {workspace_dml}"
        assert os.environ["DP_CATALOG_MOUNTS"] == mount_config

        after = client.get(f"/api/workspace/resources/container:{folder['id']}")
        assert after.status_code == 200, after.text
        assert_delete_fenced(after.json()["resource"])


def test_external_container_overlay_anchor_is_fenced_hidden_and_replay_safe(
        workspace_scope, monkeypatch):
    """A provider container lends a local destination without ever gaining write authority."""
    root = metadb.local_workspace_root()
    provider = _WorkspaceFixtureProvider()
    mount_id = f"overlay-{uuid.uuid4().hex}"
    resources = [
        CatalogResource(placement_id="container-a", kind="container", name="Provider folder"),
        CatalogResource(
            placement_id="provider-child", dataset_id="provider-child", kind="dataset",
            name="Provider child", parent_placement_id="container-a",
            uri="file:///provider-child.parquet"),
    ]
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": root["id"],
    }]))

    created_ids: list[str] = []
    binding_ids: list[str] = []
    principal_ids: list[str] = []
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/workspace/containers/{root['id']}", params={"limit": 50})
            assert response.status_code == 200, response.text
            page = response.json()
            remote = next(
                (item for item in page["items"] if item.get("resourceId") == "container-a"),
                None,
            )
            cursor = page["nextCursor"]
            while remote is None and cursor is not None:
                response = client.get(
                    f"/api/workspace/containers/{root['id']}",
                    params={"limit": 50, "cursor": cursor},
                )
                assert response.status_code == 200, response.text
                page = response.json()
                remote = next(
                    (item for item in page["items"]
                     if item.get("resourceId") == "container-a"),
                    None,
                )
                cursor = page["nextCursor"]
            assert remote is not None
            binding_ids.append(remote["bindingId"])
            capability = remote["localPlacement"]
            assert remote["providerMutation"] is False
            assert capability == {
                "writable": True, "canCreateCanvas": True, "canMoveCanvas": True,
                "containerId": capability["containerId"],
                "containerVersion": capability["containerVersion"], "recoveryState": "ready",
            }
            anchor_id = capability["containerId"]

            # Moving an existing Canvas remains a local placement operation. Editors retain the
            # same authority; a user without an owner/editor role cannot use the anchor as a bypass.
            source_placement = metadb.workspace_create_placement(
                root["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
                name="Shared Canvas")
            editor_id, viewer_id = f"overlay-editor-{uuid.uuid4().hex}", f"overlay-viewer-{uuid.uuid4().hex}"
            principal_ids.extend([editor_id, viewer_id])
            with metadb.session() as session:
                session.add_all([
                    metadb.User(id=editor_id, name="Overlay editor"),
                    metadb.User(id=viewer_id, name="Overlay viewer"),
                ])
                # Flush the principals before the ID-only share row. SQLite does not enforce this
                # foreign-key ordering by default, while PostgreSQL correctly rejects the reverse.
                session.flush()
                session.add(metadb.CanvasShare(
                    canvas_id=workspace_scope["canvas_id"], user_id=editor_id, role="editor"))
            editor_move = client.put(
                f"/api/workspace/placements/{source_placement['id']}/canvas",
                headers={"X-DP-User": editor_id}, json={
                    "containerId": anchor_id,
                    "expectedContainerVersion": capability["containerVersion"],
                    "expectedVersion": source_placement["version"],
                })
            assert editor_move.status_code == 200, editor_move.text
            editor_move_doc = editor_move.json()
            assert editor_move_doc["resource"]["parentId"] == remote["id"]
            assert editor_move_doc["container"]["id"] == remote["id"]
            assert editor_move_doc["container"]["name"] == remote["name"]
            assert editor_move_doc["container"]["localPlacement"] == capability
            denied_move = client.put(
                f"/api/workspace/placements/{source_placement['id']}/canvas",
                headers={"X-DP-User": viewer_id}, json={
                    "containerId": root["id"], "expectedContainerVersion": root["version"],
                    "expectedVersion": editor_move.json()["resource"]["version"],
                })
            assert denied_move.status_code == 403

            # The anchor is an opaque local placement target, never an ordinary local folder.
            assert client.get(f"/api/workspace/containers/{anchor_id}").status_code == 404
            search = client.get("/api/workspace/search", params={"q": "External overlay"})
            assert search.status_code == 200
            assert all(item["id"] != f"container:{anchor_id}"
                       for group in search.json()["groups"] for item in group["items"])

            request_id = str(uuid.uuid4())
            create_body = {
                "requestId": request_id,
                "containerId": anchor_id,
                "expectedContainerVersion": capability["containerVersion"],
                "name": "Local overlay canvas",
            }
            missing_request = client.post("/api/workspace/canvases", json={
                key: value for key, value in create_body.items() if key != "requestId"
            })
            assert missing_request.status_code == 422
            assert "requires a client requestId" in missing_request.json()["detail"]
            created = client.post("/api/workspace/canvases", json=create_body)
            assert created.status_code == 200, created.text
            created_doc = created.json()
            created_ids.append(created_doc["id"])
            assert created_doc["resource"]["parentId"] == remote["id"]
            assert anchor_id not in json.dumps(created_doc)
            assert "External overlay" not in json.dumps(created_doc)
            hidden_search = client.get("/api/workspace/search", params={"q": "Local overlay canvas"})
            assert hidden_search.status_code == 200, hidden_search.text
            assert all(item["id"] != f"canvas:{created_doc['id']}"
                       for group in hidden_search.json()["groups"] for item in group["items"])
            deep_link = client.get(f"/api/workspace/resources/canvas:{created_doc['id']}")
            assert deep_link.status_code == 200, deep_link.text
            assert deep_link.json()["resource"]["parentId"] == remote["id"]
            assert deep_link.json()["ancestors"][-1]["id"] == remote["id"]

            second = client.post("/api/workspace/canvases", json={
                **create_body, "requestId": str(uuid.uuid4()), "name": "Second local overlay canvas",
            })
            assert second.status_code == 200, second.text
            created_ids.append(second.json()["id"])

            # The public external container owns a bounded, source-aware local-overlay-first page.
            # Local Canvas placements and provider children are neither duplicated nor omitted.
            composed_ids: list[str] = []
            remote_container_id = remote["id"].removeprefix("container:")
            composed = client.get(f"/api/workspace/containers/{remote_container_id}", params={"limit": 1})
            assert composed.status_code == 200, composed.text
            first_composed = composed.json()
            assert first_composed["sources"][0]["id"] == "local"
            assert first_composed["sources"][0]["completeness"] == "page"
            assert first_composed["items"][0]["id"] == f"canvas:{created_doc['id']}"
            cursor = first_composed["nextCursor"]
            while cursor:
                composed_ids.extend(item["id"] for item in composed.json()["items"])
                composed = client.get(f"/api/workspace/containers/{remote_container_id}", params={
                    "limit": 1, "cursor": cursor,
                })
                assert composed.status_code == 200, composed.text
                cursor = composed.json()["nextCursor"]
            composed_ids.extend(item["id"] for item in composed.json()["items"])
            assert composed_ids == [
                f"canvas:{created_doc['id']}", f"canvas:{second.json()['id']}",
                f"canvas:{workspace_scope['canvas_id']}",
                next(item["id"] for item in composed.json()["items"] if item["resourceId"] == "provider-child"),
            ]
            mismatched_cursor = client.get(f"/api/workspace/containers/{root['id']}", params={
                "limit": 1, "cursor": first_composed["nextCursor"],
            })
            assert mismatched_cursor.status_code == 422

            # A provider failure or a removed mount never hides the locally owned overlay page or
            # Canvas deep link.  The provider source remains explicitly partial/unavailable.
            normal_resolve = provider.resolve
            monkeypatch.setattr(provider, "resolve", lambda _mount, _resource_id: ProviderResourceResult(
                state="unavailable", reason="access revoked", failure="permission_lost"))
            unavailable_page = client.get(
                f"/api/workspace/containers/{remote_container_id}", params={"limit": 10})
            assert unavailable_page.status_code == 200, unavailable_page.text
            assert {item["id"] for item in unavailable_page.json()["items"]} == {
                f"canvas:{created_doc['id']}", f"canvas:{second.json()['id']}",
                f"canvas:{workspace_scope['canvas_id']}",
            }
            assert unavailable_page.json()["completeness"] == "partial"
            assert unavailable_page.json()["sources"][1]["referenceState"] == "permission_lost"
            assert client.get(f"/api/workspace/resources/canvas:{created_doc['id']}").status_code == 200
            monkeypatch.setattr(provider, "resolve", normal_resolve)
            assert client.get(f"/api/workspace/containers/{remote_container_id}").status_code == 200

            monkeypatch.delenv("DP_CATALOG_MOUNTS")
            removed_mount = client.get(f"/api/workspace/containers/{remote_container_id}")
            assert removed_mount.status_code == 200, removed_mount.text
            assert {item["id"] for item in removed_mount.json()["items"]} == {
                f"canvas:{created_doc['id']}", f"canvas:{second.json()['id']}",
                f"canvas:{workspace_scope['canvas_id']}",
            }
            assert removed_mount.json()["sources"][1]["kind"] == "configuration"
            assert client.get(f"/api/workspace/resources/canvas:{created_doc['id']}").status_code == 200
            monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
                "id": mount_id, "provider": "fixture", "containerId": root["id"],
            }]))

            bounded_resolve = workspace_providers.bounded_resolve
            monkeypatch.setattr(
                workspace_providers, "bounded_resolve",
                lambda provider_arg, mount, resource_id, **_kwargs: bounded_resolve(
                    provider_arg, mount, resource_id, timeout=0),
            )
            timed_out = client.get(f"/api/workspace/containers/{remote_container_id}")
            assert timed_out.status_code == 200, timed_out.text
            assert timed_out.json()["sources"][1]["referenceState"] == "offline"
            assert timed_out.json()["sources"][1]["error"] == "deadline exceeded"
            monkeypatch.setattr(workspace_providers, "bounded_resolve", bounded_resolve)

            normal_list_children = provider.list_children
            monkeypatch.setattr(provider, "list_children", lambda mount, parent_id, *, limit, cursor=None: (
                ProviderPage(state="partial", reason="provider returned a bounded subset")
                if parent_id == "container-a" else normal_list_children(
                    mount, parent_id, limit=limit, cursor=cursor)
            ))
            partial_page = client.get(f"/api/workspace/containers/{remote_container_id}")
            assert partial_page.status_code == 200, partial_page.text
            assert partial_page.json()["sources"][1]["completeness"] == "partial"
            assert partial_page.json()["sources"][1]["error"] == "provider returned a bounded subset"
            monkeypatch.setattr(provider, "list_children", normal_list_children)
            replay = client.post("/api/workspace/canvases", json=create_body)
            assert replay.status_code == 200, replay.text
            assert replay.json() == created_doc
            conflict = client.post("/api/workspace/canvases", json={
                **create_body, "name": "A different intent",
            })
            assert conflict.status_code == 422
            assert "different semantic request" in conflict.json()["detail"]

            moved_away = client.put(
                f"/api/workspace/placements/{source_placement['id']}/canvas", json={
                    "containerId": root["id"], "expectedContainerVersion": root["version"],
                    "expectedVersion": editor_move_doc["resource"]["version"],
                })
            assert moved_away.status_code == 200, moved_away.text
            moved_away_doc = moved_away.json()
            assert moved_away_doc["resource"]["parentId"] == f"container:{root['id']}"
            assert moved_away_doc["previousContainer"]["id"] == remote["id"]
            assert moved_away_doc["previousContainer"]["name"] == remote["name"]
            previous_capability = moved_away_doc["previousContainer"]["localPlacement"]
            assert previous_capability == capability
            undo = client.put(
                f"/api/workspace/placements/{source_placement['id']}/canvas", json={
                    "containerId": previous_capability["containerId"],
                    "expectedContainerVersion": previous_capability["containerVersion"],
                    "expectedVersion": moved_away_doc["resource"]["version"],
                })
            assert undo.status_code == 200, undo.text
            assert undo.json()["resource"]["parentId"] == remote["id"]
            assert undo.json()["container"]["id"] == remote["id"]

            # Both PostgreSQL (root-row lock) and SQLite (writer lock) serialize the final replay
            # lookup with the Canvas insert.  The replay table's composite primary key remains the
            # durable fence: a future lock regression rolls back the losing whole transaction.
            parallel_request = str(uuid.uuid4())
            parallel_intent = {
                "containerId": anchor_id,
                "expectedContainerVersion": capability["containerVersion"],
                "name": "Concurrent local overlay canvas",
                "datasetIds": [], "providerDatasetRefs": [], "transform": None,
            }
            start = threading.Barrier(3)
            parallel_results: list[dict] = []

            def submit_once() -> None:
                start.wait(timeout=5)
                parallel_results.append(metadb.workspace_create_canvas_action(
                    uid=metadb.DEFAULT_USER_ID, container_id=anchor_id,
                    expected_container_version=capability["containerVersion"],
                    name="Concurrent local overlay canvas", request_id=parallel_request,
                    request_intent=parallel_intent,
                ))

            workers = [threading.Thread(target=submit_once) for _ in range(2)]
            for worker in workers:
                worker.start()
            start.wait(timeout=5)
            for worker in workers:
                worker.join(timeout=5)
                assert not worker.is_alive()
            assert len(parallel_results) == 2
            assert parallel_results[0] == parallel_results[1]
            created_ids.append(parallel_results[0]["id"])
            with metadb.session() as session:
                assert len(list(session.scalars(select(metadb.Canvas.id).where(
                    metadb.Canvas.id == parallel_results[0]["id"])))) == 1

            ensure_calls: list[str] = []
            ensure_anchor = metadb.workspace_provider_ensure_overlay_anchor
            write_sessions = 0
            workspace_write_session = metadb._workspace_write_session

            def counted_ensure(binding_id: str) -> dict:
                ensure_calls.append(binding_id)
                return ensure_anchor(binding_id)

            @contextlib.contextmanager
            def counted_write_session():
                nonlocal write_sessions
                write_sessions += 1
                with workspace_write_session() as session:
                    yield session

            monkeypatch.setattr(metadb, "workspace_provider_ensure_overlay_anchor", counted_ensure)
            monkeypatch.setattr(metadb, "_workspace_write_session", counted_write_session)
            assert client.get(f"/api/workspace/resources/{remote['id']}").status_code == 200
            assert client.get(f"/api/workspace/resources/{remote['id']}").status_code == 200
            assert ensure_calls == []
            assert write_sessions == 0

            # Provider display rename/move only refreshes binding snapshots; the local placement
            # stays on this binding generation's anchor.
            parent = CatalogResource(
                placement_id="parent-a", kind="container", name="New provider parent")
            resources[:] = [
                parent,
                CatalogResource(
                    placement_id="container-a", kind="container", name="Renamed folder",
                    parent_placement_id="parent-a"),
            ]
            monkeypatch.setattr(
                provider, "ancestors",
                lambda _mount, resource_id: ProviderAncestors(items=[parent])
                if resource_id == "container-a" else ProviderAncestors(),
            )
            fresh_page = client.get(f"/api/workspace/containers/{remote_container_id}")
            assert fresh_page.status_code == 200, fresh_page.text
            assert fresh_page.json()["container"]["name"] == "Renamed folder"
            assert fresh_page.json()["container"]["parentId"].startswith("container:external.")
            fresh_parent_id = fresh_page.json()["container"]["parentId"]
            monkeypatch.setattr(
                provider, "ancestors",
                lambda _mount, _resource_id: ProviderAncestors(
                    state="partial", reason="ancestor read interrupted"),
            )
            partial_ancestors = client.get(f"/api/workspace/containers/{remote_container_id}")
            assert partial_ancestors.status_code == 200, partial_ancestors.text
            assert partial_ancestors.json()["container"]["parentId"] == fresh_parent_id
            assert partial_ancestors.json()["container"]["lastKnown"] is True
            assert partial_ancestors.json()["sources"][1]["completeness"] == "partial"
            assert partial_ancestors.json()["sources"][1]["error"] == "ancestor read interrupted"
            monkeypatch.setattr(
                provider, "ancestors",
                lambda _mount, resource_id: ProviderAncestors(items=[parent])
                if resource_id == "container-a" else ProviderAncestors(),
            )
            renamed = client.get(f"/api/workspace/resources/{remote['id']}")
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["resource"]["name"] == "Renamed folder"
            assert renamed.json()["resource"]["localPlacement"]["containerId"] == anchor_id
            metadb.engine().dispose()
            assert metadb.workspace_provider_overlay_anchor(remote["bindingId"])["containerId"] == anchor_id
            assert client.get(f"/api/canvas/{created_doc['id']}").status_code == 200
            renamed_deep_link = client.get(f"/api/workspace/resources/canvas:{created_doc['id']}")
            assert renamed_deep_link.status_code == 200, renamed_deep_link.text
            assert renamed_deep_link.json()["ancestors"][-1]["name"] == "Renamed folder"

            # A delete/recreate with the same provider ID is terminally detached until explicit relink.
            resources[:] = []
            detached = client.get(f"/api/workspace/resources/{remote['id']}")
            assert detached.status_code == 200, detached.text
            assert detached.json()["resource"]["referenceState"] == "detached"
            detached_deep_link = client.get(f"/api/workspace/resources/canvas:{created_doc['id']}")
            assert detached_deep_link.status_code == 200, detached_deep_link.text
            assert detached_deep_link.json()["resource"]["parentId"] == remote["id"]
            assert detached_deep_link.json()["source"]["referenceState"] == "detached"

            blocked_sibling_id = f"detached-sibling-{uuid.uuid4().hex}"
            blocked_sibling = client.post(
                "/api/canvas",
                params={"besideCanvasId": created_doc["id"]},
                json={
                    "id": blocked_sibling_id,
                    "name": "Must not enter detached provider folder",
                    "version": 1,
                    "nodes": [],
                    "edges": [],
                },
            )
            assert blocked_sibling.status_code == 422, blocked_sibling.text
            with metadb.session() as session:
                assert session.get(metadb.Canvas, blocked_sibling_id) is None

            root_canvas_id = f"detached-move-{uuid.uuid4().hex}"
            root_canvas = client.post("/api/canvas", json={
                "id": root_canvas_id,
                "name": "Remain visible at root",
                "version": 1,
                "nodes": [],
                "edges": [],
            })
            assert root_canvas.status_code == 200, root_canvas.text
            created_ids.append(root_canvas_id)
            root_resource = client.get(
                f"/api/workspace/resources/canvas:{root_canvas_id}").json()["resource"]
            blocked_move = client.post("/api/workspace/batch", json={
                "action": "move",
                "items": [{
                    "placementId": root_resource["placementId"],
                    "expectedVersion": root_resource["version"],
                }],
                "containerId": anchor_id,
                "expectedContainerVersion": capability["containerVersion"],
            })
            assert blocked_move.status_code == 422, blocked_move.text
            still_at_root = client.get(
                f"/api/workspace/resources/canvas:{root_canvas_id}").json()["resource"]
            assert still_at_root["parentId"] == f"container:{root['id']}"

            resources[:] = [CatalogResource(
                placement_id="container-a", kind="container", name="Recreated folder")]
            still_detached = client.get(f"/api/workspace/resources/{remote['id']}")
            assert still_detached.json()["resource"]["referenceState"] == "detached"
            assert still_detached.json()["resource"]["localPlacement"]["containerId"] == anchor_id
            relinked = client.post(f"/api/workspace/resources/{remote['id']}/relink", json={
                "mountId": mount_id, "resourceId": "container-a",
            })
            assert relinked.status_code == 200, relinked.text
            replacement = relinked.json()["resource"]
            binding_ids.append(replacement["bindingId"])
            assert replacement["bindingId"] != remote["bindingId"]
            assert replacement["localPlacement"]["containerId"] != anchor_id
    finally:
        with metadb.session() as session:
            if metadb._is_sqlite_database():
                session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")
            anchors = list(session.scalars(select(metadb.WorkspaceExternalOverlayAnchor).where(
                metadb.WorkspaceExternalOverlayAnchor.mount_id == mount_id)))
            anchor_ids = [anchor.container_id for anchor in anchors]
            if anchor_ids:
                # A moved pre-existing Canvas is not test-owned. Restore its visible parent before
                # deleting the hidden container so this cleanup exercises real FK enforcement.
                session.execute(update(metadb.WorkspacePlacement).where(
                    metadb.WorkspacePlacement.container_id.in_(anchor_ids)).values(
                    container_id=root["id"], version=metadb.WorkspacePlacement.version + 1))
            if created_ids:
                placement_ids = list(session.scalars(select(metadb.WorkspacePlacement.id).where(
                    metadb.WorkspacePlacement.target_kind == "canvas",
                    metadb.WorkspacePlacement.target_id.in_(created_ids),
                )))
                if placement_ids:
                    session.execute(delete(metadb.WorkspacePlacement).where(
                        metadb.WorkspacePlacement.id.in_(placement_ids)))
                session.execute(delete(metadb.Canvas).where(metadb.Canvas.id.in_(created_ids)))
            session.execute(delete(metadb.WorkspaceExternalOverlayAnchor).where(
                metadb.WorkspaceExternalOverlayAnchor.mount_id == mount_id))
            for anchor in anchors:
                session.execute(delete(metadb.WorkspaceContainer).where(
                    metadb.WorkspaceContainer.id == anchor.container_id))
            if principal_ids:
                session.execute(delete(metadb.CanvasShare).where(
                    metadb.CanvasShare.user_id.in_(principal_ids)))
                session.execute(delete(metadb.User).where(metadb.User.id.in_(principal_ids)))


def test_workspace_degraded_container_binding_lazily_installs_anchor(workspace_scope, monkeypatch):
    """A pre-0034 cached binding gets a local capability even when its provider cannot activate."""
    root = metadb.local_workspace_root()
    mount_id = f"overlay-degraded-{uuid.uuid4().hex}"
    binding = metadb.workspace_provider_cache_resource(
        mount_id=mount_id, provider="fixture", container_id=root["id"],
        provider_placement_id="container-a",
        kind="container", name="Cached container")
    assert metadb.workspace_provider_overlay_anchor(binding["bindingId"]) is None
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": root["id"],
    }]))

    def unavailable_provider(_name):
        raise LookupError("adapter unavailable")

    monkeypatch.setattr(workspace_providers, "_load_provider", unavailable_provider)
    ref = f"container:{workspace_providers._external_identity(mount_id, 'container-a', binding['bindingId'])}"
    try:
        with TestClient(app) as client:
            ensure_calls: list[str] = []
            ensure_anchor = metadb.workspace_provider_ensure_overlay_anchor

            def counted_ensure(binding_id: str) -> dict:
                ensure_calls.append(binding_id)
                return ensure_anchor(binding_id)

            monkeypatch.setattr(metadb, "workspace_provider_ensure_overlay_anchor", counted_ensure)
            first_page = client.get(f"/api/workspace/containers/{ref.removeprefix('container:')}")
            assert first_page.status_code == 200, first_page.text
            assert ensure_calls == [binding["bindingId"]]
            second_page = client.get(f"/api/workspace/containers/{ref.removeprefix('container:')}")
            assert second_page.status_code == 200, second_page.text
            assert ensure_calls == [binding["bindingId"]]
            response = client.get(f"/api/workspace/resources/{ref}")
            assert response.status_code == 200, response.text
            resource = response.json()["resource"]
            assert resource["referenceState"] == "provider_error"
            assert resource["localPlacement"]["writable"] is True
            assert resource["localPlacement"]["recoveryState"] == "ready"
        anchor = metadb.workspace_provider_overlay_anchor(binding["bindingId"])
        assert anchor is not None
        assert (anchor["mountId"], anchor["resourceId"]) == (mount_id, "container-a")
    finally:
        with metadb.session() as session:
            anchor = session.get(metadb.WorkspaceExternalOverlayAnchor, binding["bindingId"])
            if anchor is not None:
                session.delete(anchor)
                session.delete(session.get(metadb.WorkspaceContainer, anchor.container_id))


def test_workspace_provider_reference_recovery_detach_and_explicit_relink(
        workspace_scope, monkeypatch):
    provider_credential = "must-not-be-cached"
    monkeypatch.setenv("DP_TEST_PROVIDER_CREDENTIAL", provider_credential)
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-repair")
    mount_id = f"repair-{token}"
    provider = _WorkspaceFixtureProvider()
    mode = {"failure": None}
    ancestor_partial = {"value": False}
    resolve_calls = 0
    normal_resolve = provider.resolve
    normal_ancestors = provider.ancestors

    def resolve(mount, resource_id):
        nonlocal resolve_calls
        assert mount.config["credential"] == provider_credential
        resolve_calls += 1
        failure = mode["failure"]
        if failure is None:
            return normal_resolve(mount, resource_id)
        return ProviderResourceResult(
            state="unavailable",
            reason={
                "offline": "provider offline",
                "permission_lost": "access revoked: must-not-be-cached",
                "not_found": "resource not found",
                "provider_error": "provider response invalid",
            }[failure],
            failure=failure,
        )

    monkeypatch.setattr(provider, "resolve", resolve)
    monkeypatch.setattr(provider, "ancestors", lambda mount, resource_id: (
        ProviderAncestors(state="partial", reason="ancestor read interrupted")
        if ancestor_partial["value"] else normal_ancestors(mount, resource_id)
    ))
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    mount_config = json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
        "config": {"credential": "env:DP_TEST_PROVIDER_CREDENTIAL"},
    }])
    monkeypatch.setenv("DP_CATALOG_MOUNTS", mount_config)

    with TestClient(app) as client:
        page = client.get(f"/api/workspace/containers/{folder['id']}").json()
        assert provider_credential not in json.dumps(page)
        resource = next(
            item for item in page["items"] if item.get("resourceId") == "dataset-a")
        stable_ref = resource["id"]
        binding_id = resource["bindingId"]

        current = client.get(f"/api/workspace/resources/{stable_ref}")
        assert current.status_code == 200, current.text
        assert current.json()["resource"]["referenceState"] == "current"

        # Operator configuration can disappear transiently. It must preserve the exact binding
        # without terminally fencing it so Retry converges after the same mount returns.
        monkeypatch.delenv("DP_CATALOG_MOUNTS")
        unconfigured = client.get(f"/api/workspace/resources/{stable_ref}")
        assert unconfigured.status_code == 200, unconfigured.text
        assert unconfigured.json()["resource"]["referenceState"] == "provider_error"
        assert unconfigured.json()["resource"]["bindingId"] == binding_id
        monkeypatch.setenv("DP_CATALOG_MOUNTS", mount_config)
        restored = client.get(f"/api/workspace/resources/{stable_ref}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["resource"]["referenceState"] == "current"
        assert restored.json()["resource"]["bindingId"] == binding_id

        ancestor_partial["value"] = True
        stale_path = client.get(f"/api/workspace/resources/{stable_ref}")
        assert stale_path.status_code == 200, stale_path.text
        assert stale_path.json()["resource"]["lastKnown"] is True
        assert stale_path.json()["source"]["completeness"] == "partial"
        ancestor_partial["value"] = False

        mode["failure"] = "offline"
        offline = client.get(f"/api/workspace/resources/{stable_ref}")
        assert offline.status_code == 200, offline.text
        offline_resource = offline.json()["resource"]
        assert offline_resource["id"] == stable_ref
        assert offline_resource["name"] == "shared"
        assert offline_resource["referenceState"] == "offline"
        assert offline_resource["lastKnown"] is True
        assert offline.json()["source"]["referenceState"] == "offline"

        # Retry re-resolves this exact binding and converges when the provider returns.
        mode["failure"] = None
        recovered = client.get(f"/api/workspace/resources/{stable_ref}")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["resource"]["referenceState"] == "current"
        assert recovered.json()["resource"]["bindingId"] == binding_id

        mode["failure"] = "permission_lost"
        denied = client.get(f"/api/workspace/resources/{stable_ref}")
        assert denied.json()["resource"]["referenceState"] == "permission_lost"
        with metadb.session() as session:
            persisted = session.get(metadb.WorkspaceProviderBinding, binding_id)
            assert persisted is not None
            assert persisted.last_error == "provider permission was lost"
        mode["failure"] = "provider_error"
        failed = client.get(f"/api/workspace/resources/{stable_ref}")
        assert failed.json()["resource"]["referenceState"] == "provider_error"

        mode["failure"] = "not_found"
        detached = client.get(f"/api/workspace/resources/{stable_ref}")
        assert detached.json()["resource"]["referenceState"] == "detached"
        assert detached.json()["resource"]["detached"] is True

        # Recreating the same name and provider ID cannot revive the terminal old binding.
        mode["failure"] = None
        calls_before = resolve_calls
        still_detached = client.get(f"/api/workspace/resources/{stable_ref}")
        assert still_detached.json()["resource"]["referenceState"] == "detached"
        assert resolve_calls == calls_before

        relinked = client.post(f"/api/workspace/resources/{stable_ref}/relink", json={
            "mountId": mount_id, "resourceId": "dataset-a",
        })
        assert relinked.status_code == 200, relinked.text
        fresh = relinked.json()["resource"]
        assert fresh["id"] != stable_ref
        assert fresh["bindingId"] != binding_id
        assert fresh["referenceState"] == "current"
        assert client.get(f"/api/workspace/resources/{stable_ref}").json()[
            "resource"]["referenceState"] == "detached"
        assert client.get(f"/api/workspace/resources/{fresh['id']}").json()[
            "resource"]["referenceState"] == "current"

    with metadb.session() as session:
        old = session.get(metadb.WorkspaceProviderBinding, binding_id)
        new = session.get(metadb.WorkspaceProviderBinding, fresh["bindingId"])
        assert old is not None and old.state == "detached" and old.active is False
        assert new is not None and new.relinked_from_id == binding_id and new.active is True
        serialized = json.dumps(metadb._workspace_provider_binding_doc(new), default=str)
        assert provider_credential not in serialized
        assert "uri" not in serialized.lower()


def test_external_overlay_browse_rejects_legacy_and_cross_layout_cursors(
        workspace_scope, monkeypatch):
    root = metadb.local_workspace_root()
    mount_id = f"overlay-cursor-{uuid.uuid4().hex}"
    provider = _WorkspaceFixtureProvider()
    resources = [
        CatalogResource(placement_id="container-a", kind="container", name="External folder"),
        CatalogResource(placement_id="one", dataset_id="one", kind="dataset", name="one",
                        parent_placement_id="container-a", uri="file:///one"),
        CatalogResource(placement_id="two", dataset_id="two", kind="dataset", name="two",
                        parent_placement_id="container-a", uri="file:///two"),
    ]
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": root["id"],
    }]))
    with TestClient(app) as client:
        response = client.get(
            f"/api/workspace/containers/{root['id']}", params={"limit": 50})
        assert response.status_code == 200, response.text
        root_page = response.json()
        remote = next(
            (item for item in root_page["items"] if item.get("resourceId") == "container-a"),
            None,
        )
        cursor = root_page["nextCursor"]
        while remote is None and cursor is not None:
            response = client.get(
                f"/api/workspace/containers/{root['id']}",
                params={"limit": 50, "cursor": cursor},
            )
            assert response.status_code == 200, response.text
            root_page = response.json()
            remote = next(
                (item for item in root_page["items"]
                 if item.get("resourceId") == "container-a"),
                None,
            )
            cursor = root_page["nextCursor"]
        assert remote is not None
        identity = remote["id"].removeprefix("container:")
        current = client.get(f"/api/workspace/containers/{identity}", params={"limit": 1})
        assert current.status_code == 200, current.text
        assert current.json()["nextCursor"] is not None
        fingerprint = workspace_providers._mount_fingerprint([
            workspace_providers._MountedProvider(
                CatalogMount(id=mount_id, provider="fixture", config={}), root["id"]),
        ], False)
        legacy = base64.urlsafe_b64encode(json.dumps(
            [1, identity, fingerprint, 0, "1", []], separators=(",", ":")).encode()
        ).decode().rstrip("=")
        legacy_response = client.get(f"/api/workspace/containers/{identity}", params={
            "limit": 1, "cursor": legacy,
        })
        assert legacy_response.status_code == 422
        cross_layout = workspace_providers._cursor_encode(
            "local-mounted", identity, fingerprint, 0, None, [])
        cross_response = client.get(f"/api/workspace/containers/{identity}", params={
            "limit": 1, "cursor": cross_layout,
        })
        assert cross_response.status_code == 422


def test_external_overlay_canvas_deep_link_refreshes_live_container_and_falls_back(
        workspace_scope, monkeypatch):
    root = metadb.local_workspace_root()
    mount_id = f"overlay-deep-link-{uuid.uuid4().hex}"
    provider = _WorkspaceFixtureProvider()
    resources = [CatalogResource(
        placement_id="container-a", kind="container", name="Original folder")]
    parent = CatalogResource(placement_id="parent-a", kind="container", name="Original parent")
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: resources)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": root["id"],
    }]))
    normal_resolve = provider.resolve
    normal_ancestors = provider.ancestors
    failure: str | None = None
    ancestor_partial = {"value": False}

    def resolve(mount, resource_id):
        if failure is None:
            return normal_resolve(mount, resource_id)
        return ProviderResourceResult(
            state="unavailable", reason=failure, failure=failure)

    def ancestors(mount, resource_id):
        if ancestor_partial["value"]:
            return ProviderAncestors(state="partial", reason="ancestor read interrupted")
        if resource_id == "container-a":
            return ProviderAncestors(items=[parent])
        return normal_ancestors(mount, resource_id)

    monkeypatch.setattr(provider, "resolve", resolve)
    monkeypatch.setattr(provider, "ancestors", ancestors)
    with TestClient(app) as client:
        response = client.get(
            f"/api/workspace/containers/{root['id']}", params={"limit": 50})
        assert response.status_code == 200, response.text
        root_page = response.json()
        remote = next(
            (item for item in root_page["items"] if item.get("resourceId") == "container-a"),
            None,
        )
        cursor = root_page["nextCursor"]
        while remote is None and cursor is not None:
            response = client.get(
                f"/api/workspace/containers/{root['id']}",
                params={"limit": 50, "cursor": cursor},
            )
            assert response.status_code == 200, response.text
            root_page = response.json()
            remote = next(
                (item for item in root_page["items"]
                 if item.get("resourceId") == "container-a"),
                None,
            )
            cursor = root_page["nextCursor"]
        assert remote is not None
        created = client.post("/api/workspace/canvases", json={
            "requestId": str(uuid.uuid4()),
            "containerId": remote["localPlacement"]["containerId"],
            "expectedContainerVersion": remote["localPlacement"]["containerVersion"],
            "name": "Deep link overlay canvas",
        })
        assert created.status_code == 200, created.text
        canvas_ref = f"canvas:{created.json()['id']}"

        # Do not browse or resolve the external parent again: the deep link itself refreshes it.
        resources[:] = [
            parent,
            CatalogResource(
                placement_id="container-a", kind="container", name="Renamed folder",
                parent_placement_id="parent-a"),
        ]
        renamed = client.get(f"/api/workspace/resources/{canvas_ref}")
        assert renamed.status_code == 200, renamed.text
        assert [item["name"] for item in renamed.json()["ancestors"][-2:]] == [
            "Original parent", "Renamed folder"]
        assert renamed.json()["source"]["completeness"] == "complete"

        for state in ("offline", "permission_lost"):
            failure = state
            degraded = client.get(f"/api/workspace/resources/{canvas_ref}")
            assert degraded.status_code == 200, degraded.text
            assert degraded.json()["resource"]["parentId"] == remote["id"]
            assert degraded.json()["source"]["completeness"] == "unavailable"
            assert degraded.json()["source"]["referenceState"] == state
            failure = None
            assert client.get(f"/api/workspace/resources/{canvas_ref}").json()[
                "source"]["referenceState"] == "current"

        ancestor_partial["value"] = True
        partial = client.get(f"/api/workspace/resources/{canvas_ref}")
        assert partial.status_code == 200, partial.text
        assert partial.json()["source"]["completeness"] == "partial"
        assert partial.json()["ancestors"][-1]["name"] == "Renamed folder"
        ancestor_partial["value"] = False

        resources[:] = []
        detached = client.get(f"/api/workspace/resources/{canvas_ref}")
        assert detached.status_code == 200, detached.text
        assert detached.json()["source"]["referenceState"] == "detached"


def test_workspace_search_groups_sources_preserves_duplicates_and_reports_partial_truth(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    local_match = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-shared")
    provider = _WorkspaceFixtureProvider()
    slow_search_started = threading.Event()
    slow_search_released = threading.Event()
    slow_search_finished = threading.Event()
    provider_search = provider.search

    def controlled_search(mount, query, *, limit, cursor=None):
        if mount.id != "a-slow":
            return provider_search(mount, query, limit=limit, cursor=cursor)
        slow_search_started.set()
        if not slow_search_released.wait(timeout=5):
            raise AssertionError("test did not release the slow provider search")
        try:
            return provider_search(mount, query, limit=limit, cursor=cursor)
        finally:
            slow_search_finished.set()

    monkeypatch.setattr(provider, "search", controlled_search)
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    bounded = workspace_providers.bounded_search

    def search_with_controlled_timeout(provider_arg, mount, *args, **kwargs):
        kwargs.pop("timeout", None)
        timeout = 0.001 if mount.id == "a-slow" else 1.0
        return bounded(provider_arg, mount, *args, **kwargs, timeout=timeout)

    monkeypatch.setattr(
        workspace_providers, "bounded_search",
        search_with_controlled_timeout,
    )
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {"id": "a-slow", "provider": "fixture"},
        {"id": "b-partial", "provider": "fixture"},
        {"id": "c-first", "provider": "fixture"},
        {"id": "d-second", "provider": "fixture"},
        {"id": "e-unsupported", "provider": "fixture"},
        {"id": "f-overlimit", "provider": "fixture"},
        {"id": "g-stuck", "provider": "fixture"},
    ]))

    with TestClient(app) as client:
        try:
            response = client.get("/api/workspace/search", params={"q": "shared", "limit": 1})
            assert slow_search_started.wait(timeout=1)
            assert not slow_search_finished.is_set()
        finally:
            slow_search_released.set()
        assert slow_search_finished.wait(timeout=1)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["query"] == "shared"
        assert page["completeness"] == "partial"
        assert page["hasMore"] is True
        groups = {group["source"]["id"]: group for group in page["groups"]}
        assert groups["local"]["source"]["completeness"] in {"complete", "page"}
        assert groups["local"]["source"]["freshness"] == "current"
        assert groups["local"]["source"]["searchMode"] == "native"
        assert groups["local"]["items"]
        assert groups["mount:a-slow"]["source"]["completeness"] == "unavailable"
        assert groups["mount:a-slow"]["source"]["error"] == "deadline exceeded"
        assert groups["mount:b-partial"]["source"]["freshness"] == "stale"
        assert groups["mount:b-partial"]["source"]["completeness"] == "partial"
        assert groups["mount:e-unsupported"]["source"]["searchMode"] == "unsupported"
        assert groups["mount:e-unsupported"]["source"]["completeness"] == "unsupported"
        assert groups["mount:f-overlimit"]["source"]["completeness"] == "unavailable"
        assert groups["mount:f-overlimit"]["source"]["error"] == "provider search result is invalid"

        found: list[dict] = [
            item for group in page["groups"] for item in group["items"]
        ]
        cursor = page["nextCursor"]
        while cursor:
            continued = client.get("/api/workspace/search", params={
                "q": "shared", "limit": 1, "cursor": cursor,
            })
            assert continued.status_code == 200, continued.text
            document = continued.json()
            assert document["completeness"] == "partial"
            found.extend(item for group in document["groups"] for item in group["items"])
            cursor = document["nextCursor"]
        duplicates = [item for item in found if item["name"] == "shared"]
        assert f"container:{local_match['id']}" in {item["id"] for item in found}
        assert {item["mountId"] for item in duplicates if item.get("resourceId") == "dataset-a"} == {
            "c-first", "d-second",
        }
        assert len({item["id"] for item in duplicates}) == len(duplicates)
        final_groups = {group["source"]["id"]: group for group in document["groups"]}
        assert final_groups["mount:g-stuck"]["source"]["completeness"] == "unavailable"
        assert final_groups["mount:g-stuck"]["source"]["error"] == "provider search result is invalid"

        mismatched = client.get("/api/workspace/search", params={
            "q": "different", "limit": 1, "cursor": page["nextCursor"],
        })
        assert mismatched.status_code == 422
    time.sleep(0.03)


def test_workspace_provider_deadlines_keep_browse_fast_and_explicit_actions_bounded(
        workspace_scope, monkeypatch):
    provider = _WorkspaceFixtureProvider()
    root = metadb.local_workspace_root()
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-provider-deadlines")
    observed: dict[str, list[float]] = {
        "list": [], "resolve": [], "ancestors": [], "search": [], "detail": [],
    }

    def capture(name, bounded):
        def invoke(*args, **kwargs):
            observed[name].append(kwargs["timeout"])
            return bounded(*args, **kwargs)
        return invoke

    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setattr(
        workspace_providers, "bounded_list_children",
        capture("list", workspace_providers.bounded_list_children))
    monkeypatch.setattr(
        workspace_providers, "bounded_resolve",
        capture("resolve", workspace_providers.bounded_resolve))
    monkeypatch.setattr(
        workspace_providers, "bounded_ancestors",
        capture("ancestors", workspace_providers.bounded_ancestors))
    monkeypatch.setattr(
        workspace_providers, "bounded_search",
        capture("search", workspace_providers.bounded_search))
    monkeypatch.setattr(
        workspace_providers, "bounded_dataset_detail",
        capture("detail", workspace_providers.bounded_dataset_detail))
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {
            "id": "deadline-fixture",
            "provider": "fixture",
            "containerId": folder["id"],
        },
    ]))

    page = workspace_providers.browse(folder["id"], uid=metadb.DEFAULT_USER_ID, limit=100)
    resource_ref = next(
        item["id"] for item in page["items"] if item.get("resourceId") == "dataset-a")
    assert workspace_providers._PASSIVE_PROVIDER_READ_TIMEOUT_SECONDS == 10.0
    assert observed["list"] == [workspace_providers._PASSIVE_PROVIDER_READ_TIMEOUT_SECONDS]

    workspace_providers.search("shared", uid=metadb.DEFAULT_USER_ID)
    workspace_providers.resolve(resource_ref, uid=metadb.DEFAULT_USER_ID)
    workspace_providers.provider_dataset_source(
        resource_ref, uid=metadb.DEFAULT_USER_ID, resolve_physical=lambda _uri: object())
    workspace_providers.relink(
        resource_ref, uid=metadb.DEFAULT_USER_ID,
        mount_id="deadline-fixture", resource_id="dataset-a")

    expected = workspace_providers._INTERACTIVE_PROVIDER_READ_TIMEOUT_SECONDS
    assert expected == 20.0
    assert observed["search"] == [expected]
    assert observed["resolve"] and all(timeout == expected for timeout in observed["resolve"])
    assert observed["ancestors"] and all(timeout == expected for timeout in observed["ancestors"])
    assert observed["detail"] == [expected]


def test_workspace_search_finds_local_kinds_with_stable_identity_and_bounded_pages(
        workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    name = f"workspace-{token}-needle"
    container = metadb.workspace_create_container(root["id"], name)
    placement = metadb.workspace_create_placement(
        container["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"], name=name)
    try:
        with TestClient(app) as client:
            first = client.get("/api/workspace/search", params={"q": name, "limit": 1})
            assert first.status_code == 200, first.text
            page = first.json()
            assert page["completeness"] == "page"
            assert [group["source"]["id"] for group in page["groups"]] == ["local"]
            assert page["groups"][0]["items"][0]["id"] == (
                f"canvas:{workspace_scope['canvas_id']}")
            second = client.get("/api/workspace/search", params={
                "q": name, "limit": 1, "cursor": page["nextCursor"],
            })
            assert second.status_code == 200, second.text
            assert second.json()["completeness"] == "complete"
            assert second.json()["groups"][0]["items"][0]["id"] == f"container:{container['id']}"
    finally:
        metadb.workspace_delete_placement(placement["id"], expected_version=placement["version"])
        metadb.workspace_delete_container(container["id"], expected_version=container["version"])


def test_workspace_search_reports_browse_timestamps_and_orders_by_recency(workspace_scope):
    """Near-duplicate names are only separable when search carries the dates browse already shows."""
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    prefix = f"recency-{token}"
    root = metadb.local_workspace_root()
    now = datetime.datetime.now(datetime.timezone.utc)
    canvas_at = now - datetime.timedelta(days=1)
    view_at = now - datetime.timedelta(days=2)
    dataset_at = now - datetime.timedelta(days=3)

    uri = f"file:///{prefix}.parquet"
    metadb.catalog_upsert_entry(uri, f"{prefix} dataset", {
        "id": f"tbl_{prefix}", "name": f"{prefix} dataset", "uri": uri, "version": "v1"})
    dataset_id = metadb.workspace_builtin_dataset_identity(uri)
    canvas_placement = metadb.workspace_create_placement(
        root["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"{prefix} canvas")
    container = metadb.workspace_create_container(root["id"], f"{prefix} folder")
    view_id = uuid.uuid4().hex
    with metadb.session() as session:
        session.add(metadb.DatasetView(
            id=view_id, owner_id=metadb.DEFAULT_USER_ID, submission_id=view_id,
            request_sha256="0" * 64, definition_sha256="1" * 64,
            definition_doc="{}", created_at=view_at))
        session.add(metadb.WorkspacePlacement(
            container_id=root["id"], target_kind="dataset_view", target_id=view_id,
            name=f"{prefix} view"))
        session.execute(update(metadb.Canvas).where(
            metadb.Canvas.id == workspace_scope["canvas_id"]).values(updated_at=canvas_at))
        session.execute(update(metadb.CatalogEntry).where(
            metadb.CatalogEntry.registration_id == dataset_id).values(updated_at=dataset_at))
    try:
        found = metadb.workspace_search(prefix, uid=metadb.DEFAULT_USER_ID, limit=25)["items"]
        assert [item["name"] for item in found] == [
            f"{prefix} canvas", f"{prefix} view", f"{prefix} dataset", f"{prefix} folder"]

        browsed: dict[str, dict] = {}
        cursor = None
        for _page in range(20):
            page = metadb.workspace_browse(
                root["id"], uid=metadb.DEFAULT_USER_ID, limit=50, cursor=cursor,
                sort="updated", order="desc")
            browsed.update({item["id"]: item for item in page["items"]})
            cursor = page["nextCursor"]
            if cursor is None:
                break
        assert browsed[f"container:{container['id']}"].get("updatedAt") is None
        for item in found:
            assert item.get("updatedAt") == browsed[item["id"]].get("updatedAt")
        assert [datetime.datetime.fromisoformat(item["updatedAt"]) for item in found[:3]] == [
            canvas_at, view_at, dataset_at]
        assert found[3].get("updatedAt") is None
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_id.in_([view_id, dataset_id])))
            session.execute(delete(metadb.DatasetView).where(metadb.DatasetView.id == view_id))
        metadb.workspace_delete_placement(
            canvas_placement["id"], expected_version=canvas_placement["version"])
        metadb.workspace_delete_container(
            container["id"], expected_version=container["version"])
        metadb.catalog_delete_entry(uri)


def test_workspace_search_recency_pages_stay_ordered_and_complete(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    prefix = f"paging-{token}"
    root = metadb.local_workspace_root()
    now = datetime.datetime.now(datetime.timezone.utc)
    canvas_ids = [f"{prefix}-{index}" for index in range(4)]
    with metadb.session() as session:
        for index, canvas_id in enumerate(canvas_ids):
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name=f"{prefix} {index}", version=1,
                doc=json.dumps({"id": canvas_id, "name": f"{prefix} {index}", "version": 1,
                                "nodes": [], "edges": []}),
                updated_at=now - datetime.timedelta(days=4 - index)))
            session.add(metadb.WorkspacePlacement(
                container_id=root["id"], target_kind="canvas", target_id=canvas_id,
                name=f"{prefix} {index}"))
    try:
        # Newest last by name, so a page that stayed alphabetical would invert this.
        expected = [f"{prefix} {index}" for index in reversed(range(4))]
        assert [item["name"] for item in metadb.workspace_search(
            prefix, uid=metadb.DEFAULT_USER_ID, limit=25)["items"]] == expected

        walked: list[str] = []
        cursor = None
        for _page in range(10):
            page = metadb.workspace_search(
                prefix, uid=metadb.DEFAULT_USER_ID, limit=1, cursor=cursor)
            walked.extend(item["name"] for item in page["items"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
        assert walked == expected
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_id.in_(canvas_ids)))
            session.execute(delete(metadb.Canvas).where(metadb.Canvas.id.in_(canvas_ids)))


def test_workspace_create_and_explore_are_atomic_stable_and_allow_duplicate_names(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-actions")
    created_ids: list[str] = []
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = metadb.engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        with TestClient(app) as client:
            for _ in range(2):
                response = client.post("/api/workspace/canvases", json={
                    "containerId": folder["id"],
                    "expectedContainerVersion": folder["version"],
                    "name": "Duplicate exploration",
                    "datasetIds": [workspace_scope["dataset_id"]],
                })
                assert response.status_code == 200, response.text
                created = response.json()
                created_ids.append(created["id"])
                graph = client.get(f"/api/canvas/{created['id']}").json()
                assert len(graph["nodes"]) == 1
                assert graph["nodes"][0]["type"] == "source"
                assert created["nodeId"] == graph["nodes"][0]["id"]
            assert len(set(created_ids)) == 2

            with metadb.session() as session:
                for canvas_id in created_ids:
                    canvas = session.get(metadb.Canvas, canvas_id)
                    placement = session.scalar(select(metadb.WorkspacePlacement).where(
                        metadb.WorkspacePlacement.target_kind == "canvas",
                        metadb.WorkspacePlacement.target_id == canvas_id,
                    ))
                    doc = json.loads(canvas.doc)
                    assert placement.container_id == folder["id"]
                    assert placement.name == "Duplicate exploration"
                    assert doc["nodes"][0]["data"]["config"] == {
                            "uri": workspace_scope["uri"],
                            "tableId": f"tbl_{token}",
                            "registrationId": workspace_scope["dataset_id"],
                        }

            renamed = metadb.workspace_update_container(
                folder["id"], expected_version=folder["version"],
                name=f"workspace-{token}-actions-renamed")
            stale = client.post("/api/workspace/canvases", json={
                "containerId": folder["id"],
                "expectedContainerVersion": folder["version"],
                "name": "Must not exist",
            })
            assert stale.status_code == 409
            assert "expected version" in stale.json()["detail"]

            missing = client.post("/api/workspace/canvases", json={
                "containerId": folder["id"],
                "expectedContainerVersion": renamed["version"],
                "name": "Must not exist",
                "datasetIds": ["missing-stable-dataset"],
            })
            assert missing.status_code == 404
    finally:
        event.remove(engine, "before_cursor_execute", record)
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_id.in_(created_ids)))
            session.execute(delete(metadb.Canvas).where(metadb.Canvas.id.in_(created_ids)))

    assert not any(statement.lstrip().startswith(("insert", "update", "delete"))
                   and "catalog_" in statement for statement in statements)


def test_workspace_create_places_batch_sources_separately(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    second_uri = f"file:///workspace-create-second-{token}.parquet"
    metadb.catalog_upsert_entry(second_uri, "Second create dataset", {
        "id": f"tbl_create_second_{token}", "name": "Second create dataset",
        "uri": second_uri, "version": "v1",
    })
    second_dataset_id = metadb.workspace_builtin_dataset_identity(second_uri)
    root = metadb.local_workspace_root()
    created_id: str | None = None
    try:
        with TestClient(app) as client:
            response = client.post("/api/workspace/canvases", json={
                "containerId": root["id"],
                "expectedContainerVersion": root["version"],
                "name": "Batch placement",
                "datasetIds": [workspace_scope["dataset_id"], second_dataset_id],
            })
            assert response.status_code == 200, response.text
            created = response.json()
            created_id = created["id"]
            assert created["nodeId"] is None

        with metadb.session() as session:
            canvas = session.get(metadb.Canvas, created_id)
            assert canvas is not None
            doc = json.loads(canvas.doc)
            assert [node["type"] for node in doc["nodes"]] == ["source", "source"]
            assert [node["position"] for node in doc["nodes"]] == [
                {"x": 160, "y": 160},
                {"x": 440, "y": 160},
            ]
    finally:
        if created_id is not None:
            metadb.delete_canvas_cascade(created_id)
        metadb.catalog_delete_entry(second_uri)


def test_workspace_source_placement_avoids_sections_and_is_bounded_and_deterministic():
    def existing_nodes() -> list[dict]:
        return [
            {"id": "existing", "type": "write", "position": {"x": 160, "y": 160}},
            {"id": "section", "type": "section", "position": {"x": 440, "y": 160}},
            # Child coordinates are relative to the Section and must not reserve top-level space.
            {"id": "child", "type": "source", "parentId": "section",
             "position": {"x": 10_000, "y": 10_000}},
        ]

    def sources() -> list[dict]:
        return [
            {"id": f"source-{index}", "type": "source", "position": {"x": 0, "y": 0}}
            for index in range(50)
        ]

    first_nodes, first_sources = existing_nodes(), sources()
    metadb._workspace_place_sources(first_nodes, first_sources)
    second_nodes, second_sources = existing_nodes(), sources()
    metadb._workspace_place_sources(second_nodes, second_sources)

    positions = [source["position"] for source in first_sources]
    assert positions == [source["position"] for source in second_sources]
    assert positions[:4] == [
        {"x": 1000, "y": 160},
        {"x": 160, "y": 435},
        {"x": 440, "y": 435},
        {"x": 720, "y": 435},
    ]
    assert positions[-1] == {"x": 160, "y": 3735}
    assert len({(position["x"], position["y"]) for position in positions}) == 50


def test_workspace_add_deduplicates_provider_canonical_uri_without_binding_lookup(
        workspace_scope, monkeypatch):
    canvas_id = workspace_scope["canvas_id"]
    mount_id = f"dedup-{canvas_id.removeprefix('workspace-canvas-')}"
    binding_id = "a" * 32
    uri = workspace_providers.provider_dataset_uri(mount_id, binding_id)
    identity = f"workspace-provider:{uri.removeprefix('workspace-provider://')}"

    def provider_source(node_id: str, title: str, revision: str, placement: str) -> dict:
        return {
            "id": node_id, "type": "source", "position": {"x": 0, "y": 0},
            "data": {"title": title, "status": "latest", "config": {
                "uri": uri,
                "datasetRef": {"kind": "exact", "datasetId": identity, "revisionId": revision},
                "providerResourceRef": placement,
            }},
        }

    left = provider_source("left", "Left placement", "revision-1", "left-occurrence")
    right = provider_source("right", "Right placement", "revision-2", "right-occurrence")
    intent = {"canvasId": canvas_id, "expectedCanvasVersion": 7,
              "datasetIds": [], "providerDatasetRefs": ["left-occurrence"]}
    with metadb.session() as session:
        session.add(metadb.WorkspaceProviderDataset(
            mount_id=mount_id, provider_dataset_id="canonical-dataset", provider="fixture",
            source_binding_id=binding_id))
    request_ids = ["canonical-left", "canonical-right", "canonical-mixed"]
    try:
        first = metadb.workspace_add_datasets_action(
            uid=metadb.DEFAULT_USER_ID, canvas_id=canvas_id, expected_canvas_version=7,
            dataset_ids=[], provider_sources=[left], request_id="canonical-left", request_intent=intent)
        assert first["changed"] is True
        assert first["addedCount"] == 1
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspaceProviderDataset).where(
                metadb.WorkspaceProviderDataset.mount_id == mount_id))
            canvas = session.get(metadb.Canvas, canvas_id)
            assert canvas is not None
            before_doc = canvas.doc
            before_version = canvas.version
            snapshot_ids_before = list(session.scalars(select(metadb.CanvasVersion.id).where(
                metadb.CanvasVersion.canvas_id == canvas_id)))

        broadcasts: list[str] = []

        async def record_external_edit(changed_canvas_id: str) -> None:
            broadcasts.append(changed_canvas_id)

        monkeypatch.setattr("hub.main._broadcast_external_edit", record_external_edit)
        admission_calls: list[list[str]] = []

        def unavailable_provider_admission(refs: list[str], uid: str) -> list[dict]:
            del uid
            admission_calls.append(refs)
            raise workspace_providers.ProviderDatasetOffline("provider is unavailable")

        monkeypatch.setattr(
            "hub.routers.workspace._provider_dataset_sources", unavailable_provider_admission)
        with TestClient(app) as client:
            replay = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
                "providerDatasetRefs": ["left-occurrence"], "expectedCanvasVersion": 7,
                "requestId": "canonical-left",
            })
            assert replay.status_code == 200, replay.text
            assert replay.json() == first

            changed_intent = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
                "providerDatasetRefs": ["right-occurrence"], "expectedCanvasVersion": 7,
                "requestId": "canonical-left",
            })
            assert changed_intent.status_code == 422, changed_intent.text
            assert admission_calls == []

            def resolved_provider_source(refs: list[str], uid: str) -> list[dict]:
                assert refs == ["right-occurrence"]
                assert uid == metadb.DEFAULT_USER_ID
                return [right]

            monkeypatch.setattr(
                "hub.routers.workspace._provider_dataset_sources", resolved_provider_source)
            duplicate = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
                "providerDatasetRefs": ["right-occurrence"], "expectedCanvasVersion": 8,
                "requestId": "canonical-right",
            })
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json() == {
                "ok": True, "id": canvas_id, "version": 8, "changed": False,
                "alreadyPresent": True, "addedCount": 0,
            }

            with metadb.session() as session:
                canvas = session.get(metadb.Canvas, canvas_id)
                assert canvas is not None
                assert canvas.version == before_version
                assert canvas.doc == before_doc
                assert set(session.scalars(select(metadb.CanvasVersion.id).where(
                    metadb.CanvasVersion.canvas_id == canvas_id))) == set(snapshot_ids_before)
            assert broadcasts == []

            mixed = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
                "datasetIds": [workspace_scope["dataset_id"]],
                "providerDatasetRefs": ["right-occurrence"], "expectedCanvasVersion": 8,
                "requestId": "canonical-mixed",
            })
            assert mixed.status_code == 200, mixed.text
            assert mixed.json() == {
                "ok": True, "id": canvas_id, "version": 9, "changed": True,
                "alreadyPresent": True, "addedCount": 1,
            }

        with metadb.session() as session:
            canvas = session.get(metadb.Canvas, canvas_id)
            assert canvas is not None
            doc = json.loads(canvas.doc)
            assert canvas.version == doc["version"] == 9
            assert len(doc["nodes"]) == 2
            provider_nodes = [node for node in doc["nodes"]
                              if node["data"]["config"]["uri"] == uri]
            assert len(provider_nodes) == 1
            assert provider_nodes[0]["data"]["config"] == left["data"]["config"]
            assert any(node["data"]["config"]["uri"] == workspace_scope["uri"]
                       for node in doc["nodes"])
            snapshot_ids_after = list(session.scalars(select(metadb.CanvasVersion.id).where(
                metadb.CanvasVersion.canvas_id == canvas_id)))
            assert len(snapshot_ids_after) == len(snapshot_ids_before) + 1
        assert broadcasts == [canvas_id]
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspaceCanvasDatasetAddReplay).where(
                metadb.WorkspaceCanvasDatasetAddReplay.request_id.in_(request_ids)))
            session.execute(delete(metadb.WorkspaceProviderDataset).where(
                metadb.WorkspaceProviderDataset.mount_id == mount_id))
        metadb.delete_canvas_cascade(canvas_id)


def test_postgres_concurrent_workspace_dataset_adds_fence_same_canvas_version(workspace_scope):
    if metadb._is_sqlite_database():
        pytest.skip("PostgreSQL Canvas dataset-add CAS concurrency regression")
    canvas_id = workspace_scope["canvas_id"]
    token = canvas_id.removeprefix("workspace-canvas-")
    request_ids = [
        f"concurrent-workspace-add-left-{token}",
        f"concurrent-workspace-add-right-{token}"]
    start = threading.Barrier(3)
    results: list[dict | Exception] = []

    def add_dataset(request_id: str) -> None:
        intent = {
            "canvasId": canvas_id,
            "expectedCanvasVersion": 7,
            "datasetIds": [workspace_scope["dataset_id"]],
            "providerDatasetRefs": [],
        }
        start.wait(timeout=5)
        try:
            results.append(metadb.workspace_add_datasets_action(
                uid=metadb.DEFAULT_USER_ID,
                canvas_id=canvas_id,
                expected_canvas_version=7,
                dataset_ids=[workspace_scope["dataset_id"]],
                request_id=request_id,
                request_intent=intent,
            ))
        except Exception as exc:  # noqa: BLE001 - concurrent failures are asserted below
            results.append(exc)

    threads = [
        threading.Thread(target=add_dataset, args=(request_id,))
        for request_id in request_ids
    ]
    try:
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        winners = [result for result in results if isinstance(result, dict)]
        conflicts = [
            result for result in results
            if isinstance(result, metadb.WorkspaceVersionConflict)
        ]
        assert len(winners) == len(conflicts) == 1, results
        assert winners[0] == {
            "ok": True, "id": canvas_id, "version": 8, "changed": True,
            "alreadyPresent": False, "addedCount": 1,
        }
        assert "changed from expected version 7" in str(conflicts[0])
        with metadb.session() as session:
            canvas = session.get(metadb.Canvas, canvas_id)
            assert canvas is not None
            doc = json.loads(canvas.doc)
            assert canvas.version == doc["version"] == 8
            assert len(doc["nodes"]) == 1
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspaceCanvasDatasetAddReplay).where(
                metadb.WorkspaceCanvasDatasetAddReplay.request_id.in_(request_ids)))
        metadb.delete_canvas_cascade(canvas_id)


def test_workspace_add_uses_exact_canvas_and_dataset_versions(workspace_scope, monkeypatch):
    canvas_id = workspace_scope["canvas_id"]
    token = canvas_id.removeprefix("workspace-canvas-")
    second_uri = f"file:///workspace-second-{token}.parquet"
    metadb.catalog_upsert_entry(second_uri, "Second dataset", {
        "id": f"tbl_second_{token}", "name": "Second dataset", "uri": second_uri,
        "version": "v1", "columns": [],
    })
    second_dataset_id = metadb.workspace_builtin_dataset_identity(second_uri)
    selected_dataset_ids = [workspace_scope["dataset_id"], second_dataset_id]
    broadcasts: list[str] = []

    async def record_external_edit(changed_canvas_id: str) -> None:
        broadcasts.append(changed_canvas_id)

    monkeypatch.setattr("hub.main._broadcast_external_edit", record_external_edit)
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id)
        original_node = {
            "id": "write-existing", "type": "write", "position": {"x": 160, "y": 160},
            "data": {"title": "Durable output", "status": "draft", "config": {
                "destinationId": "local", "destinationPath": "kept/path", "name": "kept.parquet",
            }},
        }
        original_doc = {
            "id": canvas_id, "name": "Original canvas", "version": 7,
            "nodes": [original_node], "edges": [], "requirements": ["polars==1.42.1"],
        }
        canvas.doc = json.dumps(original_doc)

    with TestClient(app) as client:
        hub_main._collab_rooms[canvas_id] = {cast(WebSocket, object())}
        try:
            concurrent = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
                "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
                "requestId": "open-canvas-check",
            })
            assert concurrent.status_code == 409
            assert "currently open" in concurrent.json()["detail"]
        finally:
            hub_main._collab_rooms.pop(canvas_id, None)

        missing_request_id = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
        })
        assert missing_request_id.status_code == 422
        assert "requestId" in missing_request_id.text

        added = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
            "requestId": "workspace-add-replay",
        })
        assert added.status_code == 200, added.text
        assert added.json() == {
            "ok": True, "id": canvas_id, "version": 8, "changed": True,
            "alreadyPresent": False, "addedCount": 2,
        }

        replay = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
            "requestId": "workspace-add-replay",
        })
        assert replay.status_code == 200, replay.text
        assert replay.json() == added.json()

        changed_intent = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [workspace_scope["dataset_id"]], "expectedCanvasVersion": 7,
            "requestId": "workspace-add-replay",
        })
        assert changed_intent.status_code == 422

        stale = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
            "requestId": "stale-add-check",
        })
        assert stale.status_code == 409

        missing = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [workspace_scope["dataset_id"], "missing-stable-dataset"],
            "expectedCanvasVersion": 8, "requestId": "missing-add-check",
        })
        assert missing.status_code == 404

        duplicate = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [second_dataset_id, second_dataset_id], "expectedCanvasVersion": 8,
            "requestId": "duplicate-add-check",
        })
        assert duplicate.status_code == 422

        oversized = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [f"dataset-{index}" for index in range(51)],
            "expectedCanvasVersion": 8, "requestId": "oversized-add-check",
        })
        assert oversized.status_code == 422

    assert broadcasts == [canvas_id]

    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id)
        doc = json.loads(canvas.doc)
        assert canvas.version == doc["version"] == 8
        assert doc["requirements"] == original_doc["requirements"]
        assert doc["nodes"][0] == original_node
        assert len(doc["nodes"]) == 3
        assert doc["nodes"][1]["data"]["config"]["uri"] == workspace_scope["uri"]
        assert doc["nodes"][2]["data"]["config"]["uri"] == second_uri
        assert [node["position"] for node in doc["nodes"]] == [
            {"x": 160, "y": 160},
            {"x": 440, "y": 160},
            {"x": 720, "y": 160},
        ]
        snapshots = list(session.scalars(select(metadb.CanvasVersion).where(
            metadb.CanvasVersion.canvas_id == canvas_id)))
        assert any(snapshot.label == "before Workspace dataset add" for snapshot in snapshots)
    metadb.delete_canvas_cascade(canvas_id)
    metadb.catalog_delete_entry(second_uri)


def test_workspace_add_guard_blocks_new_collab_admission_until_edit_finishes():
    canvas_id = f"workspace-add-guard-{uuid.uuid4().hex}"

    async def exercise() -> None:
        edit_started = asyncio.Event()
        finish_edit = asyncio.Event()
        peer_joined = asyncio.Event()

        async def edit() -> None:
            async with hub_main._idle_collab_room_edit(canvas_id) as idle:
                assert idle
                edit_started.set()
                await finish_edit.wait()

        async def join() -> None:
            await edit_started.wait()
            lock = hub_main._retain_collab_room_lock(canvas_id)
            try:
                async with lock:
                    hub_main._collab_rooms.setdefault(canvas_id, set()).add(
                        cast(WebSocket, object()))
                    peer_joined.set()
            finally:
                hub_main._release_collab_room_lock(canvas_id, lock)

        edit_task = asyncio.create_task(edit())
        join_task = asyncio.create_task(join())
        await edit_started.wait()
        await asyncio.sleep(0)
        assert not peer_joined.is_set()
        finish_edit.set()
        await edit_task
        await asyncio.wait_for(join_task, timeout=1)

    try:
        asyncio.run(exercise())
    finally:
        hub_main._collab_rooms.pop(canvas_id, None)


def test_workspace_move_and_undo_change_only_canvas_placement(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    canvas_id = workspace_scope["canvas_id"]
    root = metadb.local_workspace_root()
    source = metadb.workspace_create_container(root["id"], f"workspace-{token}-move-source")
    destination = metadb.workspace_create_container(root["id"], f"workspace-{token}-move-destination")
    placement = metadb.workspace_create_placement(
        source["id"], target_kind="canvas", target_id=canvas_id,
        name=f"workspace-{token}-movable")
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id)
        canvas.visibility = "workspace"
        before = (canvas.doc, canvas.version, canvas.visibility, canvas.owner_id)

    with TestClient(app) as client:
        moved = client.put(f"/api/workspace/placements/{placement['id']}/canvas", json={
            "containerId": destination["id"],
            "expectedContainerVersion": destination["version"],
            "expectedVersion": placement["version"],
        })
        assert moved.status_code == 200, moved.text
        move_doc = moved.json()
        assert move_doc["resource"]["parentId"] == f"container:{destination['id']}"
        assert move_doc["previousContainer"]["id"] == f"container:{source['id']}"

        stale = client.put(f"/api/workspace/placements/{placement['id']}/canvas", json={
            "containerId": source["id"], "expectedContainerVersion": source["version"],
            "expectedVersion": placement["version"],
        })
        assert stale.status_code == 409

        undone = client.put(f"/api/workspace/placements/{placement['id']}/canvas", json={
            "containerId": source["id"], "expectedContainerVersion": source["version"],
            "expectedVersion": move_doc["resource"]["version"],
        })
        assert undone.status_code == 200, undone.text
        assert undone.json()["resource"]["parentId"] == f"container:{source['id']}"

        destination_next = metadb.workspace_update_container(
            destination["id"], expected_version=destination["version"],
            name=f"workspace-{token}-move-destination-renamed")
        stale_target = client.put(f"/api/workspace/placements/{placement['id']}/canvas", json={
            "containerId": destination["id"],
            "expectedContainerVersion": destination["version"],
            "expectedVersion": undone.json()["resource"]["version"],
        })
        assert stale_target.status_code == 409
        assert destination_next["version"] == destination["version"] + 1

    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id)
        current = session.get(metadb.WorkspacePlacement, placement["id"])
        assert (canvas.doc, canvas.version, canvas.visibility, canvas.owner_id) == before
        assert current.container_id == source["id"]


def test_workspace_server_query_sorts_and_filters_the_complete_result(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-server-query")
    for suffix in ("Zulu", "Alpha", "Middle"):
        metadb.workspace_create_container(
            folder["id"], f"workspace-{token}-{suffix}")
    older = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-older",
    )
    newer = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-newer",
    )
    try:
        with metadb.session() as session:
            session.get(metadb.Canvas, older["id"]).updated_at = datetime.datetime(
                2026, 1, 1, tzinfo=datetime.timezone.utc)
            session.get(metadb.Canvas, newer["id"]).updated_at = datetime.datetime(
                2026, 2, 1, tzinfo=datetime.timezone.utc)

        default_page = metadb.workspace_browse(
            folder["id"], uid=metadb.DEFAULT_USER_ID, limit=50)
        default_by_name = {item["name"]: item for item in default_page["items"]}
        assert default_by_name[f"workspace-{token}-older"]["updatedAt"] == (
            "2026-01-01T00:00:00+00:00")
        assert default_by_name[f"workspace-{token}-newer"]["updatedAt"] == (
            "2026-02-01T00:00:00+00:00")

        with TestClient(app) as client:
            names: list[str] = []
            cursor: str | None = None
            while True:
                params: list[tuple[str, str | int]] = [
                    ("limit", 1), ("sort", "name"), ("order", "desc"),
                    ("kind", "container"),
                ]
                if cursor is not None:
                    params.append(("cursor", cursor))
                response = client.get(
                    f"/api/workspace/containers/{folder['id']}", params=params)
                assert response.status_code == 200, response.text
                page = response.json()
                assert {item["kind"] for item in page["items"]} <= {"container"}
                names.extend(item["name"].removeprefix(f"workspace-{token}-")
                             for item in page["items"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
            assert names == ["Zulu", "Middle", "Alpha"]

            recent = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 1), ("sort", "updated"), ("order", "desc"),
                    ("kind", "canvas"),
                ])
            assert recent.status_code == 200, recent.text
            recent_page = recent.json()
            assert [item["name"] for item in recent_page["items"]] == [
                f"workspace-{token}-newer"]
            assert recent_page["items"][0]["updatedAt"].startswith(
                "2026-02-01T00:00:00")
            assert recent_page["items"][0]["updatedAt"].endswith(("Z", "+00:00"))
            older_page = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 1), ("sort", "updated"), ("order", "desc"),
                    ("kind", "canvas"), ("cursor", recent_page["nextCursor"]),
                ])
            assert older_page.status_code == 200, older_page.text
            assert [item["name"] for item in older_page.json()["items"]] == [
                f"workspace-{token}-older"]
            assert older_page.json()["items"][0]["updatedAt"].startswith(
                "2026-01-01T00:00:00")
            assert older_page.json()["items"][0]["updatedAt"].endswith(("Z", "+00:00"))

            mismatched = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("sort", "name"), ("kind", "canvas"),
                    ("cursor", recent_page["nextCursor"]),
                ])
            assert mismatched.status_code == 422
            assert "does not match" in mismatched.text

            mismatched_order = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("sort", "updated"), ("order", "asc"), ("kind", "canvas"),
                    ("cursor", recent_page["nextCursor"]),
                ])
            assert mismatched_order.status_code == 422
            assert "does not match" in mismatched_order.text

            mismatched_kinds = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("sort", "updated"), ("order", "desc"), ("kind", "dataset"),
                    ("cursor", recent_page["nextCursor"]),
                ])
            assert mismatched_kinds.status_code == 422
            assert "does not match" in mismatched_kinds.text

            mismatched_folder = client.get(
                f"/api/workspace/containers/{root['id']}", params=[
                    ("sort", "updated"), ("order", "desc"), ("kind", "canvas"),
                    ("cursor", recent_page["nextCursor"]),
                ])
            assert mismatched_folder.status_code == 422
            assert "does not match this folder" in mismatched_folder.text

            malformed = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("sort", "name"), ("kind", "canvas"),
                    ("cursor", "not-a-cursor"),
                ])
            assert malformed.status_code == 422
            assert "invalid Workspace query cursor" in malformed.text
    finally:
        metadb.delete_canvas_cascade(older["id"])
        metadb.delete_canvas_cascade(newer["id"])


def test_workspace_server_name_query_keyset_survives_insert_before_cursor(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-name-keyset")
    originals = [
        metadb.workspace_create_container(folder["id"], f"workspace-{token}-{suffix}")
        for suffix in ("BETA", "beta", "charlie")
    ]

    with TestClient(app) as client:
        first_response = client.get(
            f"/api/workspace/containers/{folder['id']}", params=[
                ("limit", 1), ("sort", "name"), ("order", "asc"),
                ("kind", "container"),
            ])
        assert first_response.status_code == 200, first_response.text
        first_page = first_response.json()
        assert first_page["nextCursor"] is not None

        inserted = metadb.workspace_create_container(
            folder["id"], f"workspace-{token}-aardvark")
        seen_ids = [first_page["items"][0]["id"]]
        cursor = first_page["nextCursor"]
        while cursor is not None:
            response = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 1), ("sort", "name"), ("order", "asc"),
                    ("kind", "container"), ("cursor", cursor),
                ])
            assert response.status_code == 200, response.text
            page = response.json()
            seen_ids.extend(item["id"] for item in page["items"])
            cursor = page["nextCursor"]

    original_ids = {f"container:{item['id']}" for item in originals}
    assert set(seen_ids) == original_ids
    assert len(seen_ids) == len(original_ids)
    assert f"container:{inserted['id']}" not in seen_ids


def test_workspace_server_updated_query_keyset_survives_insert_before_cursor(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-updated-keyset")
    originals = [
        metadb.workspace_create_canvas_action(
            uid=metadb.DEFAULT_USER_ID,
            container_id=folder["id"],
            expected_container_version=folder["version"],
            name=f"workspace-{token}-{suffix}",
        )
        for suffix in ("Zulu", "Alpha", "Beta", "Old")
    ]
    inserted = None
    try:
        timestamps = (
            datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        )
        with metadb.session() as session:
            for canvas, updated_at in zip(originals, timestamps, strict=True):
                session.get(metadb.Canvas, canvas["id"]).updated_at = updated_at

        with TestClient(app) as client:
            first_response = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 2), ("sort", "updated"), ("order", "desc"),
                    ("kind", "canvas"),
                ])
            assert first_response.status_code == 200, first_response.text
            first_page = first_response.json()
            assert [item["name"].removeprefix(f"workspace-{token}-")
                    for item in first_page["items"]] == ["Zulu", "Alpha"]
            assert first_page["nextCursor"] is not None

            inserted = metadb.workspace_create_canvas_action(
                uid=metadb.DEFAULT_USER_ID,
                container_id=folder["id"],
                expected_container_version=folder["version"],
                name=f"workspace-{token}-Newer",
            )
            with metadb.session() as session:
                session.get(metadb.Canvas, inserted["id"]).updated_at = datetime.datetime(
                    2026, 4, 1, tzinfo=datetime.timezone.utc)

            second_response = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 2), ("sort", "updated"), ("order", "desc"),
                    ("kind", "canvas"), ("cursor", first_page["nextCursor"]),
                ])
            assert second_response.status_code == 200, second_response.text
            second_page = second_response.json()
            assert [item["name"].removeprefix(f"workspace-{token}-")
                    for item in second_page["items"]] == ["Beta", "Old"]
            assert second_page["nextCursor"] is None

            for suffix in ("Null-Zulu", "Null-Alpha"):
                metadb.workspace_create_container(
                    folder["id"], f"workspace-{token}-{suffix}")
            all_names: list[str] = []
            cursor: str | None = None
            while True:
                params: list[tuple[str, str | int]] = [
                    ("limit", 2), ("sort", "updated"), ("order", "asc"),
                    ("kind", "canvas"), ("kind", "container"),
                ]
                if cursor is not None:
                    params.append(("cursor", cursor))
                response = client.get(
                    f"/api/workspace/containers/{folder['id']}", params=params)
                assert response.status_code == 200, response.text
                page = response.json()
                all_names.extend(
                    item["name"].removeprefix(f"workspace-{token}-")
                    for item in page["items"]
                )
                cursor = page["nextCursor"]
                if cursor is None:
                    break
            assert all_names == [
                "Old", "Alpha", "Beta", "Zulu", "Newer", "Null-Alpha", "Null-Zulu",
            ]
    finally:
        for canvas in originals:
            metadb.delete_canvas_cascade(canvas["id"])
        if inserted is not None:
            metadb.delete_canvas_cascade(inserted["id"])


def test_workspace_column_filters_combine_with_sort_and_bind_the_cursor(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-column-filters")
    metadb.workspace_create_container(folder["id"], f"workspace-{token}-sales folder")
    canvases = [metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-{suffix}",
    ) for suffix in ("sales report", "sales draft", "ops report")]
    try:
        with metadb.session() as session:
            for canvas, month in zip(canvases, (1, 2, 3), strict=True):
                session.get(metadb.Canvas, canvas["id"]).updated_at = datetime.datetime(
                    2026, month, 1, tzinfo=datetime.timezone.utc)
        with TestClient(app) as client:
            combined = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("kind", "canvas"), ("name", "SALES"),
                    ("updatedAfter", "2026-01-15T00:00:00Z"),
                    ("updatedBefore", "2026-12-31T00:00:00Z"),
                    ("sort", "updated"), ("order", "desc"),
                ])
            assert combined.status_code == 200, combined.text
            assert [item["name"] for item in combined.json()["items"]] == [
                f"workspace-{token}-sales draft"]

            # An updated range excludes rows with no timestamp, such as local Folders.
            unranged = client.get(
                f"/api/workspace/containers/{folder['id']}",
                params=[("name", f"{token} sales")])
            assert unranged.status_code == 200, unranged.text
            assert {item["name"].removeprefix(f"workspace-{token}-")
                    for item in unranged.json()["items"]} == {
                        "sales folder", "sales report", "sales draft"}

            first = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 1), ("kind", "canvas"), ("name", "sales"),
                    ("sort", "name"), ("order", "asc"),
                ])
            assert first.status_code == 200, first.text
            first_page = first.json()
            assert [item["name"] for item in first_page["items"]] == [
                f"workspace-{token}-sales draft"]
            assert first_page["nextCursor"] is not None

            second = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 1), ("kind", "canvas"), ("name", "sales"),
                    ("sort", "name"), ("order", "asc"),
                    ("cursor", first_page["nextCursor"]),
                ])
            assert second.status_code == 200, second.text
            assert [item["name"] for item in second.json()["items"]] == [
                f"workspace-{token}-sales report"]

            mismatched = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("limit", 1), ("kind", "canvas"), ("name", "report"),
                    ("sort", "name"), ("order", "asc"),
                    ("cursor", first_page["nextCursor"]),
                ])
            assert mismatched.status_code == 422
            assert "does not match" in mismatched.text

            inverted = client.get(
                f"/api/workspace/containers/{folder['id']}", params=[
                    ("updatedAfter", "2026-03-01T00:00:00Z"),
                    ("updatedBefore", "2026-01-01T00:00:00Z"),
                ])
            assert inverted.status_code == 422
            assert "updated-after" in inverted.text
    finally:
        for canvas in canvases:
            metadb.delete_canvas_cascade(canvas["id"])


def test_workspace_source_filter_is_capability_advertised_and_honest(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-source-filter")
    child = metadb.workspace_create_container(folder["id"], f"workspace-{token}-child")
    mount_id = f"srcf-{token}"
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))
    with TestClient(app) as client:
        local_only = client.get(
            f"/api/workspace/containers/{folder['id']}",
            params={"source": "local", "sourceId": "local"})
        assert local_only.status_code == 200, local_only.text
        page = local_only.json()
        assert [item["id"] for item in page["items"]] == [f"container:{child['id']}"]
        assert page["connectedSources"] == []
        assert page["queryCapabilities"]["filters"] == _local_filter_capabilities(mount_id)

        mount_only = client.get(
            f"/api/workspace/containers/{folder['id']}",
            params={"source": "local", "sourceId": f"mount:{mount_id}"})
        assert mount_only.status_code == 200, mount_only.text
        mount_page = mount_only.json()
        assert mount_page["items"] == []
        assert [item["mountId"] for item in mount_page["connectedSources"]] == [mount_id]
        assert mount_page["completeness"] == "complete"
        assert provider.list_calls == 0

        unknown = client.get(
            f"/api/workspace/containers/{folder['id']}",
            params={"source": "local", "sourceId": "mount:absent"})
        assert unknown.status_code == 422
        assert "does not match a source available in this folder" in unknown.text

        mixed = client.get(
            f"/api/workspace/containers/{folder['id']}",
            params={"name": "child"})
        assert mixed.status_code == 422
        assert "Sort and filters are available in local folders" in mixed.text

        provider_lens = client.get(
            "/api/workspace/containers/"
            f"{workspace_providers.mount_container_identity(mount_id)}",
            params={"source": "provider", "name": "shared"})
        assert provider_lens.status_code == 422
        assert "Workspace sort and filters are unavailable" in provider_lens.text


def test_workspace_search_filters_locally_and_labels_provider_sources(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-searchable")
    canvases = [metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=folder["id"],
        expected_container_version=folder["version"],
        name=f"workspace-{token}-{suffix}",
    ) for suffix in ("searchable early", "searchable late")]
    mount_id = f"search-{token}"
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))
    try:
        with metadb.session() as session:
            for canvas, month in zip(canvases, (1, 2), strict=True):
                session.get(metadb.Canvas, canvas["id"]).updated_at = datetime.datetime(
                    2026, month, 1, tzinfo=datetime.timezone.utc)
        with TestClient(app) as client:
            filtered = client.get("/api/workspace/search", params=[
                ("q", token), ("kind", "canvas"),
                ("updatedAfter", "2026-01-15T00:00:00Z"),
            ])
            assert filtered.status_code == 200, filtered.text
            page = filtered.json()
            local_group = next(
                group for group in page["groups"] if group["source"]["id"] == "local")
            assert [item["name"] for item in local_group["items"]] == [
                f"workspace-{token}-searchable late"]
            provider_group = next(
                group for group in page["groups"]
                if group["source"]["id"] == f"mount:{mount_id}")
            assert provider_group["items"] == []
            assert provider_group["source"]["completeness"] == "unsupported"
            assert provider_group["source"]["error"] == (
                "This connected source does not support Workspace filters.")
            assert page["completeness"] == "partial"

            local_scope = client.get("/api/workspace/search", params=[
                ("q", token), ("sourceId", "local")])
            assert local_scope.status_code == 200, local_scope.text
            assert [group["source"]["id"] for group in local_scope.json()["groups"]] == [
                "local"]

            unknown_scope = client.get("/api/workspace/search", params=[
                ("q", token), ("sourceId", "mount:absent")])
            assert unknown_scope.status_code == 422
            assert "does not match a configured Workspace source" in unknown_scope.text

            first = client.get("/api/workspace/search", params=[
                ("q", token), ("kind", "canvas"), ("limit", 1)])
            assert first.status_code == 200, first.text
            cursor = first.json()["nextCursor"]
            assert cursor is not None
            mismatched = client.get("/api/workspace/search", params=[
                ("q", token), ("kind", "dataset"), ("limit", 1), ("cursor", cursor)])
            assert mismatched.status_code == 422
            assert "invalid Workspace search cursor" in mismatched.text
    finally:
        for canvas in canvases:
            metadb.delete_canvas_cascade(canvas["id"])


def test_workspace_batch_move_and_delete_are_atomic(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    source = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-batch-source")
    destination = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-batch-destination")
    created = [metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID, container_id=source["id"],
        expected_container_version=source["version"],
        name=f"workspace-{token}-batch-{index}",
    ) for index in range(2)]
    resources = [item["resource"] for item in created]
    try:
        with TestClient(app) as client:
            stale_move = client.post("/api/workspace/batch", json={
                "action": "move",
                "items": [
                    {"placementId": resources[0]["placementId"],
                     "expectedVersion": resources[0]["version"]},
                    {"placementId": resources[1]["placementId"],
                     "expectedVersion": resources[1]["version"] + 1},
                ],
                "containerId": destination["id"],
                "expectedContainerVersion": destination["version"],
            })
            assert stale_move.status_code == 409
            with metadb.session() as session:
                assert {session.get(metadb.WorkspacePlacement, item["placementId"]).container_id
                        for item in resources} == {source["id"]}

            moved = client.post("/api/workspace/batch", json={
                "action": "move",
                "items": [{
                    "placementId": item["placementId"],
                    "expectedVersion": item["version"],
                } for item in resources],
                "containerId": destination["id"],
                "expectedContainerVersion": destination["version"],
            })
            assert moved.status_code == 200, moved.text
            moved_resources = moved.json()["items"]
            assert {item["parentId"] for item in moved_resources} == {
                f"container:{destination['id']}"}
            assert {item["canvasVersion"] for item in moved_resources} == {1}

            stale_delete = client.post("/api/workspace/batch", json={
                "action": "delete_canvases",
                "items": [
                    {"placementId": moved_resources[0]["placementId"],
                     "expectedVersion": moved_resources[0]["version"],
                     "expectedCanvasVersion": moved_resources[0]["canvasVersion"]},
                    {"placementId": moved_resources[1]["placementId"],
                     "expectedVersion": moved_resources[1]["version"] + 1,
                     "expectedCanvasVersion": moved_resources[1]["canvasVersion"]},
                ],
            })
            assert stale_delete.status_code == 409
            with metadb.session() as session:
                assert all(session.get(metadb.Canvas, item["id"]) is not None
                           for item in created)

            with metadb.session() as session:
                session.execute(update(metadb.Canvas).where(
                    metadb.Canvas.id == created[0]["id"],
                ).values(version=2))

            stale_canvas_delete = client.post("/api/workspace/batch", json={
                "action": "delete_canvases",
                "items": [{
                    "placementId": item["placementId"],
                    "expectedVersion": item["version"],
                    "expectedCanvasVersion": item["canvasVersion"],
                } for item in moved_resources],
            })
            assert stale_canvas_delete.status_code == 409
            with metadb.session() as session:
                assert all(session.get(metadb.Canvas, item["id"]) is not None
                           for item in created)

            deleted = client.post("/api/workspace/batch", json={
                "action": "delete_canvases",
                "items": [
                    {
                        "placementId": moved_resources[0]["placementId"],
                        "expectedVersion": moved_resources[0]["version"],
                        "expectedCanvasVersion": 2,
                    },
                    {
                        "placementId": moved_resources[1]["placementId"],
                        "expectedVersion": moved_resources[1]["version"],
                        "expectedCanvasVersion": moved_resources[1]["canvasVersion"],
                    },
                ],
            })
            assert deleted.status_code == 200, deleted.text
            assert set(deleted.json()["deletedCanvasIds"]) == {
                item["id"] for item in created}
            with metadb.session() as session:
                assert all(session.get(metadb.Canvas, item["id"]) is None
                           for item in created)
    finally:
        for item in created:
            metadb.delete_canvas_cascade(item["id"])


def test_workspace_provider_query_rejects_false_page_local_sort(
        workspace_scope, monkeypatch):
    folder = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"],
        f"workspace-{workspace_scope['canvas_id'].removeprefix('workspace-canvas-')}-mount")
    monkeypatch.setattr(
        workspace_providers, "is_configured_mount_container",
        lambda container_id: container_id == folder["id"],
    )
    with TestClient(app) as client:
        response = client.get(
            f"/api/workspace/containers/{folder['id']}", params={"sort": "name"})
    assert response.status_code == 422
    assert "controls its own order" in response.text


def test_workspace_connected_source_lens_is_separate_bounded_and_resolvable(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-connected-parent")
    metadb.workspace_create_container(folder["id"], f"workspace-{token}-z-local")
    metadb.workspace_create_container(folder["id"], f"workspace-{token}-a-local")
    mount_id = f"lens/{token}"
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {
            "id": mount_id,
            "provider": "fixture",
            "containerId": folder["id"],
            "config": {"endpoint": "must-not-cross-the-workspace-api"},
        },
        {"id": "invalid-without-provider"},
    ]))

    with TestClient(app) as client:
        local_response = client.get(
            f"/api/workspace/containers/{folder['id']}",
            params=[
                ("source", "local"),
                ("sort", "name"),
                ("order", "desc"),
                ("kind", "container"),
                ("limit", "10"),
            ],
        )
        assert local_response.status_code == 200, local_response.text
        local_page = local_response.json()
        assert provider.list_calls == 0
        assert [item["name"] for item in local_page["items"]] == [
            f"workspace-{token}-z-local", f"workspace-{token}-a-local"]
        assert local_page["queryCapabilities"] == {
            "sort": ["name", "updated", "opened"], "kindFilter": True,
            "filters": _local_filter_capabilities(mount_id), "reason": None}
        assert [source["kind"] for source in local_page["sources"]] == [
            "local", "configuration"]
        assert local_page["sources"][1]["error"] == "catalog mount configuration is invalid"
        assert len(local_page["connectedSources"]) == 1
        connected = local_page["connectedSources"][0]
        assert connected == {
            **connected,
            "id": f"container:{workspace_providers.mount_container_identity(mount_id)}",
            "kind": "container",
            "name": mount_id,
            "parentId": f"container:{folder['id']}",
            "source": "provider",
            "mountId": mount_id,
            "provider": "fixture",
            "referenceState": "current",
            "providerMutation": False,
            "canCreateFolder": False,
            "canRenameFolder": False,
            "canDeleteFolder": False,
        }
        assert connected["localPlacement"] is None
        assert "must-not-cross-the-workspace-api" not in local_response.text

        identity = connected["id"].removeprefix("container:")
        first = client.get(
            f"/api/workspace/containers/{identity}",
            params={"source": "provider", "limit": 1},
        )
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert provider.list_calls == 1
        assert first_page["container"] == connected
        assert first_page["queryCapabilities"]["sort"] == []
        assert first_page["queryCapabilities"]["kindFilter"] is False
        assert first_page["queryCapabilities"]["filters"] == []
        assert first_page["queryCapabilities"]["reason"] == (
            "Sorting and filters aren't available for this source."
        )
        assert first_page["items"][0]["parentId"] == connected["id"]
        assert first_page["nextCursor"].startswith("provider.")
        assert first_page["nextCursor"] != "1"

        second = client.get(
            f"/api/workspace/containers/{identity}",
            params={"source": "provider", "limit": 1,
                    "cursor": first_page["nextCursor"]},
        )
        assert second.status_code == 200, second.text
        assert provider.list_calls == 2
        assert second.json()["hasMore"] is False

        rejected = client.get(
            f"/api/workspace/containers/{identity}",
            params={"source": "provider", "sort": "name"},
        )
        assert rejected.status_code == 422
        assert provider.list_calls == 2

        resolved = client.get(f"/api/workspace/resources/{connected['id']}")
        assert resolved.status_code == 200, resolved.text
        assert provider.list_calls == 2
        resolution = resolved.json()
        assert resolution["resource"] == connected
        assert [item["id"] for item in resolution["ancestors"]] == [
            f"container:{root['id']}", f"container:{folder['id']}"]
        assert resolution["source"] == {
            "id": f"mount:{mount_id}",
            "kind": "provider",
            "completeness": "complete",
            "mountId": mount_id,
            "provider": "fixture",
            "error": None,
            "referenceState": None,
        }

        child = client.get(
            f"/api/workspace/resources/{first_page['items'][0]['id']}")
        assert child.status_code == 200, child.text
        assert [item["id"] for item in child.json()["ancestors"]] == [
            f"container:{root['id']}",
            f"container:{folder['id']}",
            connected["id"],
        ]


def test_workspace_source_lens_cursors_bind_container_mount_and_config(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    left = metadb.workspace_create_container(root["id"], f"workspace-{token}-cursor-left")
    right = metadb.workspace_create_container(root["id"], f"workspace-{token}-cursor-right")
    for parent, suffix in ((left, "a"), (left, "b"), (right, "a"), (right, "b")):
        metadb.workspace_create_container(parent["id"], f"workspace-{token}-{suffix}")
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)

    def configure(endpoint: str) -> None:
        monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
            {"id": "left-mount", "provider": "fixture", "containerId": left["id"],
             "config": {"endpoint": endpoint}},
            {"id": "right-mount", "provider": "fixture", "containerId": right["id"]},
        ]))

    configure("v1")
    left_mount = workspace_providers.mount_container_identity("left-mount")
    right_mount = workspace_providers.mount_container_identity("right-mount")
    with TestClient(app) as client:
        local_first = client.get(
            f"/api/workspace/containers/{left['id']}",
            params={"source": "local", "limit": 1},
        )
        assert local_first.status_code == 200, local_first.text
        local_cursor = local_first.json()["nextCursor"]
        assert local_cursor.startswith("local.")
        cross_folder = client.get(
            f"/api/workspace/containers/{right['id']}",
            params={"source": "local", "limit": 1, "cursor": local_cursor},
        )
        assert cross_folder.status_code == 422
        cross_lens = client.get(
            f"/api/workspace/containers/{left_mount}",
            params={"source": "provider", "limit": 1, "cursor": local_cursor},
        )
        assert cross_lens.status_code == 422

        provider_first = client.get(
            f"/api/workspace/containers/{left_mount}",
            params={"source": "provider", "limit": 1},
        )
        assert provider_first.status_code == 200, provider_first.text
        provider_cursor = provider_first.json()["nextCursor"]
        assert provider_cursor.startswith("provider.")
        provider_into_local = client.get(
            f"/api/workspace/containers/{left['id']}",
            params={"source": "local", "limit": 1, "cursor": provider_cursor},
        )
        assert provider_into_local.status_code == 422
        cross_mount = client.get(
            f"/api/workspace/containers/{right_mount}",
            params={"source": "provider", "limit": 1, "cursor": provider_cursor},
        )
        assert cross_mount.status_code == 422

        configure("v2")
        stale_config = client.get(
            f"/api/workspace/containers/{left_mount}",
            params={"source": "provider", "limit": 1, "cursor": provider_cursor},
        )
        assert stale_config.status_code == 422


def test_workspace_connected_source_final_page_reconciles_removed_bindings(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-reconcile-parent")
    mount_id = f"reconcile-{token}"

    class Provider(_WorkspaceFixtureProvider):
        include_dataset = True

        def _resources(self, current_mount_id: str) -> list[CatalogResource]:
            resources = super()._resources(current_mount_id)
            return resources if self.include_dataset else [
                item for item in resources if item.placement_id != "dataset-a"]

    provider = Provider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))
    identity = workspace_providers.mount_container_identity(mount_id)

    with TestClient(app) as client:
        initial = client.get(
            f"/api/workspace/containers/{identity}",
            params={"source": "provider", "limit": 100},
        )
        assert initial.status_code == 200, initial.text
        removed = next(item for item in initial.json()["items"]
                       if item.get("providerPlacementId") == "dataset-a")
        provider.include_dataset = False
        refreshed = client.get(
            f"/api/workspace/containers/{identity}",
            params={"source": "provider", "limit": 100},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert all(item.get("providerPlacementId") != "dataset-a"
                   for item in refreshed.json()["items"])
        resolved = client.get(f"/api/workspace/resources/{removed['id']}")
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resource"]["referenceState"] == "detached"


def test_workspace_normal_local_browse_keeps_sort_and_filter_capabilities(workspace_scope):
    root = metadb.local_workspace_root()
    with TestClient(app) as client:
        response = client.get(
            f"/api/workspace/containers/{root['id']}", params={"limit": 1})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["connectedSources"] == []
    assert page["queryCapabilities"] == {
        "sort": ["name", "updated", "opened"], "kindFilter": True,
        "filters": _local_filter_capabilities(), "reason": None}


def test_workspace_default_browse_mixes_local_and_connected_source_roots(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    folder = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-mixed-root")
    local_child = metadb.workspace_create_container(
        folder["id"], f"workspace-{token}-local-child")
    mount_id = f"mixed-{token}"
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))

    with TestClient(app) as client:
        response = client.get(
            f"/api/workspace/containers/{folder['id']}", params={"limit": 100})

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["container"]["id"] == f"container:{folder['id']}"
    assert page["connectedSources"] == []
    assert any(item["id"] == f"container:{local_child['id']}" for item in page["items"])
    assert any(item.get("source") == "provider" for item in page["items"])
    assert not any(item["id"].startswith("container:mount.") for item in page["items"])
    assert page["queryCapabilities"]["sort"] == []
    assert page["queryCapabilities"]["kindFilter"] is False
    assert page["queryCapabilities"]["filters"] == []
    assert page["queryCapabilities"]["reason"] == (
        "Sorting and filters aren't available in this view."
    )


def test_workspace_provider_delete_is_capability_driven_and_detaches_cached_dataset(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    folder = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-mutable-source")
    mount_id = f"mutable-{token}"

    class Provider(_WorkspaceFixtureProvider):
        def __init__(self):
            super().__init__()
            self.deleted: list[tuple[str, str]] = []

        def _resources(self, current_mount_id):
            deleted_ids = {dataset_id for dataset_id, _actor in self.deleted}
            return [
                item for item in _WorkspaceFixtureProvider._resources(current_mount_id)
                if item.dataset_id not in deleted_ids
            ]

        def capabilities(self, _mount):
            return ProviderCapabilities(search=True, delete_dataset=True)

        def can_delete_dataset(self, _mount, dataset_id):
            return dataset_id == "dataset-a"

        def delete_dataset(self, _mount, dataset_id, *, actor):
            self.deleted.append((dataset_id, actor))
            return True

    provider = Provider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([{
        "id": mount_id, "provider": "fixture", "containerId": folder["id"],
    }]))

    with TestClient(app) as client:
        page = client.get(
            f"/api/workspace/containers/{folder['id']}", params={"limit": 100})
        assert page.status_code == 200, page.text
        dataset = next(
            item for item in page.json()["items"]
            if item.get("providerDatasetId") == "dataset-a")
        provider_folder = next(
            item for item in page.json()["items"]
            if item.get("providerPlacementId") == "container-a")
        nested = next(
            item for item in client.get(
                f"/api/workspace/containers/{provider_folder['id'].removeprefix('container:')}",
                params={"limit": 100},
            ).json()["items"]
            if item.get("providerDatasetId") == "nested-dataset")
        assert dataset["providerMutation"] is True
        assert nested["providerMutation"] is False

        removed = client.delete(
            f"/api/workspace/resources/{dataset['id']}/provider-dataset")
        assert removed.status_code == 200, removed.text
        assert removed.json() == {"ok": True, "removedFrom": mount_id}

        resolved = client.get(f"/api/workspace/resources/{dataset['id']}")

    assert provider.deleted == [("dataset-a", metadb.DEFAULT_USER_ID)]
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resource"]["referenceState"] == "detached"
    assert resolved.json()["resource"]["canonicalReferenceState"] == "detached"


def test_workspace_api_unicode_keyset_has_no_duplicates_or_loss(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    folder = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-unicode-page")
    for name in ("A", "Z", "İ"):
        metadb.workspace_create_container(folder["id"], f"workspace-{token}-{name}")

    names: list[str] = []
    cursor = None
    with TestClient(app) as client:
        while True:
            response = client.get(f"/api/workspace/containers/{folder['id']}", params={
                "limit": 1, **({"cursor": cursor} if cursor else {}),
            })
            assert response.status_code == 200
            page = response.json()
            names.extend(item["name"].removeprefix(f"workspace-{token}-")
                         for item in page["items"])
            cursor = page["nextCursor"]
            if cursor is None:
                break

        invalid = client.get(f"/api/workspace/containers/{folder['id']}", params={
            "cursor": metadb._workspace_cursor_encode(2**63, 0, "A", "invalid"),
        })

    assert names == ["A", "Z", "İ"]
    assert invalid.status_code == 422


def test_normal_local_lifecycles_materialize_root_workspace_resources():
    token = uuid.uuid4().hex
    canvas_id = f"workspace-lifecycle-canvas-{token}"
    uri = f"file:///workspace-lifecycle-{token}.parquet"
    dataset_id = ""

    def root_resources(client: TestClient, wanted: set[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        cursor: str | None = None
        seen: set[str] = set()
        for _page_number in range(100):
            params: dict[str, str | int] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get(
                f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}", params=params)
            assert response.status_code == 200
            page = response.json()
            found.update((item["id"], item) for item in page["items"] if item["id"] in wanted)
            if wanted <= found.keys() or not page["hasMore"]:
                return found
            cursor = page["nextCursor"]
            assert cursor is not None and cursor not in seen
            seen.add(cursor)
        raise AssertionError("Workspace root pagination did not terminate")

    try:
        with TestClient(app) as client:
            created = client.post("/api/canvas", json={
                "id": canvas_id, "name": "Lifecycle canvas", "version": 1,
                "nodes": [], "edges": [],
            })
            assert created.status_code == 200 and created.json()["created"] is True
            metadb.catalog_upsert_entry(uri, "Lifecycle dataset", {
                "id": f"tbl_{token}", "name": "Lifecycle dataset", "uri": uri,
                "version": "v1", "columns": [],
            })
            dataset_id = metadb.workspace_builtin_dataset_identity(uri)
            resource_ids = {f"canvas:{canvas_id}", f"dataset:{dataset_id}"}
            resources = root_resources(client, resource_ids)
            assert {identity: item["name"] for identity, item in resources.items()} == {
                f"canvas:{canvas_id}": "Lifecycle canvas",
                f"dataset:{dataset_id}": "Lifecycle dataset",
            }
            renamed = client.put(f"/api/canvas/{canvas_id}", json={
                "id": canvas_id, "name": "Renamed lifecycle canvas", "version": 2,
                "nodes": [], "edges": [],
            })
            assert renamed.status_code == 200
            metadb.catalog_set_metadata(
                uri, "", None, None, [], name="Renamed lifecycle dataset")
            renamed_resources = root_resources(client, resource_ids)
            assert {identity: item["name"] for identity, item in renamed_resources.items()} == {
                f"canvas:{canvas_id}": "Renamed lifecycle canvas",
                f"dataset:{dataset_id}": "Renamed lifecycle dataset",
            }
    finally:
        metadb.delete_canvas_cascade(canvas_id)
        metadb.catalog_delete_entry(uri)
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_id.in_([canvas_id, dataset_id])))


def test_canvas_create_can_preserve_the_open_canvas_workspace_folder(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    source_canvas_id = workspace_scope["canvas_id"]
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(
        root["id"], f"workspace-{token}-sibling-create")
    metadb.workspace_create_placement(
        folder["id"],
        target_kind="canvas",
        target_id=source_canvas_id,
        name=f"workspace-{token}-source",
    )
    created_id = f"workspace-{token}-sibling"
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/canvas",
                params={"besideCanvasId": source_canvas_id},
                json={
                    "id": created_id,
                    "name": f"workspace-{token}-example",
                    "version": 1,
                    "nodes": [],
                    "edges": [],
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["created"] is True
            resolved = client.get(f"/api/workspace/resources/canvas:{created_id}")
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["resource"]["parentId"] == f"container:{folder['id']}"

            replay_after_source_loss = client.post(
                "/api/canvas",
                params={"besideCanvasId": f"workspace-{token}-deleted-source"},
                json={
                    "id": created_id,
                    "name": f"workspace-{token}-example",
                    "version": 1,
                    "nodes": [],
                    "edges": [],
                },
            )
            assert replay_after_source_loss.status_code == 200, replay_after_source_loss.text
            assert replay_after_source_loss.json()["created"] is False
            replayed_resource = client.get(f"/api/workspace/resources/canvas:{created_id}")
            assert replayed_resource.status_code == 200, replayed_resource.text
            assert replayed_resource.json()["resource"]["parentId"] == f"container:{folder['id']}"

            other = client.post("/api/users", json={"name": "Sibling create outsider"}).json()["id"]
            denied_id = f"workspace-{token}-denied-sibling"
            denied = client.post(
                "/api/canvas",
                params={"besideCanvasId": source_canvas_id},
                headers={"X-DP-User": other},
                json={
                    "id": denied_id,
                    "name": f"workspace-{token}-denied",
                    "version": 1,
                    "nodes": [],
                    "edges": [],
                },
            )
            assert denied.status_code == 404
            with metadb.session() as session:
                assert session.get(metadb.Canvas, denied_id) is None
    finally:
        metadb.delete_canvas_cascade(created_id)


def test_bulk_seed_materializes_workspace_placements():
    token = uuid.uuid4().hex
    uri = f"file:///workspace-bulk-seed-{token}.parquet"
    dataset_id = ""
    try:
        assert metadb.catalog_bulk_seed([{
            "uri": uri, "name": "Bulk seed dataset",
            "doc": {"id": f"tbl_{token}", "name": "Bulk seed dataset", "uri": uri,
                    "version": "v1", "columns": []},
        }]) == 1
        dataset_id = metadb.workspace_builtin_dataset_identity(uri)
        with metadb.session() as session:
            placement = session.scalar(select(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_kind == "dataset",
                metadb.WorkspacePlacement.target_id == dataset_id,
            ))
            assert placement is not None
            assert placement.container_id == metadb.LOCAL_WORKSPACE_ROOT_ID
            assert placement.name == "Bulk seed dataset"
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_id == dataset_id))
            session.execute(delete(metadb.CatalogEntry).where(metadb.CatalogEntry.uri == uri))


def test_migration_backfills_existing_local_resources_without_moving_placements():
    token = uuid.uuid4().hex
    canvas_id, dataset_id = f"workspace-backfill-canvas-{token}", uuid.uuid4().hex
    uri = f"file:///workspace-backfill-{token}.parquet"
    try:
        with metadb.session() as session:
            session.add(metadb.Canvas(
                id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="Backfill canvas", version=1,
                doc=json.dumps({"id": canvas_id, "name": "Backfill canvas", "version": 1,
                                "nodes": [], "edges": []}),
            ))
            session.add(metadb.CatalogEntry(
                uri=uri, registration_id=dataset_id, name="Backfill dataset",
                doc=json.dumps({"id": f"tbl_{token}", "name": "Backfill dataset", "uri": uri,
                                "version": "v1", "columns": []}),
            ))
        metadb.migrate_db()
        with metadb.session() as session:
            placements = {(row.target_kind, row.target_id, row.container_id) for row in session.scalars(
                select(metadb.WorkspacePlacement).where(
                    metadb.WorkspacePlacement.target_id.in_([canvas_id, dataset_id])))}
        assert placements == {
            ("canvas", canvas_id, metadb.LOCAL_WORKSPACE_ROOT_ID),
            ("dataset", dataset_id, metadb.LOCAL_WORKSPACE_ROOT_ID),
        }
    finally:
        with metadb.session() as session:
            session.execute(delete(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_id.in_([canvas_id, dataset_id])))
            session.execute(delete(metadb.Canvas).where(metadb.Canvas.id == canvas_id))
            session.execute(delete(metadb.CatalogEntry).where(metadb.CatalogEntry.uri == uri))


def test_concurrent_container_cas_has_one_winner(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    container = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-concurrent-cas")
    start = threading.Barrier(3)
    results = []

    def update_ordinal(ordinal):
        start.wait(timeout=5)
        try:
            results.append(metadb.workspace_update_container(
                container["id"], expected_version=container["version"], ordinal=ordinal))
        except Exception as exc:  # noqa: BLE001 - assert the public conflict type below
            results.append(exc)

    threads = [threading.Thread(target=update_ordinal, args=(ordinal,)) for ordinal in (1, 2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    winners = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, metadb.WorkspaceVersionConflict)]
    assert len(winners) == len(conflicts) == 1
    assert winners[0]["version"] == container["version"] + 1


def test_sqlite_workspace_write_reserves_writer_before_hierarchy_reads(workspace_scope):
    if not metadb._is_sqlite_database():
        pytest.skip("SQLite writer-reservation regression")
    database = metadb._database_url().database
    assert database

    with metadb._workspace_write_session():
        with sqlite3.connect(database, timeout=0) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
