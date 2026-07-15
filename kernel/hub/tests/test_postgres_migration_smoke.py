"""Real PostgreSQL release-contract smoke test (enabled only by DP_TEST_DATABASE_URL)."""

from __future__ import annotations

import os
import subprocess
import sys

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
        command.downgrade(metadb._alembic_cfg(connection), "-1")
    behind = metadb._current_schema_heads()
    assert behind == ()

    service = subprocess.run(
        [sys.executable, "-c", "from hub import metadb; metadb.init_db()"],
        env=service_env, text=True, capture_output=True, timeout=30,
    )
    assert service.returncode != 0, "service startup migrated a behind Postgres schema"
    assert "metadata schema is not at required Alembic head" in (service.stderr + service.stdout)
    assert metadb._current_schema_heads() == behind

    # A pre-baseline database is intentionally unsupported. Recreate the dedicated database instead
    # of weakening the migration guard that rejects non-empty, unversioned metadata stores.
    reset_engine = _reset_postgres(url)
    reset_engine.dispose()
    restore = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", str(tmp_path)],
        env=first_env, text=True, capture_output=True, timeout=60,
    )
    assert restore.returncode == 0, restore.stderr
    assert metadb.schema_at_head() is True
