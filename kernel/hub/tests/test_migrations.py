"""Metadata migration startup contracts."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hub import metadb
from hub.settings import settings


@contextlib.contextmanager
def _isolated_metadata(url: str):
    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = url
    metadb._engine = metadb._Session = None
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def test_concurrent_fresh_sqlite_startup_is_serialized_across_48_processes(tmp_path):
    db_path = tmp_path / "metadata.db"
    env = os.environ.copy()
    env.update({
        "DP_DATABASE_URL": f"sqlite:///{db_path}",
        "DP_WORKSPACE": str(tmp_path),
        "DP_DATA_DIR": str(tmp_path / "data"),
    })
    env.pop("DP_AUTH_PASSWORD", None)
    command = [sys.executable, "-c", "from hub import metadb; metadb.init_db()"]
    processes = [
        subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(48)
    ]
    failures = []
    for index, process in enumerate(processes):
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode != 0:
            failures.append(f"process {index}: {stderr or stdout}")

    assert not failures, "\n".join(failures)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            metadb.expected_schema_head(),)
        assert connection.execute("SELECT id FROM users WHERE id = 'local'").fetchone() == ("local",)


def test_sqlite_lock_path_is_canonical_for_relative_symlink_and_uri_urls(tmp_path, monkeypatch):
    real_workspace = tmp_path / "real"
    real_workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_workspace, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    expected = f"{(real_workspace / 'metadata.db').resolve()}.migrate.lock"

    with _isolated_metadata("sqlite:///alias/metadata.db"):
        assert metadb._sqlite_file_lock_path() == expected
    with _isolated_metadata(f"sqlite:///file:{real_workspace / 'metadata.db'}?uri=true"):
        assert metadb._sqlite_file_lock_path() == expected


@pytest.mark.parametrize("url_name, expected_name", [
    ("metadata.db?mode=memory", "metadata.db"),
    ("file::memory:?cache=shared", "file::memory:"),
])
def test_sqlite_memory_syntax_without_uri_semantics_remains_disk_backed(
        url_name, expected_name, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{url_name}"
    expected_path = (tmp_path / expected_name).resolve()

    with _isolated_metadata(url):
        assert metadb._sqlite_is_memory_or_temporary() is False
        assert metadb._sqlite_file_lock_path() == f"{expected_path}.migrate.lock"
        metadb.init_db()
        with metadb.engine().connect() as connection:
            databases = connection.exec_driver_sql("PRAGMA database_list").all()
            database_path = next(row[2] for row in databases if row[1] == "main")
        assert Path(database_path).resolve() == expected_path

    assert expected_path.is_file()


@pytest.mark.parametrize("url", [
    "sqlite://",
    "sqlite:///:memory:",
    "sqlite:///file::memory:?cache=shared&uri=true",
    "sqlite:///file:shared-memory?mode=memory&cache=shared&uri=true",
])
def test_process_local_sqlite_databases_use_no_file_lock_and_share_the_migrated_connection(url):
    with _isolated_metadata(url):
        assert metadb._sqlite_file_lock_path() is None
        metadb.init_db()
        assert metadb.require_schema_at_head() == metadb.expected_schema_head()
        assert metadb.resolve_user("local") == "local"


def test_nonempty_unversioned_database_is_not_silently_stamped(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")

    with _isolated_metadata(f"sqlite:///{db_path}"):
        with pytest.raises(metadb.SchemaNotReadyError, match="non-empty.*valid Alembic revision"):
            metadb.migrate_db()

    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"users"}


def test_bootstrap_password_without_session_secret_is_rejected_not_discarded(tmp_path, monkeypatch):
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    monkeypatch.setenv("DP_AUTH_PASSWORD", "must-not-be-discarded")

    with _isolated_metadata(f"sqlite:///{tmp_path / 'metadata.db'}"):
        with pytest.raises(metadb.SchemaNotReadyError, match="requires a non-empty DP_AUTH_SECRET"):
            metadb.migrate_db()

    assert os.environ["DP_AUTH_PASSWORD"] == "must-not-be-discarded"


@pytest.mark.parametrize("secret, message", [
    ("   ", "configured but blank"),
    ("test", "known-weak/default"),
])
def test_migration_rejects_unusable_session_signing_secret(secret, message, tmp_path, monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", secret)
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)

    if not secret.strip():
        from hub import auth
        assert auth.auth_enabled() is False

    with _isolated_metadata(f"sqlite:///{tmp_path / 'metadata.db'}"):
        with pytest.raises(metadb.SchemaNotReadyError, match=message):
            metadb.migrate_db()


def test_fresh_session_auth_database_requires_login_capable_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "0123456789abcdef0123456789abcdef")
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)

    with _isolated_metadata(f"sqlite:///{tmp_path / 'metadata.db'}"):
        with pytest.raises(metadb.SchemaNotReadyError, match="no administrator has a login credential"):
            metadb.migrate_db()
        assert metadb.schema_at_head() is True
        with pytest.raises(metadb.SchemaNotReadyError, match="no administrator has a login credential"):
            metadb.init_db()


def test_internal_auth_mode_marker_does_not_require_a_login_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("DP_AUTH_MODE", "1")

    with _isolated_metadata(f"sqlite:///{tmp_path / 'worker.db'}"):
        metadb.init_db()
        assert metadb.require_schema_at_head() == metadb.expected_schema_head()
        assert metadb.user_password_hash(metadb.DEFAULT_USER_ID) is None


def test_non_sqlite_service_startup_checks_head_without_running_migrations(monkeypatch):
    calls: list[str] = []
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(metadb, "_is_sqlite_database", lambda: False)
    monkeypatch.setattr(metadb, "require_schema_at_head", lambda: calls.append("check") or "head")
    monkeypatch.setattr(metadb, "_upgrade_schema_and_bootstrap",
                        lambda: pytest.fail("service startup attempted migration"))
    monkeypatch.setattr(metadb, "reap_kernels", lambda: calls.append("reap-kernels"))
    monkeypatch.setattr(metadb, "reap_orphaned_runs", lambda: calls.append("reap-runs"))

    metadb.init_db()

    assert calls == ["check", "reap-kernels", "reap-runs"]


def test_non_sqlite_bootstrap_secret_is_rejected_outside_explicit_migration(monkeypatch):
    monkeypatch.setenv("DP_AUTH_PASSWORD", "migration-only-secret")
    monkeypatch.setattr(metadb, "_is_sqlite_database", lambda: False)
    monkeypatch.setattr(metadb, "require_schema_at_head", lambda: "head")

    with pytest.raises(metadb.SchemaNotReadyError, match="accepted only.*dataplay migrate"):
        metadb.init_db()


def test_explicit_migrate_is_the_only_non_sqlite_upgrade_path(monkeypatch):
    monkeypatch.setattr(metadb, "_is_sqlite_database", lambda: False)
    monkeypatch.setattr(metadb, "_upgrade_schema_and_bootstrap", lambda: "expected-head")

    assert metadb.migrate_db() == "expected-head"


def test_schema_check_detects_a_database_behind_head(tmp_path):
    from alembic import command

    with _isolated_metadata(f"sqlite:///{tmp_path / 'behind.db'}"):
        metadb.migrate_db()
        with metadb.engine().connect() as connection:
            command.downgrade(metadb._alembic_cfg(connection), "-1")
        assert metadb.schema_at_head() is False
        with pytest.raises(metadb.SchemaNotReadyError, match="not at required Alembic head"):
            metadb.require_schema_at_head()
