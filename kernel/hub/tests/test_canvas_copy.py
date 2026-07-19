from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from hub import canvas_copy, execution_manifest, metadb
from hub.deps import get_deps
from hub.main import app
from hub.models import Graph


client = TestClient(app)


def _user(prefix: str) -> str:
    uid = f"{prefix}-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name=prefix))
    return uid


def _canvas(owner: str, name: str = "Source") -> str:
    canvas_id = f"copy-source-{uuid.uuid4().hex}"
    doc = {
        "id": canvas_id, "name": name, "version": 1,
        "nodes": [], "edges": [], "requirements": ["packaging>=20"],
        "parameters": [{"name": "threshold", "type": "integer", "default": 4}],
    }
    with metadb.session() as session:
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=owner, name=name, version=1, doc=json.dumps(doc)))
        metadb._workspace_ensure_root_placement_in_session(
            session, target_kind="canvas", target_id=canvas_id, name=name)
    return canvas_id


def _share(canvas_id: str, uid: str, role: str = "viewer") -> None:
    with metadb.session() as session:
        session.add(metadb.CanvasShare(canvas_id=canvas_id, user_id=uid, role=role))


def _promoted_transform(owner: str) -> dict:
    response = client.post("/api/processors/promote", headers={"X-DP-User": owner}, json={
        "id": f"copy.transform-{uuid.uuid4().hex}",
        "title": "Retained exact Transform",
        "blurb": "Must remain authorized by the retained execution manifest.",
        "category": "compute",
        "mode": "map",
        "code": "def fn(row):\n    return row",
        "inputSchema": [{"name": "event", "type": "string"}],
        "inputColumns": ["event"],
        "outputSchema": [{"name": "event", "type": "string"}],
        "requirements": [],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _payload(source_id: str, *, version: int | None = 1,
             subject: str | None = None, name: str = "Independent copy") -> dict:
    root = metadb.local_workspace_root()
    result = {
        "copyId": str(uuid.uuid4()), "sourceCanvasId": source_id,
        "containerId": root["id"], "expectedContainerVersion": root["version"],
        "name": name,
    }
    if subject is not None:
        result["sourceSubjectId"] = subject
    else:
        result["sourceCanvasVersion"] = version
    return result


def _validate(payload: dict, uid: str):
    return client.post("/api/canvas/copy/validate", json=payload, headers={"X-DP-User": uid})


def _create(payload: dict, validation: dict, uid: str):
    return client.post("/api/canvas/copy", json={
        **payload,
        "copyIntentDigest": validation["copyIntentDigest"],
        "validationDigest": validation["validationDigest"],
        "confirmWarnings": True,
    }, headers={"X-DP-User": uid})


def test_viewer_copy_is_private_atomic_and_idempotent_after_source_deletion():
    owner, viewer = _user("copy-owner"), _user("copy-viewer")
    source_id = _canvas(owner)
    _share(source_id, viewer)
    payload = _payload(source_id)

    checked = _validate(payload, viewer)
    assert checked.status_code == 200
    validation = checked.json()
    assert validation["canImport"] is True
    created = _create(payload, validation, viewer)
    assert created.status_code == 200
    created_id = created.json()["id"]

    with metadb.session() as session:
        copy = session.get(metadb.Canvas, created_id)
        document = json.loads(copy.doc)
        placement = session.scalar(metadb.select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "canvas",
            metadb.WorkspacePlacement.target_id == created_id))
        assert copy.owner_id == viewer
        assert copy.visibility == "private"
        assert document["parameters"][0]["name"] == "threshold"
        assert document["_copiedFrom"] == {
            "kind": "canvas", "canvasId": source_id, "canvasVersion": 1,
        }
        assert document["_copyIntent"]["digest"] == validation["copyIntentDigest"]
        assert placement.container_id == metadb.LOCAL_WORKSPACE_ROOT_ID
        assert session.scalar(metadb.select(metadb.CanvasShare.id).where(
            metadb.CanvasShare.canvas_id == created_id)) is None

    metadb.delete_canvas_cascade(source_id)
    replayed = _create(payload, validation, viewer)
    assert replayed.status_code == 200
    assert replayed.json() == {
        "ok": True, "id": created_id, "created": False, "replayed": True,
    }
    collision = _create({**payload, "name": "Changed intent"}, validation, viewer)
    assert collision.status_code == 409


def test_current_copy_fails_closed_when_source_changes_after_validation():
    owner = _user("copy-cas-owner")
    source_id = _canvas(owner)
    payload = _payload(source_id)
    validation = _validate(payload, owner).json()
    with metadb.session() as session:
        source = session.get(metadb.Canvas, source_id)
        source.version = 2
        document = json.loads(source.doc)
        document["version"] = 2
        source.doc = json.dumps(document)

    response = _create(payload, validation, owner)
    assert response.status_code == 409
    copied_id = canvas_copy.canvas_id(owner, payload["copyId"])
    with metadb.session() as session:
        assert session.get(metadb.Canvas, copied_id) is None


def test_authorized_retained_manifest_clone_uses_exact_definition_without_live_fallback():
    owner, viewer, stranger = (_user("manifest-copy-owner"), _user("manifest-copy-viewer"),
                               _user("manifest-copy-stranger"))
    source_id = _canvas(owner, "Historical source")
    _share(source_id, viewer)
    graph = Graph.model_validate({
        "id": source_id, "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 20, "y": 30},
            "data": {"status": "latest", "config": {}},
        }],
        "edges": [], "requirements": ["polars>=1"],
    })
    digest, manifest_doc = execution_manifest.build_execution_manifest(
        graph, target_node_id="source", target_port_id=None,
        input_manifest=[{
            "node_id": "source", "dataset_id": "dataset-exact",
            "revision_id": "revision-exact", "provider": "local",
            "resolved_at": "2026-01-01T00:00:00Z",
        }], write_intent=None, deps=get_deps())
    subject = f"history-{uuid.uuid4().hex}"
    with metadb.session() as session:
        metadb._persist_execution_manifest(session, digest, manifest_doc)
        session.add(metadb.RunRecord(
            id=subject, canvas_id=source_id, run_id=f"run-{subject}",
            target_node_id="source", job_type="run", status="failed", outputs="[]",
            execution_manifest_sha256=digest))
    payload = _payload(source_id, version=None, subject=subject, name="Historical clone")

    assert _validate(payload, stranger).status_code == 404
    checked = _validate(payload, viewer)
    assert checked.status_code == 200
    assert any(item["code"] == "data_unavailable" for item in checked.json()["diagnostics"])
    created = _create(payload, checked.json(), viewer)
    assert created.status_code == 200
    created_id = created.json()["id"]
    with metadb.session() as session:
        document = json.loads(session.get(metadb.Canvas, created_id).doc)
    assert document["nodes"][0]["data"]["config"]["datasetRef"] == {
        "kind": "exact", "datasetId": "dataset-exact", "revisionId": "revision-exact",
    }
    assert document["nodes"][0]["data"]["status"] == "draft"
    assert document["requirements"] == ["polars>=1"]
    assert document["_copiedFrom"] == {
        "kind": "executionManifest", "canvasId": source_id,
        "subjectId": subject, "sha256": digest,
    }

    metadb.delete_canvas_cascade(source_id)
    assert _create(payload, checked.json(), viewer).json()["id"] == created_id


