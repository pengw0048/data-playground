"""Focused coverage for the pure-ASGI non-upload request body limit."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hub.main import RequestBodyLimitMiddleware, app
from hub.settings import settings


def _request(
    *,
    path: str = "/api/test",
    headers: list[tuple[bytes, bytes]] | None = None,
    chunks: list[bytes] | None = None,
) -> tuple[int, dict | bytes, int, bytes]:
    request_messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks or []) - 1}
        for index, chunk in enumerate(chunks or [b""])
    ]
    receive_calls = 0
    app_body = bytearray()
    response_messages: list[dict] = []

    async def receive() -> dict:
        nonlocal receive_calls
        receive_calls += 1
        if not request_messages:
            raise AssertionError("request body was read after its final ASGI message")
        return request_messages.pop(0)

    async def send(message: dict) -> None:
        response_messages.append(message)

    async def downstream(scope: dict, receive, send) -> None:
        while True:
            message = await receive()
            app_body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    asyncio.run(RequestBodyLimitMiddleware(downstream)(scope, receive, send))

    status = next(message["status"] for message in response_messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in response_messages if message["type"] == "http.response.body"
    )
    body: dict | bytes = json.loads(response_body) if response_body.startswith(b"{") else response_body
    return status, body, receive_calls, bytes(app_body)


def test_chunked_body_without_content_length_is_limited(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    status, body, receive_calls, app_body = _request(
        headers=[(b"transfer-encoding", b"chunked")],
        chunks=[b"abc", b"def"],
    )

    assert status == 413
    assert body == {
        "detail": "request body exceeds the 5-byte limit (raise DP_MAX_BODY_BYTES)",
        "code": "payload_too_large",
        "retryable": False,
    }
    assert receive_calls == 2
    assert app_body == b"abc"


def test_chunked_limit_remains_413_through_fastapi_body_parsing(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    response = TestClient(app).post(
        "/api/graph/compile",
        content=(chunk for chunk in (b'{"gra', b'ph":{}}')),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "request body exceeds the 5-byte limit (raise DP_MAX_BODY_BYTES)",
        "code": "payload_too_large",
        "retryable": False,
    }


def test_chunked_limit_overrides_endpoint_parse_error_translation(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    response = TestClient(app).post(
        "/mcp",
        content=(chunk for chunk in (b'{"json', b'rpc":"2.0"}')),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "request body exceeds the 5-byte limit (raise DP_MAX_BODY_BYTES)"
    }


def test_overflow_after_response_start_aborts_without_second_start(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)
    request_messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]
    response_messages: list[dict] = []

    async def receive() -> dict:
        return request_messages.pop(0)

    async def send(message: dict) -> None:
        response_messages.append(message)

    async def downstream(scope: dict, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await receive()

    scope = {
        "type": "http",
        "path": "/api/test",
        "headers": [(b"transfer-encoding", b"chunked")],
    }
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(RequestBodyLimitMiddleware(downstream)(scope, receive, send))

    assert exc_info.value.status_code == 413
    assert [
        message["status"] for message in response_messages if message["type"] == "http.response.start"
    ] == [200]


def test_body_at_exact_limit_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    status, body, _, app_body = _request(chunks=[b"ab", b"cde"])

    assert status == 200
    assert body == b"ok"
    assert app_body == b"abcde"


def test_underreported_content_length_cannot_bypass_actual_byte_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    status, _, receive_calls, app_body = _request(
        headers=[(b"content-length", b"2")],
        chunks=[b"abc", b"def"],
    )

    assert status == 413
    assert receive_calls == 2
    assert app_body == b"abc"


def test_invalid_content_length_falls_back_to_actual_bytes(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    status, _, receive_calls, app_body = _request(
        headers=[(b"content-length", b"invalid")],
        chunks=[b"abc", b"def"],
    )

    assert status == 413
    assert receive_calls == 2
    assert app_body == b"abc"


def test_declared_oversize_is_rejected_before_reading_body(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)

    status, body, receive_calls, app_body = _request(
        headers=[(b"content-length", b"6")],
        chunks=[b"abcdef"],
    )

    assert status == 413
    assert body == {
        "detail": "request body exceeds the 5-byte limit (raise DP_MAX_BODY_BYTES)",
        "code": "payload_too_large",
        "retryable": False,
    }
    assert receive_calls == 0
    assert app_body == b""


def test_catalog_upload_uses_its_independent_streaming_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 5)
    monkeypatch.setattr(settings, "max_upload_bytes", 6)
    body = b"a\n1\n2\n"
    landed_uri = None

    try:
        response = TestClient(app).post(
            "/api/catalog/upload",
            content=body,
            headers={"x-upload-filename": "body-limit-exempt.csv"},
        )
        if response.status_code == 200:
            landed_uri = response.json()["uri"]

        assert len(body) == 6 > settings.max_body_bytes
        assert response.status_code == 200

        monkeypatch.setattr(settings, "max_upload_bytes", 5)
        response = TestClient(app).post(
            "/api/catalog/upload",
            content=body,
            headers={"x-upload-filename": "body-limit-enforced.csv"},
        )
        assert response.status_code == 413
        assert response.json() == {
            "detail": "upload exceeds the 5-byte limit (raise DP_MAX_UPLOAD_BYTES)",
            "code": "payload_too_large",
            "retryable": False,
        }
    finally:
        if landed_uri is not None:
            try:
                os.remove(landed_uri)
            except FileNotFoundError:
                pass
