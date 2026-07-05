"""Session auth — a signed, non-spoofable identity, opt-in via env.

DEFAULT (no DP_AUTH_SECRET): open, internal-tool mode — identity is the X-DP-User header (dev). Set
DP_AUTH_SECRET (+ DP_AUTH_PASSWORD) to REQUIRE a signed session cookie: /auth/login checks the shared
password and issues an HMAC-signed, time-limited token; a raw header is no longer trusted, and tokens
can't be forged without the secret.

Identity is PER-USER: /auth/login verifies the submitted password against that user's own scrypt hash
(users.password_hash), so knowing the shared instance password no longer lets you sign in as someone
else. DP_AUTH_PASSWORD survives only as a BOOTSTRAP: on first init it seeds the default user's hash so
an existing deployment keeps working; admins then create users with their own passwords and everyone
can rotate their own. SSO/OIDC would slot into the same /auth/login + session plumbing later.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

_TTL_SECONDS = 7 * 24 * 3600  # sessions expire after a week


def _secret() -> str:
    return os.environ.get("DP_AUTH_SECRET", "")


def auth_enabled() -> bool:
    return bool(_secret())


def _mac(payload: str) -> str:
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign(user_id: str, now: int | None = None) -> str:
    """A time-limited session token: '<user_id>.<expiry>.<hmac>' — unforgeable without DP_AUTH_SECRET."""
    exp = (now if now is not None else int(time.time())) + _TTL_SECONDS
    payload = f"{user_id}.{exp}"
    return f"{payload}.{_mac(payload)}"


def verify(token: str | None) -> str | None:
    """Return the user id iff the token's signature is valid AND not expired, else None."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user_id, exp, mac = parts
    if not user_id or not hmac.compare_digest(mac, _mac(f"{user_id}.{exp}")):
        return None
    try:
        if int(exp) < int(time.time()):
            return None  # expired
    except ValueError:
        return None
    return user_id


_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}  # ~16MB work factor — fine for interactive login


def hash_password(pw: str) -> str:
    """A salted scrypt hash, stored as 'scrypt$<salt_b64>$<hash_b64>' (stdlib only, no new dep)."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str | None) -> bool:
    """Constant-time check of a password against a stored scrypt hash. False if unset/malformed."""
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, hash_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"], dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001 — any parse/format error → not a valid credential
        return False


def bootstrap_password() -> str:
    """Optional DP_AUTH_PASSWORD — seeds the default user's credential on first init so an existing
    shared-password deployment keeps working after upgrade. Not a login path on its own."""
    return os.environ.get("DP_AUTH_PASSWORD", "")
