"""Register-with-curation, friendly-name rename, and batch delete — the Tables-view mutations that
back multi-select delete and the register modal / drawer rename."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.main import app

client = TestClient(app)


def _csv(tmp_path, name: str) -> str:
    p = tmp_path / f"{name}.csv"
    p.write_text("id,amount\n1,10\n2,20\n")
    return str(p)


def test_register_with_full_payload_then_rename(tmp_path):
    uri = _csv(tmp_path, "events")
    reg = client.post("/api/catalog/register", json={
        "uri": uri, "name": "my events", "folder": "demo/raw",
        "tags": ["gold", "pii"], "owner": "me", "description": "d",
    })
    assert reg.status_code == 200, reg.text
    t = reg.json()
    assert t["name"] == "my events"
    assert t["folder"] == "demo/raw"
    assert set(t["tags"]) == {"gold", "pii"}
    assert t["owner"] == "me"

    # rename via the metadata PUT (drawer Name field); other fields left untouched
    put = client.put(f"/api/catalog/tables/{t['id']}/metadata", json={"name": "renamed"})
    assert put.status_code == 200, put.text
    assert put.json()["name"] == "renamed"
    # a blank name is ignored — the rename is not cleared
    keep = client.put(f"/api/catalog/tables/{t['id']}/metadata", json={"name": "   "})
    assert keep.json()["name"] == "renamed"
    # persisted on re-read
    assert client.get(f"/api/catalog/tables/{t['id']}").json()["name"] == "renamed"

    current = client.get(f"/api/catalog/tables/{t['id']}").json()
    client.post("/api/catalog/tables/delete", json={"targets": [{
        "id": t["id"], "expectedRegistrationId": current["registrationId"],
        "expectedRevision": current["metadataRevision"],
    }]})


def test_batch_delete_is_bounded_versioned_and_reports_each_result(tmp_path):
    a = client.post("/api/catalog/register", json={"uri": _csv(tmp_path, "a")}).json()
    b = client.post("/api/catalog/register", json={"uri": _csv(tmp_path, "b")}).json()

    a = client.get(f"/api/catalog/tables/{a['id']}").json()
    b = client.get(f"/api/catalog/tables/{b['id']}").json()
    stale_revision = a["metadataRevision"]
    changed = client.put(f"/api/catalog/tables/{a['id']}/edit", json={
        "expectedRevision": stale_revision, "folder": "changed", "tags": [],
        "owner": None, "description": None, "declaredKey": [],
    })
    assert changed.status_code == 200, changed.text
    res = client.post("/api/catalog/tables/delete", json={"targets": [
        {"id": a["id"], "expectedRegistrationId": a["registrationId"], "expectedRevision": stale_revision},
        {"id": b["id"], "expectedRegistrationId": b["registrationId"], "expectedRevision": b["metadataRevision"]},
        {"id": "does-not-exist", "expectedRegistrationId": "missing-registration", "expectedRevision": "m1_missing"},
    ]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "best_effort" and body["limit"] == 50
    assert body["results"] == [
        {"id": a["id"], "status": "conflict", "detail": "catalog metadata changed; reload before removing this dataset"},
        {"id": b["id"], "status": "deleted", "detail": None},
        {"id": "does-not-exist", "status": "missing", "detail": "dataset was already unregistered"},
    ]
    assert client.get(f"/api/catalog/tables/{a['id']}").status_code == 200
    assert client.get(f"/api/catalog/tables/{b['id']}").status_code == 404

    too_many = client.post("/api/catalog/tables/delete", json={"targets": [
        {"id": f"table-{index}", "expectedRegistrationId": f"registration-{index}", "expectedRevision": "m1_revision"} for index in range(51)
    ]})
    assert too_many.status_code == 422
    client.delete(f"/api/catalog/tables/{a['id']}", params={
        "expected_registration_id": a["registrationId"],
        "expected_revision": changed.json()["metadataRevision"],
    })


def test_unregister_precondition_does_not_rebind_after_reregistration(tmp_path):
    uri = _csv(tmp_path, "reused")
    created = client.post("/api/catalog/register", json={"uri": uri}).json()
    original = client.get(f"/api/catalog/tables/{created['id']}").json()
    removed = client.delete(f"/api/catalog/tables/{created['id']}", params={
        "expected_registration_id": original["registrationId"],
        "expected_revision": original["metadataRevision"],
    })
    assert removed.status_code == 200

    replacement = client.post("/api/catalog/register", json={"uri": uri}).json()
    replacement = client.get(f"/api/catalog/tables/{replacement['id']}").json()
    assert replacement["registrationId"] != original["registrationId"]
    stale = client.post("/api/catalog/tables/delete", json={"targets": [{
        "id": replacement["id"],
        "expectedRegistrationId": original["registrationId"],
        "expectedRevision": original["metadataRevision"],
    }]})
    assert stale.status_code == 200
    assert stale.json()["results"] == [{
        "id": replacement["id"], "status": "conflict",
        "detail": "catalog registration changed; reload before removing this dataset",
    }]
    assert client.get(f"/api/catalog/tables/{replacement['id']}").status_code == 200
    client.delete(f"/api/catalog/tables/{replacement['id']}", params={
        "expected_registration_id": replacement["registrationId"],
        "expected_revision": replacement["metadataRevision"],
    })


def test_batch_unregister_isolates_and_sanitizes_provider_failures(tmp_path, monkeypatch):
    failed = client.post("/api/catalog/register", json={"uri": _csv(tmp_path, "failed")}).json()
    healthy = client.post("/api/catalog/register", json={"uri": _csv(tmp_path, "healthy")}).json()
    catalog = get_deps().catalog
    unregister = catalog.unregister_if_revision

    def fail_one(table_id, expected_registration_id, expected_revision):
        if table_id == failed["id"]:
            raise ValueError("provider secret must not cross the API boundary")
        return unregister(table_id, expected_registration_id, expected_revision)

    monkeypatch.setattr(catalog, "unregister_if_revision", fail_one)
    response = client.post("/api/catalog/tables/delete", json={"targets": [
        {"id": failed["id"], "expectedRegistrationId": failed["registrationId"],
         "expectedRevision": failed["metadataRevision"]},
        {"id": healthy["id"], "expectedRegistrationId": healthy["registrationId"],
         "expectedRevision": healthy["metadataRevision"]},
    ]})
    assert response.status_code == 200, response.text
    assert response.json()["results"] == [
        {"id": failed["id"], "status": "failed",
         "detail": "dataset could not be removed; reload and retry"},
        {"id": healthy["id"], "status": "deleted", "detail": None},
    ]
    assert client.get(f"/api/catalog/tables/{failed['id']}").status_code == 200
    unregister(failed["id"], failed["registrationId"], failed["metadataRevision"])
