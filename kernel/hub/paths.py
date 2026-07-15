"""Local dataset-path confinement — multi-user safety.

When auth is ENABLED (a shared, multi-user deployment), a source node or a /catalog/register with a
LOCAL path must resolve under an allowed root — the workspace, the data dir, or a root listed in the
`DP_DATASET_ROOTS` env (os.pathsep-separated). Otherwise an authenticated user could read ARBITRARY
local files (e.g. /etc/passwd) just by pointing a source at them. In open single-user mode (no
DP_AUTH_SECRET) there is no confinement — it's a trusted local tool. Object-store / http(s) uris are
NOT local paths and are governed by db.py's SSRF policy (extension autoload disabled), not this.
"""

from __future__ import annotations

import glob
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


def checked_local_path(uri: str) -> str | None:
    """Return a canonical local path after enforcing the deployment's filesystem boundary.

    Adapter filesystem probes must consume this returned value, not re-use the caller's original URI.
    That keeps parsing, symlink resolution, the allowed-root decision, and the eventual filesystem
    operation on one spelling. In the trusted open/local product the candidate filesystem root is the
    declared boundary, preserving intentional arbitrary local-file access; that root is not a sandbox.
    """
    raw = str(uri or "")
    if not raw:
        return None
    try:
        path = local_path(raw)
    except ValueError as exc:
        if auth.auth_enabled():
            raise PermissionError(str(exc)) from None
        raise
    if path is None:
        return None
    if auth.auth_enabled() and glob.has_magic(path):
        # A prefix check on the literal pattern is not a check on the files DuckDB later expands. In
        # particular, an in-root wildcard can match a symlink whose canonical target is outside the root.
        # Shared mode therefore fails closed until an adapter can canonicalize every concrete match.
        raise PermissionError("local glob dataset paths are not allowed in shared mode")

    candidate = os.path.realpath(os.path.expanduser(path))
    if auth.auth_enabled():
        roots = allowed_roots()
    else:
        # Open mode is the trusted localhost tool. Still pass filesystem operations a normalized path
        # that has crossed an explicit containment check; the candidate's volume root intentionally
        # permits the whole local filesystem (including non-current drives on Windows).
        drive, _tail = os.path.splitdrive(candidate)
        roots = [os.path.realpath(drive + os.sep if drive else os.sep)]

    normalized_candidate = os.path.normcase(candidate)
    for root in roots:
        normalized_root = os.path.normcase(os.path.realpath(os.path.expanduser(root)))
        root_prefix = normalized_root.rstrip(os.sep) + os.sep
        if normalized_candidate == normalized_root or normalized_candidate.startswith(root_prefix):
            return candidate
    raise PermissionError(
        f"dataset path '{raw}' is outside the allowed roots "
        f"(the workspace, the data dir, or DP_DATASET_ROOTS)"
    )


def ensure_local_uri_allowed(uri: str) -> None:
    """Raise PermissionError if `uri` is a local path outside the allowed roots (auth mode only)."""
    if not auth.auth_enabled():
        return  # open single-user mode: trusted local tool, no confinement
    checked_local_path(uri)
