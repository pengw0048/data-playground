from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hub.deps import Deps
from hub.external_wait import (
    ExternalWaitCheckpoint,
    ExternalWaitDiagnostic,
    ExternalWaitDownloadEvidence,
    ExternalWaitHandle,
    ExternalWaitPollOutcome,
    ExternalWaitRetryHint,
    ExternalWaitSubmitRequest,
)


def _write_plugin(workspace: Path, name: str, body: str) -> None:
    package = workspace / "plugins" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body)


def test_external_wait_models_are_strict_immutable_and_bounded():
    request = ExternalWaitSubmitRequest(
        provider_kind=" Fixture-Local ", idempotency_key="stable:key", operation="fixture.run",
        document_json='{"b":2,"a":1}',
    )
    assert request.provider_kind == "fixture-local"
    assert request.document_json == '{"a":1,"b":2}'
    with pytest.raises(ValidationError):
        request.operation = "other"

    invalid_requests = (
        {"provider_kind": "fixture", "idempotency_key": "key", "operation": "run", "extra": True},
        {"provider_kind": "fixture", "idempotency_key": "key", "operation": "run",
         "document_json": '{"n":NaN}'},
        {"provider_kind": "fixture", "idempotency_key": "key", "operation": "run",
         "document_json": "{\"value\":\"" + "x" * 4097 + "\"}"},
        {"provider_kind": "fixture", "idempotency_key": "key", "operation": "run",
         "document_json": '{"n":9007199254740992}'},
        {"provider_kind": "fixture", "idempotency_key": "key", "operation": "run",
         "document_json": '{"duplicate":1,"duplicate":2}'},
    )
    for invalid in invalid_requests:
        with pytest.raises(ValidationError):
            ExternalWaitSubmitRequest.model_validate(invalid)

    with pytest.raises(ValidationError):
        ExternalWaitCheckpoint(sequence=1.0, token="checkpoint")
    with pytest.raises(ValidationError):
        ExternalWaitPollOutcome(phase="unknown")
    with pytest.raises(ValidationError):
        ExternalWaitPollOutcome(phase="running")
    with pytest.raises(ValidationError):
        ExternalWaitPollOutcome(
            phase="running", retry=ExternalWaitRetryHint(after_seconds=float("inf")))
    with pytest.raises(ValidationError):
        ExternalWaitPollOutcome(phase="failed")
    with pytest.raises(ValidationError):
        ExternalWaitPollOutcome(
            phase="succeeded", diagnostic=ExternalWaitDiagnostic(code="bad", message="bad"))
    with pytest.raises(ValidationError):
        ExternalWaitDownloadEvidence(
            result_id="result", bytes_written=1, sha256="BAD", media_type="application/octet-stream")


def test_adapter_returns_are_revalidated_even_for_constructed_models():
    malformed = ExternalWaitHandle.model_construct(provider_kind="fixture", job_id="x" * 257)
    with pytest.raises(ValidationError):
        ExternalWaitHandle.model_validate(malformed)


def test_external_wait_registry_rejects_invalid_and_duplicate_kinds(tmp_path):
    workspace = tmp_path / "workspace"
    adapter = (
        "class Adapter:\n"
        "    provider_kind = KIND\n"
        "    def submit(self, request): pass\n"
        "    def status(self, handle, checkpoint=None): pass\n"
        "    def cancel(self, handle, checkpoint=None): pass\n"
        "    def download(self, handle, target): pass\n"
        "def register(reg): reg.add_external_wait_adapter(Adapter())\n"
    )
    _write_plugin(workspace, "a_first", "KIND = ' Shared-Kind '\n" + adapter)
    _write_plugin(workspace, "b_duplicate", "KIND = 'shared-kind'\n" + adapter)
    _write_plugin(workspace, "c_invalid", "KIND = '../secret/path'\n" + adapter)
    _write_plugin(
        workspace, "d_invalid_shape",
        "class Adapter:\n"
        "    provider_kind = 'invalid-shape'\n"
        "    submit = None\n"
        "    status = None\n"
        "    cancel = None\n"
        "    download = None\n"
        "def register(reg): reg.add_external_wait_adapter(Adapter())\n",
    )

    deps = Deps(str(workspace), str(tmp_path / "data"), maintain_storage=False)
    status = {entry["name"]: entry for entry in deps.plugins}
    assert list(deps.external_wait_adapters) == ["shared-kind"]
    assert deps._external_wait_adapter(" SHARED-KIND ") is deps.external_wait_adapters["shared-kind"]
    assert deps._external_wait_adapter("not/valid") is None
    assert status["a_first"]["effective_capabilities"] == ["external-wait:shared-kind"]
    assert status["a_first"]["state"] == "active"
    assert status["b_duplicate"]["state"] == "conflict"
    assert status["b_duplicate"]["effective_capabilities"] == []
    assert status["c_invalid"]["state"] == "failed"
    assert status["c_invalid"]["effective_capabilities"] == []
    assert "secret" not in status["c_invalid"]["failure_summary"]
    assert status["d_invalid_shape"]["state"] == "failed"
    assert "invalid-shape" not in deps.external_wait_adapters


def test_external_wait_registry_is_owned_by_each_deps_instance(tmp_path):
    body = (
        "class Adapter:\n"
        "    provider_kind = 'fixture-local'\n"
        "    def submit(self, request): pass\n"
        "    def status(self, handle, checkpoint=None): pass\n"
        "    def cancel(self, handle, checkpoint=None): pass\n"
        "    def download(self, handle, target): pass\n"
        "def register(reg): reg.add_external_wait_adapter(Adapter())\n"
    )
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    _write_plugin(first_workspace, "isolated", body)
    _write_plugin(second_workspace, "isolated", body)
    first = Deps(str(first_workspace), str(tmp_path / "data-first"), maintain_storage=False)
    second = Deps(str(second_workspace), str(tmp_path / "data-second"), maintain_storage=False)

    assert first.external_wait_adapters is not second.external_wait_adapters
    assert first._external_wait_adapter("fixture-local") is not second._external_wait_adapter("fixture-local")
    assert next(p for p in first.plugins if p["name"] == "isolated")["state"] == "active"
    assert next(p for p in second.plugins if p["name"] == "isolated")["state"] == "active"
