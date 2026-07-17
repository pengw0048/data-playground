"""Installed-wheel conformance command for one external-wait adapter."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from hub.external_wait import (
    ExternalWaitDownloadEvidence, ExternalWaitHandle, ExternalWaitPollOutcome,
    ExternalWaitSubmitRequest, normalize_provider_kind,
)


class _CheckFailed(Exception):
    def __init__(self, stage: str, code: str):
        self.stage = stage
        self.code = code


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hub.external_wait_conformance",
                                     description="Verify one installed dataplay.plugins external-wait adapter.")
    parser.add_argument("plugin", help="installed dataplay.plugins entry-point name")
    parser.add_argument("--provider-kind", required=True, help="registered external-wait provider kind")
    return parser.parse_args(argv)


def _safe_call(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def _validated(model, value: Any, stage: str, code: str):
    try:
        return model.model_validate(value)
    except Exception as exc:  # noqa: BLE001 — adapter values are never included in output
        raise _CheckFailed(stage, code) from exc


def _request(kind: str, scenario: str, suffix: str = "1") -> ExternalWaitSubmitRequest:
    return ExternalWaitSubmitRequest(
        provider_kind=kind,
        idempotency_key=f"external-wait-conformance-{scenario}-{suffix}",
        operation=f"conformance.{scenario}", document_json='{"purpose":"installed-wheel-conformance"}',
    )


def _handle(adapter, request: ExternalWaitSubmitRequest) -> ExternalWaitHandle:
    try:
        raw = _safe_call(adapter.submit, request)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("submit", "adapter_exception") from exc
    handle = _validated(ExternalWaitHandle, raw, "submit", "invalid_handle")
    if handle.provider_kind != request.provider_kind:
        raise _CheckFailed("submit", "provider_mismatch")
    return handle


def _outcome(adapter, handle, checkpoint=None) -> ExternalWaitPollOutcome:
    try:
        raw = _safe_call(adapter.status, handle, checkpoint)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("status", "adapter_exception") from exc
    return _validated(ExternalWaitPollOutcome, raw, "status", "invalid_outcome")


def _require_monotonic(outcomes: list[ExternalWaitPollOutcome]) -> None:
    order = {"accepted": 0, "running": 1, "succeeded": 2, "failed": 2, "cancelled": 2}
    if any(order[current.phase] < order[previous.phase]
           for previous, current in zip(outcomes, outcomes[1:], strict=False)):
        raise _CheckFailed("status", "phase_regressed")


def _poll(
    adapter, handle: ExternalWaitHandle, expected: tuple[str, ...], *, transient: bool = False,
) -> ExternalWaitPollOutcome:
    checkpoint = None
    prior_sequence = -1
    outcomes: list[ExternalWaitPollOutcome] = []
    for index, phase in enumerate(expected):
        if transient and index == 1:
            try:
                _safe_call(adapter.status, handle, checkpoint)
            except Exception:  # expected injected transport failure; raw detail stays captured
                pass
            else:
                raise _CheckFailed("status", "transient_failure_missing")
        outcome = _outcome(adapter, handle, checkpoint)
        if outcome.phase != phase:
            raise _CheckFailed("status", "unexpected_phase")
        if outcome.checkpoint is None or outcome.checkpoint.sequence < prior_sequence:
            raise _CheckFailed("status", "invalid_checkpoint")
        prior_sequence = outcome.checkpoint.sequence
        checkpoint = outcome.checkpoint
        outcomes.append(outcome)
        _require_monotonic(outcomes)
    terminal = outcomes[-1]
    if terminal.phase in {"succeeded", "failed", "cancelled"}:
        if _outcome(adapter, handle, checkpoint) != terminal:
            raise _CheckFailed("status", "unstable_terminal")
    return terminal


def _response_loss(adapter, kind: str) -> tuple[ExternalWaitHandle, ExternalWaitPollOutcome]:
    request = _request(kind, "response-loss")
    try:
        _safe_call(adapter.submit, request)
    except Exception:  # the fixture accepted before its response was lost
        pass
    else:
        raise _CheckFailed("submit", "response_loss_missing")
    first = _handle(adapter, request)
    replay = _handle(adapter, request)
    if first != replay:
        raise _CheckFailed("submit", "idempotency_mismatch")
    return first, _poll(adapter, first, ("accepted", "running", "succeeded"))


def _cancel(adapter, kind: str) -> None:
    handle = _handle(adapter, _request(kind, "cancelled"))
    accepted = _outcome(adapter, handle)
    if accepted.phase != "accepted":
        raise _CheckFailed("cancel", "precondition_failed")
    try:
        first_raw = _safe_call(adapter.cancel, handle, accepted.checkpoint)
        replay_raw = _safe_call(adapter.cancel, handle, accepted.checkpoint)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("cancel", "adapter_exception") from exc
    first = _validated(ExternalWaitPollOutcome, first_raw, "cancel", "invalid_outcome")
    replay = _validated(ExternalWaitPollOutcome, replay_raw, "cancel", "invalid_outcome")
    if first.phase != "cancelled" or first != replay or _outcome(adapter, handle) != first:
        raise _CheckFailed("cancel", "unstable_cancellation")


def _verify_file(target: Path, boundary, evidence: ExternalWaitDownloadEvidence) -> None:
    root, trusted_root, trusted_info = boundary
    try:
        current_root = root.lstat()
        resolved = target.resolve(strict=True)
        info = target.lstat()
        inside = resolved.is_relative_to(trusted_root)
    except OSError as exc:
        raise _CheckFailed("download", "invalid_target") from exc
    if ((current_root.st_dev, current_root.st_ino) != (trusted_info.st_dev, trusted_info.st_ino)
            or not stat.S_ISREG(info.st_mode) or not inside):
        raise _CheckFailed("download", "invalid_target")
    if info.st_size != evidence.bytes_written:
        raise _CheckFailed("download", "evidence_mismatch")
    try:
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise _CheckFailed("download", "invalid_target") from exc
    if digest != evidence.sha256:
        raise _CheckFailed("download", "evidence_mismatch")


def _download(adapter, handle: ExternalWaitHandle, root: Path) -> None:
    boundary = (root, root.resolve(strict=True), root.lstat())
    target = root / "result.bin"
    try:
        first_raw = _safe_call(adapter.download, handle, target)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("download", "adapter_exception") from exc
    first = _validated(ExternalWaitDownloadEvidence, first_raw, "download", "invalid_evidence")
    _verify_file(target, boundary, first)
    try:
        replay_raw = _safe_call(adapter.download, handle, target)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("download", "adapter_exception") from exc
    replay = _validated(ExternalWaitDownloadEvidence, replay_raw, "download", "invalid_evidence")
    _verify_file(target, boundary, replay)
    if replay != first:
        raise _CheckFailed("download", "idempotency_mismatch")


def _expect_rejected(fn, stage: str, codes: set[str]) -> None:
    try:
        fn()
    except _CheckFailed as exc:
        if exc.stage == stage and exc.code in codes:
            return
        raise
    raise _CheckFailed(stage, "invalid_value_accepted")


def _negative_cases(adapter, kind: str, root: Path) -> None:
    root.mkdir()
    oversized = _request(kind, "oversized-handle")
    _expect_rejected(lambda: _handle(adapter, oversized), "submit", {"invalid_handle"})

    for scenario in ("malformed-outcome", "non-finite-retry"):
        handle = _handle(adapter, _request(kind, scenario))
        _expect_rejected(lambda handle=handle: _outcome(adapter, handle), "status", {"invalid_outcome"})

    regressed = _handle(adapter, _request(kind, "regressed-phase"))
    first = _outcome(adapter, regressed)
    second = _outcome(adapter, regressed, first.checkpoint)
    third = _outcome(adapter, regressed, second.checkpoint)
    if (first.phase, second.phase, third.phase) != ("accepted", "running", "accepted"):
        raise _CheckFailed("status", "regression_fixture_invalid")
    _expect_rejected(lambda: _require_monotonic([first, second, third]),
                     "status", {"phase_regressed"})

    for scenario, codes in (
        ("invalid-download-digest", {"invalid_evidence"}),
        ("invalid-download-size", {"evidence_mismatch"}),
        ("invalid-download-path", {"invalid_target"}),
    ):
        handle = _handle(adapter, _request(kind, scenario))
        _poll(adapter, handle, ("accepted", "running", "succeeded"))
        case_root = root / scenario
        case_root.mkdir()
        _expect_rejected(lambda handle=handle, case_root=case_root: _download(adapter, handle, case_root),
                         "download", codes)


def _run(plugin: str, kind: str, workspace: Path) -> None:
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True)
    for key in tuple(os.environ):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            os.environ.pop(key, None)
    os.environ["DP_WORKSPACE"] = str(workspace)
    os.environ["DP_DATA_DIR"] = str(data_dir)

    from hub import metadb
    from hub.deps import Deps

    try:
        _safe_call(metadb.init_db)
        deps = _safe_call(Deps, str(workspace), str(data_dir), maintain_storage=False)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("activation", "load_failed") from exc
    status = next((entry for entry in deps.plugins if entry.get("name") == plugin), None)
    if status is None or status.get("state") != "active":
        raise _CheckFailed("activation", "entry_point_inactive")
    if status.get("effective_capabilities") != [f"external-wait:{kind}"]:
        raise _CheckFailed("activation", "capability_mismatch")
    adapter = deps._external_wait_adapter(kind)
    if adapter is None:
        raise _CheckFailed("activation", "adapter_missing")

    handle, _ = _response_loss(adapter, kind)
    downloads = workspace / "downloads"
    downloads.mkdir()
    _download(adapter, handle, downloads)
    _poll(adapter, _handle(adapter, _request(kind, "failed")), ("accepted", "failed"))
    _cancel(adapter, kind)
    _poll(adapter, _handle(adapter, _request(kind, "transient-status")),
          ("accepted", "running", "succeeded"), transient=True)
    _negative_cases(adapter, kind, workspace / "negative")

    second_workspace = workspace / "second-instance"
    second_data = second_workspace / "data"
    second_data.mkdir(parents=True)
    try:
        second = _safe_call(Deps, str(second_workspace), str(second_data), maintain_storage=False)
    except Exception as exc:  # noqa: BLE001
        raise _CheckFailed("isolation", "activation_failed") from exc
    second_adapter = second._external_wait_adapter(kind)
    if second_adapter is None or second_adapter is adapter:
        raise _CheckFailed("isolation", "instance_leak")
    try:
        _safe_call(second_adapter.status, handle, None)
    except Exception:
        pass
    else:
        raise _CheckFailed("isolation", "state_leak")


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        kind = normalize_provider_kind(args.provider_kind)
    except ValueError:
        print("activation: invalid_provider_kind", file=sys.stderr)
        return 1
    previous_log_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="dp-external-wait-conformance-") as directory:
            _run(args.plugin, kind, Path(directory))
    except _CheckFailed as exc:
        print(f"{exc.stage}: {exc.code}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 — no plugin or path detail may escape
        print("conformance: internal_error", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_log_disable)
    print("external-wait conformance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
