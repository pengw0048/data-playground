#!/usr/bin/env python3
"""Enforce one version identity across packaging surfaces (REL-01 / issue #114).

Compares sources that are present; missing optional sources are skipped unless
``--require`` lists them. Fail when any present value disagrees.

Examples::

    python3 scripts/check_release_versions.py \\
      --pyproject kernel/pyproject.toml \\
      --package-json web/package.json \\
      --wheel dist/*.whl \\
      --api-version-json /tmp/version.json \\
      --image-label 0.2.0 \\
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

    values = set(found.values())
    if len(values) != 1:
        lines = "\n".join(f"  {k}={v!r}" for k, v in sorted(found.items()))
        raise SystemExit(f"version identity mismatch:\n{lines}")

    print(json.dumps({"ok": True, "version": next(iter(values)), "sources": found}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
