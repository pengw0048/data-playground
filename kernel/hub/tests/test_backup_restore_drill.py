"""OPS-03 backup / isolated-restore drill — fixture backup, restore, isolation, RPO/RTO evidence."""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine.url import make_url

from hub import bounded_fanout, external_wait_tasks, handoff, linear_checkpoint, metadb
from hub.execution_manifest import build_execution_manifest
from hub.external_wait import ExternalWaitCheckpoint, ExternalWaitPollOutcome
from hub.local_run_inputs import finalize_local_file_candidates, snapshot_local_file_input
from hub.main import app
from hub.models import (
    CatalogTable,
    Graph,
    LineagePublication,
    RunOutput,
    RunStatus,
    WriteIntent,
)
from hub.plugins.adapters import (
    DuckDBAdapter, LanceAdapter, LocalFileInputRevisionAdapter,
    ManagedLocalFileRevisionAdapter, RevisionUnavailable,
)
from hub.plugins.catalog import InMemoryCatalog
from hub.storage import LocalStorage
from hub.tests.task_manifest_helpers import task_manifest_deps, with_task_manifest


def _switch_db(url: str) -> None:
    from hub.settings import settings

    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = url
    metadb._engine = metadb._Session = None


def _close_db() -> None:
    """Dispose engines and checkpoint SQLite so on-disk copies are consistent."""
    if metadb._engine is not None:
        if metadb._engine.dialect.name == "sqlite":
            with metadb._engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
        metadb._engine.dispose()
    metadb._engine = metadb._Session = None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = _file_sha256(path)
    return out


def _check_execution_manifest_owner(
        mismatches: list[dict[str, object]], *, subject: str,
        actual_sha256: str | None, expected_sha256: str,
        expected_schema_version: int, expected_document: dict) -> None:
    """Classify one restored owner and resolve it through the validating manifest read path."""
    if actual_sha256 is None:
        mismatches.append({
            "subject": subject,
            "code": "execution_manifest_owner_pruned",
            "expected": expected_sha256,
            "actual": None,
        })
        return
    if actual_sha256 != expected_sha256:
        mismatches.append({
            "subject": subject,
            "code": "execution_manifest_reference_mismatch",
            "expected": expected_sha256,
            "actual": actual_sha256,
        })
        return
    try:
        restored = metadb.execution_manifest(actual_sha256)
    except Exception as exc:  # noqa: BLE001 - classify corrupt backup evidence precisely
        mismatches.append({
            "subject": subject,
            "code": "execution_manifest_document_corrupt",
            "expected": expected_sha256,
            "actual": f"{type(exc).__name__}: {exc}",
        })
        return
    if restored is None:
        mismatches.append({
            "subject": subject,
            "code": "execution_manifest_document_missing",
            "expected": expected_sha256,
            "actual": None,
        })
        return
    if restored["schema_version"] != expected_schema_version:
        mismatches.append({
            "subject": subject,
            "code": "execution_manifest_document_corrupt",
            "expected": expected_schema_version,
            "actual": restored["schema_version"],
        })
        return
    if restored["document"] != expected_document:
        mismatches.append({
            "subject": subject,
            "code": "execution_manifest_document_mismatch",
            "expected": expected_document,
            "actual": restored["document"],
        })


def _execution_manifest_expectation(sha256: str) -> dict:
    stored = metadb.execution_manifest(sha256)
    assert stored is not None
    return {
        "sha256": sha256,
        "schema_version": stored["schema_version"],
        "document": stored["document"],
    }


@contextmanager
def _mount_restored_outputs_at_exact_path(source_root: Path, restored_root: Path):
    """Model the documented isolated-container mount without reading source artifact bytes."""
    parked = source_root.with_name(f"{source_root.name}.source-{uuid.uuid4().hex}")
    source_root.rename(parked)
    try:
        shutil.copytree(restored_root, source_root)
        yield
    finally:
        if source_root.exists():
            shutil.rmtree(source_root)
        parked.rename(source_root)


class _ClaimProvider:
    """Records namespace marker bodies so the drill can prove the source marker is untouched."""

    conditional_namespace_claims = True
    complete_inventory = True

    def __init__(self):
        self.claims: dict[tuple[str, str], dict] = {}
        self._counter = 0

    def inventory(self, _uri: str) -> list[dict]:
        return []

    def delete_exact(self, _uri: str, _member: dict) -> None:
        return None

    def read_namespace_claim(self, uri: str, namespace: str):
        return self.claims.get((urlsplit(uri).netloc, namespace))

    def write_namespace_claim(self, uri: str, namespace: str, body: bytes,
                              expected_etag: str | None):
        key = (urlsplit(uri).netloc, namespace)
        current = self.claims.get(key)
        if (current is None and expected_etag is not None) or (
                current is not None and current["etag"] != expected_etag):
            raise handoff.NamespaceClaimConflict("claim conflict")
        if current is not None and expected_etag is None:
            raise handoff.NamespaceClaimConflict("claim conflict")
        self._counter += 1
        etag = f'"claim-{self._counter}"'
        self.claims[key] = {"doc": json.loads(body), "body": body, "etag": etag}
        return etag


def _lineage(key: str, *, mappings: list[dict] | None = None) -> dict:
    return {
        "idempotency_key": key,
        "run_id": None,
        "attempt_id": None,
        "producer": "backup-restore-drill",
        "producer_version": 1,
        "step_id": None,
        "provenance": "manual",
        "field_mappings": mappings or [],
    }


def _lineage_receipt_identity(session, publication_key: str) -> dict:
    receipt = session.get(metadb.CatalogPublicationEvent, publication_key)
    assert receipt is not None
    return {
        "event_key": receipt.event_key,
        "effect_type": receipt.effect_type,
        "uri": receipt.uri,
        "version": receipt.version,
        "fingerprint": receipt.fingerprint,
    }


def _attempt_inventory(handle: dict) -> list[dict]:
    parsed = urlsplit(handle["uri"])
    root = f"{parsed.netloc}/{parsed.path.lstrip('/')}"
    return [
        {"member_id": handoff._member_id(
            "unversioned_object", f"{root}/part-00000.parquet", "null"),
         "key": f"{root}/part-00000.parquet",
         "member_type": "unversioned_object",
         "size": 10, "etag": "data", "version_id": None, "upload_id": None,
         "is_latest": True, "is_commit": False},
        {"member_id": handoff._member_id(
            "unversioned_object", handoff._object_manifest_path(root), "null"),
         "key": handoff._object_manifest_path(root),
         "member_type": "unversioned_object",
         "size": 20, "etag": "commit", "version_id": None, "upload_id": None,
         "is_latest": True, "is_commit": True},
    ]


def _publish_core_revision(
        storage: LocalStorage, catalog: InMemoryCatalog, logical_uri: str, value: int,
        *, source: dict | None = None,
) -> dict:
    run_id = f"backup-revision-{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"backup-revision:{logical_uri}", run_id)
    pq.write_table(pa.table({"value": [value]}), artifact)
    storage.commit_result(artifact, run_id)
    try:
        lineage = None
        parents = None
        if source is not None:
            parents = [source["uri"]]
            lineage = LineagePublication.model_validate(_lineage(
                f"backup-revision-lineage-{value}-{uuid.uuid4().hex}",
                mappings=[{
                    "source_dataset_id": source["registrationId"],
                    "source_version": source["version"],
                    "source_field": "id",
                    "source_field_id": None,
                    "destination_field": "value",
                }],
            ))
        published = catalog.publish_managed_local_file_output(
            name="backup_revision",
            logical_uri=logical_uri,
            artifact_uri=artifact,
            parents=parents,
            lineage=lineage,
        )
    except Exception:
        storage.abort_result(artifact, run_id)
        raise
    assert storage.release_result(artifact, run_id) is True
    return {
        "dataset_id": published["dataset_id"],
        "revision_id": published["revision_id"],
        "artifact_uri": artifact,
        "artifact_rel": str(Path(artifact).resolve().relative_to(Path(storage.root).resolve())),
        "artifact_sha256": _file_sha256(Path(artifact)),
        "value": value,
    }


def _register_provider_revision(path: Path, name: str, value: int) -> dict:
    lance = pytest.importorskip("lance")
    lance.write_dataset(pa.table({"value": [value]}), str(path))
    table = CatalogTable(id=f"tbl_{uuid.uuid4().hex}", name=name, uri=str(path), columns=[])
    assert metadb.catalog_upsert_entry(
        str(path), name, table.model_dump(by_alias=True)) is True
    binding = metadb.catalog_revision_binding_for_uri(str(path))
    assert binding is not None
    revision = LanceAdapter().resolve_revision(str(path))
    return {
        "dataset_id": binding["dataset_id"],
        "revision_id": revision["revision_id"],
        "uri": str(path),
        "value": value,
    }


def _exact_source_node(node_id: str, ref: dict) -> dict:
    return {
        "id": node_id,
        "type": "source",
        "position": {"x": 0, "y": 0},
        "data": {"config": {
            "uri": ref.get("artifact_uri", ref.get("uri")),
            "datasetRef": {
                "kind": "exact",
                "datasetId": ref["dataset_id"],
                "revisionId": ref["revision_id"],
            },
        }},
    }


def _parquet_bytes(rows: int) -> bytes:
    sink = io.BytesIO()
    pq.write_table(pa.table({
        "id": list(range(rows)),
        "label": [f"row-{index}" for index in range(rows)],
    }), sink)
    return sink.getvalue()


def _task_write_intent(task_id: str, canvas_id: str, logical_uri: str) -> dict:
    key = f"write:{task_id}"
    return {
        "destination": {
            "logicalUri": logical_uri,
            "name": "durable_result",
            "provider": "managed-local-file",
        },
        "mode": "create",
        "expectedSchema": [{"name": "value", "type": "int"}],
        "idempotencyKey": key,
        "partitions": [],
        "provenance": {
            "publication": {
                "idempotencyKey": key,
                "runId": task_id,
                "producer": canvas_id,
                "producerVersion": 1,
                "stepId": "write",
                "provenance": "run",
                "fieldMappings": [],
            },
            "parents": [],
        },
    }


def _finish_task(
        storage: LocalStorage, catalog: InMemoryCatalog, *, task_id: str,
        attempt_id: str, owner_token: str, intent: dict) -> dict:
    from hub.local_writes import write_managed_local_file

    receipt = write_managed_local_file(
        storage=storage,
        catalog=catalog,
        intent=WriteIntent.model_validate(intent),
        write_artifact=lambda uri: pq.write_table(pa.table({"value": [1]}), uri),
    )
    status = RunStatus(
        run_id=task_id,
        status="done",
        target_node_id="write",
        total_rows=1,
        outputs=[RunOutput(
            node_id="write",
            port_id="out",
            wire="dataset",
            publication_kind="catalog",
            outcome="committed",
            uri=receipt.publication.artifact_uri,
            table="durable_result",
            version=receipt.publication.catalog_version,
            rows=1,
            write_receipt=receipt,
        )],
    ).model_dump()
    assert metadb.finish_durable_task_attempt(
        task_id, attempt_id, owner_token, status)
    return receipt.model_dump(by_alias=True, mode="json")


