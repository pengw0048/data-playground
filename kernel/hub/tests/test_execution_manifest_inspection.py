from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import quote

from fastapi.testclient import TestClient

from hub import auth, metadb
from hub.main import app


client = TestClient(app)
SECRET = "manifest-inspection-auth-secret-0123456789"


def _headers(uid: str) -> dict[str, str]:
    return {"Cookie": f"dp_session={auth.sign(uid)}"}


def _manifest(*, secret: bool = False, schema_version: int = 1) -> tuple[str, str]:
    doc = {
        "schemaVersion": schema_version,
        "graph": {
            "nodes": [{
                "id": "source", "type": "source",
                "data": {"config": {"api_key": "must-not-leak"} if secret else {}},
            }],
            "edges": [],
            "requirements": ["polars==1.0"],
        },
        "target": {"nodeId": "source", "portId": None},
        "admittedInputs": [{
            "nodeId": "source", "datasetId": "dataset-1",
            "revisionId": "revision-1", "provider": "local",
        }],
        "writeIntent": None,
        "descriptors": {
            "core": {"apiVersion": "1", "packageVersion": "test"},
            "nodes": [], "plugins": [],
        },
    }
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest(), payload


def _fixture(monkeypatch) -> tuple[str, dict[str, str]]:
    suffix = uuid.uuid4().hex
    identities = {
        "owner": f"manifest-owner-{suffix}",
        "editor": f"manifest-editor-{suffix}",
        "viewer": f"manifest-viewer-{suffix}",
        "stranger": f"manifest-stranger-{suffix}",
        "canvas": f"manifest-canvas-{suffix}",
    }
    digest, payload = _manifest()
    with metadb.session() as session:
        session.add_all([
            metadb.User(id=identities[key], name=key)
            for key in ("owner", "editor", "viewer", "stranger")
        ])
        session.add(metadb.Canvas(
            id=identities["canvas"], owner_id=identities["owner"], name="Manifest canvas"))
        session.flush()
        session.add_all([
            metadb.CanvasShare(
                canvas_id=identities["canvas"], user_id=identities["editor"], role="editor"),
            metadb.CanvasShare(
                canvas_id=identities["canvas"], user_id=identities["viewer"], role="viewer"),
        ])
        metadb._persist_execution_manifest(session, digest, payload)
    monkeypatch.setenv("DP_AUTH_SECRET", SECRET)
    client.cookies.clear()
    return digest, identities


def _history(
        canvas_id: str, identity: str, digest: str | None, *, job_type: str = "run") -> None:
    with metadb.session() as session:
        session.add(metadb.RunRecord(
            id=identity,
            canvas_id=canvas_id,
            run_id=f"logical-{identity}",
            target_node_id="source",
            target_port_id="out" if job_type == "profile" else None,
            job_type=job_type,
            status="failed",
            error="expected fixture failure",
            outputs="[]",
            execution_manifest_sha256=digest,
        ))


def _detail(canvas_id: str, subject_id: str, uid: str):
    return client.get(
        f"/api/canvas/{quote(canvas_id, safe='')}/runs/{quote(subject_id, safe='')}/manifest",
        headers=_headers(uid),
    )


def test_manifest_detail_reuses_subject_canvas_visibility_and_revocation(monkeypatch):
    digest, ids = _fixture(monkeypatch)
    _history(ids["canvas"], "history-visible", digest)

    for role in ("owner", "editor", "viewer"):
        response = _detail(ids["canvas"], "history-visible", ids[role])
        assert response.status_code == 200
        body = response.json()
        assert body["availability"] == "available"
        assert body["sha256"] == digest
        assert body["document"]["target"] == {"nodeId": "source", "portId": None}
        if role == "owner":
            # The detail path is DB-backed and survives a fresh connection pool; no runner cache or
            # live Canvas document participates in the next collaborator read.
            metadb._engine.dispose()
    assert _detail(ids["canvas"], "history-visible", ids["stranger"]).status_code == 404

    metadb.unshare_canvas(ids["canvas"], ids["viewer"])
    assert _detail(ids["canvas"], "history-visible", ids["viewer"]).status_code == 404


