"""Local Workspace storage invariants, independent of browse and UI delivery."""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import threading
import time
import uuid
from typing import cast

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from hub import db, main as hub_main, metadb, workspace_providers
from hub.catalog_provider import (
    CatalogResource,
    ProviderAncestors,
    ProviderCapabilities,
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
        detached = session.get(metadb.WorkspacePlacement, replacement_placement["id"])
        assert detached is not None and detached.target_id == workspace_scope["canvas_id"]
    metadb.workspace_delete_placement(
        replacement_placement["id"], expected_version=replacement_placement["version"])


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
            CatalogResource(id="container-a", kind="container", name="shared"),
            CatalogResource(
                id="dataset-a", kind="dataset", name="shared",
                uri=f"file:///{mount_id}.parquet"),
            CatalogResource(
                id="nested-dataset", kind="dataset", name="nested",
                parent_id="container-a", uri=f"file:///{mount_id}-nested.parquet"),
        ]

    def list_children(self, mount, parent_id, *, limit, cursor=None):
        self.list_calls += 1
        if mount.id == "a-slow":
            time.sleep(0.02)
        resources = sorted(
            (item for item in self._resources(mount.id) if item.parent_id == parent_id),
            key=lambda item: (item.name, item.id),
        )
        start = int(cursor or 0)
        items = resources[start:start + limit]
        if mount.id == "b-partial":
            return ProviderPage(
                state="partial", items=items[:1], reason="provider returned a bounded subset")
        next_cursor = str(start + len(items)) if start + len(items) < len(resources) else None
        return ProviderPage(items=items, next_cursor=next_cursor)

    def resolve(self, mount, resource_id):
        item = next((item for item in self._resources(mount.id) if item.id == resource_id), None)
        return ProviderResourceResult(item=item) if item else ProviderResourceResult(
            state="unavailable", reason="resource not found", failure="not_found")

    def ancestors(self, mount, resource_id):
        if resource_id == "nested-dataset":
            return ProviderAncestors(items=[self._resources(mount.id)[0]])
        return ProviderAncestors()

    def dataset_detail(self, mount, resource_id):
        return self.resolve(mount, resource_id)

    def capabilities(self, _mount):
        return ProviderCapabilities(search=_mount.id != "e-unsupported")

    def search(self, mount, query, *, limit, cursor=None):
        if mount.id == "a-slow":
            time.sleep(0.02)
        tokens = query.casefold().split()
        resources = sorted(
            (item for item in self._resources(mount.id)
             if all(token in item.name.casefold() for token in tokens)),
            key=lambda item: (item.name.casefold(), item.kind, item.id),
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


class _ExactFixtureAdapter:
    name = "fixture-exact"

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
        return {"revision_id": revision_id, "columns": [], "preview_table": relation.limit(1).arrow()}


def test_provider_dataset_use_exact_preview_and_mutable_run_rejection(
        workspace_scope, tmp_path, monkeypatch):
    path = tmp_path / "provider.csv"
    path.write_text("value\n1\n2\n")
    provider = _WorkspaceFixtureProvider()
    resource = CatalogResource(
        id="dataset-a", kind="dataset", name="Provider observations", uri=str(path))
    monkeypatch.setattr(provider, "_resources", lambda _mount_id: [resource])
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
        source = graph["nodes"][0]
        config = source["data"]["config"]
        assert config["uri"].startswith("workspace-provider://")
        assert str(path) not in json.dumps(graph)
        assert config["providerReadMode"] == "exact"
        assert config["datasetRef"]["revisionId"] == "fixture-revision-1"
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
        assert denied.json()["detail"] == "permission to read an exact input revision was lost"
        exact_adapter.failure = "offline"
        offline = client.post("/api/run", json={
            "graph": graph, "targetNodeId": source["id"],
            "inputManifest": preview.json()["inputManifest"],
        })
        assert offline.status_code == 503
        assert offline.json()["detail"] == "exact input revision provider is offline"
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
            "provider dataset binding is unavailable; install or restore a compatible provider "
            "and dataset adapter")
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
        assert sanitized.json()["detail"] == "provider dataset is offline"
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
            "provider dataset binding is unavailable; install or restore a compatible provider "
            "and dataset adapter")
        assert "secret" not in incompatible.text
        incompatible_add = client.post(
            f"/api/workspace/canvases/{created.json()['id']}/datasets",
            json={
                "expectedCanvasVersion": graph["version"],
                "providerDatasetRefs": [provider_resource["id"]],
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

        monkeypatch.setattr(deps, "chosen_backend", lambda _uid=None: "local-out-of-core")
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
        monkeypatch.setattr(deps, "chosen_backend", lambda _uid=None: "kernel")
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
            "workspace-provider://00000000000000000000000000000000")
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
        assert "mutable-only" in rejected.json()["detail"]
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
        assert "mutable-only" in fabricated.json()["detail"]
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
        assert gone_use.json()["detail"] == "provider dataset was deleted; relink it explicitly"
        gone_add = client.post(
            f"/api/workspace/canvases/{created.json()['id']}/datasets",
            json={
                "expectedCanvasVersion": graph["version"],
                "providerDatasetRefs": [provider_resource["id"]],
            },
        )
        assert gone_add.status_code == 410, gone_add.text
        assert gone_add.json()["detail"] == gone_use.json()["detail"]
        monkeypatch.setattr(provider, "resolve", normal_resolve)
        gone = client.post("/api/run", json={
            "graph": graph, "targetNodeId": source["id"],
        })
        assert gone.status_code == 410, gone.text
        assert gone.json()["detail"] == "local_run_input_revision_unavailable"
        assert "secret" not in gone.text
        assert dispatched is False
        assert set(deps.run_index) == run_index_before


@pytest.mark.parametrize("config", [[], "", 0, False])
def test_workspace_rejects_falsy_non_object_mount_config(monkeypatch, config):
    monkeypatch.setenv("DP_CATALOG_MOUNTS", json.dumps([
        {"id": "invalid-config", "provider": "fixture", "config": config},
    ]))

    mounts, invalid = workspace_providers._configured_mounts()

    assert mounts == []
    assert invalid


def test_workspace_composes_mounts_with_per_source_errors_stable_cursors_and_deep_links(
        workspace_scope, monkeypatch):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-providers")
    local_child = metadb.workspace_create_container(
        folder["id"], f"workspace-{token}-local-child")
    provider = _WorkspaceFixtureProvider()
    monkeypatch.setattr(workspace_providers, "_load_provider", lambda _name: provider)
    bounded = workspace_providers.bounded_list_children

    def deterministic_list_children(_provider, mount, *args, **kwargs):
        if mount.id == "a-slow":
            return ProviderPage(state="unavailable", reason="deadline exceeded")
        return bounded(_provider, mount, *args, **kwargs, timeout=0.001)

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
            f"container:{root['id']}", f"container:{folder['id']}", remote_container["id"],
        ]

        reads_before_canvas_action = provider.list_calls
        created = client.post("/api/workspace/canvases", json={
            "containerId": folder["id"], "expectedContainerVersion": folder["version"],
            "name": "Provider write guard",
        })
        assert created.status_code == 200, created.text
        assert provider.list_calls == reads_before_canvas_action
        created_document = created.json()
        metadb.workspace_delete_placement(
            created_document["resource"]["placementId"], expected_version=1)
        metadb.delete_canvas_cascade(created_document["id"])
    # A timed-out synchronous read is intentionally allowed to finish in the bounded executor. Let
    # this fixture relinquish its two short-lived leases before later concurrency-cap tests run.
    time.sleep(0.03)


