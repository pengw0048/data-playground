"""Canvas file-key contract: full-strength UUID4 for every newly minted Canvas identity."""

from __future__ import annotations

import json
import re
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from hub import metadb
from hub.main import app

UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def test_new_canvas_file_key_batches_are_unique_full_strength_uuid4():
    batch_a = [metadb.new_canvas_file_key() for _ in range(2_000)]
    batch_b = [metadb.new_canvas_file_key() for _ in range(2_000)]
    for key in [*batch_a, *batch_b]:
        assert UUID_V4.fullmatch(key)
        uuid.UUID(key, version=4)
    assert len(set(batch_a)) == len(batch_a)
    assert len(set(batch_b)) == len(batch_b)
    assert set(batch_a).isdisjoint(batch_b)


def test_workspace_and_omitted_id_creates_use_full_strength_keys():
    with metadb.session() as session:
        root = session.get(metadb.WorkspaceContainer, metadb.LOCAL_WORKSPACE_ROOT_ID)
        assert root is not None
        root_version = root.version
    created = metadb.workspace_create_canvas_action(
        uid=metadb.DEFAULT_USER_ID,
        container_id=metadb.LOCAL_WORKSPACE_ROOT_ID,
        expected_container_version=root_version,
        name="Strong key workspace canvas",
    )
    assert UUID_V4.fullmatch(created["id"])
    omitted_id = None
    try:
        with TestClient(app) as client:
            omitted = client.post(
                "/api/canvas",
                json={"name": "omitted id", "version": 99, "nodes": [], "edges": []},
            )
            assert omitted.status_code == 200
            omitted_id = omitted.json()["id"]
            assert omitted.json()["created"] is True
            assert UUID_V4.fullmatch(omitted_id)
            assert omitted_id != created["id"]
            assert client.get(f"/api/canvas/{omitted_id}").json()["id"] == omitted_id
    finally:
        metadb.delete_canvas_cascade(created["id"])
        if omitted_id is not None:
            metadb.delete_canvas_cascade(omitted_id)


def test_duplicate_supplied_key_fails_closed_without_mutating_existing():
    canvas_id = metadb.new_canvas_file_key()
    original_doc = {
        "id": canvas_id, "name": "original owner doc", "version": 1,
        "nodes": [], "edges": [],
    }
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID, name="original owner doc",
            version=3, doc=json.dumps(original_doc), visibility="private",
        ))
        session.add(metadb.WorkspacePlacement(
            container_id=metadb.LOCAL_WORKSPACE_ROOT_ID, target_kind="canvas",
            target_id=canvas_id, name="original owner doc", ordinal=0, version=7,
        ))
    try:
        with TestClient(app) as client:
            conflict = client.post(
                "/api/canvas",
                json={
                    "id": canvas_id, "name": "attacker rename", "version": 1,
                    "nodes": [], "edges": [],
                },
            )
            assert conflict.status_code == 200
            assert conflict.json() == {
                "ok": True, "id": canvas_id, "version": 3, "created": False,
            }
            persisted = client.get(f"/api/canvas/{canvas_id}").json()
            assert persisted == original_doc
        with metadb.session() as session:
            row = session.get(metadb.Canvas, canvas_id)
            assert row is not None
            assert row.owner_id == metadb.DEFAULT_USER_ID
            assert row.name == "original owner doc"
            assert row.version == 3
            assert json.loads(row.doc) == original_doc
            placement = session.scalar(select(metadb.WorkspacePlacement).where(
                metadb.WorkspacePlacement.target_kind == "canvas",
                metadb.WorkspacePlacement.target_id == canvas_id,
            ))
            assert placement is not None
            assert placement.version == 7
            assert placement.name == "original owner doc"
    finally:
        metadb.delete_canvas_cascade(canvas_id)


def test_legacy_canvas_ids_remain_readable_and_writable():
    legacy_ids = (
        "canvas_legacy_prefix_ok",
        "abcdef123456",
        "hist-test-with.unusual_chars",
    )
    with TestClient(app) as client:
        for canvas_id in legacy_ids:
            metadb.delete_canvas_cascade(canvas_id)
            created = client.post(
                "/api/canvas",
                json={"id": canvas_id, "name": "legacy", "version": 1, "nodes": [], "edges": []},
            )
            assert created.status_code == 200
            assert created.json() == {
                "ok": True, "id": canvas_id, "version": 1, "created": True,
            }
            assert client.get(f"/api/canvas/{canvas_id}").json() == {
                "id": canvas_id, "name": "legacy", "version": 1, "nodes": [], "edges": [],
            }
            saved = client.put(
                f"/api/canvas/{canvas_id}?expectedVersion=1",
                json={"id": canvas_id, "name": "legacy saved", "version": 1, "nodes": [], "edges": []},
            )
            assert saved.status_code == 200
            assert saved.json()["version"] == 2
            assert client.get(f"/api/canvas/{canvas_id}").json()["name"] == "legacy saved"
            metadb.delete_canvas_cascade(canvas_id)