def _checkpoint_admission(
        *, canvas_id: str, submission_id: str, task_kind: str,
        input_manifest: list[dict], logical_uri: str) -> dict:
    task_id = metadb.durable_task_submission_id(
        metadb.DEFAULT_USER_ID, canvas_id, submission_id)
    graph = {
        "id": canvas_id,
        "version": 1,
        "nodes": [
            {"id": "checkpoint", "type": "write", "data": {
                "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
            {"id": "write", "type": "write", "data": {
                "title": "durable result", "config": {"filename": "result.parquet"}}},
        ],
        "edges": [{
            "id": "checkpoint-write",
            "source": "checkpoint",
            "target": "write",
            "sourceHandle": "out",
            "targetHandle": "in",
        }],
    }
    manifest_sha = hashlib.sha256(json.dumps(
        input_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    admission, created = metadb.submit_linear_checkpoint_task(**with_task_manifest(dict(
        uid=metadb.DEFAULT_USER_ID,
        canvas_id=canvas_id,
        submission_id=submission_id,
        final_target_node_id="write",
        checkpoint_id=f"checkpoint:{uuid.uuid4().hex}",
        checkpoint_node_id="checkpoint",
        output_port_id="out",
        task_intent_sha256=hashlib.sha256(task_id.encode()).hexdigest(),
        graph_prefix_sha256=hashlib.sha256(
            json.dumps(graph, sort_keys=True).encode()).hexdigest(),
        input_manifest_sha256=manifest_sha,
        graph_doc=graph,
        input_manifest=input_manifest,
        write_intent=_task_write_intent(task_id, canvas_id, logical_uri),
        task_kind=task_kind,
    ), target_key="final_target_node_id"))
    assert created is True
    return admission


def _seed_checkpoint(
        storage: LocalStorage, *, canvas_id: str, task_kind: str,
        owner_token: str, rows: int, logical_uri: str) -> dict:
    admission = _checkpoint_admission(
        canvas_id=canvas_id,
        submission_id=str(uuid.uuid4()),
        task_kind=task_kind,
        input_manifest=[],
        logical_uri=logical_uri,
    )
    claim_fn = (
        metadb.claim_linear_checkpoint_task
        if task_kind == "linear_checkpoint_write"
        else metadb.claim_bounded_fanout_write_task
    )
    task = claim_fn(admission["task_id"], owner_token)
    assert task is not None
    attempt = task["attempts"][-1]
    candidate = metadb.reserve_linear_checkpoint_candidate(
        task_id=admission["task_id"],
        attempt_id=attempt["id"],
        owner_token=owner_token,
        namespace_id=storage.namespace_id,
        storage_root=storage.result_root,
        writer_token=uuid.uuid4().hex,
        lock_token=uuid.uuid4().hex,
    )
    evidence = linear_checkpoint.materialize_and_commit_checkpoint(
        storage,
        task_id=admission["task_id"],
        attempt_id=attempt["id"],
        owner_token=owner_token,
        candidate=candidate,
        content=_parquet_bytes(rows),
    )
    return {
        "admission": admission,
        "attempt": attempt,
        "candidate": candidate,
        "evidence": evidence,
        "owner_token": owner_token,
    }


class _ReattachAdapter:
    provider_kind = "backup-drill"

    def __init__(self):
        self.submit_calls = 0
        self.status_calls = 0
        self.seen_handles: list[str] = []

    def submit(self, _request):
        self.submit_calls += 1
        raise AssertionError("restored external wait must not resubmit")

    def status(self, handle, checkpoint=None):
        self.status_calls += 1
        self.seen_handles.append(handle.job_id)
        sequence = checkpoint.sequence + 1 if checkpoint else 1
        return ExternalWaitPollOutcome(
            phase="running",
            checkpoint=ExternalWaitCheckpoint(
                sequence=sequence, token=f"restore-{sequence}"),
            retry={"after_seconds": 1.0},
        )

    def cancel(self, _handle, checkpoint=None):
        raise AssertionError(f"unexpected cancel at {checkpoint}")


def _seed_durable_tasks(
        workspace: Path, storage: LocalStorage, catalog: InMemoryCatalog, *, canvas_id: str,
        ordinary_input: dict) -> dict:
    manifest = [{
        "node_id": "ordinary",
        "dataset_id": ordinary_input["dataset_id"],
        "revision_id": ordinary_input["revision_id"],
        "provider": "local-file-snapshot",
        "resolved_at": "2026-07-18T00:00:00+00:00",
    }]
    logical_root = workspace / "data"

    managed_submission = str(uuid.uuid4())
    managed_task_id = metadb.durable_task_submission_id(
        metadb.DEFAULT_USER_ID, canvas_id, managed_submission)
    managed_graph = {
        "id": canvas_id,
        "version": 1,
        "nodes": [
            _exact_source_node("ordinary", ordinary_input),
            {"id": "write", "type": "write", "data": {
                "config": {"filename": "managed-task.parquet"}}},
        ],
        "edges": [{"id": "ordinary-write", "source": "ordinary", "target": "write"}],
    }
    managed, created = metadb.submit_durable_local_write_task(**with_task_manifest(dict(
        uid=metadb.DEFAULT_USER_ID,
        canvas_id=canvas_id,
        submission_id=managed_submission,
        target_node_id="write",
        intent_sha256=hashlib.sha256(managed_task_id.encode()).hexdigest(),
        graph_doc=managed_graph,
        input_manifest=manifest,
        write_intent=_task_write_intent(
            managed_task_id, canvas_id, str(logical_root / "managed-task.parquet")),
    )))
    assert created is True
    managed_claim = metadb.claim_durable_task(managed["id"], "backup-managed-owner")
    assert managed_claim is not None
    managed_attempt = managed_claim["attempts"][-1]
    managed_receipt = _finish_task(
        storage, catalog, task_id=managed["id"], attempt_id=managed_attempt["id"],
        owner_token="backup-managed-owner",
        intent=_task_write_intent(
            managed_task_id, canvas_id, str(logical_root / "managed-task.parquet")),
    )

    checkpoint = _seed_checkpoint(
        storage,
        canvas_id=canvas_id,
        task_kind="linear_checkpoint_write",
        owner_token="backup-checkpoint-owner",
        rows=3,
        logical_uri=str(logical_root / "checkpoint-task.parquet"),
    )
    checkpoint_receipt = _finish_task(
        storage, catalog,
        task_id=checkpoint["admission"]["task_id"],
        attempt_id=checkpoint["attempt"]["id"],
        owner_token=checkpoint["owner_token"],
        intent=checkpoint["admission"]["write_intent"],
    )

    fanout = _seed_checkpoint(
        storage,
        canvas_id=canvas_id,
        task_kind="bounded_fanout_write",
        owner_token="backup-fanout-owner",
        rows=5,
        logical_uri=str(logical_root / "fanout-task.parquet"),
    )
    plan = bounded_fanout.create_or_reopen_plan(
        parent_task_id=fanout["admission"]["task_id"],
        parent_attempt_id=fanout["attempt"]["id"],
        owner_token=fanout["owner_token"],
    )
    fanout_artifacts = []
    for unit in [item for item in plan["units"] if item["kind"] == "child"]:
        claim = bounded_fanout.claim_unit(
            parent_task_id=fanout["admission"]["task_id"],
            unit_id=unit["unit_id"],
            parent_attempt_id=fanout["attempt"]["id"],
            owner_token=fanout["owner_token"],
        )
        candidate = bounded_fanout.reserve_unit_artifact(
            storage, attempt_id=claim["attempt_id"])
        plan = bounded_fanout.commit_unit_evidence(
            storage,
            attempt_id=claim["attempt_id"],
            claim_token=claim["claim_token"],
            owner_token=fanout["owner_token"],
            candidate=candidate,
            content=_parquet_bytes(unit["range_end"] - unit["range_start"]),
        )
        fanout_artifacts.append(candidate["uri"])
    gather = next(item for item in plan["units"] if item["kind"] == "gather")
    gather_claim = bounded_fanout.claim_unit(
        parent_task_id=fanout["admission"]["task_id"],
        unit_id=gather["unit_id"],
        parent_attempt_id=fanout["attempt"]["id"],
        owner_token=fanout["owner_token"],
    )
    gather_candidate = bounded_fanout.reserve_unit_artifact(
        storage, attempt_id=gather_claim["attempt_id"])
    plan = bounded_fanout.commit_unit_evidence(
        storage,
        attempt_id=gather_claim["attempt_id"],
        claim_token=gather_claim["claim_token"],
        owner_token=fanout["owner_token"],
        candidate=gather_candidate,
        content=_parquet_bytes(5),
    )
    fanout_artifacts.append(gather_candidate["uri"])
    assert all(unit["status"] == "done" for unit in plan["units"])
    fanout_receipt = _finish_task(
        storage, catalog,
        task_id=fanout["admission"]["task_id"],
        attempt_id=fanout["attempt"]["id"],
        owner_token=fanout["owner_token"],
        intent=fanout["admission"]["write_intent"],
    )

    external_submission = str(uuid.uuid4())
    external_task_id = metadb.durable_task_submission_id(
        metadb.DEFAULT_USER_ID, canvas_id, external_submission)
    external_graph = {
        "id": canvas_id,
        "version": 1,
        "nodes": [
            {"id": "wait", "type": "external_wait_fixture", "data": {"config": {
                "operation": "backup.restore",
                "documentJson": "{}",
                "outputSchema": [{"name": "value", "type": "int"}],
            }}},
            {"id": "write", "type": "write", "data": {"config": {
                "destination": str(logical_root / "external-task.parquet"),
                "mode": "create",
            }}},
        ],
        "edges": [{"id": "wait-write", "source": "wait", "target": "write"}],
    }
    external, created = metadb.submit_durable_external_wait_task(**with_task_manifest(dict(
        uid=metadb.DEFAULT_USER_ID,
        canvas_id=canvas_id,
        submission_id=external_submission,
        target_node_id="write",
        intent_sha256=hashlib.sha256(external_task_id.encode()).hexdigest(),
        graph_doc=external_graph,
        provider_kind="backup-drill",
        operation="backup.restore",
        document_json="{}",
        write_intent=_task_write_intent(
            external_task_id, canvas_id, str(logical_root / "external-task.parquet")),
    )))
    assert created is True
    submit_claim = metadb.claim_external_wait_transition(
        external["id"], "backup-external-submit")
    assert submit_claim is not None
    handle = {"provider_kind": "backup-drill", "job_id": f"job-{uuid.uuid4().hex}"}
    assert metadb.commit_external_wait_transition(
        external["id"], submit_claim["attempt_id"], "backup-external-submit",
        handle=handle,
    )
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, external["id"], with_for_update=True)
        wait.next_poll_at = metadb._now() - datetime.timedelta(seconds=1)
        wait.deadline_at = metadb._now() + datetime.timedelta(days=1)
    poll_claim = metadb.claim_external_wait_transition(
        external["id"], "backup-external-poll")
    assert poll_claim is not None
    checkpoint_doc = {"sequence": 7, "token": "before-backup"}
    assert metadb.commit_external_wait_transition(
        external["id"], poll_claim["attempt_id"], "backup-external-poll",
        outcome={
            "phase": "running",
            "checkpoint": checkpoint_doc,
            "retry": {"after_seconds": 1.0},
            "diagnostic": None,
        },
    )
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, external["id"], with_for_update=True)
        wait.deadline_at = metadb._now() + datetime.timedelta(days=1)

    inbox = metadb.list_durable_task_inbox_items(metadb.DEFAULT_USER_ID, limit=200)["items"]
    task_ids = {
        managed["id"], checkpoint["admission"]["task_id"],
        fanout["admission"]["task_id"],
    }
    terminal_inbox = {item["task_id"]: item for item in inbox if item["task_id"] in task_ids}
    assert set(terminal_inbox) == task_ids
    read_item = metadb.mark_durable_task_inbox_item_read(
        metadb.DEFAULT_USER_ID, terminal_inbox[managed["id"]]["id"])
    assert read_item is not None and read_item["read_at"] is not None
    assert not any(item["task_id"] == external["id"] for item in inbox)

    artifact_uris = [
        managed_receipt["publication"]["artifactUri"],
        checkpoint_receipt["publication"]["artifactUri"], checkpoint["candidate"]["uri"],
        fanout_receipt["publication"]["artifactUri"], fanout["candidate"]["uri"],
        *fanout_artifacts,
    ]
    return {
        "managed": {
            "task_id": managed["id"],
            "attempt_id": managed_attempt["id"],
            "manifest": _execution_manifest_expectation(
                managed["execution_manifest_sha256"]),
            "input_manifest": managed["input_manifest"],
            "input_artifact_uri": ordinary_input["artifact_uri"],
            "receipt": managed_receipt,
        },
        "checkpoint": {
            "task_id": checkpoint["admission"]["task_id"],
            "attempt_id": checkpoint["attempt"]["id"],
            "manifest": _execution_manifest_expectation(
                metadb.durable_task(
                    checkpoint["admission"]["task_id"])["execution_manifest_sha256"]),
            "receipt": checkpoint_receipt,
        },
        "fanout": {
            "task_id": fanout["admission"]["task_id"],
            "attempt_id": fanout["attempt"]["id"],
            "manifest": _execution_manifest_expectation(
                metadb.durable_task(
                    fanout["admission"]["task_id"])["execution_manifest_sha256"]),
            "plan_digest": plan["plan_digest"],
            "unit_count": len(plan["units"]),
            "old_unit_attempt_id": gather_claim["attempt_id"],
            "old_unit_claim_token": gather_claim["claim_token"],
            "receipt": fanout_receipt,
        },
        "external": {
            "task_id": external["id"],
            "attempt_id": poll_claim["attempt_id"],
            "manifest": _execution_manifest_expectation(
                external["execution_manifest_sha256"]),
            "handle": handle,
            "checkpoint": checkpoint_doc,
        },
        "inbox": {
            task_id: {
                "id": terminal_inbox[task_id]["id"],
                "read": task_id == managed["id"],
            }
            for task_id in task_ids
        },
        "artifacts": [{
            "uri": uri,
            "rel": str(Path(uri).resolve().relative_to(Path(storage.root).resolve())),
            "sha256": _file_sha256(Path(uri)),
        } for uri in artifact_uris],
    }


def _seed_overlay_recovery_fixture() -> dict:
    """Seed only durable overlay metadata; no provider bytes or configuration are retained."""
    root = metadb.local_workspace_root()
    overlays: dict[str, dict] = {}
    for label, state in (("active", "current"), ("detached", "detached")):
        binding = metadb.workspace_provider_cache_resource(
            mount_id="backup-drill-overlay",
            provider="backup-drill-provider",
            container_id=root["id"],
            provider_placement_id=f"overlay-{label}",
            kind="container",
            name=f"Backup {label} overlay",
        )
        anchor = metadb.workspace_provider_ensure_overlay_anchor(binding["bindingId"])
        request_id = f"backup-drill-overlay-{label}"
        intent = {
            "containerId": anchor["containerId"],
            "expectedContainerVersion": anchor["containerVersion"],
            "name": f"Backup {label} overlay Canvas",
            "datasetIds": [],
            "providerDatasetRefs": [],
            "transform": None,
        }
        created = metadb.workspace_create_canvas_action(
            uid=metadb.DEFAULT_USER_ID,
            container_id=anchor["containerId"],
            expected_container_version=anchor["containerVersion"],
            name=intent["name"],
            request_id=request_id,
            request_intent=intent,
        )
        if state == "detached":
            binding = metadb.workspace_provider_mark_binding(
                binding["bindingId"], state="detached", error="fixture resource removed")
        overlays[label] = {
            "binding_id": binding["bindingId"],
            "binding_state": state,
            "anchor": anchor,
            "canvas_id": created["id"],
            "placement_id": created["resource"]["placementId"],
            "request_id": request_id,
            "intent": intent,
            "replay": created,
        }
    return overlays


def _seed_provider_canonical_recovery_fixture() -> dict:
    """Seed one shared canonical dataset and two independently recoverable occurrences."""
    root = metadb.local_workspace_root()
    common = {
        "mount_id": "backup-drill-canonical",
        "provider": "backup-drill-provider",
        "container_id": root["id"],
        "kind": "dataset",
        "provider_dataset_id": "shared-dataset",
        "uri": "s3://provider-owned/shared-dataset.parquet",
        "columns": [{"name": "value", "type": "int64"}],
    }
    left = metadb.workspace_provider_cache_resource(
        **common,
        provider_placement_id="shared-dataset-left",
        name="Shared dataset left",
    )
    right = metadb.workspace_provider_cache_resource(
        **common,
        provider_placement_id="shared-dataset-right",
        name="Shared dataset right",
    )
    left = metadb.workspace_provider_mark_binding(
        left["bindingId"], state="detached", error="fixture occurrence removed")
    canonical = metadb.workspace_provider_dataset(
        mount_id=common["mount_id"],
        provider_dataset_id=common["provider_dataset_id"],
    )
    assert canonical is not None
    source_binding = metadb.workspace_provider_source_binding(right["bindingId"])
    assert source_binding is not None
    return {
        "mount_id": common["mount_id"],
        "provider_dataset_id": common["provider_dataset_id"],
        "source_binding_id": source_binding["sourceBindingId"],
        "uri": common["uri"],
        "columns": canonical["columns"],
        "left_binding_id": left["bindingId"],
        "right_binding_id": right["bindingId"],
    }


def _seed_fixture(workspace: Path, storage: LocalStorage, *, claim_uri: str,
                  provider: _ClaimProvider) -> dict:
    metadb.init_db()
    namespace = metadb.object_storage_namespace()
    owner = metadb.object_attempt_owner_id()
    alembic_head = metadb.expected_schema_head()
    assert metadb.require_schema_at_head() == alembic_head
    release_sha = os.environ.get("DP_GIT_SHA", "").strip() or "drill-fixture"
    release_version = package_version("data-playground")

    canvas_id = f"drill-canvas-{uuid.uuid4().hex}"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    parent_path = data_dir / "parent.parquet"
    child_path = data_dir / "child.parquet"
    parent_path.write_bytes(b"PAR1-parent")
    child_path.write_bytes(b"PAR1-child")
    parent_uri = f"file://{parent_path.resolve()}"
    child_uri = f"file://{child_path.resolve()}"

    with metadb.session() as session:
        if session.get(metadb.User, metadb.DEFAULT_USER_ID) is None:
            session.add(metadb.User(id=metadb.DEFAULT_USER_ID, name="Local", is_admin=True))
        session.add(metadb.Canvas(
            id=canvas_id, owner_id=metadb.DEFAULT_USER_ID,
            name="backup-restore-drill", version=1,
            doc=json.dumps({"id": canvas_id, "nodes": [{"id": "n1", "type": "load"}]}),
        ))

    workspace_container = metadb.workspace_create_container(
        metadb.local_workspace_root()["id"], "Backup restore drill")
    workspace_placement = metadb.workspace_create_placement(
        workspace_container["id"], target_kind="canvas", target_id=canvas_id,
        name="backup-restore-drill")
    overlays = _seed_overlay_recovery_fixture()
    provider_canonical = _seed_provider_canonical_recovery_fixture()

    metadb.catalog_upsert_entry(
        parent_uri, "drill-parent",
        {"id": "drill_parent", "name": "drill-parent", "uri": parent_uri,
         "version": "parent-v1",
         "columns": [{"name": "id", "type": "int64"}]})
    parent = metadb.catalog_get(parent_uri)
    assert parent is not None
    lineage_idempotency_key = f"backup-restore-drill-{uuid.uuid4().hex}"
    lineage_publication_key = (
        "lineage-publication:v1:sha256:"
        + hashlib.sha256(lineage_idempotency_key.encode("utf-8")).hexdigest()
    )
    metadb.catalog_upsert_entry(
        child_uri, "drill-child",
        {"id": "drill_child", "name": "drill-child", "uri": child_uri,
         "version": "child-v1",
         "columns": [{"name": "id", "type": "int64"}]},
        parents=[parent_uri], pipeline="backup-restore-drill",
        lineage=_lineage(
            lineage_idempotency_key,
            mappings=[{
                "source_dataset_id": parent["registrationId"],
                "source_version": "parent-v1",
                "source_field": "id",
                "source_field_id": None,
                "destination_field": "id",
            }],
        ))
    child = metadb.catalog_get(child_uri)
    assert child is not None
    with metadb.session() as session:
        lineage_receipt = _lineage_receipt_identity(session, lineage_publication_key)
    assert lineage_receipt["effect_type"] == "lineage"
    assert lineage_receipt["uri"] == child_uri
    assert lineage_receipt["version"] == "child-v1"
    assert lineage_receipt["fingerprint"].startswith("lineage-publication:v3:sha256:")

    revision_catalog = InMemoryCatalog(str(data_dir), lambda _uri: DuckDBAdapter())
    managed_logical_uri = str(data_dir / "managed-recovery.parquet")
    core_original = _publish_core_revision(
        storage, revision_catalog, managed_logical_uri, 101, source=parent)
    core_head = _publish_core_revision(
        storage, revision_catalog, managed_logical_uri, 202, source=parent)
    assert core_original["dataset_id"] == core_head["dataset_id"]

    ordinary_path = data_dir / "ordinary-input.parquet"
    pq.write_table(pa.table({"value": [606]}), ordinary_path)
    revision_catalog._add(
        name="ordinary-input", uri=str(ordinary_path), strict_probe=True)
    ordinary_binding = metadb.catalog_revision_binding_for_uri(str(ordinary_path))
    assert ordinary_binding is not None
    ordinary_revision_id, ordinary_candidate = snapshot_local_file_input(
        uri=str(ordinary_path), config={"uri": str(ordinary_path)},
        dataset_id=ordinary_binding["dataset_id"], adapter=DuckDBAdapter(), storage=storage)
    assert ordinary_candidate is not None
    ordinary_input = {
        "dataset_id": ordinary_binding["dataset_id"],
        "revision_id": ordinary_revision_id,
        "artifact_uri": ordinary_candidate["artifact_uri"],
        "artifact_rel": str(Path(ordinary_candidate["artifact_uri"]).resolve().relative_to(
            Path(storage.root).resolve())),
        "artifact_sha256": _file_sha256(Path(ordinary_candidate["artifact_uri"])),
        "source_uri": str(ordinary_path),
        "value": 606,
    }

    tombstone_logical_uri = str(data_dir / "unregistered-recovery.parquet")
    tombstone = _publish_core_revision(
        storage, revision_catalog, tombstone_logical_uri, 303)
    metadb.catalog_delete_entry(tombstone["artifact_uri"])

    provider_root = workspace / "provider-owned"
    provider_root.mkdir()
    provider_available = _register_provider_revision(
        provider_root / "available.lance", "provider-available", 404)
    provider_unavailable = _register_provider_revision(
        provider_root / "unavailable.lance", "provider-unavailable", 505)

    exact_refs = [core_original, provider_available, provider_unavailable]
    ordinary_submission_id = str(uuid.uuid4())
    ordinary_run_id = metadb.local_run_submission_id(
        metadb.DEFAULT_USER_ID, canvas_id, ordinary_submission_id)
    ordinary_output_uri = str(data_dir / "ordinary-manifest-output.parquet")
    ordinary_write_intent = _task_write_intent(
        ordinary_run_id, canvas_id, ordinary_output_uri)
    ordinary_write_intent["provenance"]["parents"] = [parent_uri]
    canvas_doc = {
        "id": canvas_id,
        "version": 1,
        "nodes": [
            _exact_source_node("core", core_original),
            _exact_source_node("provider-available", provider_available),
            _exact_source_node("provider-unavailable", provider_unavailable),
            {"id": "ordinary", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": ordinary_input["source_uri"]}}},
            {"id": "write", "type": "write", "data": {
                "config": {"filename": "ordinary-manifest-output.parquet"}}},
        ],
        "edges": [{"id": "ordinary-write", "source": "ordinary", "target": "write"}],
    }
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id, with_for_update=True)
        assert canvas is not None
        canvas.doc = json.dumps(canvas_doc)
        metadb.sync_local_result_owner(session, "canvas", canvas_id, canvas_doc)

    admitted_manifest = [
        {
            "node_id": node_id,
            "dataset_id": ref["dataset_id"],
            "revision_id": ref["revision_id"],
            "provider": provider_name,
            "resolved_at": "2026-07-16T00:00:00+00:00",
        }
        for node_id, provider_name, ref in (
            ("core", "managed-local-file", core_original),
            ("provider-available", "lance", provider_available),
            ("provider-unavailable", "lance", provider_unavailable),
            ("ordinary", "local-file-snapshot", ordinary_input),
        )
    ]
    ordinary_manifest_sha256, ordinary_manifest_doc = build_execution_manifest(
        Graph.model_validate(canvas_doc),
        target_node_id="write",
        target_port_id=None,
        input_manifest=admitted_manifest,
        write_intent=WriteIntent.model_validate(ordinary_write_intent),
        deps=task_manifest_deps(Graph.model_validate(canvas_doc)),
    )
    run_id, created = metadb.admit_local_run_inputs(
        uid=metadb.DEFAULT_USER_ID,
        canvas_id=canvas_id,
        submission_id=ordinary_submission_id,
        target_node_id="write",
        intent_sha256=ordinary_manifest_sha256,
        manifest=admitted_manifest,
        execution_manifest_sha256=ordinary_manifest_sha256,
        execution_manifest_doc=ordinary_manifest_doc,
        local_file_candidates=[ordinary_candidate],
    )
    assert created is True and run_id == ordinary_run_id
    finalize_local_file_candidates(storage, [ordinary_candidate], run_id)
    from hub.local_writes import write_managed_local_file

    ordinary_receipt = write_managed_local_file(
        storage=storage,
        catalog=revision_catalog,
        intent=WriteIntent.model_validate(ordinary_write_intent),
        write_artifact=lambda uri: pq.write_table(pa.table({"value": [707]}), uri),
    )
    assert ordinary_receipt.execution_manifest_sha256 == ordinary_manifest_sha256
    artifact_uri = storage.begin_result(f"plan-{uuid.uuid4().hex}", run_id)
    Path(artifact_uri).write_bytes(b"drill-local-result-bytes")
    storage.commit_result(artifact_uri, run_id)
    metadb.bind_run_owner(run_id, metadb.DEFAULT_USER_ID, canvas_id)
    run_output = RunOutput(
        node_id="write",
        port_id="out",
        wire="dataset",
        publication_kind="result",
        outcome="committed",
        uri=artifact_uri,
        rows=1,
    ).model_dump()
    metadb.save_run_state(
        run_id,
        {
            "run_id": run_id,
            "status": "done",
            "target_node_id": "write",
            "total_rows": 1,
            "outputs": [run_output],
        },
        canvas_id=canvas_id,
        execution_manifest_sha256=ordinary_manifest_sha256,
        execution_manifest_doc=ordinary_manifest_doc,
    )
    assert storage.release_result(artifact_uri, run_id) is True
    metadb.record_run(
        canvas_id, "write", "run", "done", rows=1, outputs=[run_output], run_id=run_id)
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, canvas_id, with_for_update=True)
        assert canvas is not None
        edited_canvas_doc = json.loads(canvas.doc)
        next(node for node in edited_canvas_doc["nodes"] if node["id"] == "write")[
            "data"]["config"]["filename"] = "edited-after-manifest.parquet"
        canvas.doc = json.dumps(edited_canvas_doc)
        metadb.sync_local_result_owner(session, "canvas", canvas_id, edited_canvas_doc)
    artifact_rel = str(Path(artifact_uri).resolve().relative_to(Path(storage.root).resolve()))

    object_store_cred = metadb.cred_upsert(
        f"drill-object-store-{uuid.uuid4().hex}",
        "Backup drill object store",
        "object_store",
        {
            "accessKeyId": "env:DP_BACKUP_DRILL_ACCESS_KEY",
            "secretAccessKey": "file:/run/secrets/dp-backup-drill-secret",
            "endpoint": "http://minio:9000",
        },
    )
    metadb.set_setting("defaultObjectStoreCredId", object_store_cred["id"])

    handoff.set_managed_object_provider(provider)
    handoff.ensure_storage_namespace_claim(claim_uri, namespace)
    logical = f"{claim_uri.rsplit('/', 1)[0]}/managed.parquet"
    managed = metadb.allocate_object_attempt(
        logical_uri=logical,
        kind="region",
        run_id=f"managed-{uuid.uuid4().hex}",
        allocation_key=f"managed-{uuid.uuid4().hex}",
        uri_factory=lambda ns, generation, attempt_id: handoff.physical_attempt_uri(
            logical, ns, generation, attempt_id),
        write_lease_seconds=30,
    )
    metadb.record_object_attempt_commit(managed["uri"], _attempt_inventory(managed))

    plugins = workspace / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "drill-note.txt").write_text("fixture plugin tree\n", encoding="utf-8")

    durable_tasks = _seed_durable_tasks(
        workspace, storage, revision_catalog,
        canvas_id=canvas_id, ordinary_input=ordinary_input)

    claim_key = (urlsplit(claim_uri).netloc, namespace)
    return {
        "namespace": namespace,
        "owner": owner,
        "alembic_head": alembic_head,
        "release_sha": release_sha,
        "release_version": release_version,
        "canvas_id": canvas_id,
        "workspace_container_id": workspace_container["id"],
        "workspace_placement_id": workspace_placement["id"],
        "overlays": overlays,
        "provider_canonical": provider_canonical,
        "parent_uri": parent_uri,
        "parent_dataset_id": parent["registrationId"],
        "child_uri": child_uri,
        "child_dataset_id": child["registrationId"],
        "lineage_publication_key": lineage_publication_key,
        "lineage_receipt": lineage_receipt,
        "run_id": run_id,
        "ordinary_execution_manifest": {
            **_execution_manifest_expectation(ordinary_manifest_sha256),
            "receipt_revision_id": ordinary_receipt.revision_id,
        },
        "admitted_manifest": admitted_manifest,
        "exact_refs": exact_refs,
        "core_original": core_original,
        "core_head": core_head,
        "tombstone": tombstone,
        "tombstone_logical_uri": tombstone_logical_uri,
        "provider_available": provider_available,
        "provider_unavailable": provider_unavailable,
        "ordinary_input": ordinary_input,
        "artifact_uri": artifact_uri,
        "artifact_rel": artifact_rel,
        "artifact_bytes": Path(artifact_uri).read_bytes(),
        "managed_uri": managed["uri"],
        "claim_uri": claim_uri,
        "claim_key": claim_key,
        "marker_body": provider.claims[claim_key]["body"],
        "cred_id": object_store_cred["id"],
        "secret_ref": "file:/run/secrets/dp-backup-drill-secret",
        "durable_tasks": durable_tasks,
    }


