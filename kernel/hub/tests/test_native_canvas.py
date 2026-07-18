"""Native Canvas export/import contract: bounded, explicit and replay-safe."""

from __future__ import annotations

import concurrent.futures
import json
import threading
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from hub import metadb, native_canvas
from hub.deps import get_deps
from hub.main import app
from hub.plugins.processors import RegisteredProcessor


OWNER = "native-canvas-owner"
VIEWER = "native-canvas-viewer"


def _doc(canvas_id: str, *, dataset_ref: dict | None = None) -> dict:
    config = {"datasetRef": dataset_ref} if dataset_ref is not None else {"uri": "events"}
    return {
        "id": canvas_id, "name": "portable report", "version": 4,
        "requirements": ["requests>=2"], "parameters": [
            {"name": "limit", "type": "integer", "default": 10},
        ],
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0}, "data": {
            "title": "source", "status": "latest", "lastRun": {"rows": 4}, "config": config,
        }}],
        "edges": [],
    }


def _seed(canvas_id: str, doc: dict) -> None:
    metadb.delete_canvas_cascade(canvas_id)
    with metadb.session() as s:
        for uid in (OWNER, VIEWER):
            if s.get(metadb.User, uid) is None:
                s.add(metadb.User(id=uid, name=uid))
        s.add(metadb.Canvas(id=canvas_id, owner_id=OWNER, name=doc["name"], version=4,
                            doc=json.dumps(doc)))
    metadb.share_canvas(canvas_id, VIEWER, "viewer")


def test_export_is_viewer_readable_and_omits_identity_and_run_history():
    canvas_id = "native-export-viewer"
    _seed(canvas_id, _doc(canvas_id))
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/canvas/{canvas_id}/native-export", headers={"X-DP-User": VIEWER})
        assert response.status_code == 200
        assert response.headers["content-disposition"].endswith('portable-report.dp-canvas.json"')
        envelope = response.json()
        assert envelope["format"] == native_canvas.FORMAT
        assert envelope["canvas"].get("id") is None
        assert envelope["canvas"]["nodes"][0]["data"]["status"] == "draft"
        assert "lastRun" not in envelope["canvas"]["nodes"][0]["data"]
    finally:
        metadb.delete_canvas_cascade(canvas_id)


def test_import_warns_for_missing_data_creates_once_and_replays_after_response_loss():
    source_id = "native-import-source"
    _seed(source_id, _doc(source_id, dataset_ref={
        "kind": "exact", "datasetId": "missing-dataset", "revisionId": "missing-revision",
    }))
    try:
        envelope = native_canvas.export_envelope(_doc(source_id, dataset_ref={
            "kind": "exact", "datasetId": "missing-dataset", "revisionId": "missing-revision",
        }), get_deps())
        payload = {"filename": "portable.dp-canvas.json", "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed3", "envelope": envelope}
        with TestClient(app) as client:
            checked = client.post("/api/canvas/native-import/validate", json=payload, headers={"X-DP-User": OWNER})
            assert checked.status_code == 200
            assert checked.json()["canImport"] is True
            assert checked.json()["requiresConfirmation"] is True
            digest = checked.json()["validationDigest"]
            assert any(item["code"] == "data_unavailable" for item in checked.json()["diagnostics"])
            unconfirmed = client.post(
                "/api/canvas/native-import",
                json={**payload, "validationDigest": digest, "confirmWarnings": False},
                headers={"X-DP-User": OWNER})
            assert unconfirmed.status_code == 409
            created = client.post("/api/canvas/native-import", json={**payload, "validationDigest": digest, "confirmWarnings": True}, headers={"X-DP-User": OWNER})
            assert created.status_code == 200 and created.json()["created"] is True
            replayed = client.post("/api/canvas/native-import", json={**payload, "validationDigest": digest, "confirmWarnings": True}, headers={"X-DP-User": OWNER})
            assert replayed.status_code == 200
            assert replayed.json() == {"ok": True, "id": created.json()["id"], "created": False, "replayed": True}
            imported = client.get(f"/api/canvas/{created.json()['id']}", headers={"X-DP-User": OWNER})
            assert imported.status_code == 200
            assert imported.json()["id"] != source_id and imported.json()["version"] == 1
            assert imported.json()["name"] == "portable report"
    finally:
        metadb.delete_canvas_cascade(source_id)
        metadb.delete_canvas_cascade(native_canvas.import_canvas_id(OWNER, "018f5d64-bce4-7f1b-9b9f-5035482e2ed3"))


