"""Destinations — a pluggable "places" list for save/open dialogs (like a file dialog's sidebar).

A destination is a named place data is read from / written to: a local directory tree, or an
object-store prefix (s3://…, gs://…). Core ships the `local` backend; object stores are a PLUGIN
backend — core keeps them as a known kind (so a target uri can be picked and saved) but browsing
and writing without the plugin's adapter fail honestly rather than silently going local.

A backend implements DestinationBackend and is registered via register_backend(); an org plugin
adds real s3/gcs/catalog browsing the same way. Presets (named roots) live in global settings.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable
from urllib.parse import unquote

from hub import metadb


@runtime_checkable
class DestinationBackend(Protocol):
    kind: str
    def browse(self, root: str, path: str, cred_id: str | None = None) -> dict: ...  # {path, entries:[{name,kind,uri}], error?}
    def target_uri(self, root: str, path: str, filename: str) -> str: ...


class LocalBackend:
    kind = "local"

    def browse(self, root: str, path: str, cred_id: str | None = None) -> dict:
        top = os.path.realpath(root)
        base = os.path.realpath(os.path.join(top, path.lstrip("/")))
        if not (base == top or base.startswith(top + os.sep)):  # never escape the destination root
            base, path = top, ""
        try:
            names = sorted(os.listdir(base))
        except OSError as e:
            return {"path": path, "entries": [], "error": str(e)}
        entries = []
        for fn in names:
            if fn.startswith("."):
                continue
            p = os.path.join(base, fn)
            # a `.lance` dir is a dataset (a "file"), not a folder to descend into
            is_dir = os.path.isdir(p) and not fn.endswith(".lance")
            entries.append({"name": fn, "kind": "dir" if is_dir else "file", "uri": p})
        return {"path": path, "entries": entries}

    def target_uri(self, root: str, path: str, filename: str) -> str:
        return os.path.join(self._safe(root, path), os.path.basename(filename))  # basename: no traversal via the name

    def _safe(self, root: str, path: str) -> str:
        top = os.path.realpath(root)
        base = os.path.realpath(os.path.join(top, path.lstrip("/")))
        return base if (base == top or base.startswith(top + os.sep)) else top  # never escape the root

    def mkdir(self, root: str, path: str, name: str) -> None:
        os.makedirs(os.path.join(self._safe(root, path), os.path.basename(name)), exist_ok=True)


class ObjectStoreBackend:
    """s3:// / gs:// — real, via DuckDB httpfs. Lists objects at a prefix with glob() and writes via
    the adapter. (Object stores have no true folders, so browse shows the objects at the prefix; you
    can also type a sub-prefix.)"""

    def __init__(self, kind: str):
        self.kind = kind

    def browse(self, root: str, path: str, cred_id: str | None = None) -> dict:
        from hub import db, metadb
        from hub.secrets import resolve_object_store
        try:
            prefix = self._safe_prefix(root, path)
            # Resolve the destination's credential per request and publish it right before the glob,
            # both under the base lock so the just-published secret is the one this listing uses.
            cfg = resolve_object_store(metadb.cred_object_store_config(cred_id))
            with db.lock():
                db.ensure_object_store(cfg)
                rows = db.conn().execute("SELECT file FROM glob(?)", [f"{prefix}/*"]).fetchall()
        except Exception as e:  # noqa: BLE001 — no creds / bad bucket → say so honestly
            return {"path": path, "entries": [], "error": str(e)}
        entries = []
        for (f,) in rows:
            if not isinstance(f, str) or not f.startswith(prefix + "/"):
                continue
            name = f.rstrip("/").rsplit("/", 1)[-1]
            entries.append({"name": name, "kind": "dir" if f.endswith("/") else "file", "uri": f})
        return {"path": path, "entries": entries}

    def _safe_prefix(self, root: str, path: str) -> str:
        base = root.rstrip("/")
        scheme = f"{self.kind}://"
        if not base.startswith(scheme) or not base[len(scheme):].split("/", 1)[0]:
            raise ValueError(f"invalid {self.kind} destination root")

        relative = path.strip("/")
        decoded = relative
        # Decode enough layers to catch encoded traversal without letting a deliberately deep value
        # turn validation into an unbounded CPU loop. More layers are not a useful browse prefix.
        for _ in range(4):
            unquoted = unquote(decoded)
            if unquoted == decoded:
                break
            decoded = unquoted
        else:
            if unquote(decoded) != decoded:
                raise ValueError("destination path is excessively encoded")
        parts = decoded.strip("/").split("/") if decoded else []
        if "://" in decoded or "\\" in decoded or any(part in (".", "..") for part in parts):
            raise ValueError("destination path must stay within its configured root")
        if any(char in root or char in decoded for char in "*?[]"):
            raise ValueError("destination path cannot contain glob characters")
        return f"{base}/{relative}" if relative else base

    def target_uri(self, root: str, path: str, filename: str) -> str:
        base = (root.rstrip("/") + "/" + path.strip("/")).rstrip("/")
        return f"{base}/{filename}"


_BACKENDS: dict[str, DestinationBackend] = {
    "local": LocalBackend(), "s3": ObjectStoreBackend("s3"), "gs": ObjectStoreBackend("gs"),
}


def register_backend(b: DestinationBackend) -> None:
    """Plugin extension point — add real s3/gcs/catalog browsing behind the same interface."""
    _BACKENDS[b.kind] = b


def backend_kinds() -> list[str]:
    return list(_BACKENDS)


def _default_root(workspace: str) -> str:
    url = os.environ.get("DP_STORAGE_URL", "").strip()
    if url.startswith(("s3://", "gs://")):
        return url
    return (url[len("file://"):] if url.startswith("file://") else url) or os.path.join(workspace, "outputs")


def presets(workspace: str) -> list[dict]:
    """User-configured destinations (global setting `destinations`), always including the default
    local outputs place so there's somewhere to write out of the box."""
    saved = metadb.get_setting("destinations", "global", default=[]) or []
    saved = [d for d in saved if isinstance(d, dict) and d.get("id")]
    if not any(d.get("id") == "outputs" for d in saved):
        root = _default_root(workspace)
        kind = "s3" if root.startswith("s3://") else "gs" if root.startswith("gs://") else "local"
        saved = [{"id": "outputs", "name": "Workspace outputs", "backend": kind, "root": root}, *saved]
    return saved


