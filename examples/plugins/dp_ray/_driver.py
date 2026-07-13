"""Isolated Ray driver subprocess for the dp_ray backend (see __init__.py).

Spawned by RayRunner._supervise as `python _driver.py <job.json>`. Runs the clean IR on Ray in a FRESH
process whose MAIN thread initializes Ray BEFORE any DuckDB is created — which is what avoids the
in-process DuckDB↔Ray deadlock that makes inline execution hang. Reads a job file (workspace, data_dir,
graph, target, hub-resolved sink URIs, the dp_ray module path), executes, and writes a result JSON to the
status file the parent polls. Any failure is captured there so the parent never waits forever.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys


def _log(m: str) -> None:
    print(f"[driver] {m}", flush=True)


def _progress_writer(status_file: str):
    """Rewrite the status file with an interim {running, progress} so the parent surfaces mid-run
    progress for a placed region. The parent tolerates a partial read; main()'s finally writes the term."""
    def emit(frac: float, rows: int | None = None) -> None:
        try:
            with open(status_file, "w") as f:
                json.dump({"status": "running", "progress": float(frac), "rows": int(rows or 0)}, f)
        except OSError:
            pass
    return emit


def main() -> None:
    job = json.load(open(sys.argv[1]))
    status_file = job["status_file"]
    result = {"status": "failed", "error": "ray driver did not run", "rows": 0}
    try:
        os.environ.setdefault("RAY_DATA_DISABLE_PROGRESS_BARS", "1")
        # THE macOS/uv fix: if the kernel was launched via `uv run`, Ray (RAY_ENABLE_UV_RUN_RUNTIME_ENV,
        # default on) re-launches its WORKERS through uv too — which builds a fresh, ray-less .venv, so a
        # worker can't `import ray`, the raylet dies, and the run hangs. Turn it off so workers use THIS
        # interpreter (it has ray). Must precede `import ray` (read once as a module constant).
        os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
        _log("import ray + init")
        import ray
        _ncpu = os.environ.get("DP_RAY_NUM_CPUS")
        # RAY_ADDRESS set → ATTACH to a real multi-node cluster head (docker-compose / KubeRay); unset →
        # start a local head (dev / single-host). num_cpus is ignored when attaching to an existing cluster.
        _addr = os.environ.get("RAY_ADDRESS") or None
        # Ray 2.56 + numpy 2.5 crashes every hash-shuffle (read-only hash array); patch it on the DRIVER
        # here and on every WORKER. The shuffle runs in worker processes, and Ray Data's internal shuffle
        # actors do NOT inherit the job runtime_env hook — but they DO inherit the raylet's env, so set the
        # worker-setup-hook ENV VAR (default_worker.py reads it at startup) before ray.init on a LOCAL head.
        # (On a remote cluster the worker containers set the same var — see docker/ray + deploy/kuberay.)
        from ray._private import ray_constants
        from hub.ray_compat import patch_hash_shuffle, validate_ray_cluster
        patch_hash_shuffle()
        if not _addr:
            os.environ.setdefault(ray_constants.WORKER_PROCESS_SETUP_HOOK_ENV_VAR, "hub.ray_compat.patch_hash_shuffle")
        ray.init(address=_addr, ignore_reinit_error=True, configure_logging=False, log_to_driver=False,
                 include_dashboard=False, num_cpus=int(_ncpu) if (_ncpu and not _addr) else None)
        versions = validate_ray_cluster(ray)
        _log("version handshake OK: " + ", ".join(f"{node}={version}" for node, version in versions.items()))
        # Pin the HASH shuffle so relational ops (groupby keys / later sort/join) partition predictably.
        # Under a SORT strategy Ray silently ignores the group keys — fail loud instead of computing wrong.
        from ray.data import DataContext
        from ray.data.context import ShuffleStrategy
        _strat = DataContext.get_current().shuffle_strategy
        if _strat != ShuffleStrategy.HASH_SHUFFLE:
            raise RuntimeError(f"dp_ray needs HASH_SHUFFLE, got {_strat} — unset RAY_DATA_DEFAULT_SHUFFLE_STRATEGY")
        _log(f"ray init done (address={_addr or 'local'}); set_workspace")

        # Deps expects catalog tables, but a Ray driver does not own hub control state. Use a private
        # disposable metadata DB rather than receiving the hub's unrestricted database identity.
        from hub.workload_env import initialize_ephemeral_metadata
        initialize_ephemeral_metadata(os.path.dirname(status_file))
        from hub.deps import set_workspace
        from hub.ir import lower_to_ir
        from hub.models import Graph

        deps = set_workspace(
            job["workspace"], job["data_dir"], maintain_storage=False
        )  # fresh DuckDB, created AFTER ray.init; parent hub owns shared-storage maintenance
        _log("deps built; load module")
        spec = importlib.util.spec_from_file_location("dp_ray_driver_mod", job["module"])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        runner = mod.RayRunner(deps)
        graph, target = Graph(**job["graph"]), job["target"]
        ir = lower_to_ir(graph, target, deps.node_specs, deps.node_ir)
        mat = job.get("materialize_uri")
        ray_opts = mod._ray_opts(job.get("requires"))  # region resource need → per-Ray-task placement
        prog = _progress_writer(status_file)
        _log(f"lowered; {'_run_ir_materialize' if mat else '_run_ir_sync'}; ray_opts={ray_opts}")
        result = (runner._run_ir_materialize(
            ir, graph, target, mat, ray_opts, prog, job.get("attempt_id")
        ) if mat
                  else runner._run_ir_sync(
                      ir, graph, target, ray_opts, prog, job.get("sink_targets"),
                      job.get("attempt_id"), job.get("sink_attempts"),
                  ))
        _log(f"run done: {result.get('status')}")
    except Exception as e:  # noqa: BLE001 — always leave the parent a status to read
        result = {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}
    finally:
        with open(status_file, "w") as f:
            json.dump(result, f)


if __name__ == "__main__":
    main()
