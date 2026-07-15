"""Credentials as a first-class Cred entity (issue #156).

Covers: PUT/POST validation rejects raw secrets (references only), credential CRUD round-trip,
and a destination's credId reaching ``db.ensure_object_store`` on a write open.
"""

from __future__ import annotations

import contextlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app
from hub.settings import settings

client = TestClient(app)


def _delete_all_creds() -> None:
    for c in client.get("/api/creds").json():
        client.delete(f"/api/creds/{c['id']}")


def test_object_store_cred_rejects_raw_secret_accepts_reference():
    # A raw secret must be rejected; only env:/file: references are stored.
    try:
        raw = client.post("/api/creds", json={
            "name": "bad", "kind": "object_store",
            "fields": {"accessKeyId": "AKIARAWSECRET", "secretAccessKey": "env:OK"},
        })
        assert raw.status_code == 400, raw.text
        assert "reference" in raw.json()["detail"]

        ok = client.post("/api/creds", json={
            "name": "prod", "kind": "object_store",
            "fields": {"accessKeyId": "env:AWS_ACCESS_KEY_ID",
                       "secretAccessKey": "env:AWS_SECRET_ACCESS_KEY", "region": "us-east-1"},
        })
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["kind"] == "object_store"
        # references are echoed as-is (they are not sensitive); no plaintext is stored
        assert body["fields"]["accessKeyId"] == "env:AWS_ACCESS_KEY_ID"
        assert body["fields"]["region"] == "us-east-1"
    finally:
        _delete_all_creds()


def test_agent_cred_rejects_raw_secret():
    try:
        raw = client.post("/api/creds", json={
            "name": "bad-agent", "kind": "agent", "fields": {"apiKey": "sk-rawkey"}})
        assert raw.status_code == 400, raw.text
        ok = client.post("/api/creds", json={
            "name": "agent", "kind": "agent", "fields": {"apiKey": "env:ANTHROPIC_API_KEY"}})
        assert ok.status_code == 200
        assert ok.json()["fields"] == {"apiKey": "env:ANTHROPIC_API_KEY"}
    finally:
        _delete_all_creds()


def test_unknown_cred_kind_rejected():
    r = client.post("/api/creds", json={"name": "x", "kind": "database", "fields": {}})
    assert r.status_code == 400


