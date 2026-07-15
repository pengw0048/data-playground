"""Upgrade smoke: migrate a populated previous-schema DB to head (issue #115 / REL-01).

Until a previous tagged release exists, the "previous supported schema" is the prior committed
Alembic revision (``0020_object_attempt_lifecycle``). After the first release, replace
``PREVIOUS_SUPPORTED_REVISION`` with that release's head revision.

Covers SQLite always, and PostgreSQL when ``DP_TEST_DATABASE_URL`` is set (CI postgres job).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text

from hub import metadb
from hub.settings import settings

# Until v0.1.0 (or whichever first tag) exists as an installable prior release, pin the previous
# supported schema to the revision immediately before head. Keep this in lockstep with
# kernel/hub/migrations/versions/.
PREVIOUS_SUPPORTED_REVISION = "0020_object_attempt_lifecycle"

FIXTURE_CANVAS_ID = "cv_upgrade_smoke"
FIXTURE_CANVAS_NAME = "upgrade-smoke-canvas"
FIXTURE_RUN_ID = "rr_upgrade_smoke"
FIXTURE_CATALOG_URI = "file:///tmp/upgrade-smoke-events.parquet"
FIXTURE_CATALOG_NAME = "upgrade_smoke_events"
FIXTURE_CATALOG_FOLDER = "upgrade/smoke"


def _reset_engine() -> None:
    metadb.engine().dispose()
    metadb._engine = metadb._Session = None


def _reset_postgres(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    _reset_engine()


def _reset_sqlite() -> None:
    """Wipe the conftest throwaway SQLite file (settings.database_url is fixed at import)."""
    url = settings.database_url
    assert url.startswith("sqlite:///"), url
    path = url.removeprefix("sqlite:///")
    _reset_engine()
    for suffix in ("", "-wal", "-shm"):
        candidate = f"{path}{suffix}"
        if os.path.exists(candidate):
            os.unlink(candidate)


def _cli_migrate(workspace: str, env: dict) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "hub.cli", "migrate", "--workspace", workspace],
        env=env, text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _upgrade_to(revision: str) -> None:
    with metadb.engine().connect() as connection:
        connection.rollback()
        command.upgrade(metadb._alembic_cfg(connection), revision)


def _seed_fixture_rows() -> None:
    """Insert representative rows that must survive migrate-to-head (canvases, runs, catalog)."""
    doc = json.dumps({
        "id": FIXTURE_CANVAS_ID,
        "name": FIXTURE_CANVAS_NAME,
        "version": 1,
        "nodes": [],
        "edges": [],
    })
    catalog_doc = json.dumps({
        "id": "tbl_upgrade_smoke",
        "name": FIXTURE_CATALOG_NAME,
        "uri": FIXTURE_CATALOG_URI,
        "format": "parquet",
        "folder": FIXTURE_CATALOG_FOLDER,
        "columns": [{"name": "id", "type": "int64"}],
    })
    dialect = metadb.engine().dialect.name
    with metadb.engine().begin() as connection:
        if dialect == "postgresql":
            connection.execute(text(
                "INSERT INTO users (id, name, is_admin, token_epoch) "
                "VALUES (:id, :name, true, 0) ON CONFLICT (id) DO NOTHING"
            ), {"id": metadb.DEFAULT_USER_ID, "name": "Local"})
        else:
            connection.execute(text(
                "INSERT OR IGNORE INTO users (id, name, is_admin, token_epoch) "
                "VALUES (:id, :name, 1, 0)"
            ), {"id": metadb.DEFAULT_USER_ID, "name": "Local"})
        connection.execute(text(
            "INSERT INTO canvases (id, owner_id, name, version, doc, visibility) "
            "VALUES (:id, :owner, :name, 1, :doc, 'private')"
        ), {
            "id": FIXTURE_CANVAS_ID,
            "owner": metadb.DEFAULT_USER_ID,
            "name": FIXTURE_CANVAS_NAME,
            "doc": doc,
        })
        connection.execute(text(
            "INSERT INTO run_records (id, canvas_id, run_id, target_node_id, status, rows, ms) "
            "VALUES (:id, :canvas, :run_id, 'agg', 'done', 50, 12)"
        ), {
            "id": FIXTURE_RUN_ID,
            "canvas": FIXTURE_CANVAS_ID,
            "run_id": "run_upgrade_smoke",
        })
        connection.execute(text(
            "INSERT INTO catalog_entries (uri, name, doc, folder) "
            "VALUES (:uri, :name, :doc, :folder)"
        ), {
            "uri": FIXTURE_CATALOG_URI,
            "name": FIXTURE_CATALOG_NAME,
            "doc": catalog_doc,
            "folder": FIXTURE_CATALOG_FOLDER,
        })


def _assert_fixtures_intact() -> None:
    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, FIXTURE_CANVAS_ID)
        assert canvas is not None, "canvas fixture missing after migrate"
        assert canvas.name == FIXTURE_CANVAS_NAME
        assert FIXTURE_CANVAS_NAME in (canvas.doc or "")

        run = session.get(metadb.RunRecord, FIXTURE_RUN_ID)
        assert run is not None, "run_records fixture missing after migrate"
        assert run.canvas_id == FIXTURE_CANVAS_ID
        assert run.status == "done"
        assert run.rows == 50

        catalog = session.get(metadb.CatalogEntry, FIXTURE_CATALOG_URI)
        assert catalog is not None, "catalog_entries fixture missing after migrate"
        assert catalog.name == FIXTURE_CATALOG_NAME
        assert catalog.folder == FIXTURE_CATALOG_FOLDER
        folders = set(session.scalars(
            select(metadb.CatalogFolder.path).order_by(metadb.CatalogFolder.path)))
        assert {"upgrade", FIXTURE_CATALOG_FOLDER} <= folders


def _run_upgrade_smoke(tmp_path, *, postgres: bool) -> None:
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace, exist_ok=True)
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    # Always use settings.database_url — conftest freezes it at import (throwaway SQLite or
    # DP_TEST_DATABASE_URL). Mutating os.environ alone would not retarget metadb.engine().
    db_url = settings.database_url
    if postgres:
        assert db_url.startswith("postgresql"), db_url
        _reset_postgres(db_url)
    else:
        assert db_url.startswith("sqlite:///"), db_url
        _reset_sqlite()

    env = os.environ.copy()
    env.update({
        "DP_DATABASE_URL": db_url,
        "DP_WORKSPACE": workspace,
        "DP_DATA_DIR": data_dir,
        "DP_AUTH_SECRET": "0123456789abcdef0123456789abcdef",
        "DP_AUTH_PASSWORD": "upgrade-smoke-bootstrap",
    })
    env.pop("DP_AUTH_MODE", None)

    # Bring schema to the previous supported revision, then seed durable fixture rows.
    _upgrade_to(PREVIOUS_SUPPORTED_REVISION)
    assert metadb._current_schema_heads() == (PREVIOUS_SUPPORTED_REVISION,)
    with metadb.session() as session:
        if session.get(metadb.User, metadb.DEFAULT_USER_ID) is None:
            session.add(metadb.User(id=metadb.DEFAULT_USER_ID, name="Local", is_admin=True))
    _seed_fixture_rows()

    # Release migration path: dataplay migrate → head.
    _cli_migrate(workspace, env)
    _reset_engine()
    assert metadb.require_schema_at_head() == metadb.expected_schema_head()
    assert metadb.expected_schema_head() != PREVIOUS_SUPPORTED_REVISION
    _assert_fixtures_intact()


def test_sqlite_upgrade_preserves_fixture_data(tmp_path, monkeypatch):
    monkeypatch.delenv("DP_TEST_DATABASE_URL", raising=False)
    if not settings.database_url.startswith("sqlite:///"):
        pytest.skip("suite is running against a non-SQLite DP_TEST_DATABASE_URL")
    _run_upgrade_smoke(tmp_path, postgres=False)


@pytest.mark.skipif(not os.environ.get("DP_TEST_DATABASE_URL"), reason="requires dedicated Postgres")
def test_postgres_upgrade_preserves_fixture_data(tmp_path):
    assert settings.database_url.startswith("postgresql")
    _run_upgrade_smoke(tmp_path, postgres=True)
