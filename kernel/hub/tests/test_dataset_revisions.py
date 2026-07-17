"""Contract coverage for provider-native exact dataset revisions."""

from __future__ import annotations

import datetime
import uuid

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from hub.main import app
from hub.models import Graph
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


def _register_one_row_lance(tmp_path, stem: str) -> tuple[str, dict]:
    lance = pytest.importorskip("lance")
    name = f"{stem}-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table({"id": [1], "value": [stem]}), uri)
    response = client.post("/api/catalog/register", json={"uri": uri, "name": name})
    assert response.status_code == 200, response.text
    return uri, response.json()


def test_inspectors_and_cache_identity_reuse_one_ordered_exact_input_set(tmp_path):
    lance = pytest.importorskip("lance")
    left_uri, _left = _register_one_row_lance(tmp_path, "binding-left")
    right_uri, _right = _register_one_row_lance(tmp_path, "binding-right")
    graph = {
        "id": f"inspection-binding-{uuid.uuid4().hex}", "version": 1,
        "nodes": [
            {"id": "left", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": left_uri}}},
            {"id": "right", "type": "source", "position": {"x": 0, "y": 100},
             "data": {"config": {"uri": right_uri}}},
            {"id": "union", "type": "union", "position": {"x": 200, "y": 0},
             "data": {"config": {"mode": "all", "align": "name"}}},
        ],
        "edges": [
            {"id": "left-union", "source": "left", "target": "union",
             "data": {"wire": "dataset"}},
            {"id": "right-union", "source": "right", "target": "union",
             "data": {"wire": "dataset"}},
        ],
    }
    first_response = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "union",
    })
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()["inputManifest"]
    # The manifest uses the engine's stable upstream traversal order (not an unordered set).
    assert [item["node_id"] for item in first] == ["right", "left"]
    assert all("uri" not in item and "secret" not in str(item).lower() for item in first)

    lance.write_dataset(pa.table({"id": [2], "value": ["binding-left-new"]}),
                        left_uri, mode="append")
    lance.write_dataset(pa.table({"id": [2], "value": ["binding-right-new"]}),
                        right_uri, mode="append")

    preview = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "union", "inputManifest": first,
    })
    assert preview.status_code == 200, preview.text
    assert [row["id"] for row in preview.json()["rows"]] == [1, 1], preview.json()
    assert preview.json()["inputManifest"] == first

    sampled = client.post("/api/run/profile", json={
        "graph": graph, "nodeId": "union", "inputManifest": first,
    })
    assert sampled.status_code == 200, sampled.text
    assert sampled.json()["rowCount"] == 2
    assert sampled.json()["inputManifest"] == first

    schema = client.post("/api/graph/schema", json={
        "graph": graph, "targetNodeId": "union", "inputManifest": first,
    })
    assert schema.status_code == 200, schema.text
    assert set(schema.json()) == {"left", "right", "union"}

    preflight = client.post("/api/run/profile-estimate", json={
        "graph": graph, "nodeId": "union", "inputManifest": first,
    })
    identity = client.post("/api/run/profile-identity", json={
        "graph": graph, "nodeId": "union", "inputManifest": first,
    })
    assert preflight.status_code == identity.status_code == 200
    assert preflight.json()["rows"] == 2
    assert preflight.json()["planDigest"] == identity.json()["planDigest"]
    assert identity.json()["inputManifest"] == first

    current = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "union",
    })
    assert current.status_code == 200, current.text
    assert [row["id"] for row in current.json()["rows"]] == [1, 2, 1, 2]
    second = current.json()["inputManifest"]
    assert [item["revision_id"] for item in second] != [
        item["revision_id"] for item in first]

    from hub.deps import get_deps
    from hub.plan_key import plan_hash
    from hub.routers import runs

    deps = get_deps()
    parsed = Graph.model_validate(graph)
    old_graph = runs._bind_local_run_manifest(parsed, first, deps, "union")
    new_graph = runs._bind_local_run_manifest(parsed, second, deps, "union")
    assert plan_hash(old_graph, "union", deps.resolve_adapter) != plan_hash(
        new_graph, "union", deps.resolve_adapter)


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


def test_lance_exact_revision_detail_does_not_invent_parent_across_retention_gap(tmp_path):
    lance = pytest.importorskip("lance")
    name = f"revision-gap-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    for value in range(3):
        lance.write_dataset(pa.table({"value": [value]}), uri,
                            mode="create" if value == 0 else "append")
    dataset = lance.dataset(uri)
    dataset.tags.create("keep-first", 1)
    dataset.cleanup_old_versions(
        older_than=datetime.timedelta(0), retain_versions=1,
        error_if_tagged_old_versions=False)
    assert [entry["version"] for entry in dataset.versions()] == [1, 3]

    registered = client.post("/api/catalog/register", json={"uri": uri, "name": name})
    assert registered.status_code == 200, registered.text
    revision = client.get(
        f"/api/catalog/tables/{registered.json()['id']}/revisions").json()["items"][0]
    response = client.get(
        f"/api/catalog/revisions/{revision['datasetId']}/{revision['revisionId']}")
    assert response.status_code == 200, response.text
    assert response.json()["parentRevisionId"] is None


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


