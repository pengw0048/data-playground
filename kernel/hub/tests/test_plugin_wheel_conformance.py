"""Installed-wheel regression coverage for the public plugin conformance kit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, check=False, text=True, capture_output=True)


def test_run_log_wheel_conformance_uses_only_its_entry_point(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    kernel = repo / "kernel"
    plugin = repo / "examples" / "plugins" / "dp_run_log"
    uv = shutil.which("uv")
    assert uv is not None, "the supported wheel conformance path requires uv"

    core_dist = tmp_path / "core-dist"
    plugin_dist = tmp_path / "plugin-dist"
    assert _run([uv, "build", "--wheel", "--out-dir", str(core_dist)], cwd=kernel).returncode == 0
    assert _run([uv, "build", "--wheel", "--out-dir", str(plugin_dist)], cwd=plugin).returncode == 0
    core_wheel, = core_dist.glob("data_playground-*.whl")
    plugin_wheel, = plugin_dist.glob("dp_run_log-*.whl")

    venv = tmp_path / "venv"
    assert _run([uv, "venv", str(venv)], cwd=tmp_path).returncode == 0
    python = venv / "bin" / "python"
    install = _run(
        [uv, "pip", "install", "--python", str(python), str(core_wheel), str(plugin_wheel)], cwd=tmp_path)
    assert install.returncode == 0, install.stderr

    clean_env = os.environ.copy()
    for key in tuple(clean_env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            clean_env.pop(key)
    workspace = tmp_path / "workspace"
    secret = "token-should-not-leak"
    telemetry_log = workspace / f"{secret}.jsonl"
    decoy_workspace = tmp_path / "decoy-workspace"
    env = clean_env | {
        "DP_WORKSPACE": str(decoy_workspace),
        "DP_DATA_DIR": str(decoy_workspace / "data"),
        "DP_DATABASE_URL": f"sqlite:///{decoy_workspace / 'metadata.db'}",
        "DP_PLUGINS": "dp_run_log",
    }
    checked = _run(
        [str(python), "-m", "hub.plugin_conformance", "dp-run-log",
         "--workspace", str(workspace), "--telemetry-log", str(telemetry_log)],
        cwd=tmp_path, env=env)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "plugin conformance passed"
    records = [json.loads(line) for line in telemetry_log.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["run_id"].startswith("plugin-conformance-")
    assert (workspace / "dataplay.db").exists()
    assert not decoy_workspace.exists()
    assert secret not in checked.stdout + checked.stderr

    repeated = _run(
        [str(python), "-m", "hub.plugin_conformance", "dp-run-log",
         "--workspace", str(workspace), "--telemetry-log", str(telemetry_log)],
        cwd=tmp_path, env=env)
    assert repeated.returncode == 0, repeated.stderr
    repeated_records = [json.loads(line) for line in telemetry_log.read_text().splitlines() if line.strip()]
    assert len(repeated_records) == 2
    assert len({record["run_id"] for record in repeated_records}) == 2

    rejected = _run(
        [str(python), "-m", "hub.plugin_conformance", f"activation-{secret}",
         "--workspace", str(tmp_path / "failure-workspace"),
         "--telemetry-log", str(tmp_path / f"{secret}-failure.jsonl")],
        cwd=tmp_path, env=clean_env)
    assert rejected.returncode == 1
    assert rejected.stderr.strip() == "activation: entry point did not activate"
    assert secret not in rejected.stdout + rejected.stderr

    invalid_log = tmp_path / f"{secret}-directory"
    invalid_log.mkdir()
    capability_failure = _run(
        [str(python), "-m", "hub.plugin_conformance", "dp-run-log",
         "--workspace", str(tmp_path / "capability-workspace"),
         "--telemetry-log", str(invalid_log)],
        cwd=tmp_path, env=clean_env)
    assert capability_failure.returncode == 1
    assert capability_failure.stderr.strip() == "capability: telemetry sink did not produce a valid JSONL record"
    assert secret not in capability_failure.stdout + capability_failure.stderr

    stale_workspace = tmp_path / "stale-workspace"
    stale_workspace.mkdir()
    stale_log = stale_workspace / f"{secret}-stale.jsonl"
    stale_log.write_text('{"run_id":"plugin-conformance"}\n')
    stale_log.chmod(0o444)
    stale_failure = _run(
        [str(python), "-m", "hub.plugin_conformance", "dp-run-log",
         "--workspace", str(stale_workspace), "--telemetry-log", str(stale_log)],
        cwd=tmp_path, env=clean_env)
    assert stale_failure.returncode == 1
    assert stale_failure.stderr.strip() == "capability: telemetry sink did not receive the finished run"
    assert secret not in stale_failure.stdout + stale_failure.stderr


def test_external_wait_fixture_wheel_passes_sanitized_conformance(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    kernel = repo / "kernel"
    plugin = repo / "examples" / "plugins" / "dp_external_wait_fixture"
    uv = shutil.which("uv")
    assert uv is not None, "the supported wheel conformance path requires uv"

    core_dist = tmp_path / "core-dist"
    plugin_dist = tmp_path / "plugin-dist"
    assert _run([uv, "build", "--wheel", "--out-dir", str(core_dist)], cwd=kernel).returncode == 0
    assert _run([uv, "build", "--wheel", "--out-dir", str(plugin_dist)], cwd=plugin).returncode == 0
    core_wheel, = core_dist.glob("data_playground-*.whl")
    plugin_wheel, = plugin_dist.glob("dp_external_wait_fixture-*.whl")

    venv = tmp_path / "venv"
    assert _run([uv, "venv", str(venv)], cwd=tmp_path).returncode == 0
    python = venv / "bin" / "python"
    installed = _run(
        [uv, "pip", "install", "--python", str(python), str(core_wheel), str(plugin_wheel)],
        cwd=tmp_path,
    )
    assert installed.returncode == 0, installed.stderr

    clean_env = os.environ.copy()
    for key in tuple(clean_env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            clean_env.pop(key)
    decoy = tmp_path / "decoy"
    env = clean_env | {
        "DP_WORKSPACE": str(decoy),
        "DP_DATA_DIR": str(decoy / "data"),
        "DP_DATABASE_URL": f"sqlite:///{decoy / 'metadata.db'}",
        "DP_PLUGINS": "module-that-must-not-load",
        "DP_EXECUTION": "provider-that-must-not-load",
    }
    command = [
        str(python), "-m", "hub.external_wait_conformance", "dp-external-wait-fixture",
        "--provider-kind", "fixture-local",
    ]
    checked = _run(command, cwd=tmp_path, env=env)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.strip() == "external-wait conformance passed"
    assert checked.stderr == ""
    assert not decoy.exists()
    assert "external-wait-secret-sentinel" not in checked.stdout + checked.stderr
    assert "/private/configured/path" not in checked.stdout + checked.stderr

    secret = "requested-plugin-secret-sentinel"
    rejected = _run(
        [str(python), "-m", "hub.external_wait_conformance", secret,
         "--provider-kind", "fixture-local"],
        cwd=tmp_path, env=clean_env,
    )
    assert rejected.returncode == 1
    assert rejected.stderr.strip() == "activation: entry_point_inactive"
    assert secret not in rejected.stdout + rejected.stderr


def test_installed_descriptor_fixture_certifies_backend_api_and_execution(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    kernel = repo / "kernel"
    plugin = repo / "examples" / "plugins" / "dp_descriptor_contract"
    uv = shutil.which("uv")
    assert uv is not None, "the supported wheel conformance path requires uv"

    core_dist = tmp_path / "core-dist"
    plugin_dist = tmp_path / "plugin-dist"
    assert _run([uv, "build", "--wheel", "--out-dir", str(core_dist)], cwd=kernel).returncode == 0
    assert _run([uv, "build", "--wheel", "--out-dir", str(plugin_dist)], cwd=plugin).returncode == 0
    core_wheel, = core_dist.glob("data_playground-*.whl")
    plugin_wheel, = plugin_dist.glob("dp_descriptor_contract-*.whl")

    venv = tmp_path / "venv"
    assert _run([uv, "venv", str(venv)], cwd=tmp_path).returncode == 0
    python = venv / "bin" / "python"
    install = _run(
        [uv, "pip", "install", "--python", str(python), str(core_wheel), str(plugin_wheel), "httpx2"],
        cwd=tmp_path)
    assert install.returncode == 0, install.stderr

    workspace = tmp_path / "workspace"
    clean_env = os.environ.copy()
    for key in tuple(clean_env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            clean_env.pop(key)
    env = clean_env | {
        "DP_WORKSPACE": str(workspace),
        "DP_DATA_DIR": str(workspace / "data"),
        "DP_DATABASE_URL": f"sqlite:///{workspace / 'metadata.db'}",
        "DP_EXECUTION": "local-out-of-core",
    }
    checked = _run([str(python), "-c", _DESCRIPTOR_CONFORMANCE_SCRIPT], cwd=tmp_path, env=env)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.strip().endswith("descriptor contract passed")


_DESCRIPTOR_CONFORMANCE_SCRIPT = r'''\
import json
from importlib.resources import files
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient

from hub import metadb

metadb.migrate_db()

data_dir = Path(__import__("os").environ["DP_DATA_DIR"])
data_dir.mkdir(parents=True, exist_ok=True)
first = data_dir / "first.parquet"
second = data_dir / "second.parquet"
duckdb.connect().execute(f"COPY (SELECT 'first' AS source, 1 AS ordinal) TO '{first}' (FORMAT PARQUET)")
duckdb.connect().execute(f"COPY (SELECT 'second' AS source, 2 AS ordinal) TO '{second}' (FORMAT PARQUET)")

from hub.main import app

expected = json.loads(files("dp_descriptor_contract").joinpath("descriptor.json").read_text(encoding="utf-8"))

def node(node_id, kind, config):
    return {
        "id": node_id, "type": kind, "position": {"x": 0, "y": 0},
        "data": {"title": node_id, "config": config},
    }

def edge(source, target, target_handle=None):
    return {
        "id": f"{source}-{target}", "source": source, "target": target,
        "sourceHandle": None, "targetHandle": target_handle, "data": {"wire": "dataset"},
    }

with TestClient(app) as client:
    descriptors = {item["kind"]: item for item in client.get("/api/nodes").json()}
    assert [descriptors[item["kind"]] for item in expected] == expected
    status = next(item for item in client.get("/api/plugins").json()
                  if item["name"] == "dp-descriptor-contract")
    assert status["state"] == "active"
    assert set(status["effective_capabilities"]) == {
        "node:descriptor_contract", "node:descriptor_contract_unavailable",
    }

    canvas_id = "installed-descriptor-contract"
    graph = {
        "id": canvas_id, "name": "installed descriptor contract", "version": 1,
        "nodes": [
            node("first", "source", {"uri": str(first)}),
            node("second", "source", {"uri": str(second)}),
            node("contract", "descriptor_contract", {
                "columns": ["source", "ordinal"], "count": 7, "ratio": 1.25,
            }),
        ],
        "edges": [edge("first", "contract", "items"), edge("second", "contract", "items")],
    }
    saved = client.put(f"/api/canvas/{canvas_id}", json=graph)
    assert saved.status_code == 200, saved.text
    restored = client.get(f"/api/canvas/{canvas_id}")
    assert restored.status_code == 200
    assert restored.json()["nodes"][2]["data"]["config"] == graph["nodes"][2]["data"]["config"]
    assert restored.json()["edges"] == graph["edges"]

    plan = client.post("/api/graph/plan", json={"graph": restored.json(), "targetNodeId": "contract"})
    assert plan.status_code == 200, plan.text
    assert plan.json()["regions"][-1]["unsatisfied"] is False
    assert "cpu" in plan.json()["regions"][-1]["requires"]

    preview = client.post(
        "/api/run/preview", json={"graph": restored.json(), "nodeId": "contract", "k": 10})
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"] == [
        {"source": "first", "ordinal": 1, "input_order": 0,
         "configured_count": 7, "configured_ratio": 1.25},
        {"source": "second", "ordinal": 2, "input_order": 1,
         "configured_count": 7, "configured_ratio": 1.25},
    ]

    for invalid in (
        {"columns": ["source"], "count": "12abc", "ratio": 1.25},
        {"columns": ["source"], "count": 7, "ratio": "Infinity"},
        {"columns": "source", "count": 7, "ratio": 1.25},
    ):
        invalid_graph = json.loads(json.dumps(graph))
        invalid_graph["nodes"][2]["data"]["config"] = invalid
        rejected = client.post(
            "/api/run/preview", json={"graph": invalid_graph, "nodeId": "contract", "k": 10})
        assert rejected.status_code == 400, rejected.text

    unavailable = {
        "id": "installed-descriptor-unavailable", "name": "unavailable", "version": 1,
        "nodes": [
            node("first", "source", {"uri": str(first)}),
            node("unavailable", "descriptor_contract_unavailable", {}),
        ],
        "edges": [edge("first", "unavailable", "in")],
    }
    blocked_preview = client.post(
        "/api/run/preview", json={"graph": unavailable, "nodeId": "unavailable", "k": 10})
    assert blocked_preview.status_code == 200, blocked_preview.text
    assert blocked_preview.json()["notPreviewable"] is True
    assert "not sample-previewable" in blocked_preview.json()["reason"]

    blocked_plan = client.post(
        "/api/graph/plan", json={"graph": unavailable, "targetNodeId": "unavailable"})
    assert blocked_plan.status_code == 200, blocked_plan.text
    region = blocked_plan.json()["regions"][-1]
    assert region["unsatisfied"] is True
    assert "engine=descriptor-contract" in region["requires"]
    rejected = client.post(
        "/api/run/estimate", json={"graph": unavailable, "targetNodeId": "unavailable"})
    assert rejected.status_code == 400
    assert "no registered backend can satisfy required resources" in rejected.json()["detail"]

print("descriptor contract passed")
'''
