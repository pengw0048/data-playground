"""Where run outputs are persisted — pluggable so the default local directory can be swapped for
object storage (S3/GCS) with no change to the engine or write nodes: a write node/DuckDB just writes
to the uri that Storage hands back (a local path, or an ``s3://…`` uri — both are real, written via
the same DuckDB path the adapters use). Selected by DP_STORAGE_URL.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Protocol

from hub.plugins.adapters import is_object_uri

# temp siblings an interrupted local append/compaction/partition overwrite can leave next to a base dir
# (see adapters.py):
# `<base>.parttmp-<hex10>` (an in-flight append part), `<base>.old-<hex8>` (pre-compaction originals, briefly
# holding the data while the two-rename swap is in flight), `<base>.compact-<hex8>` (the compaction output),
# and `<base>.partition-{old,new}-<hex8>` (the same recoverable protocol for partition overwrite).
# The hex lengths are matched EXACTLY (what the adapter emits) so a real output can't accidentally collide
# with the suffix and be renamed/deleted at startup.
_TEMP_SUFFIX = re.compile(
    r"\.(?:(?P<parttmp>parttmp)-[0-9a-f]{10}|"
    r"(?P<kind>old|compact|partition-old|partition-new)-[0-9a-f]{8})$")

_EXTS = (".parquet", ".csv", ".tsv", ".json", ".arrow", ".feather", ".ipc")
# ephemeral full-pass run results (runner._materialize_result) share the outputs dir but are NOT
# user-published datasets — exclude them from list_outputs so a restart doesn't re-catalog them into
# the Tables view (P0-UX-01). Keyed by this basename prefix.
RESULT_PREFIX = "__result_"


class Storage(Protocol):
    def output_uri(self, name: str, ext: str) -> str: ...
    def list_outputs(self) -> list[str]: ...
    def recover_orphans(self) -> None: ...


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
            if fn.startswith(RESULT_PREFIX):
                continue  # an ephemeral run result, not a published output — never re-catalog it
            if fn.endswith(_EXTS) or (os.path.isdir(p) and fn.endswith(".lance")):
                out.append(p)
        return out

    def recover_orphans(self) -> None:
        """Recover/clean temp siblings an interrupted local write left under ``root`` — run once at startup
        (before re-cataloging outputs). Three hazards it closes: (1) a SIGKILL/OOM/power-loss
        mid append-part write leaves a ``<base>.parttmp-*`` that must NOT surface as a dataset; (2) a crash
        between compaction's two renames leaves ``<base>`` momentarily absent with the data in ``<base>.old-*``
        — restore it so the dataset stays readable; (3) partition overwrite uses the same old/new protocol,
        with ``partition-old`` as the rollback version and ``partition-new`` as unpublished staging.
        Best-effort; never raises.

        Pass 1 restores parked prior versions: if ``<base>`` is gone, the swap was cut between renames and
        the prior version is renamed back; if ``<base>`` exists, publication completed and the old sibling is
        stale. Pass 2 drops append, compaction, and partition staging, which is never the committed copy."""
        if not os.path.isdir(self.root):
            return
        try:
            entries = os.listdir(self.root)
        except OSError:
            return
        def _kind(m: re.Match) -> str:
            return m.group("parttmp") or m.group("kind")

        def _remove(path: str) -> None:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)

        for fn in entries:  # pass 1: restore a swap cut between its two renames
            m = _TEMP_SUFFIX.search(fn)
            if not m or _kind(m) not in ("old", "partition-old"):
                continue
            old, base = os.path.join(self.root, fn), os.path.join(self.root, fn[: m.start()])
            try:
                if os.path.lexists(base):
                    _remove(old)                           # base intact → superseded prior version
                else:
                    os.replace(old, base)                  # base gone mid-swap → restore prior version
            except OSError:
                pass
        for fn in entries:  # pass 2: drop partial/superseded in-flight staging
            m = _TEMP_SUFFIX.search(fn)
            if not m or _kind(m) not in ("parttmp", "compact", "partition-new"):
                continue
            p = os.path.join(self.root, fn)
            try:
                _remove(p)
            except OSError:
                pass

    def prune_results(self, keep: int = 200) -> None:
        """Bound the ephemeral run-result artifacts (RESULT_PREFIX) — coarse newest-N GC so they can't
        grow without limit (retention/refcount is later work). Best-effort; never raises."""
        try:
            files = [os.path.join(self.root, fn) for fn in os.listdir(self.root) if fn.startswith(RESULT_PREFIX)]
            for p in sorted(files, key=lambda x: os.path.getmtime(x), reverse=True)[keep:]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except OSError:
            pass


class ObjectStorage:
    """Outputs live under an object-store prefix (``s3://…`` / ``gs://…``). Reads and writes go
    through the same DuckDB httpfs path the adapters use — the write node just writes to the uri
    handed back."""

    def __init__(self, root: str):
        self.root = root.rstrip("/")

    def output_uri(self, name: str, ext: str) -> str:
        return f"{self.root}/{name}{ext}"

    def recover_orphans(self) -> None:
        return  # object append writes unique part names directly (no temp-sibling staging, no compaction)

    def list_outputs(self) -> list[str]:
        from hub import db
        try:  # missing creds / unreachable bucket at boot must not crash startup — just show nothing
            db.ensure_object_store()
            with db.lock():
                rows = db.conn().execute(f"SELECT file FROM glob('{self.root}/*')").fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [f for (f,) in rows
                if f.lower().endswith(_EXTS) and not os.path.basename(f.rstrip("/")).startswith(RESULT_PREFIX)]


def make_storage(workspace: str) -> Storage:
    """DP_STORAGE_URL selects the backend. Default (unset) = ``<workspace>/outputs`` locally; a
    ``file://`` or absolute path overrides the dir; an ``s3://…`` / ``gs://…`` uri persists outputs
    to that object-store prefix (real, via httpfs). For a CUSTOM sink, set ``DP_STORAGE`` to a dotted
    path to a Storage class (``pkg.mod:Cls``), instantiated as ``Cls(workspace)`` — a plugin sink with
    no core patch. The built-in Local/Object storages are just the two default paths here."""
    cls = os.environ.get("DP_STORAGE", "").strip()
    if cls:
        from hub.settings import import_dotted
        return import_dotted(cls)(workspace)
    url = os.environ.get("DP_STORAGE_URL", "").strip()
    if is_object_uri(url):
        return ObjectStorage(url)
    root = url[len("file://"):] if url.startswith("file://") else (url or os.path.join(workspace, "outputs"))
    return LocalStorage(root)