def test_validation_rejects_filename_mismatch_missing_node_and_secret_bearing_export():
    envelope = native_canvas.export_envelope(_doc("native-bad"), get_deps())
    payload = {"filename": "wrong.json", "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed3", "envelope": envelope}
    with TestClient(app) as client:
        mismatch = client.post("/api/canvas/native-import/validate", json=payload, headers={"X-DP-User": OWNER})
        assert mismatch.status_code == 422
        broken = json.loads(json.dumps(envelope))
        broken["canvas"]["nodes"][0]["type"] = "missing-plugin-node"
        broken["descriptors"]["nodes"][0]["kind"] = "missing-plugin-node"
        missing = client.post("/api/canvas/native-import/validate", json={**payload, "filename": "ok.dp-canvas.json", "envelope": broken}, headers={"X-DP-User": OWNER})
        assert missing.status_code == 200
        assert missing.json()["canImport"] is False
        assert any(item["code"] == "missing_node" for item in missing.json()["diagnostics"])
    secret_doc = _doc("native-secret")
    secret_doc["nodes"][0]["data"]["config"] = {"apiKey": "not-exportable"}
    try:
        native_canvas.export_envelope(secret_doc, get_deps())
    except native_canvas.NativeCanvasError as exc:
        assert "credential" in str(exc)
    else:  # pragma: no cover - makes a secret-export regression unmistakable
        raise AssertionError("credential-bearing Canvas exported")
    output_doc = _doc("native-output")
    output_doc["nodes"][0]["data"]["config"] = {"uri": "/workspace/.dp-results/__result_run.parquet"}
    try:
        native_canvas.export_envelope(output_doc, get_deps())
    except native_canvas.NativeCanvasError as exc:
        assert "outputs" in str(exc)
    else:  # pragma: no cover - makes an output-export regression unmistakable
        raise AssertionError("local run output exported")


def test_validation_rejects_old_new_versions_and_oversized_raw_upload():
    envelope = native_canvas.export_envelope(_doc("native-version"), get_deps())
    payload = {"filename": "version.dp-canvas.json", "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed3", "envelope": envelope}
    with TestClient(app) as client:
        for version in (0, native_canvas.VERSION + 1):
            changed = json.loads(json.dumps(envelope))
            changed["version"] = version
            response = client.post("/api/canvas/native-import/validate", json={**payload, "envelope": changed}, headers={"X-DP-User": OWNER})
            assert response.status_code == 422
        oversized = client.post("/api/canvas/native-import/validate", content=b"x" * (native_canvas.MAX_BYTES + 8193), headers={"X-DP-User": OWNER, "Content-Type": "application/json"})
        assert oversized.status_code == 413


@pytest.mark.parametrize("field", [
    "accessToken", "access_token", "authToken", "auth_token", "authorization",
    "bearerToken", "bearer_token", "credential", "credentials",
])
def test_export_reuses_manifest_secret_guard_for_sensitive_fields(field: str):
    doc = _doc(f"native-secret-{field}")
    doc["nodes"][0]["data"]["config"] = {field: "material-secret"}
    with pytest.raises(native_canvas.NativeCanvasError, match="credentials"):
        native_canvas.export_envelope(doc, get_deps())


@pytest.mark.parametrize("uri", [
    "https://example.test/data?access_token=secret",
    "https://example.test/data?X-Amz-Signature=secret",
    "https://example.test/data?X-Amz-Credential=secret",
    "https://example.test/data?X-Amz-Security-Token=secret",
    "https://example.test/data?client_secret=secret",
    "https://example.test/data?id_token=secret",
    "https://example.test/data?refresh_token=secret",
    "https://example.test/data?sig=secret",
    "https://example.test/callback#access_token=secret",
    "https://example.test/callback#/done?auth_token=secret",
])
def test_export_rejects_credential_material_in_uri_query_and_fragment(uri: str):
    doc = _doc("native-uri-secret")
    doc["nodes"][0]["data"]["config"] = {"uri": uri}
    with pytest.raises(native_canvas.NativeCanvasError, match="credentials"):
        native_canvas.export_envelope(doc, get_deps())


