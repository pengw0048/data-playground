"""Durable exact-version contract for promoted Transforms."""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub import metadb
from hub.deps import get_deps
from hub.execution_manifest import build_execution_manifest
from hub.main import app
from hub.models import ColumnSchema, Graph
from hub.plugins.processors import ProcessorRegistry, RegisteredProcessor
from hub.settings import settings


client = TestClient(app)


def _user(prefix: str) -> str:
    uid = f"{prefix}-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name=prefix))
    return uid


def _headers(uid: str) -> dict[str, str]:
    return {"X-DP-User": uid}


def _promotion(
        uid: str, *, key: str | None = None, title: str = "Flag rows",
        code: str = "def fn(row):\n    row['flagged'] = 'old'\n    return row",
        requirements: list[str] | None = None,
        blurb: str = "A durable transform") -> dict:
    response = client.post("/api/processors/promote", headers=_headers(uid), json={
        "id": key or f"user.flag-{uuid.uuid4().hex}",
        "title": title,
        "blurb": blurb,
        "category": "compute",
        "mode": "map",
        "code": code,
        "inputSchema": [{"name": "event", "type": "string"}],
        "inputColumns": ["event"],
        "outputSchema": [{"name": "flagged", "type": "string"}],
        "requirements": requirements or [],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _graph(canvas_id: str, descriptor: dict, *, inline_code: str | None = None) -> dict:
    uri = get_deps().catalog.get_table("tbl_events").uri
    return {
        "id": canvas_id,
        "name": "promoted exact",
        "version": 1,
        "requirements": descriptor.get("requirements", []),
        "nodes": [
            {
                "id": "source",
                "type": "source",
                "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": uri}},
            },
            {
                "id": "transform",
                "type": "transform",
                "position": {"x": 100, "y": 0},
                "data": {"config": {
                    "source": "library",
                    "processor": descriptor["id"],
                    "version": descriptor["version"],
                    "mode": "map",
                    "code": inline_code,
                }},
            },
        ],
        "edges": [{
            "id": "source-transform",
            "source": "source",
            "target": "transform",
            "data": {"wire": "dataset"},
        }],
    }


def _poll_run(uid: str, run_id: str, tries: int = 400) -> dict:
    status: dict = {}
    for _ in range(tries):
        response = client.get(f"/api/run/{run_id}", headers=_headers(uid))
        assert response.status_code == 200, response.text
        status = response.json()
        if status["status"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.1)
    return status


def test_promotion_is_durable_idempotent_versioned_and_exact() -> None:
    uid = _user("promoted-owner")
    key = f"user.durable-{uuid.uuid4().hex}"
    first = _promotion(uid, key=key, requirements=["pyarrow", "numpy"])
    replay = _promotion(uid, key=key, requirements=["numpy", "pyarrow", "numpy"])
    assert replay == first
    assert first["id"].startswith("tr_") and len(first["id"]) == 32
    assert first["version"] == "v1"
    assert first["provenance"] == "promoted"
    assert len(first["semanticDigest"]) == 64
    assert "code" not in first

    metadata_edit = _promotion(
        uid, key=key, requirements=["numpy", "pyarrow"], blurb="Edited immutable metadata")
    code_edit = _promotion(
        uid, key=key, requirements=["numpy", "pyarrow"],
        blurb="Edited immutable metadata",
        code="def fn(row):\n    row['flagged'] = 'new'\n    return row",
    )
    assert (metadata_edit["version"], code_edit["version"]) == ("v2", "v3")
    assert len({first["semanticDigest"], metadata_edit["semanticDigest"],
                code_edit["semanticDigest"]}) == 3

    # A fresh registry instance after a simulated hub restart reopens durable code, not process memory.
    restarted = ProcessorRegistry().get(first["id"], first["version"])
    assert restarted.code and "'old'" in restarted.code
    old_result = client.post(
        "/api/run/preview", headers=_headers(uid),
        json={"graph": _graph(f"adhoc-{uuid.uuid4().hex}", first), "nodeId": "transform", "k": 3},
    )
    new_result = client.post(
        "/api/run/preview", headers=_headers(uid),
        json={"graph": _graph(f"adhoc-{uuid.uuid4().hex}", code_edit), "nodeId": "transform", "k": 3},
    )
    assert old_result.status_code == new_result.status_code == 200
    assert {row["flagged"] for row in old_result.json()["rows"]} == {"old"}
    assert {row["flagged"] for row in new_result.json()["rows"]} == {"new"}


