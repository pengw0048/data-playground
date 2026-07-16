"""Local Workspace storage invariants, independent of browse and UI delivery."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid

import pytest
from sqlalchemy import delete, select

from hub import metadb


@pytest.fixture
def workspace_scope():
    metadb.migrate_db()
    token = uuid.uuid4().hex
    user_id = f"workspace-user-{token}"
    canvas_id = f"workspace-canvas-{token}"
    uri = f"file:///workspace-{token}.parquet"
    with metadb.session() as session:
        session.add(metadb.User(id=user_id, name="Workspace test"))
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=user_id, name="Original canvas", version=7,
            doc=json.dumps({"id": canvas_id, "name": "Original canvas", "version": 7,
                            "nodes": [], "edges": []}),
        ))
    metadb.catalog_upsert_entry(uri, "Original dataset", {
        "id": f"tbl_{token}", "name": "Original dataset", "uri": uri, "version": "v1",
    })
    try:
        yield {"canvas_id": canvas_id, "uri": uri}
    finally:
        with metadb.session() as session:
            placement_ids = list(session.scalars(select(metadb.WorkspacePlacement.id).where(
                metadb.WorkspacePlacement.name.like(f"workspace-{token}%"))))
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
    dataset_id = metadb.workspace_builtin_dataset_identity(workspace_scope["uri"])
    canvas_placement = metadb.workspace_create_placement(
        replacement["id"], target_kind="canvas", target_id=workspace_scope["canvas_id"],
        name=f"workspace-{token}-canvas",
    )
    dataset_placement = metadb.workspace_create_placement(
        replacement["id"], target_kind="dataset", target_id=dataset_id,
        name=f"workspace-{token}-dataset",
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
    original = metadb.workspace_builtin_dataset_identity(uri)
    container = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], f"workspace-{token}-dataset-lifecycle")
    placement = metadb.workspace_create_placement(
        container["id"], target_kind="dataset", target_id=original,
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
    metadb.workspace_delete_placement(placement["id"], expected_version=placement["version"])


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
