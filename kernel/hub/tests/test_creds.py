"""Credentials as a first-class Cred entity (issue #156).

Covers: PUT/POST validation rejects raw secrets (references only), cred CRUD round-trip, migration
0026 backfill (objectStore + agentApiKey -> seeded creds, destinations tagged), and a destination's
credId reaching ``db.ensure_object_store`` on a write open.
"""

from __future__ import annotations

import contextlib
import json

import sqlalchemy as sa
from alembic import command
from fastapi.testclient import TestClient

from hub import metadb
from hub.main import app
from hub.settings import settings

client = TestClient(app)


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
    try:
        cid = client.post("/api/creds", json={
            "name": "agent", "kind": "agent", "fields": {"apiKey": "env:DP_FIXTURE_AGENT_CRED"}}).json()["id"]
        metadb.set_setting("agentCredId", cid, "global")
        assert agent._agent_config()[1] == "resolved-agent-key"
    finally:
        metadb.set_setting("agentCredId", "", "global")
        _delete_all_creds()


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


def test_migration_0026_backfills_creds_and_tags_destinations(tmp_path):
    legacy = tmp_path / "legacy.db"

    def _upgrade_to(target: str) -> None:
        with _isolated_metadata(f"sqlite:///{legacy}"):
            command.upgrade(metadb._alembic_cfg(), target)

    _upgrade_to("0025_run_request_id")
    engine = sa.create_engine(f"sqlite:///{legacy}")
    with engine.begin() as conn:
        for key, value in (
            ("objectStore", {"accessKeyId": "env:AK", "secretAccessKey": "env:SK",
                             "region": "us-east-1"}),
            ("agentApiKey", "env:AGENT"),
            ("destinations", [
                {"id": "s3d", "name": "S3", "backend": "s3", "root": "s3://b/p"},
                {"id": "loc", "name": "Local", "backend": "local", "root": "/tmp/out"},
            ]),
        ):
            conn.execute(sa.text(
                "INSERT INTO settings (scope, scope_id, key, value) VALUES ('global','',:k,:v)"),
                {"k": key, "v": json.dumps(value)})
    engine.dispose()

    _upgrade_to("0026_creds")

    with _isolated_metadata(f"sqlite:///{legacy}"):
        creds = {c["kind"]: c for c in metadb.creds_list()}
        assert creds["object_store"]["fields"]["accessKeyId"] == "env:AK"
        assert creds["agent"]["fields"] == {"apiKey": "env:AGENT"}

        assert metadb.get_setting("defaultObjectStoreCredId") == "cred-object-store-default"
        assert metadb.get_setting("agentCredId") == "cred-agent-default"

        dests = {d["id"]: d for d in metadb.get_setting("destinations")}
        assert dests["s3d"]["credId"] == "cred-object-store-default"  # object-store dest tagged
        assert "credId" not in dests["loc"]                          # local dest untouched

        # a default cred configured this way reaches everything through cred_object_store_config(None)
        assert metadb.cred_object_store_config(None)["accessKeyId"] == "env:AK"
        assert metadb.cred_agent_api_key_ref() == "env:AGENT"


def test_migration_0026_is_a_noop_without_legacy_settings(tmp_path):
    clean = tmp_path / "clean.db"
    with _isolated_metadata(f"sqlite:///{clean}"):
        command.upgrade(metadb._alembic_cfg(), "0026_creds")
        assert metadb.creds_list() == []
        assert metadb.get_setting("defaultObjectStoreCredId") is None
        assert metadb.get_setting("agentCredId") is None


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