def _assert_fixture_readable(info: dict, *, outputs_root: Path) -> None:
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, info["canvas_id"])
        assert canvas is not None and canvas.name == "backup-restore-drill"
        assert [node["id"] for node in json.loads(canvas.doc)["nodes"]] == [
            "core", "provider-available", "provider-unavailable", "ordinary", "write",
        ]
        runs = list(session.scalars(
            select(metadb.RunRecord).where(metadb.RunRecord.canvas_id == info["canvas_id"])))
        assert any(
            row.run_id == info["run_id"]
            and row.target_node_id == "write"
            and json.loads(row.outputs)[0]["uri"] == info["artifact_uri"]
            and json.loads(row.input_manifest) == info["admitted_manifest"]
            for row in runs
        )
        state = session.get(metadb.RunState, info["run_id"])
        assert state is not None and state.status == "done"
        state_doc = json.loads(state.doc)
        assert state_doc["target_node_id"] == "write"
        assert state_doc["outputs"][0]["uri"] == info["artifact_uri"]
        assert not ({"output_uri", "output_table"} & state_doc.keys())
        artifact = session.get(metadb.LocalResultArtifact, info["artifact_uri"])
        assert artifact is not None and artifact.state == "ready"
        setting = session.scalar(select(metadb.Setting).where(
            metadb.Setting.key == "defaultObjectStoreCredId",
            metadb.Setting.scope == "global"))
        assert setting is not None and json.loads(setting.value) == info["cred_id"]
        assert session.scalar(select(metadb.Setting).where(
            metadb.Setting.key == "objectStore")) is None
        cred = session.get(metadb.CredEntity, info["cred_id"])
        assert cred is not None and cred.kind == "object_store"
        fields = json.loads(cred.fields_json)
        assert fields["secretAccessKey"] == info["secret_ref"]
        assert fields["accessKeyId"] == "env:DP_BACKUP_DRILL_ACCESS_KEY"
        container = session.get(metadb.WorkspaceContainer, info["workspace_container_id"])
        placement = session.get(metadb.WorkspacePlacement, info["workspace_placement_id"])
        assert container is not None and container.name == "Backup restore drill"
        assert placement is not None and placement.target_id == info["canvas_id"]

    assert metadb.catalog_get(info["parent_uri"]) is not None
    assert metadb.catalog_get(info["child_uri"]) is not None
    edges = metadb.catalog_lineage_pairs()
    assert any(
        edge["parent"] in (info["parent_uri"], "drill_parent")
        and edge["child"] in (info["child_uri"], "drill_child")
        and edge["fact_count"] == 1
        for edge in edges
    )
    with metadb.session() as session:
        fact = session.scalar(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.destination_uri == info["child_uri"]))
        assert fact is not None
        assert json.loads(fact.field_mappings_json) == [
            {
                "destination_field": "id",
                "source_dataset_id": info["parent_dataset_id"],
                "source_field": "id",
                "source_field_id": None,
                "source_version": "parent-v1",
            },
        ]
        projection = session.scalar(select(
            metadb.CatalogFieldLineageProjection).where(
                metadb.CatalogFieldLineageProjection.destination_dataset_id
                == info["child_dataset_id"]))
        assert projection is not None
        assert (
            projection.source_dataset_id,
            projection.source_version,
            projection.destination_revision_id,
            projection.destination_field,
        ) == (info["parent_dataset_id"], "parent-v1", "child-v1", "id")
        assert _lineage_receipt_identity(
            session, info["lineage_publication_key"]) == info["lineage_receipt"]
    restored_file = outputs_root / info["artifact_rel"]
    assert restored_file.is_file()
    assert restored_file.read_bytes() == info["artifact_bytes"]


