"""Kernel settings — zero-config defaults, env overrides."""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_KERNEL_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _canvas_pip_deps_default() -> bool:
    """Whether per-canvas pip installs are allowed by default. Explicit DP_CANVAS_PIP_DEPS wins;
    otherwise OFF in auth/production mode (DP_AUTH_SECRET or the kernel-child DP_AUTH_MODE marker),
    ON for the open local tool. Kept out of `auth` to avoid importing it into this foundational module."""
    v = os.environ.get("DP_CANVAS_PIP_DEPS", "").strip().lower()
    if v:
        return v not in ("0", "false", "no", "off")   # explicit operator choice always wins
    auth_on = bool(os.environ.get("DP_AUTH_SECRET", "").strip()) or os.environ.get("DP_AUTH_MODE") == "1"
    return not auth_on   # open local tool → on; auth/production → off (rely on a pre-baked image)


class Settings:
    # workspace holds canvases/, outputs/, plugins/, and the local catalog
    workspace: str = os.environ.get("DP_WORKSPACE", _KERNEL_ROOT)
    data_dir: str = os.environ.get("DP_DATA_DIR", os.path.join(_KERNEL_ROOT, "data"))
    # metadata DB (users, canvases, settings): dev = bundled SQLite file; prod = set DP_DATABASE_URL
    # to a Postgres URL (postgresql+psycopg://…). Only the connection string lives in config/env.
    database_url: str = os.environ.get("DP_DATABASE_URL") or (
        "sqlite:///" + os.path.join(os.environ.get("DP_WORKSPACE", _KERNEL_ROOT), "dataplay.db"))
    preview_k: int = int(os.environ.get("DP_PREVIEW_K", "50"))
    # base URL the running web app is served at — used only to build clickable canvas links for
    # out-of-process clients (the MCP server), so "open this canvas" points at a real browser URL.
    base_url: str = (os.environ.get("DP_BASE_URL") or "http://127.0.0.1:8471").rstrip("/")
    # cap on a single dataset upload (POST /catalog/upload). Streamed + enforced as bytes arrive, so a
    # too-large upload is rejected without buffering it. Default 2 GiB; raise for bigger local files.
    max_upload_bytes: int = int(os.environ.get("DP_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
    # caps every NON-upload request body (JSON API, /mcp, canvas save) + the WebSocket frame size.
    # 64 MiB is generous vs. any real canvas/graph; uploads stream + self-cap at max_upload_bytes (SEC-10).
    max_body_bytes: int = int(os.environ.get("DP_MAX_BODY_BYTES", str(64 * 1024**2)))
    plugin_modules: list[str] = [m.strip() for m in os.environ.get("DP_PLUGINS", "").split(",") if m.strip()]
    # LLM agent (optional, provider-agnostic via LiteLLM): pick any model with DP_AGENT_MODEL, e.g.
    # anthropic/claude-opus-4-8, openai/gpt-5, gemini/gemini-2.5-pro, openrouter/…, ollama/llama3.3.
    # The matching provider key is read from the env (ANTHROPIC_API_KEY / OPENAI_API_KEY / …).
    agent_model: str = os.environ.get("DP_AGENT_MODEL", "anthropic/claude-opus-4-8")
    agent_base_url: str | None = os.environ.get("DP_AGENT_BASE_URL") or None  # local/self-hosted endpoint
    agent_api_key: str | None = os.environ.get("DP_AGENT_API_KEY") or None    # optional explicit override
    agent_max_steps: int = int(os.environ.get("DP_AGENT_MAX_STEPS", "24"))
    # execution backend opt-in: "kernel" routes runs to a per-canvas, restart-surviving kernel process
    # (else the in-process / subprocess runner). Empty = default selection (see deps.pick_runner).
    execution: str = os.environ.get("DP_EXECUTION", "").strip()
    # how a per-canvas kernel is launched: "local" (a detached process, single-host), "pod" (a k8s Pod +
    # Service per canvas, cross-host), OR a dotted path to a plugin KernelSpawner class — e.g.
    # DP_KERNEL_SPAWNER=my_pkg.spawners:EcsSpawner — so a third substrate needs no core patch. Case is
    # preserved (a class path is case-sensitive); the local/pod keywords are matched case-insensitively.
    kernel_spawner: str = os.environ.get("DP_KERNEL_SPAWNER", "local").strip()
    # per-canvas pip deps (kernel installs a canvas's declared requirements). Arbitrary code + egress, so
    # per-canvas pip installs = arbitrary code + network egress. An explicit DP_CANVAS_PIP_DEPS always
    # wins; otherwise it defaults OFF once auth/production mode is on (use a pre-baked image), ON for the
    # open local tool. A locked-down deploy can still force it either way via the env var.
    canvas_pip_deps: bool = _canvas_pip_deps_default()


settings = Settings()


def import_dotted(spec: str):
    """Resolve a 'pkg.module:Attr' (or 'pkg.module.Attr') string to the object it names. Used to load a
    plugin backend class from an env setting (kernel spawner, storage) so a third implementation is a
    config value, not a core patch."""
    import importlib
    mod, sep, attr = spec.partition(":")
    if not sep:
        mod, _, attr = spec.rpartition(".")
    return getattr(importlib.import_module(mod), attr)