def test_external_container_overlay_anchor_is_fenced_hidden_and_replay_safe(
        workspace_scope, monkeypatch):
    """A provider container lends a local destination without ever gaining write authority."""
    root = metadb.local_workspace_root()
    provider = _WorkspaceFixtureProvider()
    mount_id = f"overlay-{uuid.uuid4().hex}"
    resources = [CatalogResource(id="container-a", kind="container", name="Provider folder")]
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
            page = client.get(f"/api/workspace/containers/{root['id']}")
            assert page.status_code == 200, page.text
            remote = next(item for item in page.json()["items"] if item.get("resourceId") == "container-a")
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
                    metadb.CanvasShare(
                        canvas_id=workspace_scope["canvas_id"], user_id=editor_id, role="editor"),
                ])
            editor_move = client.put(
                f"/api/workspace/placements/{source_placement['id']}/canvas",
                headers={"X-DP-User": editor_id}, json={
                    "containerId": anchor_id,
                    "expectedContainerVersion": capability["containerVersion"],
                    "expectedVersion": source_placement["version"],
                })
            assert editor_move.status_code == 200, editor_move.text
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
            replay = client.post("/api/workspace/canvases", json=create_body)
            assert replay.status_code == 200, replay.text
            assert replay.json() == created_doc
            conflict = client.post("/api/workspace/canvases", json={
                **create_body, "name": "A different intent",
            })
            assert conflict.status_code == 422
            assert "different semantic request" in conflict.json()["detail"]

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

            # Provider display rename/move only refreshes binding snapshots; the local placement
            # stays on this binding generation's anchor.
            parent = CatalogResource(id="parent-a", kind="container", name="New provider parent")
            resources[:] = [
                parent,
                CatalogResource(
                    id="container-a", kind="container", name="Renamed folder", parent_id="parent-a"),
            ]
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

            # A delete/recreate with the same provider ID is terminally detached until explicit relink.
            resources[:] = []
            detached = client.get(f"/api/workspace/resources/{remote['id']}")
            assert detached.status_code == 200, detached.text
            assert detached.json()["resource"]["referenceState"] == "detached"
            resources[:] = [CatalogResource(id="container-a", kind="container", name="Recreated folder")]
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
            if created_ids:
                placement_ids = list(session.scalars(select(metadb.WorkspacePlacement.id).where(
                    metadb.WorkspacePlacement.target_kind == "canvas",
                    metadb.WorkspacePlacement.target_id.in_(created_ids),
                )))
                if placement_ids:
                    session.execute(delete(metadb.WorkspacePlacement).where(
                        metadb.WorkspacePlacement.id.in_(placement_ids)))
                session.execute(delete(metadb.Canvas).where(metadb.Canvas.id.in_(created_ids)))
            anchors = list(session.scalars(select(metadb.WorkspaceExternalOverlayAnchor).where(
                metadb.WorkspaceExternalOverlayAnchor.mount_id == mount_id)))
            session.execute(delete(metadb.WorkspaceExternalOverlayAnchor).where(
                metadb.WorkspaceExternalOverlayAnchor.mount_id == mount_id))
            for anchor in anchors:
                session.execute(delete(metadb.WorkspaceContainer).where(
                    metadb.WorkspaceContainer.id == anchor.container_id))
            if principal_ids:
                session.execute(delete(metadb.CanvasShare).where(
                    metadb.CanvasShare.user_id.in_(principal_ids)))
                session.execute(delete(metadb.User).where(metadb.User.id.in_(principal_ids)))


