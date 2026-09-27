"""Request-local cancellation and disposable processes for Python cell previews.

Source readers, their leases, and the warm graph cache remain in the caller. Only a
code-backed Transform receives a bounded Arrow input in a child process. This is a
termination boundary, not an additional security sandbox.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

import pyarrow as pa

from hub.process_scope import OwnedProcessScope, owned_process_popen_kwargs


class PreviewCancelled(Exception):
    def __init__(self):
        super().__init__("Preview stopped. Your code and graph are unchanged.")


class PreviewTimedOut(Exception):
    def __init__(self):
        super().__init__("Preview exceeded its time budget and was stopped. Edit the code and try again.")


class PreviewControl:
    """Signal an exact request; completion is acknowledged by the request owner."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._deadline: float | None = None
        self._interrupts: dict[object, Callable[[], None]] = {}

    def _stop(self, reason: str) -> None:
        with self._lock:
            if self._reason is not None:
                return
            self._reason = reason
            callbacks = list(self._interrupts.values())
        for callback in callbacks:
            with contextlib.suppress(Exception):
                callback()

    def cancel(self) -> None:
        self._stop("cancelled")

    def add_interrupt(self, callback: Callable[[], None]) -> Callable[[], None]:
        key = object()
        with self._lock:
            self._interrupts[key] = callback
            stopped = self._reason is not None
        if stopped:
            with contextlib.suppress(Exception):
                callback()

        def remove() -> None:
            with self._lock:
                self._interrupts.pop(key, None)

        return remove

    def check(self) -> None:
        with self._lock:
            deadline = self._deadline
        if deadline is not None and time.monotonic() >= deadline:
            self._stop("timeout")
        with self._lock:
            reason = self._reason
        if reason == "cancelled":
            raise PreviewCancelled()
        if reason == "timeout":
            raise PreviewTimedOut()

    def run(self, fn: Callable[[], Any], seconds: float) -> Any:
        """Keep ownership until interrupted work exits, including the child's reap."""
        with self._lock:
            self._deadline = time.monotonic() + seconds
        self.check()
        result: list[Any] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                result.append(fn())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=work, daemon=True, name="preview-worker")
        thread.start()
        while thread.is_alive():
            thread.join(0.02)
            # Sending a stop is not proof that work stopped. Keep the source scope and
            # request alive until its worker has unwound, even after the budget elapsed.
            with contextlib.suppress(PreviewCancelled, PreviewTimedOut):
                self.check()
        self.check()
        if errors:
            raise errors[0]
        return result[0] if result else None


def _write_arrow(path: Path, batches, schema) -> None:
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, schema) as writer:
        for batch in batches:
            writer.write_batch(batch)


def run_python_preview(node, batches, schema, *, code: str, mode: str,
                       control: PreviewControl, capture_editor_input: bool):
    """Execute a code-backed cell and return its output and actual editor input evidence."""
    from hub import sandbox
    from hub.models import EditorInputSample

    control.check()
    with tempfile.TemporaryDirectory(prefix="dp-python-preview-") as directory:
        root = Path(directory)
        _write_arrow(root / "input.arrow", batches, schema)
        job = {
            "node": node.model_dump(mode="json", by_alias=True),
            "code": code,
            "mode": mode,
            "captureEditorInput": capture_editor_input,
            "sysPath": list(sys.path),
            "allowedModules": sandbox.allowed_modules(),
            "parentPid": os.getpid(),
        }
        (root / "job.json").write_text(json.dumps(job))
        control.check()
        # stdout/stderr go to files: arbitrary print output cannot deadlock a full pipe.
        with (root / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "hub.python_preview_worker", str(root)],
                **owned_process_popen_kwargs({"stdout": log, "stderr": log}))
            scope = OwnedProcessScope(process, owns_process_group=os.name == "posix")
            remove = control.add_interrupt(scope.request_stop)
            try:
                while process.poll() is None:
                    control.check()
                    time.sleep(0.02)
            finally:
                # Never release the source lease or delete input files while the child can
                # still use them. A rare OS-level failure to reap remains pending.
                while not scope.fence():
                    time.sleep(0.1)
                remove()
        control.check()
        status_file = root / "result.json"
        if not status_file.exists():
            raise RuntimeError(f"Python preview process exited without a result (exit {process.returncode})")
        status = json.loads(status_file.read_text())
        raw_sample = status.get("editorInputSample")
        sample = EditorInputSample.model_validate(raw_sample) if raw_sample is not None else None
        if "error" in status:
            return None, sample, status["error"]
        with pa.OSFile(str(root / "output.arrow"), "rb") as source:
            # Read into owned buffers before the temporary IPC file is removed.
            table = pa.ipc.open_file(source).read_all()
        return table, sample, None


def raise_worker_error(error: dict, node) -> None:
    """Recreate the existing structured diagnostics without unpickling child objects."""
    from hub import sandbox
    from hub.executors.engine import TransformSyntaxError, UserCodeError

    if error["kind"] == "syntax":
        syntax = sandbox.SandboxSyntaxError(SyntaxError(error["message"]))
        syntax.line, syntax.column = error["line"], error["column"]
        raise TransformSyntaxError(node, syntax)
    if error["kind"] == "user":
        exc = UserCodeError(
            node, RuntimeError(error["message"]), row_index=error.get("rowIndex"),
            available_columns=error.get("availableColumns"))
        exc.exception_type = error["exceptionType"]
        exc.guidance = error.get("guidance")
        exc.args = (error["text"],)
        raise exc
    if error["kind"] == "sandbox":
        raise sandbox.SandboxError(error["message"])
    raise RuntimeError(error["text"])
