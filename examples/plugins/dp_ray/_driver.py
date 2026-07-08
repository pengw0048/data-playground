"""Isolated Ray driver subprocess for the dp_ray backend (see __init__.py).

Spawned by RayRunner._supervise as `python _driver.py <job.json>`. Runs the clean IR on Ray in a FRESH
process whose MAIN thread initializes Ray BEFORE any DuckDB is created — which is what avoids the
in-process DuckDB↔Ray deadlock that makes inline execution hang. Reads a job file (workspace, data_dir,
graph, target, the dp_ray module path), executes, and writes a result JSON to the status file the parent
polls. Any failure is captured into that status file so the parent never waits forever.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys


def _log(m: str) -> None:
    print(f"[driver] {m}", flush=True)


def main() -> None:
    job = json.load(open(sys.argv[1]))
    status_file = job["status_file"]
    result = {"status": "failed", "error": "ray driver did not run", "rows": 0}
    try:
        os.environ.setdefault("RAY_DATA_DISABLE_PROGRESS_BARS", "1")
        _log("import ray + init")
        import ray
        ray.init(ignore_reinit_error=True, configure_logging=False, log_to_driver=False, include_dashboard=False)
        _log("ray init done; set_workspace")

        from hub.deps import set_workspace
        from hub.ir import lower_to_ir
        from hub.models import Graph

        deps = set_workspace(job["workspace"], job["data_dir"])  # fresh DuckDB, created AFTER ray.init
        _log("deps built; load module")
        spec = importlib.util.spec_from_file_location("dp_ray_driver_mod", job["module"])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        runner = mod.RayRunner(deps)
        graph, target = Graph(**job["graph"]), job["target"]
        ir = lower_to_ir(graph, target, deps.node_specs, deps.node_ir)
        _log("lowered; _run_ir_sync")
        result = runner._run_ir_sync(ir, graph, target)
        _log(f"run done: {result.get('status')}")
    except Exception as e:  # noqa: BLE001 — always leave the parent a status to read
        result = {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}
    finally:
        with open(status_file, "w") as f:
            json.dump(result, f)


if __name__ == "__main__":
    main()
