"""Child entrypoint for the subprocess execution backend (hub.subprocess_runner).

Runs ONE job in an isolated process: `python -m hub.subrun <job.json>`. The job file gives the
workspace, the graph, the target, and a status-file path. We rebuild Deps for the workspace, run the
plan with the in-process LocalRunner, and write its status JSON (atomically) to the status file for
the parent to poll. A clean, guarded __main__ keeps this robust regardless of how the kernel was
launched (no multiprocessing 'spawn' re-import surprises).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def _atomic_write(path: str, obj: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)  # atomic — the parent never reads a half-written file


def main() -> int:
    job = json.load(open(sys.argv[1]))
    status_file = job["statusFile"]
    cancel_file = job.get("cancelFile")
    try:
        # The worker needs catalog tables for normal Deps composition, not the hub's users/settings/run
        # state. Build a private disposable DB before importing settings instead of forwarding the hub
        # metadata credential into caller-controlled code.
        from hub.workload_env import initialize_ephemeral_metadata
        initialize_ephemeral_metadata(os.path.dirname(status_file))
        from hub import compiler
        from hub.deps import set_workspace
        from hub.models import Graph
        deps = set_workspace(job["workspace"], job["dataDir"])
        # the PARENT (SubprocessRunner) owns ALL run_states writes and run-history recording: it reads our
        # status file and persists under the authoritative (hub) run_id. If we also wrote, we'd emit orphan
        # rows keyed by the child's own id (and race a hard-kill on cancel). Silence both hooks here.
        deps.runner.on_complete = None
        deps.runner.on_status = None
        graph = Graph(**job["graph"])
        external_cancel = (lambda: os.path.exists(cancel_file)) if cancel_file else None
        # deps parity with the warm kernel's _ensure_deps: install the canvas's declared pip deps and let
        # the sandbox import EXACTLY them (and put the deps dir on THIS process's sys.path). A fresh child
        # doesn't inherit the kernel's sys.path/allow-list, so without this a run-time import in a cell
        # would fail in isolation though it works in-kernel.
        from hub import kernel_deps, sandbox
        from hub.settings import settings as _s
        if _s.canvas_pip_deps:
            reqs = getattr(graph, "requirements", None) or []
            mods = kernel_deps.ensure(reqs, kernel_deps.deps_dir(job["workspace"], getattr(graph, "id", "canvas"))) if reqs else set()
            sandbox.set_allowed(mods)
        else:
            sandbox.set_allowed(set())
        # region-materialize mode (RunController C3): run this sub-graph in THIS worker process and
        # write the target node's relation to a given uri — how a placed region executes on its worker.
        mat_uri = job.get("materializeUri")
        if mat_uri:
            if external_cancel and external_cancel():
                _atomic_write(status_file, {"run_id": "child", "status": "cancelled", "per_node": [],
                                            "rows_processed": 0, "ms": 0, "placement": "local"})
                return 1
            _atomic_write(status_file, {"run_id": "child", "status": "running", "per_node": [],
                                        "rows_processed": 0, "ms": 0, "placement": "local"})
            from hub import db
            from hub.executors.engine import BuildEngine
            from hub.plugins.runner import _CancelToken
            try:
                with db.run_scope() as scope:
                    monitor_done = threading.Event()

                    def _interrupt_on_cancel() -> None:
                        while not monitor_done.wait(0.05):
                            if external_cancel and external_cancel():
                                scope.interrupt()
                                return

                    threading.Thread(target=_interrupt_on_cancel, daemon=True).start()
                    try:
                        eng = BuildEngine(graph, deps.resolve_adapter, deps.registry, full=True,
                                             node_builders=deps.node_builders, node_specs=deps.node_specs,
                                             pushdown=True, output_node=job.get("target"))
                        rel = eng.relation(job.get("target"))
                        adapter = deps.resolve_adapter(mat_uri)
                        deps.runner._adapter_write(adapter, mat_uri, rel, "overwrite",
                                                   _CancelToken(external_cancel))
                    finally:
                        monitor_done.set()
            except Exception:
                if external_cancel and external_cancel():
                    _atomic_write(status_file, {"run_id": "child", "status": "cancelled", "per_node": [],
                                                "rows_processed": 0, "ms": 0, "placement": "local"})
                    return 1
                raise
            _atomic_write(status_file, {"run_id": "child", "status": "done", "per_node": [], "rows_processed": 0,
                                        "ms": 0, "placement": "local", "output_uri": mat_uri})
            return 0
        plan = compiler.compile_plan(graph, job.get("target"), deps.registry, deps.node_specs, deps.node_ir)
        if external_cancel and external_cancel():
            _atomic_write(status_file, {"run_id": "child", "status": "cancelled", "per_node": [],
                                        "rows_processed": 0, "ms": 0, "placement": "local"})
            return 1
        # Read the parent's request directly at every LocalRunner/adapter fence; the polling loop below
        # additionally calls cancel() to interrupt an in-flight DuckDB cursor.
        st = deps.runner.run(plan, graph, job.get("target"), "local", cancel_check=external_cancel)
        rid = st.run_id
        cancel_requested = False
        while True:
            if not cancel_requested and cancel_file and os.path.exists(cancel_file):
                deps.runner.cancel(rid)  # cooperative cursor interrupt + adapter pre-publish fence
                cancel_requested = True
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
