#!/usr/bin/env python3
"""Enforce one version identity across packaging surfaces (REL-01 / issue #114).

Compares sources that are present; missing optional sources are skipped unless
``--require`` lists them. Fail when any present value disagrees. Python normalizes
the checked-in SemVer development spelling ``X.Y.Z-dev.N`` to ``X.Y.Z.devN`` in
wheel metadata, so compare those equivalent spellings canonically.

Examples::

    python3 scripts/check_release_versions.py \\
      --pyproject kernel/pyproject.toml \\
      --package-json web/package.json \\
      --wheel dist/*.whl \\
      --api-version-json /tmp/version.json \\
      --image-label 1.2.3 \\
      --git-tag          # only when GITHUB_REF_TYPE=tag

    # Fail unless every named source was supplied and agreed:
    python3 scripts/check_release_versions.py ... --require pyproject,package_json,wheel,api,image
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path


_SEMVER_DEV = re.compile(r"^(?P<release>\d+(?:\.\d+){2,})-dev\.(?P<serial>\d+)$")
_CLEAN_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")


def _canonical_version(value: str) -> str:
    """Match Python's metadata spelling for the supported SemVer dev convention."""
    if match := _SEMVER_DEV.fullmatch(value):
        return f"{match.group('release')}.dev{match.group('serial')}"
    return value


def _read_pyproject(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit(f"no version= in {path}")
    return m.group(1)


def _read_package_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    v = data.get("version")
    if not isinstance(v, str) or not v:
        raise SystemExit(f"no version in {path}")
    return v


def _read_wheel(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        metas = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if not metas:
            raise SystemExit(f"no METADATA in wheel {path}")
        meta = zf.read(metas[0]).decode("utf-8", errors="replace")
    m = re.search(r"(?m)^Version:\s*(\S+)\s*$", meta)
    if not m:
        raise SystemExit(f"no Version: in METADATA of {path}")
    return m.group(1)


def _read_api_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    v = data.get("version")
    if not isinstance(v, str) or not v:
        raise SystemExit(f"no version field in {path}: {data!r}")
    return v


def _git_tag_version() -> str | None:
    """Return the semver from GITHUB_REF_NAME when this is a tag build, else None."""
    if os.environ.get("GITHUB_REF_TYPE") != "tag":
        return None
    name = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not name:
        return None
    return name[1:] if name.startswith("v") else name


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pyproject", type=Path)
    p.add_argument("--package-json", type=Path)
    p.add_argument("--wheel", type=Path)
    p.add_argument("--api-version-json", type=Path,
                   help="Saved GET /api/version JSON body")
    p.add_argument("--image-label",
                   help="org.opencontainers.image.version from docker inspect")
    p.add_argument("--git-tag", action="store_true",
                   help="When GITHUB_REF_TYPE=tag, require the tag to match")
    p.add_argument("--release", action="store_true",
                   help="Require an exact clean release version, never a development identity")
    p.add_argument("--print-version", action="store_true",
                   help="Print the canonical agreed version without JSON wrapping")
    p.add_argument("--require", default="",
                   help="Comma-separated sources that must be present: "
                        "pyproject,package_json,wheel,api,image,tag")
    args = p.parse_args(argv)

    found: dict[str, str] = {}
    if args.pyproject:
        found["pyproject"] = _read_pyproject(args.pyproject)
    if args.package_json:
        found["package_json"] = _read_package_json(args.package_json)
    if args.wheel:
        found["wheel"] = _read_wheel(args.wheel)
    if args.api_version_json:
        found["api"] = _read_api_json(args.api_version_json)
    if args.image_label:
        found["image"] = args.image_label.strip()
    if args.git_tag:
        tag_v = _git_tag_version()
        if tag_v is not None:
            found["tag"] = tag_v

    required = {s.strip() for s in args.require.split(",") if s.strip()}
    missing = required - set(found)
    if missing:
        raise SystemExit(f"required version sources missing: {sorted(missing)}")
    if not found:
        raise SystemExit("no version sources provided")

    canonical = {source: _canonical_version(value) for source, value in found.items()}
    if args.release:
        non_release = {source: value for source, value in canonical.items()
                       if not _CLEAN_RELEASE.fullmatch(value)}
        if non_release:
            lines = "\n".join(f"  {k}={v!r}" for k, v in sorted(non_release.items()))
            raise SystemExit(f"release version identity must be an exact clean version:\n{lines}")

    values = set(canonical.values())
    if len(values) != 1:
        lines = "\n".join(f"  {k}={v!r}" for k, v in sorted(canonical.items()))
        raise SystemExit(f"version identity mismatch:\n{lines}")

    version = next(iter(values))
    if args.print_version:
        print(version)
        return 0
    print(json.dumps({"ok": True, "version": version, "sources": canonical}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