def test_retained_manifest_transform_authorization_survives_live_canvas_removal():
    owner, viewer, stranger = (_user("manifest-transform-owner"),
                               _user("manifest-transform-viewer"),
                               _user("manifest-transform-stranger"))
    source_id = _canvas(owner, "Retained Transform source")
    _share(source_id, viewer)
    promoted = _promoted_transform(owner)
    graph_doc = {
        "id": source_id, "name": "Retained Transform source", "version": 1,
        "nodes": [{
            "id": "transform", "type": "transform", "position": {"x": 20, "y": 30},
            "data": {"config": {
                "source": "library", "processor": promoted["id"],
                "version": promoted["version"], "mode": "map",
            }},
        }],
        "edges": [], "requirements": [], "parameters": [],
    }
    digest, manifest_doc = execution_manifest.build_execution_manifest(
        Graph.model_validate(graph_doc), target_node_id="transform", target_port_id=None,
        input_manifest=None, write_intent=None, deps=get_deps())
    subject = f"history-{uuid.uuid4().hex}"
    with metadb.session() as session:
        source = session.get(metadb.Canvas, source_id)
        source.doc = json.dumps(graph_doc)
        metadb._replace_promoted_transform_refs(session, "canvas", source_id, graph_doc)
        metadb._persist_execution_manifest(session, digest, manifest_doc)
        session.add(metadb.RunRecord(
            id=subject, canvas_id=source_id, run_id=f"run-{subject}",
            target_node_id="transform", job_type="run", status="failed", outputs="[]",
            execution_manifest_sha256=digest))

    # The immutable manifest is now the only authorized hold for this exact Transform version.
    with metadb.session() as session:
        source = session.get(metadb.Canvas, source_id)
        empty = {**graph_doc, "version": 2, "nodes": [], "edges": []}
        metadb._replace_promoted_transform_refs(session, "canvas", source_id, empty)
        source.doc = json.dumps(empty)
        source.version = 2
        assert session.scalar(metadb.select(
            metadb.PromotedTransformVersionRef.owner_key,
        ).where(
            metadb.PromotedTransformVersionRef.owner_kind == "canvas",
            metadb.PromotedTransformVersionRef.owner_key == source_id,
        ).limit(1)) is None
        assert session.scalar(metadb.select(
            metadb.PromotedTransformVersionRef.owner_key,
        ).where(
            metadb.PromotedTransformVersionRef.owner_kind == "execution_manifest",
            metadb.PromotedTransformVersionRef.owner_key == digest,
        ).limit(1)) == digest

    payload = _payload(source_id, version=None, subject=subject, name="Retained Transform clone")
    hidden = _validate(payload, stranger)
    assert hidden.status_code == 404
    assert promoted["title"] not in hidden.text

    checked = _validate(payload, viewer)
    assert checked.status_code == 200, checked.text
    created = _create(payload, checked.json(), viewer)
    assert created.status_code == 200, created.text
    with metadb.session() as session:
        copied = json.loads(session.get(metadb.Canvas, created.json()["id"]).doc)
    config = copied["nodes"][0]["data"]["config"]
    assert (config["processor"], config["version"]) == (
        promoted["id"], promoted["version"])

    # A visible subject without its exact durable manifest hold cannot borrow the live or copied
    # Canvas capability, and the rejection happens before descriptor metadata is returned.
    with metadb.session() as session:
        metadb._drop_promoted_transform_refs(session, "execution_manifest", digest)
    denied = _validate({**payload, "copyId": str(uuid.uuid4())}, viewer)
    assert denied.status_code == 403
    assert promoted["title"] not in denied.text


def test_manifest_copy_removes_registered_secret_references():
    document = {
        "graph": {
            "nodes": [{"id": "source", "type": "source", "data": {
                "config": {"uri": "s3://bucket/data", "accessKeyId": "env:DP_TEST_KEY"},
            }}],
            "edges": [], "requirements": [],
        },
    }
    canvas, removed = canvas_copy.prepare_manifest(document, "Safe clone")
    assert removed == 1
    assert "accessKeyId" not in canvas["nodes"][0]["data"]["config"]
