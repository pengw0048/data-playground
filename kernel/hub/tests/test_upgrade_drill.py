"""Focused safety contract for the release upgrade drill."""

from __future__ import annotations

import importlib.util
import subprocess
import traceback
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "upgrade_drill", _ROOT / "scripts" / "upgrade_drill.py")
assert _SPEC is not None and _SPEC.loader is not None
upgrade_drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upgrade_drill)


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
