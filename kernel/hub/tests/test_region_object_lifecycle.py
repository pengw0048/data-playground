from __future__ import annotations

import contextlib
import threading
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from hub import db, handoff, metadb
from hub.deps import Deps
from hub.models import ColumnSchema, Graph, ResourceSpec, RunOutput, RunStatus
from hub.planner import Region
from hub.run_controller import _RegionMaterialization
from hub.run_outputs import commit_output, sole_committed_document_output
from hub.tiers import Tier


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    """Keep lifecycle rows created by these concurrency tests out of the suite's shared database."""
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = f"sqlite:///{tmp_path / 'region-lifecycle.db'}"
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


class _ObjectAdapter:
    def __init__(self):
        self.rows: dict[str, int] = {}
        self.writes: list[str] = []
        self.write_hook = None
        self._lock = threading.Lock()

    @staticmethod
    def _root(uri: str) -> str:
        suffix = "/part-00000.parquet"
        return uri[:-len(suffix)] if uri.endswith(suffix) else uri.rstrip("/")

    def write(self, uri, rel, mode="overwrite", partition_by=None, cancelled=None):
        if cancelled and cancelled():
            raise RuntimeError("run cancelled before output commit")
        root = self._root(uri)
        rows = int(rel.aggregate("count(*)").fetchone()[0])
        with self._lock:
            if uri in self.writes:
                raise AssertionError("an immutable region attempt was overwritten")
            self.writes.append(uri)
            self.rows[root] = rows
        if self.write_hook is not None:
            self.write_hook(root)
        if cancelled and cancelled():
            raise RuntimeError("run cancelled before output commit")
        return {"uri": uri, "rows": rows}

    def scan(self, uri, columns=None, predicate=None, limit=None, options=None):
        rows = self.rows[self._root(uri)]
        rel = db.conn().sql(f"SELECT range AS value FROM range({rows})")
        return rel.limit(limit) if limit is not None else rel

    def schema(self, uri):
        if self._root(uri) not in self.rows:
            raise FileNotFoundError(uri)
        return [ColumnSchema(name="value", type="BIGINT")]

    def count(self, uri):
        return self.rows.get(self._root(uri))

    @staticmethod
    def fingerprint(uri):
        return f"object:{uri}"


def _inventory(uri: str) -> list[dict]:
    parsed = urlsplit(uri)
    root = f"{parsed.netloc}/{parsed.path.lstrip('/').rstrip('/')}"
    result = []
    for key, is_commit in (
            (f"{root}/part-00000.parquet", False),
            (handoff._object_manifest_path(root), True)):
        result.append({
            "member_id": handoff._member_id("unversioned_object", key, "null"),
            "key": key, "member_type": "unversioned_object", "size": 10,
            "etag": "test", "version_id": None, "upload_id": None,
            "is_latest": True, "is_commit": is_commit,
        })
    return result


