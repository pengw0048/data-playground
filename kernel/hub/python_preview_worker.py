"""Private child entrypoint for one bounded code-backed Transform preview."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time


def _watch_parent(parent_pid: int, directory: Path) -> None:
    """Retire an orphaned POSIX preview after its kernel crashes or hard-restarts.

    This thread handles ordinary Python loops; native extensions holding the GIL
    indefinitely still need the live parent's external process termination.
    """
    while os.getppid() == parent_pid:
        time.sleep(0.1)
    # The owning parent's TemporaryDirectory cleanup cannot run after a hard crash.
    shutil.rmtree(directory, ignore_errors=True)
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(1)


def main(directory: str) -> None:
    root = Path(directory)
    job = json.loads((root / "job.json").read_text())
    if os.name == "posix":
        threading.Thread(
            target=_watch_parent, args=(job["parentPid"], root), daemon=True,
            name="preview-parent-watch").start()
    # Preserve the per-canvas dependency target and its transitive import allow-set.
    sys.path[:] = job["sysPath"]
    import pyarrow as pa
    from hub import db, sandbox
    from hub.executors.engine import BuildEngine, TransformSyntaxError, UserCodeError
    from hub.models import Graph, GraphNode
    from hub.python_preview import _write_arrow

    sandbox.set_allowed(job["allowedModules"])
    node = GraphNode.model_validate(job["node"])
    node.data = dict(node.data)
    node.data["config"] = {
        **node.data.get("config", {}), "source": "adhoc",
        "code": job["code"], "mode": job["mode"],
    }
    graph = Graph(id="python-preview", version=1, nodes=[node], edges=[])
    engine = BuildEngine(
        graph, lambda _uri: None, None,
        editor_input_node=node.id if job["captureEditorInput"] else None)
    result: dict = {}
    try:
        with pa.memory_map(str(root / "input.arrow"), "r") as source:
            table = pa.ipc.open_file(source).read_all()
            with db.run_scope():
                output = engine._transform(node, db.conn().from_arrow(table)).to_arrow_table()
                _write_arrow(root / "output.arrow", output.to_batches(), output.schema)
    except TransformSyntaxError as exc:
        result["error"] = {
            "kind": "syntax", "line": exc.line, "column": exc.column, "message": exc.message}
    except UserCodeError as exc:
        result["error"] = {
            "kind": "user", "message": exc.message, "exceptionType": exc.exception_type,
            "rowIndex": exc.row_index, "availableColumns": exc.available_columns,
            "guidance": exc.guidance, "text": str(exc),
        }
    except sandbox.SandboxError as exc:
        result["error"] = {"kind": "sandbox", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result["error"] = {"kind": "other", "text": f"{type(exc).__name__}: {exc}"}
    if engine.editor_input_sample is not None:
        result["editorInputSample"] = engine.editor_input_sample.model_dump(mode="json")
    (root / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1])