def test_export_allows_noncredential_access_auth_and_bearer_query_fields():
    doc = _doc("native-uri-ordinary-fields")
    uri = "https://example.test/data?access=public&auth=none&bearer=disabled"
    doc["nodes"][0]["data"]["config"] = {"uri": uri}
    assert native_canvas.export_envelope(doc, get_deps())["canvas"]["nodes"][0][
        "data"]["config"]["uri"] == uri


def test_export_rejects_credentials_in_direct_requirement_urls_and_accepts_pep508():
    valid = _doc("native-pep508")
    valid["requirements"] = [
        "requests[socks]>=2; python_version >= '3.11'",
        "demo @ https://packages.example.test/demo-1.0-py3-none-any.whl",
    ]
    assert native_canvas.export_envelope(valid, get_deps())["canvas"]["requirements"] == valid["requirements"]

    credentialed = _doc("native-direct-url-secret")
    credentialed["requirements"] = [
        "demo @ https://user:password@packages.example.test/demo.whl",
    ]
    with pytest.raises(native_canvas.NativeCanvasError, match="Direct requirement URL"):
        native_canvas.export_envelope(credentialed, get_deps())

    invalid = _doc("native-invalid-pep508")
    invalid["requirements"] = ["demo ??? 1.0"]
    with pytest.raises(native_canvas.NativeCanvasError, match="Invalid requirement"):
        native_canvas.export_envelope(invalid, get_deps())


def test_export_rejects_unknown_node_data_and_enforces_its_own_canonical_cap():
    unknown = _doc("native-unknown-node-data")
    unknown["nodes"][0]["data"]["pluginExecutionFlag"] = True
    with pytest.raises(native_canvas.NativeCanvasError, match="execution-affecting data"):
        native_canvas.export_envelope(unknown, get_deps())

    oversized = _doc("native-oversized-export")
    oversized["nodes"] = [{
        "id": "note", "type": "note", "position": {"x": 0, "y": 0},
        "data": {"title": "note", "config": {"markdown": "x" * (native_canvas.MAX_BYTES + 1)}},
    }]
    with pytest.raises(native_canvas.NativeCanvasError, match="2 MiB"):
        native_canvas.export_envelope(oversized, get_deps())


def test_envelope_uses_manifest_core_node_and_plugin_identity_snapshot():
    deps = get_deps()
    plugin_source = deps.node_specs["source"].model_copy(update={"source": "plugin:demo"})
    fixture = SimpleNamespace(
        node_specs={"source": plugin_source},
        plugins=[{
            "name": "demo", "package": "dataplay-demo", "version": "3.2.1",
            "source": "entry-point",
        }],
        registry=deps.registry,
    )
    envelope = native_canvas.export_envelope(_doc("native-plugin-identity"), fixture)
    assert envelope["descriptors"]["core"]["apiVersion"] >= 1
    assert envelope["descriptors"]["core"]["packageVersion"]
    assert envelope["descriptors"]["plugins"] == [{
        "name": "demo", "package": "dataplay-demo", "version": "3.2.1",
        "source": "entry-point",
    }]
    assert envelope["descriptors"]["nodes"][0]["source"] == "plugin:demo"


