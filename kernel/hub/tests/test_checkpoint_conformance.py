"""Installed-wheel coverage for ``python -m hub.checkpoint_conformance``."""

from __future__ import annotations

import ast
import inspect
import os
import shutil
import subprocess
from pathlib import Path


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, check=False, text=True, capture_output=True)


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key == "PYTHONPATH" or key.startswith("DP_"):
            env.pop(key)
    return env


def test_checkpoint_conformance_module_uses_only_production_lifecycle_apis():
    """The command must not invent an alternate persistence or filesystem lifecycle."""
    from hub import checkpoint_conformance

    source = inspect.getsource(checkpoint_conformance)
    tree = ast.parse(source)
    banned_calls = {
        "execute",  # raw SQL through a connection/session
    }
    banned_names = {
        "DurableCheckpoint",  # never construct/mutate the ORM row directly
        "text",  # no sqlalchemy.text SQL
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in banned_calls:
                raise AssertionError(f"direct persistence call: .{func.attr}()")
            if isinstance(func, ast.Name) and func.id in banned_names:
                raise AssertionError(f"banned constructor/call: {func.id}()")
        if isinstance(node, ast.Name) and node.id == "DurableCheckpoint":
            raise AssertionError("command must not name DurableCheckpoint")
    # Required production entry points must appear in the module body.
    required = (
        "submit_linear_checkpoint_task",
        "claim_linear_checkpoint_task",
        "reserve_linear_checkpoint_candidate",
        "materialize_and_commit_checkpoint",
        "reconcile_checkpoint",
        "reopen_checkpoint",
        "recover_checkpoint",
        "abort_checkpoint",
        "release_checkpoint",
        "prune_results",
    )
    for name in required:
        assert name in source, f"missing production API reference: {name}"
    assert "checkpoint conformance passed" in source


def test_checkpoint_conformance_wheel_passes_and_redacts_failures(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    kernel = repo / "kernel"
    uv = shutil.which("uv")
    assert uv is not None, "the supported wheel conformance path requires uv"

    core_dist = tmp_path / "core-dist"
    assert _run([uv, "build", "--wheel", "--out-dir", str(core_dist)], cwd=kernel).returncode == 0
    core_wheel, = core_dist.glob("data_playground-*.whl")

    venv = tmp_path / "venv"
    assert _run([uv, "venv", str(venv)], cwd=tmp_path).returncode == 0
    python = venv / "bin" / "python"
    installed = _run(
        [uv, "pip", "install", "--python", str(python), str(core_wheel)], cwd=tmp_path)
    assert installed.returncode == 0, installed.stderr

    clean = _clean_env()
    decoy = tmp_path / "decoy-developer-workspace"
    decoy.mkdir()
    (decoy / "dataplay.db").write_text("must-not-be-touched")
    secret = "path-token-secret-sentinel"
    env = clean | {
        "DP_WORKSPACE": str(decoy),
        "DP_DATA_DIR": str(decoy / "data"),
        "DP_DATABASE_URL": f"sqlite:///{decoy / 'metadata.db'}",
        "DP_PLUGINS": f"module-{secret}",
        "DP_EXECUTION": f"provider-{secret}",
    }

    checked = _run([str(python), "-m", "hub.checkpoint_conformance"], cwd=tmp_path, env=env)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.strip() == "checkpoint conformance passed"
    assert checked.stderr == ""
    assert (decoy / "dataplay.db").read_text() == "must-not-be-touched"
    assert secret not in checked.stdout + checked.stderr
    assert "Traceback" not in checked.stdout + checked.stderr
    assert "/private/" not in checked.stdout + checked.stderr
    assert "writer_token" not in checked.stdout + checked.stderr
    assert "lock_token" not in checked.stdout + checked.stderr

    # Repeated independent invocations remain green.
    again = _run([str(python), "-m", "hub.checkpoint_conformance"], cwd=tmp_path, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stdout.strip() == "checkpoint conformance passed"

    # A deliberate activation/setup failure stays bounded and redacted.
    broken = _run(
        [str(python), "-c",
         "import hub.checkpoint_conformance as c; "
         "c._bind_workspace = lambda p: (_ for _ in ()).throw(RuntimeError(%r)); "
         "raise SystemExit(c.main([]))" % f"boom-{secret}"],
        cwd=tmp_path, env=clean)
    assert broken.returncode == 1
    assert broken.stderr.strip() == "conformance: internal_error"
    assert secret not in broken.stdout + broken.stderr
    assert "Traceback" not in broken.stdout + broken.stderr


def test_checkpoint_conformance_negative_modes_emit_bounded_codes(tmp_path, monkeypatch, capsys):
    """In-process probe: a planted failure surfaces exactly one stage:code line."""
    from hub import checkpoint_conformance as cc

    monkeypatch.setattr(cc, "_positive", lambda workspace: None)

    def boom(workspace):
        raise cc._CheckFailed("stale_token", "accepted")

    monkeypatch.setattr(cc, "_negatives", boom)
    monkeypatch.setattr(cc, "_bind_workspace", lambda workspace: None)
    monkeypatch.setattr(cc, "_dispose_db", lambda: None)

    rc = cc.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "stale_token: accepted"
    assert captured.out == ""
    assert "Traceback" not in captured.err