def test_same_promotion_converges_after_duplicate_concurrent_requests() -> None:
    uid = _user("promoted-race")
    key = f"user.concurrent-{uuid.uuid4().hex}"
    definition = {
        "owner_id": uid,
        "key": key,
        "title": "Concurrent",
        "blurb": "same response-loss retry",
        "category": "compute",
        "mode": "map",
        "code": "def fn(row):\n    return row",
        "input_schema": [ColumnSchema(name="event", type="string")],
        "output_schema": [],
        "requirements": ["numpy"],
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(
            lambda _index: metadb.promote_transform(**definition), range(16)))
    assert {(item["id"], item["version"], item["semantic_digest"]) for item in versions} == {
        (versions[0]["id"], "v1", versions[0]["semantic_digest"]),
    }
    with metadb.session() as session:
        count = session.query(metadb.PromotedTransformVersion).filter_by(
            transform_id=versions[0]["id"]).count()
    assert count == 1


def test_promoted_exact_version_runs_across_metadata_isolated_subprocess() -> None:
    uid = _user("promoted-subprocess")
    promoted = _promotion(uid, code=(
        "def fn(row):\n    row['isolated_marker'] = 'exact-v1'\n    return row"))
    graph = _graph(f"promoted-subprocess-{uuid.uuid4().hex}", promoted)
    created = client.post("/api/canvas", headers=_headers(uid), json=graph)
    assert created.status_code == 200, created.text
    metadb.set_setting("backend", "local-subprocess", "user", uid)
    try:
        started = client.post("/api/run", headers=_headers(uid), json={
            "graph": graph,
            "targetNodeId": "transform",
            "confirmed": True,
            "submissionId": str(uuid.uuid4()),
        })
        assert started.status_code == 200, started.text
        status = _poll_run(uid, started.json()["runId"])
        assert status["status"] == "done", status.get("error")
        assert status["totalRows"] and status["totalRows"] > 0
        assert len(status["outputs"]) == 1
        output = status["outputs"][0]
        sampled = client.post(
            f"/api/run/{started.json()['runId']}/sample", headers=_headers(uid), json={
                "nodeId": output["nodeId"], "portId": output["portId"], "k": 5, "offset": 0,
            },
        )
        assert sampled.status_code == 200, sampled.text
        assert {row["isolated_marker"] for row in sampled.json()["rows"]} == {"exact-v1"}

        with metadb.session() as session:
            state = session.get(metadb.RunState, started.json()["runId"])
            assert state is not None and state.execution_manifest_sha256
            manifest = session.get(
                metadb.ExecutionManifest, state.execution_manifest_sha256)
            assert manifest is not None
            assert "exact-v1" not in manifest.semantic_doc
            assert "_promotedTransformDefinitions" not in manifest.semantic_doc
        public_docs = json.dumps({"start": started.json(), "status": status})
        assert "exact-v1" not in public_docs
        assert "_promotedTransformDefinitions" not in public_docs

        estimate = client.post(
            "/api/run/profile-estimate", headers=_headers(uid),
            json={"graph": graph, "nodeId": "transform"},
        )
        assert estimate.status_code == 200, estimate.text
        profile_started = client.post(
            "/api/run/profile-job", headers=_headers(uid), json={
                "graph": graph,
                "nodeId": "transform",
                "planDigest": estimate.json()["planDigest"],
                "submissionId": str(uuid.uuid4()),
                "confirmed": True,
            },
        )
        assert profile_started.status_code == 200, profile_started.text
        profile_status = _poll_run(uid, profile_started.json()["runId"])
        assert profile_status["status"] == "done", profile_status.get("error")
        assert profile_status["profile"]["rowCount"] > 0
    finally:
        metadb.set_setting("backend", "", "user", uid)