def test_agent_config_resolves_key_via_referenced_cred(monkeypatch):
    # The agent reads its key from the referenced agent cred's reference, resolved in-process.
    from hub import agent
    monkeypatch.setenv("DP_FIXTURE_AGENT_CRED", "resolved-agent-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "different-ambient-provider-key")
    monkeypatch.setattr(settings, "agent_api_key", "different-ambient-settings-key")
    try:
        cid = client.post("/api/creds", json={
            "name": "agent", "kind": "agent", "fields": {"apiKey": "env:DP_FIXTURE_AGENT_CRED"}}).json()["id"]
        metadb.set_setting("agentCredId", cid, "global")
        assert agent._agent_config()[1] == "resolved-agent-key"
        status = agent.agent_status()
        assert status["available"] is True and "errorCode" not in status
        assert "resolved-agent-key" not in json.dumps(status)
    finally:
        metadb.set_setting("agentCredId", "", "global")
        _delete_all_creds()


@pytest.mark.parametrize("failure", ["empty", "broken_ref", "missing", "wrong_kind", "deleted"])
def test_explicit_agent_credential_failures_never_use_ambient_identity(
        failure, monkeypatch):
    """Every explicit-selection failure has one stable, non-secret contract on config/status/execute."""
    from hub import agent
    import hub.routers.runs as runs

    ambient_settings = "ambient-settings-material-must-not-appear"
    ambient_provider = "ambient-provider-material-must-not-appear"
    broken_ref = "env:DP_AGENT_EXPLICIT_REFERENCE_MUST_NOT_APPEAR"
    created_ids: list[str] = []
    metadb.set_setting("agentModel", "anthropic/credential-test", "global")
    metadb.set_setting("agentBaseUrl", "", "global")
    metadb.set_setting("agentApiKey", "", "global")
    metadb.set_setting("agentCredId", "", "global")
    monkeypatch.setattr(settings, "agent_api_key", ambient_settings)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_provider)
    monkeypatch.delenv("DP_AGENT_EXPLICIT_REFERENCE_MUST_NOT_APPEAR", raising=False)
    try:
        if failure == "empty":
            selected = client.post("/api/creds", json={
                "name": "empty-agent", "kind": "agent", "fields": {},
            }).json()["id"]
            created_ids.append(selected)
        elif failure == "broken_ref":
            selected = client.post("/api/creds", json={
                "name": "broken-agent", "kind": "agent",
                "fields": {"apiKey": broken_ref},
            }).json()["id"]
            created_ids.append(selected)
        elif failure == "wrong_kind":
            selected = client.post("/api/creds", json={
                "name": "not-an-agent", "kind": "object_store", "fields": {},
            }).json()["id"]
            created_ids.append(selected)
        elif failure == "deleted":
            selected = client.post("/api/creds", json={
                "name": "deleted-agent", "kind": "agent",
                "fields": {"apiKey": "env:ANTHROPIC_API_KEY"},
            }).json()["id"]
            assert client.delete(f"/api/creds/{selected}").status_code == 200
        else:
            selected = "missing-agent-credential"
        metadb.set_setting("agentCredId", selected, "global")

        with pytest.raises(agent.AgentCredentialError) as exc_info:
            agent._agent_config()
        assert exc_info.value.code == agent.AGENT_CREDENTIAL_ERROR_CODE
        assert str(exc_info.value) == agent.AGENT_CREDENTIAL_ERROR_REASON

        monkeypatch.setattr(
            runs, "run_agent",
            lambda *_args, **_kwargs: pytest.fail("invalid explicit Cred reached Agent execution"),
        )
        status_response = client.get("/api/agent")
        action_response = client.post("/api/agent", json={
            "outcome": "inspect", "graph": {"nodes": [], "edges": []},
        })
        assert status_response.status_code == action_response.status_code == 200
        status, action = status_response.json(), action_response.json()
        for payload in (status, action):
            assert payload["available"] is False
            assert payload["errorCode"] == agent.AGENT_CREDENTIAL_ERROR_CODE
            assert payload["reason"] == agent.AGENT_CREDENTIAL_ERROR_REASON
            serialized = json.dumps(payload)
            for forbidden in (ambient_settings, ambient_provider, broken_ref, selected):
                assert forbidden not in serialized
    finally:
        metadb.set_setting("agentCredId", "", "global")
        metadb.set_setting("agentApiKey", "", "global")
        metadb.set_setting("agentModel", "", "global")
        metadb.set_setting("agentBaseUrl", "", "global")
        for cred_id in created_ids:
            client.delete(f"/api/creds/{cred_id}")


def test_no_agent_credential_selection_retains_ambient_default(monkeypatch):
    from hub import agent

    ambient = "ambient-default-agent-key"
    metadb.set_setting("agentCredId", "", "global")
    metadb.set_setting("agentApiKey", "", "global")
    metadb.set_setting("agentModel", "anthropic/ambient-test", "global")
    metadb.set_setting("agentBaseUrl", "", "global")
    monkeypatch.setattr(settings, "agent_api_key", ambient)
    try:
        # An explicitly supplied empty selection is invalid, while the persisted empty default below
        # means that no Cred has been selected and therefore retains the ambient/default behaviour.
        with pytest.raises(metadb.CredResolutionError):
            metadb.cred_agent_api_key_ref("")
        assert agent._agent_config() == ("anthropic/ambient-test", ambient, None)
        status = agent.agent_status()
        assert status["available"] is True and "errorCode" not in status
        assert ambient not in json.dumps(status)
    finally:
        metadb.set_setting("agentModel", "", "global")


