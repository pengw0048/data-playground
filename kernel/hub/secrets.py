"""Secret references and a pluggable SecretResolver seam.

Settings that hold credentials store a *reference* string — never the secret itself:

  env:<VAR>     resolve from the process environment
  file:<path>   read the secret from a file (first line, stripped)

A custom scheme (e.g. ``vault:``, ``aws-sm:``) can be registered at plugin load time via
``register_resolver`` / ``Registry.add_secret_resolver`` without core importing any vendor client.

Resolution happens only in the process that needs the capability. Resolved values must not be written
back into settings, API responses, logs, telemetry, or job payloads.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

# Object-store setting subkeys that hold credentials (sessionToken included: consumed by handoff).
OBJECT_STORE_SECRET_SUBKEYS = ("accessKeyId", "secretAccessKey", "sessionToken")

# Top-level setting keys that are secret references (scalar string values).
SCALAR_SECRET_KEYS = frozenset({"agentApiKey"})

Resolver = Callable[[str], str]

_resolvers: dict[str, Resolver] = {}


class SecretResolveError(RuntimeError):
    """Raised when a secret reference cannot be resolved (missing env, unreadable file, unknown scheme)."""


def is_secret_ref(value: Any) -> bool:
    """True when ``value`` looks like a ``scheme:rest`` secret reference string."""
    if not isinstance(value, str) or ":" not in value:
        return False
    scheme, _, rest = value.partition(":")
    return bool(scheme) and bool(rest) and scheme.isidentifier()


def parse_secret_ref(value: str) -> tuple[str, str]:
    """Split ``scheme:rest``; raises ``SecretResolveError`` if the shape is wrong."""
    if not is_secret_ref(value):
        raise SecretResolveError(
            f"not a secret reference (expected env:VAR or file:/path, got {value!r})")
    scheme, _, rest = value.partition(":")
    return scheme.lower(), rest


def register_resolver(scheme: str, resolver: Resolver, *, replace: bool = False) -> None:
    """Register a resolver for ``scheme:…`` references.

    Built-in schemes are ``env`` and ``file``. Plugins call this (or
    ``Registry.add_secret_resolver``) during ``register(reg)`` to add private or third-party backends
    without changing core.
    """
    key = scheme.lower().strip()
    if not key or not key.isidentifier():
        raise ValueError(f"invalid secret-reference scheme: {scheme!r}")
    if key in _resolvers and not replace and _resolvers[key] is not resolver:
        raise ValueError(f"secret-reference scheme {key!r} is already registered")
    _resolvers[key] = resolver


def unregister_resolver(scheme: str) -> None:
    """Remove a previously registered resolver (tests / plugin unload). Built-ins can be restored
    with ``ensure_builtin_resolvers()``."""
    _resolvers.pop(scheme.lower().strip(), None)


def list_schemes() -> tuple[str, ...]:
    ensure_builtin_resolvers()
    return tuple(sorted(_resolvers))


def ensure_builtin_resolvers() -> None:
    """Install the OSS built-in ``env`` / ``file`` resolvers if missing."""
    if "env" not in _resolvers:
        _resolvers["env"] = _resolve_env
    if "file" not in _resolvers:
        _resolvers["file"] = _resolve_file


def _resolve_env(name: str) -> str:
    var = name.strip()
    if not var:
        raise SecretResolveError("env: reference is missing the variable name")
    value = os.environ.get(var)
    if value is None or value == "":
        raise SecretResolveError(
            f"secret reference env:{var} could not be resolved: environment variable "
            f"{var!r} is unset or empty")
    return value


def _resolve_file(path: str) -> str:
    loc = path.strip()
    if not loc:
        raise SecretResolveError("file: reference is missing the path")
    try:
        with open(loc, encoding="utf-8") as f:
            # First line, stripped — matches common secret-file / Docker-secret conventions.
            line = f.readline()
    except OSError as exc:
        raise SecretResolveError(
            f"secret reference file:{loc} could not be resolved: {exc}") from exc
    value = line.rstrip("\r\n")
    if value == "":
        raise SecretResolveError(
            f"secret reference file:{loc} could not be resolved: file is empty")
    return value


def resolve_secret(ref: str) -> str:
    """Resolve a secret reference to its material value.

    Raises ``SecretResolveError`` with an actionable message on failure. Never log the returned
    value.
    """
    ensure_builtin_resolvers()
    scheme, rest = parse_secret_ref(ref)
    resolver = _resolvers.get(scheme)
    if resolver is None:
        known = ", ".join(sorted(_resolvers)) or "(none)"
        raise SecretResolveError(
            f"unknown secret-reference scheme {scheme!r} in {ref!r}; "
            f"registered schemes: {known}")
    return resolver(rest)


def resolve_secret_value(value: Any, *, allow_plaintext: bool = True) -> Any:
    """Resolve ``value`` when it is a secret reference; otherwise return it unchanged.

    ``allow_plaintext`` keeps a literal string usable for in-process test fixtures that write
    through ``metadb.set_setting`` directly. The settings API and migration reject / strip
    plaintext for reference-backed keys; callers that need a hard fail pass ``allow_plaintext=False``.
    """
    if value in (None, ""):
        return value
    if is_secret_ref(value):
        return resolve_secret(str(value))
    if allow_plaintext:
        return value
    raise SecretResolveError(
        "expected a secret reference (env:VAR or file:/path), got a raw value")


def resolve_object_store(cfg: dict | None) -> dict:
    """Return a copy of an ``objectStore`` setting with secret subkeys resolved."""
    if not cfg:
        return {}
    out = dict(cfg)
    for key in OBJECT_STORE_SECRET_SUBKEYS:
        if key in out and out[key] not in (None, ""):
            out[key] = resolve_secret_value(out[key])
    return out


def is_plaintext_secret(value: Any) -> bool:
    """True when ``value`` is a non-empty string that is not a secret reference."""
    return isinstance(value, str) and value != "" and not is_secret_ref(value)


def is_registered_secret_ref(value: Any) -> bool:
    """A well-formed reference whose scheme has a registered resolver (built-in ``env``/``file`` or a
    plugin's). Shape alone is not enough: a raw ``user:token`` credential is ``scheme:rest``-shaped but
    would only fail later at resolve time, so the write guard must check the scheme is real."""
    if not is_secret_ref(value):
        return False
    scheme, _ = parse_secret_ref(str(value))
    return scheme in list_schemes()


# Echoed in place of a secret-backed setting that still holds legacy plaintext, so GET never leaks it.
_REDACTED_DISPLAY = "__redacted__"


def redact_secret_for_display(value: Any) -> Any:
    """Reference strings are safe to echo; a residual plaintext value is masked."""
    if value in (None, ""):
        return value
    if is_secret_ref(value):
        return value
    return _REDACTED_DISPLAY


def redact_global_setting(key: str, value: Any, *, plugin_secrets: set[str]) -> Any:
    """Mask residual plaintext in a reference-backed global setting before it leaves an API response."""
    if key in SCALAR_SECRET_KEYS or key in plugin_secrets:
        return redact_secret_for_display(value)
    if key == "objectStore" and isinstance(value, dict):
        return {k: (redact_secret_for_display(v) if k in OBJECT_STORE_SECRET_SUBKEYS else v)
                for k, v in value.items()}
    return value


def plugin_secret_setting_keys() -> set[str]:
    """Setting keys ``plugin.<pack>.<field>`` whose ``[[config]]`` declares ``secret = true``."""
    out: set[str] = set()
    try:
        from hub.deps import get_deps
        for p in get_deps().plugins:
            for f in (p.get("config") or []):
                if isinstance(f, dict) and f.get("secret") and f.get("key"):
                    out.add(f"plugin.{p['name']}.{f['key']}")
    except Exception:  # noqa: BLE001 — settings must not crash when plugins are mid-load
        pass
    return out


def is_reference_backed_key(key: str, *, plugin_secrets: set[str] | None = None) -> bool:
    """Whether ``key`` (or an objectStore secret subkey) must store a reference, not plaintext."""
    if key in SCALAR_SECRET_KEYS:
        return True
    if key == "objectStore":
        return True
    secrets = plugin_secrets if plugin_secrets is not None else plugin_secret_setting_keys()
    return key in secrets


def validate_secret_setting_value(key: str, value: Any, *,
                                  plugin_secrets: set[str] | None = None) -> Any:
    """Validate a PUT body for a reference-backed setting.

    Accepts empty / None (clear), a well-formed reference, or — for ``objectStore`` — a dict whose
    secret subkeys are each empty or a reference. Rejects raw secrets.
    """
    secrets = plugin_secrets if plugin_secrets is not None else plugin_secret_setting_keys()
    if key == "objectStore":
        if value in (None, "", {}):
            return value if value is not None else {}
        if not isinstance(value, dict):
            raise ValueError("objectStore must be an object")
        out = dict(value)
        for sub in OBJECT_STORE_SECRET_SUBKEYS:
            v = out.get(sub)
            if v in (None, ""):
                continue
            if not isinstance(v, str) or not is_registered_secret_ref(v):
                raise ValueError(
                    f"objectStore.{sub} must be a secret reference "
                    f"(env:VAR or file:/path), not a raw credential")
        return out
    if key in SCALAR_SECRET_KEYS or key in secrets:
        if value in (None, ""):
            return value if value is not None else ""
        if not isinstance(value, str) or not is_registered_secret_ref(value):
            raise ValueError(
                f"{key} must be a secret reference (env:VAR or file:/path), not a raw credential")
        return value
    return value


def scan_settings_rows_for_plaintext(rows: list[tuple[str, Any]]) -> list[str]:
    """Return human-readable descriptions of settings that still hold plaintext secrets.

    ``rows`` is a list of ``(key, decoded_value)`` for global settings. Used by the destructive
    migration and by tests.
    """
    secrets = plugin_secret_setting_keys()
    problems: list[str] = []
    for key, value in rows:
        if key in SCALAR_SECRET_KEYS or key in secrets:
            if is_plaintext_secret(value):
                problems.append(key)
        elif key == "objectStore" and isinstance(value, dict):
            for sub in OBJECT_STORE_SECRET_SUBKEYS:
                if is_plaintext_secret(value.get(sub)):
                    problems.append(f"objectStore.{sub}")
    return problems


# Install builtins at import so the first resolve works without an explicit ensure call.
ensure_builtin_resolvers()
