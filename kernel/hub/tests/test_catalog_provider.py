"""Contract and installed-wheel coverage for read-only catalog mounts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from hub.catalog_provider import CatalogMount, ProviderPage, bounded_list_children


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
    assert first_dataset.uri == "file:///first.parquet"
    assert second_dataset.uri == "file:///second.parquet"
    assert first_dataset.columns[0].name == "id"
    assert [item.id for item in catalog.ancestors(first, "nested-dataset").items] == ["container-a"]


class _SlowProvider:
    def list_children(self, *_args, **_kwargs):
        time.sleep(0.4)
        return ProviderPage()


class _CancelledProvider:
    def list_children(self, *_args, **_kwargs):
        raise asyncio.CancelledError()


def test_bounded_listing_normalizes_deadline_and_cancellation_without_waiting():
    mount = CatalogMount(id="local", provider="test")
    started = time.monotonic()
    timeout = bounded_list_children(_SlowProvider(), mount, None, limit=1, timeout=0.02)
    assert time.monotonic() - started < 0.2
    assert timeout.state == "unavailable" and timeout.reason == "deadline exceeded"
    cancelled = bounded_list_children(_CancelledProvider(), mount, None, limit=1)
    assert cancelled.state == "unavailable" and cancelled.reason == "request cancelled"


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
    install = _run([uv, "pip", "install", "--python", str(python), str(core_wheel), str(plugin_wheel)], cwd=tmp_path)
    assert install.returncode == 0, install.stderr

    root = tmp_path / "catalog"
    _write_catalog(root, _resources("file:///reference.parquet"))
    clean_env = os.environ.copy()
    for key in tuple(clean_env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            clean_env.pop(key)
    checked = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", "dp-file-catalog",
         "--mount-id", "reference-mount", "--config", f"root={root}"], cwd=tmp_path, env=clean_env)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "catalog provider conformance passed"

    secret = "config-should-not-leak"
    rejected = _run(
        [str(python), "-m", "hub.catalog_provider_conformance", secret,
         "--mount-id", "reference-mount", "--config", f"root={root / secret}"], cwd=tmp_path, env=clean_env)
    assert rejected.returncode == 1
    assert rejected.stderr.strip() == "activation: entry point did not provide a read-only catalog provider"
    assert secret not in rejected.stdout + rejected.stderr
