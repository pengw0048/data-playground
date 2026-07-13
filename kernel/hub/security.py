"""Request authentication — the `current_user` dependency, in its own leaf module.

Kept separate from main.py so any router module can depend on it without importing the app (which
would create a router→main→router cycle), and so the auth boundary lives in one obvious place. The
whole /api router is gated by this dependency at include time (main.py), making auth the secure
default: a new route is protected unless it's explicitly added to the pre-login `public` router.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException

from hub import auth, metadb


@dataclass(frozen=True)
class RequestIdentity:
    """A request identity established by the auth gate, including its admitted session epoch."""

    user_id: str
    session_epoch: int | None


def current_identity(x_dp_user: str | None = Header(default=None),
                     dp_session: str | None = Cookie(default=None)) -> RequestIdentity:
    """Resolve one trusted identity for reuse by every dependency in this request."""
    if auth.auth_enabled():
        claims = auth.verify_claims(dp_session)
        # Retain the exact signed principal instead of passing it through the open-mode resolver, whose
        # unknown-user fallback is intentionally the local account. The second epoch read narrows the
        # deletion/revocation window without ever changing the admitted identity.
        if (claims is None
                or metadb.user_token_epoch(claims.user_id) != claims.epoch):
            raise HTTPException(401, "authentication required")
        return RequestIdentity(user_id=claims.user_id, session_epoch=claims.epoch)
    return RequestIdentity(user_id=metadb.resolve_user(x_dp_user), session_epoch=None)


def current_user(identity: RequestIdentity = Depends(current_identity)) -> str:
    """Return the already-resolved user id expected by ordinary route dependencies."""
    return identity.user_id
