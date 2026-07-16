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

from hub.models import CompilePlan, Graph, PerNodeStatus, Placement, RunEstimate, RunOutput, RunStatus
from hub.plugins.runner import _CONFIRM_ROWS, _MAX_RUNS, _persist_local_result_done
from hub.process_scope import OwnedProcessScope, owned_process_popen_kwargs
from hub.run_outputs import (
    discard_unpublished_outputs,
    expected_run_outputs,
    outputs_cache_document,
    preflight_output_table,
    preflight_run_output_target,
    require_single_run_output,
    settle_uncommitted_outputs,
    sole_output,
)

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


def _abort_local_results(storage, owned: dict, run_id: str, *, context: str) -> None:
    """Abort every parent reservation only after its child writer has been fenced."""
    for item in owned.get("results", []):
        try:
            storage.abort_result(item["uri"], run_id)
        except Exception:  # exact row remains for bounded startup/background reconciliation
            logging.getLogger("hub").exception("%s cleanup failed", context)


def _release_local_results(storage, owned: dict, run_id: str, uris: set[str]) -> None:
    """Release only the writer locks whose URI is durably owned by the terminal status."""
    for item in owned.get("results", []):
        if item["uri"] not in uris:
            continue
        try:
            storage.release_result(item["uri"], run_id)
        except Exception:  # durable ownership exists; maintenance may retry the exact release
            logging.getLogger("hub").exception("parent local-result writer release failed")


def _local_result_row_count(uri: str) -> int:
    """Read the written local artifact after reap; a child-reported count is not a receipt."""
    import duckdb

    path = uri[len("file://"):] if uri.startswith("file://") else uri
    connection = duckdb.connect(":memory:")
    try:
        return int(connection.execute(
            "SELECT count(*) FROM read_parquet(?)", [path]).fetchone()[0])
    finally:
        connection.close()


