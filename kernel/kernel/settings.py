"""Kernel settings — zero-config defaults, env overrides."""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_KERNEL_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


class Settings:
    # workspace holds canvases/, outputs/, plugins/, and the local catalog
    workspace: str = os.environ.get("DP_WORKSPACE", _KERNEL_ROOT)
    data_dir: str = os.environ.get("DP_DATA_DIR", os.path.join(_KERNEL_ROOT, "data"))
    # metadata DB (users, canvases, settings): dev = bundled SQLite file; prod = set DP_DATABASE_URL
    # to a Postgres URL (postgresql+psycopg://…). Only the connection string lives in config/env.
    database_url: str = os.environ.get("DP_DATABASE_URL") or (
        "sqlite:///" + os.path.join(os.environ.get("DP_WORKSPACE", _KERNEL_ROOT), "dataplay.db"))
    preview_k: int = int(os.environ.get("DP_PREVIEW_K", "50"))
    plugin_modules: list[str] = [m.strip() for m in os.environ.get("DP_PLUGINS", "").split(",") if m.strip()]
    # LLM agent (optional, provider-agnostic via LiteLLM): pick any model with DP_AGENT_MODEL, e.g.
    # anthropic/claude-opus-4-8, openai/gpt-4o, gemini/gemini-1.5-pro, openrouter/…, ollama/llama3.
    # The matching provider key is read from the env (ANTHROPIC_API_KEY / OPENAI_API_KEY / …).
    agent_model: str = os.environ.get("DP_AGENT_MODEL", "anthropic/claude-opus-4-8")
    agent_base_url: str | None = os.environ.get("DP_AGENT_BASE_URL") or None  # local/self-hosted endpoint
    agent_api_key: str | None = os.environ.get("DP_AGENT_API_KEY") or None    # optional explicit override
    agent_max_steps: int = int(os.environ.get("DP_AGENT_MAX_STEPS", "24"))


settings = Settings()
