"""Folders as first-class entities: create empty, rename (cascading), delete (reparenting).

The load-bearing contract: rename/delete work over the UNION of folder ENTITY paths and the distinct
entry `folder` strings — so a folder that exists only because a dataset was registered into it (the
normal flow, which never writes a folder entity) is still renameable/deletable. Also covers the
migration backfill and the ValueError -> 400 mapping on the routes.
"""

from __future__ import annotations

import contextlib
import json

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import or_, select, text

from hub import metadb
from hub.main import app
from hub.metadb import CatalogEntry, CatalogFolder

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


def test_register_materializes_folder_and_rename_cascades(tmp_path):
    """#155: registering into 'x/y' MATERIALIZES first-class folder entities (its full ancestry), not
    just an implicit derived string; rename then cascades over both the entities and the entry folder."""
    t = _register(tmp_path, "unionds", "x/y")
    uri = t["uri"]
    try:
        paths = {f["path"] for f in metadb.catalog_folders_list()}
        assert {"x", "x/y"} <= paths, "register materializes the assigned folder + its ancestor"

        r = client.put("/api/catalog/folders/rename", json={"oldPath": "x", "newPath": "z"})
        assert r.status_code == 200, r.text
        assert _raw_folder(uri) == ("z/y", "z/y"), "both the indexed column and the doc JSON move"
        assert client.get(f"/api/catalog/tables/{t['id']}").json()["folder"] == "z/y"
        assert {"z", "z/y"} <= {f["path"] for f in metadb.catalog_folders_list()}  # entities moved too
    finally:
        _wipe(["x", "z"], [uri])


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
    finally:
        _wipe(["keep", "taken"], [uri])


@contextlib.contextmanager
def _isolated_metadata(url: str):
    original_url = metadb.settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    metadb.settings.database_url = url
    metadb._engine = metadb._Session = None
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        metadb.settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def test_migration_0027_backfills_one_row_per_folder_and_ancestor(tmp_path):
    from alembic import command

    with _isolated_metadata(f"sqlite:///{tmp_path / 'backfill.db'}"):
        with metadb.engine().connect() as conn:
            command.upgrade(metadb._alembic_cfg(conn), "0025_run_request_id")
        with metadb.engine().begin() as conn:
            conn.execute(
                text("INSERT INTO catalog_entries (uri, name, doc, folder) VALUES (:u, :n, :d, :f)"),
                [{"u": "mem://a", "n": "a", "d": "{}", "f": "x/y"},
                 {"u": "mem://b", "n": "b", "d": "{}", "f": "p"},
                 {"u": "mem://c", "n": "c", "d": "{}", "f": ""}])
        with metadb.engine().connect() as conn:
            command.upgrade(metadb._alembic_cfg(conn), "0027_catalog_folders")
        with metadb.engine().connect() as conn:
            paths = {r[0] for r in conn.execute(text("SELECT path FROM catalog_folders"))}
        assert paths == {"x", "x/y", "p"}, "distinct folders + ancestors backfilled; root-filed rows ignored"


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


def test_folder_mutation_501_when_provider_unsupported(monkeypatch):
    # #161 review #4: a catalog provider that doesn't own the local folder store must not have a
    # local-only mutation reported as success.
    from hub.deps import get_deps
    monkeypatch.setattr(get_deps().catalog, "folders_mutable", False, raising=False)
    assert client.post("/api/catalog/folders", json={"path": "x"}).status_code == 501
    assert client.put("/api/catalog/folders/rename", json={"oldPath": "x", "newPath": "y"}).status_code == 501
    assert client.post("/api/catalog/folders/delete", json={"path": "x"}).status_code == 501
