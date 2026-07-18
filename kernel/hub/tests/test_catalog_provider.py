"""Contract and installed-wheel coverage for read-only catalog mounts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hub.catalog_provider import (
    _PROVIDER_READ_CONCURRENCY, CatalogMount, CatalogResource, ProviderAncestors, ProviderPage,
    ProviderResourceResult, bounded_ancestors, bounded_dataset_detail, bounded_list_children,
    bounded_resolve,
)


def _write_catalog(root: Path, resources: list[dict]) -> None:
    root.mkdir()
    (root / "catalog.json").write_text(json.dumps({"resources": resources}))


def _resources(uri: str) -> list[dict]:
    return [
        {"id": "container-a", "kind": "container", "name": "shared"},
        {"id": "dataset-a", "kind": "dataset", "name": "shared", "uri": uri,
         "columns": [{"name": "id", "type": "int64"}]},
        {"id": "nested-dataset", "kind": "dataset", "name": "nested", "parentId": "container-a",
         "uri": uri + "/nested", "columns": [{"name": "id", "type": "int64"}]},
    ]


def test_file_provider_keeps_mount_config_identity_and_duplicate_names_isolated(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repo / "examples" / "plugins"))
    from dp_file_catalog_provider import provider

    first_root, second_root = tmp_path / "first", tmp_path / "second"
    _write_catalog(first_root, _resources("file:///first.parquet"))
    _write_catalog(second_root, _resources("file:///second.parquet"))
    catalog = provider()
    first = CatalogMount(id="mount-one", provider="dp-file-catalog", config={"root": str(first_root)})
    second = CatalogMount(id="mount-two", provider="dp-file-catalog", config={"root": str(second_root)})

    first_page = catalog.list_children(first, None, limit=1)
    second_page = catalog.list_children(second, None, limit=1)
    assert first_page.items[0].name == second_page.items[0].name == "shared"
    assert first_page.items[0].id == second_page.items[0].id == "container-a"
    assert first_page.next_cursor == second_page.next_cursor == "1"
    first_dataset = catalog.resolve(first, "dataset-a").item
    second_dataset = catalog.dataset_detail(second, "dataset-a").item
    assert first_dataset is not None and second_dataset is not None
    assert first_dataset.uri.startswith("dp-file-catalog-mutable://")
    assert second_dataset.uri.startswith("dp-file-catalog-mutable://")
    assert first_dataset.uri != second_dataset.uri
    assert first_dataset.columns[0].name == "id"
    assert [item.id for item in catalog.ancestors(first, "nested-dataset").items] == ["container-a"]

    (first_root / "catalog.json").write_text(json.dumps({"resources": [
        {"id": "container-a", "kind": "container", "name": "a", "parentId": "container-b"},
        {"id": "container-b", "kind": "container", "name": "b", "parentId": "container-a"},
    ]}))
    cyclic = catalog.ancestors(first, "container-a")
    assert cyclic.state == "partial" and cyclic.reason == "ancestor cycle detected"
    assert [item.id for item in cyclic.items] == ["container-b"]


class _SlowProvider:
    def list_children(self, *_args, **_kwargs):
        time.sleep(0.2)
        return ProviderPage()


class _CancelledProvider:
    def list_children(self, *_args, **_kwargs):
        raise asyncio.CancelledError()


def test_bounded_listing_caps_background_work_and_normalizes_failures():
    mount = CatalogMount(id="local", provider="test")
    started = time.monotonic()
    timeouts = [
        bounded_list_children(_SlowProvider(), mount, None, limit=1, timeout=0.005)
        for _ in range(_PROVIDER_READ_CONCURRENCY)
    ]
    saturated = bounded_list_children(_SlowProvider(), mount, None, limit=1, timeout=0.005)
    assert time.monotonic() - started < 0.2
    assert all(item.state == "unavailable" and item.reason == "deadline exceeded" for item in timeouts)
    assert saturated.state == "unavailable" and saturated.reason == "provider busy"
    provider_threads = [
        thread for thread in threading.enumerate() if thread.name.startswith("dp-catalog-provider")
    ]
    assert len(provider_threads) <= _PROVIDER_READ_CONCURRENCY
    time.sleep(0.25)
    cancelled = bounded_list_children(_CancelledProvider(), mount, None, limit=1)
    assert cancelled.state == "unavailable" and cancelled.reason == "request cancelled"


def test_resource_failures_are_explicitly_classified():
    with pytest.raises(ValueError, match="must classify"):
        ProviderResourceResult(state="unavailable", reason="ambiguous failure")
    assert ProviderResourceResult(
        state="unavailable", reason="access revoked", failure="permission_lost",
    ).failure == "permission_lost"


def test_malformed_dataset_detail_is_sanitized_as_provider_error():
    class MalformedProvider:
        def dataset_detail(self, *_args, **_kwargs):
            return {"state": "ready", "item": {
                "id": "dataset", "kind": "dataset", "name": "dataset", "uri": "fixture://data",
                "columns": [{"name": f"column-{index}", "type": "int"}
                            for index in range(2049)],
            }}

    result = bounded_dataset_detail(
        MalformedProvider(), CatalogMount(id="mount", provider="fixture"), "dataset")
    assert result.state == "unavailable"
    assert result.failure == "provider_error"
    assert result.reason == "provider dataset detail is invalid"


def test_constructed_dataset_detail_instance_is_revalidated():
    invalid_item = CatalogResource.model_construct(
        id="dataset", kind="dataset", name="dataset", uri="x" * 8193, columns=[])
    invalid_result = ProviderResourceResult.model_construct(
        state="ready", item=invalid_item, reason=None, failure=None)

    class ConstructedProvider:
        def dataset_detail(self, *_args, **_kwargs):
            return invalid_result

    result = bounded_dataset_detail(
        ConstructedProvider(), CatalogMount(id="mount", provider="fixture"), "dataset")
    assert result.state == "unavailable"
    assert result.failure == "provider_error"
    assert result.reason == "provider dataset detail is invalid"


def test_constructed_browse_resolve_and_ancestor_instances_are_revalidated():
    invalid_item = CatalogResource.model_construct(
        id="dataset", kind="dataset", name="x" * 513, uri="fixture://data", columns=[])

    class ConstructedProvider:
        def list_children(self, *_args, **_kwargs):
            return ProviderPage.model_construct(
                state="ready", items=[invalid_item], next_cursor=None, reason=None)

        def resolve(self, *_args, **_kwargs):
            return ProviderResourceResult.model_construct(
                state="ready", item=invalid_item, reason=None, failure=None)

        def ancestors(self, *_args, **_kwargs):
            return ProviderAncestors.model_construct(
                state="ready", items=[invalid_item], reason=None)

    provider = ConstructedProvider()
    mount = CatalogMount(id="mount", provider="fixture")
    listed = bounded_list_children(provider, mount, None, limit=10)
    assert listed.state == "unavailable"
    assert listed.reason == "provider list result is invalid"
    resolved = bounded_resolve(provider, mount, "dataset")
    assert resolved.state == "unavailable" and resolved.failure == "provider_error"
    assert resolved.reason == "provider resolve result is invalid"
    ancestors = bounded_ancestors(provider, mount, "dataset")
    assert ancestors.state == "unavailable"
    assert ancestors.reason == "provider ancestor result is invalid"


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, check=False, text=True, capture_output=True)


def test_file_provider_wheel_passes_public_conformance(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    kernel = repo / "kernel"
    plugin = repo / "examples" / "plugins" / "dp_file_catalog_provider"
    uv = shutil.which("uv")
    assert uv is not None, "the supported wheel conformance path requires uv"

    core_dist, plugin_dist = tmp_path / "core-dist", tmp_path / "plugin-dist"
    assert _run([uv, "build", "--wheel", "--out-dir", str(core_dist)], cwd=kernel).returncode == 0
    assert _run([uv, "build", "--wheel", "--out-dir", str(plugin_dist)], cwd=plugin).returncode == 0
    core_wheel, = core_dist.glob("data_playground-*.whl")
    plugin_wheel, = plugin_dist.glob("dp_file_catalog_provider-*.whl")
    venv = tmp_path / "venv"
    assert _run([uv, "venv", str(venv)], cwd=tmp_path).returncode == 0
    python = venv / "bin" / "python"
    install = _run([
        uv, "pip", "install", "--python", str(python),
        str(core_wheel), str(plugin_wheel), "httpx2>=2.5",
    ], cwd=tmp_path)
    assert install.returncode == 0, install.stderr

    root = tmp_path / "catalog"
    exact_resources = _resources("reference.csv")
    exact_resources[1]["revisionId"] = "provider-dataset-a-v1"
    _write_catalog(root, exact_resources)
    (root / "reference.csv").write_text("id\n1\n2\n")
    clean_env = os.environ.copy()
    for key in tuple(clean_env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            clean_env.pop(key)
    checked = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", "dp-file-catalog",
         "--mount-id", "reference-mount", "--config", f"root={root}"], cwd=tmp_path, env=clean_env)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "catalog provider conformance passed"

    second_root = tmp_path / "catalog-two"
    _write_catalog(second_root, _resources("reference-two.csv"))
    (second_root / "reference-two.csv").write_text("id\n3\n4\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mixed_env = {
        **clean_env,
        "DP_WORKSPACE": str(workspace),
        "DP_DATA_DIR": str(workspace / "data"),
        "DP_DATABASE_URL": f"sqlite:///{workspace / 'dataplay.db'}",
        "DP_EXECUTION": "local-out-of-core",
        "DP_CATALOG_MOUNTS": json.dumps([
            {"id": "wheel-a", "provider": "dp-file-catalog", "config": {"root": str(root)}},
            {"id": "wheel-b", "provider": "dp-file-catalog", "config": {"root": str(second_root)}},
        ]),
    }
    composed = _run([str(python), "-c", """
