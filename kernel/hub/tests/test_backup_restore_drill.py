"""OPS-03 backup / isolated-restore drill — fixture backup, restore, isolation, RPO/RTO evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine.url import make_url

from hub import handoff, metadb
from hub.local_run_inputs import finalize_local_file_candidates, snapshot_local_file_input
from hub.models import CatalogTable, RunOutput
from hub.plugins.adapters import (
    DuckDBAdapter, LanceAdapter, LocalFileInputRevisionAdapter,
    ManagedLocalFileRevisionAdapter, RevisionUnavailable,
)
from hub.plugins.catalog import InMemoryCatalog
from hub.storage import LocalStorage


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
        storage: LocalStorage, catalog: InMemoryCatalog, logical_uri: str, value: int) -> dict:
    run_id = f"backup-revision-{uuid.uuid4().hex}"
    artifact = storage.begin_result(f"backup-revision:{logical_uri}", run_id)
    pq.write_table(pa.table({"value": [value]}), artifact)
    storage.commit_result(artifact, run_id)
    try:
        published = catalog.publish_managed_local_file_output(
            name="backup_revision", logical_uri=logical_uri, artifact_uri=artifact)
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


def _seed_fixture(workspace: Path, storage: LocalStorage, *, claim_uri: str,
                  provider: _ClaimProvider) -> dict:
    metadb.init_db()
    namespace = metadb.object_storage_namespace()
    owner = metadb.object_attempt_owner_id()
    alembic_head = metadb.expected_schema_head()
    assert metadb.require_schema_at_head() == alembic_head
    release_sha = os.environ.get("DP_GIT_SHA", "").strip() or "drill-fixture"

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

    metadb.catalog_upsert_entry(
        parent_uri, "drill-parent",
        {"id": "drill_parent", "name": "drill-parent", "uri": parent_uri,
         "columns": [{"name": "id", "type": "int64"}]})
    lineage_idempotency_key = f"backup-restore-drill-{uuid.uuid4().hex}"
    lineage_publication_key = (
        "lineage-publication:v1:sha256:"
        + hashlib.sha256(lineage_idempotency_key.encode("utf-8")).hexdigest()
    )
    metadb.catalog_upsert_entry(
        child_uri, "drill-child",
        {"id": "drill_child", "name": "drill-child", "uri": child_uri,
         "columns": [{"name": "id", "type": "int64"}]},
        parents=[parent_uri], pipeline="backup-restore-drill",
        lineage=_lineage(
            lineage_idempotency_key,
            mappings=[{"source_field": "id", "destination_field": "id"}],
        ))
    with metadb.session() as session:
        lineage_receipt = _lineage_receipt_identity(session, lineage_publication_key)
    assert lineage_receipt["effect_type"] == "lineage"
    assert lineage_receipt["uri"] == child_uri
    assert lineage_receipt["version"] is None
    assert lineage_receipt["fingerprint"].startswith("lineage-publication:v2:sha256:")

    revision_catalog = InMemoryCatalog(str(data_dir), lambda _uri: DuckDBAdapter())
    managed_logical_uri = str(data_dir / "managed-recovery.parquet")
    core_original = _publish_core_revision(
        storage, revision_catalog, managed_logical_uri, 101)
    core_head = _publish_core_revision(
        storage, revision_catalog, managed_logical_uri, 202)
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
    canvas_doc = {
        "id": canvas_id,
        "nodes": [
            _exact_source_node("core", core_original),
            _exact_source_node("provider-available", provider_available),
            _exact_source_node("provider-unavailable", provider_unavailable),
            {"id": "ordinary", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": ordinary_input["source_uri"]}}},
        ],
        "edges": [],
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
    run_id, created = metadb.admit_local_run_inputs(
        uid=metadb.DEFAULT_USER_ID,
        canvas_id=canvas_id,
        submission_id=str(uuid.uuid4()),
        target_node_id="core",
        intent_sha256=hashlib.sha256(b"backup-restore-revision-drill").hexdigest(),
        manifest=admitted_manifest,
        local_file_candidates=[ordinary_candidate],
    )
    assert created is True
    finalize_local_file_candidates(storage, [ordinary_candidate], run_id)
    artifact_uri = storage.begin_result(f"plan-{uuid.uuid4().hex}", run_id)
    Path(artifact_uri).write_bytes(b"drill-local-result-bytes")
    storage.commit_result(artifact_uri, run_id)
    metadb.bind_run_owner(run_id, metadb.DEFAULT_USER_ID, canvas_id)
    run_output = RunOutput(
        node_id="n1",
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
            "target_node_id": "n1",
            "total_rows": 1,
            "outputs": [run_output],
        },
        canvas_id=canvas_id,
    )
    assert storage.release_result(artifact_uri, run_id) is True
    metadb.record_run(
        canvas_id, "n1", "run", "done", rows=1, outputs=[run_output], run_id=run_id)
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

    claim_key = (urlsplit(claim_uri).netloc, namespace)
    return {
        "namespace": namespace,
        "owner": owner,
        "alembic_head": alembic_head,
        "release_sha": release_sha,
        "canvas_id": canvas_id,
        "workspace_container_id": workspace_container["id"],
        "workspace_placement_id": workspace_placement["id"],
        "parent_uri": parent_uri,
        "child_uri": child_uri,
        "lineage_publication_key": lineage_publication_key,
        "lineage_receipt": lineage_receipt,
        "run_id": run_id,
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
    }


def _assert_fixture_readable(info: dict, *, outputs_root: Path) -> None:
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, info["canvas_id"])
        assert canvas is not None and canvas.name == "backup-restore-drill"
        assert [node["id"] for node in json.loads(canvas.doc)["nodes"]] == [
            "core", "provider-available", "provider-unavailable", "ordinary",
        ]
        runs = list(session.scalars(
            select(metadb.RunRecord).where(metadb.RunRecord.canvas_id == info["canvas_id"])))
        assert any(
            row.run_id == info["run_id"]
            and json.loads(row.outputs)[0]["uri"] == info["artifact_uri"]
            and json.loads(row.input_manifest) == info["admitted_manifest"]
            for row in runs
        )
        state = session.get(metadb.RunState, info["run_id"])
        assert state is not None and state.status == "done"
        state_doc = json.loads(state.doc)
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
            {"destination_field": "id", "source_field": "id"},
        ]
        assert _lineage_receipt_identity(
            session, info["lineage_publication_key"]) == info["lineage_receipt"]
    restored_file = outputs_root / info["artifact_rel"]
    assert restored_file.is_file()
    assert restored_file.read_bytes() == info["artifact_bytes"]


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
            "sha": info["release_sha"], "db": "sqlite", "storage": "local",
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

        _switch_db(f"sqlite:///{restore_ws / 'dataplay.db'}")
        metadb.init_db()
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
                    _assert_revision_recovery(
                        info, outputs_root=restore_ws / "outputs",
                        exact_storage=exact_storage)
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
                          metadb.WorkspaceContainer, metadb.WorkspacePlacement,
                          metadb.CatalogLineageFact, metadb.CatalogPublicationEvent,
                          metadb.LocalResultArtifact, metadb.LocalResultReference,
                          metadb.LocalFileInputRevision,
                          metadb.CatalogLogicalDataset, metadb.ManagedLocalFileRevision,
                          metadb.RunInputAdmission, metadb.ObjectAttempt):
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
            "sha": info["release_sha"], "db": "postgresql", "storage": "s3",
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

        _switch_db(restore_url)
        metadb.init_db()
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
                    _assert_revision_recovery(
                        info, outputs_root=restore_ws / "outputs",
                        exact_storage=exact_storage)
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
                          metadb.WorkspaceContainer, metadb.WorkspacePlacement,
                          metadb.CatalogLineageFact, metadb.CatalogPublicationEvent,
                          metadb.LocalResultArtifact, metadb.LocalResultReference,
                          metadb.LocalFileInputRevision,
                          metadb.CatalogLogicalDataset, metadb.ManagedLocalFileRevision,
                          metadb.RunInputAdmission, metadb.ObjectAttempt):
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