def test_diagnostics_recalculate_data_intent_and_cover_exact_uri_provider_and_defaults(monkeypatch):
    doc = _doc("native-data-diagnostics", dataset_ref={
        "kind": "exact", "datasetId": "missing-source", "revisionId": "source-r1",
    })
    doc["nodes"][0]["data"]["config"].update({
        "uri": "missing://dataset", "providerResourceRef": "provider-resource",
        "providerMountId": "mount", "providerName": "fixture", "providerReadMode": "exact",
    })
    doc["parameters"] = [{
        "name": "dataset", "type": "dataset",
        "default": {"kind": "exact", "datasetId": "missing-default", "revisionId": "default-r1"},
    }]
    envelope = native_canvas.export_envelope(doc, get_deps())
    payload = {
        "filename": "diagnostics.dp-canvas.json",
        "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed4", "envelope": envelope,
    }
    monkeypatch.setattr(
        get_deps(), "resolve_adapter",
        lambda *_args, **_kwargs: pytest.fail("validation contacted an adapter before acknowledgement"))
    with TestClient(app) as client:
        checked = client.post(
            "/api/canvas/native-import/validate", json=payload, headers={"X-DP-User": OWNER})
        tampered = json.loads(json.dumps(envelope))
        tampered["dataReferences"][0]["intent"]["uri"] = "changed://intent"
        mismatch = client.post(
            "/api/canvas/native-import/validate", json={**payload, "envelope": tampered},
            headers={"X-DP-User": OWNER})
    assert checked.status_code == 200
    codes = [item["code"] for item in checked.json()["diagnostics"]]
    assert codes.count("data_unavailable") >= 2
    assert "uri_unavailable" in codes
    assert "provider_availability_unproven" in codes
    assert mismatch.status_code == 200 and mismatch.json()["canImport"] is False
    assert any(item["code"] == "data_reference_mismatch"
               for item in mismatch.json()["diagnostics"])


def test_diagnostics_verify_exact_library_processor_version_without_using_it():
    deps = get_deps()
    processor = RegisteredProcessor(
        id="native-fixture-processor", title="fixture", mode="map", version="v7",
        fn_factory=lambda _params: pytest.fail("validation used the processor"),
    )
    deps.registry.register(processor)
    doc = _doc("native-library")
    doc["nodes"].append({
        "id": "library", "type": "transform", "position": {"x": 200, "y": 0},
        "data": {"title": "library", "status": "draft", "config": {
            "source": "library", "processor": processor.id, "version": "v7", "mode": "map",
        }},
    })
    try:
        envelope = native_canvas.export_envelope(doc, deps)
        assert envelope["libraryProcessors"][0]["processor"] == processor.id
        del deps.registry._procs[processor.id]
        parsed = native_canvas.parse_envelope(envelope, filename="library.dp-canvas.json")
        diagnostics = native_canvas.diagnostics(parsed, deps, OWNER)
        assert any(item.code == "library_processor_unavailable" and item.severity == "warning"
                   for item in diagnostics)
    finally:
        deps.registry._procs.pop(processor.id, None)


def test_validation_digest_is_canonical_and_import_rejects_stale_or_changed_intent():
    import_id = "018f5d64-bce4-7f1b-9b9f-5035482e2ed5"
    canvas_id = native_canvas.import_canvas_id(OWNER, import_id)
    metadb.delete_canvas_cascade(canvas_id)
    envelope = native_canvas.export_envelope(_doc("native-digest"), get_deps())
    payload = {"filename": "digest.dp-canvas.json", "importId": import_id, "envelope": envelope}
    try:
        with TestClient(app) as client:
            checked = client.post(
                "/api/canvas/native-import/validate", json=payload, headers={"X-DP-User": OWNER})
            digest = checked.json()["validationDigest"]
            stale = client.post("/api/canvas/native-import", json={
                **payload, "validationDigest": "0" * 64, "confirmWarnings": True,
            }, headers={"X-DP-User": OWNER})
            created = client.post("/api/canvas/native-import", json={
                **payload, "validationDigest": digest, "confirmWarnings": True,
            }, headers={"X-DP-User": OWNER})

            changed = json.loads(json.dumps(envelope))
            changed["canvas"]["name"] = "changed intent"
            changed_payload = {**payload, "envelope": changed}
            changed_check = client.post(
                "/api/canvas/native-import/validate", json=changed_payload,
                headers={"X-DP-User": OWNER})
            changed_import = client.post("/api/canvas/native-import", json={
                **changed_payload, "validationDigest": changed_check.json()["validationDigest"],
                "confirmWarnings": True,
            }, headers={"X-DP-User": OWNER})
        assert stale.status_code == 409
        assert created.status_code == 200 and created.json()["created"] is True
        assert changed_check.status_code == 200
        assert changed_import.status_code == 409 and "different import intent" in changed_import.text
    finally:
        metadb.delete_canvas_cascade(canvas_id)


