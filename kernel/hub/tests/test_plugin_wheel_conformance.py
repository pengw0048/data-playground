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
