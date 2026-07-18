"""Two-phase durable worker for one exact Source -> Select(checkpoint) -> Write task.

Phase 1 materializes and commits Select output through #413's lifecycle. Phase 2 resumes from that
committed evidence and publishes only the frozen managed-local Write. The generic whole-graph worker
and planner checkpoint semantics are never used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
import uuid

from hub import compiler, linear_checkpoint as lc, metadb
from hub.local_run_inputs import bind_manifest
from hub.models import Graph, RunOutput, RunStatus, WriteIntent, WriteReceipt
from hub.plugins.runner import LocalRunner


_active_lock = threading.Lock()
_active: dict[str, tuple[LocalRunner | None, threading.Thread, str | None]] = {}
_JOIN_POLL_SECONDS = 0.1
log = logging.getLogger("hub")


def _failed(task_id: str, target: str, exc: BaseException) -> dict:
    return RunStatus(
        run_id=task_id, status="failed", target_node_id=target,
        error=f"{type(exc).__name__}: {exc}",
    ).model_dump()


def _cancelled(task_id: str, target: str) -> dict:
    return RunStatus(
        run_id=task_id, status="cancelled", target_node_id=target,
    ).model_dump()


def _progress(
        task_id: str, target: str, *, phase: str, progress: float,
        outputs: list | None = None, error: str | None = None) -> dict:
    status = RunStatus(
        run_id=task_id, status="running", target_node_id=target,
        progress=progress, error=error, outputs=outputs or [],
    )
    doc = status.model_dump()
    doc["checkpoint_phase"] = phase
    return doc


def _cancel_quietly(runner: LocalRunner | None, task_id: str) -> None:
    if runner is None:
        return
    try:
        runner.cancel(task_id)
    except KeyError:
        pass


def _wait_for_owned_worker(
        runner: LocalRunner | None, task_id: str, attempt_id: str, owner_token: str) -> bool:
    if runner is None:
        return True
    while True:
        try:
            if runner.wait_for_worker(task_id, timeout=_JOIN_POLL_SECONDS):
                return True
        except KeyError:
            return True
        except BaseException:
            _cancel_quietly(runner, task_id)
            return False
        if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
            _cancel_quietly(runner, task_id)
            return False
        if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
            _cancel_quietly(runner, task_id)


def _prefix_graph(graph: Graph, select_id: str) -> Graph:
    """Return only Source -> Select; Write is excluded from Phase 1 execution."""
    select = next(node for node in graph.nodes if node.id == select_id)
    incoming = [edge for edge in graph.edges if edge.target == select_id]
    source_ids = {edge.source for edge in incoming}
    nodes = [node for node in graph.nodes if node.id in source_ids or node.id == select_id]
    return Graph.model_validate({
        "id": graph.id, "version": graph.version,
        "nodes": [node.model_dump(by_alias=True, mode="json") for node in nodes],
        "edges": [edge.model_dump(by_alias=True, mode="json") for edge in incoming],
    })


def _read_result_bytes(storage, uri: str) -> bytes:
    guard = storage.acquire_result_read(uri, owner="checkpoint-prefix")
    try:
        fd = os.dup(guard.artifact_fileno())
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
        finally:
            os.close(fd)
        guard.check()
        return content
    finally:
        guard.close()


def _run_local(
        deps, graph: Graph, target: str, task_id: str, attempt_id: str, owner_token: str,
        *, parent_attested: frozenset[str] | None = None,
        run_key: str | None = None) -> tuple[LocalRunner, RunStatus]:
    runner = LocalRunner(
        deps.resolve_adapter, deps.registry, deps.catalog, deps.workspace,
        node_builders=deps.node_builders, node_specs=deps.node_specs,
        storage=deps.storage,
    )
    if parent_attested is not None:
        runner.parent_attested_source_uris = parent_attested
    key = run_key or task_id
    with _active_lock:
        active = _active.get(task_id)
        if active is not None and active[1] is threading.current_thread():
            _active[task_id] = (runner, active[1], key)

    def persist(_graph, status: RunStatus) -> None:
        if status.status not in ("done", "failed", "cancelled"):
            metadb.update_durable_task_status(
                task_id, attempt_id, owner_token, status.model_dump())

    runner.on_status = persist
    runner.on_complete = None
    plan = compiler.compile_plan(
        graph, target, deps.registry, deps.node_specs, deps.node_ir)
    if not plan.acyclic:
        raise RuntimeError(plan.error or "linear checkpoint graph has a cycle")
    status = runner.run(
        plan, graph, target, "local", run_id=key,
        cancel_check=lambda: metadb.durable_task_attempt_should_stop(
            task_id, attempt_id, owner_token),
    )
    next_heartbeat = 0.0
    while status.status not in ("done", "failed", "cancelled"):
        now = time.monotonic()
        if now >= next_heartbeat:
            next_heartbeat = now + 1.0
            if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                runner.cancel(key)
            elif metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                runner.cancel(key)
        time.sleep(0.1)
        status = runner.status(key)
    return runner, status


def _materialize_prefix(
        deps, claimed: dict, attempt_id: str, owner_token: str,
        candidate: dict) -> lc.CheckpointEvidence:
    task_id = claimed["id"]
    target = claimed["target_node_id"]
    select_id = claimed["checkpoint_node_id"]
    graph = Graph.model_validate(claimed["graph_doc"])
    prefix = _prefix_graph(graph, select_id)
    prefix = bind_manifest(
        prefix, select_id, claimed["input_manifest"], deps.resolve_adapter)
    metadb.update_durable_task_status(
        task_id, attempt_id, owner_token,
        _progress(task_id, target, phase="materializing", progress=0.2))
    runner, status = _run_local(
        deps, prefix, select_id, task_id, attempt_id, owner_token,
        run_key=f"{task_id}:prefix")
    try:
        if not _wait_for_owned_worker(runner, f"{task_id}:prefix", attempt_id, owner_token):
            raise RuntimeError("linear checkpoint prefix owner lost the lease")
        if status.status == "cancelled" or metadb.durable_task_attempt_should_stop(
                task_id, attempt_id, owner_token):
            lc.abort_checkpoint(
                deps.storage, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)
            raise RuntimeError("cancelled")
        if status.status != "done":
            raise RuntimeError(status.error or "linear checkpoint prefix failed")
        outputs = [output for output in status.outputs if output.outcome == "committed"]
        if len(outputs) != 1 or not outputs[0].uri:
            raise RuntimeError("linear checkpoint prefix did not commit one Select output")
        content = _read_result_bytes(deps.storage, outputs[0].uri)
        # Prefix LocalRunner results are never checkpoint truth; reclaim after bytes are copied.
        with contextlib.suppress(OSError, RuntimeError, KeyError):
            deps.storage.abort_result(outputs[0].uri, f"{task_id}:prefix")
        return lc.materialize_and_commit_checkpoint(
            deps.storage, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
            candidate=candidate, content=content)
    finally:
        _cancel_quietly(runner, f"{task_id}:prefix")


def _done_status(task_id: str, target: str, write_intent: WriteIntent, receipt: WriteReceipt) -> RunStatus:
    output = RunOutput(
        node_id=target, port_id="out", wire="dataset",
        publication_kind="catalog", outcome="committed",
        uri=receipt.publication.artifact_uri,
        table=write_intent.destination.name,
        version=receipt.publication.catalog_version,
        rows=receipt.rows,
        write_receipt=receipt,
    )
    return RunStatus(
        run_id=task_id, status="done", target_node_id=target, progress=1.0,
        outputs=[output], total_rows=receipt.rows,
    )


def _publish_from_checkpoint(
        deps, claimed: dict, attempt_id: str, owner_token: str) -> RunStatus:
    task_id = claimed["id"]
    target = claimed["target_node_id"]
    write_intent = WriteIntent.model_validate(claimed["write_intent"])
    # Response-loss reconcile before opening the artifact.
    prior = metadb.catalog_managed_local_write_receipt(
        write_intent.model_dump(by_alias=True, mode="json"))
    if prior is not None:
        return _done_status(task_id, target, write_intent, WriteReceipt.model_validate(prior))

    metadb.update_durable_task_status(
        task_id, attempt_id, owner_token,
        _progress(task_id, target, phase="publishing", progress=0.7))
    try:
        guard, _evidence = lc.reopen_checkpoint(deps.storage, task_id)
    except Exception as exc:
        raise RuntimeError(f"checkpoint_invalid: {type(exc).__name__}") from exc

    from hub.local_writes import write_managed_local_file

    def write_artifact(candidate_uri: str) -> None:
        if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
            raise RuntimeError("cancelled")
        fd = os.dup(guard.artifact_fileno())
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            src = os.fdopen(fd, "rb")
            fd = -1
            with src, open(candidate_uri, "wb") as dst:
                while True:
                    if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                        raise RuntimeError("linear checkpoint publication owner lost the lease")
                    if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                        raise RuntimeError("cancelled")
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        guard.check()

    try:
        if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
            return RunStatus.model_validate(_cancelled(task_id, target))
        receipt = write_managed_local_file(
            storage=deps.storage, catalog=deps.catalog, intent=write_intent,
            write_artifact=write_artifact, before_publish=guard.check)
        guard.check()
        return _done_status(task_id, target, write_intent, receipt)
    except RuntimeError as exc:
        if str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(
                task_id, attempt_id, owner_token):
            prior = metadb.catalog_managed_local_write_receipt(
                write_intent.model_dump(by_alias=True, mode="json"))
            if prior is not None:
                return _done_status(
                    task_id, target, write_intent, WriteReceipt.model_validate(prior))
            return RunStatus.model_validate(_cancelled(task_id, target))
        raise
    finally:
        try:
            guard.close()
        except Exception:
            log.warning("checkpoint read guard release failed", exc_info=True)


def _worker(task_id: str, deps) -> None:
    owner_token = f"{uuid.uuid4().hex}:{threading.get_ident()}"
    runner: LocalRunner | None = None
    try:
        claimed = metadb.claim_linear_checkpoint_task(task_id, owner_token)
        if claimed is None:
            return
        # Prefer the durable checkpoint row for node/port identity; avoid candidate-binding
        # validation that only applies while a reservation is in flight.
        with metadb.session() as session:
            checkpoint = session.get(metadb.DurableCheckpoint, task_id)
            if checkpoint is None:
                return
            claimed = {
                **claimed,
                "checkpoint_node_id": checkpoint.checkpoint_node_id,
                "output_port_id": checkpoint.output_port_id,
                "checkpoint_id": checkpoint.checkpoint_id,
            }
        attempt = claimed["attempts"][-1]
        attempt_id = str(attempt["id"])
        target = str(claimed["target_node_id"])
        try:
            if not metadb.heartbeat_durable_task(task_id, attempt_id, owner_token):
                return
            recovery = lc.recover_checkpoint(
                deps.storage, task_id=task_id, attempt_id=attempt_id, owner_token=owner_token)
            evidence = None
            if recovery["action"] == "committed":
                evidence = recovery["evidence"]
            elif recovery["action"] == "reattach":
                evidence = lc.commit_reattached_checkpoint(
                    deps.storage, task_id=task_id, attempt_id=attempt_id,
                    owner_token=owner_token, candidate=recovery["candidate"])
            else:
                if recovery["action"] == "retire":
                    pass  # candidate already reclaimed
                if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token, _cancelled(task_id, target))
                    return
                candidate = metadb.reserve_linear_checkpoint_candidate(
                    task_id=task_id, attempt_id=attempt_id, owner_token=owner_token,
                    namespace_id=deps.storage.namespace_id,
                    storage_root=deps.storage.result_root,
                    writer_token=uuid.uuid4().hex, lock_token=uuid.uuid4().hex)
                try:
                    evidence = _materialize_prefix(
                        deps, claimed, attempt_id, owner_token, candidate)
                except RuntimeError as exc:
                    if str(exc) == "cancelled" or metadb.durable_task_attempt_should_stop(
                            task_id, attempt_id, owner_token):
                        with contextlib.suppress(OSError, RuntimeError):
                            lc.abort_checkpoint(
                                deps.storage, task_id=task_id, attempt_id=attempt_id,
                                owner_token=owner_token)
                        if _wait_for_owned_worker(runner, task_id, attempt_id, owner_token):
                            metadb.finish_durable_task_attempt(
                                task_id, attempt_id, owner_token, _cancelled(task_id, target))
                        return
                    raise
                # Commit response-loss: prefer DB truth over the raised path.
                if evidence is None:
                    evidence = lc.reconcile_checkpoint(task_id)
                if evidence is None:
                    raise RuntimeError("checkpoint commit did not produce durable evidence")

            metadb.update_durable_task_status(
                task_id, attempt_id, owner_token,
                _progress(task_id, target, phase="committed", progress=0.55))
            if metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                # Post-commit cancel retains the checkpoint.
                metadb.finish_durable_task_attempt(
                    task_id, attempt_id, owner_token, _cancelled(task_id, target))
                return

            status = _publish_from_checkpoint(deps, claimed, attempt_id, owner_token)
            if status.status == "cancelled" or metadb.durable_task_attempt_should_stop(
                    task_id, attempt_id, owner_token):
                # Publication vs cancel: if receipt already landed, reconcile success.
                prior = metadb.catalog_managed_local_write_receipt(claimed["write_intent"])
                if prior is not None:
                    status = _done_status(
                        task_id, target, WriteIntent.model_validate(claimed["write_intent"]),
                        WriteReceipt.model_validate(prior))
                else:
                    status = RunStatus.model_validate(_cancelled(task_id, target))
            metadb.finish_durable_task_attempt(
                task_id, attempt_id, owner_token, status.model_dump())
        except BaseException as exc:
            log.exception("linear checkpoint task failed")
            if runner is None or _wait_for_owned_worker(
                    runner, task_id, attempt_id, owner_token):
                # Prefer receipt if publication committed under a racing cancel/failure.
                prior = None
                with contextlib.suppress(Exception):
                    prior = metadb.catalog_managed_local_write_receipt(claimed["write_intent"])
                if prior is not None:
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token,
                        _done_status(
                            task_id, target,
                            WriteIntent.model_validate(claimed["write_intent"]),
                            WriteReceipt.model_validate(prior)).model_dump())
                elif metadb.durable_task_attempt_should_stop(task_id, attempt_id, owner_token):
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token, _cancelled(task_id, target))
                else:
                    metadb.finish_durable_task_attempt(
                        task_id, attempt_id, owner_token, _failed(task_id, target, exc))
    finally:
        with _active_lock:
            current = _active.get(task_id)
            if current is not None and current[1] is threading.current_thread():
                _active.pop(task_id, None)


def dispatch(task_id: str, deps) -> None:
    """Start one two-phase supervisor after the durable admission transaction committed."""
    with _active_lock:
        current = _active.get(str(task_id))
        if current is not None and current[1].is_alive():
            return
        thread = threading.Thread(
            target=_worker, args=(str(task_id), deps), daemon=True,
            name=f"dp-linear-checkpoint-{str(task_id)[-12:]}",
        )
        _active[str(task_id)] = (None, thread, None)
        thread.start()


def recover(deps) -> None:
    for task_id in metadb.recoverable_linear_checkpoint_task_ids():
        dispatch(task_id, deps)


def request_cancel(task_id: str) -> None:
    with _active_lock:
        active = _active.get(str(task_id))
    if active is not None and active[0] is not None:
        key = active[2] or str(task_id)
        try:
            active[0].cancel(key)
        except KeyError:
            pass


def graph_prefix_sha256(graph: Graph, select_id: str) -> str:
    prefix = _prefix_graph(graph, select_id)
    payload = json.dumps(
        prefix.model_dump(by_alias=True, mode="json"),
        sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def checkpoint_identity(task_id: str, select_id: str, port_id: str) -> str:
    digest = hashlib.sha256(
        f"linear-checkpoint-id-v1\0{task_id}\0{select_id}\0{port_id}".encode()
    ).hexdigest()
    return f"cp:{digest}"
