"""Immutable, provider-neutral external-wait DTOs and adapter protocol for ``dataplay.plugins``."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}")
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MEDIA_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_DOCUMENT_BYTES = 16 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1


def normalize_provider_kind(value: object) -> str:
    """Return the canonical provider kind or reject it without echoing the supplied value."""
    if not isinstance(value, str):
        raise ValueError("provider kind must be a string")
    normalized = value.strip().lower()
    if _KIND_RE.fullmatch(normalized) is None:
        raise ValueError("provider kind must be a lowercase slug")
    return normalized


def _bounded_text(value: object, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty bounded string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _validate_document(value: object) -> str:
    value = _bounded_text(value, name="document_json", limit=_MAX_DOCUMENT_BYTES)
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(pairs) != len({key for key, _ in pairs}):
            raise ValueError("document_json contains a duplicate key")
        return dict(pairs)
    try:
        document = json.loads(value, object_pairs_hook=object_pairs)
    except (TypeError, ValueError) as exc:
        raise ValueError("document_json must be valid JSON") from exc

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 4:
            raise ValueError("document_json is too deeply nested")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            if abs(item) > _MAX_SAFE_INTEGER:
                raise ValueError("document_json contains an unsafe integer")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("document_json contains a non-finite number")
            return
        if isinstance(item, str):
            if len(item) > 4096:
                raise ValueError("document_json contains an oversized string")
            return
        if isinstance(item, list):
            if len(item) > 64:
                raise ValueError("document_json contains an oversized list")
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 32:
                raise ValueError("document_json contains an oversized object")
            for key, child in item.items():
                _bounded_text(key, name="document key", limit=128)
                visit(child, depth + 1)
            return
        raise ValueError("document_json contains a non-JSON value")

    visit(document)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(canonical.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise ValueError("document_json is oversized")
    return canonical


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
        revalidate_instances="always",
    )


class ExternalWaitSubmitRequest(_StrictModel):
    provider_kind: str
    idempotency_key: str
    operation: str
    document_json: str = "{}"

    _kind = field_validator("provider_kind", mode="before")(normalize_provider_kind)

    @field_validator("idempotency_key")
    @classmethod
    def _idempotency_key(cls, value: str) -> str:
        if _KEY_RE.fullmatch(value) is None:
            raise ValueError("idempotency_key is invalid")
        return value

    @field_validator("operation")
    @classmethod
    def _operation(cls, value: str) -> str:
        if _OPERATION_RE.fullmatch(value) is None:
            raise ValueError("operation is invalid")
        return value

    _document = field_validator("document_json", mode="before")(_validate_document)


class ExternalWaitHandle(_StrictModel):
    provider_kind: str
    job_id: str

    _kind = field_validator("provider_kind", mode="before")(normalize_provider_kind)
    _job_id = field_validator("job_id")(
        lambda value: _bounded_text(value, name="job_id", limit=256))


class ExternalWaitCheckpoint(_StrictModel):
    sequence: int = Field(ge=0, le=_MAX_SAFE_INTEGER)
    token: str

    _token = field_validator("token")(
        lambda value: _bounded_text(value, name="checkpoint token", limit=512))


class ExternalWaitRetryHint(_StrictModel):
    after_seconds: float = Field(gt=0, le=300)


class ExternalWaitDiagnostic(_StrictModel):
    code: str
    message: str

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if _CODE_RE.fullmatch(value) is None:
            raise ValueError("diagnostic code is invalid")
        return value

    _message = field_validator("message")(
        lambda value: _bounded_text(value, name="diagnostic message", limit=512))


class ExternalWaitPollOutcome(_StrictModel):
    phase: Literal["accepted", "running", "succeeded", "failed", "cancelled"]
    checkpoint: ExternalWaitCheckpoint | None = None
    retry: ExternalWaitRetryHint | None = None
    diagnostic: ExternalWaitDiagnostic | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ExternalWaitPollOutcome":
        terminal = self.phase in {"succeeded", "failed", "cancelled"}
        if terminal and self.retry is not None:
            raise ValueError("terminal outcomes cannot include a retry hint")
        if not terminal and self.retry is None:
            raise ValueError("non-terminal outcomes require a retry hint")
        if self.phase == "failed" and self.diagnostic is None:
            raise ValueError("failed outcomes require a diagnostic")
        if self.phase != "failed" and self.diagnostic is not None:
            raise ValueError("only failed outcomes can include a diagnostic")
        return self


class ExternalWaitDownloadEvidence(_StrictModel):
    result_id: str
    bytes_written: int = Field(ge=0, le=_MAX_SAFE_INTEGER)
    sha256: str
    media_type: str

    _result_id = field_validator("result_id")(
        lambda value: _bounded_text(value, name="result_id", limit=256))

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("sha256 is invalid")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if _MEDIA_TYPE_RE.fullmatch(value) is None:
            raise ValueError("media_type is invalid")
        return value


@runtime_checkable
class ExternalWaitAdapter(Protocol):
    provider_kind: str

    def submit(self, request: ExternalWaitSubmitRequest) -> ExternalWaitHandle: ...

    def status(
        self, handle: ExternalWaitHandle, checkpoint: ExternalWaitCheckpoint | None = None,
    ) -> ExternalWaitPollOutcome: ...

    def cancel(
        self, handle: ExternalWaitHandle, checkpoint: ExternalWaitCheckpoint | None = None,
    ) -> ExternalWaitPollOutcome: ...

    def download(
        self, handle: ExternalWaitHandle, target: Path,
    ) -> ExternalWaitDownloadEvidence: ...
