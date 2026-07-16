"""OPS-01 / issue #116 — telemetry metrics, audit events, request-ID, and sink isolation."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from hub.backends import ExecutionBackend
from hub.models import CompilePlan, Graph, Placement, RunEstimate, RunOutput, RunStatus
from hub.observability import (
    ALLOWED_METRIC_LABEL_KEYS,
    SCHEMA_VERSION,
    AuditAction,
    AuditEvent,
    AuditOutcome,
    InMemoryObservabilitySink,
    MetricEvent,
    MetricName,
    MetricType,
    MetricUnit,
    assert_event_redacted,
    add_audit_sink,
    add_metric_sink,
    clear_sinks,
    drain_sinks,
    emit_audit,
    emit_metric,
    invoke_backend_run,
    mint_request_id,
    normalize_request_id,
    shutdown_sinks,
)


@pytest.fixture(autouse=True)
def _clean_sinks():
    clear_sinks()
    yield
    clear_sinks()


def _labels_for(name: MetricName) -> dict[str, str]:
    if name in (MetricName.HTTP_REQUESTS, MetricName.HTTP_DURATION_MS):
        return {"method": "GET", "route_class": "api.livez", "outcome": "success"}
    if name == MetricName.KERNEL_HEALTH:
        return {"probe": "livez", "ready": "true"}
    if name in (MetricName.PUBLICATION, MetricName.STORAGE_GC, MetricName.PROVIDER_ERRORS):
        return {"kind": "publication" if name == MetricName.PUBLICATION else "gc",
                "outcome": "success", "error_class": "none"}
    if name in (MetricName.RUN_QUEUE_DELAY_MS, MetricName.RUN_RETRIES, MetricName.RUN_CANCEL_LATENCY_MS):
        return {"backend": "local", "error_class": "none", "outcome": "success", "placement": "local"}
    return {"status": "done", "outcome": "success", "placement": "local", "error_class": "none"}


def _committed_output(rows: int) -> RunOutput:
    return RunOutput(
        node_id="n",
        port_id="out",
        wire="dataset",
        publication_kind="result",
        outcome="committed",
        uri=f"/tmp/observability-result-{rows}.parquet",
        rows=rows,
    )


def test_metric_and_audit_models_carry_schema_version_and_match_catalog():
    metric = MetricEvent(
        name=MetricName.RUN_FINISHED, type=MetricType.COUNTER, unit=MetricUnit.UNIT,
        value=1, labels=_labels_for(MetricName.RUN_FINISHED),
    )
    audit = AuditEvent(
        action=AuditAction.JOB_SUBMIT, outcome=AuditOutcome.SUCCESS,
        principal_id="u1", resource_type="run", resource_id="run_abc",
        attrs={"placement": "local"},
    )
    assert metric.schema_version == SCHEMA_VERSION == 1
    assert audit.schema_version == SCHEMA_VERSION
    for name in MetricName:
        assert emit_metric(name, 0.0, labels=_labels_for(name)) is not None
    for action in AuditAction:
        assert emit_audit(action, AuditOutcome.SUCCESS, attrs={"note": "schema"}) is not None


def test_metric_labels_reject_raw_ids_and_unknown_keys():
    with pytest.raises(ValueError, match="allow-list"):
        MetricEvent(name=MetricName.RUN_FINISHED, type=MetricType.COUNTER, value=1,
                    labels={"canvas_id": "c1"})
    with pytest.raises(ValueError, match="raw id"):
        MetricEvent(name=MetricName.RUN_FINISHED, type=MetricType.COUNTER, value=1,
                    labels={"status": "run_deadbeef01"})
    assert "status" in ALLOWED_METRIC_LABEL_KEYS


def test_audit_attrs_reject_secret_shaped_values():
    with pytest.raises(ValueError, match="secret"):
        AuditEvent(action=AuditAction.ADMIN_SETTINGS_CHANGE, outcome=AuditOutcome.SUCCESS,
                   attrs={"password": "hunter2"})
    with pytest.raises(ValueError, match="secret"):
        AuditEvent(action=AuditAction.ADMIN_SETTINGS_CHANGE, outcome=AuditOutcome.SUCCESS,
                   attrs={"note": "api_key=abcd"})


def test_settings_change_audit_reaches_sinks():
    """Regression: admin.settings_change must reach sinks. It was silently dropped when put_setting
    tagged secret keys with an attr named 'secret', which the secret-name guard rejects — so the attrs
    shape put_setting emits (scope + sensitive) must stay guard-safe and emittable."""
    sink = InMemoryObservabilitySink().register()
    event = emit_audit(AuditAction.ADMIN_SETTINGS_CHANGE, AuditOutcome.SUCCESS, principal_id="admin",
                       resource_type="setting", resource_id="plugin.example.token",
                       attrs={"scope": "global", "sensitive": "true"})
    assert event is not None  # not rejected at construction (a 'secret'-named attr would be)
    assert drain_sinks()
    changes = [e for e in sink.audits if e.action == AuditAction.ADMIN_SETTINGS_CHANGE]
    assert changes and changes[-1].attrs.get("sensitive") == "true"


def test_inmemory_sink_records_shape_valid_events_and_redacts_fixtures():
    sink = InMemoryObservabilitySink().register()
    secret = "super-secret-token-VALUE"
    row_sample = "PII_ROW_VALUE_ALICE"
    emit_metric(MetricName.HTTP_REQUESTS, 1.0,
                labels={"method": "GET", "route_class": "api.livez", "outcome": "success"},
                request_id="req_test")
    emit_audit(AuditAction.AUTH_LOGIN, AuditOutcome.SUCCESS, principal_id="alice",
               attrs={"mode": "open"})
    assert drain_sinks()
    assert sink.metrics and sink.audits
    for event in (*sink.metrics, *sink.audits):
        assert event.schema_version == SCHEMA_VERSION
        assert_event_redacted(event, forbidden=[secret, row_sample])


def test_label_cardinality_bounded_across_many_canvases():
    sink = InMemoryObservabilitySink().register()
    for i in range(50):
        # Varying canvases/datasets must not enlarge the label set — only bucket labels are used.
        emit_metric(MetricName.RUN_FINISHED, 1.0,
                    labels={"status": "done", "outcome": "success", "placement": "local",
                            "error_class": "none"},
                    run_id=f"run_{i:04d}")
    assert drain_sinks()
    label_sets = sink.label_sets(MetricName.RUN_FINISHED)
    assert len(label_sets) == 50
    assert len(set(label_sets)) == 1  # cardinality does not grow with N


class _FakeBackend:
    name = "fake"

    def __init__(self):
        self.calls: list[dict] = []

    def can_run(self, plan: CompilePlan) -> bool:
        return True

    def estimate(self, plan: CompilePlan, rows: int | None, byts: int | None = None) -> RunEstimate:
        return RunEstimate(rows=rows, bytes=byts, placement="local", needs_confirm=False)

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None, placement: Placement,
            run_id: str | None = None, request_id: str | None = None,
            attempt_id: str | None = None) -> RunStatus:
        rid = run_id or "run_fake001"
        self.calls.append({"run_id": rid, "request_id": request_id, "attempt_id": attempt_id,
                           "placement": placement, "target": target_node_id})
        return RunStatus(run_id=rid, status="queued", placement=placement, request_id=request_id)

    def status(self, run_id: str) -> RunStatus:
        return RunStatus(run_id=run_id, status="done", placement="local")

    def cancel(self, run_id: str) -> RunStatus:
        return RunStatus(run_id=run_id, status="cancelled", placement="local")


def test_request_id_reaches_execution_backend_port_with_run_and_attempt_ids():
    backend = _FakeBackend()
    assert isinstance(backend, ExecutionBackend)
    graph = Graph(id="g1", version=1, nodes=[], edges=[])
    plan = CompilePlan(target_node_id=None, steps=[], acyclic=True)
    status = invoke_backend_run(
        backend, plan, graph, None, "local",
        run_id="run_abc1234567", request_id="req_from_http", attempt_id="att_9f3c")
    assert status.request_id == "req_from_http"
    assert backend.calls == [{
        "run_id": "run_abc1234567",
        "request_id": "req_from_http",
        "attempt_id": "att_9f3c",
        "placement": "local",
        "target": None,
    }]


def test_http_responses_echo_request_id_header():
    from hub.main import app
    with TestClient(app) as client:
        minted = client.get("/api/livez")
        assert minted.status_code == 200
        assert minted.headers.get("X-Request-Id", "").startswith("req_")
        custom = client.get("/api/livez", headers={"X-Request-Id": "req_client_supplied_01"})
        assert custom.headers.get("X-Request-Id") == "req_client_supplied_01"


def test_http_outcome_is_unchanged_by_throwing_and_blocking_metric_sinks():
    from hub.main import app

    started = threading.Event()
    release = threading.Event()

    def raising(_event):
        raise RuntimeError("metric exporter failed")

    def blocking(_event):
        started.set()
        release.wait(timeout=10)

    add_metric_sink(raising)
    add_metric_sink(blocking)
    try:
        with TestClient(app) as client:
            t0 = time.perf_counter()
            response = client.get("/api/livez")
            assert time.perf_counter() - t0 < 1.0
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            assert started.wait(timeout=1)
            release.set()
    finally:
        release.set()


def test_observability_registration_survives_repeated_app_lifespans():
    from hub.main import app

    sink = InMemoryObservabilitySink().register()
    with TestClient(app) as client:
        assert client.get("/api/livez").status_code == 200
    first_count = len(sink.metrics)
    assert first_count >= 3  # health gauge + request count + request duration

    with TestClient(app) as client:
        assert client.get("/api/livez").status_code == 200
    assert len(sink.metrics) >= first_count + 3


def test_run_persists_request_id_on_durable_record(tmp_path):
    from hub import metadb
    from hub.deps import Deps, _persist_run
    from hub.models import PerNodeStatus

    canvas_id = f"c_obs_{tmp_path.name}"
    metadb.init_db()
    with metadb.session() as s:
        from hub.metadb import Canvas, User
        if s.get(User, "local") is None:
            s.add(User(id="local", name="local"))
        if s.get(Canvas, canvas_id) is None:
            s.add(Canvas(id=canvas_id, owner_id="local", name="obs", doc="{}"))
    metadb.bind_run_request_id("run_persist01", "req_persisted_01", canvas_id=canvas_id)
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    d = Deps(str(tmp_path / "ws"), str(tmp_path / "data"), maintain_storage=False)
    g = Graph(id=canvas_id, version=1, nodes=[], edges=[])
    st = RunStatus(run_id="run_persist01", status="done", target_node_id="n",
                   total_rows=3, ms=11,
                   placement="local", request_id="req_persisted_01",
                   outputs=[_committed_output(3)],
                   per_node=[PerNodeStatus(node_id="n", status="done", rows=3, ms=11)])
    _persist_run(d, g, "n", st)
    rows = metadb.list_runs(canvas_id)
    assert rows and rows[0]["requestId"] == "req_persisted_01"
    assert rows[0]["outputs"][0]["uri"] == "/tmp/observability-result-3.parquet"
    assert not ({"outputUri", "outputTable"} & rows[0].keys())
    assert metadb.run_request_id("run_persist01") == "req_persisted_01"


def test_faulty_and_blocking_sinks_do_not_change_run_results(tmp_path):
    from hub.deps import Deps, Registry, _persist_run
    from hub.models import PerNodeStatus

    ws = tmp_path / "ws"
    ws.mkdir()
    d = Deps(str(ws), str(tmp_path / "data"), maintain_storage=False)
    blocked = threading.Event()
    telemetry_received = threading.Event()
    metric_received = threading.Event()

    def raising(_record):
        raise RuntimeError("sink boom")

    def blocking(_record):
        blocked.wait(timeout=30)  # held until test cleanup; only this sink's worker is occupied

    good: list[dict] = []

    def good_telemetry(record):
        good.append(record)
        telemetry_received.set()

    Registry(d).add_telemetry_sink(raising)
    Registry(d).add_telemetry_sink(blocking)
    Registry(d).add_telemetry_sink(good_telemetry)

    def raising_metric(_e):
        raise RuntimeError("metric boom")

    def blocking_metric(_e):
        blocked.wait(timeout=30)

    add_metric_sink(raising_metric)
    add_metric_sink(blocking_metric)

    metrics: list[MetricEvent] = []

    def good_metric(event):
        metrics.append(event)
        if event.name == MetricName.RUN_FINISHED:
            metric_received.set()

    add_metric_sink(good_metric)

    g = Graph(id="no_such_canvas", version=1, nodes=[], edges=[])
    st = RunStatus(run_id="run_iso01", status="done", target_node_id="n",
                   total_rows=7, ms=5, placement="local",
                   request_id="req_iso",
                   outputs=[_committed_output(7)],
                   per_node=[PerNodeStatus(node_id="n", status="done", rows=7, ms=5)])
    t0 = time.perf_counter()
    try:
        _persist_run(d, g, "n", st)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0
        assert telemetry_received.wait(timeout=1)
        assert metric_received.wait(timeout=1)
        assert st.status == "done" and st.total_rows == 7 and st.ms == 5
        assert good[0]["run_id"] == "run_iso01" and good[0]["request_id"] == "req_iso"
        assert good[0]["outputs"][0]["uri"] == "/tmp/observability-result-7.parquet"
        assert any(m.name == MetricName.RUN_FINISHED for m in metrics)
    finally:
        blocked.set()
    assert drain_sinks()


def test_legacy_telemetry_sink_still_receives_finished_run_records(tmp_path):
    from hub.deps import Deps, Registry, _persist_run
    from hub.models import PerNodeStatus

    ws = tmp_path / "ws"
    ws.mkdir()
    d = Deps(str(ws), str(tmp_path / "data"), maintain_storage=False)
    got: list[dict] = []
    Registry(d).add_telemetry_sink(got.append)
    g = Graph(id="no_such_canvas", version=1, nodes=[], edges=[])
    _persist_run(d, g, "n", RunStatus(
        run_id="run_legacy01", status="done", target_node_id="n",
        total_rows=2, ms=3, placement="local",
        request_id="req_legacy",
        outputs=[_committed_output(2)],
        per_node=[PerNodeStatus(node_id="n", status="done", rows=2, ms=3)]))
    assert drain_sinks()
    assert len(got) == 1
    assert got[0]["run_id"] == "run_legacy01"
    assert got[0]["request_id"] == "req_legacy"
    assert set(got[0]) >= {
        "canvas_id", "target_node_id", "run_id", "request_id", "job_type", "status",
        "rows", "ms", "error", "outputs", "placement", "per_node",
    }
    assert got[0]["outputs"][0]["port_id"] == "out"
    assert not ({"output_uri", "output_table"} & got[0].keys())


def test_request_id_middleware_asgi_unit():
    from hub.main import RequestIdMiddleware

    response_headers: list[tuple[bytes, bytes]] = []

    async def downstream(scope, receive, send):
        from hub.observability import get_request_id
        assert get_request_id() == "req_unit_test_01"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response_headers.extend(message.get("headers") or [])

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/api/livez",
        "raw_path": b"/api/livez", "query_string": b"",
        "headers": [(b"x-request-id", b"req_unit_test_01")],
        "client": ("test", 1), "server": ("test", 80),
    }
    asyncio.run(RequestIdMiddleware(downstream)(scope, receive, send))
    assert (b"x-request-id", b"req_unit_test_01") in response_headers


def test_normalize_request_id_rejects_unsafe_tokens():
    assert normalize_request_id("req_ok-1").startswith("req_ok")
    assert normalize_request_id("bad id with spaces").startswith("req_")
    assert normalize_request_id(None).startswith("req_")
    assert mint_request_id().startswith("req_")


def test_blocking_sinks_do_not_delay_or_starve_healthy_sink():
    release = threading.Event()
    started = [threading.Event(), threading.Event()]
    healthy_received = threading.Event()
    seen: list[MetricEvent] = []

    def blocking(index):
        def sink(_event):
            started[index].set()
            release.wait(timeout=10)
        return sink

    def healthy(event):
        seen.append(event)
        if len(seen) == 2:
            healthy_received.set()

    add_metric_sink(blocking(0))
    add_metric_sink(blocking(1))
    add_metric_sink(healthy)
    try:
        t0 = time.perf_counter()
        emit_metric(MetricName.HTTP_REQUESTS, labels={
            "method": "GET", "route_class": "api.livez", "outcome": "success",
        })
        assert time.perf_counter() - t0 < 1.0
        assert all(event.wait(timeout=1) for event in started)

        emit_metric(MetricName.HTTP_REQUESTS, labels={
            "method": "GET", "route_class": "api.livez", "outcome": "success",
        })
        assert healthy_received.wait(timeout=1)
        assert len(seen) == 2
    finally:
        release.set()
    assert drain_sinks()


def test_throwing_sink_worker_recovers_for_later_events():
    recovered = threading.Event()
    calls = 0

    def flaky(_event):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first delivery fails")
        recovered.set()

    add_metric_sink(flaky)
    for _ in range(2):
        emit_metric(MetricName.HTTP_REQUESTS, labels={
            "method": "GET", "route_class": "api.livez", "outcome": "success",
        })
    assert recovered.wait(timeout=1)
    assert drain_sinks()
    from hub import observability
    stats = observability._sink_delivery_stats()
    assert len(stats) == 1
    assert {key: stats[0][key] for key in (
        "kind", "accepted", "delivered", "failed", "dropped", "pending", "active",
    )} == {
        "kind": "metric", "accepted": 2, "delivered": 1, "failed": 1,
        "dropped": 0, "pending": 0, "active": False,
    }


def test_sink_queue_saturation_is_bounded_and_logged(caplog):
    from hub import observability

    release = threading.Event()
    started = threading.Event()

    def blocking(_event):
        started.set()
        release.wait(timeout=10)

    add_metric_sink(blocking)
    try:
        emit_metric(MetricName.HTTP_REQUESTS, labels={
            "method": "GET", "route_class": "api.livez", "outcome": "success",
        })
        assert started.wait(timeout=1)
        for _ in range(observability._SINK_QUEUE_CAPACITY + 1):
            emit_metric(MetricName.HTTP_REQUESTS, labels={
                "method": "GET", "route_class": "api.livez", "outcome": "success",
            })
        stats = observability._sink_delivery_stats()[0]
        assert stats["pending"] == observability._SINK_QUEUE_CAPACITY
        assert stats["dropped"] == 1
        assert "queue is full" in caplog.text
        assert "dropped=1" in caplog.text
        t0 = time.perf_counter()
        assert drain_sinks(timeout=0.02) is False
        assert time.perf_counter() - t0 < 0.25
    finally:
        release.set()
    assert drain_sinks()


def test_sink_worker_count_is_bounded(monkeypatch):
    from hub import observability

    monkeypatch.setattr(observability, "_MAX_SINK_WORKERS", 2)
    add_metric_sink(lambda _event: None)
    add_audit_sink(lambda _event: None)
    with pytest.raises(RuntimeError, match="sink limit reached"):
        add_metric_sink(lambda _event: None)
    assert len(observability._sink_delivery_stats()) == 2


def test_shutdown_flushes_healthy_sinks_and_bounds_wedged_sink_wait():
    delivered: list[MetricEvent] = []
    add_metric_sink(delivered.append)
    emit_metric(MetricName.HTTP_REQUESTS, labels={
        "method": "GET", "route_class": "api.livez", "outcome": "success",
    })
    assert shutdown_sinks(timeout=1)
    assert len(delivered) == 1

    started = threading.Event()
    release = threading.Event()

    def wedged(_event):
        started.set()
        release.wait(timeout=10)

    add_metric_sink(wedged)
    emit_metric(MetricName.HTTP_REQUESTS, labels={
        "method": "GET", "route_class": "api.livez", "outcome": "success",
    })
    assert started.wait(timeout=1)
    try:
        assert shutdown_sinks(timeout=0.01) is False
    finally:
        release.set()
    assert shutdown_sinks(timeout=1)


def test_docs_and_models_agree_on_metric_names():
    from pathlib import Path
    doc = Path(__file__).resolve().parents[3] / "docs" / "OBSERVABILITY.md"
    text = doc.read_text()
    for name in MetricName:
        assert f"`{name.value}`" in text, name
    for action in AuditAction:
        assert f"`{action.value}`" in text, action
    assert "add_telemetry_sink" in text
    assert "schema_version = 1" in text
