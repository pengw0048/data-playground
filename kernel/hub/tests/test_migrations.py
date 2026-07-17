"""Metadata migration startup contracts."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

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


def _normalized_default(value) -> str | None:
    if value is None:
        return None
    value = getattr(value, "arg", value)
    rendered = str(value).strip()
    if len(rendered) >= 2 and rendered[0] == rendered[-1] == "'":
        rendered = rendered[1:-1].replace("''", "'")
    normalized = rendered.lower()
    return "now()" if normalized == "current_timestamp" else normalized


def test_migration_graph_has_one_linear_head():
    scripts = ScriptDirectory.from_config(metadb._alembic_cfg())
    revisions = list(scripts.walk_revisions())

    assert [(revision.revision, revision.down_revision) for revision in revisions] == [
        ("0012_linear_checkpoint_admission", "0011_external_wait_publication"),
        ("0011_external_wait_publication", "0010_durable_external_waits"),
        ("0010_durable_external_waits", "0009_durable_local_write_tasks"),
        ("0009_durable_local_write_tasks", "0008_managed_local_lance_writes"),
        ("0008_managed_local_lance_writes", "0007_workspace_provider_bindings"),
        ("0007_workspace_provider_bindings", "0006_typed_local_writes"),
        ("0006_typed_local_writes", "0005_profile_output_ports"),
        ("0005_profile_output_ports", "0004_local_run_input_admissions"),
        ("0004_local_run_input_admissions", "0003_repair_historical_metadata"),
        ("0003_repair_historical_metadata", "0002_managed_file_revs"),
        ("0002_managed_file_revs", "0001_schema_baseline"),
        ("0001_schema_baseline", None),
    ]
    assert scripts.get_heads() == ["0012_linear_checkpoint_admission"]
    assert metadb.expected_schema_head() == "0012_linear_checkpoint_admission"


def test_committed_migration_revisions_are_immutable():
    versions_path = Path(metadb._MIGRATIONS_DIR) / "versions"
    expected_hashes = {
        "0001_schema_baseline.py": "f8a793dd0af47e189939f1ce41ec39ae336009bf353e8ac8147fd961386c1e96",
        "0002_managed_local_file_revisions.py": (
            "c69ae2c9e2b6311261b694ecdd057008d5d6ffccd7e88bd3cbadfe04af7095f5"
        ),
        "0003_repair_historical_metadata.py": (
            "66165953789dbc0d2c46c8c8a5f5605c0e9c62b0393235062c8929500aca5b54"
        ),
        "0004_local_run_input_admissions.py": (
            "d47eb32ac70084eab237d48a9f5678bfdc4d09057e47d8cbb727e9d4770026a1"
        ),
        "0005_profile_output_ports.py": (
            "af30394298fed43a53a7be86f23256d63a4d97217d0fceb1718d77c81d351547"
        ),
        "0006_typed_local_writes.py": (
            "132a4a8ff77a5a48ad45538beaded4480b45b7bf4006fe504e02e1481845507c"
        ),
        "0007_workspace_provider_bindings.py": (
            "5bd4feb0205b08e19275f6513644b347a5d5fd0fe1d45d9bd5e47a3fc1b3800c"
        ),
        "0008_managed_local_lance_writes.py": (
            "3aef01923a3b252285a78fbbce9d8173630264bf51315934b90aa4601454e540"
        ),
        "0009_durable_local_write_tasks.py": (
            "2ed6efefd51b1ed51f5487742b8acae0e78c1f960a4f7454687ddbf83ee6f2e1"
        ),
        "0010_durable_external_waits.py": (
            "183506ed4f43142cbaff7e63ee47267d5c9bcd7969c94efb28e62e3b84a4d7cf"
        ),
        "0011_external_wait_publication.py": (
            "bc779148f2d745f0ef0a0e227dd1877eb08b92d7ac8184f28c6608e3fefebfaf"
        ),
        "0012_linear_checkpoint_admission.py": (
            "83237f57a39bdb92aa52d660aea9d7c4dddfc4bf7636ff18523b3fcec418214f"
        ),
    }
    revision_paths = {path.name: path for path in versions_path.glob("*.py")}

    assert revision_paths.keys() == expected_hashes.keys(), (
        "record every new forward migration in the immutable revision checksum guard"
    )
    for name, expected_hash in expected_hashes.items():
        assert hashlib.sha256(revision_paths[name].read_bytes()).hexdigest() == expected_hash, (
            "committed migration revisions are immutable; add a forward migration instead"
        )


def test_linear_checkpoint_downgrade_rejects_retained_hidden_rows(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'checkpoint-downgrade.db'}"):
        metadb.migrate_db()
        now = metadb._now()
        with metadb.session() as session:
            session.add(metadb.User(id="checkpoint-owner", name="Checkpoint owner"))
            session.flush()
            session.add(metadb.Canvas(
                id="checkpoint-canvas", owner_id="checkpoint-owner", name="Checkpoint", doc="{}"))
            session.flush()
            session.add(metadb.DurableTask(
                id="checkpoint-task", owner_id="checkpoint-owner", canvas_id="checkpoint-canvas",
                submission_id="submission", intent_sha256="a" * 64,
                target_node_id="final", task_kind="linear_checkpoint_write",
                backend_kind="local", graph_doc="{}", input_manifest="[]", write_intent="{}",
                status="queued", status_doc="{}", created_at=now, updated_at=now))
        with metadb.engine().connect() as connection:
            with pytest.raises(RuntimeError, match="cannot downgrade"):
                command.downgrade(
                    metadb._alembic_cfg(connection), "0011_external_wait_publication")
        assert metadb._current_schema_heads() == ("0012_linear_checkpoint_admission",)

        with metadb.session() as session:
            session.delete(session.get(metadb.DurableTask, "checkpoint-task"))
        with metadb.engine().connect() as connection:
            command.downgrade(
                metadb._alembic_cfg(connection), "0011_external_wait_publication")
            assert "durable_checkpoints" not in inspect(connection).get_table_names()
            command.upgrade(metadb._alembic_cfg(connection), "head")


def test_historical_baseline_upgrade_repairs_workspace_metadata_without_data_loss(tmp_path):
    """Exercise the exact old schema shape instead of fixing committed revision 0001 in place."""
    with _isolated_metadata(f"sqlite:///{tmp_path / 'historical.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0001_schema_baseline")

        # These are exactly the post-baseline additions that stranded databases created at 09339e9
        # without. Keep this fixture explicit so the regression exercises a real historical shape.
        with metadb.engine().begin() as connection:
            connection.execute(sa.text("DROP TABLE workspace_placements"))
            connection.execute(sa.text("DROP TABLE workspace_containers"))
            connection.execute(sa.text("ALTER TABLE run_records DROP COLUMN profile"))
            connection.execute(sa.text("""
                INSERT INTO canvases (id, owner_id, name, version, doc, visibility, created_at, updated_at)
                VALUES ('historical-canvas', 'local', 'Historical', 7, '{\"nodes\":[]}', 'private',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """))
            connection.execute(sa.text("""
                INSERT INTO run_records (id, canvas_id, status, outputs, created_at)
                VALUES ('historical-run', 'historical-canvas', 'ok', '[]', CURRENT_TIMESTAMP)
            """))
            connection.execute(sa.text("""
                INSERT INTO catalog_entries (uri, registration_id, name, doc, updated_at)
                VALUES ('file:///historical.parquet', 'historical-catalog-entry-00000001',
                        'Historical catalog entry', '{}', CURRENT_TIMESTAMP)
            """))
            connection.execute(sa.text("""
                INSERT INTO settings (scope, scope_id, key, value)
                VALUES ('global', '', 'historical.setting', :value)
            """), {"value": '{"preserved":true}'})

        assert metadb._current_schema_heads() == ("0001_schema_baseline",)
        assert metadb.migrate_db() == metadb.expected_schema_head()
        metadb.init_db()  # A repaired local database must restart at head cleanly.

        with metadb.engine().connect() as connection:
            inspector = inspect(connection)
            assert {"workspace_containers", "workspace_placements"} <= set(inspector.get_table_names())
            assert "profile" in {column["name"] for column in inspector.get_columns("run_records")}
            assert {index["name"] for index in inspector.get_indexes("workspace_containers")} >= {
                "ix_workspace_containers_parent_id"
            }
            assert {index["name"] for index in inspector.get_indexes("workspace_placements")} >= {
                "ix_workspace_placements_container_id"
            }
            assert connection.execute(sa.text("""
                SELECT id, name FROM workspace_containers WHERE id = 'workspace-local-root'
            """)).one() == ("workspace-local-root", "Workspace")
            assert connection.execute(sa.text("SELECT count(*) FROM workspace_containers")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT version, doc FROM canvases WHERE id = 'historical-canvas'")) \
                .one() == (7, '{"nodes":[]}')
            assert connection.execute(sa.text("SELECT status, outputs FROM run_records WHERE id = 'historical-run'")) \
                .one() == ("ok", "[]")
            assert connection.execute(sa.text("SELECT name FROM catalog_entries WHERE uri = 'file:///historical.parquet'")) \
                .scalar_one() == "Historical catalog entry"
            assert connection.execute(sa.text("SELECT value FROM settings WHERE key = 'historical.setting'")) \
                .scalar_one() == '{"preserved":true}'

            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "target_metadata": metadb.Base.metadata},
            )
            assert compare_metadata(context, metadb.Base.metadata) == []

        assert metadb.migrate_db() == metadb.expected_schema_head()


def test_fresh_sqlite_baseline_matches_runtime_metadata(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'baseline.db'}"):
        metadb.migrate_db()
        with metadb.engine().connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "target_metadata": metadb.Base.metadata},
            )
            assert compare_metadata(context, metadb.Base.metadata) == []

            installation = connection.execute(sa.text(
                "SELECT id, owner_token, storage_namespace FROM installation_identity"
            )).one()
            assert installation.id == 1
            assert len(installation.owner_token) == 32
            assert installation.storage_namespace
            local_registry = connection.execute(sa.text(
                "SELECT id, owner_token FROM local_result_registry"
            )).one()
            assert local_registry.id == 1
            assert len(local_registry.owner_token) == 32

            inspector = inspect(connection)
            for table in metadb.Base.metadata.sorted_tables:
                actual_columns = {
                    column["name"]: column
                    for column in inspector.get_columns(table.name)
                }
                for column in table.columns:
                    assert _normalized_default(actual_columns[column.name]["default"]) == (
                        _normalized_default(column.server_default)
                    ), f"server default drift for {table.name}.{column.name}"

                expected_checks = {
                    (constraint.name, str(constraint.sqltext))
                    for constraint in table.constraints
                    if isinstance(constraint, sa.CheckConstraint)
                }
                actual_checks = {
                    (constraint["name"], constraint["sqltext"])
                    for constraint in inspector.get_check_constraints(table.name)
                }
                assert actual_checks == expected_checks, (
                    f"check constraint drift for {table.name}"
                )

                expected_uniques = {
                    (constraint.name, tuple(column.name for column in constraint.columns))
                    for constraint in table.constraints
                    if isinstance(constraint, sa.UniqueConstraint)
                }
                actual_uniques = {
                    (constraint["name"], tuple(constraint["column_names"]))
                    for constraint in inspector.get_unique_constraints(table.name)
                }
                assert actual_uniques == expected_uniques, (
                    f"unique constraint drift for {table.name}"
                )


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
    results = {}
    timed_out = set()
    deadline = time.monotonic() + 150
    try:
        for index, process in enumerate(processes):
            remaining = deadline - time.monotonic()
            if process.poll() is None and remaining <= 0:
                timed_out.add(index)
                failures.append(f"process {index}: exceeded the shared 150-second deadline")
                continue
            try:
                results[index] = process.communicate(timeout=max(0, remaining))
            except subprocess.TimeoutExpired:
                timed_out.add(index)
                failures.append(f"process {index}: exceeded the shared 150-second deadline")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for index, process in enumerate(processes):
            if index not in results:
                results[index] = process.communicate(timeout=5)

    for index, process in enumerate(processes):
        if process.returncode != 0 and index not in timed_out:
            stdout, stderr = results[index]
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
