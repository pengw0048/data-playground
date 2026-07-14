"""Real PostgreSQL release-contract smoke test (enabled only by DP_TEST_DATABASE_URL)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

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
def test_postgres_0021_to_0022_preserves_history_and_backfills_terminal_fences():
    """Exercise the deployed PostgreSQL data-upgrade path, not only a fresh-schema upgrade."""
    url = os.environ["DP_TEST_DATABASE_URL"]
    assert url.startswith("postgresql"), "DP_TEST_DATABASE_URL must name a dedicated Postgres database"
    engine = _reset_postgres(url)

    with engine.connect() as connection:
        command.upgrade(metadb._alembic_cfg(connection), "0021_local_result_artifacts")
    active_doc = '{"run_id":"legacy-live","status":"running","per_node":[]}'
    terminal_docs = {
        "legacy-done": '{"run_id":"legacy-done","status":"done","per_node":[]}',
        "legacy-failed": '{"run_id":"legacy-failed","status":"failed","per_node":[]}',
        "legacy-cancelled": (
            '{"run_id":"legacy-cancelled","status":"cancelled","per_node":[]}'
        ),
    }
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, name) VALUES ('legacy-user', 'Legacy User')"
        ))
        connection.execute(text(
            "INSERT INTO canvases (id, owner_id, name, version, doc) "
            "VALUES ('legacy-canvas', 'legacy-user', 'Legacy', 1, '{}')"
        ))
        connection.execute(text("""
            INSERT INTO run_records
                (id, canvas_id, run_id, status, rows, error, output_uri, created_at)
            VALUES
                ('history-older', 'legacy-canvas', 'duplicate-run', 'failed', 3,
                 'original failure', 's3://legacy/older', '2026-07-01 00:00:00+00'),
                ('history-newer', 'legacy-canvas', 'duplicate-run', 'done', 7,
                 NULL, 's3://legacy/newer', '2026-07-02 00:00:00+00'),
                ('history-undated', 'legacy-canvas', 'duplicate-run', 'cancelled', 5,
                 'legacy clock unavailable', 's3://legacy/undated', NULL),
                ('history-unlinked', 'legacy-canvas', NULL, 'done', 11,
                 NULL, 's3://legacy/unlinked', '2026-07-03 00:00:00+00')
        """))
        connection.execute(text(
            "INSERT INTO run_states (run_id, status, doc) "
            "VALUES ('legacy-live', 'running', :doc)"
        ), {"doc": active_doc})
        connection.execute(text("""
            INSERT INTO run_states
                (run_id, canvas_id, status, doc, created_by, auth_canvas_id)
            VALUES (:run_id, 'legacy-canvas', :status, :doc, 'legacy-user', 'legacy-canvas')
        """), [
            {"run_id": run_id, "status": run_id.removeprefix("legacy-"), "doc": doc}
            for run_id, doc in terminal_docs.items()
        ])

    with engine.connect() as connection:
        command.upgrade(metadb._alembic_cfg(connection), "0022_backend_jobs")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == \
            "0022_backend_jobs"
        history = {
            row.id: (row.run_id, row.status, row.rows, row.error, row.output_uri)
            for row in connection.execute(text("""
                SELECT id, run_id, status, rows, error, output_uri
                FROM run_records ORDER BY id
            """))
        }
        assert history == {
            "history-newer": ("duplicate-run", "done", 7, None, "s3://legacy/newer"),
            "history-older": (None, "failed", 3, "original failure", "s3://legacy/older"),
            "history-undated": (
                None, "cancelled", 5, "legacy clock unavailable", "s3://legacy/undated"
            ),
            "history-unlinked": (None, "done", 11, None, "s3://legacy/unlinked"),
        }
        states = {
            row.run_id: row.doc
            for row in connection.execute(text(
                "SELECT run_id, doc FROM run_states ORDER BY run_id"
            ))
        }
        assert states == {"legacy-live": active_doc, **terminal_docs}
        fences = {
            row.run_id: (row.status, row.created_by, row.auth_canvas_id, row.canvas_id)
            for row in connection.execute(text(
                "SELECT run_id, status, created_by, auth_canvas_id, canvas_id "
                "FROM run_terminal_fences ORDER BY run_id"
            ))
        }
        assert fences == {
            "legacy-cancelled": (
                "cancelled", "legacy-user", "legacy-canvas", "legacy-canvas",
            ),
            "legacy-done": ("done", "legacy-user", "legacy-canvas", "legacy-canvas"),
            "legacy-failed": ("failed", "legacy-user", "legacy-canvas", "legacy-canvas"),
        }
    with pytest.raises(IntegrityError) as duplicate_error:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO run_records (id, canvas_id, run_id, status, created_at)
                VALUES ('history-conflict', 'legacy-canvas', 'duplicate-run', 'done', CURRENT_TIMESTAMP)
            """))
    assert "uq_run_record_canvas_run" in str(duplicate_error.value)
    # PostgreSQL unique constraints allow multiple NULL legacy links; retain that migration contract.
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO run_records (id, canvas_id, run_id, status, created_at)
            VALUES ('history-unlinked-2', 'legacy-canvas', NULL, 'failed', CURRENT_TIMESTAMP)
        """))
    engine.dispose()


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
    with metadb.session() as session:
        admin = session.get(metadb.User, metadb.DEFAULT_USER_ID)
        assert admin is not None and admin.is_admin and admin.password_hash

    with metadb.engine().connect() as connection:
        command.downgrade(metadb._alembic_cfg(connection), "-1")
    behind = metadb._current_schema_heads()
    assert behind and behind != (metadb.expected_schema_head(),)

    service_env = base_env.copy()
    service_env.pop("DP_AUTH_PASSWORD", None)
    service = subprocess.run(
        [sys.executable, "-c", "from hub import metadb; metadb.init_db()"],
        env=service_env, text=True, capture_output=True, timeout=30,
    )
    assert service.returncode != 0, "service startup migrated a behind Postgres schema"
    assert "metadata schema is not at required Alembic head" in (service.stderr + service.stdout)
    assert metadb._current_schema_heads() == behind

    restore = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", str(tmp_path)],
        env=service_env, text=True, capture_output=True, timeout=60,
    )
    assert restore.returncode == 0, restore.stderr
    assert metadb.schema_at_head() is True
