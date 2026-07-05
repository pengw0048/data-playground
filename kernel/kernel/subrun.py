"""Child entrypoint for the subprocess execution backend (kernel.subprocess_runner).

Runs ONE job in an isolated process: `python -m kernel.subrun <job.json>`. The job file gives the
workspace, the graph, the target, and a status-file path. We rebuild Deps for the workspace, run the
plan with the in-process LocalRunner, and write its status JSON (atomically) to the status file for
the parent to poll. A clean, guarded __main__ keeps this robust regardless of how the kernel was
launched (no multiprocessing 'spawn' re-import surprises).
"""

from __future__ import annotations

import json
import os
import sys
import time


def _atomic_write(path: str, obj: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)  # atomic — the parent never reads a half-written file


def main() -> int:
    job = json.load(open(sys.argv[1]))
    status_file = job["statusFile"]
    try:
        from kernel import compiler
        from kernel.deps import set_workspace
        from kernel.models import Graph
        deps = set_workspace(job["workspace"], job["dataDir"])
        # the PARENT (SubprocessRunner) owns run-history recording — it reads our terminal status file
        # and persists it. Recording here would race: on_complete runs in a daemon thread AFTER status
        # flips to "done", and this process can exit (or be hard-killed on cancel) before it commits.
        deps.runner.on_complete = None
        graph = Graph(**job["graph"])
        plan = compiler.compile_plan(graph, job.get("target"), deps.registry, deps.node_specs)
        st = deps.runner.run(plan, graph, job.get("target"), "local")  # in-process runner, in THIS process
        rid = st.run_id
        while True:
            s = deps.runner.status(rid)
            _atomic_write(status_file, s.model_dump())
            if s.status in ("done", "failed", "cancelled"):
                return 0 if s.status == "done" else 1
            time.sleep(0.12)
    except Exception as e:  # noqa: BLE001
        _atomic_write(status_file, {"run_id": "child", "status": "failed", "per_node": [],
                                    "rows_processed": 0, "ms": 0, "placement": "local",
                                    "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
