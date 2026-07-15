"""Folders as first-class entities: create empty, rename (cascading), delete (reparenting).

The load-bearing contract: rename/delete work over the UNION of folder ENTITY paths and the distinct
entry `folder` strings — so a folder that exists only because a dataset was registered into it (the
normal flow, which never writes a folder entity) is still renameable/deletable. Also covers the
ValueError -> 400 mapping on the routes.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from urllib.parse import urlsplit

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import or_, select

from hub import handoff, metadb
from hub.main import app
from hub.metadb import CatalogEntry, CatalogFolder, CatalogLogicalDataset, ObjectAttempt

client = TestClient(app)


def _parquet(tmp_path, name: str) -> str:
    p = str(tmp_path / f"{name}.parquet")
    duckdb.connect(":memory:").execute(f"COPY (SELECT 1 id) TO '{p}' (FORMAT PARQUET)")
    return p


def _register(tmp_path, name: str, folder: str) -> dict:
    r = client.post("/api/catalog/register", json={"uri": _parquet(tmp_path, name), "name": name, "folder": folder})
    assert r.status_code == 200, r.text
    return r.json()


def _wipe(folder_roots: list[str], uri_prefixes: list[str]) -> None:
    for up in uri_prefixes:
        with contextlib.suppress(Exception):
            metadb.catalog_delete_prefix(up)
    with metadb.session() as s:
        for root in folder_roots:
            for row in s.scalars(select(CatalogFolder).where(or_(
                    CatalogFolder.path == root, CatalogFolder.path.like(root + "/%")))):
                s.delete(row)


def _raw_folder(uri: str) -> tuple[str, str]:
    """(indexed folder column, folder inside the stored doc JSON) for an entry — proves both are updated."""
    with metadb.session() as s:
        r = s.get(CatalogEntry, uri.rstrip("/"))
        assert r is not None
        return r.folder, json.loads(r.doc).get("folder")


def _allocate_managed_generation(logical_uri: str, name: str) -> dict:
    token = uuid.uuid4().hex
    handle = metadb.allocate_object_attempt(
        logical_uri=logical_uri,
        kind="sink",
        run_id=f"run-folder-{token}",
        allocation_key=f"allocation-folder-{token}",
        catalog_key_base=f"tbl_folder_{name}",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
        write_lease_seconds=30,
    )
    parsed = urlsplit(handle["uri"])
    root = f"{parsed.netloc}/{parsed.path.lstrip('/')}"
    inventory = [
        {
            "member_id": handoff._member_id(
                "unversioned_object", f"{root}/part-00000.parquet", "null"),
            "key": f"{root}/part-00000.parquet",
            "member_type": "unversioned_object",
            "size": 10,
            "etag": "data",
            "version_id": None,
            "upload_id": None,
            "is_latest": True,
            "is_commit": False,
        },
        {
            "member_id": handoff._member_id(
                "unversioned_object", handoff._object_manifest_path(root), "null"),
            "key": handoff._object_manifest_path(root),
            "member_type": "unversioned_object",
            "size": 20,
            "etag": "commit",
            "version_id": None,
            "upload_id": None,
            "is_latest": True,
            "is_commit": True,
        },
    ]
    metadb.record_object_attempt_commit(handle["uri"], inventory)
    return handle


def _managed_generation(logical_uri: str, name: str, folder: str) -> dict:
    handle = _allocate_managed_generation(logical_uri, name)
    metadb.catalog_upsert_entry(handle["uri"], name, {
        "id": "ignored",
        "name": name,
        "uri": handle["uri"],
        "folder": folder,
        "tags": [],
    })
    return handle


def _managed_folder_state(uri: str) -> dict:
    with metadb.session() as s:
        attempt = s.get(ObjectAttempt, uri)
        assert attempt is not None and attempt.logical_id
        logical = s.get(CatalogLogicalDataset, attempt.logical_id)
        assert logical is not None and logical.current_uri
        entry = s.get(CatalogEntry, logical.current_uri)
        assert entry is not None
        return {
            "current_uri": logical.current_uri,
            "entry_folder": entry.folder,
            "entry_doc": json.loads(entry.doc),
            "governance": json.loads(logical.governance_doc),
            "metadata_version": logical.metadata_version,
        }


def _cleanup_managed(handles: list[dict]) -> None:
    if not handles:
        return
    with contextlib.suppress(Exception):
        metadb.catalog_delete_entry(handles[0]["uri"])
    for handle in handles:
        with contextlib.suppress(Exception):
            metadb.quarantine_object_attempt(handle["uri"], "catalog folder test cleanup")


def test_create_empty_folder_persists_and_lists():
    try:
        assert metadb.catalog_folder_create("empties/one") == "empties/one"
        paths = {f["path"] for f in metadb.catalog_folders_list()}
        assert {"empties", "empties/one"} <= paths, "create backfills the ancestor path too"
        with pytest.raises(ValueError, match="already exists"):
            metadb.catalog_folder_create("empties/one")
    finally:
        _wipe(["empties"], [])


def test_empty_folder_appears_in_browse_tree():
    try:
        metadb.catalog_folder_create("archive/2024")
        root = client.get("/api/catalog/tree").json()
        node = next((f for f in root["folders"] if f["name"] == "archive"), None)
        assert node is not None and node["tableCount"] == 0, "an empty folder still shows (count 0)"
        level = client.get("/api/catalog/tree", params={"prefix": "archive"}).json()
        assert any(f["path"] == "archive/2024" for f in level["folders"])
    finally:
        _wipe(["archive"], [])


def test_register_normalizes_materializes_folder_and_rename_cascades(tmp_path):
    """#155: registering into 'x/y' MATERIALIZES first-class folder entities (its full ancestry), not
    just an implicit derived string; rename then cascades over both the entities and the entry folder."""
    t = _register(tmp_path, "unionds", " x / y ")
    uri = t["uri"]
    try:
        assert t["folder"] == "x/y", "the returned table uses the same canonical path as the entity"
        assert _raw_folder(uri) == ("x/y", "x/y"), "the indexed column and doc are canonical too"
        paths = {f["path"] for f in metadb.catalog_folders_list()}
        assert {"x", "x/y"} <= paths, "register materializes the assigned folder + its ancestor"

        r = client.put("/api/catalog/folders/rename", json={"oldPath": "x", "newPath": "z"})
        assert r.status_code == 200, r.text
        assert _raw_folder(uri) == ("z/y", "z/y"), "both the indexed column and the doc JSON move"
        assert client.get(f"/api/catalog/tables/{t['id']}").json()["folder"] == "z/y"
        assert {"z", "z/y"} <= {f["path"] for f in metadb.catalog_folders_list()}  # entities moved too
    finally:
        _wipe(["x", "z"], [uri])


def test_rename_updates_managed_governance_and_survives_later_generation():
    token = uuid.uuid4().hex
    old = f"managed-rename-{token[:10]}"
    new = f"managed-renamed-{token[:10]}"
    nested_logical = f"s3://folder-tests/{token}/nested.parquet"
    direct_logical = f"s3://folder-tests/{token}/direct.parquet"
    nested_name = f"nested_{token[:10]}"
    direct_name = f"direct_{token[:10]}"
    nested_handles: list[dict] = []
    direct_handles: list[dict] = []
    try:
        nested_handles.append(_managed_generation(
            nested_logical, nested_name, f"{old}/vision/raw"))
        direct_handles.append(_managed_generation(direct_logical, direct_name, old))
        nested_before = _managed_folder_state(nested_handles[0]["uri"])
        direct_before = _managed_folder_state(direct_handles[0]["uri"])

        metadb.catalog_folder_rename(old, new)

        nested_after = _managed_folder_state(nested_handles[0]["uri"])
        direct_after = _managed_folder_state(direct_handles[0]["uri"])
        assert nested_after["entry_folder"] == f"{new}/vision/raw"
        assert nested_after["entry_doc"]["folder"] == f"{new}/vision/raw"
        assert nested_after["governance"]["folder"] == f"{new}/vision/raw"
        assert nested_after["metadata_version"] == nested_before["metadata_version"] + 1
        assert direct_after["entry_folder"] == new
        assert direct_after["governance"]["folder"] == new
        assert direct_after["metadata_version"] == direct_before["metadata_version"] + 1
        paths = {folder["path"] for folder in metadb.catalog_folders_list()}
        assert {new, f"{new}/vision", f"{new}/vision/raw"} <= paths
        assert not any(path == old or path.startswith(old + "/") for path in paths)

        # A producer may still submit the original folder. Stable logical governance must win, so a
        # new physical generation cannot restore the stale path.
        nested_handles.append(_managed_generation(
            nested_logical, nested_name, f"{old}/vision/raw"))
        republished = _managed_folder_state(nested_handles[-1]["uri"])
        assert republished["current_uri"] == nested_handles[-1]["uri"]
        assert republished["entry_folder"] == f"{new}/vision/raw"
        assert republished["entry_doc"]["folder"] == f"{new}/vision/raw"
        assert republished["governance"]["folder"] == f"{new}/vision/raw"
        assert republished["metadata_version"] == nested_after["metadata_version"]
    finally:
        _cleanup_managed(nested_handles)
        _cleanup_managed(direct_handles)
        _wipe([old, new], [])


def test_set_metadata_normalizes_and_materializes_folder(tmp_path):
    t = _register(tmp_path, "curatedds", "")
    uri = t["uri"]
    try:
        r = client.put(
            f"/api/catalog/tables/{t['id']}/metadata",
            json={"folder": " curated / daily "},
        )
        assert r.status_code == 200, r.text
        assert r.json()["folder"] == "curated/daily"
        assert _raw_folder(uri) == ("curated/daily", "curated/daily")
        assert {"curated", "curated/daily"} <= {
            f["path"] for f in metadb.catalog_folders_list()
        }
    finally:
        _wipe(["curated"], [uri])


def test_delete_reparents_datasets_preserving_structure(tmp_path):
    t = _register(tmp_path, "deepds", "a/b/c")
    uri = t["uri"]
    try:
        r = client.post("/api/catalog/folders/delete", json={"path": "a/b"})
        assert r.status_code == 200, r.text
        # deleting a/b moves its subtree UP one level to the parent 'a', PRESERVING structure:
        # a/b/c -> a/c (not flattened to 'a'); nothing under a/b remains
        assert _raw_folder(uri) == ("a/c", "a/c")
        assert not any(f["path"].startswith("a/b") for f in metadb.catalog_folders_list())
    finally:
        _wipe(["a"], [uri])


def test_delete_reparents_managed_governance_and_survives_later_generation():
    token = uuid.uuid4().hex
    root = f"managed-delete-{token[:10]}"
    deleted = f"{root}/discard"
    old_folder = f"{deleted}/robotics/hands"
    reparented = f"{root}/robotics/hands"
    logical_uri = f"s3://folder-tests/{token}/delete.parquet"
    name = f"delete_{token[:10]}"
    handles: list[dict] = []
    try:
        handles.append(_managed_generation(logical_uri, name, old_folder))
        before = _managed_folder_state(handles[0]["uri"])

        metadb.catalog_folder_delete(deleted)

        after = _managed_folder_state(handles[0]["uri"])
        assert after["entry_folder"] == reparented
        assert after["entry_doc"]["folder"] == reparented
        assert after["governance"]["folder"] == reparented
        assert after["metadata_version"] == before["metadata_version"] + 1
        paths = {folder["path"] for folder in metadb.catalog_folders_list()}
        assert {root, f"{root}/robotics", reparented} <= paths
        assert not any(path == deleted or path.startswith(deleted + "/") for path in paths)

        handles.append(_managed_generation(logical_uri, name, old_folder))
        republished = _managed_folder_state(handles[-1]["uri"])
        assert republished["current_uri"] == handles[-1]["uri"]
        assert republished["entry_folder"] == reparented
        assert republished["governance"]["folder"] == reparented
        assert republished["metadata_version"] == after["metadata_version"]
    finally:
        _cleanup_managed(handles)
        _wipe([root], [])


def test_folder_rename_rolls_back_entry_governance_and_entities_on_failure(monkeypatch):
    token = uuid.uuid4().hex
    old = f"managed-atomic-{token[:10]}"
    new = f"managed-atomic-new-{token[:10]}"
    logical_uri = f"s3://folder-tests/{token}/atomic.parquet"
    handles: list[dict] = []
    try:
        handles.append(_managed_generation(
            logical_uri, f"atomic_{token[:10]}", f"{old}/nested"))
        before = _managed_folder_state(handles[0]["uri"])
        before_paths = {folder["path"] for folder in metadb.catalog_folders_list()}

        def fail_materialization(_session, _paths):
            raise RuntimeError("injected folder materialization failure")

        monkeypatch.setattr(metadb, "_ensure_folder_rows", fail_materialization)
        with pytest.raises(RuntimeError, match="injected folder materialization failure"):
            metadb.catalog_folder_rename(old, new)

        assert _managed_folder_state(handles[0]["uri"]) == before
        assert {folder["path"] for folder in metadb.catalog_folders_list()} == before_paths
        assert _raw_folder(handles[0]["uri"]) == (f"{old}/nested", f"{old}/nested")
    finally:
        _cleanup_managed(handles)
        _wipe([old, new], [])


def test_batch_publication_materializes_normalized_folders_that_survive_emptying():
    token = uuid.uuid4().hex
    root = f"batch-folders-{token[:10]}"
    name_a = f"batch_a_{token[:10]}"
    name_b = f"batch_b_{token[:10]}"
    handles: list[dict] = []
    try:
        handles = [
            _allocate_managed_generation(
                f"s3://folder-tests/{token}/batch-a.parquet", name_a),
            _allocate_managed_generation(
                f"s3://folder-tests/{token}/batch-b.parquet", name_b),
        ]
        metadb.catalog_publish_entries([
            (handles[0]["uri"], name_a, {
                "id": "ignored", "name": name_a, "uri": handles[0]["uri"],
                "folder": f" {root} / nested / a ",
            }, None, None),
            (handles[1]["uri"], name_b, {
                "id": "ignored", "name": name_b, "uri": handles[1]["uri"],
                "folder": f"{root}/nested/b",
            }, None, None),
        ])

        assert _raw_folder(handles[0]["uri"]) == (
            f"{root}/nested/a", f"{root}/nested/a")
        assert _managed_folder_state(handles[0]["uri"])["governance"]["folder"] == \
            f"{root}/nested/a"
        paths = {folder["path"] for folder in metadb.catalog_folders_list()}
        expected = {
            root, f"{root}/nested", f"{root}/nested/a", f"{root}/nested/b",
        }
        assert expected <= paths

        metadb.catalog_delete_entry(handles[0]["uri"])
        metadb.catalog_delete_entry(handles[1]["uri"])
        assert expected <= {folder["path"] for folder in metadb.catalog_folders_list()}
        tree, tables, total = metadb.catalog_tree(f"{root}/nested")
        assert {path for _name, path, _count in tree} == {
            f"{root}/nested/a", f"{root}/nested/b",
        }
        assert tables == [] and total == 0
    finally:
        for handle in handles:
            _cleanup_managed([handle])
        _wipe([root], [])


def test_route_errors_map_to_400(tmp_path):
    t = _register(tmp_path, "collideds", "keep")
    uri = t["uri"]
    try:
        metadb.catalog_folder_create("taken")
        assert client.put("/api/catalog/folders/rename",
                          json={"oldPath": "nope", "newPath": "whatever"}).status_code == 400
        assert client.put("/api/catalog/folders/rename",
                          json={"oldPath": "keep", "newPath": "taken"}).status_code == 400
        assert client.post("/api/catalog/folders/delete", json={"path": "missing"}).status_code == 400
        assert client.post("/api/catalog/folders", json={"path": ".."}).status_code == 400
        assert client.put(
            f"/api/catalog/tables/{t['id']}/metadata", json={"folder": "bad//path"}
        ).status_code == 400
    finally:
        _wipe(["keep", "taken"], [uri])


def test_rename_into_descendant_is_rejected(tmp_path):
    # #161 review #6: renaming a folder into its own descendant would self-nest — must be rejected.
    t = _register(tmp_path, "descds", "a")
    uri = t["uri"]
    try:
        r = client.put("/api/catalog/folders/rename", json={"oldPath": "a", "newPath": "a/b"})
        assert r.status_code == 400, r.text
        assert "itself" in r.json()["detail"]
    finally:
        _wipe(["a"], [uri])


def test_external_provider_subclass_refuses_local_folder_mutation(monkeypatch, tmp_path):
    # #161 review #4: a read-only external provider (subclasses InMemoryCatalog to own a REMOTE
    # namespace) sets folders_mutable=False so it inherits none of the local-metadb folder writes; the
    # routes must refuse (501), never report a local-only write against a store its browse() ignores.
    from hub.deps import get_deps
    from hub.plugins.catalog import InMemoryCatalog

    class ReadOnlyExternal(InMemoryCatalog):
        folders_mutable = False

    monkeypatch.setattr(get_deps(), "catalog", ReadOnlyExternal(str(tmp_path), lambda _uri: object()))
    assert client.post("/api/catalog/folders", json={"path": "x"}).status_code == 501
    assert client.put("/api/catalog/folders/rename", json={"oldPath": "x", "newPath": "y"}).status_code == 501
    assert client.post("/api/catalog/folders/delete", json={"path": "x"}).status_code == 501
