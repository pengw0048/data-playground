"""Contract coverage for provider-native exact dataset revisions."""

from __future__ import annotations

import datetime
import uuid

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from hub.main import app
from hub.plugins.adapters import LanceAdapter

client = TestClient(app)


def _register_lance(tmp_path) -> tuple[str, dict]:
    lance = pytest.importorskip("lance")
    name = f"revision-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table({"value": [1]}), uri)
    lance.write_dataset(pa.table({"value": [2]}), uri, mode="append")
    response = client.post("/api/catalog/register", json={"uri": uri, "name": name})
    assert response.status_code == 200, response.text
    return uri, response.json()


def test_lance_revision_history_resolves_and_opens_an_exact_version(tmp_path):
    lance = pytest.importorskip("lance")
    uri, table = _register_lance(tmp_path)
    history = client.get(f"/api/catalog/tables/{table['id']}/revisions?limit=1")
    assert history.status_code == 200, history.text
    first = history.json()
    assert len(first["items"]) == 1 and first["hasMore"] is True
    dataset_id = first["items"][0]["datasetId"]
    latest = first["items"][0]["revisionId"]

    older = client.get(
        f"/api/catalog/tables/{table['id']}/revisions?limit=1&cursor={first['nextCursor']}")
    assert older.status_code == 200, older.text
    assert older.json()["items"][0]["revisionId"] != latest
    exact = older.json()["items"][0]["revisionId"]

    resolved = client.get(f"/api/catalog/tables/{table['id']}/revisions/resolve")
    assert resolved.status_code == 200 and resolved.json()["revisionId"] == latest
    as_of = client.get(f"/api/catalog/tables/{table['id']}/revisions/resolve",
                       params={"asOf": datetime.datetime.now().isoformat()})
    assert as_of.status_code == 200, as_of.text
    assert as_of.json()["selector"] == "as_of"
    assert as_of.json()["revisionId"] == latest
    lance.write_dataset(pa.table({"value": [3]}), uri, mode="append")
    opened = client.get(f"/api/catalog/revisions/{dataset_id}/{exact}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["selector"] == "exact"
    assert LanceAdapter().open_revision(uri, exact).fetchall() == [(1,)]


def test_unregistered_lance_binding_never_retargets_same_path(tmp_path):
    uri, table = _register_lance(tmp_path)
    history = client.get(f"/api/catalog/tables/{table['id']}/revisions")
    old = history.json()["items"][0]
    assert client.delete(f"/api/catalog/tables/{table['id']}").status_code == 200
    replacement = client.post("/api/catalog/register", json={"uri": uri, "name": table["name"]})
    assert replacement.status_code == 200, replacement.text
    assert client.get(f"/api/catalog/revisions/{old['datasetId']}/{old['revisionId']}").status_code == 410
    fresh = client.get(f"/api/catalog/tables/{replacement.json()['id']}/revisions")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["items"][0]["datasetId"] != old["datasetId"]


def test_missing_lance_revision_is_a_stable_unavailable_error(tmp_path):
    _uri, table = _register_lance(tmp_path)
    dataset_id = client.get(f"/api/catalog/tables/{table['id']}/revisions").json()["items"][0]["datasetId"]
    response = client.get(f"/api/catalog/revisions/{dataset_id}/999999")
    assert response.status_code == 410
    assert response.json()["detail"] == "dataset_revision_unavailable"
    assert response.json()["code"] == "resource_gone"
