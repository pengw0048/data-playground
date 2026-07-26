"""Regression coverage for development and tagged-release version identities."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version
import pytest


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "check_release_versions.py"
_SPEC = importlib.util.spec_from_file_location("check_release_versions", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_release_versions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_release_versions)


def _version_files(tmp_path: Path, version: str, api_version: str) -> tuple[Path, Path, Path]:
    pyproject = tmp_path / "pyproject.toml"
    package_json = tmp_path / "package.json"
    api = tmp_path / "api.json"
    pyproject.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    package_json.write_text(json.dumps({"version": version}), encoding="utf-8")
    api.write_text(json.dumps({"version": api_version}), encoding="utf-8")
    return pyproject, package_json, api


def test_development_version_normalizes_across_package_surfaces(tmp_path: Path, capsys) -> None:
    pyproject, package_json, api = _version_files(tmp_path, "0.3.0-dev.0", "0.3.0.dev0")

    assert check_release_versions.main([
        "--pyproject", str(pyproject), "--package-json", str(package_json),
        "--api-version-json", str(api), "--require", "pyproject,package_json,api",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.3.0.dev0"


def test_canonical_version_output_matches_python_metadata_spelling(tmp_path: Path, capsys) -> None:
    pyproject, _, _ = _version_files(tmp_path, "0.3.0-dev.0", "0.3.0.dev0")

    assert check_release_versions.main([
        "--pyproject", str(pyproject), "--print-version",
    ]) == 0
    assert capsys.readouterr().out == "0.3.0.dev0\n"


def test_development_identity_satisfies_reference_plugin_dependencies() -> None:
    core = tomllib.loads((_ROOT / "kernel" / "pyproject.toml").read_text(encoding="utf-8"))
    core_version = Version(core["project"]["version"])
    checked: set[str] = set()

    for plugin_path in sorted((_ROOT / "examples" / "plugins").glob("*/pyproject.toml")):
        plugin = tomllib.loads(plugin_path.read_text(encoding="utf-8"))
        for raw_requirement in plugin["project"].get("dependencies", []):
            requirement = Requirement(raw_requirement)
            if requirement.name == "data-playground":
                assert requirement.specifier.contains(core_version), (plugin_path, requirement, core_version)
                checked.add(plugin_path.parent.name)

    assert {"dp_descriptor_contract", "dp_sidecar_fixture"} <= checked


def test_release_gate_rejects_development_identity(tmp_path: Path) -> None:
    pyproject, package_json, api = _version_files(tmp_path, "0.3.0-dev.0", "0.3.0.dev0")

    with pytest.raises(SystemExit, match="exact clean version"):
        check_release_versions.main([
            "--pyproject", str(pyproject), "--package-json", str(package_json),
            "--api-version-json", str(api), "--release",
        ])


def test_release_gate_accepts_exact_clean_identity(tmp_path: Path) -> None:
    pyproject, package_json, api = _version_files(tmp_path, "1.2.3", "1.2.3")

    assert check_release_versions.main([
        "--pyproject", str(pyproject), "--package-json", str(package_json),
        "--api-version-json", str(api), "--release",
    ]) == 0
