"""Isolated Ray driver subprocess for the dp_ray backend (see __init__.py).

Spawned by RayRunner._supervise as `python _driver.py <job.json>`. Runs the clean IR on Ray in a FRESH
process whose MAIN thread initializes Ray BEFORE any DuckDB is created — which is what avoids the
in-process DuckDB↔Ray deadlock that makes inline execution hang. Reads a job file (workspace, data_dir,
graph, target, hub-resolved sink URIs, the dp_ray module path), executes, and writes a result JSON to the
status file the parent polls. Any failure is captured there so the parent never waits forever.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import sys
import tempfile


def _log(m: str) -> None:
    print(f"[driver] {m}", flush=True)


def _progress_writer(status_file: str):
    """Rewrite the status file with an interim {running, progress} so the parent surfaces mid-run
    progress for a placed region. The parent tolerates a partial read; main()'s finally writes the term."""
    from hub.plugins.adapters import is_object_uri

    def emit(frac: float, rows: int | None = None) -> None:
        if is_object_uri(status_file):
            return  # Jobs mode uses the official JobStatus stream; the shared artifact is terminal-only
        try:
            with open(status_file, "w") as f:
                json.dump({"status": "running", "progress": float(frac), "rows": int(rows or 0)}, f)
        except OSError:
            pass
    return emit


def _run_job(runner, ir, graph, target, ray_opts, progress, job: dict,
             sink_credentials: dict[str, dict]):
    """Dispatch one verified job while preserving each control plane's sink binding."""
    materialize_uri = job.get("materialize_uri")
    if materialize_uri:
        return runner._run_ir_materialize(
            ir, graph, target, materialize_uri, ray_opts, progress, job.get("attempt_id")
        )
    return runner._run_ir_sync(
        ir,
        graph,
        target,
        ray_opts,
        progress,
        job.get("sink_targets"),
        job.get("attempt_id"),
        sink_attempts=job.get("sink_attempts"),
        sink_contracts=job.get("sink_contracts"),
        sink_credentials=sink_credentials,
    )