class SubprocessRunner:
    name = "local-subprocess"
    manages_source_leases = True

    @staticmethod
    def supports_selected_destination_credentials() -> bool:
        return False  # the ephemeral child has ambient data identity only; it cannot resolve hub Creds

    def __init__(self, workspace: str, data_dir: str, catalog=None, deadline_s: float | None = None,
                 storage=None, resolve_adapter=None, node_builders=None, node_specs=None):
        self.workspace = workspace
        self.data_dir = data_dir
        self.catalog = catalog  # register outputs written by children into the parent's live catalog
        if storage is None:
            from hub.storage import make_storage
            storage = make_storage(workspace)
        self.storage = storage
        self.resolve_adapter = resolve_adapter
        self.node_builders = node_builders if node_builders is not None else {}
        if node_specs is None:
            from hub.nodespecs import BUILTIN_NODE_SPECS
            node_specs = {spec.kind: spec for spec in BUILTIN_NODE_SPECS}
        self.node_specs = node_specs
        self.result_put = None  # optional parent DB cache publication after RunState owns the result
        self.on_complete = None  # optional (graph, target, status) hook — Deps wires it to run-history
        self.on_status = None    # optional (graph, status) hook — Deps wires it to DB-backed live status
        self.runs: dict[str, RunStatus] = {}
        # Execution and supervision retain their mutable owner in ``runs``.  Public status/cancel reads
        # are served from independently validated snapshots published only at coherent _emit boundaries.
        self._published_statuses: dict[str, RunStatus] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._process_scopes: dict[str, OwnedProcessScope] = {}
        self._cancel_files: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._object_results: dict[str, dict] = {}
        self._internal_runs: set[str] = set()
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
        """Spawn and retain exact ownership before returning the child to setup code."""
        grouped = os.name == "posix"
        proc = subprocess.Popen(command, **owned_process_popen_kwargs(kwargs))
        scope = OwnedProcessScope(proc, owns_process_group=grouped)
        with self._lock:
            self._process_scopes[run_id] = scope
        return proc

    def _signal_process(self, run_id: str, proc: subprocess.Popen, *, force: bool) -> None:
        """Signal only the scope still owned by this exact run and Popen."""
        with self._lock:
            scope = self._process_scopes.get(run_id)
        if scope is not None and scope.process is proc:
            scope.request_stop(force=force)

    def _finalize_process_scope(self, run_id: str, proc: subprocess.Popen) -> None:
        """Fence descendants, reap the child, then release this exact process scope."""
        with self._lock:
            scope = self._process_scopes.get(run_id)
        if scope is None or scope.process is not proc:
            if proc.poll() is None:
                raise RuntimeError("live subprocess has no owned process scope")
            return
        if not scope.fence():
            raise RuntimeError("subprocess could not be reaped after SIGKILL")
        with self._lock:
            if self._process_scopes.get(run_id) is scope:
                self._process_scopes.pop(run_id, None)

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
                _abort_local_results(
                    self.storage, local_owned, run_id, context="parent local-result shutdown")
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

    def _claim_sink_contracts(self, plan: CompilePlan, graph: Graph, run_id: str, status: RunStatus
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
        from hub import metadb
        from hub.plugins.catalog import lineage_for_output

        for step in plan.steps:
            if step.kind != "write":
                continue
            node = nodes.get(step.node_id)
            if node is None:
                raise RuntimeError(f"write step '{step.node_id}' has no graph node")
            cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
            title = node.data.get("title") if isinstance(node.data, dict) else None
            spec = SinkSpec.from_config(cfg, title)
            preflight_output_table(status, spec.name)
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
            parents = metadb.catalog_lineage_parent_tokens(
                graph_mod.all_upstream_publication_uris(graph, step.node_id))
            lineage = lineage_for_output(graph, run_id, step.node_id)
            contracts[step.node_id] = {
                "logical_uri": target_uri,
                "published_uri": expected_sink_uri(spec, target_uri, adapter),
                "name": spec.name, "parents": parents, "lineage": lineage,
            }
            if adapter is not None and _is_core_managed_sink(spec, target_uri, adapter):
                managed.append((step.node_id, target_uri, spec, parents, lineage))

        from hub.plugins.catalog import core_managed_publisher, unmanaged_publication_supported
        if len(targets) > 1:
            raise RuntimeError(
                "isolated subprocess runs support one sink until atomic multi-sink publication "
                "is enabled")
        managed_ids = {step_id for step_id, _uri, _spec, _parents, _lineage in managed}
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
            for step_id, logical_uri, spec, parents, lineage in managed:
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
                    "parents": parents, "lineage": lineage,
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

    @staticmethod
    def _set_catalog_output_version(
            status: RunStatus, output: RunOutput, version: str | None) -> None:
        """Replace the child-reported catalog identity with the parent's exact receipt."""
        status.outputs = [RunOutput.model_validate({
            **output.model_dump(),
            "version": version,
        })]

    def _publish_object_sinks(self, sinks: dict[str, dict], status: RunStatus) -> None:
        if not sinks:
            return
        if len(sinks) != 1:
            raise RuntimeError("managed subprocess sink publication requires one exact sink")
        step_id, item = next(iter(sinks.items()))
        output = sole_output(status, committed=True)
        if output is None or output.uri != item["uri"] or output.table != item["name"]:
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
            parents=item["parents"], pipeline="canvas", lineage=item["lineage"])
        table = receipt.get("table") if isinstance(receipt, dict) else None
        table_uri = table.get("uri") if isinstance(table, dict) else getattr(table, "uri", None)
        table_name = table.get("name") if isinstance(table, dict) else getattr(table, "name", None)
        table_version = (
            table.get("version") if isinstance(table, dict) else getattr(table, "version", None)
        )
        if (not isinstance(receipt, dict) or receipt.get("uri") != item["uri"]
                or table is None or table_uri != item["uri"] or table_name != item["name"]):
            raise RuntimeError(
                f"core publisher returned an invalid receipt for sink '{step_id}'")
        self._set_catalog_output_version(status, output, table_version)

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement, run_id: str | None = None,
            request_id: str | None = None, attempt_id: str | None = None,
            input_manifest: list[dict[str, str]] | None = None) -> RunStatus:
        output_target = preflight_run_output_target(plan, target_node_id)
        from hub.sampling import explicit_sample_provenance
        provenance = (explicit_sample_provenance(
            graph, output_target, self.resolve_adapter, returned_rows=0)
            if output_target is not None and self.resolve_adapter is not None else None)
        expected_outputs = ([output.model_copy(update={"sample_provenance": provenance})
                             for output in expected_run_outputs(graph, output_target, self.node_specs)]
                            if output_target is not None else [])
        from hub.backends import require_destination_credential_support
        require_destination_credential_support(self, plan, graph, self.workspace)
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"  # a kernel passes the hub-minted id (authoritative)
        per = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        _ = attempt_id  # OPS-01 port parity; managed publication stamps attempts itself
        status = RunStatus(run_id=run_id, status="queued", placement="local", per_node=per,
                           target_node_id=output_target, request_id=request_id,
                           outputs=expected_outputs)
        job_extra: dict = {"runId": run_id}
        from hub.local_run_inputs import LocalRunInputError, source_nodes, validate_manifest_graph
        private_revisions = any(
            isinstance(node.data, dict)
            and isinstance(node.data.get("config"), dict)
            and node.data["config"].get("_input_revision_id") is not None
            for node in source_nodes(graph, target_node_id)
        )
        if input_manifest is None:
            if private_revisions:
                raise LocalRunInputError("isolated run is missing its admitted input manifest")
        else:
            manifest = validate_manifest_graph(
                graph, target_node_id, input_manifest, require_bound_revisions=True)
            job_extra["inputManifest"] = manifest
            job_extra["inputManifestIdentity"] = {
                "runId": run_id,
                "canvasId": str(graph.id),
                "targetNodeId": target_node_id,
            }
        try:
            source_leases = self._claim_source_leases(graph, target_node_id, run_id)
            with self._lock:
                self._source_leases[run_id] = source_leases
            job_extra["managedSourceAttempts"] = source_leases["attempts"]
            job_extra["managedLocalSources"] = source_leases["local_sources"]
            sink_targets, object_sinks, sink_contracts = self._claim_sink_contracts(
                plan, graph, run_id, status)
            if object_sinks:
                self._object_sinks[run_id] = object_sinks
            if sink_contracts:
                self._sink_contracts[run_id] = sink_contracts
            job_extra["sinkTargets"] = sink_targets
            job_extra["sinkAttempts"] = {
                step_id: item["uri"] for step_id, item in object_sinks.items()}

            target = next((node for node in graph.nodes if node.id == output_target), None)
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
                    reservations = []
                    self._local_results[run_id] = {
                        "results": reservations, "cache_key": phash if cacheable else None,
                        "run_state_owner": True,
                    }
                    for index, output in enumerate(expected_outputs):
                        result_uri = begin_local(
                            f"{phash}:{output.node_id}:{output.port_id}:{index}", run_id)
                        reservation = {
                            "nodeId": output.node_id,
                            "portId": output.port_id,
                            "uri": result_uri,
                        }
                        reservations.append(reservation)
                        lock_fd = self.storage.result_lock_fd(result_uri, run_id)
                        reservation.update({
                            "lockFd": lock_fd,
                            "lockToken": (
                                self.storage._read_lock_token(lock_fd)
                                if lock_fd is not None else None),
                        })
                    job_extra["forcedResults"] = reservations
                    identity = self.storage.result_namespace_identity()
                    job_extra["resultNamespaceId"] = self.storage.namespace_id
                    job_extra["resultNamespaceIdentity"] = list(identity)
                else:
                    logical_uri = self.storage.output_uri(
                        f"__result_{run_id}", ".parquet")
                    from hub.plugins.adapters import is_object_uri
                    if not is_object_uri(logical_uri):
                        raise RuntimeError(
                            "isolated named outputs require local managed result storage")
                    if len(expected_outputs) != 1:
                        raise RuntimeError(
                            "object-backed isolated results do not support named multi-output runs")
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
                    output = expected_outputs[0]
                    job_extra["forcedResults"] = [{
                        "nodeId": output.node_id,
                        "portId": output.port_id,
                        "uri": handle["uri"],
                    }]
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
                _abort_local_results(
                    self.storage, local_owned, run_id, context="parent local-result pre-dispatch")
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
        expected_output = require_single_run_output(graph, output_node, self.node_specs)
        from hub import compiler
        from hub.backends import require_destination_credential_support
        plan = compiler.compile_plan(graph, output_node)
        require_destination_credential_support(self, plan, graph, self.workspace)
        run_id = f"unit_{uuid.uuid4().hex[:10]}"
        status = RunStatus(
            run_id=run_id, status="queued", placement="local", per_node=[],
            target_node_id=output_node, outputs=[expected_output])
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
        if job_extra.get("materializeUri"):
            self._internal_runs.add(run_id)
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
            inherited_fds.extend(
                int(item["lockFd"])
                for item in (job_extra.get("forcedResults") or [])
                if item.get("lockFd") is not None)
            if inherited_fds:
                popen_kwargs["pass_fds"] = tuple(sorted(set(inherited_fds)))
            proc = self._spawn_process(
                run_id, [sys.executable, "-m", "hub.subrun", job_file], **popen_kwargs)
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            self._internal_runs.discard(run_id)
            raise
        try:
            with self._lock:
                self.runs[run_id] = status
                self._published_statuses[run_id] = RunStatus.model_validate(
                    status.model_dump())
                self._procs[run_id] = proc
                self._cancel_files[run_id] = cancel_file
                self._evict()
            self._emit(graph, status)  # persist 'queued' to the DB (pollable on any instance / after restart)
            threading.Thread(
                target=self._watch,
                args=(run_id, proc, status_file, job_dir, graph, target),
                daemon=True,
            ).start()
            return self.status(run_id)
        except Exception as exc:
            # Once Popen succeeds the child may be writing. Reap it before the caller terminalizes the
            # parent-owned attempt; setup failure alone is not writer terminal proof.
            reaped = False
            try:
                self._finalize_process_scope(run_id, proc)
                reaped = True
            except Exception:  # noqa: BLE001
                logging.getLogger("hub").exception(
                    "post-Popen setup failure could not fence and reap child")
            if reaped:
                with self._lock:
                    self.runs.pop(run_id, None)
                    self._published_statuses.pop(run_id, None)
                    self._procs.pop(run_id, None)
                    self._process_scopes.pop(run_id, None)
                    self._cancel_files.pop(run_id, None)
                    self._internal_runs.discard(run_id)
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
        snapshot = RunStatus.model_validate(status.model_dump())
        internal = status.run_id in self._internal_runs
        if not internal and self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise
        self._publish_status_snapshot(status, snapshot=snapshot)

    def _publish_status_snapshot(
            self, status: RunStatus, *, snapshot: RunStatus | None = None) -> None:
        """Atomically publish one validated observation without replacing the live owner."""
        snapshot = snapshot or RunStatus.model_validate(status.model_dump())
        with self._lock:
            if status.run_id in self.runs:
                self._published_statuses[status.run_id] = snapshot

    def _complete(self, graph: Graph, target: str | None, status: RunStatus) -> None:
        if status.run_id in self._internal_runs:
            return
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
            victim = next((rid for rid in self.runs
                           if (published := self._published_statuses.get(rid)) is not None
                           and published.status in _terminal), None)
            if victim is None:
                break  # all retained runs are still live — exceed the cap rather than drop one
            self.runs.pop(victim, None)
            self._published_statuses.pop(victim, None)
            self._cancelled.discard(victim)
            self._procs.pop(victim, None)
            self._process_scopes.pop(victim, None)
            self._cancel_files.pop(victim, None)
            self._internal_runs.discard(victim)
            self._sink_contracts.pop(victim, None)

    def _sanitize_child_status(self, run_id: str, observed: RunStatus) -> RunStatus:
        """Apply backend-specific identity fences to an untrusted child status document.

        Ordinary subprocess runs already get their parent run id overridden in :meth:`_read` and need
        no additional changes. Specialized one-shot workloads can override this hook without copying
        the process supervision and reap-before-terminal state machine.
        """
        return observed

    def _fence_child_outputs(self, run_id: str, observed: RunStatus) -> RunStatus:
        """Bind an untrusted child receipt to the declaration snapshot owned by the parent."""
        current = self.runs.get(run_id)
        if current is None:
            raise ValueError("subprocess status has no parent-owned run")
        observed.target_node_id = current.target_node_id
        observed.request_id = current.request_id
        observed.job_type = current.job_type
        observed.placement = current.placement
        if current.job_type == "profile":
            observed.outputs = []
            return observed
        if not current.outputs and current.target_node_id is None:
            observed.outputs = []
            return observed
        expected = current.outputs
        if not expected:
            raise ValueError("subprocess status does not contain the expected output set")
        expected_identities = [
            (output.node_id, output.port_id, output.port_label, output.wire,
             output.publication_kind)
            for output in expected]
        actual_identities = [
            (output.node_id, output.port_id, output.port_label, output.wire,
             output.publication_kind)
            for output in observed.outputs]
        if observed.status in ("done", "failed", "cancelled") \
                and actual_identities != expected_identities:
            raise ValueError("subprocess status output declaration does not match its parent")
        if observed.status not in ("done", "failed", "cancelled"):
            observed.outputs = [RunOutput(
                node_id=output.node_id, port_id=output.port_id,
                port_label=output.port_label, wire=output.wire,
                publication_kind=output.publication_kind, outcome="pending")
                for output in expected]
            observed.total_rows = None
            return observed
        if observed.status == "done" and any(
                output.outcome != "committed" for output in observed.outputs):
            raise ValueError("a completed subprocess did not commit every expected output")
        if observed.status == "cancelled":
            # Cancellation wins over a child-local success report; the parent aborts every reservation
            # after reap and no URI may become externally visible.
            observed.outputs = [RunOutput(
                node_id=output.node_id, port_id=output.port_id,
                port_label=output.port_label, wire=output.wire,
                publication_kind=output.publication_kind, outcome="cancelled")
                for output in expected]
        if observed.status in ("failed", "cancelled"):
            observed.total_rows = None
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
        try:
            observed = self._fence_child_outputs(
                run_id, self._sanitize_child_status(run_id, observed))
        except Exception:  # noqa: BLE001 — output receipts are untrusted child input
            logging.getLogger("hub").exception(
                "ignored subprocess status with an invalid output receipt")
            return None
        if observed.status in ("done", "failed", "cancelled"):
            return observed
        # Child progress is untrusted and parent-owned result/sink paths are provisional until reap,
        # exact receipt validation, and parent commit. Never mirror an intermediate output binding.
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
                    self._finalize_process_scope(run_id, proc)
                    reaped = True
                except Exception:  # noqa: BLE001 — continue into ownership cleanup
                    logging.getLogger("hub").exception(
                        "subprocess supervisor could not fence and reap child")
                if reaped:
                    owned_result = self._object_results.get(run_id)
                    if owned_result is not None:
                        _safe_abandon_attempt(
                            owned_result["uri"], context="parent object-result supervisor")
                    local_result = self._local_results.get(run_id)
                    if local_result is not None:
                        _abort_local_results(
                            self.storage, local_result, run_id,
                            context="parent local-result supervisor")
                    self._discard_object_sinks(self._object_sinks.get(run_id, {}))
                    self._release_source_leases(run_id)
                else:
                    logging.getLogger("hub").error(
                        "subprocess writer could not be proven stopped; ownership retained")
                current = self.runs.get(run_id)
                status = (current.model_copy(deep=True) if current is not None else RunStatus(
                    run_id=run_id, status="running", placement="local", per_node=[]))
                if reaped:
                    status.status = "cancelled" if cancelled else "failed"
                    status.error = None if status.status == "cancelled" else "execution supervisor failed"
                    if status.job_type == "run":
                        discard_unpublished_outputs(
                            status, status.status, status.error)
                    status = self._finalize_reaped_status(
                        run_id, status, deadline_hit=False, returncode=proc.returncode)
                    self._complete(graph, target, status)
                    self._emit(graph, status)
                else:
                    status.status = "running"
                    status.stalled = True
                    status.error = "execution supervisor is retrying writer reconciliation"
                    if status.job_type == "run":
                        discard_unpublished_outputs(status, "pending")
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
                        self._process_scopes.pop(run_id, None)
                        self._cancel_files.pop(run_id, None)
                        self._internal_runs.discard(run_id)
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
        stop_requested = source_lease_lost or deadline_hit or run_id in self._cancelled
        if terminal is not None and not stop_requested and proc.poll() is None:
            try:
                # A valid terminal receipt may precede ordinary interpreter cleanup very slightly.
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_process(run_id, proc, force=False)
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
            if st.job_type == "run":
                discard_unpublished_outputs(st, st.status, st.error)
                st.total_rows = None
        if source_lease_lost:
            st.status = "failed"
            st.error = "managed source lease was lost during execution"
            if st.job_type == "run":
                discard_unpublished_outputs(st, "failed", st.error)
                st.total_rows = None
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
                    discard_unpublished_outputs(st, "failed", st.error)
                    st.total_rows = None
            else:
                # The process-scope fence above is writer terminal proof; only now may attempts enter GC.
                self._discard_object_sinks(owned_sinks)
                if cancelled:
                    st.status = "cancelled"
                elif st.status == "done":
                    st.status = "failed"
                    st.error = "subprocess sink did not exit cleanly"
                discard_unpublished_outputs(st, st.status, st.error)
                st.total_rows = None
        local_result = self._local_results.get(run_id)
        local_result_committed: set[str] = set()
        if local_result is not None and st is not None:
            cancelled = run_id in self._cancelled
            reservations = local_result["results"]
            child_outputs = st.outputs
            committed_count = 0
            valid_child_commit = (
                not cancelled and proc.returncode == 0
                and st.status in ("done", "failed")
                and len(child_outputs) == len(reservations)
                and all(
                    output.node_id == reservation["nodeId"]
                    and output.port_id == reservation["portId"]
                    and (output.outcome != "committed" or output.uri == reservation["uri"])
                    for output, reservation in zip(child_outputs, reservations)))
            if valid_child_commit:
                while (committed_count < len(child_outputs)
                       and child_outputs[committed_count].outcome == "committed"):
                    committed_count += 1
                valid_child_commit = (
                    all(output.outcome != "committed"
                        for output in child_outputs[committed_count:])
                    and (st.status != "done" or committed_count == len(child_outputs)))
            if valid_child_commit:
                try:
                    # The child is reaped. Commit the exact declaration-ordered prefix and abort every
                    # reservation it did not truthfully publish.
                    for reservation, output in zip(
                            reservations[:committed_count], child_outputs[:committed_count]):
                        if _local_result_row_count(reservation["uri"]) != output.rows:
                            raise RuntimeError(
                                "child local-result row count does not match its artifact")
                        self.storage.commit_result(reservation["uri"], run_id)
                        local_result_committed.add(reservation["uri"])
                    for reservation in reservations[committed_count:]:
                        self.storage.abort_result(reservation["uri"], run_id)
                except Exception:  # publication cannot continue without the exact ready transition
                    logging.getLogger("hub").exception(
                        "parent local-result commit failed")
                    for reservation in reservations:
                        if reservation["uri"] in local_result_committed:
                            continue
                        try:
                            self.storage.abort_result(reservation["uri"], run_id)
                        except Exception:
                            logging.getLogger("hub").exception(
                                "parent local-result commit cleanup failed")
                    st.status = "failed"
                    st.error = "parent local-result commit failed"
                    preserved = {
                        output.port_id: output for output in st.outputs
                        if output.outcome == "committed" and output.uri in local_result_committed}
                    discard_unpublished_outputs(st, "failed", st.error)
                    for index, output in enumerate(st.outputs):
                        if output.port_id in preserved:
                            st.outputs[index] = preserved[output.port_id]
                    st.total_rows = None
            else:
                _abort_local_results(
                    self.storage, local_result, run_id,
                    context="parent local-result terminal")
                if cancelled:
                    st.status = "cancelled"
                elif st.status == "done":
                    st.status = "failed"
                    st.error = "child returned an unexpected local-result binding"
                discard_unpublished_outputs(st, st.status, st.error)
                st.total_rows = None
        owned_result = self._object_results.get(run_id)
        terminal_persisted = False
        if owned_result is not None and st is not None:
            attempt_uri = owned_result["uri"]
            cancelled = run_id in self._cancelled
            child_output = sole_output(st, committed=True)
            valid_child_commit = (
                not cancelled and st.status == "done" and proc.returncode == 0
                and child_output is not None and child_output.uri == attempt_uri
            )
            if valid_child_commit:
                try:
                    from hub.handoff import prepare_attempt_commit
                    prepare_attempt_commit(attempt_uri)
                except Exception:  # noqa: BLE001 - parent commit is the publication boundary
                    logging.getLogger("hub").exception(
                        "parent object-result commit failed")
                    _safe_abandon_attempt(
                        attempt_uri, context="parent object-result commit")
                    st.status = "failed"
                    st.error = "parent object-result commit failed"
                    discard_unpublished_outputs(st, "failed", st.error)
                    st.total_rows = None
            else:
                _safe_abandon_attempt(
                    attempt_uri, context="parent object-result terminal cleanup")
                if cancelled:
                    st.status = "cancelled"
                elif st.status == "done":
                    st.status = "failed"
                    st.error = "child returned an unexpected object-result binding"
                discard_unpublished_outputs(st, st.status, st.error)
                st.total_rows = None
        # a subprocess run wrote its output in the CHILD's catalog (discarded) — register it here so
        # it shows up in the parent's live catalog, just like an in-process run.
        catalog_output = sole_output(st, committed=True) if st is not None else None
        if (st and st.status == "done" and catalog_output is not None
                and catalog_output.table and catalog_output.uri not in managed_sink_uris
                and self.catalog is not None):
            try:
                if len(sink_contracts) != 1:
                    raise RuntimeError("child output has no exact parent sink contract")
                contract = next(iter(sink_contracts.values()))
                if (catalog_output.table != contract["name"]
                        or catalog_output.uri != contract["published_uri"]):
                    raise RuntimeError("child returned an unexpected unmanaged sink binding")
                publish_kwargs = {
                    "name": catalog_output.table, "uri": catalog_output.uri,
                    "parents": contract["parents"], "pipeline": "canvas",
                    "lineage": contract["lineage"],
                }
                from hub.plugins.catalog import publish_unmanaged_output_attested
                observed = publish_unmanaged_output_attested(self.catalog, **publish_kwargs)
                observed_version = (
                    observed.get("version") if isinstance(observed, dict)
                    else getattr(observed, "version", None)
                )
                self._set_catalog_output_version(st, catalog_output, observed_version)
            except Exception:  # noqa: BLE001 — registration is part of terminal output publication
                logging.getLogger("hub").exception(
                    "parent subprocess catalog registration failed")
                st.status = "failed"
                st.error = "parent catalog registration failed"
                discard_unpublished_outputs(st, "failed", st.error)
                st.total_rows = None
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
            if st.job_type == "run":
                if st.status in ("failed", "cancelled"):
                    settle_uncommitted_outputs(st, st.status, st.error)
                committed_output = sole_output(st, committed=True)
                if st.status == "done" and st.target_node_id is not None \
                        and (not st.outputs or any(
                            output.outcome != "committed" for output in st.outputs)):
                    st.status = "failed"
                    st.error = "subprocess completed without every expected output"
                    discard_unpublished_outputs(st, "failed", st.error)
                    committed_output = None
                st.total_rows = committed_output.rows if committed_output is not None else None
            terminal_rejected = False
            primary_result = local_result or owned_result
            run_state_owner = bool(
                primary_result is not None and primary_result.get("run_state_owner", True))
            if run_state_owner and (
                    st.status == "done" or (local_result is not None and local_result_committed)):
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
                            if visible.job_type == "run":
                                discard_unpublished_outputs(visible, "pending")
                                visible.total_rows = None
                            self.runs[run_id] = visible

                    try:
                        _persist_local_result_done(
                            lambda: self._emit(graph, persisted_done, strict=True),
                            lambda: self.storage.result_publication_receipt(
                                local_result_committed, run_id, persisted_doc),
                            on_retry=publication_retry,
                            wait=self.publication_retry_wait)
                    except Exception as exc:  # definitive owner deletion is not commit-unknown
                        from hub.metadb import RunStatePublicationRejected
                        if not isinstance(exc, RunStatePublicationRejected):
                            raise
                        terminal_rejected = True
                        _abort_local_results(
                            self.storage, local_result, run_id,
                            context="parent local-result publication rejection")
                        st.status = "failed"
                        st.error = str(exc)
                        discard_unpublished_outputs(st, "failed", st.error)
                        st.total_rows = None
                    else:
                        terminal_persisted = True
                        self._complete(graph, target, st)
                        cache_key = primary_result.get("cache_key")
                        if cache_key and self.result_put:
                            try:
                                self.result_put(cache_key, outputs_cache_document(st))
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
                                self.result_put(cache_key, outputs_cache_document(st))
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
                        discard_unpublished_outputs(st, "failed", st.error)
                        st.total_rows = None
            if not terminal_persisted:
                self._complete(graph, target, st)
            if not terminal_persisted and not terminal_rejected:
                self._emit(graph, st)
            elif terminal_rejected:
                # The durable owner is definitively gone, so invoking the persistence hook again would
                # be incorrect. The process-local owner still publishes its sanitized failure snapshot.
                self._publish_status_snapshot(st)
            if terminal_persisted and local_result is not None and local_result_committed:
                _release_local_results(
                    self.storage, local_result, run_id, local_result_committed)
            with self._lock:
                self.runs[run_id] = st
        shutil.rmtree(job_dir, ignore_errors=True)
        with self._lock:
            self._procs.pop(run_id, None)
            self._process_scopes.pop(run_id, None)
            self._cancel_files.pop(run_id, None)
            self._internal_runs.discard(run_id)
            self._object_results.pop(run_id, None)
            self._local_results.pop(run_id, None)
            self._object_sinks.pop(run_id, None)
            self._sink_contracts.pop(run_id, None)

    def status(self, run_id: str) -> RunStatus:
        with self._lock:
            return self._published_statuses[run_id].model_copy(deep=True)

    def cancel_acknowledged(self, run_id: str) -> bool:
        """True only once a cancelled child's process is observably gone/reaped."""
        st = self.runs.get(run_id)
        if st is None or st.status != "cancelled":
            return False
        with self._lock:
            proc = self._procs.get(run_id)
            scope = self._process_scopes.get(run_id)
        return proc is None and scope is None

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
        # _watch publishes `cancelled` only after the process group is fenced and its direct child reaped.
        # Until then status remains non-terminal, so it cannot acknowledge stop while a descendant may write.
        return self.status(run_id)
