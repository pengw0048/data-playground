"""Runtime and snapshot coverage for the public HTTP API contract."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hub.contracts.openapi import check_snapshot, render_openapi
from hub.api_errors import APIError, APIErrorCode, classify_http_error
from hub.main import app


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


def test_snapshot_check_returns_an_actionable_diff(tmp_path: Path):
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n", encoding="utf-8")

    matches, diff = check_snapshot('{"openapi": "3.1.0"}\n', stale)

    assert not matches
    assert str(stale) in diff
    assert "generated OpenAPI" in diff
    assert "-{}" in diff
    assert '+{"openapi": "3.1.0"}' in diff