def test_forced_concurrent_duplicate_import_converges_to_one_atomic_canvas():
    import_id = "018f5d64-bce4-7f1b-9b9f-5035482e2ed6"
    canvas_id = native_canvas.import_canvas_id(OWNER, import_id)
    metadb.delete_canvas_cascade(canvas_id)
    envelope = native_canvas.export_envelope(_doc("native-concurrent"), get_deps())
    payload = {"filename": "concurrent.dp-canvas.json", "importId": import_id, "envelope": envelope}
    parsed = native_canvas.parse_envelope(envelope, filename=payload["filename"])
    doc = {**parsed["canvas"], "id": canvas_id, "version": 1}
    barrier = threading.Barrier(2)

    def import_once(_index: int) -> bool:
        barrier.wait(timeout=5)
        return metadb.import_native_canvas(
            uid=OWNER, canvas_id=canvas_id, doc=doc,
            intent_digest=native_canvas.import_intent_digest(parsed))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(import_once, range(2)))
        with metadb.session() as session:
            assert session.get(metadb.Canvas, canvas_id) is not None
            placement_count = session.scalar(metadb.select(metadb.func.count()).select_from(
                metadb.WorkspacePlacement).where(
                    metadb.WorkspacePlacement.target_kind == "canvas",
                    metadb.WorkspacePlacement.target_id == canvas_id))
            assert placement_count == 1
    finally:
        metadb.delete_canvas_cascade(canvas_id)
    assert sorted(created) == [False, True]


def test_native_openapi_is_strict_and_reuses_kernel_capability_surface():
    schema = app.openapi()
    assert "/api/pipeline-import-capability" not in schema["paths"]
    validate = schema["paths"]["/api/canvas/native-import/validate"]["post"]
    request_schema = validate["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == {"filename", "importId", "envelope"}
    assert request_schema["properties"]["importId"] == {
        "format": "uuid", "title": "Importid", "type": "string",
    }
    create = schema["paths"]["/api/canvas/native-import"]["post"]
    create_schema = create["requestBody"]["content"]["application/json"]["schema"]
    assert create_schema["additionalProperties"] is False
    assert {"validationDigest", "confirmWarnings"}.issubset(create_schema["required"])
    assert "200" in create["responses"]


def test_validation_rejects_non_uuid_import_id_at_request_boundary():
    envelope = native_canvas.export_envelope(_doc("native-invalid-import-id"), get_deps())
    with TestClient(app) as client:
        response = client.post("/api/canvas/native-import/validate", json={
            "filename": "invalid-id.dp-canvas.json",
            "importId": "x" * 36,
            "envelope": envelope,
        }, headers={"X-DP-User": OWNER})
    assert response.status_code == 422


def test_validation_rejects_non_string_canvas_name_instead_of_failing_response_serialization():
    envelope = native_canvas.export_envelope(_doc("native-invalid-name"), get_deps())
    envelope["canvas"]["name"] = 7
    with TestClient(app) as client:
        response = client.post("/api/canvas/native-import/validate", json={
            "filename": "invalid-name.dp-canvas.json",
            "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed8",
            "envelope": envelope,
        }, headers={"X-DP-User": OWNER})
    assert response.status_code == 422


def test_validation_checks_descriptor_config_types_required_fields_and_select_options():
    doc = _doc("native-invalid-config")
    doc["nodes"] = [
        {"id": "sample", "type": "sample", "position": {"x": 0, "y": 0},
         "data": {"config": {"n": 10}}},
        {"id": "sort", "type": "sort", "position": {"x": 100, "y": 0},
         "data": {"config": {"by": "event"}}},
        {"id": "write", "type": "write", "position": {"x": 200, "y": 0},
         "data": {"config": {"filename": "out.parquet", "writeMode": "overwrite"}}},
    ]
    envelope = native_canvas.export_envelope(doc, get_deps())
    envelope["canvas"]["nodes"][0]["data"]["config"]["n"] = "ten"
    envelope["canvas"]["nodes"][1]["data"]["config"].pop("by")
    envelope["canvas"]["nodes"][2]["data"]["config"]["writeMode"] = "destroy"
    with TestClient(app) as client:
        response = client.post("/api/canvas/native-import/validate", json={
            "filename": "invalid-config.dp-canvas.json",
            "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed9",
            "envelope": envelope,
        }, headers={"X-DP-User": OWNER})
    assert response.status_code == 200
    invalid = [item for item in response.json()["diagnostics"]
               if item["code"] == "invalid_config"]
    assert response.json()["canImport"] is False
    assert {item.get("path") for item in invalid} >= {
        "canvas.nodes.sample.data.config.n",
        "canvas.nodes.sort.data.config.by",
        "canvas.nodes.write.data.config.writeMode",
    }


def test_validation_applies_current_user_promoted_transform_authorization():
    owner = f"native-promoted-owner-{uuid.uuid4().hex}"
    recipient = f"native-promoted-recipient-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add_all([
            metadb.User(id=owner, name="Native promoted owner"),
            metadb.User(id=recipient, name="Native promoted recipient"),
        ])
    descriptor = get_deps().registry.promote(
        owner_id=owner, id=f"native.promoted.{uuid.uuid4().hex}",
        title="Owner-only transform", mode="map",
        code="def fn(row):\n    return row", input_schema=[], output_schema=[],
        requirements=[],
    ).descriptor()
    doc = _doc("native-promoted-auth")
    doc["nodes"] = [{
        "id": "transform", "type": "transform", "position": {"x": 0, "y": 0},
        "data": {"config": {
            "source": "library", "processor": descriptor.id,
            "version": descriptor.version, "mode": descriptor.mode,
        }},
    }]
    envelope = native_canvas.export_envelope(doc, get_deps())
    payload = {
        "filename": "promoted-auth.dp-canvas.json",
        "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2eda",
        "envelope": envelope,
    }
    with TestClient(app) as client:
        checked = client.post(
            "/api/canvas/native-import/validate", json=payload,
            headers={"X-DP-User": recipient})
        imported = client.post("/api/canvas/native-import", json={
            **payload,
            "validationDigest": checked.json()["validationDigest"],
            "confirmWarnings": True,
        }, headers={"X-DP-User": recipient})
    assert checked.status_code == 200
    assert checked.json()["canImport"] is False
    assert any(item["code"] == "promoted_transform_unavailable"
               for item in checked.json()["diagnostics"])
    assert imported.status_code == 409