@pytest.fixture
def region_env(tmp_path, monkeypatch):
    workspace, data_dir = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data_dir.mkdir()
    source = tmp_path / "source.parquet"
    with db.run_scope():
        db.conn().sql("SELECT range AS value FROM range(4)").write_parquet(str(source))

    deps = Deps(str(workspace), str(data_dir))
    object_adapter = _ObjectAdapter()
    real_resolve = deps.resolve_adapter

    def resolve(uri: str):
        return object_adapter if str(uri).startswith("s3://") else real_resolve(uri)

    monkeypatch.setattr(deps, "resolve_adapter", resolve)
    monkeypatch.setattr(deps.runner, "resolve_adapter", resolve)
    monkeypatch.setattr(db, "ensure_object_store", lambda: None)
    monkeypatch.setattr(handoff, "ensure_storage_namespace_claim", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("DP_STORAGE_URL", "s3://region-lifecycle/root")
    object_tier = Tier("object", "s3://region-lifecycle/root/regions", 10)
    monkeypatch.setattr(deps.controller, "_boundary_tier", lambda *_args, **_kwargs: object_tier)

    manifests: dict[str, dict] = {}
    state = SimpleNamespace(on_manifest=None)

    def write_manifest(uri: str, *, run_id: str, rows: int, schema) -> None:
        assert uri in object_adapter.rows
        manifests[uri] = {"runId": run_id, "rows": rows, "schema": str(schema)}
        if state.on_manifest is not None:
            state.on_manifest(uri)

    def prepare_attempt_commit(uri: str) -> None:
        if uri not in manifests:
            raise RuntimeError("SECRET_SENTINEL missing manifest")
        with metadb.session() as session:
            row = session.get(metadb.ObjectAttempt, uri)
            current = row.state if row is not None else None
        if current in ("committed", "published"):
            return
        if current != "writing":
            raise RuntimeError("SECRET_SENTINEL invalid lifecycle state")
        metadb.record_object_attempt_commit(uri, _inventory(uri))

    monkeypatch.setattr(handoff, "write_manifest", write_manifest)
    monkeypatch.setattr(handoff, "prepare_attempt_commit", prepare_attempt_commit)
    monkeypatch.setattr(handoff, "read_manifest", lambda uri: manifests.get(uri))
    monkeypatch.setattr(
        handoff, "validate_shards", lambda uri, manifest: uri in object_adapter.rows and bool(manifest))

    graph = Graph.model_validate({
        "id": "region-object-lifecycle", "version": 1,
        "nodes": [{"id": "source", "type": "source", "position": {"x": 0, "y": 0},
                   "data": {"config": {"uri": str(source)}}}],
        "edges": [],
    })
    region = Region(
        id="region-source", node_ids={"source"}, output_node="source",
        backend="default", worker=None, requires=ResourceSpec(), cut_inputs=[])
    subgraph = deps.controller._subgraph(graph, region, {})
    key = deps.runner._plan_hash(subgraph, region.output_node)
    cache_key = f"{key}@object"
    logical_uri = object_tier.uri(f"{region.id}_{key}.parquet")
    return SimpleNamespace(
        deps=deps, graph=graph, region=region, source=str(source), adapter=object_adapter,
        tier=object_tier, key=key, cache_key=cache_key, logical_uri=logical_uri,
        manifests=manifests, lifecycle=state,
    )


def _attempt_state(uri: str) -> str:
    with metadb.session() as session:
        return session.get(metadb.ObjectAttempt, uri).state


def _attempt_refs(uri: str) -> list[str]:
    with metadb.session() as session:
        return [row.ref_type for row in session.scalars(select(metadb.ObjectAttemptRef).where(
            metadb.ObjectAttemptRef.attempt_uri == uri))]


def test_region_allocation_attestation_failure_abandons_attempt(region_env, monkeypatch):
    controller = region_env.deps.controller
    allocated: list[str] = []
    real_allocate = handoff.allocate_attempt

    def track_allocate(**kwargs):
        handle = real_allocate(**kwargs)
        allocated.append(handle["uri"])
        return handle

    def fail_attestation(*_args, **_kwargs):
        raise RuntimeError("attestation failed")

    monkeypatch.setattr(handoff, "allocate_attempt", track_allocate)
    monkeypatch.setattr(controller, "_assert_region_attempt", fail_attestation)

    with pytest.raises(RuntimeError, match="region object lifecycle allocation failed"):
        controller._allocate_region_attempt(
            logical_uri=region_env.logical_uri,
            run_id="run-allocation-attestation-failure",
            region_id=region_env.region.id,
            cache_key=region_env.cache_key,
        )

    assert len(allocated) == 1
    assert _attempt_state(allocated[0]) == "abandoned"


def _committed_handle(logical_uri: str) -> dict:
    run_id = f"primitive-{uuid.uuid4().hex}"
    handle = handoff.allocate_attempt(
        logical_uri=logical_uri, kind="region", run_id=run_id,
        allocation_key=f"primitive:{run_id}",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
    )
    metadb.record_object_attempt_commit(handle["uri"], _inventory(handle["uri"]))
    return handle


def _committed_output(
        uri: str, rows: int, *, node_id: str = "source", port_id: str = "out") -> RunOutput:
    return RunOutput(
        node_id=node_id, port_id=port_id, wire="dataset",
        publication_kind="result", outcome="committed", uri=uri, rows=rows,
    )


def _cache_document(
        uri: str, rows: int, *, node_id: str = "source", port_id: str = "out") -> dict:
    return {"outputs": [_committed_output(
        uri, rows, node_id=node_id, port_id=port_id).model_dump()]}


def _cached_output(key: str) -> RunOutput:
    document = metadb.get_result(key)
    assert document is not None and set(document) == {"outputs"}
    output = sole_committed_document_output(document)
    assert output is not None and output.rows is not None
    return output


def _close(result) -> None:
    pin = getattr(result, "cache_pin", None)
    if pin is not None:
        pin.close()


def test_orchestrator_holds_region_cache_pin_until_final_region_stops(region_env, monkeypatch):
    env = region_env
    controller = env.deps.controller
    final = Region(
        id="final", node_ids={"final"}, output_node="final", backend="default",
        worker=None, requires=ResourceSpec(),
        cut_inputs=[("source", None, "final", None)])
    graph = Graph.model_validate({
        "id": "pin-lifetime", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": env.source}}},
            {"id": "final", "type": "filter", "position": {"x": 100, "y": 0},
             "data": {"config": {"predicate": "value > 0"}}},
        ],
        "edges": [{"id": "source-final", "source": "source", "target": "final"}],
    })

    class Pin:
        closed = False
        checks = 0

        def check(self):
            assert not self.closed
            self.checks += 1

        def close(self):
            assert not self.closed
            self.closed = True

    pin = Pin()
    run_id = f"pin-lifetime-{uuid.uuid4().hex}"
    controller.runs[run_id] = RunStatus(
        run_id=run_id, status="queued", placement="distributed", target_node_id="final",
        per_node=[], outputs=[RunOutput(
            node_id="final", port_id="out", wire="dataset",
            publication_kind="result", outcome="pending")])
    controller._cancel[run_id] = threading.Event()
    monkeypatch.setattr(
        controller, "_materialize",
        lambda *_args, **_kwargs: _RegionMaterialization(env.logical_uri, pin, rows=4))

    def run_final(*_args, **_kwargs):
        assert not pin.closed and pin.checks >= 2
        return RunStatus(
            run_id="final-subrun", status="done", placement="local", per_node=[],
            target_node_id="final", rows_processed=1, total_rows=1,
            outputs=[_committed_output(
                "/tmp/final.parquet", 1, node_id="final")])

    monkeypatch.setattr(controller, "_run_final", run_final)
    monkeypatch.setattr(controller, "on_status", None)
    monkeypatch.setattr(controller, "on_complete", None)
    controller._orchestrate(run_id, graph, "final", [env.region, final])
    assert controller.runs[run_id].status == "done"
    assert pin.closed


