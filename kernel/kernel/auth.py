"""Session auth — a signed, non-spoofable identity, opt-in via env.

DEFAULT (no DP_AUTH_SECRET): open, internal-tool mode — identity is the X-DP-User header (dev). Set
DP_AUTH_SECRET (+ DP_AUTH_PASSWORD) to REQUIRE a signed session cookie: /auth/login checks the shared
password and issues an HMAC-signed token; a raw header is no longer trusted, and tokens can't be
forged without the secret. This is a first, honest auth layer — per-user credentials / SSO plug into
/auth/login later (the signing + session plumbing stays the same).
"""

from __future__ import annotations

import hashlib
import hmac
import os


def _secret() -> str:
    return os.environ.get("DP_AUTH_SECRET", "")


def auth_enabled() -> bool:
    return bool(_secret())


def _mac(user_id: str) -> str:
    return hmac.new(_secret().encode(), user_id.encode(), hashlib.sha256).hexdigest()


def sign(user_id: str) -> str:
    """A session token: '<user_id>.<hmac>' — unforgeable without DP_AUTH_SECRET."""
    return f"{user_id}.{_mac(user_id)}"


def verify(token: str | None) -> str | None:
    """Return the user id iff the token's signature is valid, else None."""
    if not token or "." not in token:
        return None
    user_id, _, mac = token.rpartition(".")
    if not user_id:
        return None
    return user_id if hmac.compare_digest(mac, _mac(user_id)) else None


def check_password(pw: str) -> bool:
    """The shared login gate (DP_AUTH_PASSWORD). A real per-user credential/SSO replaces this."""
    expected = os.environ.get("DP_AUTH_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(pw or "", expected)
