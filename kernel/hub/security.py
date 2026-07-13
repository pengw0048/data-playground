"""Request authentication — the `current_user` dependency, in its own leaf module.

Kept separate from main.py so any router module can depend on it without importing the app (which
would create a router→main→router cycle), and so the auth boundary lives in one obvious place. The
whole /api router is gated by this dependency at include time (main.py), making auth the secure
default: a new route is protected unless it's explicitly added to the pre-login `public` router.
"""

from __future__ import annotations

from fastapi import Cookie, Header, HTTPException

from hub import auth, metadb


def current_user(x_dp_user: str | None = Header(default=None),
                 dp_session: str | None = Cookie(default=None)) -> str:
    """Resolve the request's user. With auth enabled (DP_AUTH_SECRET), identity comes ONLY from a
    valid signed session cookie (a raw header is not trusted); otherwise it's the X-DP-User header
    (open internal-tool mode), defaulting to the local user."""
    if auth.auth_enabled():
        uid = auth.verify(dp_session)
        # Authentication mode must never pass a verified identity through resolve_user(): that helper
        # intentionally falls back to the local admin for unknown X-DP-User values in open dev mode.
        # Re-check existence to close the verify->resolve deletion race, then retain the exact signed
        # principal. A concurrent deletion after this check can fail a downstream operation, but it can
        # never silently change the request into the local administrator.
        if not uid or metadb.user_token_epoch(uid) is None:
            raise HTTPException(401, "authentication required")
        return uid
    return metadb.resolve_user(x_dp_user)
