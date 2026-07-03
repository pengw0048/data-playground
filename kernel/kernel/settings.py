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
    # LLM agent (optional): activates when ANTHROPIC_API_KEY is set and `anthropic` is installed
    agent_model: str = os.environ.get("DP_AGENT_MODEL", "claude-opus-4-8")
    agent_max_steps: int = int(os.environ.get("DP_AGENT_MAX_STEPS", "24"))


settings = Settings()
