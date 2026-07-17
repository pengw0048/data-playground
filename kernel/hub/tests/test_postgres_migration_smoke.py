"""Real PostgreSQL release-contract smoke test (enabled only by DP_TEST_DATABASE_URL)."""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import os
import subprocess
import sys
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from hub import metadb


def _reset_postgres(url: str):
    """Return a clean engine for this job's dedicated PostgreSQL database."""
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    metadb.engine().dispose()
    metadb._engine = metadb._Session = None
    return admin_engine


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_cli_migration_and_service_startup_contract(tmp_path):
    url = os.environ["DP_TEST_DATABASE_URL"]
    assert url.startswith("postgresql"), "DP_TEST_DATABASE_URL must name a dedicated Postgres database"

    # This job owns the dedicated database. Reset it so the CLI proves a genuinely fresh migration,
    # independent of conftest's normal test-database setup.
    admin_engine = _reset_postgres(url)
    admin_engine.dispose()

    base_env = os.environ.copy()
    base_env.update({
        "DP_DATABASE_URL": url,
        "DP_WORKSPACE": str(tmp_path),
        "DP_DATA_DIR": str(tmp_path / "data"),
        "DP_AUTH_SECRET": "0123456789abcdef0123456789abcdef",
    })
    base_env.pop("DP_AUTH_MODE", None)

    first_env = {**base_env, "DP_AUTH_PASSWORD": "postgres-bootstrap-test"}
    first = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", str(tmp_path)],
        env=first_env, text=True, capture_output=True, timeout=60,
    )
    assert first.returncode == 0, first.stderr
    assert metadb.require_schema_at_head() == metadb.expected_schema_head()
    with metadb.engine().connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "target_metadata": metadb.Base.metadata},
        )
        assert compare_metadata(context, metadb.Base.metadata) == []
    with metadb.session() as session:
        admin = session.get(metadb.User, metadb.DEFAULT_USER_ID)
        assert admin is not None and admin.is_admin and admin.password_hash

    service_env = base_env.copy()
    service_env.pop("DP_AUTH_PASSWORD", None)
    ready_service = subprocess.run(
        [sys.executable, "-c", "from hub import metadb; metadb.init_db()"],
        env=service_env, text=True, capture_output=True, timeout=30,
    )
    assert ready_service.returncode == 0, ready_service.stderr

    with metadb.engine().connect() as connection:
        command.downgrade(metadb._alembic_cfg(connection), "0002_managed_file_revs")
    with metadb.engine().begin() as connection:
        connection.execute(text("DROP TABLE workspace_placements"))
        connection.execute(text("DROP TABLE workspace_containers"))
        connection.execute(text("ALTER TABLE run_records DROP COLUMN profile"))
    behind = metadb._current_schema_heads()
    assert behind == ("0002_managed_file_revs",)

    service = subprocess.run(
        [sys.executable, "-c", "from hub import metadb; metadb.init_db()"],
        env=service_env, text=True, capture_output=True, timeout=30,
    )
    assert service.returncode != 0, "service startup migrated a behind Postgres schema"
    assert "metadata schema is not at required Alembic head" in (service.stderr + service.stdout)
    assert metadb._current_schema_heads() == behind

    # The explicit migration command repairs the supported historical schema without service startup
    # silently mutating a behind production schema.
    restore = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", str(tmp_path)],
        env=first_env, text=True, capture_output=True, timeout=60,
    )
    assert restore.returncode == 0, restore.stderr
    assert metadb.schema_at_head() is True
    with metadb.engine().connect() as connection:
        assert "profile" in {
            column.name for column in metadb.Base.metadata.tables["run_records"].columns
        }
        assert "profile" in {
            column["name"] for column in connection.dialect.get_columns(connection, "run_records")
        }
        assert connection.execute(text("""
            SELECT id FROM workspace_containers WHERE id = 'workspace-local-root'
        """)).scalar_one() == "workspace-local-root"


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_linear_checkpoint_db_time_fencing_and_reservation_race(tmp_path):
    suffix = uuid.uuid4().hex
    uid, canvas_id, submission = f"cp-user-{suffix}", f"cp-canvas-{suffix}", str(uuid.uuid4())
    task_id = metadb.local_run_submission_id(uid, canvas_id, submission)
    key = f"write:{task_id}"
    graph = {"id": canvas_id, "version": 1, "nodes": [
        {"id": "checkpoint", "type": "write", "data": {
            "title": "checkpoint", "config": {"filename": "checkpoint.parquet"}}},
        {"id": "final", "type": "write", "data": {
            "title": "final", "config": {"filename": "final.parquet"}}},
    ], "edges": []}
    intent = {
        "destination": {"logicalUri": f"/tmp/{suffix}/final.parquet", "name": "final",
                        "provider": "managed-local-file"},
        "mode": "create", "expectedSchema": [], "idempotencyKey": key,
        "partitions": [], "provenance": {"publication": {
            "idempotencyKey": key, "runId": task_id, "producer": canvas_id,
            "producerVersion": 1, "stepId": "final", "provenance": "run",
            "fieldMappings": []}, "parents": []}}
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Postgres checkpoint owner"))
        session.flush()
        session.add(metadb.Canvas(id=canvas_id, owner_id=uid, name="Checkpoint", doc="{}"))
    admission, _ = metadb.submit_linear_checkpoint_task(
        uid=uid, canvas_id=canvas_id, submission_id=submission,
        final_target_node_id="final", checkpoint_id=f"cp:{suffix}",
        checkpoint_node_id="checkpoint", output_port_id="out",
        task_intent_sha256="a" * 64, graph_prefix_sha256="b" * 64,
        input_manifest_sha256=hashlib.sha256(b"[]").hexdigest(),
        graph_doc=graph, input_manifest=[], write_intent=intent)
    first = metadb.claim_linear_checkpoint_task(admission["task_id"], "expired-owner")
    old_attempt = first["attempts"][-1]
    with metadb.session() as session:
        session.get(metadb.DurableTaskAttempt, old_attempt["id"]).lease_until = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1))
    stale = {
        "task_id": admission["task_id"], "attempt_id": old_attempt["id"],
        "owner_token": "expired-owner", "namespace_id": uuid.uuid4().hex,
        "storage_root": str(tmp_path / ".dp-results"),
        "writer_token": uuid.uuid4().hex, "lock_token": uuid.uuid4().hex}
    with pytest.raises(RuntimeError, match="stale or fenced"):
        metadb.reserve_linear_checkpoint_candidate(**stale)

    current = metadb.claim_linear_checkpoint_task(admission["task_id"], "current-owner")
    attempt_id = current["attempts"][-1]["id"]
    namespace, lock_token = uuid.uuid4().hex, uuid.uuid4().hex

    def reserve(writer_token):
        return metadb.reserve_linear_checkpoint_candidate(
            task_id=admission["task_id"], attempt_id=attempt_id,
            owner_token="current-owner", namespace_id=namespace,
            storage_root=str(tmp_path / ".dp-results"),
            writer_token=writer_token, lock_token=lock_token)

    writers = [uuid.uuid4().hex for _ in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reserve, writer) for writer in writers]
    winners, errors = [], []
    for future in futures:
        try:
            winners.append(future.result())
        except Exception as exc:
            errors.append(exc)
    assert len(winners) == 1
    assert len(errors) == 7
    assert all(
        isinstance(error, RuntimeError)
        and "changed exact authority" in str(error)
        for error in errors
    )
    assert metadb.linear_checkpoint_candidate(admission["task_id"]) == winners[0]
