from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from hub.models import Graph, RunStatus
from hub.subprocess_runner import SubprocessRunner


def _wait_terminal(runner: SubprocessRunner, run_id: str, timeout: float = 8.0) -> RunStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status(run_id)
        if status.status in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"subprocess run did not become terminal: {run_id}")


def _wait_for_file(path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise AssertionError(f"descendant did not write its marker: {path}")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group containment")
@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("normal", "done"), ("cancel", "cancelled"), ("deadline", "failed")],
)
def test_ordinary_run_fences_descendant_before_terminal(
        tmp_path, monkeypatch, mode, expected_status):
    marker = tmp_path / f"ordinary-{mode}-descendant"
    grandchild = f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    with open({str(marker)!r}, "a") as output:
        output.write("x"); output.flush(); os.fsync(output.fileno())
    time.sleep(0.01)
"""
    terminal = mode == "normal"
    child_tail = "sys.exit(0)" if terminal else "while True: time.sleep(1)"
    child = f"""
import json, os, subprocess, sys, time
job = json.load(open(sys.argv[1]))
subprocess.Popen([sys.executable, "-c", {grandchild!r}])
while not os.path.exists({str(marker)!r}): time.sleep(0.01)
payload = {{"run_id": "child", "status": {"done" if terminal else "running"!r},
           "rows_processed": 0, "ms": 0, "placement": "local", "per_node": []}}
tmp = job["statusFile"] + ".tmp"
json.dump(payload, open(tmp, "w")); os.replace(tmp, job["statusFile"])
{child_tail}
"""
    real_popen = subprocess.Popen

    def popen(command, **kwargs):
        assert command[1:3] == ["-m", "hub.subrun"]
        return real_popen([sys.executable, "-c", child, command[-1]], **kwargs)

    monkeypatch.setattr("hub.subprocess_runner.subprocess.Popen", popen)
    monkeypatch.setattr("hub.subprocess_runner._CANCEL_GRACE_S", 0.01)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SubprocessRunner(
        str(workspace), str(tmp_path / "data"),
        deadline_s=0.25 if mode == "deadline" else 10.0,
    )
    run_id = f"ordinary-{mode}"
    graph = Graph(id=run_id, version=1, nodes=[], edges=[])
    status = RunStatus(
        run_id=run_id, status="queued", placement="local", per_node=[])
    try:
        runner._spawn(status, {}, graph, None)
        _wait_for_file(marker)
        if mode == "cancel":
            runner.cancel(run_id)
        final = _wait_terminal(runner, run_id)
        size_at_terminal = marker.stat().st_size
        time.sleep(0.2)

        assert final.status == expected_status
        if mode == "deadline":
            assert "deadline" in (final.error or "")
        assert marker.stat().st_size == size_at_terminal
        assert run_id not in runner._process_scopes
    finally:
        runner._terminate_all()
