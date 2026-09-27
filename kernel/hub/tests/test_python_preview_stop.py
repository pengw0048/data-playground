"""A stopped cell is reaped before its request releases inputs or reports completion."""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pyarrow as pa
import pytest

from hub import db, sandbox
from hub.executors import preview
from hub.models import Graph
from hub.python_preview import PreviewControl


def _graph(code):
    return Graph.model_validate({
        "id": "preview-stop", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": "test://preview-input"}}},
            {"id": "cell", "type": "transform", "position": {"x": 200, "y": 0},
             "data": {"config": {"source": "adhoc", "mode": "map", "code": code}}},
        ],
        "edges": [{"id": "edge", "source": "source", "target": "cell",
                   "sourceHandle": "out", "targetHandle": "in", "data": {"wire": "dataset"}}],
    })


class _Adapter:
    def preview_scan(self, _uri, *, limit, **_kwargs):
        return db.conn().from_arrow(pa.table({"value": [7, 11]}).slice(0, limit))

    def fingerprint(self, _uri):
        return "preview-input"


def _preview(code, *, control=None):
    return preview.preview_node(
        _graph(code), "cell", 10, lambda _uri: _Adapter(), object(),
        capture_editor_input=True, control=control)


@pytest.fixture
def process_probe(tmp_path, monkeypatch):
    from hub import python_preview
    marker = tmp_path / "started"
    (tmp_path / "preview_stop_probe.py").write_text(
        "import os, signal\nfrom pathlib import Path\n"
        "def started(ignore_term=False):\n"
        "    if ignore_term:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    Path({str(marker)!r}).write_text(str(os.getpid()))\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    allowed = sandbox.allowed_modules()
    sandbox.allow_modules(["preview_stop_probe"])
    processes = []
    popen = python_preview.subprocess.Popen

    def record_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(python_preview.subprocess, "Popen", record_process)
    try:
        yield marker, processes
    finally:
        sandbox.set_allowed(allowed)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_stop_kills_running_cell_before_releasing_input_then_corrected_code_runs(
        process_probe, monkeypatch):
    marker, processes = process_probe
    control = PreviewControl()
    outputs = []
    released = threading.Event()

    @contextmanager
    def source_lease(*_args, **_kwargs):
        try:
            yield
        finally:
            assert all(process.poll() is not None for process in processes)
            released.set()

    monkeypatch.setattr("hub.storage.source_read_scope", source_lease)
    code = (
        "import preview_stop_probe\ndef fn(row):\n"
        "    preview_stop_probe.started(ignore_term=True)\n"
        "    while True:\n        pass\n")
    worker = threading.Thread(target=lambda: outputs.append(_preview(code, control=control)))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "the real cell must start before cancellation"
        assert not released.is_set()
        assert int(marker.read_text()) == processes[0].pid
        control.cancel()
        worker.join(8)
        assert not worker.is_alive(), "Stop must complete even when a cell ignores TERM"
        assert released.is_set() and processes[0].poll() is not None
        assert outputs[0].failure_category == "cancelled"
        assert outputs[0].editor_input_sample.rows[0]["value"].representation == "7"
    finally:
        control.cancel()
        worker.join(8)

    result = _preview("def fn(row):\n    return {'value': row['value'] * 2}")
    assert not result.error, result.reason
    assert result.rows == [{"value": 14}, {"value": 22}]
    assert all(process.poll() is not None for process in processes)


def test_timeout_kills_top_level_python_and_keeps_input_evidence(process_probe, monkeypatch):
    marker, processes = process_probe
    monkeypatch.setattr(preview, "PREVIEW_BUDGET_S", 3.0)
    result = _preview(
        "import preview_stop_probe\npreview_stop_probe.started()\n"
        "while True:\n    pass\ndef fn(row):\n    return row\n")
    assert marker.exists(), "the authored top-level code must execute before its deadline"
    assert result.failure_category == "timeout"
    assert result.editor_input_sample.rows[0]["value"].representation == "7"
    assert processes[0].poll() is not None
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    recovered = _preview("def fn(row):\n    return row")
    assert not recovered.error and recovered.rows == [{"value": 7}, {"value": 11}]


def test_cancel_before_start_never_allocates_a_child(process_probe):
    _marker, processes = process_probe
    control = PreviewControl()
    control.cancel()
    result = _preview("while True:\n    pass", control=control)
    assert result.failure_category == "cancelled"
    assert processes == []


def test_sample_profile_timeout_reaps_python_process(process_probe, monkeypatch):
    from hub.executors import profile
    marker, processes = process_probe
    monkeypatch.setattr(profile, "PREVIEW_BUDGET_S", 3.0)
    result = profile.profile_node(
        _graph("import preview_stop_probe\ndef fn(row):\n"
               "    preview_stop_probe.started()\n    while True:\n        pass\n"),
        "cell", lambda _uri: _Adapter(), object())
    assert marker.exists()
    assert result.error and "time budget" in result.reason
    assert processes[0].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent-process supervision")
def test_kernel_parent_crash_stops_its_python_preview(tmp_path):
    marker = tmp_path / "orphan-pid"
    (tmp_path / "orphan_preview_probe.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()) + '\\n' + sys.argv[1])\n")
    parent_code = (
        "import sys\nfrom hub import sandbox\n"
        "from hub.tests.test_python_preview_stop import _preview\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "sandbox.allow_modules(['orphan_preview_probe'])\n"
        "_preview('import orphan_preview_probe\\ndef fn(row):\\n    while True:\\n        pass')\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "the real Python child must start before its parent crashes"
        raw_pid, job_directory = marker.read_text().splitlines()
        child_pid = int(raw_pid)
        parent.kill()
        parent.wait(timeout=5)
        # The orphan can briefly remain a zombie until the host's PID 1 reaps it.
        # A zombie is not executing code and no longer owns its memory/files/threads.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True,
                check=False).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("the Python preview kept running after its kernel parent died")
        assert not Path(job_directory).exists(), "orphaned input IPC files must also be removed"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
