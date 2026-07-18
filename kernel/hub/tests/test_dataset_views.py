"""Immutable DatasetView API, Workspace, replay, and retention contracts."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hub import metadb
from hub.main import app
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.plugins.catalog import InMemoryCatalog
from hub.routers import dataset_views as dataset_view_routes
from hub.storage import LocalStorage


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'dataset-views.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _register_lance(client: TestClient, tmp_path, rows: dict[str, list]) -> tuple[str, dict, dict]:
    lance = pytest.importorskip("lance")
    name = f"dataset-view-{uuid.uuid4().hex}"
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table(rows), uri)
    registered = client.post(
        "/api/catalog/register", json={"uri": uri, "name": name})
    assert registered.status_code == 200, registered.text
    table = registered.json()
    history = client.get(f"/api/catalog/tables/{table['id']}/revisions")
    assert history.status_code == 200, history.text
    return uri, table, history.json()["items"][0]


def _request(revision: dict, submission_id: str, **changes) -> dict:
    request = {
        "submissionId": submission_id,
        "name": "Useful rows",
        "datasetRef": {
            "kind": "exact",
            "datasetId": revision["datasetId"],
            "revisionId": revision["revisionId"],
        },
        "selectedColumns": ["value", "id"],
        "predicate": "id >= 2",
        "sampling": {"kind": "all"},
    }
    request.update(changes)
    return request


def _workspace_item(client: TestClient, container_id: str, resource_id: str) -> dict:
    """Find one resource without assuming a shared Workspace fits on its first page."""
    cursor = None
    seen: set[str] = set()
    for _ in range(100):
        params = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(f"/api/workspace/containers/{container_id}", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        found = next((item for item in page["items"] if item["id"] == resource_id), None)
        if found is not None:
            return found
        if not page["hasMore"]:
            break
        next_cursor = page["nextCursor"]
        assert next_cursor and next_cursor not in seen
        seen.add(next_cursor)
        cursor = next_cursor
    raise AssertionError(f"Workspace resource {resource_id!r} was not found")


def test_exact_view_replay_workspace_owner_isolation_and_terminal_delete(tmp_path):
    lance = pytest.importorskip("lance")
    with TestClient(app) as client:
        uri, _table, revision = _register_lance(client, tmp_path, {
            "id": [1, 2, 3], "value": ["old-1", "old-2", "old-3"], "unused": [1, 1, 1],
        })
        submission = uuid.uuid4().hex
        request = _request(revision, submission)
        created = client.post("/api/dataset-views", json=request)
        assert created.status_code == 201, created.text
        definition = created.json()
        assert definition["schemaVersion"] == 1
        assert definition["selectedColumns"] == ["value", "id"]
        assert definition["predicate"] == "id >= 2"
        assert definition["retentionOwner"] == "provider"
        assert definition["sampleProvenance"] is None
        assert len(definition["semanticSha256"]) == len(definition["definitionSha256"]) == 64
        view_id = definition["id"]
        capabilities = client.get(
            f"/api/catalog/tables/{_table['id']}/revisions/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["datasetViewSave"] is True
        assert dataset_view_routes.supports_dataset_view_source(uri, LanceAdapter()) is True
        assert dataset_view_routes.supports_dataset_view_source(
            "s3://example/remote.lance", LanceAdapter()) is False

        # Moving the provider head cannot change an exact DatasetView replay.
        lance.write_dataset(
            pa.table({"id": [4], "value": ["new-head"], "unused": [2]}),
            uri,
            mode="append",
        )
        preview = client.post(f"/api/dataset-views/{view_id}/preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["rows"] == [
            {"value": "old-2", "id": 2}, {"value": "old-3", "id": 3},
        ]
        assert [column["name"] for column in preview.json()["columns"]] == ["value", "id"]
        assert preview.json()["rowCount"] == 2
        assert preview.json()["hasMore"] is False

        replay = client.post("/api/dataset-views", json=request)
        assert replay.status_code == 200
        assert replay.json() == definition
        mismatch = client.post(
            "/api/dataset-views", json={**request, "name": "Different intent"})
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "conflict"

        workspace_item = _workspace_item(
            client,
            definition["placement"]["containerId"],
            f"dataset_view:{view_id}",
        )
        assert workspace_item["name"] == "Useful rows"
        resolved = client.get(f"/api/workspace/resources/dataset_view:{view_id}")
        assert resolved.status_code == 200
        assert resolved.json()["resource"]["placementId"] == definition["placement"]["placementId"]
        with pytest.raises(ValueError, match="immutable DatasetView"):
            metadb.workspace_update_placement(
                definition["placement"]["placementId"], expected_version=1,
                name="Bypassed immutable name",
            )
        with pytest.raises(ValueError, match="immutable DatasetView"):
            metadb.workspace_delete_placement(
                definition["placement"]["placementId"], expected_version=1)
        assert client.get(f"/api/dataset-views/{view_id}").status_code == 200

        other = f"dataset-view-other-{uuid.uuid4().hex}"
        with metadb.session() as session:
            session.add(metadb.User(id=other, name="Other"))
        headers = {"X-DP-User": other}
        assert client.get(f"/api/dataset-views/{view_id}", headers=headers).status_code == 404
        assert client.get(
            f"/api/workspace/resources/dataset_view:{view_id}", headers=headers).status_code == 404
        other_browse = client.get(
            "/api/workspace/containers/workspace-local-root", headers=headers)
        assert all(item["id"] != f"dataset_view:{view_id}" for item in other_browse.json()["items"])

        deleted = client.delete(f"/api/dataset-views/{view_id}")
        assert deleted.status_code == 200 and deleted.json() == {"ok": True, "deleted": True}
        assert client.get(f"/api/dataset-views/{view_id}").status_code == 410
        assert client.post(f"/api/dataset-views/{view_id}/preview").status_code == 410
        assert client.post("/api/dataset-views", json=request).status_code == 410
        assert client.delete(f"/api/dataset-views/{view_id}").json()["deleted"] is False
        assert client.get(
            f"/api/workspace/resources/dataset_view:{view_id}").status_code == 404


def test_reservoir_is_deterministic_and_invalid_draft_does_not_claim_submission(tmp_path):
    with TestClient(app) as client:
        _uri, _table, revision = _register_lance(client, tmp_path, {
            "id": list(range(100)), "value": [f"row-{index}" for index in range(100)],
        })
        submission = uuid.uuid4().hex
        invalid = client.post("/api/dataset-views", json=_request(
            revision, submission, selectedColumns=["missing"], predicate=None))
        assert invalid.status_code == 422
        assert metadb.dataset_view_submission(metadb.DEFAULT_USER_ID, submission) is None

        request = _request(
            revision,
            submission,
            predicate="id % 2 = 0",
            sampling={"kind": "reservoir", "size": 10, "seed": 42},
        )
        created = client.post("/api/dataset-views", json=request)
        assert created.status_code == 201, created.text
        definition = created.json()
        evidence = definition["sampleProvenance"]
        assert evidence["strategy"] == "reservoir"
        assert evidence["seed"] == 42 and evidence["requestedRows"] == 10
        assert evidence["returnedRows"] == 10
        assert evidence["datasetIdentity"] == revision["datasetId"]
        assert evidence["datasetRevision"] == revision["revisionId"]
        first = client.post(f"/api/dataset-views/{definition['id']}/preview")
        second = client.post(f"/api/dataset-views/{definition['id']}/preview")
        assert first.status_code == second.status_code == 200
        assert first.json()["rows"] == second.json()["rows"]
        assert len(first.json()["rows"]) == 10
        assert first.json()["rowCount"] == 10 and first.json()["hasMore"] is False
        assert all(row["id"] % 2 == 0 for row in first.json()["rows"])

        same_population = client.post("/api/dataset-views", json={
            **request,
            "submissionId": uuid.uuid4().hex,
            "name": "A different display name",
        })
        assert same_population.status_code == 201
        assert same_population.json()["semanticSha256"] == definition["semanticSha256"]
        assert same_population.json()["definitionSha256"] != definition["definitionSha256"]
        changed_population = client.post("/api/dataset-views", json={
            **request,
            "submissionId": uuid.uuid4().hex,
            "sampling": {"kind": "reservoir", "size": 10, "seed": 43},
        })
        assert changed_population.status_code == 201
        assert changed_population.json()["semanticSha256"] != definition["semanticSha256"]

        maximum_seed = client.post("/api/dataset-views", json={
            **request,
            "submissionId": uuid.uuid4().hex,
            "sampling": {"kind": "reservoir", "size": 10, "seed": 2_147_483_647},
        })
        assert maximum_seed.status_code == 201, maximum_seed.text
        assert client.post(
            f"/api/dataset-views/{maximum_seed.json()['id']}/preview").status_code == 200
        rejected_submission = uuid.uuid4().hex
        above_maximum = client.post("/api/dataset-views", json={
            **request,
            "submissionId": rejected_submission,
            "sampling": {"kind": "reservoir", "size": 10, "seed": 2_147_483_648},
        })
        assert above_maximum.status_code == 422
        assert metadb.dataset_view_submission(
            metadb.DEFAULT_USER_ID, rejected_submission) is None


def test_core_revision_hold_is_installed_and_released_with_view(
    tmp_path, monkeypatch,
):
    storage = LocalStorage(str(tmp_path / "outputs"))
    catalog = InMemoryCatalog(str(tmp_path / "data"), lambda _uri: DuckDBAdapter())
    try:
        logical_uri = str(tmp_path / "published" / "managed.parquet")
        run_id = uuid.uuid4().hex
        artifact = storage.begin_result(f"managed-file:{logical_uri}", run_id)
        pq.write_table(pa.table({"id": [1, 2], "value": ["one", "two"]}), artifact)
        storage.commit_result(artifact, run_id)
        published = catalog.publish_managed_local_file_output(
            name="managed", logical_uri=logical_uri, artifact_uri=artifact)
        assert storage.release_result(artifact, run_id) is True
        monkeypatch.setattr(dataset_view_routes, "get_deps", lambda: SimpleNamespace(
            storage=storage,
            resolve_adapter=lambda _uri: DuckDBAdapter(),
        ))

        with TestClient(app) as client:
            request = _request({
                "datasetId": published["dataset_id"],
                "revisionId": published["revision_id"],
            }, uuid.uuid4().hex, predicate=None)
            created = client.post("/api/dataset-views", json=request)
            assert created.status_code == 201, created.text
            definition = created.json()
            assert definition["retentionOwner"] == "core"
            with metadb.session() as session:
                refs = list(session.scalars(select(metadb.LocalResultReference).where(
                    metadb.LocalResultReference.owner_kind == "dataset_view",
                    metadb.LocalResultReference.owner_key == definition["id"],
                )))
                assert [ref.uri for ref in refs] == [artifact]
            assert client.delete(f"/api/dataset-views/{definition['id']}").status_code == 200
            with metadb.session() as session:
                assert session.scalar(select(metadb.LocalResultReference.uri).where(
                    metadb.LocalResultReference.owner_kind == "dataset_view",
                    metadb.LocalResultReference.owner_key == definition["id"],
                )) is None
    finally:
        storage.close()


def test_sqlite_backup_restores_definition_placement_and_tombstone(tmp_path):
    from hub.settings import settings

    if not settings.database_url.startswith("sqlite:///"):
        pytest.skip("SQLite backup contract")
    with TestClient(app) as client:
        _uri, _table, revision = _register_lance(client, tmp_path, {
            "id": [1, 2, 3], "value": ["one", "two", "three"],
        })
        live_request = _request(revision, uuid.uuid4().hex)
        deleted_request = _request(
            revision, uuid.uuid4().hex, name="Deleted view", predicate=None)
        live = client.post("/api/dataset-views", json=live_request)
        deleted = client.post("/api/dataset-views", json=deleted_request)
        assert live.status_code == deleted.status_code == 201
        live_definition = live.json()
        deleted_definition = deleted.json()
        assert client.delete(
            f"/api/dataset-views/{deleted_definition['id']}").status_code == 200

    source_path = settings.database_url.removeprefix("sqlite:///")
    restored_path = str(tmp_path / "dataset-views-restored.db")
    with sqlite3.connect(source_path) as source, sqlite3.connect(restored_path) as restored:
        source.backup(restored)

    assert metadb._engine is not None
    metadb._engine.dispose()
    settings.database_url = f"sqlite:///{restored_path}"
    metadb._engine = metadb._Session = None
    metadb.init_db()

    with TestClient(app) as client:
        restored = client.get(f"/api/dataset-views/{live_definition['id']}")
        assert restored.status_code == 200
        assert restored.json() == live_definition
        preview = client.post(
            f"/api/dataset-views/{live_definition['id']}/preview")
        assert preview.status_code == 200
        assert preview.json()["rows"] == [
            {"value": "two", "id": 2}, {"value": "three", "id": 3},
        ]

        assert client.get(
            f"/api/dataset-views/{deleted_definition['id']}").status_code == 410
        assert client.post(
            "/api/dataset-views", json=deleted_request).status_code == 410
        workspace = client.get(
            "/api/workspace/containers/workspace-local-root").json()["items"]
        identities = {item["id"] for item in workspace}
        assert f"dataset_view:{live_definition['id']}" in identities
        assert f"dataset_view:{deleted_definition['id']}" not in identities


def test_concurrent_same_submission_has_one_atomic_winner(tmp_path):
    uri = str(tmp_path / "concurrent.parquet")
    metadb.catalog_upsert_entry(uri, "Concurrent source", {
        "id": uuid.uuid4().hex,
        "name": "Concurrent source",
        "uri": uri,
        "columns": [{"name": "id", "type": "int"}],
    })
    dataset_id = metadb.workspace_builtin_dataset_identity(uri)
    workspace = metadb.dataset_view_source_workspace(dataset_id)
    submission_id = uuid.uuid4().hex
    request_sha256 = "a" * 64

    def create(index: int):
        view_id = uuid.uuid4().hex
        placement_id = uuid.uuid4().hex
        definition = {
            "name": f"Concurrent view {index}",
            "retentionOwner": "provider",
            "datasetRef": {
                "kind": "exact", "datasetId": dataset_id, "revisionId": "revision-1",
            },
            "id": view_id,
        }
        document = json.dumps(definition, sort_keys=True)
        return metadb.dataset_view_create(
            uid=metadb.DEFAULT_USER_ID,
            view_id=view_id,
            placement_id=placement_id,
            submission_id=submission_id,
            request_sha256=request_sha256,
            definition_sha256=("b" if index == 0 else "c") * 64,
            definition_doc=document,
            source_dataset_id=dataset_id,
            source_registration_id=workspace["sourceRegistrationId"],
            expected_container_id=workspace["containerId"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))

    assert sorted(created for _definition, created in results) == [False, True]
    assert results[0][0] == results[1][0]
    with metadb.session() as session:
        assert len(list(session.scalars(select(metadb.DatasetView).where(
            metadb.DatasetView.submission_id == submission_id)))) == 1
        assert len(list(session.scalars(select(metadb.WorkspacePlacement).where(
            metadb.WorkspacePlacement.target_kind == "dataset_view",
            metadb.WorkspacePlacement.target_id == results[0][0]["id"],
        )))) == 1


def test_dataset_view_openapi_documents_create_and_replay_responses():
    responses = app.openapi()["paths"]["/api/dataset-views"]["post"]["responses"]
    expected = {"$ref": "#/components/schemas/DatasetViewDefinitionV1"}
    assert responses["200"]["content"]["application/json"]["schema"] == expected
    assert responses["201"]["content"]["application/json"]["schema"] == expected