def test_pinned_source_preview_and_reload_keep_the_exact_revision_after_append(tmp_path):
    lance = pytest.importorskip("lance")
    uri, table = _register_lance(tmp_path)
    first = client.get(f"/api/catalog/tables/{table['id']}/revisions?limit=1").json()
    older = client.get(
        f"/api/catalog/tables/{table['id']}/revisions?limit=1&cursor={first['nextCursor']}").json()
    selected = older["items"][0]
    canvas_id = f"pinned-{uuid.uuid4().hex}"
    graph = {
        "id": canvas_id, "name": "pinned", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"title": "source", "status": "draft", "config": {
                "uri": uri, "tableId": table["id"],
                "datasetRef": {"datasetId": selected["datasetId"],
                               "revisionId": selected["revisionId"]},
            }},
        }],
        "edges": [],
    }
    try:
        saved = client.put(f"/api/canvas/{canvas_id}", json=graph)
        assert saved.status_code == 200, saved.text
        restored = client.get(f"/api/canvas/{canvas_id}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["nodes"][0]["data"]["config"]["datasetRef"] == {
            "datasetId": selected["datasetId"], "revisionId": selected["revisionId"]}

        lance.write_dataset(pa.table({"value": [3]}), uri, mode="append")
        preview = client.post("/api/run/preview", json={
            "graph": restored.json(), "nodeId": "source", "k": 50, "offset": 0})
        assert preview.status_code == 200, preview.text
        payload = preview.json()
        assert payload["rows"] == [{"value": 1}]
        assert payload["sampleProvenance"]["datasetIdentity"] == selected["datasetId"]
        assert payload["sampleProvenance"]["datasetRevision"] == selected["revisionId"]
    finally:
        client.delete(f"/api/canvas/{canvas_id}")


def test_preview_manifest_reports_drift_and_reuses_exact_membership_until_refresh(tmp_path):
    lance = pytest.importorskip("lance")
    uri, table = _register_lance(tmp_path)
    graph = {
        "id": f"preview-binding-{uuid.uuid4().hex}", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri, "tableId": table["id"]}},
        }], "edges": [],
    }
    preview = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "source", "k": 50, "offset": 0})
    assert preview.status_code == 200, preview.text
    retained = preview.json()["inputManifest"]
    assert preview.json()["rows"] == [{"value": 1}, {"value": 2}]

    lance.write_dataset(pa.table({"value": [3]}), uri, mode="append")
    drift = client.post("/api/run/input-drift", json={
        "graph": graph, "targetNodeId": "source", "inputManifest": retained})
    assert drift.status_code == 200, drift.text
    assert drift.json()["drifted"] is True
    assert drift.json()["sources"][0]["previewRevisionId"] == retained[0]["revision_id"]
    assert drift.json()["sources"][0]["oldRevisionReadable"] is True

    reused = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "source", "k": 50, "offset": 0,
        "inputManifest": retained,
    })
    assert reused.status_code == 200, reused.text
    assert reused.json()["rows"] == [{"value": 1}, {"value": 2}]
    assert reused.json()["inputManifest"] == retained

    refreshed = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "source", "k": 50, "offset": 0})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["rows"] == [{"value": 1}, {"value": 2}, {"value": 3}]
    assert refreshed.json()["inputManifest"][0]["revision_id"] != retained[0]["revision_id"]


def test_pinned_source_missing_revision_fails_without_retargeting_latest(tmp_path):
    _uri, table = _register_lance(tmp_path)
    current = client.get(f"/api/catalog/tables/{table['id']}/revisions").json()["items"][0]
    graph = {
        "id": f"missing-pin-{uuid.uuid4().hex}", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": table["uri"], "tableId": table["id"],
                                  "datasetRef": {"datasetId": current["datasetId"],
                                                 "revisionId": "999999"}}},
        }], "edges": [],
    }
    response = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": "source", "k": 50, "offset": 0})
    assert response.status_code == 200, response.text
    assert response.json()["notPreviewable"] is True
    assert "selected pinned revision is unavailable" in response.json()["reason"]


def test_pinned_dataset_ref_is_a_strict_typed_graph_value():
    with pytest.raises(ValueError, match="datasetId"):
        Graph.model_validate({
            "id": "bad-pin", "nodes": [{
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": "dataset.lance",
                                      "datasetRef": {"revisionId": "1"}}},
            }], "edges": [],
        })