def test_workspace_provider_reference_recovery_detach_and_explicit_relink(
        workspace_scope, monkeypatch):
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
        "config": {"credential": "must-not-be-cached"},
    }])
    monkeypatch.setenv("DP_CATALOG_MOUNTS", mount_config)

    with TestClient(app) as client:
        page = client.get(f"/api/workspace/containers/{folder['id']}").json()
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
        assert "must-not-be-cached" not in serialized
        assert "uri" not in serialized.lower()


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
        assert groups["mount:f-overlimit"]["source"]["error"] == (
            "catalog provider exceeded the requested search limit")

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
        assert final_groups["mount:g-stuck"]["source"]["error"] == (
            "catalog provider returned a non-advancing search page")

        mismatched = client.get("/api/workspace/search", params={
            "q": "different", "limit": 1, "cursor": page["nextCursor"],
        })
        assert mismatched.status_code == 422
    time.sleep(0.03)


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
            assert page["groups"][0]["items"][0]["id"] == f"container:{container['id']}"
            second = client.get("/api/workspace/search", params={
                "q": name, "limit": 1, "cursor": page["nextCursor"],
            })
            assert second.status_code == 200, second.text
            assert second.json()["completeness"] == "complete"
            assert second.json()["groups"][0]["items"][0]["id"] == (
                f"canvas:{workspace_scope['canvas_id']}")
    finally:
        metadb.workspace_delete_placement(placement["id"], expected_version=placement["version"])
        metadb.workspace_delete_container(container["id"], expected_version=container["version"])


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
                created_ids.append(response.json()["id"])
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
            })
            assert concurrent.status_code == 409
            assert "currently open" in concurrent.json()["detail"]
        finally:
            hub_main._collab_rooms.pop(canvas_id, None)

        added = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
        })
        assert added.status_code == 200, added.text
        assert added.json()["version"] == 8

        stale = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": selected_dataset_ids, "expectedCanvasVersion": 7,
        })
        assert stale.status_code == 409

        missing = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [workspace_scope["dataset_id"], "missing-stable-dataset"],
            "expectedCanvasVersion": 8,
        })
        assert missing.status_code == 404

        duplicate = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [second_dataset_id, second_dataset_id], "expectedCanvasVersion": 8,
        })
        assert duplicate.status_code == 422

        oversized = client.post(f"/api/workspace/canvases/{canvas_id}/datasets", json={
            "datasetIds": [f"dataset-{index}" for index in range(51)],
            "expectedCanvasVersion": 8,
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
