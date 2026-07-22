"""Focused safety contract for the release upgrade drill."""

from __future__ import annotations

import importlib.util
import subprocess
import traceback
import zipfile
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "upgrade_drill", _ROOT / "scripts" / "upgrade_drill.py")
assert _SPEC is not None and _SPEC.loader is not None
upgrade_drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upgrade_drill)


def _candidate_wheel(path: Path, version: str = "9.8.7") -> Path:
    wheel = path / f"data_playground-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"data_playground-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: data-playground\nVersion: {version}\n",
        )
    return wheel


def test_candidate_identity_uses_wheel_metadata_and_accepts_matching_api(tmp_path) -> None:
    candidate_version = upgrade_drill.candidate_wheel_version(_candidate_wheel(tmp_path))

    upgrade_drill.assert_candidate_identity(
        {"version": "9.8.7", "sha": "candidate-sha"}, candidate_version, "candidate-sha")


def test_candidate_identity_rejects_api_version_mismatch(tmp_path) -> None:
    candidate_version = upgrade_drill.candidate_wheel_version(_candidate_wheel(tmp_path))

    with pytest.raises(RuntimeError, match=r"candidate is not v9\.8\.7"):
        upgrade_drill.assert_candidate_identity(
            {"version": "9.8.6", "sha": "candidate-sha"}, candidate_version, "candidate-sha")


def test_failed_command_redacts_url_credentials_from_error_and_traceback(monkeypatch) -> None:
    secret = "postgresql://operator:do-not-leak@db.example/metadata"

    def fail(command, **_kwargs):
        raise subprocess.CalledProcessError(
            23, command, output="stdout do-not-leak", stderr="stderr do-not-leak")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError) as caught:
        upgrade_drill.run("pg_dump", secret)
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))

    assert "do-not-leak" not in str(caught.value)
    assert "do-not-leak" not in rendered
    assert str(caught.value) == (
        "command failed with exit code 23: pg_dump postgresql://***@db.example/metadata")
