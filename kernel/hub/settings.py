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
    # cap on a single dataset upload (POST /catalog/upload). Streamed + enforced as bytes arrive, so a
    # too-large upload is rejected without buffering it. Default 2 GiB; raise for bigger local files.
    max_upload_bytes: int = int(os.environ.get("DP_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
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
    # a locked-down deployment can turn it OFF here: the kernel then installs nothing and allows no extra
    # imports (canvases must rely on a pre-baked image instead). Default on (trusted/local tool).
    canvas_pip_deps: bool = os.environ.get("DP_CANVAS_PIP_DEPS", "1").strip().lower() not in ("0", "false", "no", "off")


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
