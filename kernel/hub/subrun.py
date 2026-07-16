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
import stat
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # Windows receives no inherited resultLockFd and disables automatic crash GC.
    fcntl = None


def _atomic_write(path: str, obj: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)  # atomic — the parent never reads a half-written file


def _materialize_region(deps, rel, mat_uri: str, cancel, external_cancel, run_id: str) -> int:
    """Write a controller handoff, committing managed object attempts with a manifest last."""
    from hub.plugins.adapters import is_object_uri
    from hub.handoff import is_attempt_uri, write_manifest

    managed_attempt = is_object_uri(mat_uri) and is_attempt_uri(mat_uri)
    physical_uri = (
        mat_uri.rstrip("/") + "/part-00000.parquet" if managed_attempt else mat_uri)
    adapter = deps.resolve_adapter(physical_uri)
    result = deps.runner._adapter_write(adapter, physical_uri, rel, "overwrite", cancel)
    rows = int(result.get("rows") or 0)
    if external_cancel and external_cancel():
        raise RuntimeError("region materialization cancelled before commit")
    if managed_attempt:
        schema = list(zip(rel.columns, (str(t) for t in rel.types)))
        write_manifest(mat_uri, run_id=run_id, rows=rows, schema=schema)
    return rows


def _parent_attested_source_uris(job: dict, graph) -> frozenset[str]:
    """Validate the exact managed-source contract before graph compilation or adapter scanning."""
    from hub import graph as graph_mod
    from hub.handoff import (
        has_attempt_path_component, is_attempt_uri, physical_attempt_uri)
    from hub.plugins.adapters import is_object_uri
    from hub.paths import local_path
    from hub.storage import MAX_MANAGED_EXECUTION_SOURCES

    source_attempts: set[str] = set()
    local_sources: set[str] = set()
    for uri in graph_mod.execution_source_uris(graph, job.get("target")):
        normalized = str(uri).rstrip("/")
        try:
            path = local_path(normalized)
        except ValueError as exc:
            raise RuntimeError("managed local-source URI is not canonical") from exc
        if (path is not None
                and os.path.basename(os.path.dirname(path)) == ".dp-results"
                and os.path.basename(path).startswith("__result_")
                and path.endswith(".parquet")):
            local_sources.add(path)
            continue
        if not is_object_uri(normalized) or not has_attempt_path_component(normalized):
            continue
        if not is_attempt_uri(normalized):
            raise RuntimeError("managed source must reference the exact attempt root")
        source_attempts.add(normalized)
    if len(source_attempts | local_sources) > MAX_MANAGED_EXECUTION_SOURCES:
        raise RuntimeError(
            f"an execution may use at most {MAX_MANAGED_EXECUTION_SOURCES} managed sources")
    raw = job.get("managedSourceAttempts")
    if not isinstance(raw, dict):
        raise RuntimeError("managed source attestation contract is missing")
    if len(raw) > MAX_MANAGED_EXECUTION_SOURCES:
        raise RuntimeError("managed source attestation contract exceeds the source limit")
    attested: set[str] = set()
    for uri, identity in raw.items():
        normalized = str(uri).rstrip("/")
        if not isinstance(identity, dict):
            raise RuntimeError("managed source attestation contract is malformed")
        try:
            expected = physical_attempt_uri(
                str(identity["logicalUri"]), str(identity["storageNamespace"]),
                int(identity["generation"]), str(identity["attemptId"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("managed source attestation contract is malformed") from exc
        if (expected.rstrip("/") != normalized
                or identity.get("kind") not in ("region", "sink")):
            raise RuntimeError("managed source attestation identity does not match its URI")
        attested.add(normalized)
    if source_attempts != set(attested):
        raise RuntimeError("managed source attestation contract does not match the graph")
    raw_local = job.get("managedLocalSources")
    if not isinstance(raw_local, dict) or not all(
            isinstance(uri, str) and isinstance(contract, dict)
            for uri, contract in raw_local.items()):
        raise RuntimeError("managed local-source attestation contract is missing or malformed")
    if len(raw) + len(raw_local) > MAX_MANAGED_EXECUTION_SOURCES:
        raise RuntimeError("managed source attestation contract exceeds the source limit")
    attested_local: set[str] = set()
    for uri in raw_local:
        try:
            path = local_path(uri)
        except ValueError as exc:
            raise RuntimeError("managed local-source attestation contract is malformed") from exc
        if path is None:
            raise RuntimeError("managed local-source attestation contract is malformed")
        attested_local.add(path)
    if local_sources != attested_local:
        raise RuntimeError("managed local-source attestation contract does not match the graph")
    return frozenset(attested | attested_local)


def _validate_local_source_locks(job: dict, storage) -> None:
    """Bind every parent-attested source URI to this namespace and its inherited lock inode."""
    raw = job.get("managedLocalSources")
    if not isinstance(raw, dict):
        raise RuntimeError("managed local-source attestation contract is missing")
    if not raw:
        return
    expected_identity = tuple(storage.result_namespace_identity())
    for uri, contract in raw.items():
        identity = contract.get("namespaceIdentity") if isinstance(contract, dict) else None
        if (not isinstance(identity, list) or len(identity) != 2
                or not all(isinstance(part, int) for part in identity)
                or tuple(identity) != expected_identity
                or contract.get("namespaceId") != storage.namespace_id
                or not storage.is_managed_result_uri(uri)):
            raise RuntimeError("managed local-source namespace identity is invalid")
        fd = contract.get("lockFd")
        if getattr(storage, "lock_supported", False) and fd is None:
            raise RuntimeError("managed local-source lock was not inherited")
        if fd is None:
            continue
        if fcntl is None:
            raise RuntimeError("inherited local-source locks are unavailable on this platform")
        fd = int(fd)
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode):
            raise RuntimeError("inherited local-source lock is not a regular file")
        _artifact_name, lock_name = storage._result_names(uri)
        expected = os.stat(
            lock_name, dir_fd=storage._result_lock_dir_fd, follow_symlinks=False)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("inherited local-source lock does not match its exact URI")
        if storage._read_lock_token(fd) != contract.get("lockToken"):
            raise RuntimeError("inherited local-source lock token is invalid")
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)


def _run_profile_job(job: dict, deps, graph, status_file: str, external_cancel) -> int:
    """Execute one full profile directly on the one-shot child's main thread.

    The durable parent owns identity, deadline, cancellation, and source leases. This child only emits
    workload progress/result documents; the parent treats them as untrusted and publishes no terminal
    status until this process has exited and been reaped.
    """
    from hub.executors.profile import profile_node
    from hub.models import PerNodeStatus, RunStatus

    node_id = job.get("target")
    if not isinstance(node_id, str) or not node_id:
        raise RuntimeError("profile job target is missing")

    started = time.monotonic()

    def write(state: str, *, result=None, error: str | None = None) -> None:
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        rows = result.row_count if result is not None and state == "done" else 0
        status = RunStatus(
            run_id="child",
            status=state,
            job_type="profile",
            target_node_id=node_id,
            rows_processed=rows,
            total_rows=None,
            ms=elapsed_ms,
            placement="local",
            per_node=[PerNodeStatus(
                node_id=node_id,
                status=state,
                rows=rows if state == "done" else None,
                ms=elapsed_ms,
                label="Full profile",
                error=error if state == "failed" else None,
            )],
            progress=1.0 if state == "done" else None,
            error=error,
            profile=result if state == "done" else None,
        )
        _atomic_write(status_file, status.model_dump())

    if external_cancel and external_cancel():
        write("cancelled")
        return 1
    write("running")
    result = profile_node(
        graph,
        node_id,
        deps.resolve_adapter,
        deps.registry,
        deps.node_builders,
        deps.node_specs,
        full=True,
        storage=deps.storage,
        process_isolated=True,
        source_leases_preclaimed=True,
    )
    if external_cancel and external_cancel():
        write("cancelled")
        return 1
    if result.error or result.not_previewable:
        write("failed", error=result.reason or "full profile failed")
        return 1
    if result.sampled:
        write("failed", error="profile worker returned a sampled result for a full profile")
        return 1
    write("done", result=result)
    return 0


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
        deps = set_workspace(
            job["workspace"], job["dataDir"], maintain_storage=False)
        _validate_local_source_locks(job, deps.storage)
        # the PARENT (SubprocessRunner) owns ALL run_states writes and run-history recording: it reads our
        # status file and persists under the authoritative (hub) run_id. If we also wrote, we'd emit orphan
        # rows keyed by the child's own id (and race a hard-kill on cancel). Silence both hooks here.
        deps.runner.on_complete = None
        deps.runner.on_status = None
        deps.runner.result_get = None
        deps.runner.result_acquire = None
        deps.runner.result_put = None
        forced_results = job.get("forcedResults")
        if forced_results is None:
            deps.runner.forced_results = None
        elif not isinstance(forced_results, list):
            raise RuntimeError("isolated forced result contract is missing or malformed")
        else:
            deps.runner.forced_results = forced_results
        from hub.plugins.adapters import is_object_uri
        forced_object = bool(
            forced_results and len(forced_results) == 1
            and isinstance(forced_results[0], dict)
            and isinstance(forced_results[0].get("uri"), str)
            and is_object_uri(forced_results[0].get("uri")))
        identity = job.get("resultNamespaceIdentity")
        if forced_results is None:
            identity = None
        elif forced_object:
            if identity is not None or job.get("resultNamespaceId") is not None:
                raise RuntimeError("object result contract cannot carry a local namespace")
        elif (not forced_results or not isinstance(identity, list) or len(identity) != 2
              or not all(isinstance(part, int) for part in identity)):
            raise RuntimeError("local result namespace identity is malformed")
        elif job.get("resultNamespaceId") != deps.storage.namespace_id:
            raise RuntimeError("local result filesystem namespace is invalid")
        else:
            deps.runner.forced_result_namespace_identity = tuple(identity)
        seen_forced: set[tuple[str, str]] = set()
        for result in forced_results or []:
            if not isinstance(result, dict):
                raise RuntimeError("isolated forced result contract is malformed")
            node_id, port_id, uri = result.get("nodeId"), result.get("portId"), result.get("uri")
            if (not isinstance(node_id, str) or not isinstance(port_id, str)
                    or not isinstance(uri, str)
                    or (not forced_object and not deps.storage.is_managed_result_uri(uri))
                    or (node_id, port_id) in seen_forced):
                raise RuntimeError("isolated forced result contract is malformed")
            seen_forced.add((node_id, port_id))
            fd = result.get("lockFd")
            if forced_object and (fd is not None or result.get("lockToken") is not None):
                raise RuntimeError("object result contract cannot carry a local writer lock")
            if (not forced_object and getattr(deps.storage, "lock_supported", False)
                    and fd is None):
                raise RuntimeError("local result writer lock was not inherited")
            if fd is None:
                continue
            if fcntl is None:
                raise RuntimeError("inherited local-result locks are unavailable on this platform")
            fd = int(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("inherited local-result lock is not a regular file")
            _artifact_name, lock_name = deps.storage._result_names(uri)
            expected_lock = os.stat(
                lock_name, dir_fd=deps.storage._result_lock_dir_fd, follow_symlinks=False)
            actual_lock = os.fstat(fd)
            if ((actual_lock.st_dev, actual_lock.st_ino) != (expected_lock.st_dev, expected_lock.st_ino)
                    or deps.storage._read_lock_token(fd) != result.get("lockToken")):
                raise RuntimeError("inherited local-result lock does not match its exact URI")
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        # Every write target was resolved by the real parent control plane. For managed object sinks the
        # child also receives one exact physical attempt and is write-only: allocation, inventory commit,
        # and catalog publication remain in the parent's durable metadata database.
        deps.runner.forced_sink_targets = (
            dict(job.get("sinkTargets") or {}) if "sinkTargets" in job else None)
        deps.runner.forced_sink_attempts = dict(job.get("sinkAttempts") or {})
        graph = Graph(**job["graph"])
        # The disposable child DB cannot prove lifecycle ownership. Accept managed source attempts only
        # when the durable parent attested the exact physical URI and is holding its renewable read lease.
        deps.runner.parent_attested_source_uris = _parent_attested_source_uris(job, graph)
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
        job_kind = job.get("jobKind", "run")
        if job_kind == "profile":
            return _run_profile_job(job, deps, graph, status_file, external_cancel)
        if job_kind != "run":
            raise RuntimeError(f"unsupported subprocess job kind: {job_kind}")
        # region-materialize mode (RunController C3): run this sub-graph in THIS worker process and
        # write the target node's relation to a given uri — how a placed region executes on its worker.
        mat_uri = job.get("materializeUri")
        if mat_uri:
            from hub.models import RunStatus
            from hub.run_outputs import (
                commit_output, require_single_run_output, settle_uncommitted_outputs)
            target = job.get("target")
            expected_output = require_single_run_output(
                graph, target, deps.node_specs)
            status = RunStatus(
                run_id="child", status="running", target_node_id=target,
                placement="local", per_node=[], outputs=[expected_output])
            if external_cancel and external_cancel():
                status.status = "cancelled"
                settle_uncommitted_outputs(status, "cancelled")
                _atomic_write(status_file, status.model_dump())
                return 1
            _atomic_write(status_file, status.model_dump())
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
                                             pushdown=True, output_node=target)
                        rel = eng.relation(target)
                        rows = _materialize_region(
                            deps, rel, mat_uri, _CancelToken(external_cancel), external_cancel,
                            job.get("runId") or "child")
                    finally:
                        monitor_done.set()
            except Exception as exc:  # noqa: BLE001 — emit a complete per-port terminal receipt
                cancelled = bool(external_cancel and external_cancel())
                status.status = "cancelled" if cancelled else "failed"
                status.error = None if cancelled else f"{type(exc).__name__}: {exc}"
                settle_uncommitted_outputs(status, status.status, status.error)
                _atomic_write(status_file, status.model_dump())
                return 1
            status.rows_processed = rows
            status.total_rows = rows
            commit_output(status, uri=mat_uri, rows=rows)
            status.status = "done"
            _atomic_write(status_file, status.model_dump())
            return 0
        plan = compiler.compile_plan(graph, job.get("target"), deps.registry, deps.node_specs, deps.node_ir)
        if external_cancel and external_cancel():
            _atomic_write(status_file, {"run_id": "child", "status": "cancelled", "per_node": [],
                                        "rows_processed": 0, "ms": 0, "placement": "local"})
            return 1
        # Read the parent's request directly at every LocalRunner/adapter fence; the polling loop below
        # additionally calls cancel() to interrupt an in-flight DuckDB cursor.
        st = deps.runner.run(
            plan, graph, job.get("target"), "local",
            run_id=job.get("runId"), cancel_check=external_cancel)
        rid = st.run_id
        cancel_requested = False
        while True:
            if not cancel_requested and cancel_file and os.path.exists(cancel_file):
                deps.runner.cancel(rid)  # cooperative cursor interrupt + adapter pre-publish fence
                cancel_requested = True
            s = deps.runner.status(rid)
            _atomic_write(status_file, s.model_dump())
            if s.status in ("done", "failed", "cancelled"):
                if not deps.runner.wait_for_worker(rid, timeout=30.0):
                    raise RuntimeError("local execution worker did not stop after terminal status")
                return 0 if s.status == "done" else 1
            time.sleep(0.12)
    except Exception as e:  # noqa: BLE001
        _atomic_write(status_file, {"run_id": "child", "status": "failed", "per_node": [],
                                    "rows_processed": 0, "ms": 0, "placement": "local",
                                    "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