@pytest.mark.parametrize("provider", ["openrouter", "xai"])
def test_selected_key_for_provider_without_locked_support_fails_closed(provider, monkeypatch):
    """Status must not claim availability when the locked agent extra cannot bind a selected key."""
    from hub import agent
    import hub.routers.runs as runs

    selected_material = "selected-unsupported-provider-material"
    monkeypatch.setenv("DP_AGENT_UNSUPPORTED_PROVIDER_KEY", selected_material)
    cid = client.post("/api/creds", json={
        "name": f"{provider}-agent", "kind": "agent",
        "fields": {"apiKey": "env:DP_AGENT_UNSUPPORTED_PROVIDER_KEY"},
    }).json()["id"]
    metadb.set_setting("agentCredId", cid, "global")
    metadb.set_setting("agentModel", f"{provider}/credential-test", "global")
    metadb.set_setting("agentBaseUrl", "", "global")
    try:
        with pytest.raises(agent.AgentCredentialError):
            agent._agent_config()
        monkeypatch.setattr(
            runs, "run_agent",
            lambda *_args, **_kwargs: pytest.fail("unsupported provider reached Agent execution"),
        )
        for response in (
            client.get("/api/agent"),
            client.post("/api/agent", json={
                "outcome": "inspect", "graph": {"nodes": [], "edges": []},
            }),
        ):
            assert response.status_code == 200
            payload = response.json()
            assert payload["available"] is False
            assert payload["errorCode"] == agent.AGENT_CREDENTIAL_ERROR_CODE
            assert payload["reason"] == agent.AGENT_CREDENTIAL_ERROR_REASON
            serialized = json.dumps(payload)
            assert selected_material not in serialized
            assert cid not in serialized
    finally:
        metadb.set_setting("agentCredId", "", "global")
        metadb.set_setting("agentModel", "", "global")
        metadb.set_setting("agentBaseUrl", "", "global")
        client.delete(f"/api/creds/{cid}")


def test_agent_credential_rotation_and_deletion_do_not_cache_material(
        monkeypatch):
    """Each resolution is fresh and model construction never leaves the selected key in process env."""
    from pydantic_ai import models as pydantic_models, providers as pydantic_providers
    from hub import agent

    selected_v1 = "selected-agent-key-v1"
    selected_v2 = "selected-agent-key-v2"
    ambient_provider = "ambient-provider-key"
    ambient_default = "ambient-default-key"
    inferred_names: list[str] = []
    observed: list[tuple[str, str | None, str | None]] = []

    def fake_provider_class(provider_name: str):
        class Provider:
            def __init__(self, *, api_key: str | None = None):
                observed.append((
                    provider_name, api_key, os.environ.get("ANTHROPIC_API_KEY")))
        return Provider

    def fake_infer_model(name: str, provider_factory):
        inferred_names.append(name)
        provider_factory(name.split(":", 1)[0])
        return object()

    monkeypatch.setattr(pydantic_models, "infer_model", fake_infer_model)
    monkeypatch.setattr(pydantic_providers, "infer_provider_class", fake_provider_class)
    monkeypatch.setenv("DP_AGENT_ROTATION_V1", selected_v1)
    monkeypatch.setenv("DP_AGENT_ROTATION_V2", selected_v2)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_provider)
    monkeypatch.setattr(settings, "agent_api_key", ambient_default)
    metadb.set_setting("agentModel", "anthropic/rotation-test", "global")
    metadb.set_setting("agentBaseUrl", "", "global")
    metadb.set_setting("agentApiKey", "", "global")
    cid = client.post("/api/creds", json={
        "name": "rotating-agent", "kind": "agent",
        "fields": {"apiKey": "env:DP_AGENT_ROTATION_V1"},
    }).json()["id"]
    metadb.set_setting("agentCredId", cid, "global")
    try:
        config_v1 = agent._agent_config()
        assert config_v1[1] == selected_v1
        agent._build_model(*config_v1)
        assert inferred_names[-1] == "anthropic:rotation-test"
        assert observed[-1] == ("anthropic", selected_v1, ambient_provider)
        assert os.environ["ANTHROPIC_API_KEY"] == ambient_provider

        updated = client.put(f"/api/creds/{cid}", json={
            "name": "rotating-agent", "kind": "agent",
            "fields": {"apiKey": "env:DP_AGENT_ROTATION_V2"},
        })
        assert updated.status_code == 200
        config_v2 = agent._agent_config()
        assert config_v2[1] == selected_v2
        agent._build_model(*config_v2)
        assert observed[-1] == ("anthropic", selected_v2, ambient_provider)
        assert os.environ["ANTHROPIC_API_KEY"] == ambient_provider

        metadb.set_setting("agentCredId", "", "global")
        assert client.delete(f"/api/creds/{cid}").status_code == 200
        default_config = agent._agent_config()
        assert default_config[1] == ambient_default
        agent._build_model(*default_config)
        assert observed[-1] == ("anthropic", ambient_default, ambient_provider)
        assert os.environ["ANTHROPIC_API_KEY"] == ambient_provider
        assert all(key != selected_v1 for _provider, key, _ambient in observed[1:])
    finally:
        metadb.set_setting("agentCredId", "", "global")
        metadb.set_setting("agentModel", "", "global")
        metadb.set_setting("agentBaseUrl", "", "global")
        client.delete(f"/api/creds/{cid}")