def main() -> None:
    from hub.job_artifacts import (canonical_json, ray_job_canonical_fields,
                                   ray_job_envelope_fields, read_json_artifact,
                                   require_exact_object, write_json_artifact,
                                   write_json_artifact_once)

    jobs_mode = len(sys.argv) == 5
    if len(sys.argv) not in (2, 5):
        raise RuntimeError(
            "usage: _driver.py JOB_URI EXPECTED_ATTEMPT_ID EXPECTED_SUBMISSION_ID EXPECTED_ENVELOPE_SHA256"
        )
    job_uri = sys.argv[1]
    job = read_json_artifact(job_uri)
    if jobs_mode:
        expected_attempt, expected_submission, expected_envelope = sys.argv[2:]
        try:
            contract_version = int(job.get("contract_version") or 0)
            canonical_fields = ray_job_canonical_fields(contract_version)
            envelope_fields = ray_job_envelope_fields(contract_version)
        except (AttributeError, TypeError, ValueError) as e:
            raise RuntimeError(str(e)) from e
        job = require_exact_object(job, envelope_fields, label="Ray job artifact")
        canonical = {key: job[key] for key in canonical_fields}
        attempt = hashlib.sha256(canonical_json(canonical)).hexdigest()[:24]
        envelope = {key: job[key] for key in envelope_fields if key != "envelope_sha256"}
        envelope_sha256 = hashlib.sha256(canonical_json(envelope)).hexdigest()
        semantic_env = job["semantic_env"]
        if (job["job_uri"] != job_uri
                or job["attempt_id"] != expected_attempt
                or job["submission_id"] != expected_submission
                or job["envelope_sha256"] != expected_envelope
                or attempt != expected_attempt
                or envelope_sha256 != expected_envelope
                or not isinstance(semantic_env, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in semantic_env.items())
                or hashlib.sha256(canonical_json(semantic_env)).hexdigest() != job["semantic_env_sha256"]):
            raise RuntimeError("Ray job artifact does not match the independently submitted execution binding")
        # The artifact is now fully verified. Apply its frozen non-secret semantics before importing Ray
        # or workload/plugin code; rotatable credentials arrived separately through runtime_env.
        os.environ.update(semantic_env)
    status_file = job.get("result_uri") or job["status_file"]
    result = {"status": "failed", "error": "ray driver did not run", "rows": 0}
    metadata_dir = None
    try:
        from hub.workload_credentials import (
            DESTINATION_CREDENTIAL_REFERENCE_ENV,
            read_fd_capability,
            resolve_reference_capability,
            validate_bindings,
        )
        if jobs_mode:
            sink_contracts = job.get("sink_contracts")
            if (not isinstance(sink_contracts, dict)
                    or any(not isinstance(contract, dict)
                           or "credential" not in contract
                           for contract in sink_contracts.values())):
                raise RuntimeError("Ray job destination credential contract is malformed")
            sink_bindings = validate_bindings({
                step_id: contract.get("credential")
                for step_id, contract in sink_contracts.items()
            })
            sink_credentials = resolve_reference_capability(
                os.environ.pop(DESTINATION_CREDENTIAL_REFERENCE_ENV, None),
                sink_bindings,
            )
        else:
            sink_bindings = validate_bindings(
                job.get("sink_credential_bindings") or {})
            sink_credentials = read_fd_capability(
                job.get("sink_credential_capability"), job["attempt_id"],
                sink_bindings,
            )
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
        _addr = "auto" if os.environ.get("DP_RAY_JOB_MODE") == "1" else (os.environ.get("RAY_ADDRESS") or None)
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
        # A Jobs result lives in object storage, not a local directory. Metadata remains a private temp DB
        # inside the entrypoint container and is never the hub's control-plane identity.
        metadata_dir = tempfile.mkdtemp(prefix="dp-ray-job-metadata-")
        initialize_ephemeral_metadata(metadata_dir)
        from hub.deps import set_workspace
        from hub.ir import lower_to_ir
        from hub.models import Graph

        deps = set_workspace(
            job["workspace"], job["data_dir"], maintain_storage=False
        )  # fresh DuckDB, created AFTER ray.init; parent hub owns shared-storage maintenance
        _log("deps built; load module")
        module_path = job.get("module") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
        spec = importlib.util.spec_from_file_location("dp_ray_driver_mod", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        runner = mod.RayRunner(deps)
        graph, target = Graph(**job["graph"]), job["target"]
        ir = lower_to_ir(graph, target, deps.node_specs, deps.node_ir)
        ray_opts = mod._ray_opts(job.get("requires"))  # region resource need → per-Ray-task placement
        prog = _progress_writer(status_file)
        _log(
            f"lowered; {'_run_ir_materialize' if job.get('materialize_uri') else '_run_ir_sync'}; "
            f"ray_opts={ray_opts}"
        )
        result = _run_job(
            runner, ir, graph, target, ray_opts, prog, job, sink_credentials)
        _log(f"run done: {result.get('status')}")
    except Exception as e:  # noqa: BLE001 — always leave the parent a status to read
        result = {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}
    finally:
        if jobs_mode:
            result = {
                "contract_version": int(job["contract_version"]),
                "attempt_id": job["attempt_id"],
                "submission_id": job["submission_id"],
                "envelope_sha256": job["envelope_sha256"],
                "status": result.get("status", "failed"),
                "rows": int(result.get("rows") or 0),
                "error": result.get("error"),
                "output_uri": result.get("output_uri"),
                "output_table": result.get("output_table"),
                "outputs": result.get("outputs") or [],
            }
        try:
            (write_json_artifact_once if jobs_mode else write_json_artifact)(status_file, result)
        finally:
            if metadata_dir:
                shutil.rmtree(metadata_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