def test_region_failure_waits_for_every_sibling_stop_ack_before_terminal_status(
        region_env, monkeypatch):
    controller = region_env.deps.controller
    regions = [
        Region(
            id="first", node_ids={"first"}, output_node="first", backend="default",
            worker=None, requires=ResourceSpec(), cut_inputs=[]),
        Region(
            id="sibling", node_ids={"sibling"}, output_node="sibling", backend="default",
            worker=None, requires=ResourceSpec(), cut_inputs=[]),
        Region(
            id="final", node_ids={"final"}, output_node="final", backend="default",
            worker=None, requires=ResourceSpec(),
            cut_inputs=[("first", None, "final", None),
                        ("sibling", None, "final", None)]),
    ]
    graph = Graph(id="sibling-stop", version=1, nodes=[], edges=[])
    run_id = f"sibling-stop-{uuid.uuid4().hex}"
    controller.runs[run_id] = RunStatus(
        run_id=run_id, status="queued", placement="distributed",
        target_node_id="final", per_node=[])
    controller._cancel[run_id] = threading.Event()

    both_started = threading.Barrier(2)
    stop_requested = threading.Event()
    allow_sibling_unwind = threading.Event()
    stalled_published = threading.Event()
    final_calls = []

    class SiblingBackend:
        def cancel(self, sub_id):
            assert sub_id == "sub-sibling"
            stop_requested.set()

    sibling_backend = SiblingBackend()

    def materialize(_run_id, _graph, region, *_args, **_kwargs):
        if region.id == "sibling":
            controller._track_sub(run_id, sibling_backend, "sub-sibling")
            try:
                both_started.wait(timeout=5)
                assert stop_requested.wait(timeout=5)
                assert allow_sibling_unwind.wait(timeout=5)
                raise RuntimeError("sibling stopped after acknowledgement")
            finally:
                controller._untrack_sub(run_id, "sub-sibling")
        both_started.wait(timeout=5)
        raise RuntimeError("primary region boom")

    def on_status(_graph, status):
        if status.status == "running" and status.stalled:
            stalled_published.set()

    monkeypatch.setattr(controller, "_materialize", materialize)
    monkeypatch.setattr(
        controller, "_run_final",
        lambda *_args, **_kwargs: final_calls.append(True))
    monkeypatch.setattr(controller, "on_status", on_status)
    monkeypatch.setattr(controller, "on_complete", None)
    thread = threading.Thread(
        target=controller._orchestrate,
        args=(run_id, graph, "final", regions), daemon=True)
    thread.start()
    try:
        assert stop_requested.wait(timeout=5)
        assert stalled_published.wait(timeout=5)
        assert thread.is_alive()
        status = controller.runs[run_id]
        assert status.status == "running" and status.stalled is True
        assert run_id in controller._cancel
        assert run_id in controller._stop and controller._stop[run_id].is_set()
        assert controller._sub[run_id] == {"sub-sibling": sibling_backend}
        assert final_calls == []
        # Tracking remains live during reconciliation, so a user may re-send cancellation without
        # erasing the primary failure outcome or letting the controller claim an early terminal state.
        controller.cancel(run_id)
        assert controller._cancel[run_id].is_set()
        assert controller.runs[run_id].status == "running"
    finally:
        allow_sibling_unwind.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    status = controller.runs[run_id]
    assert status.status == "failed" and status.stalled is False
    assert "primary region boom" in status.error
    assert "sibling stopped" not in status.error
    assert final_calls == []
    assert run_id not in controller._cancel
    assert run_id not in controller._stop
    assert run_id not in controller._sub