def test_agent_execution_race_uses_normalized_credential_error(monkeypatch):
    from hub import agent
    import hub.routers.runs as runs

    monkeypatch.setattr(runs, "agent_status", lambda: {"available": True, "reason": ""})

    def rotated_after_status(*_args, **_kwargs):
        raise agent.AgentCredentialError()

    monkeypatch.setattr(runs, "run_agent", rotated_after_status)
    response = client.post("/api/agent", json={
        "outcome": "inspect", "graph": {"nodes": [], "edges": []},
    })
    assert response.status_code == 200
    assert response.json()["errorCode"] == agent.AGENT_CREDENTIAL_ERROR_CODE
    assert response.json()["reason"] == agent.AGENT_CREDENTIAL_ERROR_REASON


def test_cred_crud_round_trip():
    try:
        created = client.post("/api/creds", json={
            "name": "store", "kind": "object_store", "fields": {"region": "eu-west-1"}}).json()
        cid = created["id"]

        listed = client.get("/api/creds").json()
        assert any(c["id"] == cid and c["name"] == "store" for c in listed)

        updated = client.put(f"/api/creds/{cid}", json={
            "name": "store-2", "kind": "object_store", "fields": {"region": "us-east-2"}})
        assert updated.status_code == 200
        assert updated.json()["name"] == "store-2"
        assert updated.json()["fields"]["region"] == "us-east-2"

        assert client.delete(f"/api/creds/{cid}").status_code == 200
        assert all(c["id"] != cid for c in client.get("/api/creds").json())
    finally:
        _delete_all_creds()


