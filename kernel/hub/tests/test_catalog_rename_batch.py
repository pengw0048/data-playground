"""Register-with-curation, friendly-name rename, and batch delete — the Tables-view mutations that
back multi-select delete and the register modal / drawer rename."""

from __future__ import annotations

from fastapi.testclient import TestClient

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

    client.post("/api/catalog/tables/delete", json={"ids": [t["id"]]})


def test_batch_delete_reports_deleted_and_missing(tmp_path):
    a = client.post("/api/catalog/register", json={"uri": _csv(tmp_path, "a")}).json()
    b = client.post("/api/catalog/register", json={"uri": _csv(tmp_path, "b")}).json()

    res = client.post("/api/catalog/tables/delete", json={"ids": [a["id"], b["id"], "does-not-exist"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body["deleted"]) == {a["id"], b["id"]}
    assert body["missing"] == ["does-not-exist"]
    assert client.get(f"/api/catalog/tables/{a['id']}").status_code == 404
    assert client.get(f"/api/catalog/tables/{b['id']}").status_code == 404
