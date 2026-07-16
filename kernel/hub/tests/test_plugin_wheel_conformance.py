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

    env = os.environ.copy()
    for key in tuple(env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            env.pop(key)
    workspace = tmp_path / "workspace"
    secret = "token-should-not-leak"
    telemetry_log = workspace / f"{secret}.jsonl"
    checked = _run(
        [str(python), "-m", "hub.plugin_conformance", "dp-run-log",
         "--workspace", str(workspace), "--telemetry-log", str(telemetry_log)],
        cwd=tmp_path, env=env)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "plugin conformance passed"
    records = [json.loads(line) for line in telemetry_log.read_text().splitlines() if line.strip()]
    assert any(record["run_id"] == "plugin-conformance" for record in records)
    assert secret not in checked.stdout + checked.stderr

    rejected = _run(
        [str(python), "-m", "hub.plugin_conformance", f"activation-{secret}",
         "--workspace", str(tmp_path / "failure-workspace"),
         "--telemetry-log", str(tmp_path / f"{secret}-failure.jsonl")],
        cwd=tmp_path, env=env)
    assert rejected.returncode == 1
    assert rejected.stderr.strip() == "activation: entry point did not activate"
    assert secret not in rejected.stdout + rejected.stderr

    invalid_log = tmp_path / f"{secret}-directory"
    invalid_log.mkdir()
    capability_failure = _run(
        [str(python), "-m", "hub.plugin_conformance", "dp-run-log",
         "--workspace", str(tmp_path / "capability-workspace"),
         "--telemetry-log", str(invalid_log)],
        cwd=tmp_path, env=env)
    assert capability_failure.returncode == 1
    assert capability_failure.stderr.strip() == "capability: telemetry sink did not produce a valid JSONL record"
    assert secret not in capability_failure.stdout + capability_failure.stderr