def _assert_overlay_recovery(info: dict) -> None:
    """The backup retains only local overlay identities, not provider bytes or configuration."""
    observed_states: dict[str, dict[str, str]] = {}
    for label, expected in info["overlays"].items():
        binding = metadb.workspace_provider_binding(expected["binding_id"])
        assert binding is not None
        assert binding["referenceState"] == expected["binding_state"]
        assert binding["mountId"] == "backup-drill-overlay"
        assert binding["resourceId"] == f"overlay-{label}"
        serialized = json.dumps(binding, sort_keys=True, default=str)
        assert "config" not in serialized.lower()
        assert "uri" not in serialized.lower()

        anchor = metadb.workspace_provider_overlay_anchor(expected["binding_id"])
        assert anchor == expected["anchor"]
        replay = metadb.workspace_canvas_create_replay(
            uid=metadb.DEFAULT_USER_ID,
            request_id=expected["request_id"],
            intent=expected["intent"],
        )
        assert replay == expected["replay"]
        with metadb.session() as session:
            canvas = session.get(metadb.Canvas, expected["canvas_id"])
            placement = session.get(metadb.WorkspacePlacement, expected["placement_id"])
            assert canvas is not None and canvas.name == expected["intent"]["name"]
            assert placement is not None
            assert placement.target_id == expected["canvas_id"]
            assert placement.container_id == expected["anchor"]["containerId"]

        # Canvas reopen stays local.  The resource deep link also survives without restoring an
        # external provider mount; its provider source may honestly report unavailable.
        with TestClient(app) as client:
            reopened = client.get(f"/api/canvas/{expected['canvas_id']}")
            assert reopened.status_code == 200, reopened.text
            deep_link = client.get(f"/api/workspace/resources/canvas:{expected['canvas_id']}")
            assert deep_link.status_code == 200, deep_link.text
            assert deep_link.json()["resource"]["placementId"] == expected["placement_id"]
        reopened_binding = metadb.workspace_provider_binding(expected["binding_id"])
        assert reopened_binding is not None
        if label == "detached":
            assert reopened_binding["referenceState"] == "detached"
        observed_states[label] = {
            "restored_state": binding["referenceState"],
            "post_reopen_state": reopened_binding["referenceState"],
        }
    canonical_expected = info["provider_canonical"]
    left = metadb.workspace_provider_binding(canonical_expected["left_binding_id"])
    right = metadb.workspace_provider_binding(canonical_expected["right_binding_id"])
    canonical = metadb.workspace_provider_dataset(
        mount_id=canonical_expected["mount_id"],
        provider_dataset_id=canonical_expected["provider_dataset_id"],
    )
    assert left is not None and left["referenceState"] == "detached"
    assert right is not None and right["referenceState"] == "current"
    assert left["providerDatasetId"] == right["providerDatasetId"] == (
        canonical_expected["provider_dataset_id"])
    assert canonical is not None
    assert canonical["referenceState"] == "current"
    assert canonical["sourceBindingId"] == canonical_expected["source_binding_id"]
    assert canonical["uri"] == canonical_expected["uri"]
    assert canonical["columns"] == canonical_expected["columns"]
    assert metadb.workspace_provider_source_binding(
        canonical_expected["right_binding_id"]
    ) == {"sourceBindingId": canonical_expected["source_binding_id"]}
    serialized_binding = json.dumps({
        "sourceBindingId": canonical_expected["source_binding_id"],
    })
    assert canonical_expected["provider_dataset_id"] not in serialized_binding
    assert canonical_expected["uri"] not in serialized_binding
    assert canonical_expected["left_binding_id"] not in serialized_binding
    assert canonical_expected["right_binding_id"] not in serialized_binding
    info["overlay_recovery_evidence"] = observed_states


