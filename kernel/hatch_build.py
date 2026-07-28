"""Carry the built SPA through source distributions and into wheels.

A normal ``uv build`` creates an sdist, then builds the wheel from that archive. The source
checkout's ``../web/dist`` is outside the Python project root, so it must be explicitly copied
into the sdist before the wheel can map it to ``hub/_web``. Direct wheel builds use the external
directory without staging a second copy.

Backend-only development remains supported: when ``../web/dist`` is absent, the hook creates the
same placeholder page used historically. Release checks reject wheels containing that placeholder.
"""
from __future__ import annotations

import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


_SDIST_WEB_PATH = "_web_dist"
_WHEEL_WEB_PATH = "hub/_web"
_PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><title>Data Playground</title>"
    "<p>The web UI was not built. Run <code>make build</code> "
    "(<code>npm run build</code> in web/).</p>\n"
)


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        external_dist = Path(self.root).parent / "web" / "dist"
        staged_dist = Path(self.root) / _SDIST_WEB_PATH

        if self.target_name == "sdist":
            dist = external_dist
            self._ensure_web_dist(dist)
            build_data["force_include"][os.fspath(dist)] = _SDIST_WEB_PATH
            return

        # PKG-INFO is generated in the sdist root. Use that explicit signal so a stale local
        # _web_dist directory can never override a direct build's current ../web/dist.
        from_sdist = (Path(self.root) / "PKG-INFO").is_file()
        dist = staged_dist if from_sdist else external_dist
        self._ensure_web_dist(dist)
        build_data["force_include"][os.fspath(dist)] = _WHEEL_WEB_PATH
        # Editable wheel builders use a separate force-include map.
        build_data["force_include_editable"][os.fspath(dist)] = _WHEEL_WEB_PATH

    @staticmethod
    def _ensure_web_dist(dist: Path) -> None:
        dist.mkdir(parents=True, exist_ok=True)
        if not any(dist.iterdir()):
            (dist / "index.html").write_text(_PLACEHOLDER, encoding="utf-8")
