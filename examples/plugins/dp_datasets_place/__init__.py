"""Reference plugin — a **destination "place"** that browses only DATASET files.

A destination is a named place in the save/open dialog's sidebar (like a file dialog's shortcuts) that
you browse and read/write. Core ships `local` (any directory) and `s3`/`gs`. This adds a place of kind
`datasets` that lists sub-directories and dataset files (`.parquet`/`.csv`/`.json`/`.arrow`/`.lance`)
under a configured root and HIDES everything else — a cleaner browser than the raw local place for a
folder full of mixed files. Add a preset in Settings → Destinations with this backend + a root dir.

It demonstrates the `DestinationBackend` seam via `reg.add_destination(...)` (the built-in local/s3/gs
go through the same registry): implement `kind` + `browse(root, path)` (→ `{path, entries:[{name, kind,
uri}], error?}`) + `target_uri(root, path, filename)` (the write path). Path traversal is fenced to the
preset's root. Drop this folder into `<workspace>/plugins/`.
"""

from __future__ import annotations

import os

_DATASET_EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc", ".lance")


class DatasetsPlace:
    kind = "datasets"

    def _safe(self, root: str, path: str) -> str:
        top = os.path.realpath(root)
        base = os.path.realpath(os.path.join(top, path.lstrip("/")))
        return base if (base == top or base.startswith(top + os.sep)) else top  # never escape the root

    def browse(self, root: str, path: str) -> dict:
        base = self._safe(root, path)
        try:
            names = sorted(os.listdir(base))
        except OSError as e:
            return {"path": path, "entries": [], "error": str(e)}
        entries = []
        for fn in names:
            if fn.startswith("."):
                continue
            p = os.path.join(base, fn)
            is_dir = os.path.isdir(p) and not fn.endswith(".lance")  # a .lance dir is a dataset, not a folder
            if is_dir or fn.lower().endswith(_DATASET_EXTS):         # datasets-only: hide the rest
                entries.append({"name": fn, "kind": "dir" if is_dir else "file", "uri": p})
        return {"path": path, "entries": entries}

    def target_uri(self, root: str, path: str, filename: str) -> str:
        return os.path.join(self._safe(root, path), os.path.basename(filename))  # basename: no traversal via the name


def register(reg) -> None:
    reg.add_destination(DatasetsPlace())  # claims the 'datasets' place kind
