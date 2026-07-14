#!/usr/bin/env python3
"""Fail if a built wheel's bundled SPA is the hatch_build.py placeholder page.

Usage::

    python3 scripts/check_wheel_has_spa.py dist/data_playground-*.whl
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

PLACEHOLDER_MARKER = "The web UI was not built"


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        # hatch force-include maps ../web/dist → hub/_web
        index_names = [n for n in zf.namelist() if n.endswith("_web/index.html")]
        if not index_names:
            raise SystemExit(f"{path}: wheel has no hub/_web/index.html (SPA missing)")
        html = zf.read(index_names[0]).decode("utf-8", errors="replace")
    if PLACEHOLDER_MARKER in html:
        raise SystemExit(
            f"{path}: bundled hub/_web/index.html is the hatch_build.py placeholder — "
            "build the real SPA (cd web && npm ci && npm run build) before uv build"
        )
    if "<html" not in html.lower() and "<!doctype" not in html.lower():
        raise SystemExit(f"{path}: hub/_web/index.html does not look like an HTML document")
    print(f"ok: {path.name} ships a real SPA ({len(html)} bytes in {index_names[0]})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("wheel", type=Path, nargs="+")
    args = p.parse_args(argv)
    for w in args.wheel:
        if not w.is_file():
            raise SystemExit(f"wheel not found: {w}")
        check_wheel(w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
