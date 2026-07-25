"""Focused safety contract for the release upgrade drill."""

from __future__ import annotations

import importlib.util
import subprocess
import traceback
import zipfile
from pathlib import Path
from typing import Iterable

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "upgrade_drill", _ROOT / "scripts" / "upgrade_drill.py")
assert _SPEC is not None and _SPEC.loader is not None
upgrade_drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upgrade_drill)


def _candidate_wheel(
    path: Path,
    *,
    filename_version: str = "9.8.6",
    dist_info_version: str = "9.8.5",
    metadata_headers: Iterable[tuple[str, str]] = (
        ("Metadata-Version", "2.1"),
        ("Name", "Data.Playground"),
        ("Version", "9.8.7"),
    ),
) -> Path:
    wheel = path / f"data_playground-{filename_version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"data_playground-{dist_info_version}.dist-info/METADATA",
            "".join(f"{name}: {value}\n" for name, value in metadata_headers),
        )
    return wheel


def test_candidate_identity_uses_wheel_metadata_and_accepts_matching_api(tmp_path) -> None:
    candidate_version = upgrade_drill.candidate_wheel_version(_candidate_wheel(tmp_path))

    assert candidate_version == "9.8.7"
    upgrade_drill.assert_candidate_identity(
        {"version": "9.8.7", "sha": "candidate-sha"}, candidate_version, "candidate-sha")


def test_candidate_identity_rejects_api_version_mismatch(tmp_path) -> None:
    candidate_version = upgrade_drill.candidate_wheel_version(_candidate_wheel(tmp_path))

    with pytest.raises(RuntimeError, match=r"candidate is not v9\.8\.7"):
        upgrade_drill.assert_candidate_identity(
            {"version": "9.8.6", "sha": "candidate-sha"}, candidate_version, "candidate-sha")


def test_candidate_identity_rejects_api_sha_mismatch(tmp_path) -> None:
    candidate_version = upgrade_drill.candidate_wheel_version(_candidate_wheel(tmp_path))

    with pytest.raises(RuntimeError, match=r"candidate is not v9\.8\.7"):
        upgrade_drill.assert_candidate_identity(
            {"version": "9.8.7", "sha": "wrong-sha"}, candidate_version, "candidate-sha")


def test_candidate_schema_head_uses_isolated_candidate_interpreter(monkeypatch, tmp_path) -> None:
    candidate_venv = tmp_path / "venv-candidate"
    observed: tuple[str, ...] | None = None

    def candidate_run(*command: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal observed
        observed = command
        return subprocess.CompletedProcess(command, 0, stdout="9.8.7_candidate_head\n")

    monkeypatch.setattr(upgrade_drill, "run", candidate_run)

    assert upgrade_drill.candidate_schema_head(candidate_venv) == "9.8.7_candidate_head"
    assert observed is not None
    assert observed[:3] == (str(candidate_venv / "bin" / "python"), "-I", "-c")
    assert "from hub import metadb" in observed[3]
    assert "module.is_relative_to(venv)" in observed[3]
    assert "migrations.is_relative_to(venv)" in observed[3]


def test_candidate_schema_rejects_a_migrated_head_that_disagrees_with_the_wheel() -> None:
    with pytest.raises(RuntimeError, match="target schema '0046' != '9.8.7_candidate_head'"):
        upgrade_drill.assert_candidate_schema("0046", "9.8.7_candidate_head")


@pytest.mark.parametrize("metadata_headers", [
    (
        ("Metadata-Version", "2.1"),
        ("Name", "data-playground"),
        ("Version", "9.8.7"),
        ("Version", "9.8.8"),
    ),
    (
        ("Name", "data-playground"),
        ("Version", "9.8.7"),
    ),
    (
        ("Metadata-Version", "2.1"),
        ("Name", "data-playground"),
        ("Version", "9.8"),
    ),
])
def test_candidate_wheel_metadata_rejects_malformed_headers(tmp_path, metadata_headers) -> None:
    with pytest.raises(RuntimeError) as caught:
        upgrade_drill.candidate_wheel_version(
            _candidate_wheel(tmp_path, metadata_headers=metadata_headers))

    assert str(caught.value) == "candidate wheel has invalid package metadata"


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