def _find(workspace: str, dest_id: str) -> dict | None:
    return next((d for d in presets(workspace) if d.get("id") == dest_id), None)


def get_destination(workspace: str, dest_id: str) -> dict | None:
    """Return a detached destination record for trusted execution-contract construction."""
    found = _find(workspace, dest_id)
    return dict(found) if found is not None else None


def browse(workspace: str, dest_id: str, path: str) -> dict:
    d = _find(workspace, dest_id)
    if not d:
        return {"path": path, "entries": [], "error": "unknown destination"}
    b = _BACKENDS.get(d.get("backend", "local"))
    if not b:
        return {"path": path, "entries": [], "error": f"no backend for '{d.get('backend')}'"}
    res = b.browse(d.get("root", ""), path or "", d.get("credId"))
    res["writable"] = True  # both local and object-store backends can write
    return res


def object_store_cred_cfg(workspace: str, dest_id: str | None) -> dict | None:
    """Resolved object-store credentials for an object-store destination's cred (falls back to the
    default cred / legacy setting), or None when the destination is not an object store. The write
    path calls this to bind the destination's credentials before the object-store open."""
    if not dest_id:
        return None
    d = _find(workspace, dest_id)
    if not d or d.get("backend") not in ("s3", "gs"):
        return None
    from hub import metadb
    from hub.secrets import resolve_object_store
    return resolve_object_store(metadb.cred_object_store_config(d.get("credId")))


def target_uri(workspace: str, dest_id: str, path: str, filename: str) -> str:
    d = _find(workspace, dest_id)
    if not d:
        raise ValueError(f"unknown destination '{dest_id}'")
    return _BACKENDS[d.get("backend", "local")].target_uri(d.get("root", ""), path or "", filename)


def mkdir(workspace: str, dest_id: str, path: str, name: str) -> dict:
    d = _find(workspace, dest_id)
    if not d:
        return {"error": "unknown destination"}
    b = _BACKENDS.get(d.get("backend", "local"))
    if b is None or not hasattr(b, "mkdir"):
        return {"ok": True}  # object stores have no real folders — the prefix is created on write
    try:
        b.mkdir(d.get("root", ""), path or "", name)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