def test_destination_cred_reaches_ensure_object_store_on_write(monkeypatch, tmp_path):
    # The KEY requirement: a destination's credId (its resolved cfg) — not the global default — is
    # what the write path binds at the object-store open.
    from hub import db, destinations
    from hub.models import Graph, PerNodeStatus, RunStatus
    from hub.plugins.runner import LocalRunner, _CancelToken

    DEST_CFG = {"region": "dest-region"}     # distinctive, non-secret so resolve is a pass-through
    GLOBAL_CFG = {"region": "GLOBAL-region"}
    monkeypatch.setattr(metadb, "cred_object_store_config",
                        lambda cred_id=None: DEST_CFG if cred_id == "cred-x" else GLOBAL_CFG)
    monkeypatch.setattr(destinations, "presets", lambda ws: [
        {"id": "s3dest", "name": "S3", "backend": "s3", "root": "s3://bkt/pre", "credId": "cred-x"}])

    recorded: list[dict | None] = []
    monkeypatch.setattr(db, "ensure_object_store", lambda cfg=None: recorded.append(cfg))

    graph = Graph.model_validate({
        "id": "cred-write", "version": 1,
        "nodes": [{"id": "write", "type": "write", "position": {"x": 0, "y": 0},
                   "data": {"config": {"destId": "s3dest", "filename": "out.parquet"}}}],
        "edges": [],
    })
    node = {n.id: n for n in graph.nodes}["write"]
    runner = LocalRunner(lambda _uri: object(), {}, object(), str(tmp_path))
    status = RunStatus(run_id="r", status="running",
                       per_node=[PerNodeStatus(node_id="write", status="running", label="write")])

    # no incoming edge -> _commit_write binds the destination's credential, then returns before any
    # real object I/O; that binding is exactly what a real write would run on.
    rows = runner._commit_write(node, graph, None, status, None, _CancelToken())
    assert rows == 0
    assert DEST_CFG in recorded                     # the destination's cred cfg reached the open
    assert GLOBAL_CFG not in recorded               # NOT the global/default

    # the run-level binding (primed before the scope cursor) resolves the same destination cred
    from hub.models import CompilePlan, PlanStep
    plan = CompilePlan(target_node_id="write", steps=[PlanStep(node_id="write", kind="write", label="write")])
    assert runner._run_object_store_cfg(plan, {"write": node}) == DEST_CFG


def test_unknown_cred_field_is_rejected():
    # #161 re-review: an unknown field is REJECTED (400), not silently dropped — a stray key is a bug,
    # and dropping would hide it. No cred is created.
    r = client.post("/api/creds", json={
        "name": "leaky", "kind": "object_store",
        "fields": {"accessKeyId": "env:AK", "password": "raw-secret"},
    })
    assert r.status_code == 400, r.text
    assert "unknown credential field" in r.json()["detail"]


def test_explicit_missing_or_wrong_kind_cred_raises():
    # #161 re-review: only None may use a default; a non-empty explicit id (or a configured-but-broken
    # default) that is missing/wrong-kind RAISES — never silently resolves to another identity.
    import pytest
    try:
        cid = client.post("/api/creds", json={
            "name": "dflt", "kind": "object_store", "fields": {"accessKeyId": "env:AK"}}).json()["id"]
        metadb.set_setting("defaultObjectStoreCredId", cid, scope="global", scope_id="")
        assert metadb.cred_object_store_config(None).get("accessKeyId") == "env:AK"  # None → default
        with pytest.raises(metadb.CredResolutionError):
            metadb.cred_object_store_config("no-such-cred")                          # explicit missing → raise
        agent = client.post("/api/creds", json={
            "name": "ag", "kind": "agent", "fields": {"apiKey": "env:K"}}).json()["id"]
        with pytest.raises(metadb.CredResolutionError):
            metadb.cred_object_store_config(agent)                                   # wrong-kind → raise
    finally:
        metadb.set_setting("defaultObjectStoreCredId", "", scope="global", scope_id="")
        _delete_all_creds()


def test_delete_in_use_cred_returns_409_and_kind_change_rejected():
    # #161 review P1: refuse to strand a live reference; refuse an in-place kind change.
    try:
        cid = client.post("/api/creds", json={
            "name": "used", "kind": "object_store", "fields": {"accessKeyId": "env:AK"}}).json()["id"]
        metadb.set_setting("defaultObjectStoreCredId", cid, scope="global", scope_id="")
        r = client.delete(f"/api/creds/{cid}")
        assert r.status_code == 409, r.text
        assert "in use" in r.json()["detail"]
        ch = client.put(f"/api/creds/{cid}", json={
            "name": "used", "kind": "agent", "fields": {"apiKey": "env:K"}})
        assert ch.status_code == 400 and "kind" in ch.json()["detail"]
    finally:
        metadb.set_setting("defaultObjectStoreCredId", "", scope="global", scope_id="")
        _delete_all_creds()