def test_sqlite_local_runner_serializes_concurrent_first_cache_publications(
        region_env, monkeypatch):
    assert metadb.engine().dialect.name == "sqlite"
    env = region_env
    monkeypatch.setattr(env.deps.runner, "result_put", metadb.put_result)
    logical = f"s3://region-lifecycle/root/primitive/{uuid.uuid4().hex}.parquet"
    handles = [_committed_handle(logical) for _ in range(2)]
    cache_key = f"primitive-first-put-{uuid.uuid4().hex}"
    barrier = threading.Barrier(2)
    errors = []

    def publish(handle):
        try:
            barrier.wait(timeout=5)
            env.deps.runner._cache_put(cache_key, _cache_document(handle["uri"], 1))
        except BaseException as exc:  # noqa: BLE001 - asserted after both publishers stop
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(handle,)) for handle in handles]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert _cached_output(cache_key).uri in {handle["uri"] for handle in handles}
    states = [_attempt_state(handle["uri"]) for handle in handles]
    assert sorted(states) == ["published", "superseded"]


def test_sqlite_local_runner_cache_acquire_cannot_pin_a_superseded_generation(
        region_env, monkeypatch):
    assert metadb.engine().dialect.name == "sqlite"
    env = region_env
    monkeypatch.setattr(env.deps.runner, "result_put", metadb.put_result)
    logical = f"s3://region-lifecycle/root/primitive/{uuid.uuid4().hex}.parquet"
    first, second = _committed_handle(logical), _committed_handle(logical)
    cache_key = f"primitive-put-acquire-{uuid.uuid4().hex}"
    env.deps.runner._cache_put(cache_key, _cache_document(first["uri"], 1))
    barrier = threading.Barrier(2)
    acquired, errors = [], []

    def replace():
        try:
            barrier.wait(timeout=5)
            env.deps.runner._cache_put(cache_key, _cache_document(second["uri"], 1))
        except BaseException as exc:  # noqa: BLE001 - asserted after both operations stop
            errors.append(exc)

    def acquire():
        try:
            barrier.wait(timeout=5)
            acquired.append(env.deps.runner._cache_acquire(cache_key, "primitive-reader", 30))
        except BaseException as exc:  # noqa: BLE001 - asserted after both operations stop
            errors.append(exc)

    threads = [threading.Thread(target=replace), threading.Thread(target=acquire)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [] and len(acquired) == 1
    doc, guard = acquired[0]
    acquired_output = sole_committed_document_output(doc)
    assert acquired_output is not None
    assert acquired_output.uri in (first["uri"], second["uri"])
    assert guard is not None and _attempt_state(acquired_output.uri) == "published"
    assert "result_reader" in _attempt_refs(acquired_output.uri)
    guard.close()
    assert _attempt_state(second["uri"]) == "published"
    assert _attempt_state(first["uri"]) == "superseded"


def test_base_recompute_publishes_only_a_parent_owned_attempt(region_env, monkeypatch):
    env = region_env
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)
    run_id = f"base-{uuid.uuid4().hex}"
    env.deps.controller._cancel[run_id] = threading.Event()
    try:
        result = env.deps.controller._materialize(
            run_id, env.graph, env.region, {}, [env.region])
        assert str(result) != env.logical_uri and ".attempt-" in str(result)
        assert env.adapter.writes == [str(result) + "/part-00000.parquet"]
        assert _cached_output(env.cache_key).uri == str(result)
        assert _attempt_state(str(result)) == "published"
        assert sorted(_attempt_refs(str(result))) == ["result_cache", "result_reader"]
    finally:
        env.deps.controller._cancel.pop(run_id, None)
        if "result" in locals():
            _close(result)
    assert _attempt_refs(str(result)) == ["result_cache"]