def test_raw_request_cap_allows_only_bounded_wrapper_overhead_before_json_decoding():
    with TestClient(app) as client:
        at_limit = client.post(
            "/api/canvas/native-import/validate", content=b" " * native_canvas.MAX_REQUEST_BYTES,
            headers={"X-DP-User": OWNER, "Content-Type": "application/json"})
        over_limit = client.post(
            "/api/canvas/native-import/validate",
            content=b" " * (native_canvas.MAX_REQUEST_BYTES + 1),
            headers={"X-DP-User": OWNER, "Content-Type": "application/json"})
    assert at_limit.status_code == 422
    assert over_limit.status_code == 413


def test_near_limit_canonical_envelope_is_not_rejected_for_json_wrapper():
    doc = _doc("native-near-limit")
    doc["nodes"] = [{
        "id": "note", "type": "note", "position": {"x": 0, "y": 0},
        "data": {"title": "note", "config": {"markdown": "x"}},
    }]
    small = native_canvas.export_envelope(doc, get_deps())
    overhead = native_canvas.canonical_size(small) - 1
    doc["nodes"][0]["data"]["config"]["markdown"] = "x" * (
        native_canvas.MAX_BYTES - overhead)
    envelope = native_canvas.export_envelope(doc, get_deps())
    payload = {
        "filename": "near-limit.dp-canvas.json",
        "importId": "018f5d64-bce4-7f1b-9b9f-5035482e2ed7", "envelope": envelope,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert native_canvas.canonical_size(envelope) == native_canvas.MAX_BYTES
    assert native_canvas.MAX_BYTES < len(raw) <= native_canvas.MAX_REQUEST_BYTES
    with TestClient(app) as client:
        response = client.post(
            "/api/canvas/native-import/validate", content=raw,
            headers={"X-DP-User": OWNER, "Content-Type": "application/json"})
    assert response.status_code == 200, response.text