from hub import metadb, workspace_providers
metadb.migrate_db()
page = workspace_providers.browse(
    metadb.LOCAL_WORKSPACE_ROOT_ID, uid=metadb.DEFAULT_USER_ID, limit=100)
duplicates = [item for item in page['items']
              if item['name'] == 'shared' and item.get('resourceId') == 'dataset-a']
assert {item['mountId'] for item in duplicates} == {'wheel-a', 'wheel-b'}
assert len({item['id'] for item in duplicates}) == 2
assert all(source['completeness'] == 'complete' for source in page['sources'])
print('installed provider Workspace composition passed')
"""], cwd=tmp_path, env=mixed_env)
    assert composed.returncode == 0, composed.stderr
    assert composed.stdout.strip().endswith("installed provider Workspace composition passed")

    acceptance_state = tmp_path / "provider-acceptance.json"
    journey = _run([str(python), "-c", r'''
import json
import os
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app

with TestClient(app) as client:
    root = client.get(
        f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}",
        params={"limit": 100},
    )
    assert root.status_code == 200, root.text
    page = root.json()
    resource = next(
        item for item in page["items"]
        if item.get("mountId") == "wheel-a" and item.get("resourceId") == "dataset-a"
    )
    created = client.post("/api/workspace/canvases", json={
        "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
        "expectedContainerVersion": page["container"]["version"],
        "name": "Installed provider exact journey",
        "providerDatasetRefs": [resource["id"]],
    })
    assert created.status_code == 200, created.text
    canvas_id = created.json()["id"]
    graph_response = client.get(f"/api/canvas/{canvas_id}")
    assert graph_response.status_code == 200, graph_response.text
    graph = graph_response.json()
    source = graph["nodes"][0]
    config = source["data"]["config"]
    assert config["uri"].startswith("workspace-provider://")
    assert config["providerReadMode"] == "exact"
    assert config["datasetRef"]["revisionId"] == "provider-dataset-a-v1"
    assert os.environ["DP_CATALOG_MOUNTS"] not in json.dumps(graph)
    assert str(Path(os.environ["DP_WORKSPACE"]).parent / "catalog") not in json.dumps(graph)

    preview = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": source["id"], "k": 10,
    })
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["rows"] == [{"id": 1}, {"id": 2}]
    inputs = preview_body["inputManifest"]
    assert len(inputs) == 1
    assert inputs[0]["revision_id"] == "provider-dataset-a-v1"
    assert inputs[0]["provider"] == "dp-file-catalog-exact"

    started = client.post("/api/run", json={
        "graph": graph,
        "targetNodeId": source["id"],
        "confirmed": True,
        "submissionId": str(uuid.uuid4()),
        "inputManifest": inputs,
    })
    assert started.status_code == 200, started.text
    run_id = started.json()["runId"]
    deadline = time.monotonic() + 20
    final = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/run/{run_id}")
        assert status.status_code == 200, status.text
        final = status.json()
        if final["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert final is not None and final["status"] == "done", final
    assert final["totalRows"] == 2

    history = None
    history_deadline = time.monotonic() + 5
    while time.monotonic() < history_deadline:
        history_response = client.get(f"/api/canvas/{canvas_id}/runs")
        assert history_response.status_code == 200, history_response.text
        history = next(
            (item for item in history_response.json() if item.get("runId") == run_id), None)
        if history is not None:
            break
        time.sleep(0.05)
    assert history is not None, "terminal run was not projected into Canvas history"
    assert history["status"] == "done" and history["rows"] == 2
    assert history["inputManifest"] == inputs
    assert history["executionManifestAvailability"] == "available"
    manifest_response = client.get(
        f"/api/canvas/{canvas_id}/runs/{history['id']}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()
    assert manifest["availability"] == "available"
    assert manifest["document"]["admittedInputs"] == [{
        "nodeId": inputs[0]["node_id"],
        "datasetId": inputs[0]["dataset_id"],
        "revisionId": inputs[0]["revision_id"],
        "provider": inputs[0]["provider"],
    }]
    assert "dp-file-catalog://" not in json.dumps(graph)
    assert "dp-file-catalog://" not in json.dumps(history)
    assert "dp-file-catalog://" not in json.dumps(manifest)
    Path(os.environ["ACCEPTANCE_STATE"]).write_text(json.dumps({
        "canvas_id": canvas_id, "run_id": run_id, "source_id": source["id"],
        "input_manifest": inputs,
    }))
print("installed provider exact run passed")
'''], cwd=tmp_path, env={**mixed_env, "ACCEPTANCE_STATE": str(acceptance_state)})
    assert journey.returncode == 0, journey.stderr
    assert journey.stdout.strip().endswith("installed provider exact run passed")
    assert "dp-file-catalog://" not in journey.stdout + journey.stderr

    restarted = _run([str(python), "-c", r'''
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from hub.main import app

state = json.loads(Path(os.environ["ACCEPTANCE_STATE"]).read_text())
with TestClient(app) as client:
    graph_response = client.get(f"/api/canvas/{state['canvas_id']}")
    assert graph_response.status_code == 200, graph_response.text
    graph = graph_response.json()
    source = next(node for node in graph["nodes"] if node["id"] == state["source_id"])
    assert source["data"]["config"]["datasetRef"]["revisionId"] == "provider-dataset-a-v1"
    preview = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": source["id"], "k": 10,
        "inputManifest": state["input_manifest"],
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"] == [{"id": 1}, {"id": 2}]
    history_response = client.get(f"/api/canvas/{state['canvas_id']}/runs")
    assert history_response.status_code == 200, history_response.text
    history = next(
        item for item in history_response.json() if item.get("runId") == state["run_id"])
    assert history["status"] == "done"
    assert history["inputManifest"] == state["input_manifest"]
    manifest = client.get(
        f"/api/canvas/{state['canvas_id']}/runs/{history['id']}/manifest")
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["availability"] == "available"
print("installed provider restart evidence passed")
'''], cwd=tmp_path, env={**mixed_env, "ACCEPTANCE_STATE": str(acceptance_state)})
    assert restarted.returncode == 0, restarted.stderr
    assert restarted.stdout.strip().endswith("installed provider restart evidence passed")

    mutable = _run([str(python), "-c", r'''
import os
from pathlib import Path

from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app

with TestClient(app) as client:
    root = client.get(
        f"/api/workspace/containers/{metadb.LOCAL_WORKSPACE_ROOT_ID}",
        params={"limit": 100},
    )
    assert root.status_code == 200, root.text
    page = root.json()
    resource = next(
        item for item in page["items"]
        if item.get("mountId") == "wheel-b" and item.get("resourceId") == "dataset-a"
    )
    created = client.post("/api/workspace/canvases", json={
        "containerId": metadb.LOCAL_WORKSPACE_ROOT_ID,
        "expectedContainerVersion": page["container"]["version"],
        "name": "Installed provider mutable journey",
        "providerDatasetRefs": [resource["id"]],
    })
    assert created.status_code == 200, created.text
    graph = client.get(f"/api/canvas/{created.json()['id']}").json()
    source = graph["nodes"][0]
    config = source["data"]["config"]
    assert config["providerReadMode"] == "mutable"
    assert "datasetRef" not in config

    first = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": source["id"], "k": 10,
    })
    assert first.status_code == 200, first.text
    assert first.json()["rows"] == [{"id": 3}, {"id": 4}]
    Path(os.environ["MUTABLE_FILE"]).write_text("id\n30\n40\n")
    second = client.post("/api/run/preview", json={
        "graph": graph, "nodeId": source["id"], "k": 10,
    })
    assert second.status_code == 200, second.text
    assert second.json()["rows"] == [{"id": 30}, {"id": 40}]

    rejected = client.post("/api/run", json={
        "graph": graph, "targetNodeId": source["id"], "confirmed": True,
    })
    assert rejected.status_code == 409, rejected.text
    assert "mutable-only" in rejected.json()["detail"]
print("installed provider mutable mutation guard passed")
'''], cwd=tmp_path, env={
        **mixed_env, "MUTABLE_FILE": str(second_root / "reference-two.csv"),
    })
    assert mutable.returncode == 0, mutable.stderr
    assert mutable.stdout.strip().endswith("installed provider mutable mutation guard passed")

    unique_names = _resources("reference.csv")
    unique_names[1]["name"] = "different"
    (root / "catalog.json").write_text(json.dumps({"resources": unique_names}))
    invalid_fixture = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", "dp-file-catalog",
         "--mount-id", "reference-mount", "--config", f"root={root}"], cwd=tmp_path, env=clean_env)
    assert invalid_fixture.returncode == 1
    assert invalid_fixture.stderr.strip() == "capability: provider did not preserve duplicate display names"

    secret = "config-should-not-leak"
    rejected = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", secret,
         "--mount-id", "reference-mount", "--config", f"root={root / secret}"], cwd=tmp_path, env=clean_env)
    assert rejected.returncode == 1
    assert rejected.stderr.strip() == "activation: entry point did not provide a read-only catalog provider"
    assert secret not in rejected.stdout + rejected.stderr