def test_base_recompute_pins_exact_managed_source_during_blocked_scan_and_gc(
        region_env, monkeypatch):
    env = region_env
    controller = env.deps.controller
    monkeypatch.setattr(controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)

    source_logical = f"s3://region-lifecycle/root/source/{uuid.uuid4().hex}.parquet"
    old_source = _committed_handle(source_logical)
    new_source = _committed_handle(source_logical)
    env.adapter.rows[old_source["uri"]] = 4
    env.adapter.rows[new_source["uri"]] = 4
    source_cache_key = f"source-pointer-{uuid.uuid4().hex}"
    metadb.put_result(source_cache_key, _cache_document(old_source["uri"], 4))

    graph = Graph.model_validate({
        "id": "blocked-managed-source", "version": 1,
        "nodes": [{
            "id": "source", "type": "source", "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": old_source["uri"]}},
        }],
        "edges": [],
    })
    scan_entered = threading.Event()
    allow_scan = threading.Event()
    original_scan = env.adapter.scan

    def blocked_scan(uri, columns=None, predicate=None, limit=None, options=None):
        if env.adapter._root(uri) == old_source["uri"]:
            scan_entered.set()
            assert allow_scan.wait(timeout=5)
        return original_scan(
            uri, columns=columns, predicate=predicate, limit=limit, options=options)

    monkeypatch.setattr(env.adapter, "scan", blocked_scan)
    run_id = f"managed-source-{uuid.uuid4().hex}"
    controller._cancel[run_id] = threading.Event()
    results, errors = [], []

    def materialize():
        try:
            results.append(controller._materialize(
                run_id, graph, env.region, {}, [env.region]))
        except BaseException as exc:  # noqa: BLE001 - asserted after the reader unwinds
            errors.append(exc)

    thread = threading.Thread(target=materialize)
    thread.start()
    try:
        assert scan_entered.wait(timeout=5)
        with metadb.session() as session:
            leases = list(session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == old_source["uri"],
                metadb.ObjectAttemptLease.lease_type == "read",
            )))
        assert len(leases) == 1

        metadb.put_result(source_cache_key, _cache_document(new_source["uri"], 4))
        assert _attempt_state(old_source["uri"]) == "superseded"
        actions = metadb.object_attempt_gc_batch(0, 0)
        assert old_source["uri"] not in {action["uri"] for action in actions}
    finally:
        allow_scan.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == [] and len(results) == 1
    result = results[0]
    assert _attempt_state(str(result)) == "published"
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == old_source["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []

    _close(result)
    # The autouse metadata fixture drops this test's isolated result-cache owners.
    controller._cancel.pop(run_id, None)


def test_base_recompute_lost_source_lease_fails_before_manifest_or_cache_publication(
        region_env, monkeypatch):
    env = region_env
    controller = env.deps.controller
    monkeypatch.setattr(controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)

    class LostGuard:
        @staticmethod
        def check():
            raise FileNotFoundError("SECRET_SENTINEL lease renewal failed")

    @contextlib.contextmanager
    def lost_lease(*_args, **_kwargs):
        yield LostGuard()

    monkeypatch.setattr(handoff, "managed_read_lease", lost_lease)
    run_id = f"lost-source-lease-{uuid.uuid4().hex}"
    controller._cancel[run_id] = threading.Event()
    try:
        with pytest.raises(
                RuntimeError, match="region source ownership lease was lost") as raised:
            controller._materialize(
                run_id, env.graph, env.region, {}, [env.region])
        assert "SECRET_SENTINEL" not in str(raised.value)
        attempt = next(iter(env.adapter.rows))
        assert _attempt_state(attempt) == "abandoned"
        assert attempt not in env.manifests
        assert metadb.get_result(env.cache_key) is None
    finally:
        controller._cancel.pop(run_id, None)


def test_subprocess_region_gets_logical_target_and_returns_parent_attested_attempt(
        region_env, monkeypatch):
    env = region_env
    backend = next(runner for runner in env.deps.runners if runner.name == "local-subprocess")
    monkeypatch.setattr(backend, "resolve_adapter", env.deps.resolve_adapter)
    captured = {}

    def fake_spawn(status, job_extra, graph, target):
        attempt = job_extra["materializeUri"]
        captured["attempt"] = attempt
        with db.run_scope():
            rel = db.conn().read_parquet(env.source)
            written = env.adapter.write(attempt + "/part-00000.parquet", rel)
            handoff.write_manifest(
                attempt, run_id=job_extra["runId"], rows=written["rows"], schema=rel.types)
        commit_output(status, uri=attempt, rows=written["rows"])
        status.rows_processed = status.total_rows = written["rows"]
        status.status = "done"
        backend.runs[status.run_id] = status
        backend._emit(graph, status)
        return status

    original_run_unit = backend.run_unit

    def observed_run_unit(graph, output_node, output_uri, requires=None):
        captured["logical"] = output_uri
        return original_run_unit(graph, output_node, output_uri, requires=requires)

    monkeypatch.setattr(backend, "_spawn", fake_spawn)
    monkeypatch.setattr(backend, "run_unit", observed_run_unit)
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: backend)
    run_id = f"subprocess-{uuid.uuid4().hex}"
    env.deps.controller._cancel[run_id] = threading.Event()
    try:
        result = env.deps.controller._materialize(
            run_id, env.graph, env.region, {}, [env.region])
        assert captured["logical"] == env.logical_uri
        assert captured["attempt"] == str(result) and captured["attempt"] != env.logical_uri
        assert _attempt_state(str(result)) == "published"
        assert metadb.attest_object_attempt(
            str(result), logical_uri=env.logical_uri, kind="region",
            expected_run_id=next(iter(backend._object_results)),
        )["uri"] == str(result)
    finally:
        env.deps.controller._cancel.pop(run_id, None)
        if "result" in locals():
            _close(result)
        backend._object_results.clear()