def test_manifest_detail_covers_profile_live_state_and_durable_task_subjects(monkeypatch):
    digest, ids = _fixture(monkeypatch)
    _history(ids["canvas"], "profile-history", digest, job_type="profile")
    with metadb.session() as session:
        session.add(metadb.RunState(
            run_id="current-run", canvas_id=ids["canvas"], status="queued",
            doc=json.dumps({"run_id": "current-run", "status": "queued"}),
            execution_manifest_sha256=digest,
        ))
        session.add(metadb.DurableTask(
            id="durable-task", owner_id=ids["owner"], canvas_id=ids["canvas"],
            submission_id=str(uuid.uuid4()), intent_sha256=digest,
            target_node_id="source", task_kind="managed_local_write",
            execution_manifest_sha256=digest, status="queued", status_doc="{}",
        ))

    for subject in ("profile-history", "s:current-run", "t:durable-task"):
        response = _detail(ids["canvas"], subject, ids["viewer"])
        assert response.status_code == 200
        assert response.json()["availability"] == "available"
        assert response.json()["sha256"] == digest


def test_manifest_detail_classifies_lifecycle_and_integrity_without_fallback(monkeypatch):
    digest, ids = _fixture(monkeypatch)
    _history(ids["canvas"], "legacy-history", None)
    _history(ids["canvas"], "pruned-history", digest)
    secret_digest, secret_payload = _manifest(secret=True)
    unsupported_digest, unsupported_payload = _manifest(schema_version=2)
    _history(ids["canvas"], "corrupt-history", secret_digest)
    _history(ids["canvas"], "unsupported-history", unsupported_digest)
    with metadb.session() as session:
        session.delete(session.get(metadb.ExecutionManifest, digest))
        session.add(metadb.ExecutionManifest(
            sha256=secret_digest, schema_version=1, semantic_doc=secret_payload))
        session.add(metadb.ExecutionManifest(
            sha256=unsupported_digest, schema_version=2, semantic_doc=unsupported_payload))

    expected = {
        "legacy-history": "not_recorded",
        "pruned-history": "pruned",
        "corrupt-history": "corrupt",
        "unsupported-history": "unavailable",
        "missing-history": "unavailable",
    }
    for subject, availability in expected.items():
        response = _detail(ids["canvas"], subject, ids["owner"])
        assert response.status_code == 200
        assert response.json()["availability"] == availability
        assert response.json()["document"] is None
        assert "must-not-leak" not in response.text

    listed = {row["id"]: row for row in metadb.list_runs(ids["canvas"])}
    assert listed["legacy-history"]["executionManifestAvailability"] == "not_recorded"
    assert listed["pruned-history"]["executionManifestAvailability"] == "pruned"
    # List reads intentionally do not parse documents; integrity failures become explicit only after
    # the authorized lazy detail read.
    assert listed["corrupt-history"]["executionManifestAvailability"] == "available"
    assert listed["unsupported-history"]["executionManifestAvailability"] == "unavailable"

    metadb.delete_canvas_cascade(ids["canvas"])
    assert _detail(ids["canvas"], "corrupt-history", ids["owner"]).status_code == 404


def test_history_and_jobs_lists_read_only_bounded_manifest_metadata(monkeypatch):
    digest, ids = _fixture(monkeypatch)
    _history(ids["canvas"], "bounded-history", digest)

    def must_not_validate(*_args, **_kwargs):
        raise AssertionError("list endpoints must not parse the manifest document")

    monkeypatch.setattr("hub.execution_manifest.validate_execution_manifest", must_not_validate)
    history = client.get(
        f"/api/canvas/{quote(ids['canvas'], safe='')}/runs",
        headers=_headers(ids["owner"]),
    )
    jobs = client.get("/api/jobs?limit=1", headers=_headers(ids["owner"]))

    assert history.status_code == 200
    assert jobs.status_code == 200
    for row in (history.json()[0], jobs.json()["items"][0]):
        assert row["executionManifestSha256"] == digest
        assert row["executionManifestSchemaVersion"] == 1
        assert row["executionManifestAvailability"] == "available"
        assert row["executionManifestReconstructable"] is True
        assert "document" not in row
