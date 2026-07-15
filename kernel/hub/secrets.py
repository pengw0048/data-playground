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
import re
from collections.abc import Callable
from typing import Any

# Object-store Cred fields that hold credentials (sessionToken included: consumed by handoff).
OBJECT_STORE_SECRET_SUBKEYS = ("accessKeyId", "secretAccessKey", "sessionToken")

Resolver = Callable[[str], str]

_resolvers: dict[str, Resolver] = {}
_SECRET_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


class SecretResolveError(RuntimeError):
    """Raised when a secret reference cannot be resolved (missing env, unreadable file, unknown scheme)."""


def _canonical_secret_scheme(scheme: Any) -> str | None:
    """Return one case-insensitive RFC URI scheme, or None when the name is malformed."""
    if not isinstance(scheme, str) or _SECRET_SCHEME_RE.fullmatch(scheme) is None:
        return None
    return scheme.lower()


def _split_secret_ref(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    scheme, separator, rest = value.partition(":")
    canonical = _canonical_secret_scheme(scheme)
    if not separator or not rest or canonical is None:
        return None
    return canonical, rest


def is_secret_ref(value: Any) -> bool:
    """True when ``value`` looks like a ``scheme:rest`` secret reference string."""
    return _split_secret_ref(value) is not None


def parse_secret_ref(value: str) -> tuple[str, str]:
    """Split ``scheme:rest``; raises ``SecretResolveError`` if the shape is wrong."""
    parsed = _split_secret_ref(value)
    if parsed is None:
        raise SecretResolveError(
            f"not a secret reference (expected env:VAR or file:/path, got {value!r})")
    return parsed


def register_resolver(scheme: str, resolver: Resolver) -> None:
    """Register a resolver for ``scheme:…`` references.

    Built-in schemes are ``env`` and ``file``. Plugins call this (or
    ``Registry.add_secret_resolver``) during ``register(reg)`` to add private or third-party backends
    without changing core.
    """
    key = _canonical_secret_scheme(scheme)
    if key is None:
        raise ValueError(f"invalid secret-reference scheme: {scheme!r}")
    if key in _resolvers and _resolvers[key] is not resolver:
        raise ValueError(f"secret-reference scheme {key!r} is already registered")
    _resolvers[key] = resolver


def unregister_resolver(scheme: str) -> None:
    """Remove a previously registered resolver (tests / plugin unload). Built-ins can be restored
    with ``ensure_builtin_resolvers()``."""
    key = _canonical_secret_scheme(scheme)
    if key is None:
        raise ValueError(f"invalid secret-reference scheme: {scheme!r}")
    _resolvers.pop(key, None)


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

    ``allow_plaintext`` keeps literal values usable for the ephemeral workload environment bridge.
    Cred fields and plugin secret settings validate references before persistence; callers that need
    a hard fail at resolution time pass ``allow_plaintext=False``.
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
    """Return a copy of object-store Cred fields with secret references resolved."""
    if not cfg:
        return {}
    out = dict(cfg)
    for key in OBJECT_STORE_SECRET_SUBKEYS:
        if key in out and out[key] not in (None, ""):
            out[key] = resolve_secret_value(out[key])
    return out


def is_registered_secret_ref(value: Any) -> bool:
    """A well-formed reference whose scheme has a registered resolver (built-in ``env``/``file`` or a
    plugin's). Shape alone is not enough: a raw ``user:token`` credential is ``scheme:rest``-shaped but
    would only fail later at resolve time, so the write guard must check the scheme is real."""
    if not is_secret_ref(value):
        return False
    scheme, _ = parse_secret_ref(str(value))
    return scheme in list_schemes()


# Echoed in place of a malformed plaintext value so an API response never leaks it.
_REDACTED_DISPLAY = "__redacted__"


def redact_secret_for_display(value: Any) -> Any:
    """Reference strings are safe to echo; a residual plaintext value is masked."""
    if value in (None, ""):
        return value
    if is_secret_ref(value):
        return value
    return _REDACTED_DISPLAY


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


def validate_secret_reference(value: Any, *, field: str) -> str:
    """Validate one Cred field or plugin secret setting before persistence.

    Empty values clear the field. Non-empty values must use a registered SecretResolver scheme;
    resolved credential bytes are never accepted by supported persistence APIs.
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not is_registered_secret_ref(value):
        raise ValueError(
            f"{field} must be a secret reference (env:VAR or file:/path), not a raw credential")
    return value


# Install builtins at import so the first resolve works without an explicit ensure call.
ensure_builtin_resolvers()
