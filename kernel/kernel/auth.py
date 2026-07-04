"""Session auth — a signed, non-spoofable identity, opt-in via env.

DEFAULT (no DP_AUTH_SECRET): open, internal-tool mode — identity is the X-DP-User header (dev). Set
DP_AUTH_SECRET (+ DP_AUTH_PASSWORD) to REQUIRE a signed session cookie: /auth/login checks the shared
password and issues an HMAC-signed, time-limited token; a raw header is no longer trusted, and tokens
can't be forged without the secret.

CAVEAT (by design, documented): this is a SHARED-password gate — /auth/login lets any holder of the
one password claim any user id, so it authenticates "someone with the instance password", not a
specific person. Password holders can therefore act as each other; the owner/editor/viewer model
isolates outsiders, not co-holders. Per-user credentials / SSO (a real authentication factor bound to
the identity) are the production upgrade and plug into /auth/login — the signing + session plumbing
stays the same.
"""

from __future__ import annotations

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


def check_password(pw: str) -> bool:
    """The shared login gate (DP_AUTH_PASSWORD). A real per-user credential/SSO replaces this."""
    expected = os.environ.get("DP_AUTH_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(pw or "", expected)
