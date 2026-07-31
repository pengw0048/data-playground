"""Product contract for exact Transform discovery, targeting, and upgrade (#482)."""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app


client = TestClient(app)


def _user(prefix: str) -> str:
    metadb.migrate_db()
    uid = f"{prefix}-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name=prefix))
    return uid


def _headers(uid: str) -> dict[str, str]:
    return {"X-DP-User": uid}


def _promote(
        uid: str, key: str, *, title: str, marker: str,
        input_schema: list[dict] | None = None,
        output_schema: list[dict] | None = None) -> dict:
    response = client.post("/api/processors/promote", headers=_headers(uid), json={
        "id": key, "title": title, "blurb": f"{title} description",
        "category": "robotics", "mode": "map",
        "code": f"def fn(row):\n    row['marker'] = '{marker}'\n    return row",
        "inputSchema": input_schema or [], "outputSchema": output_schema or [],
        "requirements": ["pyarrow"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _create_with_transform(uid: str, descriptor: dict, name: str = "Transform target") -> dict:
    response = client.post("/api/workspace/canvases", headers=_headers(uid), json={
        "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
        "expectedContainerVersion": 1,
        "name": name,
        "transformId": descriptor["id"],
        "transformVersion": descriptor["version"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_library_is_keyset_paginated_filterable_and_keeps_deleted_exact_truth() -> None:
    uid = _user("library-page")
    descriptors = [_promote(
        uid, f"page-{uuid.uuid4().hex}", title=f"Robot transform {index:02d}", marker=str(index),
    ) for index in range(28)]

    seen: list[str] = []
    cursor = None
    while True:
        response = client.get("/api/transform-library", headers=_headers(uid), params={
            "source": "promoted", "category": "robotics", "limit": 7,
            **({"cursor": cursor} if cursor else {}),
        })
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
        if not page["hasMore"]:
            break
        cursor = page["nextCursor"]
    assert len(seen) == 28 and len(set(seen)) == 28
    assert client.get("/api/transform-library", headers=_headers(uid), params={
        "cursor": "%%%", "source": "promoted",
    }).status_code == 422

    exact = descriptors[12]
    filtered = client.get("/api/transform-library", headers=_headers(uid), params={
        "q": "transform 12", "source": "promoted", "mode": "map",
    })
    assert [item["id"] for item in filtered.json()["items"]] == [exact["id"]]
    deleted = client.delete(
        f"/api/processors/{exact['id']}/versions/{exact['version']}", headers=_headers(uid))
    assert deleted.status_code == 200
    detail = client.get(
        f"/api/transform-library/{exact['id']}", headers=_headers(uid),
        params={"version": exact["version"]}).json()
    assert detail["versions"][0]["availability"] == "deleted"
    assert detail["versions"][0]["version"] == exact["version"]
    assert "code" not in detail["versions"][0]


def test_promoted_source_is_durable_exact_owner_scoped_and_integrity_checked() -> None:
    from hub.plugins.processors import ProcessorRegistry

    owner = _user("promoted-source-owner")
    other = _user("promoted-source-other")
    key = f"source-{uuid.uuid4().hex}"
    first = _promote(owner, key, title="Versioned source", marker="v1")
    second = _promote(owner, key, title="Versioned source", marker="v2")
    expected = {
        first["version"]: "def fn(row):\n    row['marker'] = 'v1'\n    return row",
        second["version"]: "def fn(row):\n    row['marker'] = 'v2'\n    return row",
    }

    detail = client.get(
        f"/api/transform-library/{first['id']}", headers=_headers(owner),
        params={"version": first["version"]},
    )
    assert detail.status_code == 200
    assert all("source" not in version and "code" not in version
               for version in detail.json()["versions"])
    listed = client.get("/api/processors", headers=_headers(owner))
    assert listed.status_code == 200
    descriptor = next(item for item in listed.json() if item["id"] == first["id"])
    assert "source" not in descriptor and "code" not in descriptor
    assert "'marker'" not in listed.text and "'marker'" not in detail.text

    # A fresh registry has no process-local promoted entries; source comes from the durable exact row.
    restarted = ProcessorRegistry()
    for version, code in expected.items():
        durable = restarted.installed_source(
            first["id"], version, owner_id=owner)
        assert durable.source == code
        assert durable.sha256 == hashlib.sha256(code.encode()).hexdigest()

        response = client.get(
            f"/api/processors/{first['id']}/versions/{version}/source",
            headers=_headers(owner),
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "processorId": first["id"],
            "version": version,
            "language": "python",
            "source": code,
            "sha256": hashlib.sha256(code.encode()).hexdigest(),
        }
        unauthorized = client.get(
            f"/api/processors/{first['id']}/versions/{version}/source",
            headers=_headers(other),
        )
        assert unauthorized.status_code == 404
        assert unauthorized.json()["detail"] == "Processor implementation source unavailable"

    assert client.get(
        f"/api/processors/{first['id']}/versions/v999/source",
        headers=_headers(owner),
    ).status_code == 404

    # A tombstone remains an immutable owner-visible version and keeps its inspectable definition.
    deleted = client.delete(
        f"/api/processors/{first['id']}/versions/{first['version']}",
        headers=_headers(owner),
    )
    assert deleted.status_code == 200, deleted.text
    retained_source = client.get(
        f"/api/processors/{first['id']}/versions/{first['version']}/source",
        headers=_headers(owner),
    )
    assert retained_source.status_code == 200
    assert retained_source.json()["source"] == expected[first["version"]]

    with metadb.session() as session:
        row = session.get(
            metadb.PromotedTransformVersion, (second["id"], 2))
        assert row is not None
        original_digest = row.semantic_digest
        row.semantic_digest = "0" * 64
    try:
        corrupt = client.get(
            f"/api/processors/{second['id']}/versions/{second['version']}/source",
            headers=_headers(owner),
        )
        assert corrupt.status_code == 503
        assert corrupt.json()["detail"] == "Processor implementation source could not be loaded"
        assert "integrity" not in corrupt.text
    finally:
        with metadb.session() as session:
            row = session.get(
                metadb.PromotedTransformVersion, (second["id"], 2))
            assert row is not None
            row.semantic_digest = original_digest


def test_plugin_installed_source_is_exact_opt_in_and_separate_from_metadata() -> None:
    from hub.deps import get_deps
    from hub.plugins.processors import ProcessorSourceProvider, RegisteredProcessor

    uid = _user("installed-source")
    processor_id = f"plugin.source.{uuid.uuid4().hex}"
    source = (
        b"MAX_DECODED_PIXELS = 50_000_000\n\n"
        b"def processor_factory(params):\n"
        b"    return lambda row: {**row, 'bounded': MAX_DECODED_PIXELS}\n"
    )
    registry = get_deps().registry
    registry.register(RegisteredProcessor(
        id=processor_id,
        version="v7",
        title="Installed source fixture",
        mode="map",
        fn_factory=lambda _params: lambda row: row,
        source=ProcessorSourceProvider(language="python", read=lambda: source),
    ))
    try:
        direct = registry.installed_source(processor_id, "v7")
        assert direct.source == source.decode("utf-8")

        listed = client.get("/api/processors", headers=_headers(uid))
        assert listed.status_code == 200
        descriptor = next(item for item in listed.json() if item["id"] == processor_id)
        assert "source" not in descriptor
        assert "MAX_DECODED_PIXELS" not in listed.text

        detail = client.get(
            f"/api/transform-library/{processor_id}",
            headers=_headers(uid),
            params={"version": "v7"},
        )
        assert detail.status_code == 200
        assert "source" not in detail.json()["versions"][0]
        assert "MAX_DECODED_PIXELS" not in detail.text

        response = client.get(
            f"/api/processors/{processor_id}/versions/v7/source",
            headers=_headers(uid),
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "processorId": processor_id,
            "version": "v7",
            "language": "python",
            "source": source.decode("utf-8"),
            "sha256": hashlib.sha256(source).hexdigest(),
        }
        assert client.get(
            f"/api/processors/{processor_id}/versions/v6/source",
            headers=_headers(uid),
        ).status_code == 404
    finally:
        registry._procs.pop(processor_id, None)


def test_installed_source_unavailable_is_honest_and_does_not_expose_loader_errors() -> None:
    from hub.deps import get_deps
    from hub.plugins.processors import ProcessorSourceProvider, RegisteredProcessor

    uid = _user("installed-source-unavailable")
    registry = get_deps().registry
    plain_id = f"plugin.plain.{uuid.uuid4().hex}"
    broken_id = f"plugin.broken.{uuid.uuid4().hex}"

    def broken_reader() -> bytes:
        raise OSError("/private/plugin/path/mapping.py contains internal detail")

    registry.register(RegisteredProcessor(
        id=plain_id, title="No source", mode="map",
        fn_factory=lambda _params: lambda row: row,
    ))
    registry.register(RegisteredProcessor(
        id=broken_id, title="Broken source", mode="map",
        fn_factory=lambda _params: lambda row: row,
        source=ProcessorSourceProvider(language="python", read=broken_reader),
    ))
    try:
        absent = client.get(
            f"/api/processors/{plain_id}/versions/v1/source",
            headers=_headers(uid),
        )
        assert absent.status_code == 404
        broken = client.get(
            f"/api/processors/{broken_id}/versions/v1/source",
            headers=_headers(uid),
        )
        assert broken.status_code == 503
        assert broken.json()["detail"] == "Processor implementation source could not be loaded"
        assert "/private/" not in broken.text
    finally:
        registry._procs.pop(plain_id, None)
        registry._procs.pop(broken_id, None)


def test_library_unicode_keyset_and_search_use_one_canonical_order() -> None:
    uid = _user("library-unicode")
    first = _promote(
        uid, f"unicode-{uuid.uuid4().hex}", title="Ä robot Transform", marker="a")
    second = _promote(
        uid, f"unicode-{uuid.uuid4().hex}", title="Ö robot Transform", marker="o")
    expanded = _promote(
        uid, f"unicode-{uuid.uuid4().hex}", title="ﷺ" * 256, marker="expanded")
    long_first = _promote(
        uid, f"unicode-{uuid.uuid4().hex}", title="😀" * 256, marker="emoji-1")
    long_second = _promote(
        uid, f"unicode-{uuid.uuid4().hex}", title="😁" * 256, marker="emoji-2")
    seen: list[str] = []
    cursor = None
    while True:
        response = client.get("/api/transform-library", headers=_headers(uid), params={
            "source": "promoted", "limit": 1,
            **({"cursor": cursor} if cursor else {}),
        })
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
        if not page["hasMore"]:
            break
        cursor = page["nextCursor"]
        assert len(cursor) <= 512
    assert seen == [
        first["id"], second["id"], expanded["id"], long_first["id"], long_second["id"],
    ]
    searched = client.get("/api/transform-library", headers=_headers(uid), params={
        "source": "promoted", "q": "ä ROBOT transform",
    })
    assert [item["id"] for item in searched.json()["items"]] == [first["id"]]


def test_library_cursor_keeps_its_exact_version_boundary_when_latest_title_moves() -> None:
    uid = _user("library-moving-title")
    key = f"moving-{uuid.uuid4().hex}"
    first = _promote(uid, key, title="Alpha", marker="v1")
    middle = _promote(
        uid, f"middle-{uuid.uuid4().hex}", title="Zulu", marker="middle")
    page_one = client.get("/api/transform-library", headers=_headers(uid), params={
        "source": "promoted", "limit": 1,
    }).json()
    assert page_one["items"][0]["id"] == first["id"]
    cursor = page_one["nextCursor"]

    moved = _promote(uid, key, title="ZZZ", marker="v2")
    assert moved["version"] == "v2"
    deleted = client.delete(
        f"/api/processors/{first['id']}/versions/{first['version']}",
        headers=_headers(uid))
    assert deleted.status_code == 200
    seen = [first["id"]]
    while cursor:
        response = client.get("/api/transform-library", headers=_headers(uid), params={
            "source": "promoted", "limit": 1, "cursor": cursor,
        })
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["nextCursor"]
    assert seen == [first["id"], middle["id"]]
    assert len(seen) == len(set(seen))


def test_exact_target_actions_are_atomic_role_checked_and_canvas_scoped() -> None:
    owner = _user("target-owner")
    viewer = _user("target-viewer")
    descriptor = _promote(owner, f"target-{uuid.uuid4().hex}", title="Targeted", marker="v1")
    missing_canvas = client.post(
        "/api/workspace/canvases/missing-canvas/transforms",
        headers=_headers(owner), json={
            "transformId": descriptor["id"], "transformVersion": "v1",
            "expectedCanvasVersion": 1,
        })
    assert missing_canvas.status_code == 404
    created = _create_with_transform(owner, descriptor)
    assert created["nodeId"]
    doc = client.get(f"/api/canvas/{created['id']}", headers=_headers(owner)).json()
    assert doc["nodes"][0]["data"]["config"] == {
        "source": "library", "processor": descriptor["id"],
        "version": "v1", "mode": "map",
    }

    with metadb.session() as session:
        session.add(metadb.CanvasShare(
            canvas_id=created["id"], user_id=viewer, role="viewer"))
    denied = client.post(
        f"/api/workspace/canvases/{created['id']}/transforms",
        headers=_headers(viewer), json={
            "transformId": descriptor["id"], "transformVersion": "v1",
            "expectedCanvasVersion": 1,
        })
    assert denied.status_code == 403
    stale = client.post(
        f"/api/workspace/canvases/{created['id']}/transforms",
        headers=_headers(owner), json={
            "transformId": descriptor["id"], "transformVersion": "v1",
            "expectedCanvasVersion": 99,
        })
    assert stale.status_code == 409
    assert len(client.get(f"/api/canvas/{created['id']}", headers=_headers(owner)).json()["nodes"]) == 1

    refs = client.get(
        f"/api/canvas/{created['id']}/transform-references", headers=_headers(viewer))
    assert refs.status_code == 200
    assert refs.json()[0]["availability"] == "active"
    assert refs.json()[0]["descriptor"]["id"] == descriptor["id"]
    assert "code" not in refs.json()[0]["descriptor"]


def test_upgrade_requires_proven_compatibility_and_invalidates_section_downstream() -> None:
    uid = _user("upgrade-owner")
    key = f"upgrade-{uuid.uuid4().hex}"
    input_schema = [{
        "name": "event", "fieldId": "event-id", "type": "string",
        "nullable": False, "hasDefault": False,
    }]
    output_schema = [{
        "name": "score", "fieldId": "score-id", "type": "int",
        "nullable": False, "hasDefault": False,
    }]
    first = _promote(uid, key, title="Versioned", marker="v1",
                     input_schema=input_schema, output_schema=output_schema)
    compatible = _promote(uid, key, title="Versioned", marker="v2",
                          input_schema=input_schema, output_schema=output_schema)
    breaking = _promote(uid, key, title="Versioned", marker="breaking",
                        input_schema=input_schema, output_schema=[{
                            **output_schema[0], "type": "string",
                        }])
    unknown = _promote(uid, key, title="Versioned", marker="unknown",
                       input_schema=[{"name": "event", "type": "string"}],
                       output_schema=output_schema)
    created = _create_with_transform(uid, first)
    canvas_id, transform_id = created["id"], created["nodeId"]
    doc = client.get(f"/api/canvas/{canvas_id}", headers=_headers(uid)).json()
    doc["nodes"][0]["parentId"] = "section"
    doc["nodes"].extend([
        {"id": "section", "type": "section", "position": {"x": 0, "y": 0},
         "data": {"title": "group", "status": "latest", "config": {}}},
        {"id": "sink", "type": "write", "position": {"x": 300, "y": 0},
         "data": {"title": "sink", "status": "latest", "config": {}}},
        {"id": "unrelated", "type": "note", "position": {"x": 0, "y": 300},
         "data": {"title": "note", "status": "latest", "config": {}}},
    ])
    doc["nodes"][0]["data"]["status"] = "latest"
    doc["edges"] = [{"id": "section-sink", "source": "section", "target": "sink"}]
    saved = client.put(
        f"/api/canvas/{canvas_id}", headers=_headers(uid), params={"expectedVersion": 1}, json=doc)
    assert saved.status_code == 200, saved.text

    upgraded = client.post(
        f"/api/workspace/canvases/{canvas_id}/transforms", headers=_headers(uid), json={
            "transformId": compatible["id"], "transformVersion": compatible["version"],
            "expectedCanvasVersion": 2, "replaceNodeId": transform_id,
        })
    assert upgraded.status_code == 200, upgraded.text
    result = upgraded.json()["doc"]
    statuses = {node["id"]: node["data"]["status"] for node in result["nodes"]}
    assert statuses[transform_id] == statuses["section"] == statuses["sink"] == "stale"
    assert statuses["unrelated"] == "latest"
    exact = next(node for node in result["nodes"] if node["id"] == transform_id)
    assert exact["data"]["config"]["version"] == compatible["version"]

    for candidate in (breaking, unknown):
        rejected = client.post(
            f"/api/workspace/canvases/{canvas_id}/transforms", headers=_headers(uid), json={
                "transformId": candidate["id"], "transformVersion": candidate["version"],
                "expectedCanvasVersion": 3, "replaceNodeId": transform_id,
            })
        assert rejected.status_code == 409
        assert "compatible input and output schemas" in rejected.json()["detail"]
    stale = client.post(
        f"/api/workspace/canvases/{canvas_id}/transforms", headers=_headers(uid), json={
            "transformId": first["id"], "transformVersion": first["version"],
            "expectedCanvasVersion": 2, "replaceNodeId": transform_id,
        })
    assert stale.status_code == 409
    persisted = client.get(f"/api/canvas/{canvas_id}", headers=_headers(uid)).json()
    pinned = next(node for node in persisted["nodes"] if node["id"] == transform_id)
    assert pinned["data"]["config"]["version"] == compatible["version"]


def test_canvas_reference_reports_missing_plugin_without_inventing_metadata() -> None:
    uid = _user("missing-ref")
    canvas_id = f"missing-{uuid.uuid4().hex}"
    doc = {"id": canvas_id, "name": "missing", "version": 1, "nodes": [{
        "id": "missing-node", "type": "transform", "position": {"x": 0, "y": 0},
        "data": {"config": {
            "source": "library", "processor": "plugin.no-longer-installed", "version": "v9",
        }},
    }], "edges": []}
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=uid, name="missing", version=1, doc=json.dumps(doc)))
    response = client.get(
        f"/api/canvas/{canvas_id}/transform-references", headers=_headers(uid))
    assert response.status_code == 200
    assert response.json() == [{
        "id": "plugin.no-longer-installed", "version": "v9",
        "nodeIds": ["missing-node"], "availability": "missing", "descriptor": None,
    }]