def test_promoted_workload_sidecar_rejects_missing_extra_and_tampered_definitions() -> None:
    from hub.workload_env import prepare_workload_graph, restore_workload_graph

    uid = _user("promoted-sidecar")
    promoted = _promotion(uid)
    graph = Graph.model_validate(_graph(f"promoted-sidecar-{uuid.uuid4().hex}", promoted))
    payload = prepare_workload_graph(graph, "transform", get_deps().registry)
    sidecar_key = "_promotedTransformDefinitions"
    assert len(payload[sidecar_key]) == 1
    restored = restore_workload_graph(json.loads(json.dumps(payload)), "transform")
    restored_config = next(
        node.data["config"] for node in restored.nodes if node.id == "transform")
    assert restored_config["code"].endswith("return row")
    assert "source" not in restored_config
    original_config = next(
        node.data["config"] for node in graph.nodes if node.id == "transform")
    assert original_config["source"] == "library" and not original_config.get("code")

    missing = json.loads(json.dumps(payload))
    missing.pop(sidecar_key)
    extra = json.loads(json.dumps(payload))
    extra[sidecar_key].append(dict(extra[sidecar_key][0]))
    bad_id = json.loads(json.dumps(payload))
    replacement_id = "tr_" + ("0" if promoted["id"] != "tr_" + "0" * 29 else "1") * 29
    bad_id[sidecar_key][0]["id"] = replacement_id
    bad_version = json.loads(json.dumps(payload))
    bad_version[sidecar_key][0]["version"] = "v2"
    bad_digest = json.loads(json.dumps(payload))
    bad_digest[sidecar_key][0]["semanticDigest"] = "0" * 64
    bad_code = json.loads(json.dumps(payload))
    bad_code[sidecar_key][0]["code"] += "\n# tampered"
    for malformed in (missing, extra, bad_id, bad_version, bad_digest, bad_code):
        with pytest.raises(RuntimeError):
            restore_workload_graph(malformed, "transform")


def test_canvas_ref_removal_and_version_delete_share_one_lock_order() -> None:
    """Executable on SQLite and the PostgreSQL lifecycle job via DP_TEST_DATABASE_URL."""
    uid = _user("promoted-ref-race")
    promoted = _promotion(uid)
    canvas_id = f"promoted-ref-race-{uuid.uuid4().hex}"
    graph = _graph(canvas_id, promoted)
    assert client.post("/api/canvas", headers=_headers(uid), json=graph).status_code == 200
    empty = {**graph, "nodes": [], "edges": [], "version": 2}
    barrier = threading.Barrier(2)

    def remove_ref() -> str:
        barrier.wait()
        with metadb.session() as session:
            canvas = session.get(metadb.Canvas, canvas_id, with_for_update=True)
            assert canvas is not None
            metadb._replace_promoted_transform_refs(session, "canvas", canvas_id, empty)
            canvas.doc = json.dumps(empty)
            canvas.version = 2
        return "removed"

    def delete_version() -> str:
        barrier.wait()
        try:
            metadb.delete_promoted_transform_version(
                uid, promoted["id"], promoted["version"])
        except ValueError:
            return "retained"
        return "deleted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        remove_future = pool.submit(remove_ref)
        delete_future = pool.submit(delete_version)
        assert remove_future.result(timeout=10) == "removed"
        delete_result = delete_future.result(timeout=10)
    with metadb.session() as session:
        refs = list(session.scalars(metadb.select(
            metadb.PromotedTransformVersionRef).where(
                metadb.PromotedTransformVersionRef.owner_kind == "canvas",
                metadb.PromotedTransformVersionRef.owner_key == canvas_id,
            )))
        canvas = session.get(metadb.Canvas, canvas_id)
        assert canvas is not None and not metadb._promoted_transform_refs(canvas.doc)
    assert refs == []
    if delete_result == "deleted":
        assert metadb.promoted_transform_version(
            promoted["id"], promoted["version"]) is None
    else:
        # The deletion legitimately observed the old hold first; after the replacement commits, the
        # exact version is now unreferenced and a normal delete must succeed without a retry framework.
        assert metadb.delete_promoted_transform_version(
            uid, promoted["id"], promoted["version"])["deleted"] is True