def _assert_revision_recovery(
        info: dict, *, outputs_root: Path, exact_storage: LocalStorage) -> None:
    """Verify exact revision identity/read-back and emit actionable mismatch evidence."""
    mismatches: list[dict[str, object]] = []

    def check(subject: str, actual, expected) -> None:
        if actual != expected:
            mismatches.append({"subject": subject, "expected": expected, "actual": actual})

    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, info["canvas_id"])
        canvas_refs = []
        if canvas is not None:
            for node in json.loads(canvas.doc).get("nodes", []):
                ref = node.get("data", {}).get("config", {}).get("datasetRef")
                if isinstance(ref, dict):
                    canvas_refs.append((ref.get("datasetId"), ref.get("revisionId")))
        check(
            "canvas exact references",
            canvas_refs,
            [(ref["dataset_id"], ref["revision_id"]) for ref in info["exact_refs"]],
        )

        admission = session.get(metadb.RunInputAdmission, info["run_id"])
        check(
            "immutable run admission manifest",
            json.loads(admission.manifest) if admission is not None else None,
            info["admitted_manifest"],
        )
        history = session.scalar(select(metadb.RunRecord).where(
            metadb.RunRecord.run_id == info["run_id"],
            metadb.RunRecord.canvas_id == info["canvas_id"],
        ))
        check(
            "run history manifest",
            json.loads(history.input_manifest) if history and history.input_manifest else None,
            info["admitted_manifest"],
        )

        core_revisions = list(session.scalars(select(metadb.ManagedLocalFileRevision).where(
            metadb.ManagedLocalFileRevision.logical_id == info["core_original"]["dataset_id"],
        ).order_by(metadb.ManagedLocalFileRevision.publish_seq)))
        check(
            "managed revision ledger",
            [(row.revision_id, row.artifact_uri) for row in core_revisions],
            [
                (info["core_original"]["revision_id"], info["core_original"]["artifact_uri"]),
                (info["core_head"]["revision_id"], info["core_head"]["artifact_uri"]),
            ],
        )
        core_logical = session.get(
            metadb.CatalogLogicalDataset, info["core_original"]["dataset_id"])
        check(
            "managed current pointer",
            (core_logical.logical_id, core_logical.current_uri, core_logical.state)
            if core_logical is not None else None,
            (info["core_original"]["dataset_id"], info["core_head"]["artifact_uri"], "active"),
        )
        for label, revision in (
                ("managed original field lineage", info["core_original"]),
                ("managed head field lineage", info["core_head"])):
            rows, cursor, truncated, available = metadb.catalog_field_lineage_page(
                revision["dataset_id"], revision["revision_id"], ["value"])
            check(
                label,
                {
                    "available": available,
                    "cursor": cursor,
                    "truncated": truncated,
                    "mappings": [(
                        row["source_dataset_id"],
                        row["source_version"],
                        row["destination_revision_id"],
                        row["destination_field"],
                    ) for row in rows],
                },
                {
                    "available": True,
                    "cursor": None,
                    "truncated": False,
                    "mappings": [(
                        info["parent_dataset_id"],
                        "parent-v1",
                        revision["revision_id"],
                        "value",
                    )],
                },
            )

        tombstone_logical = session.get(
            metadb.CatalogLogicalDataset, info["tombstone"]["dataset_id"])
        check(
            "unregistered logical tombstone",
            (tombstone_logical.logical_id, tombstone_logical.logical_uri,
             tombstone_logical.current_uri, tombstone_logical.state)
            if tombstone_logical is not None else None,
            (info["tombstone"]["dataset_id"], info["tombstone_logical_uri"], None, "unregistered"),
        )
        check(
            "tombstoned revision ledger",
            session.get(
                metadb.ManagedLocalFileRevision, info["tombstone"]["revision_id"]
            ) is not None,
            True,
        )

        for label, ref in (
                ("core original retention", info["core_original"]),
                ("core head retention", info["core_head"]),
                ("tombstone retention", info["tombstone"])):
            retained = session.get(metadb.LocalResultReference, {
                "uri": ref["artifact_uri"],
                "owner_kind": "managed_file_revision",
                "owner_key": ref["revision_id"],
            })
            check(label, retained is not None, True)
        for owner_kind, owner_key in (
                ("canvas", info["canvas_id"]),
                ("run_input_admission", info["run_id"])):
            ref = session.get(metadb.LocalResultReference, {
                "uri": info["core_original"]["artifact_uri"],
                "owner_kind": owner_kind,
                "owner_key": owner_key,
            })
            check(f"exact core {owner_kind} reference", ref is not None, True)
        ordinary = session.get(metadb.LocalFileInputRevision, {
            "dataset_id": info["ordinary_input"]["dataset_id"],
            "revision_id": info["ordinary_input"]["revision_id"],
        })
        check(
            "ordinary exact input mapping",
            ordinary.artifact_uri if ordinary is not None else None,
            info["ordinary_input"]["artifact_uri"],
        )
        ordinary_ref = session.get(metadb.LocalResultReference, {
            "uri": info["ordinary_input"]["artifact_uri"],
            "owner_kind": "run_input_admission",
            "owner_key": info["run_id"],
        })
        check("ordinary exact input retention", ordinary_ref is not None, True)

    for label, ref in (
            ("core original artifact", info["core_original"]),
            ("core head artifact", info["core_head"]),
            ("tombstone artifact", info["tombstone"])):
        restored = outputs_root / ref["artifact_rel"]
        check(f"{label} present", restored.is_file(), True)
        check(
            f"{label} sha256",
            _file_sha256(restored) if restored.is_file() else None,
            ref["artifact_sha256"],
        )

    ordinary_restored = outputs_root / info["ordinary_input"]["artifact_rel"]
    check("ordinary exact input artifact present", ordinary_restored.is_file(), True)
    check(
        "ordinary exact input artifact sha256",
        _file_sha256(ordinary_restored) if ordinary_restored.is_file() else None,
        info["ordinary_input"]["artifact_sha256"],
    )
    try:
        ordinary_rows = LocalFileInputRevisionAdapter().open_revision(
            info["ordinary_input"]["artifact_uri"],
            info["ordinary_input"]["revision_id"],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — report exact operational evidence
        mismatches.append({
            "subject": "ordinary exact input read-back",
            "expected": [[info["ordinary_input"]["value"]]],
            "actual": f"{type(exc).__name__}: {exc}",
        })
    else:
        check("ordinary exact input read-back", [list(row) for row in ordinary_rows],
              [[info["ordinary_input"]["value"]]])

    core_binding = metadb.catalog_revision_binding(info["core_original"]["dataset_id"])
    check(
        "core opaque dataset binding",
        core_binding,
        {"dataset_id": info["core_original"]["dataset_id"],
         "uri": info["core_head"]["artifact_uri"]},
    )
    try:
        with exact_storage.acquire_result_read(
                info["core_original"]["artifact_uri"], "backup-restore-verifier"):
            with metadb.session() as session:
                read_lease = session.scalar(select(metadb.LocalResultReference).where(
                    metadb.LocalResultReference.uri == info["core_original"]["artifact_uri"],
                    metadb.LocalResultReference.owner_kind == "read_lease",
                ))
                check("core exact read lease", read_lease is not None, True)
            core_rows = ManagedLocalFileRevisionAdapter().open_revision(
                info["core_head"]["artifact_uri"],
                info["core_original"]["revision_id"],
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — report exact operational evidence
        mismatches.append({
            "subject": "core exact revision read-back",
            "expected": [[info["core_original"]["value"]]],
            "actual": f"{type(exc).__name__}: {exc}",
        })
    else:
        check("core exact revision read-back", [list(row) for row in core_rows],
              [[info["core_original"]["value"]]])

    provider_status: dict[str, str] = {}
    for label, ref, expected_status in (
            ("available", info["provider_available"], "available"),
            ("unavailable", info["provider_unavailable"], "unavailable")):
        binding = metadb.catalog_revision_binding(ref["dataset_id"])
        check(
            f"provider {label} opaque dataset binding",
            binding,
            {"dataset_id": ref["dataset_id"], "uri": ref["uri"]},
        )
        try:
            rows = LanceAdapter().open_revision(ref["uri"], ref["revision_id"]).fetchall()
        except RevisionUnavailable:
            provider_status[label] = "unavailable"
        except Exception as exc:  # noqa: BLE001 — classify unexpected restore failures precisely
            provider_status[label] = f"{type(exc).__name__}: {exc}"
        else:
            provider_status[label] = "available"
            check(f"provider {label} exact rows", [list(row) for row in rows], [[ref["value"]]])
        check(f"provider {label} exact status", provider_status[label], expected_status)

    if mismatches:
        pytest.fail(
            "BACKUP_RESTORE_REVISION_MISMATCH: "
            + json.dumps(mismatches, sort_keys=True, default=str)
        )
    print(
        "BACKUP_RESTORE_REVISION_EVIDENCE: "
        + json.dumps({
            "core": "verified",
            "provider_available": provider_status["available"],
            "provider_unavailable": provider_status["unavailable"],
            "dataset_id": info["core_original"]["dataset_id"],
            "revision_id": info["core_original"]["revision_id"],
            "tombstone_dataset_id": info["tombstone"]["dataset_id"],
        }, sort_keys=True),
        flush=True,
    )


def _assert_execution_manifest_recovery(info: dict) -> None:
    """Prove every retained run/task owner resolves to its exact canonical document."""
    mismatches: list[dict[str, object]] = []
    owners: list[tuple[str, str | None, dict]] = []
    ordinary = info["ordinary_execution_manifest"]
    durable = info["durable_tasks"]
    live_canvas_diverged = False

    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, info["canvas_id"])
        admission = session.get(metadb.RunInputAdmission, info["run_id"])
        state = session.get(metadb.RunState, info["run_id"])
        history = session.scalar(select(metadb.RunRecord).where(
            metadb.RunRecord.run_id == info["run_id"],
            metadb.RunRecord.canvas_id == info["canvas_id"],
        ))
        revision = session.get(
            metadb.ManagedLocalFileRevision, ordinary["receipt_revision_id"])
        lineage = session.scalar(select(metadb.CatalogLineageFact).where(
            metadb.CatalogLineageFact.run_id == info["run_id"]))
        owners.extend([
            ("ordinary admission", admission.execution_manifest_sha256 if admission else None,
             ordinary),
            ("ordinary run state", state.execution_manifest_sha256 if state else None, ordinary),
            ("ordinary run history",
             history.execution_manifest_sha256 if history else None, ordinary),
            ("ordinary write receipt row",
             revision.execution_manifest_sha256 if revision else None, ordinary),
            ("ordinary lineage fact",
             lineage.execution_manifest_sha256 if lineage else None, ordinary),
        ])
        if canvas is not None:
            live_write = next(
                node for node in json.loads(canvas.doc)["nodes"] if node["id"] == "write")
            manifest_write = next(
                node for node in ordinary["document"]["graph"]["nodes"]
                if node["id"] == "write")
            live_canvas_diverged = live_write["data"] != manifest_write["data"]
        if not live_canvas_diverged:
            mismatches.append({
                "subject": "ordinary live Canvas divergence fixture",
                "code": "execution_manifest_mutable_substitution_not_tested",
                "expected": "live Canvas differs from retained manifest",
                "actual": "missing or unchanged live Canvas",
            })
        try:
            receipt_doc = json.loads(revision.write_receipt_doc) if (
                revision is not None and revision.write_receipt_doc) else None
        except (TypeError, ValueError) as exc:
            mismatches.append({
                "subject": "ordinary embedded write receipt",
                "code": "execution_manifest_reference_corrupt",
                "expected": ordinary["sha256"],
                "actual": f"{type(exc).__name__}: {exc}",
            })
        else:
            owners.append((
                "ordinary embedded write receipt",
                receipt_doc.get("executionManifestSha256")
                if isinstance(receipt_doc, dict) else None,
                ordinary,
            ))

        for label, expected in durable.items():
            if label not in ("managed", "checkpoint", "fanout", "external"):
                continue
            manifest = expected["manifest"]
            task = session.get(metadb.DurableTask, expected["task_id"])
            attempt = session.get(metadb.DurableTaskAttempt, expected["attempt_id"])
            owners.extend([
                (f"{label} durable task",
                 task.execution_manifest_sha256 if task else None, manifest),
                (f"{label} durable attempt",
                 attempt.execution_manifest_sha256 if attempt else None, manifest),
            ])
            if label == "external":
                continue
            inbox = session.scalar(select(metadb.DurableTaskInboxItem).where(
                metadb.DurableTaskInboxItem.task_id == expected["task_id"]))
            receipt_revision = session.scalar(select(metadb.ManagedLocalFileRevision).where(
                metadb.ManagedLocalFileRevision.run_id == expected["task_id"]))
            owners.extend([
                (f"{label} Inbox item",
                 inbox.execution_manifest_sha256 if inbox else None, manifest),
                (f"{label} write receipt row",
                 receipt_revision.execution_manifest_sha256 if receipt_revision else None,
                 manifest),
            ])

    for label, expected in durable.items():
        if label not in ("managed", "checkpoint", "fanout", "external"):
            continue
        task = metadb.durable_task(expected["task_id"])
        manifest = expected["manifest"]
        jobs = metadb.list_workspace_runs(
            metadb.DEFAULT_USER_ID, run_id=expected["task_id"])["items"]
        owners.append((
            f"{label} Jobs projection",
            jobs[0].get("executionManifestSha256") if jobs else None,
            manifest,
        ))
        if label != "external":
            owners.append((
                f"{label} embedded write receipt",
                task["output_receipt"].get("executionManifestSha256")
                if task and task.get("output_receipt") else None,
                manifest,
            ))

    for subject, actual_sha256, expected in owners:
        _check_execution_manifest_owner(
            mismatches,
            subject=subject,
            actual_sha256=actual_sha256,
            expected_sha256=expected["sha256"],
            expected_schema_version=expected["schema_version"],
            expected_document=expected["document"],
        )
    if mismatches:
        pytest.fail(
            "BACKUP_RESTORE_EXECUTION_MANIFEST_MISMATCH: "
            + json.dumps(mismatches, sort_keys=True, default=str)
        )
    print(
        "BACKUP_RESTORE_EXECUTION_MANIFEST_EVIDENCE: "
        + json.dumps({
            "ordinary": ordinary["sha256"],
            "durable": {
                label: durable[label]["manifest"]["sha256"]
                for label in ("managed", "checkpoint", "fanout", "external")
            },
            "owners": len(owners),
            "live_canvas_diverged": live_canvas_diverged,
        }, sort_keys=True),
        flush=True,
    )


def _assert_backup_identity(path: Path, info: dict, *, db: str, storage: str) -> None:
    mismatches: list[dict[str, object]] = []

    def check(subject: str, actual, expected) -> None:
        if actual != expected:
            mismatches.append({"subject": subject, "expected": expected, "actual": actual})

    try:
        identity = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — emit bounded restore evidence
        identity = {"read_error": f"{type(exc).__name__}: {exc}"}
    check("version release sha", identity.get("sha"), info["release_sha"])
    check("version package release", identity.get("version"), info["release_version"])
    check("version database profile", identity.get("db"), db)
    check("version storage profile", identity.get("storage"), storage)
    check("version schema head", identity.get("alembic"), info["alembic_head"])
    try:
        restored_head = metadb.require_schema_at_head()
    except Exception as exc:  # noqa: BLE001 — structured mismatch replaces raw audit failure
        restored_head = f"{type(exc).__name__}: {exc}"
    check("restored database schema identity", restored_head, identity.get("alembic"))
    if db == "postgresql":
        check("version object namespace", identity.get("namespace"), info["namespace"])
        check("restored database object namespace",
              metadb.object_storage_namespace(), identity.get("namespace"))
    if mismatches:
        pytest.fail(
            "BACKUP_RESTORE_IDENTITY_MISMATCH: "
            + json.dumps(mismatches, sort_keys=True, default=str)
        )


def _wait_for(predicate, timeout: float = 30.0):
    # Generous ceiling: the predicate returns as soon as recovery lands, so a passing run is
    # unaffected; only a genuinely stuck condition waits this long. 3s intermittently expired under
    # CI runner load while the durable recovery was merely slow, not broken.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("durable recovery condition did not become true")


def _assert_durable_task_recovery(info: dict, *, outputs_root: Path) -> None:
    fixture = info["durable_tasks"]
    mismatches: list[dict[str, object]] = []

    def check(subject: str, actual, expected) -> None:
        if actual != expected:
            mismatches.append({"subject": subject, "expected": expected, "actual": actual})

    kinds = {
        "managed": "managed_local_write",
        "checkpoint": "linear_checkpoint_write",
        "fanout": "bounded_fanout_write",
        "external": "external_wait",
    }
    for label, task_kind in kinds.items():
        expected = fixture[label]
        task = metadb.durable_task(expected["task_id"])
        check(f"{label} task present", task is not None, True)
        if task is None:
            continue
        check(f"{label} task kind", task["task_kind"], task_kind)
        check(f"{label} attempt identity", task["attempts"][-1]["id"],
              expected["attempt_id"])
        manifest_sha256 = task["execution_manifest_sha256"]
        check(f"{label} manifest retained",
              isinstance(manifest_sha256, str) and len(manifest_sha256) == 64, True)
        check(f"{label} attempt manifest identity",
              task["attempts"][-1]["execution_manifest_sha256"], manifest_sha256)
        jobs = metadb.list_workspace_runs(
            metadb.DEFAULT_USER_ID, run_id=expected["task_id"])["items"]
        check(f"{label} Jobs manifest identity",
              jobs[0]["executionManifestSha256"] if jobs else None, manifest_sha256)
        with metadb.session() as session:
            row = session.get(metadb.DurableTask, expected["task_id"])
            check(f"{label} canonical admission owns no legacy triple",
                  (row.graph_doc, row.input_manifest, row.write_intent), (None, None, None))
        check(f"{label} status", task["status"],
              "running" if label == "external" else "done")
        check(f"{label} attempt status", task["attempts"][-1]["status"],
              "running" if label == "external" else "done")
        check(f"{label} receipt identity", task["output_receipt"],
              None if label == "external" else expected["receipt"])
        if label != "external":
            check(f"{label} receipt manifest identity",
                  task["output_receipt"].get("executionManifestSha256"), manifest_sha256)
    managed = metadb.durable_task(fixture["managed"]["task_id"])
    if managed is not None:
        check("managed input manifest", managed["input_manifest"],
              fixture["managed"]["input_manifest"])
        graph_text = json.dumps(managed["graph_doc"], sort_keys=True)
        check("managed graph excludes private artifact binding",
              "_input_artifact_uri" not in graph_text, True)
    with metadb.session() as session:
        managed_ref = session.get(metadb.LocalResultReference, {
            "uri": fixture["managed"]["input_artifact_uri"],
            "owner_kind": "durable_task",
            "owner_key": fixture["managed"]["task_id"],
        })
        check("managed task exact input owner", managed_ref is not None, True)

        wait = session.get(metadb.DurableExternalWait, fixture["external"]["task_id"])
        check("external handle identity",
              json.loads(wait.handle_doc) if wait and wait.handle_doc else None,
              fixture["external"]["handle"])
        check("external checkpoint identity",
              json.loads(wait.checkpoint_doc) if wait and wait.checkpoint_doc else None,
              fixture["external"]["checkpoint"])
        check("external download evidence", wait.download_evidence if wait else None, None)
        check("external staged artifact identity",
              (wait.stage_dev, wait.stage_ino) if wait else None, (None, None))

        fanout_slots = list(session.scalars(select(
            bounded_fanout.BoundedFanoutSlot).order_by(
                bounded_fanout.BoundedFanoutSlot.slot_number)))
        check("bounded fanout slots released",
              [(slot.holder_attempt_id, slot.claim_token) for slot in fanout_slots],
              [(None, None)] * 4)

    for artifact in fixture["artifacts"]:
        restored = outputs_root / artifact["rel"]
        check(f"durable artifact present {artifact['rel']}", restored.is_file(), True)
        check(f"durable artifact sha256 {artifact['rel']}",
              _file_sha256(restored) if restored.is_file() else None,
              artifact["sha256"])

    try:
        checkpoint_audit = {
            item["task_id"]: item for item in metadb.linear_checkpoint_restore_audit()
        }
    except Exception as exc:  # noqa: BLE001 — convert audit refusal to bounded evidence
        mismatches.append({
            "subject": "linear checkpoint restore audit",
            "expected": "complete",
            "actual": f"{type(exc).__name__}: {exc}",
        })
    else:
        for label in ("checkpoint", "fanout"):
            task_id = fixture[label]["task_id"]
            check(f"{label} committed checkpoint audit",
                  checkpoint_audit.get(task_id, {}).get("phase"), "committed")
    try:
        fanout_audit = {
            item["parent_task_id"]: item for item in bounded_fanout.restore_audit()
        }
    except Exception as exc:  # noqa: BLE001 — convert audit refusal to bounded evidence
        mismatches.append({
            "subject": "bounded fanout restore audit",
            "expected": "complete",
            "actual": f"{type(exc).__name__}: {exc}",
        })
    else:
        restored_plan = fanout_audit.get(fixture["fanout"]["task_id"])
        check("bounded fanout plan digest",
              restored_plan.get("plan_digest") if restored_plan else None,
              fixture["fanout"]["plan_digest"])
        check("bounded fanout complete unit set",
              restored_plan.get("done_units") if restored_plan else None,
              fixture["fanout"]["unit_count"])

    inbox = metadb.list_durable_task_inbox_items(
        metadb.DEFAULT_USER_ID, limit=200)["items"]
    restored_inbox = {
        item["task_id"]: item for item in inbox if item["task_id"] in fixture["inbox"]
    }
    check("terminal inbox task set", set(restored_inbox), set(fixture["inbox"]))
    for task_id, expected in fixture["inbox"].items():
        item = restored_inbox.get(task_id)
        check(f"inbox identity {task_id}", item.get("id") if item else None,
              expected["id"])
        check(f"inbox read state {task_id}",
              item.get("read_at") is not None if item else None, expected["read"])
        check(f"public inbox manifest omitted {task_id}",
              "execution_manifest_sha256" not in item if item else None, True)
    check("external wait has no Inbox item",
          any(item["task_id"] == fixture["external"]["task_id"] for item in inbox), False)

    if mismatches:
        pytest.fail(
            "BACKUP_RESTORE_DURABLE_TASK_MISMATCH: "
            + json.dumps(mismatches, sort_keys=True, default=str)
        )

    external_id = fixture["external"]["task_id"]
    with metadb.session() as session:
        publication_count = int(session.scalar(
            select(func.count()).select_from(metadb.CatalogPublicationEvent)) or 0)
        wait = session.get(metadb.DurableExternalWait, external_id, with_for_update=True)
        assert wait is not None
        wait.next_poll_at = metadb._now() - datetime.timedelta(seconds=1)
    offline_deps = SimpleNamespace(_external_wait_adapter=lambda _kind: None)
    external_wait_tasks.recover(offline_deps)
    offline = _wait_for(lambda: (
        task if (task := metadb.durable_task(external_id))
        and task["external_wait"]["diagnostic_code"] == "adapter_unavailable" else None
    ))
    assert offline["status"] == "running"
    assert offline["external_wait"]["phase"] == "running"
    assert offline["output_receipt"] is None

    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, external_id, with_for_update=True)
        assert wait is not None
        wait.next_poll_at = metadb._now() - datetime.timedelta(seconds=1)
        poll_count = wait.poll_count
    adapter = _ReattachAdapter()
    online_deps = SimpleNamespace(
        _external_wait_adapter=lambda kind: adapter if kind == "backup-drill" else None)
    external_wait_tasks.recover(online_deps)
    external_wait_tasks.recover(online_deps)
    online = _wait_for(lambda: (
        task if (task := metadb.durable_task(external_id))
        and task["external_wait"]["poll_count"] > poll_count
        and adapter.status_calls >= 1 else None
    ))
    time.sleep(.05)
    assert adapter.submit_calls == 0
    assert adapter.status_calls == 1
    assert adapter.seen_handles == [fixture["external"]["handle"]["job_id"]]
    assert online["external_wait"]["phase"] == "running"
    with metadb.session() as session:
        wait = session.get(metadb.DurableExternalWait, external_id)
        assert wait is not None
        assert json.loads(wait.checkpoint_doc)["sequence"] == 8
        assert int(session.scalar(select(func.count()).select_from(
            metadb.CatalogPublicationEvent)) or 0) == publication_count
    assert not metadb.commit_external_wait_transition(
        external_id, fixture["external"]["attempt_id"], "backup-external-poll",
        failure_code="adapter_unavailable", retry_after=1.0)
    assert metadb.claim_durable_task(fixture["managed"]["task_id"], "late-owner") is None
    assert metadb.claim_linear_checkpoint_task(
        fixture["checkpoint"]["task_id"], "late-owner") is None
    assert metadb.claim_bounded_fanout_write_task(
        fixture["fanout"]["task_id"], "late-owner") is None
    assert bounded_fanout.heartbeat_attempt(
        attempt_id=fixture["fanout"]["old_unit_attempt_id"],
        claim_token=fixture["fanout"]["old_unit_claim_token"],
        owner_token="backup-fanout-owner",
    ) is False

    print(
        "BACKUP_RESTORE_DURABLE_TASK_EVIDENCE: "
        + json.dumps({
            "tasks": {label: fixture[label]["task_id"] for label in kinds},
            "external_handle": fixture["external"]["handle"]["job_id"],
            "external_submit_calls": adapter.submit_calls,
            "external_status_calls": adapter.status_calls,
            "checkpoint_audit": "verified",
            "fanout_audit": "verified",
        }, sort_keys=True),
        flush=True,
    )


