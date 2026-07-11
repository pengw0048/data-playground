"""Build hook: ensure ../web/dist exists so a backend-only / clean-clone build never aborts (OPS-07).

The wheel force-includes ../web/dist (the built SPA → hub/_web). A fresh clone, or a backend-only
`uv sync` / `uv run pytest`, hasn't run `npm run build`, so that path is absent and hatchling would
abort with FileNotFoundError. This hook creates the directory (with a placeholder page) before the
build, so the backend packages/tests independently of the frontend. `make build` (npm run build)
populates it with the real SPA for a shipping image.
"""
from __future__ import annotations

import os

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        dist = os.path.abspath(os.path.join(self.root, "..", "web", "dist"))
        os.makedirs(dist, exist_ok=True)
        if not os.listdir(dist):
            with open(os.path.join(dist, "index.html"), "w") as f:
                f.write("<!doctype html><meta charset=utf-8><title>Data Playground</title>"
                        "<p>The web UI was not built. Run <code>make build</code> "
                        "(<code>npm run build</code> in web/).</p>\n")
