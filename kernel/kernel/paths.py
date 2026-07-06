"""Local dataset-path confinement — multi-user safety.

When auth is ENABLED (a shared, multi-user deployment), a source node or a /catalog/register with a
LOCAL path must resolve under an allowed root — the workspace, the data dir, or a root listed in the
`DP_DATASET_ROOTS` env (os.pathsep-separated). Otherwise an authenticated user could read ARBITRARY
local files (e.g. /etc/passwd) just by pointing a source at them. In open single-user mode (no
DP_AUTH_SECRET) there is no confinement — it's a trusted local tool. Object-store / http(s) uris are
NOT local paths and are governed by db.py's SSRF policy (extension autoload disabled), not this.
"""

from __future__ import annotations

import os
import re

from kernel import auth
from kernel.settings import settings

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def allowed_roots() -> list[str]:
    roots = [settings.workspace, settings.data_dir]
    roots += (os.environ.get("DP_DATASET_ROOTS") or "").split(os.pathsep)
    return [os.path.realpath(os.path.expanduser(r)) for r in roots if r and r.strip()]


def ensure_local_uri_allowed(uri: str) -> None:
    """Raise PermissionError if `uri` is a local path outside the allowed roots (auth mode only)."""
    if not auth.auth_enabled():
        return  # open single-user mode: trusted local tool, no confinement
    s = str(uri or "")
    if _SCHEME.match(s) and not s.startswith("file://"):
        return  # s3://, gs://, http(s):// … — not a local path (SSRF handled in db.ensure_object_store)
    path = s[len("file://"):] if s.startswith("file://") else s
    rp = os.path.realpath(os.path.expanduser(path))
    if not any(rp == root or rp.startswith(root + os.sep) for root in allowed_roots()):
        raise PermissionError(
            f"dataset path '{uri}' is outside the allowed roots "
            f"(the workspace, the data dir, or DP_DATASET_ROOTS)"
        )
