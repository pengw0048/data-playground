#!/usr/bin/env python3
"""Exercise real-SPA and placeholder wheel packaging paths.

The release workflow runs this after ``web/dist`` is built. It proves:

* standard sdist-to-wheel and direct-wheel builds contain the exact built SPA;
* the sdist itself carries that exact SPA into its isolated wheel build; and
* a backend-only standard build still succeeds with a placeholder that the
  release wheel checker rejects.

Usage::

    python3 scripts/test_wheel_spa_packaging.py
    python3 scripts/test_wheel_spa_packaging.py --artifact-dir kernel/dist
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from check_wheel_has_spa import check_wheel


ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "kernel"
WEB_DIST = ROOT / "web" / "dist"
SDIST_WEB_PATH = "_web_dist"
WHEEL_WEB_PATH = "hub/_web"


def _run_uv_build(kernel: Path, out_dir: Path, *, wheel_only: bool = False) -> None:
    command = ["uv", "build"]
    if wheel_only:
        command.append("--wheel")
    command.extend(["--out-dir", str(out_dir)])
    subprocess.run(command, cwd=kernel, check=True)


def _one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"expected one {pattern} in {directory}, found {matches}")
    return matches[0]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _directory_manifest(directory: Path) -> dict[str, str]:
    if not (directory / "index.html").is_file():
        raise AssertionError(f"built SPA is missing {directory / 'index.html'}")
    return {
        path.relative_to(directory).as_posix(): _digest(path.read_bytes())
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _wheel_manifest(wheel: Path) -> dict[str, str]:
    prefix = f"{WHEEL_WEB_PATH}/"
    with zipfile.ZipFile(wheel) as archive:
        return {
            name.removeprefix(prefix): _digest(archive.read(name))
            for name in sorted(archive.namelist())
            if name.startswith(prefix) and not name.endswith("/")
        }


def _sdist_manifest(sdist: Path) -> dict[str, str]:
    marker = f"/{SDIST_WEB_PATH}/"
    with tarfile.open(sdist, "r:gz") as archive:
        manifest = {}
        for member in archive.getmembers():
            if marker not in member.name or not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AssertionError(f"could not read {member.name} from {sdist}")
            manifest[member.name.split(marker, 1)[1]] = _digest(extracted.read())
        return manifest


def _assert_exact(label: str, actual: dict[str, str], expected: dict[str, str]) -> None:
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(actual) & set(expected)
                         if actual[name] != expected[name])
        raise AssertionError(
            f"{label} does not match web/dist exactly; "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    print(f"ok: {label} matches web/dist exactly ({len(expected)} files)")


def _copy_backend_only_kernel(destination: Path) -> Path:
    copied_kernel = destination / "kernel"
    shutil.copytree(
        KERNEL,
        copied_kernel,
        ignore=shutil.ignore_patterns(
            ".venv", "dist", "__pycache__", ".pytest_cache", "*.pyc", "outputs"
        ),
    )
    return copied_kernel


def _assert_backend_only_placeholder(temp_dir: Path) -> None:
    copied_kernel = _copy_backend_only_kernel(temp_dir / "backend-only")
    out_dir = temp_dir / "backend-only-artifacts"
    _run_uv_build(copied_kernel, out_dir)
    placeholder_wheel = _one(out_dir, "*.whl")
    try:
        check_wheel(placeholder_wheel)
    except SystemExit as error:
        if "placeholder" not in str(error):
            raise AssertionError(f"unexpected release-check failure: {error}") from error
        print("ok: backend-only standard build succeeds and release check rejects its placeholder")
    else:
        raise AssertionError("release wheel checker accepted a backend-only placeholder")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Keep the standard sdist and wheel here (release workflow artifact directory)",
    )
    args = parser.parse_args(argv)

    expected = _directory_manifest(WEB_DIST)
    with tempfile.TemporaryDirectory(prefix="dp-wheel-spa-") as raw_temp:
        temp_dir = Path(raw_temp)
        standard_dir = args.artifact_dir.resolve() if args.artifact_dir else temp_dir / "standard"
        standard_dir.mkdir(parents=True, exist_ok=True)
        if any(standard_dir.glob("*.whl")) or any(standard_dir.glob("*.tar.gz")):
            raise SystemExit(f"artifact directory is not empty: {standard_dir}")

        direct_dir = temp_dir / "direct"
        _run_uv_build(KERNEL, standard_dir)
        _run_uv_build(KERNEL, direct_dir, wheel_only=True)

        standard_wheel = _one(standard_dir, "*.whl")
        direct_wheel = _one(direct_dir, "*.whl")
        sdist = _one(standard_dir, "*.tar.gz")
        check_wheel(standard_wheel)
        check_wheel(direct_wheel)
        _assert_exact("standard sdist-to-wheel SPA", _wheel_manifest(standard_wheel), expected)
        _assert_exact("direct-wheel SPA", _wheel_manifest(direct_wheel), expected)
        _assert_exact("sdist-carried SPA", _sdist_manifest(sdist), expected)
        _assert_backend_only_placeholder(temp_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
