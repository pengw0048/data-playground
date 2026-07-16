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
    detail = opened.json()
    assert detail["revisionId"] == exact
    assert detail["parentRevisionId"] is None
    assert detail["producerOperation"] is None
    assert detail["summary"]["rowCount"] == 1
    assert detail["preview"]["columns"][0]["name"] == "value"
    assert detail["preview"]["rows"] == [{"value": 1}]
    assert detail["preview"]["hasMore"] is False
    assert LanceAdapter().open_revision(uri, exact).fetchall() == [(1,)]


def test_lance_exact_revision_detail_is_bounded_and_keeps_parent_after_head_moves(tmp_path):
    lance = pytest.importorskip("lance")
    name = f"revision-detail-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table({"value": list(range(101))}), uri)
    lance.write_dataset(pa.table({"value": [101]}), uri, mode="append")
    registered = client.post("/api/catalog/register", json={"uri": uri, "name": name})
    assert registered.status_code == 200, registered.text
    history = client.get(f"/api/catalog/tables/{registered.json()['id']}/revisions?limit=1")
    assert history.status_code == 200, history.text
    current = history.json()["items"][0]

    lance.write_dataset(pa.table({"value": [102]}), uri, mode="append")
    response = client.get(f"/api/catalog/revisions/{current['datasetId']}/{current['revisionId']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["parentRevisionId"] is not None
    assert detail["summary"]["rowCount"] == 102
    assert detail["summary"]["dataFileCount"] is not None
    assert detail["summary"]["totalBytes"] is not None
    assert detail["summary"]["fragmentCount"] is not None
    assert len(detail["preview"]["rows"]) == 100
    assert detail["preview"]["hasMore"] is True
    assert detail["preview"]["rows"][0] == {"value": 0}


def test_lance_exact_revision_detail_preserves_schema_for_empty_revision(tmp_path):
    lance = pytest.importorskip("lance")
    name = f"empty-revision-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table({"value": pa.array([], type=pa.int64())}), uri)
    registered = client.post("/api/catalog/register", json={"uri": uri, "name": name})
    assert registered.status_code == 200, registered.text
    history = client.get(f"/api/catalog/tables/{registered.json()['id']}/revisions")
    revision = history.json()["items"][0]

    response = client.get(f"/api/catalog/revisions/{revision['datasetId']}/{revision['revisionId']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["summary"]["rowCount"] == 0
    assert detail["preview"]["columns"][0]["name"] == "value"
    assert detail["preview"]["rows"] == []
    assert detail["preview"]["hasMore"] is False


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
