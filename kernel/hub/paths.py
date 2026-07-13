"""Local dataset-path confinement — multi-user safety.

When auth is ENABLED (a shared, multi-user deployment), a source node or a /catalog/register with a
LOCAL path must resolve under an allowed root — the workspace, the data dir, or a root listed in the
`DP_DATASET_ROOTS` env (os.pathsep-separated). Otherwise an authenticated user could read ARBITRARY
local files (e.g. /etc/passwd) just by pointing a source at them. In open single-user mode (no
DP_AUTH_SECRET) there is no confinement — it's a trusted local tool. Object-store / http(s) uris are
NOT local paths and are governed by db.py's SSRF policy (extension autoload disabled), not this.
"""

from __future__ import annotations

import ntpath
import os
from urllib.parse import urlsplit

from hub import auth
from hub.settings import settings


def allowed_roots() -> list[str]:
    roots = [settings.workspace, settings.data_dir]
    roots += (os.environ.get("DP_DATASET_ROOTS") or "").split(os.pathsep)
    return [os.path.realpath(os.path.expanduser(r)) for r in roots if r and r.strip()]


def local_path(uri: str) -> str | None:
    """Return one canonical local-path spelling, or ``None`` for a non-file URI.

    URI schemes are case-insensitive. File URIs deliberately accept only the authority-free
    ``file:///<path>`` form so the confinement check and every adapter resolve exactly the same path;
    query/fragment/remote-authority variants are rejected instead of being interpreted differently by
    separate libraries.
    """
    raw = str(uri or "")
    # ``urlsplit`` treats a Windows drive letter as a URI scheme. DuckDB still opens both slash
    # spellings as local files, so recognize them before generic scheme parsing or auth confinement
    # would incorrectly classify ``C:\\...`` / ``C:/...`` as remote.
    drive, _tail = ntpath.splitdrive(raw)
    if len(drive) == 2 and drive[1] == ":":
        # Windows accepts both slash spellings, but every managed-result comparison must use the
        # same spelling as ``os.path.realpath`` / ``LocalStorage.result_root``. Only normalize the
        # separator here; deliberately do not collapse ``..`` or change case.
        return raw.replace("/", "\\") if os.name == "nt" else raw
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("invalid dataset URI") from exc
    scheme = parsed.scheme.lower()
    if scheme == "file":
        if (not raw.lower().startswith("file://") or parsed.netloc
                or "?" in raw or "#" in raw):
            raise ValueError("file URI must use the canonical file:///<path> form")
        path = parsed.path
        # RFC file URIs spell a Windows drive as ``file:///C:/...``. urlsplit retains the leading
        # slash; Windows file APIs require the drive-root spelling without it.
        if os.name == "nt" and len(path) >= 4 and path[0] == "/" and path[2] == ":" \
                and path[1].isalpha() and path[3] in ("/", "\\"):
            path = path[1:]
        if os.name == "nt":
            path = path.replace("/", "\\")
        return path
    if scheme and raw[len(parsed.scheme):].startswith("://"):
        return None
    # POSIX filenames may contain a colon. DuckDB treats opaque spellings such as ``name:data.csv``
    # and ``x:/path.csv`` as local globs, so the policy layer must do the same; only ``scheme://`` is
    # a remote URI boundary.
    return raw


def canonical_data_uri(uri: str) -> str:
    """Return the exact local path or a remote URI with its case-insensitive scheme normalized."""
    raw = str(uri or "")
    path = local_path(raw)
    if path is not None:
        return path
    parsed = urlsplit(raw)
    return parsed.scheme.lower() + raw[len(parsed.scheme):]


def ensure_local_uri_allowed(uri: str) -> None:
    """Raise PermissionError if `uri` is a local path outside the allowed roots (auth mode only)."""
    if not auth.auth_enabled():
        return  # open single-user mode: trusted local tool, no confinement
    try:
        path = local_path(uri)
    except ValueError as exc:
        raise PermissionError(str(exc)) from None
    if path is None:
        return  # s3://, gs://, http(s):// … — not a local path (SSRF handled in db.ensure_object_store)
    rp = os.path.realpath(os.path.expanduser(path))
    if not any(rp == root or rp.startswith(root + os.sep) for root in allowed_roots()):
        raise PermissionError(
            f"dataset path '{uri}' is outside the allowed roots "
            f"(the workspace, the data dir, or DP_DATASET_ROOTS)"
        )
