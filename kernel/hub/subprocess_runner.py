"""An execution backend that runs each job in a SEPARATE OS PROCESS.

Isolation, for real: the kernel stays responsive while a job runs, a runaway / segfaulting /
OOM-killed job can't take the kernel down (the parent just sees the child exit), and cancel is a
hard kill. Same plan, same engine — the child (kernel/subrun.py) rebuilds Deps for the workspace and
runs the in-process LocalRunner, writing status JSON to a file the parent polls. A dedicated child
entrypoint (not multiprocessing 'spawn') keeps this robust however the kernel was launched. (pod /
Ray backends would be plugins over this same ExecutionBackend protocol.)
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from hub.models import CompilePlan, Graph, PerNodeStatus, Placement, RunEstimate, RunStatus
from hub.plugins.runner import _CONFIRM_ROWS, _MAX_RUNS, _persist_local_result_done

_CANCEL_GRACE_S = 2.0  # cooperative child cancel first; then SIGTERM/SIGKILL for runaway native/Python code


class _SpawnSetupError(RuntimeError):
    """Post-Popen setup failed; `reaped` controls whether the caller may release writer ownership."""

    def __init__(self, message: str, *, reaped: bool):
        super().__init__(message)
        self.reaped = reaped


def _subrun_child_env() -> dict[str, str]:
    """A one-shot worker does not own control-plane persistence and receives no metadata identity."""
    from hub.workload_env import build_workload_env
    return build_workload_env(include_metadata_db=False)


def _safe_abandon_attempt(uri: str, *, context: str) -> None:
    """Best-effort lifecycle cleanup once the parent has reaped the only writer."""
    from hub import metadb
    from hub.handoff import discard_attempt

    try:
        abandoned = metadb.abandon_committed_object_attempt(uri)
    except Exception:  # noqa: BLE001 — cleanup must not replace the terminal result
        logging.getLogger("hub").exception("%s metadata cleanup failed", context)
        return  # metadata uncertainty: retaining data is safer than deleting an owned object
    if not abandoned:
        discard_attempt(uri)


class SubprocessRunner:
    name = "local-subprocess"
    manages_source_leases = True

    @staticmethod
    def supports_selected_destination_credentials() -> bool:
        return False  # the ephemeral child has ambient data identity only; it cannot resolve hub Creds

    def __init__(self, workspace: str, data_dir: str, catalog=None, deadline_s: float | None = None,
                 storage=None, resolve_adapter=None, node_builders=None):
        self.workspace = workspace
        self.data_dir = data_dir
        self.catalog = catalog  # register outputs written by children into the parent's live catalog
        if storage is None:
            from hub.storage import make_storage
            storage = make_storage(workspace)
        self.storage = storage
        self.resolve_adapter = resolve_adapter
        self.node_builders = node_builders if node_builders is not None else {}
        self.result_put = None  # optional parent DB cache publication after RunState owns the result
        self.on_complete = None  # optional (graph, target, status) hook — Deps wires it to run-history
        self.on_status = None    # optional (graph, status) hook — Deps wires it to DB-backed live status
        self.runs: dict[str, RunStatus] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancel_files: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._object_results: dict[str, dict] = {}
        self._local_results: dict[str, dict] = {}
        self._object_sinks: dict[str, dict[str, dict]] = {}
        self._sink_contracts: dict[str, dict[str, dict]] = {}
        self._source_leases: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.publication_retry_wait = time.sleep
        # wall-clock deadline: a child that runs longer than this is hard-killed and the run fails, so a
        # runaway cell (`while True`, a livelocked native op) can't pin a worker forever. <=0 disables.
        try:
            self.deadline_s = deadline_s if deadline_s is not None else float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
        except ValueError:
            self.deadline_s = 3600.0
        atexit.register(self._terminate_all)  # don't orphan running children when the kernel exits

    def _spawn_process(self, run_id: str, command: list[str], **kwargs) -> subprocess.Popen:
        """Backend hook for process-containment variants; ordinary runs keep direct-child semantics."""
        _ = run_id
        return subprocess.Popen(command, **kwargs)

    def _signal_process(self, run_id: str, proc: subprocess.Popen, *, force: bool) -> None:
        """Signal the owned writer. Specialized runners may target a process group/cgroup instead."""
        _ = run_id
        if force:
            proc.kill()
        else:
            proc.terminate()

    def _finalize_process_scope(self, run_id: str, proc: subprocess.Popen) -> None:
        """Prove specialized descendant containment is empty after the direct child is reaped."""
        _ = run_id, proc

    def _terminate_all(self) -> None:
        """Fence, reap, then discard parent-owned writers during an orderly interpreter shutdown.

        SIGKILL cannot run this hook; its writing attempts deliberately remain unowned for operator
        reconciliation because lease expiry alone is not proof that the child writer stopped.
        """
        with self._lock:
            procs = list(self._procs.items())
            self._cancelled.update(run_id for run_id, _proc in procs)
        for run_id, proc in procs:
            try:
                if proc.poll() is None:
                    self._signal_process(run_id, proc, force=False)
            except Exception:  # noqa: BLE001
                pass
        for run_id, proc in procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal_process(run_id, proc, force=True)
                proc.wait()
            except Exception:  # noqa: BLE001
                continue
            try:
                self._finalize_process_scope(run_id, proc)
            except Exception:  # noqa: BLE001 - process exit will release the remaining OS scope
                logging.getLogger("hub").exception(
                    "subprocess shutdown could not finalize its process scope")
                continue
            owned = self._object_results.get(run_id)
            if owned is not None:
                _safe_abandon_attempt(
                    owned["uri"], context="parent object-result shutdown")
            local_owned = self._local_results.get(run_id)
            if local_owned is not None:
                try:
                    self.storage.abort_result(local_owned["uri"], run_id)
                except Exception:  # retain exact metadata for the bounded startup reaper
                    logging.getLogger("hub").exception(
                        "parent local-result shutdown cleanup failed")
            self._discard_object_sinks(self._object_sinks.get(run_id, {}))
            self._sink_contracts.pop(run_id, None)
            self._release_source_leases(run_id)

    def reachable_tiers(self) -> tuple:
        # every subprocess backend is a SAME-HOST child sharing the workspace filesystem, so it reaches the
        # local tier (a same-host handoff needs no object store) as well as a configured object store.
        # Declared on the base so any same-host subprocess subclass (e.g. PoolRunner) is covered — a named
        # backend otherwise defaults to object-only and the controller would refuse a valid local handoff.
        return ("local", "object")

    def can_run(self, plan: CompilePlan) -> bool:
        return plan.acyclic

    def _claim_source_leases(self, graph: Graph, target: str | None, run_id: str) -> dict:
        """Attest and pin every managed source in the durable parent before child dispatch."""
        try:
            deadline = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
        except ValueError:
            deadline = 3600.0
        ttl = max(300.0, deadline + 300.0)
        stack = contextlib.ExitStack()
        guards = []
        attempts: dict[str, dict] = {}
        local_sources: dict[str, dict] = {}
        seen: set[str] = set()
        try:
            from hub import graph as graph_mod
            from hub.handoff import (
                has_attempt_path_component, is_attempt_uri, managed_read_lease)
            from hub.storage import preflight_managed_execution_sources
            source_uris = preflight_managed_execution_sources(
                self.storage, graph_mod.execution_source_uris(graph, target))
            for uri in source_uris:
                normalized = str(uri).rstrip("/")
                if normalized in seen:
                    continue
                seen.add(normalized)
                if (has_attempt_path_component(normalized)
                        and not is_attempt_uri(normalized)):
                    raise FileNotFoundError(
                        "managed source must reference the exact attempt root")
                acquire_local = getattr(self.storage, "acquire_result_read", None)
                managed_local = getattr(self.storage, "requires_result_read", None)
                if (callable(acquire_local) and callable(managed_local)
                        and managed_local(normalized)):
                    guard = stack.enter_context(acquire_local(
                        normalized, f"subprocess-source:{run_id}"))
                    lock_fd = guard.fileno()
                    local_sources[guard.uri] = {
                        "namespaceId": self.storage.namespace_id,
                        "namespaceIdentity": list(self.storage.result_namespace_identity()),
                        "lockToken": (
                            self.storage._read_lock_token(lock_fd)
                            if lock_fd is not None else None),
                        "lockFd": lock_fd,
                    }
                else:
                    guard = stack.enter_context(managed_read_lease(
                        normalized, owner=f"subprocess:{run_id}", ttl_seconds=ttl))
                guards.append(guard)
                if getattr(guard, "lease_id", None):
                    attestation = guard.attestation
                    if not isinstance(attestation, dict) or attestation.get("uri") != normalized:
                        raise RuntimeError("managed source lease returned an invalid attestation")
                    attempts[normalized] = {
                        "attemptId": attestation["attempt_id"],
                        "generation": attestation["generation"],
                        "storageNamespace": attestation["storage_namespace"],
                        "logicalUri": attestation["logical_uri"],
                        "kind": attestation["kind"],
                    }
                elif has_attempt_path_component(normalized):
                    raise FileNotFoundError(
                        "managed source attempt has no durable parent attestation")
            return {
                "stack": stack,
                "guards": guards,
                "attempts": attempts,
                "local_sources": local_sources,
            }
        except Exception:
            try:
                stack.close()
            except Exception:  # noqa: BLE001 — cleanup must not replace the acquisition failure
                logging.getLogger("hub").exception(
                    "partial managed-source lease cleanup failed")
            raise

    def _check_source_leases(self, run_id: str) -> None:
        with self._lock:
            guards = list((self._source_leases.get(run_id) or {}).get("guards", ()))
        for guard in guards:
            guard.check()

    def _release_source_leases(self, run_id: str) -> None:
        with self._lock:
            owned = self._source_leases.pop(run_id, None)
        if owned is None:
            return
        try:
            owned["stack"].close()
        except Exception:  # noqa: BLE001 — an expired lease is safe and cleanup must remain terminal
            logging.getLogger("hub").exception(
                "parent managed-source lease cleanup failed")

    def estimate(self, plan: CompilePlan, rows: int | None, byts: int | None = None) -> RunEstimate:
        from hub.plugins.runner import _CONFIRM_BYTES, _fmt_bytes
        if rows is None and byts is None:  # uncountable → unreadable → fails fast; no fabricated ETA, no gate
            return RunEstimate(rows=None, bytes=None, placement="local", needs_confirm=False,
                               breakdown=f"size unknown · {len(plan.steps)} steps · isolated process")
        needs = (byts is not None and byts >= _CONFIRM_BYTES) or (rows is not None and rows >= _CONFIRM_ROWS)
        size = _fmt_bytes(byts) if byts is not None else "size unknown"
        rowstr = f"{rows:,} rows" if rows is not None else "unknown rows"
        return RunEstimate(rows=rows, bytes=byts, placement="local", needs_confirm=needs,
                           breakdown=f"{size} · {rowstr} · {len(plan.steps)} steps · isolated process")

    def _claim_sink_contracts(self, plan: CompilePlan, graph: Graph, run_id: str
                              ) -> tuple[dict[str, str], dict[str, dict], dict[str, dict]]:
        """Resolve every sink on the parent and allocate the one supported managed sink before dispatch."""
        from hub.plugins.adapters import is_object_uri
        from hub.plugins.runner import _is_core_managed_sink
        from hub.sinks import SinkSpec, expected_sink_uri, preflight_sink

        nodes = {node.id: node for node in graph.nodes}
        targets: dict[str, str] = {}
        contracts: dict[str, dict] = {}
        managed = []
        from hub import graph as graph_mod

        for step in plan.steps:
            if step.kind != "write":
                continue
            node = nodes.get(step.node_id)
            if node is None:
                raise RuntimeError(f"write step '{step.node_id}' has no graph node")
            cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
            title = node.data.get("title") if isinstance(node.data, dict) else None
            spec = SinkSpec.from_config(cfg, title)
            if self.resolve_adapter is None:
                target_uri = spec.target_uri(self.workspace, self.storage)
                if is_object_uri(target_uri) or spec.partition_by:
                    raise RuntimeError(
                        "object-backed subprocess sinks require a parent adapter resolver")
                # Even a minimal locally-constructed runner must cross the shared private-namespace /
                # hardlink guard before the child receives a sink contract.
                target_uri = preflight_sink(
                    spec, self.workspace, self.storage, self.resolve_adapter,
                    target_uri=target_uri)
                adapter = None
            else:
                target_uri = preflight_sink(
                    spec, self.workspace, self.storage, self.resolve_adapter)
                adapter = self.resolve_adapter(target_uri)
            targets[step.node_id] = target_uri
            parents = graph_mod.all_upstream_publication_uris(graph, step.node_id)
            contracts[step.node_id] = {
                "logical_uri": target_uri,
                "published_uri": expected_sink_uri(spec, target_uri, adapter),
                "name": spec.name, "parents": parents,
            }
            if adapter is not None and _is_core_managed_sink(spec, target_uri, adapter):
                managed.append((step.node_id, target_uri, spec, parents))

        from hub.plugins.catalog import core_managed_publisher, unmanaged_publication_supported
        if len(targets) > 1:
            raise RuntimeError(
                "isolated subprocess runs support one sink until atomic multi-sink publication "
                "is enabled")
        managed_ids = {step_id for step_id, _uri, _spec, _parents in managed}
        if (any(step_id not in managed_ids for step_id in targets)
                and not unmanaged_publication_supported(self.catalog)):
            raise RuntimeError(
                "subprocess sinks require parent catalog registration with read-back support")
        if managed and core_managed_publisher(self.catalog) is None:
            raise RuntimeError(
                "managed object writes require the core transactional catalog publisher")

        attempts: dict[str, dict] = {}
        try:
            from hub.handoff import allocate_attempt, physical_attempt_uri
            for step_id, logical_uri, spec, parents in managed:
                handle = allocate_attempt(
                    logical_uri=logical_uri, kind="sink", run_id=run_id,
                    allocation_key=f"subprocess-sink:{run_id}:{step_id}:{logical_uri}",
                    catalog_key_base=f"tbl_{spec.name}",
                    uri_factory=lambda namespace, generation, attempt_id,
                    logical_uri=logical_uri: physical_attempt_uri(
                        logical_uri, namespace, generation, attempt_id),
                )
                attempts[step_id] = {
                    "uri": handle["uri"], "logical_uri": logical_uri, "name": spec.name,
                    "parents": parents,
                }
            return targets, attempts, contracts
        except Exception:
            self._discard_object_sinks(attempts)
            raise

    @staticmethod
    def _discard_object_sinks(sinks: dict[str, dict]) -> None:
        if not sinks:
            return
        for item in sinks.values():
            _safe_abandon_attempt(
                item["uri"], context="parent managed-sink")

    def _publish_object_sinks(self, sinks: dict[str, dict], status: RunStatus) -> None:
        if not sinks:
            return
        if len(sinks) != 1:
            raise RuntimeError("managed subprocess sink publication requires one exact sink")
        step_id, item = next(iter(sinks.items()))
        if status.output_uri != item["uri"] or status.output_table != item["name"]:
            raise RuntimeError(
                f"child returned an unexpected output binding for managed sink '{step_id}'")
        from hub.handoff import prepare_attempt_commit
        from hub.plugins.catalog import core_managed_publisher

        prepare_attempt_commit(item["uri"])
        publish = core_managed_publisher(self.catalog)
        if publish is None:
            raise RuntimeError("managed object output has no core publisher")
        receipt = publish(
            name=item["name"], uri=item["uri"], version=None,
            parents=item["parents"], pipeline="canvas")
        if not isinstance(receipt, dict) or receipt.get("uri") != item["uri"]:
            raise RuntimeError(
                f"core publisher returned an invalid receipt for sink '{step_id}'")

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement, run_id: str | None = None,
            request_id: str | None = None, attempt_id: str | None = None) -> RunStatus:
        from hub.backends import require_destination_credential_support
        require_destination_credential_support(self, plan, graph, self.workspace)
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"  # a kernel passes the hub-minted id (authoritative)
        per = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        _ = attempt_id  # OPS-01 port parity; managed publication stamps attempts itself
        status = RunStatus(run_id=run_id, status="queued", placement="local", per_node=per,
                           target_node_id=target_node_id, request_id=request_id)
        job_extra: dict = {"runId": run_id}
        try:
            source_leases = self._claim_source_leases(graph, target_node_id, run_id)
            with self._lock:
                self._source_leases[run_id] = source_leases
            job_extra["managedSourceAttempts"] = source_leases["attempts"]
            job_extra["managedLocalSources"] = source_leases["local_sources"]
            sink_targets, object_sinks, sink_contracts = self._claim_sink_contracts(
                plan, graph, run_id)
            if object_sinks:
                self._object_sinks[run_id] = object_sinks
            if sink_contracts:
                self._sink_contracts[run_id] = sink_contracts
            job_extra["sinkTargets"] = sink_targets
            job_extra["sinkAttempts"] = {
                step_id: item["uri"] for step_id, item in object_sinks.items()}

            target = next((node for node in graph.nodes if node.id == target_node_id), None)
            if target is not None and target.type not in ("write", "assert"):
                begin_local = getattr(self.storage, "begin_result", None)
                if callable(begin_local):
                    if self.on_status is None:
                        raise RuntimeError(
                            "local subprocess results require authoritative parent run persistence")
                    from hub.plan_key import plan_cacheable, plan_hash
                    if self.resolve_adapter is not None:
                        phash = plan_hash(graph, target_node_id, self.resolve_adapter)
                        cacheable = plan_cacheable(graph, target_node_id, self.node_builders)
                    else:
                        phash, cacheable = run_id, False
                    result_uri = begin_local(phash, run_id)
                    self._local_results[run_id] = {
                        "uri": result_uri, "cache_key": phash if cacheable else None,
                        "run_state_owner": True,
                    }
                    job_extra["forcedResultUri"] = result_uri
                    identity = self.storage.result_namespace_identity()
                    job_extra["resultNamespaceId"] = self.storage.namespace_id
                    job_extra["resultNamespaceIdentity"] = list(identity)
                    lock_fd = self.storage.result_lock_fd(result_uri, run_id)
                    if lock_fd is not None:
                        job_extra["resultLockFd"] = lock_fd
                        job_extra["resultLockToken"] = self.storage._read_lock_token(lock_fd)
                else:
                    logical_uri = self.storage.output_uri(
                        f"__result_{run_id}", ".parquet")
                    from hub.plugins.adapters import is_object_uri
                    if not is_object_uri(logical_uri):
                        return self._spawn(status, job_extra, graph, target_node_id)
                    if self.resolve_adapter is None:
                        raise RuntimeError(
                            "object-backed subprocess results require a parent adapter resolver")
                    if self.on_status is None:
                        raise RuntimeError(
                            "object-backed subprocess results require authoritative parent run persistence")
                    from hub.plan_key import plan_cacheable, plan_hash
                    phash = plan_hash(graph, target_node_id, self.resolve_adapter)
                    cacheable = plan_cacheable(graph, target_node_id, self.node_builders)
                    logical_uri = self.storage.output_uri(f"__result_{phash}", ".parquet")
                    from hub.handoff import allocate_attempt, physical_attempt_uri
                    handle = allocate_attempt(
                        logical_uri=logical_uri, kind="region", run_id=run_id,
                        allocation_key=f"subprocess-full-result:{run_id}:{phash}",
                        uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                            logical_uri, namespace, generation, attempt_id),
                    )
                    self._object_results[run_id] = {
                        "uri": handle["uri"], "cache_key": phash if cacheable else None,
                        "run_state_owner": True,
                    }
                    job_extra["forcedResultUri"] = handle["uri"]
            return self._spawn(status, job_extra, graph, target_node_id)
        except Exception as exc:
            if isinstance(exc, _SpawnSetupError) and not exc.reaped:
                raise
            owned = self._object_results.pop(run_id, None)
            if owned is not None:
                _safe_abandon_attempt(
                    owned["uri"], context="parent object-result pre-dispatch")
            local_owned = self._local_results.pop(run_id, None)
            if local_owned is not None:
                try:
                    self.storage.abort_result(local_owned["uri"], run_id)
                except Exception:  # retain the fenced row for startup reconciliation
                    logging.getLogger("hub").exception(
                        "parent local-result pre-dispatch cleanup failed")
            self._discard_object_sinks(self._object_sinks.pop(run_id, {}))
            self._sink_contracts.pop(run_id, None)
            self._release_source_leases(run_id)
            raise

    def run_unit(self, graph: Graph, output_node: str, output_uri: str, requires=None) -> RunStatus:
        """Run a placement region's sub-graph in a worker PROCESS and materialize output_node's relation
        to output_uri (no catalog registration). This is how a placed region executes on its worker —
        the seam a pod/Ray backend overrides to allocate a pod / submit a job. `requires` (the region's
        resource need) is accepted for signature parity but ignored: a subprocess is one local process,
        so there's no worker to place onto."""
        from hub import compiler
        from hub.backends import require_destination_credential_support
        plan = compiler.compile_plan(graph, output_node)
        require_destination_credential_support(self, plan, graph, self.workspace)
        run_id = f"unit_{uuid.uuid4().hex[:10]}"
        status = RunStatus(run_id=run_id, status="queued", placement="local", per_node=[])
        materialize_uri = output_uri
        from hub.plugins.adapters import is_object_uri
        try:
            source_leases = self._claim_source_leases(graph, output_node, run_id)
            with self._lock:
                self._source_leases[run_id] = source_leases
            if is_object_uri(output_uri):
                if self.resolve_adapter is None:
                    raise RuntimeError(
                        "object-backed subprocess regions require a parent adapter resolver")
                self.resolve_adapter(output_uri)
                from hub.handoff import allocate_attempt, is_attempt_uri, physical_attempt_uri
                if is_attempt_uri(output_uri):
                    raise RuntimeError(
                        "subprocess region output must be a stable logical object URI")
                handle = allocate_attempt(
                    logical_uri=output_uri, kind="region", run_id=run_id,
                    allocation_key=f"subprocess-region:{run_id}:{output_node}:{output_uri}",
                    uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                        output_uri, namespace, generation, attempt_id),
                )
                materialize_uri = handle["uri"]
                self._object_results[run_id] = {
                    "uri": materialize_uri, "cache_key": None, "run_state_owner": False,
                }
            return self._spawn(
                status, {"runId": run_id, "materializeUri": materialize_uri,
                         "managedSourceAttempts": source_leases["attempts"],
                         "managedLocalSources": source_leases["local_sources"]},
                graph, output_node)
        except Exception as exc:
            if isinstance(exc, _SpawnSetupError) and not exc.reaped:
                raise
            owned = self._object_results.pop(run_id, None)
            if owned is not None:
                _safe_abandon_attempt(
                    owned["uri"], context="parent region-result pre-dispatch")
            self._release_source_leases(run_id)
            raise

    def _spawn(self, status: RunStatus, job_extra: dict, graph: Graph, target: str | None) -> RunStatus:
        from hub.workload_env import prepare_workload_graph

        run_id = status.run_id
        job_dir = tempfile.mkdtemp(prefix="dp-run-")
        status_file = os.path.join(job_dir, "status.json")
        cancel_file = os.path.join(job_dir, "cancel.requested")
        job_file = os.path.join(job_dir, "job.json")
        try:
            prepared_graph = prepare_workload_graph(graph)
            with open(job_file, "w") as f:
                json.dump({"workspace": self.workspace, "dataDir": self.data_dir,
                           "graph": prepared_graph,
                           "target": target, "statusFile": status_file,
                           "cancelFile": cancel_file, **job_extra}, f)
            # A one-shot worker gets only runtime/data capabilities, never the hub metadata identity or
            # ambient signing/bootstrap/provider secrets. It creates a disposable local metadata DB itself.
            popen_kwargs = {"env": _subrun_child_env()}
            inherited_fds = [
                int(contract["lockFd"])
                for contract in (job_extra.get("managedLocalSources") or {}).values()
                if contract.get("lockFd") is not None
            ]
            result_lock_fd = job_extra.get("resultLockFd")
            if result_lock_fd is not None:
                inherited_fds.append(int(result_lock_fd))
            if inherited_fds:
                popen_kwargs["pass_fds"] = tuple(sorted(set(inherited_fds)))
            proc = self._spawn_process(
                run_id, [sys.executable, "-m", "hub.subrun", job_file], **popen_kwargs)
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        try:
            with self._lock:
                self.runs[run_id] = status
                self._procs[run_id] = proc
                self._cancel_files[run_id] = cancel_file
                self._evict()
            self._emit(graph, status)  # persist 'queued' to the DB (pollable on any instance / after restart)
            threading.Thread(
                target=self._watch,
                args=(run_id, proc, status_file, job_dir, graph, target),
                daemon=True,
            ).start()
            return status
        except Exception as exc:
            # Once Popen succeeds the child may be writing. Reap it before the caller terminalizes the
            # parent-owned attempt; setup failure alone is not writer terminal proof.
            reaped = False
            try:
                if proc.poll() is None:
                    self._signal_process(run_id, proc, force=False)
                proc.wait(timeout=2)
                self._finalize_process_scope(run_id, proc)
                reaped = True
            except subprocess.TimeoutExpired:
                try:
                    self._signal_process(run_id, proc, force=True)
                    proc.wait()
                    self._finalize_process_scope(run_id, proc)
                    reaped = True
                except Exception:  # noqa: BLE001
                    logging.getLogger("hub").exception(
                        "post-Popen setup failure could not force-reap child")
            except Exception:  # noqa: BLE001
                logging.getLogger("hub").exception(
                    "post-Popen setup failure could not reap child")
            if reaped:
                with self._lock:
                    self.runs.pop(run_id, None)
                    self._procs.pop(run_id, None)
                    self._cancel_files.pop(run_id, None)
                shutil.rmtree(job_dir, ignore_errors=True)
            else:
                logging.getLogger("hub").error(
                    "post-Popen setup failure retained writer ownership and job directory")
                status.status = "running"
                status.stalled = True
                status.error = "execution supervisor is retrying writer reconciliation"
                self._emit(graph, status)
                self._schedule_watch_retry(
                    run_id, proc, status_file, job_dir, graph, target)
            raise _SpawnSetupError(str(exc), reaped=reaped) from exc

    def _emit(self, graph: Graph, status: RunStatus, *, strict: bool = False) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise

    def _complete(self, graph: Graph, target: str | None, status: RunStatus) -> None:
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001 — RunState already owns managed results
                pass

    def _evict(self) -> None:
        """Bound self.runs (called under self._lock) — subprocess runs accumulated forever otherwise.
        Evict only TERMINAL runs (oldest first); never drop a run whose child is still executing."""
        _terminal = {"done", "failed", "cancelled"}
        while len(self.runs) > _MAX_RUNS:
            victim = next((rid for rid, st in self.runs.items() if st.status in _terminal), None)
            if victim is None:
                break  # all retained runs are still live — exceed the cap rather than drop one
            self.runs.pop(victim, None)
            self._cancelled.discard(victim)
            self._procs.pop(victim, None)
            self._cancel_files.pop(victim, None)
            self._sink_contracts.pop(victim, None)

    def _sanitize_child_status(self, run_id: str, observed: RunStatus) -> RunStatus:
        """Apply backend-specific identity fences to an untrusted child status document.

        Ordinary subprocess runs already get their parent run id overridden in :meth:`_read` and need
        no additional changes. Specialized one-shot workloads can override this hook without copying
        the process supervision and reap-before-terminal state machine.
        """
        return observed

    def _heartbeat_interval_s(self) -> float | None:
        """Optional unchanged-status publication interval; ordinary subprocess runs stay event-driven."""
        return None

    def _finalize_reaped_status(self, run_id: str, status: RunStatus, *,
                                deadline_hit: bool, returncode: int | None) -> RunStatus:
        """Apply backend-specific terminal rules after the child is observably reaped."""
        return status

    def _read(self, run_id: str, status_file: str) -> RunStatus | None:
        """Merge progress, but hold a child terminal status for parent-side finalization."""
        try:
            with open(status_file) as f:
                payload = json.load(f)
        except OSError:
            return None
        except (TypeError, ValueError):
            logging.getLogger("hub").exception(
                "ignored malformed subprocess status document")
            return None
        try:
            observed = RunStatus(**{**payload, "run_id": run_id})  # the child had its own run id
        except Exception:  # noqa: BLE001 — the child status document is untrusted input
            logging.getLogger("hub").exception(
                "ignored malformed subprocess status document")
            return None
        observed = self._sanitize_child_status(run_id, observed)
        if observed.status in ("done", "failed", "cancelled"):
            return observed
        # Child progress is untrusted and parent-owned result/sink paths are provisional until reap,
        # exact receipt validation, and parent commit. Never mirror an intermediate output binding.
        observed.output_uri = observed.output_table = None
        if run_id not in self._cancelled:
            self.runs[run_id] = observed
        return None

    def _schedule_watch_retry(self, run_id: str, proc: subprocess.Popen, status_file: str,
                              job_dir: str, graph: Graph, target: str | None) -> None:
        """Retry reconciliation without declaring a terminal state while the writer may still run."""
        def retry() -> None:
            time.sleep(0.5)
            with self._lock:
                if self._procs.get(run_id) is not proc:
                    return
            self._watch(run_id, proc, status_file, job_dir, graph, target)

        try:
            threading.Thread(target=retry, daemon=True).start()
        except Exception:  # noqa: BLE001 — retain ownership for cancel/atexit/operator reconciliation
            logging.getLogger("hub").exception(
                "could not schedule subprocess writer reconciliation retry")

    def _watch(self, run_id: str, proc: subprocess.Popen, status_file: str, job_dir: str,
               graph: Graph, target: str | None) -> None:
        try:
            self._watch_inner(run_id, proc, status_file, job_dir, graph, target)
        except Exception:  # noqa: BLE001 — a supervisor bug must still fence and terminalize its writer
            logging.getLogger("hub").exception("subprocess supervisor failed")
            cancelled = run_id in self._cancelled
            reaped = False
            try:
                try:
                    if proc.poll() is None:
                        self._signal_process(run_id, proc, force=False)
                    proc.wait(timeout=2)
                    self._finalize_process_scope(run_id, proc)
                    reaped = True
                except subprocess.TimeoutExpired:
                    try:
                        self._signal_process(run_id, proc, force=True)
                        proc.wait()
                        self._finalize_process_scope(run_id, proc)
                        reaped = True
                    except Exception:  # noqa: BLE001
                        logging.getLogger("hub").exception(
                            "subprocess supervisor could not force-reap child")
                except Exception:  # noqa: BLE001 — continue into ownership cleanup
                    logging.getLogger("hub").exception(
                        "subprocess supervisor could not reap child cleanly")
                if reaped:
                    owned_result = self._object_results.get(run_id)
                    if owned_result is not None:
                        _safe_abandon_attempt(
                            owned_result["uri"], context="parent object-result supervisor")
                    local_result = self._local_results.get(run_id)
                    if local_result is not None:
                        try:
                            self.storage.abort_result(local_result["uri"], run_id)
                        except Exception:  # exact row remains for the bounded reaper
                            logging.getLogger("hub").exception(
                                "parent local-result supervisor cleanup failed")
                    self._discard_object_sinks(self._object_sinks.get(run_id, {}))
                    self._release_source_leases(run_id)
                else:
                    logging.getLogger("hub").error(
                        "subprocess writer could not be proven stopped; ownership retained")
                current = self.runs.get(run_id)
                status = (current.model_copy(deep=True) if current is not None else RunStatus(
                    run_id=run_id, status="running", placement="local", per_node=[]))
                status.output_uri = status.output_table = None
                if reaped:
                    status.status = "cancelled" if cancelled else "failed"
                    status.error = None if status.status == "cancelled" else "execution supervisor failed"
                    status = self._finalize_reaped_status(
                        run_id, status, deadline_hit=False, returncode=proc.returncode)
                    self._complete(graph, target, status)
                    self._emit(graph, status)
                else:
                    status.status = "running"
                    status.stalled = True
                    status.error = "execution supervisor is retrying writer reconciliation"
                    self._emit(graph, status)
                    self._schedule_watch_retry(
                        run_id, proc, status_file, job_dir, graph, target)
                with self._lock:
                    self.runs[run_id] = status
            finally:
                if reaped:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    with self._lock:
                        self._procs.pop(run_id, None)
                        self._cancel_files.pop(run_id, None)
                        self._object_results.pop(run_id, None)
                        self._local_results.pop(run_id, None)
                        self._object_sinks.pop(run_id, None)
                        self._sink_contracts.pop(run_id, None)

    def _watch_inner(self, run_id: str, proc: subprocess.Popen, status_file: str, job_dir: str,
                     graph: Graph, target: str | None) -> None:
        start = time.monotonic()
        deadline_hit = False
        cancel_seen_at = None
        last = None
        last_emit_at = 0.0
        terminal = None
        source_lease_lost = False
        while True:
            try:
                self._check_source_leases(run_id)
            except Exception:  # noqa: BLE001 — no publication may follow a lost source fence
                source_lease_lost = True
                if proc.poll() is None:
                    try:
                        self._signal_process(run_id, proc, force=False)
                    except OSError:
                        pass
                time.sleep(0.1)
                terminal = self._read(run_id, status_file)
                break
            terminal = self._read(run_id, status_file)
            if terminal is not None:
                break
            # mirror INTERMEDIATE progress to the DB: the kernel poll path reads run_states (not our
            # in-memory dict), so without this the row would sit at 'queued' for the whole run body.
            cur = self.runs.get(run_id)
            if cur is not None:
                dump = cur.model_dump()
                now = time.monotonic()
                heartbeat_s = self._heartbeat_interval_s()
                heartbeat_due = bool(
                    heartbeat_s is not None
                    and now - last_emit_at >= heartbeat_s
                    and proc.poll() is None
                    and self._procs.get(run_id) is proc
                )
                if dump != last or heartbeat_due:
                    self._emit(graph, cur)
                    last = dump
                    last_emit_at = now
            if proc.poll() is not None:      # child exited — do a final read then stop
                time.sleep(0.1)
                terminal = self._read(run_id, status_file)
                break
            if run_id in self._cancelled:
                cancel_seen_at = cancel_seen_at or time.monotonic()
                if time.monotonic() - cancel_seen_at > _CANCEL_GRACE_S:
                    # The cooperative request reaches LocalRunner's cursor interrupt + pre-publish fence.
                    # A runaway Python/native operation may ignore it; terminate only after the grace window.
                    if proc.poll() is None:
                        try:
                            self._signal_process(run_id, proc, force=False)
                        except OSError:
                            pass  # exited between poll and signal
                    time.sleep(0.1)
                    self._read(run_id, status_file)
                    break
            if self.deadline_s and self.deadline_s > 0 and time.monotonic() - start > self.deadline_s:
                deadline_hit = True           # runaway — hard-kill the child and fail the run
                if proc.poll() is None:
                    self._signal_process(run_id, proc, force=False)
                time.sleep(0.1)
                terminal = self._read(run_id, status_file)
                break
            time.sleep(0.15)
        try:
            proc.wait(timeout=2 if run_id in self._cancelled else 5)
        except subprocess.TimeoutExpired:
            # SIGTERM ignored (e.g. a C-level DuckDB loop) → force-reap so _watch can't hang.
            self._signal_process(run_id, proc, force=True)
            proc.wait()
        self._finalize_process_scope(run_id, proc)
        terminal = self._read(run_id, status_file) or terminal
        try:
            self._check_source_leases(run_id)
        except Exception:  # noqa: BLE001 — fence again after reaping and before any publication
            source_lease_lost = True
        current = self.runs.get(run_id)
        st = terminal or (current.model_copy(deep=True) if current is not None else None)
        if st is None:
            st = RunStatus(run_id=run_id, status="failed", placement="local", per_node=[])
        forced = bool(st and st.status in ("queued", "running"))  # exited without a terminal status
        if forced:
            if run_id in self._cancelled:
                st.status = "cancelled"                 # a hard-killed cancel, not a failure (user intent wins)
            elif deadline_hit:
                st.status = "failed"
                st.error = st.error or f"run exceeded the wall-clock deadline of {self.deadline_s:.0f}s — killed"
            else:
                st.status = "failed"                    # crash / OOM / unexpected exit
                st.error = st.error or "execution process exited without a valid terminal status"
        if st is not None and st.status == "done" and proc.returncode != 0:
            if run_id in self._cancelled:
                st.status = "cancelled"
            else:
                st.status = "failed"
                st.error = st.error or f"execution process exited (code {proc.returncode})"
            st.output_uri = st.output_table = None
        if source_lease_lost:
            st.status = "failed"
            st.error = "managed source lease was lost during execution"
            st.output_uri = st.output_table = None
        st = self._finalize_reaped_status(
            run_id, st, deadline_hit=deadline_hit, returncode=proc.returncode)
        owned_sinks = self._object_sinks.get(run_id, {})
        sink_contracts = self._sink_contracts.get(run_id, {})
        managed_sink_uris = {item["uri"] for item in owned_sinks.values()}
        if owned_sinks and st is not None:
            cancelled = run_id in self._cancelled
            child_stopped_cleanly = (
                not cancelled and st.status == "done" and proc.returncode == 0)
            if child_stopped_cleanly:
                try:
                    # The child wrote only its assigned immutable attempts. Exact inventory proof and the
                    # catalog pointer both belong to the parent control plane and happen before terminal done.
                    self._publish_object_sinks(owned_sinks, st)
                except Exception:  # noqa: BLE001 — publication is part of the sink run contract
                    logging.getLogger("hub").exception(
                        "parent managed-sink publication failed")
                    self._discard_object_sinks(owned_sinks)
                    st.status = "failed"
                    st.error = "parent managed-sink publication failed"
                    st.output_uri = st.output_table = None
            else:
                # wait()/kill() above is writer terminal proof; only now may failed/cancelled attempts enter GC.
                self._discard_object_sinks(owned_sinks)
                st.output_uri = st.output_table = None
                if cancelled:
                    st.status = "cancelled"
        local_result = self._local_results.get(run_id)
        local_result_committed = False
        if local_result is not None and st is not None:
            result_uri = local_result["uri"]
            cancelled = run_id in self._cancelled
            valid_child_commit = (
                not cancelled and st.status == "done" and proc.returncode == 0
                and st.output_uri == result_uri)
            if valid_child_commit:
                try:
                    # wait() above is writer terminal proof; only the durable parent may now mark ready.
                    self.storage.commit_result(result_uri, run_id)
                    local_result_committed = True
                    st.output_uri = result_uri
                    st.output_table = None
                except Exception:  # publication cannot continue without the exact ready transition
                    logging.getLogger("hub").exception(
                        "parent local-result commit failed")
                    try:
                        self.storage.abort_result(result_uri, run_id)
                    except Exception:
                        logging.getLogger("hub").exception(
                            "parent local-result commit cleanup failed")
                    st.status = "failed"
                    st.error = "parent local-result commit failed"
                    st.output_uri = st.output_table = None
            else:
                try:
                    self.storage.abort_result(result_uri, run_id)
                except Exception:  # exact row is retained for bounded startup/background recovery
                    logging.getLogger("hub").exception(
                        "parent local-result terminal cleanup failed")
                st.output_uri = st.output_table = None
                if cancelled:
                    st.status = "cancelled"
        owned_result = self._object_results.get(run_id)
        terminal_persisted = False
        if owned_result is not None and st is not None:
            attempt_uri = owned_result["uri"]
            cancelled = run_id in self._cancelled
            valid_child_commit = (
                not cancelled and st.status == "done" and proc.returncode == 0
                and st.output_uri == attempt_uri
            )
            if valid_child_commit:
                try:
                    from hub.handoff import prepare_attempt_commit
                    prepare_attempt_commit(attempt_uri)
                    st.output_uri = attempt_uri
                    st.output_table = None
                except Exception:  # noqa: BLE001 - parent commit is the publication boundary
                    logging.getLogger("hub").exception(
                        "parent object-result commit failed")
                    _safe_abandon_attempt(
                        attempt_uri, context="parent object-result commit")
                    st.status = "failed"
                    st.error = "parent object-result commit failed"
                    st.output_uri = st.output_table = None
            else:
                _safe_abandon_attempt(
                    attempt_uri, context="parent object-result terminal cleanup")
                st.output_uri = st.output_table = None
                if cancelled:
                    st.status = "cancelled"
        # a subprocess run wrote its output in the CHILD's catalog (discarded) — register it here so
        # it shows up in the parent's live catalog, just like an in-process run.
        if (st and st.status == "done" and st.output_uri and st.output_table
                and st.output_uri not in managed_sink_uris and self.catalog is not None):
            try:
                if len(sink_contracts) != 1:
                    raise RuntimeError("child output has no exact parent sink contract")
                contract = next(iter(sink_contracts.values()))
                if (st.output_table != contract["name"]
                        or st.output_uri != contract["published_uri"]):
                    raise RuntimeError("child returned an unexpected unmanaged sink binding")
                publish_kwargs = {
                    "name": st.output_table, "uri": st.output_uri,
                    "parents": contract["parents"], "pipeline": "canvas",
                }
                from hub.plugins.catalog import publish_unmanaged_output_attested
                publish_unmanaged_output_attested(self.catalog, **publish_kwargs)
            except Exception:  # noqa: BLE001 — registration is part of terminal output publication
                logging.getLogger("hub").exception(
                    "parent subprocess catalog registration failed")
                st.status = "failed"
                st.error = "parent catalog registration failed"
                st.output_uri = st.output_table = None
        # The child has been reaped and all parent-side output publication is complete. Release
        # managed inputs before any terminal callback or in-memory status can become observable.
        self._release_source_leases(run_id)
        # Finalize before publishing the terminal status. Otherwise a caller can observe `done` and query
        # the catalog while parent-side output registration is still in flight.
        # Persist run history here (the child disables its own on_complete to avoid a daemon-thread
        # race). We read the terminal status from the child's atomically-written status file, or the
        # status we forced above on a crash/cancel — recording every terminal run, like the in-process
        # backend, with no double-write.
        if st is not None and st.status in ("done", "failed", "cancelled"):
            terminal_rejected = False
            primary_result = local_result or owned_result
            run_state_owner = bool(
                primary_result is not None and primary_result.get("run_state_owner", True))
            if run_state_owner and st.status == "done":
                # This strict parent RunState transaction establishes the primary durable owner.
                # History and cache remain secondary after this point.
                if local_result is not None:
                    persisted_done = st.model_copy(deep=True)
                    persisted_doc = persisted_done.model_dump()

                    def publication_retry(_attempt: int) -> None:
                        # The child's terminal copy remains private.  Keep the parent-observable status
                        # nonterminal and binding-free until the exact done receipt is durable.
                        with self._lock:
                            current = self.runs.get(run_id)
                            visible = (current.model_copy(deep=True) if current is not None
                                       else RunStatus(run_id=run_id, status="running",
                                                      placement="local", per_node=[]))
                            visible.status = "running"
                            visible.stalled = True
                            visible.error = "terminal publication is retrying"
                            visible.output_uri = visible.output_table = None
                            self.runs[run_id] = visible

                    try:
                        _persist_local_result_done(
                            lambda: self._emit(graph, persisted_done, strict=True),
                            lambda: self.storage.result_publication_receipt(
                                local_result["uri"], run_id, persisted_doc),
                            on_retry=publication_retry,
                            wait=self.publication_retry_wait)
                    except Exception as exc:  # definitive owner deletion is not commit-unknown
                        from hub.metadb import RunStatePublicationRejected
                        if not isinstance(exc, RunStatePublicationRejected):
                            raise
                        terminal_rejected = True
                        try:
                            self.storage.abort_result(local_result["uri"], run_id)
                        except Exception:  # bounded maintenance retains an unknown abort fence
                            logging.getLogger("hub").exception(
                                "parent local-result abort failed after publication rejection")
                        st.status = "failed"
                        st.error = str(exc)
                        st.output_uri = st.output_table = None
                    else:
                        terminal_persisted = True
                        self._complete(graph, target, st)
                        cache_key = primary_result.get("cache_key")
                        if cache_key and self.result_put:
                            try:
                                self.result_put(cache_key, {
                                    "rows": st.total_rows or st.rows_processed or 0,
                                    "uri": st.output_uri, "table": None,
                                })
                            except Exception:  # RunState already owns the exact result
                                pass
                else:
                    try:
                        self._emit(graph, st, strict=True)
                        terminal_persisted = True
                        self._complete(graph, target, st)
                        cache_key = primary_result.get("cache_key")
                        if cache_key and self.result_put:
                            try:
                                self.result_put(cache_key, {
                                    "rows": st.total_rows or st.rows_processed or 0,
                                    "uri": st.output_uri, "table": None,
                                })
                            except Exception:  # RunState already owns the exact result
                                pass
                    except Exception as exc:  # object-result receipt/retry is a separate lifecycle
                        from hub.metadb import RunStatePublicationRejected
                        terminal_rejected = isinstance(exc, RunStatePublicationRejected)
                        logging.getLogger("hub").exception(
                            "parent full-result publication failed")
                        _safe_abandon_attempt(
                            owned_result["uri"], context="parent object-result publication")
                        st.status = "failed"
                        st.error = "parent full-result publication failed"
                        st.output_uri = st.output_table = None
            elif (owned_result is not None and not run_state_owner
                  and st.status == "done"):
                # The RunController publishes the region cache pointer immediately after await(). Keep
                # this unit observable without letting RunState own the committed attempt first.
                persisted = st.model_copy(deep=True)
                persisted.output_uri = persisted.output_table = None
                self._complete(graph, target, persisted)
                self._emit(graph, persisted)
                terminal_persisted = True
            if not terminal_persisted:
                self._complete(graph, target, st)
            if not terminal_persisted and not terminal_rejected:
                self._emit(graph, st)
            if terminal_persisted and local_result is not None and local_result_committed:
                try:
                    self.storage.release_result(local_result["uri"], run_id)
                except Exception:  # the durable owner exists; retaining the fd only delays GC
                    logging.getLogger("hub").exception(
                        "parent local-result writer release failed")
            with self._lock:
                self.runs[run_id] = st
        shutil.rmtree(job_dir, ignore_errors=True)
        with self._lock:
            self._procs.pop(run_id, None)
            self._cancel_files.pop(run_id, None)
            self._object_results.pop(run_id, None)
            self._local_results.pop(run_id, None)
            self._object_sinks.pop(run_id, None)
            self._sink_contracts.pop(run_id, None)

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel_acknowledged(self, run_id: str) -> bool:
        """True only once a cancelled child's process is observably gone/reaped."""
        st = self.runs.get(run_id)
        if st is None or st.status != "cancelled":
            return False
        with self._lock:
            proc = self._procs.get(run_id)
        return proc is None or proc.poll() is not None

    def cancel(self, run_id: str) -> RunStatus:
        with self._lock:
            self._cancelled.add(run_id)  # hard-kill fallback resolves as cancelled, not failed
            cancel_file = self._cancel_files.get(run_id)
        if cancel_file:
            try:
                with open(cancel_file, "x"):
                    pass
            except FileExistsError:
                pass
            except OSError:
                pass  # watcher still hard-kills after the bounded grace period
        # _watch publishes `cancelled` only after wait()/kill() has reaped the child. Until then the status
        # remains non-terminal, making terminal status a real stop acknowledgement rather than an optimistic
        # label while the process could still commit an output.
        return self.runs[run_id]
