"""Atomic, revisioned Settings batch contracts on SQLite and PostgreSQL."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from hub import auth, metadb
from hub.deps import get_deps
from hub.main import app
from hub.observability import (
    AuditAction,
    AuditOutcome,
    InMemoryObservabilitySink,
    clear_sinks,
    drain_sinks,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_observability_sinks():
    clear_sinks()
    yield
    clear_sinks()


def _key(label: str) -> str:
    return f"test.settings_batch.{label}.{uuid.uuid4().hex}"


def _snapshot(request_client: TestClient = client, *, headers: dict[str, str] | None = None) -> dict:
    response = request_client.get("/api/settings", headers=headers)
    assert response.status_code == 200
    return response.json()


def _batch(
        snapshot: dict, changes: list[dict], request_client: TestClient = client,
        *, headers: dict[str, str] | None = None):
    return request_client.put(
        "/api/settings/batch",
        json={"expectedRevision": snapshot["revision"], "changes": changes},
        headers=headers,
    )


def test_settings_batch_commits_mixed_scopes_atomically():
    global_key = _key("mixed.global")
    user_key = _key("mixed.user")
    before = _snapshot()

    response = _batch(before, [
        {"scope": "global", "key": global_key, "value": {"mode": "global"}},
        {"scope": "user", "key": user_key, "value": ["user"]},
    ])

    assert response.status_code == 200
    expected_revision = {
        "global": before["revision"]["global"] + 1,
        "user": before["revision"]["user"] + 1,
    }
    assert response.json() == {"ok": True, "revision": expected_revision}
    after = _snapshot()
    assert after["revision"] == expected_revision
    assert after["global"][global_key] == {"mode": "global"}
    assert after["user"][user_key] == ["user"]


def test_empty_settings_batch_is_a_true_noop():
    sink = InMemoryObservabilitySink().register()
    before = _snapshot()

    response = _batch(before, [])

    assert response.status_code == 200
    assert response.json()["revision"] == before["revision"]
    assert _snapshot()["revision"] == before["revision"]
    assert drain_sinks()
    assert not [
        event for event in sink.audits
        if event.action == AuditAction.ADMIN_SETTINGS_CHANGE
    ]


def test_settings_batch_validation_failure_persists_nothing():
    good_key = _key("validation.good")
    before = _snapshot()

    response = _batch(before, [
        {"scope": "global", "key": good_key, "value": "must-roll-back"},
        {"scope": "global", "key": "agentApiKey", "value": "env:REMOVED"},
    ])

    assert response.status_code == 400
    after = _snapshot()
    assert after["revision"] == before["revision"]
    assert good_key not in after["global"]
    assert metadb.get_setting(good_key, "global") is None


def test_settings_batch_persistence_failure_rolls_back_rows_and_revision(monkeypatch):
    first_key = _key("persistence.first")
    second_key = _key("persistence.second")
    before = _snapshot()
    real_set = metadb._set_setting_in_session
    writes = 0

    def fail_after_database_writes(session, key, value, *, scope, scope_id):
        nonlocal writes
        real_set(session, key, value, scope=scope, scope_id=scope_id)
        session.flush()
        writes += 1
        if writes == 2:
            raise RuntimeError("injected settings persistence failure")

    monkeypatch.setattr(metadb, "_set_setting_in_session", fail_after_database_writes)
    failing_client = TestClient(app, raise_server_exceptions=False)
    try:
        response = _batch(before, [
            {"scope": "global", "key": first_key, "value": 1},
            {"scope": "global", "key": second_key, "value": 2},
        ], failing_client)
    finally:
        failing_client.close()

    assert response.status_code == 500
    assert writes == 2
    after = _snapshot()
    assert after["revision"] == before["revision"]
    assert first_key not in after["global"] and second_key not in after["global"]


def test_stale_settings_batch_returns_current_revision_without_writes():
    same_key = _key("stale.same")
    other_key = _key("stale.other")
    baseline = _snapshot()
    winner = _batch(baseline, [
        {"scope": "global", "key": same_key, "value": "server"},
    ])
    assert winner.status_code == 200

    loser = _batch(baseline, [
        {"scope": "global", "key": same_key, "value": "local"},
        {"scope": "global", "key": other_key, "value": "local-review"},
    ])

    assert loser.status_code == 409
    latest = _snapshot()
    assert loser.json() == {
        "detail": "settings revision is stale",
        "code": "conflict",
        "retryable": False,
        "revision": latest["revision"],
    }
    assert latest["global"][same_key] == "server"
    assert other_key not in latest["global"]


def test_concurrent_settings_batches_exactly_one_wins():
    baseline = _snapshot()
    keys = (_key("race.a"), _key("race.b"))
    cas_starts = threading.Barrier(2)
    request_clients = (TestClient(app), TestClient(app))

    def align_revision_cas(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE SETTING_REVISIONS"):
            cas_starts.wait(timeout=10)

    def write(index: int):
        return _batch(baseline, [
            {"scope": "global", "key": keys[index], "value": index},
        ], request_clients[index])

    database = metadb.engine()
    event.listen(database, "before_cursor_execute", align_revision_cas)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(write, range(2)))
    finally:
        event.remove(database, "before_cursor_execute", align_revision_cas)
        for request_client in request_clients:
            request_client.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    after = _snapshot()
    present = [key for key in keys if key in after["global"]]
    assert len(present) == 1
    assert after["revision"]["global"] == baseline["revision"]["global"] + 1


def test_settings_snapshot_retries_if_values_change_between_revision_reads(monkeypatch):
    key = _key("snapshot")
    first_revision_read = threading.Event()
    writer_done = threading.Event()
    real_read = metadb._read_setting_revisions_in_session
    reads = 0

    def pause_first_read(session, user_id):
        nonlocal reads
        revision = real_read(session, user_id)
        reads += 1
        if reads == 1:
            first_revision_read.set()
            assert writer_done.wait(timeout=10)
        return revision

    def write_between_reads():
        assert first_revision_read.wait(timeout=10)
        try:
            metadb.set_setting(key, "new-value", "global")
        finally:
            writer_done.set()

    monkeypatch.setattr(metadb, "_read_setting_revisions_in_session", pause_first_read)
    writer = threading.Thread(target=write_between_reads)
    writer.start()
    try:
        rows, revision = metadb.settings_snapshot("local")
    finally:
        writer.join(timeout=10)

    values = {(scope, setting_key): json.loads(value) for scope, setting_key, value in rows}
    assert values[("global", key)] == "new-value"
    assert revision == metadb.setting_revisions("local")
    assert reads >= 4, "the mismatched first snapshot must have been retried"


def test_non_admin_mixed_scope_batch_is_denied_before_any_write(monkeypatch):
    sink = InMemoryObservabilitySink().register()
    user_key = _key("denied.user")
    global_key = _key("denied.global")
    with metadb.session() as session:
        user = metadb.User(name="settings-batch-non-admin")
        session.add(user)
        session.flush()
        user_id = user.id
    monkeypatch.setenv("DP_AUTH_SECRET", "settings-batch-auth-secret")
    headers = {"Cookie": f"dp_session={auth.sign(user_id)}"}
    client.cookies.clear()
    try:
        before = _snapshot(headers=headers)
        response = _batch(before, [
            {"scope": "user", "key": user_key, "value": "user-first"},
            {"scope": "global", "key": global_key, "value": "forbidden"},
        ], headers=headers)
        after = _snapshot(headers=headers)
    finally:
        client.cookies.clear()
        monkeypatch.delenv("DP_AUTH_SECRET", raising=False)

    assert response.status_code == 403
    assert after["revision"] == before["revision"]
    assert user_key not in after["user"] and global_key not in after["global"]
    assert drain_sinks()
    audits = [
        event for event in sink.audits
        if event.action == AuditAction.ADMIN_SETTINGS_CHANGE
    ]
    assert len(audits) == 1 and audits[0].outcome == AuditOutcome.DENIED


def test_settings_batch_emits_one_redacted_summary_audit_after_commit():
    sink = InMemoryObservabilitySink().register()
    marker = f"ordinary-value-{uuid.uuid4().hex}"
    secret_ref = f"env:DP_SETTINGS_BATCH_{uuid.uuid4().hex.upper()}"
    plugin_name = f"dp_settings_batch_{uuid.uuid4().hex}"
    secret_key = f"plugin.{plugin_name}.token"
    ordinary_key = _key("audit.ordinary")
    user_key = _key("audit.user")
    fake = {
        "name": plugin_name,
        "source": "test",
        "config": [{"key": "token", "type": "password", "secret": True}],
    }
    deps = get_deps()
    deps.plugins.append(fake)
    try:
        before = _snapshot()
        response = _batch(before, [
            {"scope": "global", "key": ordinary_key, "value": marker},
            {"scope": "global", "key": secret_key, "value": secret_ref},
            {"scope": "user", "key": user_key, "value": True},
        ])
        assert response.status_code == 200
        assert drain_sinks()
    finally:
        deps.plugins.remove(fake)

    audits = [
        event for event in sink.audits
        if event.action == AuditAction.ADMIN_SETTINGS_CHANGE
    ]
    assert len(audits) == 1
    audit = audits[0]
    assert audit.outcome == AuditOutcome.SUCCESS
    assert audit.resource_type == "settings_batch" and audit.resource_id == "batch"
    assert audit.attrs == {
        "scopes": "global,user", "change_count": "3", "sensitive": "true",
    }
    encoded = audit.model_dump_json()
    assert marker not in encoded and secret_ref not in encoded
    assert ordinary_key not in encoded and secret_key not in encoded and user_key not in encoded


def test_single_setting_write_invalidates_an_existing_batch_revision():
    single_key = _key("single")
    batch_key = _key("after.single")
    baseline = _snapshot()
    single = client.put("/api/settings", json={
        "scope": "global", "key": single_key, "value": "single",
    })
    assert single.status_code == 200

    stale = _batch(baseline, [
        {"scope": "global", "key": batch_key, "value": "must-not-write"},
    ])

    assert stale.status_code == 409
    after = _snapshot()
    assert after["global"][single_key] == "single"
    assert batch_key not in after["global"]
    assert after["revision"]["global"] == baseline["revision"]["global"] + 1


def test_settings_batch_rejects_duplicate_invalid_scope_and_unbounded_changes():
    baseline = _snapshot()
    key = _key("shape")
    raw_marker = f"raw-secret-{uuid.uuid4().hex}"
    duplicate = _batch(baseline, [
        {"scope": "global", "key": key, "value": raw_marker},
        {"scope": "global", "key": key, "value": raw_marker},
    ])
    invalid_scope = _batch(baseline, [
        {"scope": "workspace", "key": key, "value": 1},
    ])
    too_many = _batch(baseline, [
        {"scope": "global", "key": f"{key}.{index}", "value": index}
        for index in range(129)
    ])
    revision_overflow = client.put("/api/settings/batch", json={
        "expectedRevision": {"global": 2**63, "user": baseline["revision"]["user"]},
        "changes": [{"scope": "global", "key": key, "value": 1}],
    })
    invalid_single = client.put("/api/settings", json={
        "scope": "workspace", "key": key, "value": raw_marker,
    })

    assert duplicate.status_code == invalid_scope.status_code == too_many.status_code == 422
    assert revision_overflow.status_code == 422
    assert invalid_single.status_code == 422 and raw_marker not in invalid_single.text
    assert raw_marker not in duplicate.text
    assert duplicate.json() == {
        "detail": "invalid settings batch request body",
        "code": "validation_error",
        "retryable": False,
    }
    assert _snapshot()["revision"] == baseline["revision"]
    assert metadb.get_setting(key, "global") is None


def test_settings_batch_secret_validation_matches_single_setting_contract():
    plugin_name = f"dp_settings_secret_{uuid.uuid4().hex}"
    secret_key = f"plugin.{plugin_name}.token"
    fake = {
        "name": plugin_name,
        "source": "test",
        "config": [{"key": "token", "type": "password", "secret": True}],
    }
    deps = get_deps()
    deps.plugins.append(fake)
    try:
        before = _snapshot()
        raw = _batch(before, [
            {"scope": "global", "key": secret_key, "value": "raw-secret"},
        ])
        assert raw.status_code == 400
        assert _snapshot()["revision"] == before["revision"]
        valid = _batch(before, [
            {"scope": "global", "key": secret_key, "value": "env:DP_VALID_SECRET_REF"},
        ])
        assert valid.status_code == 200
        assert metadb.get_setting(secret_key, "global") == "env:DP_VALID_SECRET_REF"
    finally:
        deps.plugins.remove(fake)


def test_settings_batch_audit_payload_is_json_serializable():
    """Keep the summary shape guard explicit without depending on sink implementation details."""
    before = _snapshot()
    response = _batch(before, [
        {"scope": "user", "key": _key("json"), "value": {"enabled": True}},
    ])
    assert response.status_code == 200
    json.dumps(response.json(), allow_nan=False)