def test_moto_subprocess_region_end_to_end_publishes_after_child_reap(
        tmp_path, monkeypatch, object_store_cred):
    pytest.importorskip("moto")
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0)
    server.start()
    runner = None
    result = None
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k", aws_secret_access_key="s",
            region_name="us-east-1")
        client.create_bucket(Bucket="subprocess-region-lifecycle")
        client.put_bucket_versioning(
            Bucket="subprocess-region-lifecycle", VersioningConfiguration={"Status": "Enabled"})
        object_store_cred({
            "endpoint": endpoint, "region": "us-east-1", "accessKeyId": "k",
            "secretAccessKey": "s",
        })
        monkeypatch.setenv(
            "DP_STORAGE_URL", "s3://subprocess-region-lifecycle/results")
        monkeypatch.setenv("DP_S3_ENDPOINT", endpoint)
        monkeypatch.setenv("DP_S3_KEY", "k")
        monkeypatch.setenv("DP_S3_SECRET", "s")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setattr(db, "ensure_object_store", lambda: None)

        workspace, data_dir = tmp_path / "moto-workspace", tmp_path / "moto-data"
        workspace.mkdir()
        data_dir.mkdir()
        source = tmp_path / "moto-source.parquet"
        pq.write_table(pa.table({"value": [1, 2, 3]}), source)
        deps = Deps(str(workspace), str(data_dir))
        runner = next(candidate for candidate in deps.runners
                      if candidate.name == "local-subprocess")
        tier = Tier(
            "object", "s3://subprocess-region-lifecycle/results/regions", 10)
        monkeypatch.setattr(deps.controller, "_boundary_tier", lambda *_args, **_kwargs: tier)
        monkeypatch.setattr(deps.controller, "_backend_runner", lambda *_args, **_kwargs: runner)

        def output_exists(uri: str) -> bool:
            parsed = urlsplit(uri)
            key = parsed.path.lstrip("/").rstrip("/") + "/part-00000.parquet"
            try:
                client.head_object(Bucket=parsed.netloc, Key=key)
                return True
            except client.exceptions.ClientError:
                return False

        monkeypatch.setattr(deps.runner, "_output_exists", output_exists)
        graph = Graph.model_validate({
            "id": "moto-subprocess-region", "version": 1,
            "nodes": [{
                "id": "source", "type": "source", "position": {"x": 0, "y": 0},
                "data": {"config": {"uri": str(source)}},
            }],
            "edges": [],
        })
        region = Region(
            id="moto-region", node_ids={"source"}, output_node="source",
            backend="default", worker=None, requires=ResourceSpec(), cut_inputs=[])
        run_id = f"moto-parent-{uuid.uuid4().hex}"
        deps.controller._cancel[run_id] = threading.Event()
        result = deps.controller._materialize(run_id, graph, region, {}, [region])

        assert ".attempt-" in str(result)
        parsed = urlsplit(str(result))
        key = parsed.path.lstrip("/").rstrip("/") + "/part-00000.parquet"
        body = client.get_object(Bucket=parsed.netloc, Key=key)["Body"].read()
        assert pq.read_table(pa.BufferReader(body)).num_rows == 3
        assert _attempt_state(str(result)) == "published"
        assert sorted(_attempt_refs(str(result))) == ["result_cache", "result_reader"]
        deps.controller._cancel.pop(run_id, None)
    finally:
        if result is not None:
            _close(result)
        if runner is not None:
            runner._terminate_all()
        object_store_cred(None)
        server.stop()


