"""Deterministically render and check the committed OpenAPI contract."""

from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


SNAPSHOT_PATH = Path(__file__).with_name("openapi.json")


def serialize_openapi(schema: dict[str, Any]) -> str:
    """Return the canonical, review-friendly representation used by the snapshot."""

    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_openapi() -> str:
    """Build the core app in an isolated local workspace and render its OpenAPI schema."""

    with tempfile.TemporaryDirectory(prefix="dataplay-openapi-") as workspace:
        # A contract render must never depend on a developer database, installed private plugin,
        # authentication secret, or provider environment. These are set before importing main,
        # where Settings is instantiated and local SQLite migrations run.
        env = os.environ.copy()
        env["DP_WORKSPACE"] = workspace
        env["DP_DATABASE_URL"] = f"sqlite:///{workspace}/contracts.db"
        env["DP_DATA_DIR"] = f"{workspace}/data"
        env["DP_EXECUTION"] = "local-out-of-core"
        env["DP_PLUGINS"] = ""
        env["PYTHONHASHSEED"] = "0"
        for name in (
            "DP_AUTH_MODE",
            "DP_AUTH_PASSWORD",
            "DP_AUTH_SECRET",
            "DP_PUBLIC_URL",
        ):
            env.pop(name, None)

        rendered = Path(workspace) / "openapi.json"
        result = subprocess.run(
            [sys.executable, "-m", "hub.contracts.openapi", "--render-to", str(rendered)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "isolated OpenAPI render failed:\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return rendered.read_text(encoding="utf-8")


def _render_to(path: Path) -> None:
    """Child-process entry point; environment isolation is owned by ``render_openapi``."""

    from hub.main import app

    app.openapi_schema = None
    path.write_text(serialize_openapi(app.openapi()), encoding="utf-8")


def snapshot_diff(expected: str, generated: str, path: Path = SNAPSHOT_PATH) -> str:
    """Produce an actionable unified diff for CI and local checks."""

    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=str(path),
            tofile="generated OpenAPI",
        )
    )


def check_snapshot(generated: str, path: Path = SNAPSHOT_PATH) -> tuple[bool, str]:
    """Compare one generated schema with the committed snapshot."""

    try:
        expected = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, f"OpenAPI snapshot is missing: {path}\nRun `make openapi`.\n"
    if expected == generated:
        return True, ""
    return False, snapshot_diff(expected, generated, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="regenerate the committed snapshot")
    action.add_argument("--check", action="store_true", help="fail if the committed snapshot drifted")
    action.add_argument("--render-to", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.render_to is not None:
        _render_to(args.render_to)
        return 0

    generated = render_openapi()
    if args.write:
        SNAPSHOT_PATH.write_text(generated, encoding="utf-8")
        print(f"wrote {SNAPSHOT_PATH}")
        return 0

    matches, diff = check_snapshot(generated)
    if matches:
        print(f"OpenAPI snapshot matches {SNAPSHOT_PATH}")
        return 0
    sys.stderr.write(diff)
    sys.stderr.write("Regenerate intentionally with `make openapi` and review the diff.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
