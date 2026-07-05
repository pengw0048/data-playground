"""Request authentication — the `current_user` dependency, in its own leaf module.

Kept separate from main.py so any router module can depend on it without importing the app (which
would create a router→main→router cycle), and so the auth boundary lives in one obvious place. The
whole /api router is gated by this dependency at include time (main.py), making auth the secure
default: a new route is protected unless it's explicitly added to the pre-login `public` router.
"""

from __future__ import annotations

from fastapi import Cookie, Header, HTTPException

from kernel import auth, metadb


def current_user(x_dp_user: str | None = Header(default=None),
                 dp_session: str | None = Cookie(default=None)) -> str:
    """Resolve the request's user. With auth enabled (DP_AUTH_SECRET), identity comes ONLY from a
    valid signed session cookie (a raw header is not trusted); otherwise it's the X-DP-User header
    (open internal-tool mode), defaulting to the local user."""
    if auth.auth_enabled():
        uid = auth.verify(dp_session)
        if not uid:
            raise HTTPException(401, "authentication required")
        return metadb.resolve_user(uid)
    return metadb.resolve_user(x_dp_user)