def test_cross_tier_copy_uses_a_new_attempt_and_pins_the_destination(region_env, monkeypatch):
    env = region_env
    env.deps.runner._cache_put(
        f"{env.key}@local", _cache_document(env.source, 4))
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)
    run_id = f"copy-{uuid.uuid4().hex}"
    env.deps.controller._cancel[run_id] = threading.Event()
    try:
        result = env.deps.controller._materialize(
            run_id, env.graph, env.region, {}, [env.region])
        assert str(result) != env.logical_uri and ".attempt-" in str(result)
        assert env.adapter.writes == [str(result) + "/part-00000.parquet"]
        assert env.adapter.rows[str(result)] == 4
        assert _cached_output(env.cache_key).uri == str(result)
        assert "result_reader" in _attempt_refs(str(result))
    finally:
        env.deps.controller._cancel.pop(run_id, None)
        if "result" in locals():
            _close(result)


@pytest.mark.parametrize("destination", ["object", "local"])
def test_cross_tier_copy_lost_source_pin_never_publishes_destination(
        region_env, monkeypatch, tmp_path, destination):
    env = region_env
    controller = env.deps.controller
    destination_tier = (env.tier if destination == "object" else
                        Tier("local", str(tmp_path / "local-regions"), 0))
    monkeypatch.setattr(controller, "_boundary_tier", lambda *_args, **_kwargs: destination_tier)

    class Pin:
        lost = False
        closed = False

        def check(self):
            if self.lost:
                raise FileNotFoundError("source result-cache pin was lost during copy")

        def close(self):
            self.closed = True

    pin = Pin()
    source_uri = env.source if destination == "object" else env.logical_uri
    alt = _RegionMaterialization(source_uri, pin, rows=4)

    def acquire(cache_key, **_kwargs):
        if cache_key.endswith(f"@{destination}"):
            return None
        return alt

    monkeypatch.setattr(controller, "_acquire_region_cache", acquire)
    original_move = controller._move_tier
    moves = []

    def move(*args, **kwargs):
        moves.append((args, kwargs))
        copied_rows = original_move(*args, **kwargs) if destination == "object" else 4
        pin.lost = True
        return copied_rows

    monkeypatch.setattr(controller, "_move_tier", move)
    run_id = f"lost-copy-pin-{destination}-{uuid.uuid4().hex}"
    controller._cancel[run_id] = threading.Event()
    cache_key = f"{env.key}@{destination}"
    try:
        with pytest.raises(FileNotFoundError, match="pin was lost during copy"):
            controller._materialize(
                run_id, env.graph, env.region, {}, [env.region])
        assert len(moves) == 1 and pin.closed is True
        assert metadb.get_result(cache_key) is None
        if destination == "object":
            attempt = next(iter(env.adapter.rows))
            assert _attempt_state(attempt) == "abandoned"
            assert _attempt_refs(attempt) == []
    finally:
        controller._cancel.pop(run_id, None)


