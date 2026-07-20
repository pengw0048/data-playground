"""Stable error envelope for the public HTTP API.

``detail`` remains the human-facing FastAPI field for backwards compatibility. The additive
``code`` and ``retryable`` fields are the machine contract: clients should branch on those fields,
never on prose in ``detail``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

class APIErrorCode(StrEnum):
    """Codes emitted by the core API.

    Adding or changing a code changes the committed OpenAPI snapshot and therefore requires an
    explicit contract review.
    """

    INVALID_REQUEST = "invalid_request"
    INVALID_GRAPH = "invalid_graph"
    OUTPUT_PORT_REQUIRED = "output_port_required"
    OUTPUT_PORT_NOT_FOUND = "output_port_not_found"
    MULTI_OUTPUT_UNSUPPORTED = "multi_output_unsupported"
    AUTHENTICATION_REQUIRED = "authentication_required"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CANVAS_NOT_FOUND = "canvas_not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    LOCAL_RUN_INPUT_BINDING_FAILED = "local_run_input_binding_failed"
    RESOURCE_GONE = "resource_gone"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    NOT_IMPLEMENTED = "not_implemented"
    UPSTREAM_FAILURE = "upstream_failure"
    UPSTREAM_AGENT_FAILURE = "upstream_agent_failure"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"


class APIErrorResponse(BaseModel):
    """Additive error response shared by all ``/api`` routes."""

    detail: Any = Field(description="Human-readable detail; clients must not parse this value.")
    code: APIErrorCode = Field(description="Stable machine-readable error code.")
    retryable: bool = Field(
        description=(
            "Whether the server knows that retrying the same request is safe under this operation's "
            "semantics."
        )
    )


# ``default`` documents errors raised by dependencies, middleware, and route code without claiming
# that every operation emits every status. Explicit 422 replaces FastAPI's built-in schema because
# runtime validation errors also carry the additive fields.
API_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {
        "model": APIErrorResponse,
        "description": "API error with a stable code and retry classification.",
    },
    422: {
        "model": APIErrorResponse,
        "description": "Request validation failed.",
    },
}


_STATUS_DEFAULTS: dict[int, tuple[APIErrorCode, bool]] = {
    400: (APIErrorCode.INVALID_REQUEST, False),
    401: (APIErrorCode.AUTHENTICATION_REQUIRED, False),
    403: (APIErrorCode.PERMISSION_DENIED, False),
    404: (APIErrorCode.NOT_FOUND, False),
    405: (APIErrorCode.METHOD_NOT_ALLOWED, False),
    409: (APIErrorCode.CONFLICT, False),
    410: (APIErrorCode.RESOURCE_GONE, False),
    413: (APIErrorCode.PAYLOAD_TOO_LARGE, False),
    422: (APIErrorCode.VALIDATION_ERROR, False),
    429: (APIErrorCode.RATE_LIMITED, True),
    # A generic 5xx has an unknown commit outcome. A POST may have committed its side effect before
    # response serialization or an upstream acknowledgement failed, so status alone can never make
    # the same request safe to repeat. Known pre-effect failures opt in with ``APIError`` instead.
    500: (APIErrorCode.INTERNAL_ERROR, False),
    501: (APIErrorCode.NOT_IMPLEMENTED, False),
    502: (APIErrorCode.UPSTREAM_FAILURE, False),
    503: (APIErrorCode.SERVICE_UNAVAILABLE, False),
    504: (APIErrorCode.UPSTREAM_TIMEOUT, False),
}


class APIError(HTTPException):
    """HTTP exception with an explicit stable API classification."""

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        code: APIErrorCode,
        retryable: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.retryable = retryable


def classify_http_error(exc: HTTPException) -> tuple[APIErrorCode, bool]:
    """Return the explicit classification or the stable status-level fallback."""

    if isinstance(exc, APIError):
        return exc.code, exc.retryable
    default = (
        (APIErrorCode.INTERNAL_ERROR, False)
        if exc.status_code >= 500
        else (APIErrorCode.INVALID_REQUEST, False)
    )
    return _STATUS_DEFAULTS.get(exc.status_code, default)


def api_error_response(
    *,
    status_code: int,
    detail: Any,
    code: APIErrorCode,
    retryable: bool,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Serialize the stable envelope without changing the existing ``detail`` value."""

    return JSONResponse(
        status_code=status_code,
        # Encode the original detail directly. RequestValidationError may carry exception objects in
        # ``ctx``; FastAPI's jsonable_encoder handles those, while forcing them through a Pydantic
        # model's JSON serializer first would fail before the compatibility field reaches the wire.
        content=jsonable_encoder({"detail": detail, "code": code, "retryable": retryable}),
        headers=headers,
    )