def _assert_isolation_applied(info: dict, replacement: str, provider: _ClaimProvider) -> None:
    assert metadb.object_storage_namespace() == replacement
    assert metadb.object_attempt_owner_id() != info["owner"]
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, info["managed_uri"])
        assert attempt is not None and attempt.state == "quarantined"
        assert session.scalar(select(func.count()).select_from(metadb.ObjectStorageClaim)) == 0
    assert provider.claims[info["claim_key"]]["body"] == info["marker_body"]


def _assert_mismatch_refuses_without_isolation(monkeypatch) -> None:
    monkeypatch.setenv("DP_STORAGE_NAMESPACE", f"wrong-{uuid.uuid4().hex[:12]}")
    with pytest.raises(RuntimeError, match="does not match this metadata database"):
        metadb.object_storage_namespace()
    monkeypatch.delenv("DP_STORAGE_NAMESPACE", raising=False)


def _emit_evidence(info: dict, rto_ms: int) -> None:
    print(
        "BACKUP_RESTORE_DRILL RPO: "
        f"canvas={info['canvas_id']} "
        f"catalog={info['parent_uri']},{info['child_uri']} "
        f"run={info['run_id']} lineage=drill_parent->drill_child "
        f"lineage_receipt={info['lineage_publication_key']} "
        f"artifact={info['artifact_uri']} "
        f"managed_dataset={info['core_original']['dataset_id']} "
        f"managed_revision={info['core_original']['revision_id']} "
        f"tombstone_dataset={info['tombstone']['dataset_id']} "
        f"admission={info['run_id']} "
        f"managed_attempt={info['managed_uri']} "
        f"namespace_marker={info['claim_key'][0]}/_dp_control/namespaces/{info['namespace']}.json "
        f"alembic={info['alembic_head']} release_sha={info['release_sha']}",
        flush=True,
    )
    print(
        "BACKUP_RESTORE_OVERLAY_EVIDENCE: "
        + json.dumps({
            label: {
                "binding": "preserved",
                "anchor": "preserved",
                "placement": "preserved",
                "canvas": "reopenable",
                "replay": "preserved",
                "restored_state": info["overlay_recovery_evidence"][label]["restored_state"],
                "post_reopen_state": info["overlay_recovery_evidence"][label][
                    "post_reopen_state"],
            }
            for label in info["overlays"]
        }, sort_keys=True),
        flush=True,
    )
    print(f"BACKUP_RESTORE_DRILL RTO_MS: {rto_ms}", flush=True)


