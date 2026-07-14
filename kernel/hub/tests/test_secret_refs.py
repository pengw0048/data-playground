"""SEC-03: secret references + SecretResolver — fixture secrets never land in the metadata DB."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app
from hub.secrets import (
    SecretResolveError,
    is_secret_ref,
    list_schemes,
    register_resolver,
    resolve_object_store,
    resolve_secret,
    unregister_resolver,
)
from hub.settings import settings
from hub.workload_env import initialize_ephemeral_metadata


client = TestClient(app)

FIXTURE_AGENT = "FIXTURE-SECRET-AGENT"
FIXTURE_ACCESS = "FIXTURE-SECRET-ACCESS"
FIXTURE_SECRET = "FIXTURE-SECRET-SECRETKEY"
FIXTURE_SESSION = "FIXTURE-SECRET-SESSION"
FIXTURE_PLUGIN = "FIXTURE-SECRET-PLUGIN"


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


def _raw_settings_rows() -> list[tuple[str, str]]:
    with metadb.session() as s:
        rows = s.execute(sa.text(
            "SELECT key, value FROM settings WHERE scope = 'global'"
        )).fetchall()
    return [(r[0], r[1]) for r in rows]


def _assert_fixture_absent(blob: str) -> None:
    for token in (FIXTURE_AGENT, FIXTURE_ACCESS, FIXTURE_SECRET, FIXTURE_SESSION, FIXTURE_PLUGIN):
        assert token not in blob, f"fixture secret {token!r} leaked into: {blob[:500]}"


def test_secret_refs_never_appear_as_fixture_values_in_db_or_api(monkeypatch, tmp_path):
    # AC1: write fixture secrets through env:/file: refs; scan every settings row and GET /settings.
    monkeypatch.setenv("DP_FIXTURE_AGENT_KEY", FIXTURE_AGENT)
    monkeypatch.setenv("DP_FIXTURE_ACCESS_KEY", FIXTURE_ACCESS)
    monkeypatch.setenv("DP_FIXTURE_SECRET_KEY", FIXTURE_SECRET)
    monkeypatch.setenv("DP_FIXTURE_SESSION_TOKEN", FIXTURE_SESSION)
    plugin_file = tmp_path / "plugin.token"
    plugin_file.write_text(FIXTURE_PLUGIN + "\n", encoding="utf-8")

    from hub.deps import get_deps
    deps = get_deps()
    fake = {"name": "dp_secretaf", "source": "drop-in",
            "config": [{"key": "token", "type": "password", "secret": True}]}
    deps.plugins.append(fake)
    try:
        assert client.put("/api/settings", json={
            "scope": "global", "key": "agentApiKey", "value": "env:DP_FIXTURE_AGENT_KEY",
        }).status_code == 200
        assert client.put("/api/settings", json={
            "scope": "global", "key": "objectStore",
            "value": {
                "accessKeyId": "env:DP_FIXTURE_ACCESS_KEY",
                "secretAccessKey": "env:DP_FIXTURE_SECRET_KEY",
                "sessionToken": "env:DP_FIXTURE_SESSION_TOKEN",
                "region": "us-east-1",
            },
        }).status_code == 200
        assert client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretaf.token",
            "value": f"file:{plugin_file}",
        }).status_code == 200

        body = client.get("/api/settings").json()
        _assert_fixture_absent(json.dumps(body))
        for key, raw in _raw_settings_rows():
            _assert_fixture_absent(raw)
            _assert_fixture_absent(key)
        assert body["global"]["agentApiKey"] == "env:DP_FIXTURE_AGENT_KEY"
        assert body["global"]["objectStore"]["sessionToken"] == "env:DP_FIXTURE_SESSION_TOKEN"
    finally:
        deps.plugins.remove(fake)
        client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": ""})
        client.put("/api/settings", json={"scope": "global", "key": "objectStore", "value": {}})
        metadb.set_setting("plugin.dp_secretaf.token", "", "global")


def test_secret_resolution_end_to_end(monkeypatch, tmp_path):
    # AC2: agent config, object-store resolve, and plugin reg.config each receive the fixture value.
    monkeypatch.setenv("DP_FIXTURE_AGENT_KEY", FIXTURE_AGENT)
    monkeypatch.setenv("DP_FIXTURE_ACCESS_KEY", FIXTURE_ACCESS)
    monkeypatch.setenv("DP_FIXTURE_SECRET_KEY", FIXTURE_SECRET)
    monkeypatch.setenv("DP_FIXTURE_SESSION_TOKEN", FIXTURE_SESSION)
    plugin_file = tmp_path / "plugin.token"
    plugin_file.write_text(FIXTURE_PLUGIN + "\n", encoding="utf-8")

    metadb.set_setting("agentApiKey", "env:DP_FIXTURE_AGENT_KEY", "global")
    metadb.set_setting("objectStore", {
        "accessKeyId": "env:DP_FIXTURE_ACCESS_KEY",
        "secretAccessKey": "env:DP_FIXTURE_SECRET_KEY",
        "sessionToken": "env:DP_FIXTURE_SESSION_TOKEN",
    }, "global")
    metadb.set_setting("plugin.dp_secretaf.token", f"file:{plugin_file}", "global")

    from hub.agent import _agent_config
    from hub.deps import Deps, Registry

    try:
        _, api_key, _ = _agent_config()
        assert api_key == FIXTURE_AGENT

        resolved = resolve_object_store(metadb.get_setting("objectStore", "global"))
        assert resolved["accessKeyId"] == FIXTURE_ACCESS
        assert resolved["secretAccessKey"] == FIXTURE_SECRET
        assert resolved["sessionToken"] == FIXTURE_SESSION

        deps = Deps(str(tmp_path / "ws"), str(tmp_path / "data"))
        deps._manifests["dp_secretaf"] = {"config": [{"key": "token", "secret": True}]}
        reg = Registry(deps)
        reg._pack = "dp_secretaf"
        assert reg.config("token") == FIXTURE_PLUGIN

        with pytest.raises(SecretResolveError, match="unset or empty"):
            resolve_secret("env:DP_MISSING_SECRET_VAR_XYZ")
        with pytest.raises(SecretResolveError, match="could not be resolved"):
            resolve_secret(f"file:{tmp_path / 'no-such-secret-file'}")
    finally:
        metadb.set_setting("agentApiKey", "", "global")
        metadb.set_setting("objectStore", {}, "global")
        metadb.set_setting("plugin.dp_secretaf.token", "", "global")


def test_ephemeral_metadata_stores_refs_not_fixture_secret(monkeypatch, tmp_path, caplog):
    # AC3: reference-backed object-store creds — fixture absent from logs and worker private DB.
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("DP_S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("DP_S3_KEY", FIXTURE_ACCESS)
    monkeypatch.setenv("DP_S3_SECRET", FIXTURE_SECRET)
    monkeypatch.setenv("UNRELATED_CONTROL_SECRET", "must-not-cross")

    with caplog.at_level(logging.DEBUG):
        # initialize_ephemeral_metadata rebinds DP_DATABASE_URL + metadb engine; restore afterwards.
        original_url = settings.database_url
        original_engine, original_session = metadb._engine, metadb._Session
        try:
            url = initialize_ephemeral_metadata(str(tmp_path / "worker"))
        finally:
            if metadb._engine is not None and metadb._engine is not original_engine:
                metadb._engine.dispose()
            settings.database_url = original_url
            metadb._engine, metadb._Session = original_engine, original_session
            monkeypatch.setenv("DP_DATABASE_URL", original_url)

    assert url.endswith("workload-metadata.db")
    _assert_fixture_absent(caplog.text)
    assert "must-not-cross" not in caplog.text

    db_path = url.removeprefix("sqlite:///")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    blob = json.dumps(rows)
    _assert_fixture_absent(blob)
    assert "must-not-cross" not in blob
    assert any(r[0] == "objectStore" for r in rows)
    store = json.loads(next(r[1] for r in rows if r[0] == "objectStore"))
    assert store["accessKeyId"] == "env:DP_S3_KEY"
    assert store["secretAccessKey"] == "env:DP_S3_SECRET"


def test_secret_ref_migration_clean_and_legacy(tmp_path):
    # AC4: clean DB upgrades; legacy plaintext secrets are deleted with operator instructions.
    clean = tmp_path / "clean.db"
    legacy = tmp_path / "legacy.db"

    def _upgrade_to(path, target: str) -> None:
        with _isolated_metadata(f"sqlite:///{path}"):
            command.upgrade(metadb._alembic_cfg(), target)

    _upgrade_to(clean, "0021_local_result_artifacts")
    _upgrade_to(clean, "0024_secret_refs")
    with sa.create_engine(f"sqlite:///{clean}").connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == (
            "0024_secret_refs")

    _upgrade_to(legacy, "0021_local_result_artifacts")
    engine = sa.create_engine(f"sqlite:///{legacy}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO settings (scope, scope_id, key, value) VALUES "
            "('global', '', 'agentApiKey', :v)"
        ), {"v": json.dumps("sk-LEGACY-AGENT")})
        conn.execute(sa.text(
            "INSERT INTO settings (scope, scope_id, key, value) VALUES "
            "('global', '', 'objectStore', :v)"
        ), {"v": json.dumps({
            "accessKeyId": "LEGACY-ACCESS",
            "secretAccessKey": "LEGACY-SECRET",
            "sessionToken": "LEGACY-SESSION",
            "region": "us-east-1",
        })})
        conn.execute(sa.text(
            "INSERT INTO settings (scope, scope_id, key, value) VALUES "
            "('global', '', 'plugin.dp_x.token', :v)"
        ), {"v": json.dumps("LEGACY-PLUGIN-TOKEN")})
        conn.execute(sa.text(
            "INSERT INTO settings (scope, scope_id, key, value) VALUES "
            "('global', '', 'plugin.dp_x.host', :v)"
        ), {"v": json.dumps("db.internal")})

    _upgrade_to(legacy, "0024_secret_refs")
    with engine.connect() as conn:
        rows = {r[0]: json.loads(r[1]) for r in conn.execute(
            sa.text("SELECT key, value FROM settings WHERE scope = 'global'")).fetchall()}
    assert "agentApiKey" not in rows
    assert "plugin.dp_x.token" not in rows
    assert rows.get("plugin.dp_x.host") == "db.internal"
    store = rows.get("objectStore") or {}
    assert "accessKeyId" not in store and "secretAccessKey" not in store
    assert "sessionToken" not in store
    assert store.get("region") == "us-east-1"
    assert "LEGACY" not in json.dumps(rows)


def test_pluggable_secret_resolver_scheme():
    # AC5: a fake scheme registers and is used without core changes.
    def _fake(name: str) -> str:
        return f"resolved-{name}"

    register_resolver("fake", _fake, replace=True)
    try:
        assert "fake" in list_schemes()
        assert resolve_secret("fake:widget") == "resolved-widget"
        assert is_secret_ref("fake:widget")
        from hub.deps import Deps, Registry
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reg = Registry(Deps(d, d))
            reg.add_secret_resolver("fake2", lambda n: f"via-reg-{n}")
            assert resolve_secret("fake2:x") == "via-reg-x"
            unregister_resolver("fake2")
    finally:
        unregister_resolver("fake")


def test_put_settings_rejects_plaintext_for_reference_backed_keys():
    # AC6: no plaintext write path remains via PUT for agentApiKey / objectStore / plugin secrets.
    from hub.deps import get_deps
    deps = get_deps()
    fake = {"name": "dp_secretreject", "source": "drop-in",
            "config": [{"key": "token", "type": "password", "secret": True}]}
    deps.plugins.append(fake)
    try:
        for key, value in (
            ("agentApiKey", "sk-raw"),
            ("plugin.dp_secretreject.token", "raw-token"),
        ):
            r = client.put("/api/settings", json={"scope": "global", "key": key, "value": value})
            assert r.status_code == 400, key
        r = client.put("/api/settings", json={
            "scope": "global", "key": "objectStore",
            "value": {"accessKeyId": "raw", "secretAccessKey": "env:OK", "sessionToken": "raw"},
        })
        assert r.status_code == 400
        assert client.put("/api/settings", json={
            "scope": "global", "key": "agentApiKey", "value": "env:OK_AGENT",
        }).status_code == 200
        assert metadb.get_setting("agentApiKey", "global") == "env:OK_AGENT"
    finally:
        deps.plugins.remove(fake)
        client.put("/api/settings", json={"scope": "global", "key": "agentApiKey", "value": ""})