@pytest.mark.parametrize("same_run", [False, True], ids=["separate-runs", "same-run-retry"])
def test_concurrent_same_hash_writers_never_share_a_physical_prefix(
        region_env, monkeypatch, same_run):
    env = region_env
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)
    barrier = threading.Barrier(2)
    env.adapter.write_hook = lambda _uri: barrier.wait(timeout=5)
    run_ids = ([f"concurrent-{uuid.uuid4().hex}"] * 2 if same_run else
               [f"concurrent-{uuid.uuid4().hex}" for _ in range(2)])
    for run_id in run_ids:
        env.deps.controller._cancel[run_id] = threading.Event()
    results, errors = [], []

    def materialize(run_id):
        try:
            results.append(env.deps.controller._materialize(
                run_id, env.graph, env.region, {}, [env.region]))
        except BaseException as exc:  # noqa: BLE001 - asserted after both threads stop
            errors.append(exc)

    threads = [threading.Thread(target=materialize, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        written_roots = {uri.removesuffix("/part-00000.parquet") for uri in env.adapter.writes}
        assert len(written_roots) == 2 and env.logical_uri not in written_roots
        assert len(results) == 2 and all(getattr(result, "cache_pin", None) for result in results)
        assert _cached_output(env.cache_key).uri in written_roots
    finally:
        for result in results:
            _close(result)
        for run_id in run_ids:
            env.deps.controller._cancel.pop(run_id, None)
    states = [_attempt_state(uri) for uri in written_roots]
    assert states.count("published") == 1
    assert set(states) <= {"published", "superseded", "abandoned"}


@pytest.mark.parametrize("failure", ["cache", "cancel"])
def test_cache_failure_or_prepublication_cancel_never_publishes_attempt(
        region_env, monkeypatch, failure):
    env = region_env
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)
    run_id = f"{failure}-{uuid.uuid4().hex}"
    cancel = threading.Event()
    env.deps.controller._cancel[run_id] = cancel
    if failure == "cache":
        def fail_cache(_key, _doc):
            raise RuntimeError("SECRET_SENTINEL cache backend unavailable")

        monkeypatch.setattr(env.deps.runner, "result_put", fail_cache)
    else:
        env.lifecycle.on_manifest = lambda _uri: cancel.set()

    try:
        with pytest.raises(RuntimeError) as raised:
            env.deps.controller._materialize(
                run_id, env.graph, env.region, {}, [env.region])
        assert "SECRET_SENTINEL" not in str(raised.value)
        assert metadb.get_result(env.cache_key) is None
        attempt = next(iter(env.adapter.rows))
        assert attempt != env.logical_uri and _attempt_state(attempt) == "abandoned"
        assert _attempt_refs(attempt) == []
    finally:
        env.deps.controller._cancel.pop(run_id, None)


def test_metadata_cleanup_outage_retains_committed_region_data(region_env, monkeypatch):
    env = region_env
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)
    monkeypatch.setattr(
        env.deps.runner, "result_put",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cache unavailable")))
    monkeypatch.setattr(
        metadb, "abandon_committed_object_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("metadata unavailable")))
    discarded = []
    monkeypatch.setattr(handoff, "discard_attempt", lambda uri: discarded.append(uri))
    run_id = f"cleanup-outage-{uuid.uuid4().hex}"
    env.deps.controller._cancel[run_id] = threading.Event()
    try:
        with pytest.raises(RuntimeError, match="region object lifecycle publication failed"):
            env.deps.controller._materialize(
                run_id, env.graph, env.region, {}, [env.region])
        attempt = next(iter(env.adapter.rows))
        assert _attempt_state(attempt) == "committed"
        assert env.adapter.rows[attempt] == 4
        assert discarded == []
        assert metadb.get_result(env.cache_key) is None
    finally:
        env.deps.controller._cancel.pop(run_id, None)


def test_cache_readback_recovers_an_after_commit_error(region_env, monkeypatch):
    env = region_env
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: env.deps.runner)

    def persist_then_report_failure(key, doc):
        metadb.put_result(key, doc)
        raise RuntimeError("SECRET_SENTINEL response was lost after commit")

    monkeypatch.setattr(env.deps.runner, "result_put", persist_then_report_failure)
    run_id = f"after-commit-{uuid.uuid4().hex}"
    env.deps.controller._cancel[run_id] = threading.Event()
    try:
        result = env.deps.controller._materialize(
            run_id, env.graph, env.region, {}, [env.region])
        assert _cached_output(env.cache_key).uri == str(result)
        assert _attempt_state(str(result)) == "published"
        assert sorted(_attempt_refs(str(result))) == ["result_cache", "result_reader"]
    finally:
        env.deps.controller._cancel.pop(run_id, None)
        if "result" in locals():
            _close(result)


def test_placed_backend_cannot_substitute_another_managed_attempt(region_env, monkeypatch):
    env = region_env
    other_logical = "s3://region-lifecycle/root/regions/other.parquet"

    class WrongBackend:
        name = "wrong"
        cancel_acknowledges_stop = True

        def __init__(self):
            self.runs = {}

        def run_unit(self, graph, output_node, output_uri, requires=None):
            run_id = f"wrong-{uuid.uuid4().hex}"
            handle = handoff.allocate_attempt(
                logical_uri=other_logical, kind="region", run_id=run_id,
                allocation_key=f"wrong:{run_id}",
                uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
                    other_logical, namespace, generation, attempt_id),
            )
            status = RunStatus(
                run_id=run_id, status="done", placement="distributed", per_node=[],
                target_node_id=output_node, total_rows=4,
                outputs=[_committed_output(handle["uri"], 4, node_id=output_node)])
            self.runs[run_id] = status
            return status

        def status(self, run_id):
            return self.runs[run_id]

        def cancel(self, run_id):
            return self.runs[run_id]

    backend = WrongBackend()
    monkeypatch.setattr(env.deps.controller, "_backend_runner", lambda *_args, **_kwargs: backend)
    run_id = f"wrong-parent-{uuid.uuid4().hex}"
    env.deps.controller._cancel[run_id] = threading.Event()
    try:
        with pytest.raises(RuntimeError, match="region object lifecycle attestation failed") as raised:
            env.deps.controller._materialize(
                run_id, env.graph, env.region, {}, [env.region])
        assert "other.parquet" not in str(raised.value)
        assert metadb.get_result(env.cache_key) is None
    finally:
        env.deps.controller._cancel.pop(run_id, None)
        for status in backend.runs.values():
            with contextlib.suppress(Exception):
                metadb.quarantine_object_attempt(status.outputs[0].uri, "test cleanup")