def _libpq_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://"):]
    return url


def _ensure_sibling_database(source_url: str, sibling_name: str) -> str:
    parsed = make_url(source_url)
    admin = create_engine(source_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": sibling_name},
            ).scalar()
            if exists:
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ), {"n": sibling_name})
                conn.execute(text(f'DROP DATABASE "{sibling_name}"'))
            conn.execute(text(f'CREATE DATABASE "{sibling_name}"'))
    finally:
        admin.dispose()
    return parsed.set(database=sibling_name).render_as_string(hide_password=False)


def _drop_database(source_url: str, name: str) -> None:
    admin = create_engine(source_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ), {"n": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        admin.dispose()


def _reset_database(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def test_execution_manifest_restore_mismatch_classification(monkeypatch):
    expected_sha256 = "a" * 64
    expected_schema_version = 1
    expected_document = {"schemaVersion": 1}
    mismatches: list[dict[str, object]] = []

    monkeypatch.setattr(metadb, "execution_manifest", lambda _sha256: None)
    _check_execution_manifest_owner(
        mismatches,
        subject="pruned owner",
        actual_sha256=None,
        expected_sha256=expected_sha256,
        expected_schema_version=expected_schema_version,
        expected_document=expected_document,
    )
    _check_execution_manifest_owner(
        mismatches,
        subject="missing document",
        actual_sha256=expected_sha256,
        expected_sha256=expected_sha256,
        expected_schema_version=expected_schema_version,
        expected_document=expected_document,
    )

    def corrupt(_sha256):
        raise ValueError("digest does not match document")

    monkeypatch.setattr(metadb, "execution_manifest", corrupt)
    _check_execution_manifest_owner(
        mismatches,
        subject="corrupt document",
        actual_sha256=expected_sha256,
        expected_sha256=expected_sha256,
        expected_schema_version=expected_schema_version,
        expected_document=expected_document,
    )

    monkeypatch.setattr(metadb, "execution_manifest", lambda _sha256: {
        "sha256": expected_sha256,
        "schema_version": 2,
        "document": expected_document,
    })
    _check_execution_manifest_owner(
        mismatches,
        subject="wrong schema version",
        actual_sha256=expected_sha256,
        expected_sha256=expected_sha256,
        expected_schema_version=expected_schema_version,
        expected_document=expected_document,
    )

    assert [item["code"] for item in mismatches] == [
        "execution_manifest_owner_pruned",
        "execution_manifest_document_missing",
        "execution_manifest_document_corrupt",
        "execution_manifest_document_corrupt",
    ]


def test_sqlite_isolated_restore_drill(tmp_path, monkeypatch):
    """Profile A drill: SQLite + local files fixture backup into an isolated namespace."""
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    provider = _ClaimProvider()
    source_ws = tmp_path / "source"
    source_ws.mkdir()
    source_db = source_ws / "dataplay.db"
    source_outputs = source_ws / "outputs"
    source_outputs.mkdir()
    claim_uri = f"s3://drill-{uuid.uuid4().hex[:8]}/root/out.attempt-drill"

    try:
        _switch_db(f"sqlite:///{source_db}")
        storage = LocalStorage(str(source_outputs))
        info = _seed_fixture(source_ws, storage, claim_uri=claim_uri, provider=provider)

        source_outputs_fp = _tree_fingerprint(source_outputs)
        source_plugins_fp = _tree_fingerprint(source_ws / "plugins")
        source_marker = bytes(provider.claims[info["claim_key"]]["body"])

        # Freeze writers (dispose + checkpoint) before capturing the backup set.
        _close_db()
        source_db_sha = _file_sha256(source_db)
        backup = tmp_path / "backup"
        backup.mkdir()
        shutil.copy2(source_db, backup / "dataplay.db")
        shutil.copytree(source_outputs, backup / "outputs")
        shutil.copytree(source_ws / "plugins", backup / "plugins")
        (backup / "version.json").write_text(json.dumps({
            "version": info["release_version"], "sha": info["release_sha"],
            "db": "sqlite", "storage": "local",
            "alembic": info["alembic_head"],
        }), encoding="utf-8")
        assert not any("provider-owned" in path.parts for path in backup.rglob("*"))
        shutil.rmtree(info["provider_unavailable"]["uri"])

        # Failure path: restored clone + fresh DP_STORAGE_NAMESPACE without isolation.
        probe = tmp_path / "probe"
        probe.mkdir()
        shutil.copy2(backup / "dataplay.db", probe / "dataplay.db")
        _switch_db(f"sqlite:///{probe / 'dataplay.db'}")
        metadb.init_db()
        _assert_mismatch_refuses_without_isolation(monkeypatch)
        _close_db()

        restore_ws = tmp_path / "restore"
        restore_ws.mkdir()
        restore_start = time.perf_counter()
        shutil.copy2(backup / "dataplay.db", restore_ws / "dataplay.db")
        shutil.copytree(backup / "outputs", restore_ws / "outputs")
        shutil.copytree(backup / "plugins", restore_ws / "plugins")
        shutil.copy2(backup / "version.json", restore_ws / "version.json")

        _switch_db(f"sqlite:///{restore_ws / 'dataplay.db'}")
        metadb.init_db()
        _assert_backup_identity(
            restore_ws / "version.json", info, db="sqlite", storage="local")
        # Confirm the copied identity still matches the freeze before isolating.
        assert metadb.object_storage_namespace() == info["namespace"]
        replacement = f"restore-{uuid.uuid4().hex[:16]}"
        assert metadb.isolate_cloned_object_storage(info["namespace"], replacement) == replacement
        monkeypatch.setenv("DP_STORAGE_NAMESPACE", replacement)
        try:
            # Exact local artifact URIs are absolute. Model the isolated clone/container mounting the
            # copied tree at that recorded path, while parking the source bytes so no assertion can
            # accidentally read them.
            with _mount_restored_outputs_at_exact_path(
                    source_outputs, restore_ws / "outputs"):
                exact_storage = LocalStorage(str(source_outputs))
                try:
                    _assert_fixture_readable(info, outputs_root=restore_ws / "outputs")
                    _assert_overlay_recovery(info)
                    _assert_revision_recovery(
                        info, outputs_root=restore_ws / "outputs",
                        exact_storage=exact_storage)
                    _assert_execution_manifest_recovery(info)
                    _assert_durable_task_recovery(
                        info, outputs_root=restore_ws / "outputs")
                    _assert_isolation_applied(info, replacement, provider)
                    assert (restore_ws / "outputs" / info["artifact_rel"]).is_file()
                finally:
                    exact_storage.close()
        finally:
            monkeypatch.delenv("DP_STORAGE_NAMESPACE", raising=False)

        _emit_evidence(info, int((time.perf_counter() - restore_start) * 1000))

        # Source installation byte-for-byte untouched (DB, outputs, plugins, namespace marker).
        _switch_db(f"sqlite:///{source_db}")
        metadb.init_db()
        assert _file_sha256(source_db) == source_db_sha
        assert _tree_fingerprint(source_outputs) == source_outputs_fp
        assert _tree_fingerprint(source_ws / "plugins") == source_plugins_fp
        assert provider.claims[info["claim_key"]]["body"] == source_marker
        assert metadb.object_storage_namespace() == info["namespace"]
        assert metadb.object_attempt_owner_id() == info["owner"]
        assert metadb.catalog_get(info["parent_uri"])["uri"] == info["parent_uri"]
        with metadb.session() as session:
            assert _lineage_receipt_identity(
                session, info["lineage_publication_key"]) == info["lineage_receipt"]
            attempt = session.get(metadb.ObjectAttempt, info["managed_uri"])
            assert attempt is not None and attempt.state != "quarantined"
    finally:
        handoff.set_managed_object_provider(None)
        if metadb._engine is not None and metadb._engine is not original_engine:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


@pytest.mark.skipif(
    not (os.environ.get("DP_TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="requires dedicated Postgres via DP_TEST_DATABASE_URL",
)
def test_postgres_object_store_isolated_restore_drill(tmp_path, monkeypatch):
    """Profile B drill: PostgreSQL dump/restore + object-store namespace marker isolation."""
    url = os.environ["DP_TEST_DATABASE_URL"]
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    provider = _ClaimProvider()
    source_ws = tmp_path / "pg-source"
    source_ws.mkdir()
    source_outputs = source_ws / "outputs"
    source_outputs.mkdir()
    claim_uri = f"s3://pg-drill-{uuid.uuid4().hex[:8]}/root/out.attempt-drill"
    probe_name = f"dataplay_probe_{uuid.uuid4().hex[:8]}"
    restore_name = f"dataplay_restore_{uuid.uuid4().hex[:8]}"
    created = []

    try:
        if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
            pytest.skip("pg_dump/pg_restore required for the PostgreSQL restore drill")

        _reset_database(url)
        _switch_db(url)
        metadb.migrate_db()
        storage = LocalStorage(str(source_outputs))
        info = _seed_fixture(source_ws, storage, claim_uri=claim_uri, provider=provider)

        source_row_counts = {}
        with metadb.session() as session:
            for model in (metadb.Canvas, metadb.CatalogEntry, metadb.RunRecord,
                          metadb.RunState, metadb.ExecutionManifest,
                          metadb.WorkspaceContainer, metadb.WorkspacePlacement,
                          metadb.CatalogLineageFact, metadb.CatalogPublicationEvent,
                          metadb.LocalResultArtifact, metadb.LocalResultReference,
                          metadb.LocalFileInputRevision,
                          metadb.CatalogLogicalDataset, metadb.ManagedLocalFileRevision,
                          metadb.RunInputAdmission, metadb.DurableTask,
                          metadb.DurableTaskAttempt, metadb.DurableTaskInboxItem,
                          metadb.ObjectAttempt, metadb.WorkspaceProviderBinding,
                          metadb.WorkspaceExternalOverlayAnchor,
                          metadb.WorkspaceCanvasCreateReplay):
                source_row_counts[model.__tablename__] = session.scalar(
                    select(func.count()).select_from(model))
        source_outputs_fp = _tree_fingerprint(source_outputs)
        source_marker = bytes(provider.claims[info["claim_key"]]["body"])

        # Stop writers before the dump / outputs copy (consistency ordering from the runbook).
        _close_db()
        backup = tmp_path / "pg-backup"
        backup.mkdir()
        dump_path = backup / "dataplay.dump"
        dumped = subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-acl",
             "-f", str(dump_path), _libpq_url(url)],
            capture_output=True, text=True, timeout=120,
        )
        assert dumped.returncode == 0, dumped.stderr
        shutil.copytree(source_outputs, backup / "outputs")
        (backup / "version.json").write_text(json.dumps({
            "version": info["release_version"], "sha": info["release_sha"],
            "db": "postgresql", "storage": "s3",
            "alembic": info["alembic_head"], "namespace": info["namespace"],
        }), encoding="utf-8")
        assert not any("provider-owned" in path.parts for path in backup.rglob("*"))
        shutil.rmtree(info["provider_unavailable"]["uri"])

        probe_url = _ensure_sibling_database(url, probe_name)
        created.append(probe_name)
        probe_restore = subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
             "-d", _libpq_url(probe_url), str(dump_path)],
            capture_output=True, text=True, timeout=120,
        )
        assert probe_restore.returncode == 0, probe_restore.stderr
        _switch_db(probe_url)
        metadb.init_db()
        _assert_mismatch_refuses_without_isolation(monkeypatch)
        _close_db()

        restore_start = time.perf_counter()
        restore_url = _ensure_sibling_database(url, restore_name)
        created.append(restore_name)
        restored = subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
             "-d", _libpq_url(restore_url), str(dump_path)],
            capture_output=True, text=True, timeout=120,
        )
        assert restored.returncode == 0, restored.stderr
        restore_ws = tmp_path / "pg-restore"
        shutil.copytree(backup / "outputs", restore_ws / "outputs")
        shutil.copy2(backup / "version.json", restore_ws / "version.json")

        _switch_db(restore_url)
        metadb.init_db()
        # Compare the restored database before TestClient deep links can truthfully degrade an
        # active binding whose provider configuration is intentionally outside the backup.
        with metadb.session() as session:
            for model in (metadb.WorkspaceProviderBinding,
                          metadb.WorkspaceExternalOverlayAnchor,
                          metadb.WorkspaceCanvasCreateReplay):
                assert session.scalar(select(func.count()).select_from(model)) == \
                    source_row_counts[model.__tablename__]
        _assert_backup_identity(
            restore_ws / "version.json", info, db="postgresql", storage="s3")
        assert metadb.object_storage_namespace() == info["namespace"]
        replacement = f"restore-{uuid.uuid4().hex[:16]}"
        assert metadb.isolate_cloned_object_storage(info["namespace"], replacement) == replacement
        monkeypatch.setenv("DP_STORAGE_NAMESPACE", replacement)
        try:
            with _mount_restored_outputs_at_exact_path(
                    source_outputs, restore_ws / "outputs"):
                exact_storage = LocalStorage(str(source_outputs))
                try:
                    assert Path(info["artifact_uri"]).is_file()
                    _assert_fixture_readable(info, outputs_root=restore_ws / "outputs")
                    _assert_overlay_recovery(info)
                    _assert_revision_recovery(
                        info, outputs_root=restore_ws / "outputs",
                        exact_storage=exact_storage)
                    _assert_execution_manifest_recovery(info)
                    _assert_durable_task_recovery(
                        info, outputs_root=restore_ws / "outputs")
                    _assert_isolation_applied(info, replacement, provider)
                finally:
                    exact_storage.close()
        finally:
            monkeypatch.delenv("DP_STORAGE_NAMESPACE", raising=False)

        _emit_evidence(info, int((time.perf_counter() - restore_start) * 1000))
        _close_db()

        _switch_db(url)
        metadb.init_db()
        with metadb.session() as session:
            for model in (metadb.Canvas, metadb.CatalogEntry, metadb.RunRecord,
                          metadb.RunState, metadb.ExecutionManifest,
                          metadb.WorkspaceContainer, metadb.WorkspacePlacement,
                          metadb.CatalogLineageFact, metadb.CatalogPublicationEvent,
                          metadb.LocalResultArtifact, metadb.LocalResultReference,
                          metadb.LocalFileInputRevision,
                          metadb.CatalogLogicalDataset, metadb.ManagedLocalFileRevision,
                          metadb.RunInputAdmission, metadb.DurableTask,
                          metadb.DurableTaskAttempt, metadb.DurableTaskInboxItem,
                          metadb.ObjectAttempt, metadb.WorkspaceProviderBinding,
                          metadb.WorkspaceExternalOverlayAnchor,
                          metadb.WorkspaceCanvasCreateReplay):
                assert session.scalar(select(func.count()).select_from(model)) == \
                    source_row_counts[model.__tablename__]
            assert _lineage_receipt_identity(
                session, info["lineage_publication_key"]) == info["lineage_receipt"]
            attempt = session.get(metadb.ObjectAttempt, info["managed_uri"])
            assert attempt is not None and attempt.state != "quarantined"
            ident = session.get(metadb.InstallationIdentity, 1)
            assert ident.storage_namespace == info["namespace"]
            assert ident.owner_token == info["owner"]
        assert _tree_fingerprint(source_outputs) == source_outputs_fp
        assert provider.claims[info["claim_key"]]["body"] == source_marker
        assert metadb.object_storage_namespace() == info["namespace"]
    finally:
        handoff.set_managed_object_provider(None)
        if metadb._engine is not None and metadb._engine is not original_engine:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session
        for name in created:
            try:
                _drop_database(url, name)
            except Exception:  # noqa: BLE001 — cleanup best-effort
                pass
