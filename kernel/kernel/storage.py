"""Where run outputs are persisted — pluggable so the default local directory can later be swapped
for object storage (S3/GCS) with no change to the engine or write nodes: a write node/DuckDB just
writes to the uri that Storage hands back (a local path today; an ``s3://…`` uri once an adapter
lands). Selected by DP_STORAGE_URL; only local is implemented today.
"""

from __future__ import annotations

import os
from typing import Protocol

_EXTS = (".parquet", ".csv", ".tsv", ".json", ".arrow", ".feather", ".ipc")


class Storage(Protocol):
    def output_uri(self, name: str, ext: str) -> str: ...
    def list_outputs(self) -> list[str]: ...


class LocalStorage:
    """Outputs live as files under ``root`` (default ``<workspace>/outputs``)."""

    def __init__(self, root: str):
        self.root = root

    def output_uri(self, name: str, ext: str) -> str:
        os.makedirs(self.root, exist_ok=True)
        return os.path.join(self.root, f"{name}{ext}")

    def list_outputs(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        out: list[str] = []
        for fn in sorted(os.listdir(self.root)):
            p = os.path.join(self.root, fn)
            if fn.endswith(_EXTS) or (os.path.isdir(p) and fn.endswith(".lance")):
                out.append(p)
        return out


def make_storage(workspace: str) -> Storage:
    """DP_STORAGE_URL selects the backend. Default (unset) = ``<workspace>/outputs`` locally; a
    ``file://`` or absolute path overrides the dir; an ``s3://…`` uri is a future adapter and fails
    loudly (rather than silently writing local) so the deployment story stays honest."""
    url = os.environ.get("DP_STORAGE_URL", "").strip()
    if url.startswith("s3://") or url.startswith("gs://"):
        raise NotImplementedError(f"object-storage backend {url!r} not implemented yet — unset DP_STORAGE_URL for local")
    root = url[len("file://"):] if url.startswith("file://") else (url or os.path.join(workspace, "outputs"))
    return LocalStorage(root)