def test_owner_scope_plugin_namespace_and_canvas_capability_authorization() -> None:
    owner, collaborator = _user("transform-owner"), _user("transform-collaborator")
    promoted = _promotion(owner)
    assert promoted["id"] not in {
        item["id"] for item in client.get(
            "/api/processors", headers=_headers(collaborator)).json()
    }
    registry = ProcessorRegistry()
    try:
        registry.register(RegisteredProcessor(
            id=promoted["id"], title="shadow", mode="map"))
    except ValueError as exc:
        assert "reserved promoted Transform namespace" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("plugin shadowed the promoted Transform namespace")

    forged = _graph(f"forged-{uuid.uuid4().hex}", promoted)
    denied = client.post(
        "/api/run/preview", headers=_headers(collaborator),
        json={"graph": forged, "nodeId": "transform", "k": 1},
    )
    assert denied.status_code == 403
    denied_save = client.post("/api/canvas", headers=_headers(collaborator), json=forged)
    assert denied_save.status_code == 403

    shared = _graph(f"shared-{uuid.uuid4().hex}", promoted)
    assert client.post("/api/canvas", headers=_headers(owner), json=shared).status_code == 200
    assert client.post(
        f"/api/canvas/{shared['id']}/share", headers=_headers(owner),
        json={"userId": collaborator, "role": "editor"},
    ).status_code == 200
    allowed = client.post(
        "/api/run/preview", headers=_headers(collaborator),
        json={"graph": shared, "nodeId": "transform", "k": 1},
    )
    assert allowed.status_code == 200
    assert allowed.json()["notPreviewable"] is False


