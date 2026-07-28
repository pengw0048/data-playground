"""Runtime and snapshot coverage for the public HTTP API contract."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hub.contracts.openapi import check_snapshot, render_openapi
from hub.api_errors import APIError, APIErrorCode, classify_http_error
from hub.main import app
from hub.models import RunEstimate


client = TestClient(app)


def _malformed_graph() -> dict:
    return {
        "id": "contract-invalid",
        "version": 1,
        "nodes": [
            {
                "id": "f",
                "type": "filter",
                "position": {"x": 0, "y": 0},
                "data": {"config": {}},
            }
        ],
        "edges": [
            {
                "id": "broken",
                "source": "missing",
                "target": "f",
                "data": {"wire": "dataset"},
            }
        ],
    }


def _assert_error(response, *, status: int, code: str, retryable: bool) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["detail"]
    assert body["code"] == code
    assert body["retryable"] is retryable


def test_canvas_not_found_has_stable_error_fields():
    response = client.get("/api/canvas/contract-missing")

    _assert_error(response, status=404, code="canvas_not_found", retryable=False)
    assert response.json()["detail"] == "canvas 'contract-missing' not found"


def test_invalid_graph_has_stable_error_fields():
    response = client.post(
        "/api/run/preview",
        json={"graph": _malformed_graph(), "nodeId": "f"},
    )

    _assert_error(response, status=400, code="invalid_graph", retryable=False)
    assert "missing source node 'missing'" in response.json()["detail"]


def test_run_confirmation_is_distinct_from_structural_conflicts(monkeypatch):
    from hub.routers import runs

    def needs_confirmation(*_args, **_kwargs):
        raise runs.RunNeedsConfirm(RunEstimate(placement="local", needs_confirm=True))

    monkeypatch.setattr(runs, "start_run", needs_confirmation)
    request = {"graph": {"id": "contract-run", "version": 1, "nodes": [], "edges": []}}
    confirmation = client.post("/api/run", json=request)
    _assert_error(confirmation, status=409, code="run_confirmation_required", retryable=False)

    def structural_conflict(*_args, **_kwargs):
        raise HTTPException(409, "linear checkpoint tasks require exactly Source -> Select(checkpoint) -> Write")

    monkeypatch.setattr(runs, "start_run", structural_conflict)
    conflict = client.post("/api/run", json=request)
    _assert_error(conflict, status=409, code="conflict", retryable=False)
    assert conflict.json()["detail"].startswith("linear checkpoint tasks require")


def test_unauthenticated_request_has_stable_error_fields(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "contract-test-secret")
    unauthenticated = TestClient(app)

    response = unauthenticated.get("/api/canvas")

    _assert_error(
        response,
        status=401,
        code="authentication_required",
        retryable=False,
    )
    assert response.json()["detail"] == "authentication required"


def test_upstream_agent_failure_has_stable_error_fields(monkeypatch):
    from hub.routers import runs

    monkeypatch.setattr(runs, "agent_status", lambda: {"available": True})

    def fail_agent(*_args, **_kwargs):
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(runs, "run_agent", fail_agent)
    response = client.post(
        "/api/agent",
        json={"outcome": "inspect", "graph": {"nodes": [], "edges": []}},
    )

    _assert_error(
        response,
        status=502,
        code="upstream_agent_failure",
        retryable=True,
    )
    assert response.json()["detail"] == "agent error: TimeoutError: provider timed out"


def test_request_validation_uses_the_same_error_envelope():
    response = client.post("/api/graph/compile", json={"graph": None})

    _assert_error(response, status=422, code="validation_error", retryable=False)
    assert isinstance(response.json()["detail"], list)


def test_unhandled_api_failure_is_stable_and_redacted():
    probe = FastAPI()
    probe.add_exception_handler(Exception, app.exception_handlers[Exception])

    @probe.get("/api/failure")
    def fail():
        raise RuntimeError("private failure detail")

    response = TestClient(probe, raise_server_exceptions=False).get("/api/failure")

    _assert_error(response, status=500, code="internal_error", retryable=False)
    assert response.json()["detail"] == "internal server error"
    assert "private failure detail" not in response.text


def test_generic_5xx_never_claims_retry_safety_without_an_explicit_contract():
    assert classify_http_error(HTTPException(503, "temporarily unavailable")) == (
        APIErrorCode.SERVICE_UNAVAILABLE,
        False,
    )
    assert classify_http_error(APIError(
        503,
        "admission is temporarily unavailable",
        code=APIErrorCode.SERVICE_UNAVAILABLE,
        retryable=True,
    )) == (APIErrorCode.SERVICE_UNAVAILABLE, True)


def test_committed_openapi_snapshot_matches_the_app():
    matches, diff = check_snapshot(render_openapi())

    assert matches, diff


def test_retired_row_identity_contract_has_no_public_surface():
    import importlib.util

    from hub import metadb

    contract = app.openapi()
    paths = contract["paths"]
    assert not any("row-identity-certifications" in path for path in paths)
    assert "/api/catalog/revision-media-cell" not in paths
    assert not any(path.endswith("/media-cell") for path in paths)
    schemas = contract["components"]["schemas"]
    assert not any(
        token in name.lower()
        for name in schemas
        for token in ("certification", "media_cell", "mediacell", "rowidentity")
    )
    error_codes = schemas["APIErrorCode"]["enum"]
    assert not any(str(code).startswith("media_cell_") for code in error_codes)
    for name in (
        "submit_row_identity_certification_task",
        "claim_row_identity_certification_task",
        "recoverable_row_identity_certification_task",
        "finish_row_identity_certification_scan",
        "finish_managed_local_lance_row_identity_certification_scan",
        "finish_row_identity_certification_failure",
    ):
        assert not hasattr(metadb, name)
    assert importlib.util.find_spec("hub.row_identity_tasks") is None


def test_durable_recovery_fanout_has_no_retired_certification_worker(monkeypatch):
    from hub import (
        bounded_fanout_tasks,
        distribution_report_tasks,
        durable_tasks,
        external_wait_tasks,
        keyed_upsert_tasks,
        linear_checkpoint_tasks,
        merge_columns_tasks,
        metadb,
        restore_revision_tasks,
    )

    calls: list[str] = []
    monkeypatch.setattr(metadb, "recoverable_durable_task_ids", lambda: [])
    for name, module in (
        ("external_wait", external_wait_tasks),
        ("linear_checkpoint", linear_checkpoint_tasks),
        ("merge_columns", merge_columns_tasks),
        ("restore_revision", restore_revision_tasks),
        ("keyed_upsert", keyed_upsert_tasks),
        ("bounded_fanout", bounded_fanout_tasks),
        ("distribution_report", distribution_report_tasks),
    ):
        monkeypatch.setattr(module, "recover", lambda *_args, name=name: calls.append(name))

    durable_tasks.recover(object())

    assert calls == [
        "external_wait",
        "linear_checkpoint",
        "merge_columns",
        "restore_revision",
        "keyed_upsert",
        "bounded_fanout",
        "distribution_report",
    ]


def test_snapshot_check_returns_an_actionable_diff(tmp_path: Path):
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n", encoding="utf-8")

    matches, diff = check_snapshot('{"openapi": "3.1.0"}\n', stale)

    assert not matches
    assert str(stale) in diff
    assert "generated OpenAPI" in diff
    assert "-{}" in diff
    assert '+{"openapi": "3.1.0"}' in diff