def test_two_destinations_write_to_their_own_object_store(monkeypatch):
    # #156 acceptance (real object I/O, not a mock): a write bound to a destination's credential lands
    # in THAT destination's object store. Two destinations point at two separate moto endpoints; each
    # real httpfs COPY must land on (and read back from) its own endpoint — proving the per-destination
    # cred cfg genuinely drives the write, not a shared/global identity.
    import pytest
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    from hub import db
    from hub.secrets import resolve_object_store

    monkeypatch.setenv("DP_TEST_AK", "test-key")
    monkeypatch.setenv("DP_TEST_SK", "test-secret")
    srv_a, srv_b = ThreadedMotoServer(port=0), ThreadedMotoServer(port=0)
    srv_a.start(); srv_b.start()
    try:
        (ha, pa), (hb, pb) = srv_a.get_host_and_port(), srv_b.get_host_and_port()

        def s3(host, port):
            return boto3.client("s3", endpoint_url=f"http://{host}:{port}",
                                aws_access_key_id="k", aws_secret_access_key="s", region_name="us-east-1")
        s3(ha, pa).create_bucket(Bucket="bkt")
        s3(hb, pb).create_bucket(Bucket="bkt")

        def cred(host, port):
            return client.post("/api/creds", json={"name": f"{host}:{port}", "kind": "object_store", "fields": {
                "accessKeyId": "env:DP_TEST_AK", "secretAccessKey": "env:DP_TEST_SK",
                "region": "us-east-1", "endpoint": f"http://{host}:{port}"}}).json()["id"]
        ca, cb = cred(ha, pa), cred(hb, pb)

        # write via each destination's cred and read it back — bound one endpoint at a time (a run holds
        # one object-store identity), exactly as the runner binds a destination cred before its scope
        def write_and_read(cred_id: str, value: str) -> str:
            cfg = resolve_object_store(metadb.cred_object_store_config(cred_id))
            with db.lock():
                db.ensure_object_store(cfg)
                db.conn().execute(f"COPY (SELECT '{value}' AS v) TO 's3://bkt/out.parquet' (FORMAT PARQUET)")
                return db.conn().execute("SELECT v FROM read_parquet('s3://bkt/out.parquet')").fetchone()[0]

        assert write_and_read(ca, "A") == "A"
        assert write_and_read(cb, "B") == "B"
        # each object landed on its OWN endpoint (distinct per-destination routing), not one shared store
        assert s3(ha, pa).get_object(Bucket="bkt", Key="out.parquet")["Body"].read()  # exists on A
        assert s3(hb, pb).get_object(Bucket="bkt", Key="out.parquet")["Body"].read()  # exists on B
    finally:
        srv_a.stop(); srv_b.stop()
        _delete_all_creds()
        # this test binds the SHARED base connection to a moto endpoint; restore pristine state so a
        # later test neither inherits the dead-endpoint secret nor re-pays first-time extension loads.
        with contextlib.suppress(Exception), db.lock():
            db._base_conn().execute("DROP SECRET IF EXISTS dp_s3")
            db._base_conn().execute("DROP SECRET IF EXISTS dp_gcs")
        db._obj_store_secret_config = None
        db._obj_store_loaded = db._obj_store_aws_loaded = False