def test_canvas_snapshot_and_manifest_holds_block_deletion_then_release() -> None:
    uid = _user("transform-retention")
    promoted = _promotion(uid)
    canvas = _graph(f"retained-{uuid.uuid4().hex}", promoted)
    assert client.post("/api/canvas", headers=_headers(uid), json=canvas).status_code == 200
    blocked = client.delete(
        f"/api/processors/{promoted['id']}/versions/{promoted['version']}",
        headers=_headers(uid),
    )
    assert blocked.status_code == 409 and "canvas" in blocked.text

    assert metadb.snapshot_canvas(
        canvas["id"], json.dumps(canvas), 1, author_id=uid,
        label="retained exact Transform",
    )
    empty = {**canvas, "nodes": [], "edges": []}
    saved = client.put(
        f"/api/canvas/{canvas['id']}?expectedVersion=1",
        headers=_headers(uid), json=empty,
    )
    assert saved.status_code == 200, saved.text
    snapshot_hold = client.delete(
        f"/api/processors/{promoted['id']}/versions/{promoted['version']}",
        headers=_headers(uid),
    )
    assert snapshot_hold.status_code == 409 and "canvas_version" in snapshot_hold.text
    assert client.delete(f"/api/canvas/{canvas['id']}", headers=_headers(uid)).status_code == 200

    # A retained execution manifest independently owns the exact definition after the live Canvas is gone.
    graph = Graph.model_validate(_graph(f"manifest-{uuid.uuid4().hex}", promoted))
    digest, document = build_execution_manifest(
        graph, target_node_id="transform", target_port_id=None,
        input_manifest=None, write_intent=None, deps=get_deps())
    with metadb.session() as session:
        metadb._persist_execution_manifest(session, digest, document)
        session.add(metadb.RunInputAdmission(
            run_id=f"manifest-run-{uuid.uuid4().hex}", creator_id=uid,
            canvas_id=None, submission_id=uuid.uuid4().hex,
            target_node_id="transform", intent_sha256=digest, manifest="[]",
            execution_manifest_sha256=digest,
        ))
    blocked_manifest = client.delete(
        f"/api/processors/{promoted['id']}/versions/{promoted['version']}",
        headers=_headers(uid),
    )
    assert blocked_manifest.status_code == 409 and "execution_manifest" in blocked_manifest.text
    with metadb.session() as session:
        admission = session.scalar(metadb.select(metadb.RunInputAdmission).where(
            metadb.RunInputAdmission.execution_manifest_sha256 == digest))
        assert admission is not None
        session.delete(admission)
        session.flush()
        metadb._delete_unreferenced_execution_manifests(session, {digest})
    deleted = client.delete(
        f"/api/processors/{promoted['id']}/versions/{promoted['version']}",
        headers=_headers(uid),
    )
    assert deleted.status_code == 200

    stale = _graph(f"stale-{uuid.uuid4().hex}", promoted, inline_code=(
        "def fn(row):\n    row['flagged'] = 'fallback-must-not-run'\n    return row"))
    unavailable = client.post(
        "/api/run/preview", headers=_headers(uid),
        json={"graph": stale, "nodeId": "transform", "k": 1},
    )
    assert unavailable.status_code == 200
    assert unavailable.json()["notPreviewable"] is True
    assert f"exact version '{promoted['version']}' is unavailable" in unavailable.json()["reason"]

    for alias in ("1", "v01", "+1"):
        malformed = _graph(f"malformed-{uuid.uuid4().hex}", {**promoted, "version": alias})
        result = client.post(
            "/api/run/preview", headers=_headers(uid),
            json={"graph": malformed, "nodeId": "transform", "k": 1},
        )
        assert result.status_code == 200
        assert result.json()["notPreviewable"] is True
        assert f"exact version '{alias}' is unavailable" in result.json()["reason"]


def test_promotion_bounds_and_response_redaction() -> None:
    uid = _user("transform-bounds")
    secret = f"literal-secret-{uuid.uuid4().hex}"
    descriptor = _promotion(
        uid, code=f"def fn(row):\n    marker = '{secret}'\n    return row")
    assert secret not in json.dumps(descriptor)
    too_large = client.post("/api/processors/promote", headers=_headers(uid), json={
        "id": "user.too-large",
        "title": "Too large",
        "mode": "map",
        "code": "x" * 200_001,
    })
    assert too_large.status_code == 422


@contextlib.contextmanager
def _isolated_metadata(url: str):
    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = url
    metadb._engine = metadb._Session = None
    try:
        metadb.migrate_db()
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def test_sqlite_backup_restore_includes_promoted_logical_and_version_store(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    transform_id = ""
    with _isolated_metadata(f"sqlite:///{source}"):
        with metadb.session() as session:
            session.add(metadb.User(id="backup-owner", name="Backup owner"))
        item = metadb.promote_transform(
            owner_id="backup-owner", key="user.backup", title="Backup transform",
            blurb="survives DB backup", category="compute", mode="map",
            code="def fn(row):\n    return row",
            input_schema=[], output_schema=[], requirements=[])
        transform_id = item["id"]
        metadb._engine.dispose()
        metadb._engine = metadb._Session = None
        shutil.copy2(source, backup)
    shutil.copy2(backup, restored)
    with _isolated_metadata(f"sqlite:///{restored}"):
        reopened = metadb.promoted_transform_version(transform_id, "v1")
        assert reopened is not None
        assert reopened["title"] == "Backup transform"
        assert "return row" in reopened["code"]
