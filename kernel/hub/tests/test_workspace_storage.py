"""Local Workspace storage invariants, independent of browse and UI delivery."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

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
    dataset_id = workspace_scope["dataset_id"]
    canvas_placement = metadb.workspace_create_placement(
        replacement["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-canvas",
    )
    with metadb.session() as session:
        dataset_row = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == dataset_id,
        ))
        assert dataset_row is not None
        dataset_placement = {"id": dataset_row.id, "version": dataset_row.version}
    dataset_placement = metadb.workspace_update_placement(
        dataset_placement["id"], expected_version=dataset_placement["version"],
        container_id=replacement["id"], name=f"workspace-{token}-dataset")

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
    assert dataset_placement["targetId"] == dataset_id

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
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    original = workspace_scope["dataset_id"]
    container = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-dataset-lifecycle")
    with metadb.session() as session:
        row = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == original,
        ))
        assert row is not None
        placement = {"id": row.id, "version": row.version}
    placement = metadb.workspace_update_placement(
        placement["id"], expected_version=placement["version"], container_id=container["id"],
        name=f"workspace-{token}-dataset-lifecycle")
    metadb.catalog_delete_entry(uri)
    with metadb.session() as session:
        detached = session.get(metadb.WorkspacePlacement, placement["id"])
        assert detached is not None and detached.target_id == original
    metadb.catalog_upsert_entry(uri, "Replacement dataset", {
        "id": f"tbl_recreated_{uuid.uuid4().hex}", "name": "Replacement dataset", "uri": uri,
        "version": "v2",
    })
    assert metadb.workspace_builtin_dataset_identity(uri) != original


def test_workspace_api_mixes_keyset_pages_resolves_ancestors_and_never_writes_catalog(workspace_scope):
    token = workspace_scope["canvas_id"].removeprefix("workspace-canvas-")
    root = metadb.local_workspace_root()
    folder = metadb.workspace_create_container(root["id"], f"workspace-{token}-api", ordinal=0)
    child = metadb.workspace_create_container(folder["id"], f"workspace-{token}-api-child", ordinal=0)
    metadb.set_visibility(workspace_scope["canvas_id"], "workspace")
    dataset_id = workspace_scope["dataset_id"]
    canvas = metadb.workspace_create_placement(
        folder["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-canvas", ordinal=0)
    with metadb.session() as session:
        row = session.scalar(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset",
            metadb.WorkspacePlacement.target_id == dataset_id,
        ))
        assert row is not None
        dataset_placement = {"id": row.id, "version": row.version}
    metadb.workspace_update_placement(
        dataset_placement["id"], expected_version=dataset_placement["version"],
        container_id=folder["id"], name=f"workspace-{token}-dataset", ordinal=0)

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
                f"container:{child['id']}", f"canvas:{workspace_scope['canvas_id']}",
            ]
            assert [item["id"] for item in second.json()["items"]] == [f"dataset:{dataset_id}"]
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
            page = client.get(f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}",
                              params={"limit": 100})
            assert page.status_code == 200
            assert {(item["id"], item["name"]) for item in page.json()["items"]} >= {
                (f"canvas:{canvas_id}", "Lifecycle canvas"),
                (f"dataset:{dataset_id}", "Lifecycle dataset"),
            }
            renamed = client.put(f"/api/canvas/{canvas_id}", json={
                "id": canvas_id, "name": "Renamed lifecycle canvas", "version": 2,
                "nodes": [], "edges": [],
            })
            assert renamed.status_code == 200
            metadb.catalog_set_metadata(
                uri, "", None, None, [], name="Renamed lifecycle dataset")
            renamed_page = client.get(
                f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}",
                params={"limit": 100})
            assert renamed_page.status_code == 200
            assert {(item["id"], item["name"]) for item in renamed_page.json()["items"]} >= {
                (f"canvas:{canvas_id}", "Renamed lifecycle canvas"),
                (f"dataset:{dataset_id}", "Renamed lifecycle dataset"),
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
