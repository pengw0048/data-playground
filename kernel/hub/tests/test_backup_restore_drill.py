"""OPS-03 backup / isolated-restore drill — fixture backup, restore, isolation, RPO/RTO evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine.url import make_url

from hub import handoff, metadb
from hub.models import RunOutput
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

    metadb.catalog_upsert_entry(
        parent_uri, "drill-parent",
        {"id": "drill_parent", "name": "drill-parent", "uri": parent_uri,
         "columns": [{"name": "id", "type": "int64"}]})
    metadb.catalog_upsert_entry(
        child_uri, "drill-child",
        {"id": "drill_child", "name": "drill-child", "uri": child_uri,
         "columns": [{"name": "id", "type": "int64"}]},
        parents=[parent_uri], pipeline="backup-restore-drill")
    metadb.catalog_add_edge(parent_uri, child_uri, pipeline="backup-restore-drill", column="id")

    run_id = f"drill-run-{uuid.uuid4().hex}"
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
        "parent_uri": parent_uri,
        "child_uri": child_uri,
        "run_id": run_id,
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
        assert json.loads(canvas.doc)["nodes"][0]["id"] == "n1"
        runs = list(session.scalars(
            select(metadb.RunRecord).where(metadb.RunRecord.canvas_id == info["canvas_id"])))
        assert any(
            row.run_id == info["run_id"]
            and json.loads(row.outputs)[0]["uri"] == info["artifact_uri"]
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

    assert metadb.catalog_get(info["parent_uri"]) is not None
    assert metadb.catalog_get(info["child_uri"]) is not None
    edges = metadb.catalog_edges()
    assert any(
        edge["parent"] in (info["parent_uri"], "drill_parent")
        and edge["child"] in (info["child_uri"], "drill_child")
        for edge in edges
    )
    restored_file = outputs_root / info["artifact_rel"]
    assert restored_file.is_file()
    assert restored_file.read_bytes() == info["artifact_bytes"]


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
        f"artifact={info['artifact_uri']} "
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
            _assert_fixture_readable(info, outputs_root=restore_ws / "outputs")
            _assert_isolation_applied(info, replacement, provider)
            # Source absolute artifact path is unchanged; restored relative tree is also complete.
            assert (restore_ws / "outputs" / info["artifact_rel"]).is_file()
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
                          metadb.CatalogEdge, metadb.LocalResultArtifact, metadb.ObjectAttempt):
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
            # Artifact URI remains the source absolute path (still present and readable);
            # restored outputs tree is verified by relative layout.
            assert Path(info["artifact_uri"]).is_file()
            _assert_fixture_readable(info, outputs_root=restore_ws / "outputs")
            _assert_isolation_applied(info, replacement, provider)
        finally:
            monkeypatch.delenv("DP_STORAGE_NAMESPACE", raising=False)

        _emit_evidence(info, int((time.perf_counter() - restore_start) * 1000))
        _close_db()

        _switch_db(url)
        metadb.init_db()
        with metadb.session() as session:
            for model in (metadb.Canvas, metadb.CatalogEntry, metadb.RunRecord,
                          metadb.CatalogEdge, metadb.LocalResultArtifact, metadb.ObjectAttempt):
                assert session.scalar(select(func.count()).select_from(model)) == \
                    source_row_counts[model.__tablename__]
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
