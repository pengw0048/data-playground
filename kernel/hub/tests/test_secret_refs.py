"""SEC-03: secret references + SecretResolver — fixture secrets never land in the metadata DB."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app
from hub.secrets import (
    SecretResolveError,
    is_secret_ref,
    list_schemes,
    parse_secret_ref,
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


def _raw_secret_metadata_rows() -> list[tuple[str, str]]:
    with metadb.session() as s:
        settings_rows = s.execute(sa.text(
            "SELECT key, value FROM settings WHERE scope = 'global'"
        )).fetchall()
        cred_rows = s.execute(sa.text("SELECT id, fields_json FROM creds")).fetchall()
    return [(r[0], r[1]) for r in (*settings_rows, *cred_rows)]


def _assert_fixture_absent(blob: str) -> None:
    for token in (FIXTURE_AGENT, FIXTURE_ACCESS, FIXTURE_SECRET, FIXTURE_SESSION, FIXTURE_PLUGIN):
        assert token not in blob, f"fixture secret {token!r} leaked into: {blob[:500]}"


def test_secret_refs_never_appear_as_fixture_values_in_db_or_api(monkeypatch, tmp_path):
    # AC1: write fixture secrets through canonical Cred/plugin APIs, then scan DB and API responses.
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
    agent_id = store_id = None
    try:
        agent = client.post("/api/creds", json={
            "name": "fixture Agent", "kind": "agent",
            "fields": {"apiKey": "env:DP_FIXTURE_AGENT_KEY"},
        })
        assert agent.status_code == 200
        agent_id = agent.json()["id"]
        store = client.post("/api/creds", json={
            "name": "fixture object store", "kind": "object_store",
            "fields": {
                "accessKeyId": "env:DP_FIXTURE_ACCESS_KEY",
                "secretAccessKey": "env:DP_FIXTURE_SECRET_KEY",
                "sessionToken": "env:DP_FIXTURE_SESSION_TOKEN",
                "region": "us-east-1",
            },
        })
        assert store.status_code == 200
        store_id = store.json()["id"]
        assert client.put("/api/settings", json={
            "scope": "global", "key": "agentCredId", "value": agent_id,
        }).status_code == 200
        assert client.put("/api/settings", json={
            "scope": "global", "key": "defaultObjectStoreCredId", "value": store_id,
        }).status_code == 200
        assert client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretaf.token",
            "value": f"file:{plugin_file}",
        }).status_code == 200

        body = client.get("/api/settings").json()
        creds = client.get("/api/creds").json()
        _assert_fixture_absent(json.dumps((body, creds)))
        for key, raw in _raw_secret_metadata_rows():
            _assert_fixture_absent(raw)
            _assert_fixture_absent(key)
        assert body["global"]["agentCredId"] == agent_id
        assert body["global"]["defaultObjectStoreCredId"] == store_id
        assert "agentApiKey" not in body["global"] and "objectStore" not in body["global"]
        assert next(c for c in creds if c["id"] == agent_id)["fields"] == {
            "apiKey": "env:DP_FIXTURE_AGENT_KEY"}
        assert next(c for c in creds if c["id"] == store_id)["fields"]["sessionToken"] == \
            "env:DP_FIXTURE_SESSION_TOKEN"
    finally:
        deps.plugins.remove(fake)
        metadb.set_setting("agentCredId", "", "global")
        metadb.set_setting("defaultObjectStoreCredId", "", "global")
        for cred_id in (agent_id, store_id):
            if cred_id:
                metadb.cred_delete(cred_id)
        metadb.set_setting("plugin.dp_secretaf.token", "", "global")


def test_secret_resolution_end_to_end(monkeypatch, tmp_path, object_store_cred):
    # AC2: agent config, object-store resolve, and plugin reg.config each receive the fixture value.
    monkeypatch.setenv("DP_FIXTURE_AGENT_KEY", FIXTURE_AGENT)
    monkeypatch.setenv("DP_FIXTURE_ACCESS_KEY", FIXTURE_ACCESS)
    monkeypatch.setenv("DP_FIXTURE_SECRET_KEY", FIXTURE_SECRET)
    monkeypatch.setenv("DP_FIXTURE_SESSION_TOKEN", FIXTURE_SESSION)
    plugin_file = tmp_path / "plugin.token"
    plugin_file.write_text(FIXTURE_PLUGIN + "\n", encoding="utf-8")

    agent_id = metadb.cred_upsert(
        None, "fixture Agent", "agent", {"apiKey": "env:DP_FIXTURE_AGENT_KEY"})["id"]
    metadb.set_setting("agentCredId", agent_id, "global")
    object_store_cred({
        "accessKeyId": "env:DP_FIXTURE_ACCESS_KEY",
        "secretAccessKey": "env:DP_FIXTURE_SECRET_KEY",
        "sessionToken": "env:DP_FIXTURE_SESSION_TOKEN",
    })
    metadb.set_setting("plugin.dp_secretaf.token", f"file:{plugin_file}", "global")

    from hub.agent import _agent_config
    from hub.deps import Deps, Registry

    try:
        _, api_key, _ = _agent_config()
        assert api_key == FIXTURE_AGENT

        resolved = resolve_object_store(metadb.cred_object_store_config(None))
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
        metadb.set_setting("agentCredId", "", "global")
        metadb.cred_delete(agent_id)
        metadb.set_setting("plugin.dp_secretaf.token", "", "global")


def test_ephemeral_metadata_stores_refs_not_fixture_secret(monkeypatch, tmp_path, caplog):
    # AC3: reference-backed object-store creds — fixture absent from logs and worker private DB.
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("DP_S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("DP_S3_KEY", FIXTURE_ACCESS)
    monkeypatch.setenv("DP_S3_SECRET", FIXTURE_SECRET)
    monkeypatch.setenv("AWS_SESSION_TOKEN", FIXTURE_SESSION)
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
        settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
        cred_rows = conn.execute("SELECT id, kind, fields_json FROM creds").fetchall()
    blob = json.dumps((settings_rows, cred_rows))
    _assert_fixture_absent(blob)
    assert "must-not-cross" not in blob
    assert not any(r[0] == "objectStore" for r in settings_rows)
    default_id = json.loads(next(
        r[1] for r in settings_rows if r[0] == "defaultObjectStoreCredId"))
    assert len(cred_rows) == 1 and cred_rows[0][0] == default_id
    assert cred_rows[0][1] == "object_store"
    store = json.loads(cred_rows[0][2])
    assert store["accessKeyId"] == "env:DP_S3_KEY"
    assert store["secretAccessKey"] == "env:DP_S3_SECRET"
    assert store["sessionToken"] == "env:AWS_SESSION_TOKEN"
    assert "useSsl" not in store


def test_pluggable_secret_resolver_supports_uri_scheme(tmp_path):
    # AC5: an out-of-tree-style, hyphenated scheme works through the public registry seam.
    from hub.deps import Deps, Registry

    def _aws_sm(name: str) -> str:
        return f"resolved-{name}"

    reg = Registry(Deps(str(tmp_path / "workspace"), str(tmp_path / "data")))
    reg.add_secret_resolver("aws-sm", _aws_sm)
    try:
        assert "aws-sm" in list_schemes()
        assert parse_secret_ref("AWS-SM:widget") == ("aws-sm", "widget")
        assert resolve_secret("AWS-SM:widget") == "resolved-widget"
        assert is_secret_ref("aws-sm:widget")
        assert is_secret_ref("vault+oidc.v2:item")

        # Re-registering the identical callable is idempotent; another plugin cannot shadow it.
        reg.add_secret_resolver("AWS-SM", _aws_sm)
        with pytest.raises(ValueError, match="already registered"):
            reg.add_secret_resolver("aws-sm", lambda name: f"shadowed-{name}")
        assert resolve_secret("aws-sm:widget") == "resolved-widget"
    finally:
        unregister_resolver("AWS-SM")


@pytest.mark.parametrize(
    ("scheme", "reference"),
    [
        ("", ":item"),
        ("1vault", "1vault:item"),
        ("_vault", "_vault:item"),
        ("vault_name", "vault_name:item"),
        ("vault/name", "vault/name:item"),
        (" vault", " vault:item"),
        ("vault ", "vault :item"),
        ("väult", "väult:item"),
        ("-vault", "-vault:item"),
        (".vault", ".vault:item"),
        ("vault\n", "vault\n:item"),
    ],
)
def test_secret_scheme_rejects_malformed_names_consistently(scheme, reference):
    def _resolver(name: str) -> str:
        return name

    with pytest.raises(ValueError, match="invalid secret-reference scheme"):
        register_resolver(scheme, _resolver)
    assert not is_secret_ref(reference)
    with pytest.raises(SecretResolveError, match="not a secret reference"):
        parse_secret_ref(reference)


def test_secret_scheme_rejects_empty_reference_and_builtin_conflict(tmp_path):
    from hub.deps import Deps, Registry

    assert not is_secret_ref("aws-sm:")
    with pytest.raises(SecretResolveError, match="not a secret reference"):
        parse_secret_ref("aws-sm:")

    reg = Registry(Deps(str(tmp_path / "workspace"), str(tmp_path / "data")))
    with pytest.raises(ValueError, match="'env' is already registered"):
        reg.add_secret_resolver("ENV", lambda name: name)


def test_put_settings_rejects_removed_credential_keys_and_plaintext_plugin_secret():
    # Removed core keys are never writable; plugin secret settings retain their SecretRef contract.
    from hub.deps import get_deps
    deps = get_deps()
    fake = {"name": "dp_secretreject", "source": "drop-in",
            "config": [{"key": "token", "type": "password", "secret": True}]}
    deps.plugins.append(fake)
    try:
        for key, value, binding in (
            ("agentApiKey", "env:OK_AGENT", "agentCredId"),
            ("objectStore", {}, "defaultObjectStoreCredId"),
        ):
            r = client.put("/api/settings", json={"scope": "global", "key": key, "value": value})
            assert r.status_code == 400, key
            assert "/api/creds" in r.json()["detail"] and binding in r.json()["detail"]
            assert metadb.get_setting(key, "global") is None
        raw = client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretreject.token", "value": "raw-token",
        })
        assert raw.status_code == 400
        assert client.put("/api/settings", json={
            "scope": "global", "key": "plugin.dp_secretreject.token", "value": "env:OK_PLUGIN",
        }).status_code == 200
    finally:
        deps.plugins.remove(fake)
        metadb.set_setting("plugin.dp_secretreject.token", "", "global")