def test_broken_destination_credential_fails_the_run_not_strands_it(monkeypatch, tmp_path):
    # #161 re-review: a missing/wrong-kind destination credential raises during pre-scope resolution.
    # That must terminalize the run as FAILED (and clean up), not escape _execute, kill the worker, and
    # leave every node stranded in 'running'.
    from hub.models import CompilePlan, Graph, PerNodeStatus, PlanStep, RunStatus
    from hub.plugins.runner import LocalRunner, _CancelToken

    runner = LocalRunner(lambda _uri: object(), {}, object(), str(tmp_path))
    rid = "brk"
    runner.runs[rid] = RunStatus(run_id=rid, status="queued",
                                 per_node=[PerNodeStatus(node_id="write", status="queued", label="write")])
    runner._cancel[rid] = _CancelToken()

    class Pin:
        closed = False

        def close(self):
            self.closed = True

    pin = Pin()
    monkeypatch.setattr(runner, "_plan_hash", lambda *_: "brk-hash")
    monkeypatch.setattr(runner, "_plan_cacheable", lambda *_: True)
    monkeypatch.setattr(runner, "_cache_acquire", lambda *_: (None, pin))

    def _raise(_plan, _nm):
        raise metadb.CredResolutionError("object-store credential 'gone' not found")
    monkeypatch.setattr(runner, "_run_object_store_cfg", _raise)
    completed: list[str] = []
    monkeypatch.setattr(runner, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_complete", lambda *a, **k: completed.append(runner.runs[rid].status))

    graph = Graph.model_validate({"id": "brk", "version": 1, "edges": [], "nodes": [
        {"id": "write", "type": "write", "position": {"x": 0, "y": 0},
         "data": {"config": {"destId": "s3dest", "filename": "o.parquet"}}}]})
    plan = CompilePlan(target_node_id="write", steps=[PlanStep(node_id="write", kind="write", label="write")])

    runner._execute(rid, plan, graph, "write")  # must not raise out

    assert runner.runs[rid].status == "failed"
    assert "gone" in (runner.runs[rid].error or "")
    assert rid not in runner._cancel        # cleaned up, not stranded
    assert pin.closed                       # pre-scope cache ownership is not leaked
    assert completed == ["failed"]           # terminalized through the normal completion path


def test_worker_thread_backstop_terminalizes_escape_past_the_run_body(monkeypatch, tmp_path):
    # #161 re-review: a failure that escapes _execute (e.g. run-scope/engine setup, before the body's
    # own boundary) must still fail the run via the worker-thread backstop, not kill the daemon thread
    # silently and strand every node in 'running'.
    from hub import db
    from hub.models import CompilePlan, Graph, PerNodeStatus, RunStatus
    from hub.plugins.runner import LocalRunner, _CancelToken

    runner = LocalRunner(lambda _uri: object(), {}, object(), str(tmp_path))
    rid = "esc"
    runner.runs[rid] = RunStatus(run_id=rid, status="running",
                                 per_node=[PerNodeStatus(node_id="n", status="running", label="n")])
    runner._cancel[rid] = _CancelToken()
    completed: list[str] = []
    monkeypatch.setattr(runner, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_complete", lambda *a, **k: completed.append(runner.runs[rid].status))

    class Pin:
        closed = False

        def close(self):
            self.closed = True

    pin = Pin()
    monkeypatch.setattr(runner, "_plan_hash", lambda *_: "esc-hash")
    monkeypatch.setattr(runner, "_plan_cacheable", lambda *_: True)
    monkeypatch.setattr(runner, "_cache_acquire", lambda *_: (None, pin))

    @contextlib.contextmanager
    def _boom_scope():
        raise RuntimeError("run scope setup exploded")
        yield
    monkeypatch.setattr(db, "run_scope", _boom_scope)

    runner._execute_guarded(rid, CompilePlan(), Graph(), None)  # must not raise out

    assert runner.runs[rid].status == "failed"
    assert "exploded" in (runner.runs[rid].error or "")
    assert rid not in runner._cancel
    assert pin.closed  # run-scope setup failure releases ownership before the backstop terminalizes
    assert completed == ["failed"]
