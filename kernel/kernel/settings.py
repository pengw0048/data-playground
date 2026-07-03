"""Kernel settings — zero-config defaults, env overrides (PRD NFR-9)."""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_KERNEL_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


class Settings:
    # workspace holds canvases/, outputs/, plugins/, and the local catalog
    workspace: str = os.environ.get("DP_WORKSPACE", _KERNEL_ROOT)
    data_dir: str = os.environ.get("DP_DATA_DIR", os.path.join(_KERNEL_ROOT, "data"))
    preview_k: int = int(os.environ.get("DP_PREVIEW_K", "50"))
    plugin_modules: list[str] = [m.strip() for m in os.environ.get("DP_PLUGINS", "").split(",") if m.strip()]


settings = Settings()
