"""Session auth — a signed, non-spoofable identity, opt-in via env.

DEFAULT (no DP_AUTH_SECRET): open, internal-tool mode — identity is the X-DP-User header (dev). Set
DP_AUTH_SECRET to require a signed session cookie; DP_AUTH_PASSWORD may seed the first admin during the
one-shot ``dataplay migrate`` release step (or serialized local-SQLite initialization).
/auth/login checks the user's stored password hash and issues an HMAC-signed, time-limited token; a raw
header is no longer trusted, and tokens can't be forged without the secret.

Identity is PER-USER: /auth/login verifies the submitted password against that user's own scrypt hash
(users.password_hash), so knowing the shared instance password no longer lets you sign in as someone
else. DP_AUTH_PASSWORD is one-time BOOTSTRAP input: migration consumes it after seeding the default
user's hash (or confirming a hash already exists). Application replicas must not receive it. Admins then
create users with their own passwords and everyone can rotate their own. SSO/OIDC would slot into the
same /auth/login + session plumbing later.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass

_TTL_SECONDS = 7 * 24 * 3600  # sessions expire after a week


def _secret() -> str:
    # Whitespace is never meaningful signing material. A configured-but-blank value is rejected by
    # metadata startup/migration; canonicalizing here keeps every auth decision on the same semantics.
    return os.environ.get("DP_AUTH_SECRET", "").strip()


def _signing_secret() -> str | None:
    """The configured session-signing secret, or None when it is absent/blank.

    ``DP_AUTH_MODE`` is deliberately not considered here. It is a confinement marker inherited by
    workload children after the hub strips its signing secret; treating that marker as key material
    would turn an empty, publicly-known HMAC key into a valid authenticator.
    """
    secret = _secret()
    return secret if secret.strip() else None


def auth_enabled() -> bool:
    # DP_AUTH_MODE is an internal marker the kernel spawner sets so a kernel CHILD knows it is in
    # auth/production mode (→ turns on the DuckDB FS sandbox + local-path confinement) WITHOUT carrying
    # the forgeable signing secret's value. It is NEVER used as crypto material (see _secret/sign/verify).
    return _signing_secret() is not None or os.environ.get("DP_AUTH_MODE") == "1"


# Known-weak defaults that must never guard real sessions — the secret is public (repo/docs), so a
# token signed with it is forgeable. reject_weak_secret() is called once at startup (main.py).
_WEAK_SECRETS = {"change-me-in-production", "changeme", "secret", "dev", "test"}


def reject_weak_secret() -> None:
    secret = _signing_secret()
    if secret is None and os.environ.get("DP_AUTH_MODE") == "1":
        raise RuntimeError(
            "authentication was explicitly configured but cannot authenticate hub sessions without "
            "a non-empty DP_AUTH_SECRET. Set a real random secret on the hub; workload children keep "
            "DP_AUTH_MODE but intentionally do not receive the signing secret.")
    if secret is not None and secret.strip() in _WEAK_SECRETS:
        raise RuntimeError(
            "DP_AUTH_SECRET is a known-weak/default value — sessions signed with it are forgeable. "
            "Set a real random secret, e.g. `openssl rand -hex 32`.")


def _mac(payload: str) -> str:
    secret = _signing_secret()
    if secret is None:
        raise RuntimeError("cannot sign a session without a non-empty DP_AUTH_SECRET")
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign(user_id: str, now: int | None = None) -> str:
    """A time-limited session token: '<user_id>.<epoch>.<expiry>.<hmac>' — unforgeable without
    DP_AUTH_SECRET. `epoch` is the user's session epoch at sign time; verify rejects the token once the
    stored epoch moves past it (password change / disable / delete), so revocation is immediate rather
    than waiting out the TTL."""
    if _signing_secret() is None:
        raise RuntimeError("cannot sign a session without a non-empty DP_AUTH_SECRET")
    from hub import metadb  # function-local: metadb imports auth (avoid an import cycle)
    epoch = metadb.user_token_epoch(user_id) or 0
    return sign_at_epoch(user_id, epoch, now)


def sign_at_epoch(user_id: str, epoch: int, now: int | None = None) -> str:
    """Sign a session at an epoch already established by an atomic database operation.

    Unlike :func:`sign`, this deliberately does not re-read the user's current epoch. Password
    rotation uses it with the epoch returned by its compare-and-set: a concurrent later revocation
    must invalidate that rotation instead of letting the response "catch up" to the newer epoch.
    """
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("session epoch must be a non-negative integer")
    exp = (now if now is not None else int(time.time())) + _TTL_SECONDS
    payload = f"{user_id}.{epoch}.{exp}"
    return f"{payload}.{_mac(payload)}"


@dataclass(frozen=True)
class SessionClaims:
    """Identity and epoch extracted only after a session token passes every verification step."""

    user_id: str
    epoch: int


def verify_claims(token: str | None) -> SessionClaims | None:
    """Return trusted claims iff signature, expiry, user existence, and current epoch all verify."""
    # Fail closed before computing a MAC. HMAC with b"" is well-defined and therefore forgeable by
    # anyone; DP_AUTH_MODE is only a child-workload confinement signal, never a substitute key.
    if _signing_secret() is None or not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    user_id, epoch, exp, mac = parts
    if not user_id or not hmac.compare_digest(mac, _mac(f"{user_id}.{epoch}.{exp}")):
        return None
    try:
        epoch_value = int(epoch)
        if epoch_value < 0 or int(exp) < int(time.time()):
            return None  # expired
    except ValueError:
        return None
    from hub import metadb  # function-local (import cycle)
    current = metadb.user_token_epoch(user_id)
    if current is None:  # user deleted / unknown → revoked
        return None
    if epoch_value != current:
        return None  # a newer epoch was issued (password change / disable) → this token is revoked
    return SessionClaims(user_id=user_id, epoch=epoch_value)


def verify(token: str | None) -> str | None:
    """Return the user id iff the complete signed session claims verify, else ``None``."""
    claims = verify_claims(token)
    return claims.user_id if claims is not None else None


_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}  # ~16MB work factor — admitted before use
MAX_PASSWORD_BYTES = 1024


def password_bytes_for_kdf(password: str) -> bytes:
    """Return a bounded UTF-8 password representation before allocating scrypt work."""
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    # Every valid UTF-8 code point uses at least one byte. Reject an obviously oversized string before
    # allocating a second, encoded copy; short multibyte strings still get the exact byte check below.
    if len(password) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} UTF-8 bytes")
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("password must be valid UTF-8") from exc
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} UTF-8 bytes")
    return encoded


def hash_password(pw: str) -> str:
    """A salted scrypt hash, stored as 'scrypt$<salt_b64>$<hash_b64>' (stdlib only, no new dep)."""
    password = password_bytes_for_kdf(pw)
    salt = os.urandom(16)
    dk = hashlib.scrypt(password, salt=salt, **_SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str | None) -> bool:
    """Constant-time check of a password against a stored scrypt hash. False if unset/malformed."""
    try:
        password = password_bytes_for_kdf(pw)
        if not stored or not stored.startswith("scrypt$"):
            return False
        _, salt_b64, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        if len(salt) != 16 or len(expected) != _SCRYPT["dklen"]:
            return False
        if base64.b64encode(salt).decode() != salt_b64 or base64.b64encode(expected).decode() != hash_b64:
            return False
        dk = hashlib.scrypt(password, salt=salt, **_SCRYPT)
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001 — any parse/format error → not a valid credential
        return False


def bootstrap_password() -> str:
    """Optional DP_AUTH_PASSWORD — one-shot migration input for the default user's credential.

    It is not a login path and must not be present in a production service process.
    """
    return os.environ.get("DP_AUTH_PASSWORD", "")
