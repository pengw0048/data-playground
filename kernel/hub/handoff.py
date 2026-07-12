"""Commit and garbage-collection contract for immutable region handoffs.

A distributed writer owns one unique ``.attempt-*`` prefix. Data shards are written first and the
manifest is written last. The controller publishes an attempt URI only after validating that manifest;
failed attempts can therefore be removed without touching a committed sibling.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterable

from hub.plugins.adapters import is_object_uri, object_fs, path_of

ATTEMPT_MARKER = ".attempt-"
MANIFEST_NAME = "_DP_SUCCESS.json"
MANIFEST_FORMAT = "data-playground-ray-handoff-v1"
_DEFAULT_GC_MIN_AGE_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_GC_BATCH = 100


def is_attempt_uri(uri: str) -> bool:
    """Whether ``uri`` names an immutable region-attempt prefix (not an arbitrary parent path)."""
    return ATTEMPT_MARKER in uri.rstrip("/").rsplit("/", 1)[-1]


def write_manifest(uri: str, *, run_id: str, rows: int, schema: object) -> None:
    """Write the commit marker last. A partial marker is invalid and is never published."""
    body = json.dumps({
        "format": MANIFEST_FORMAT,
        "runId": run_id,
        "rows": int(rows),
        "schema": str(getattr(schema, "base_schema", schema)),
    }, sort_keys=True).encode()
    if is_object_uri(uri):
        fs, path = object_fs(uri)
        with fs.open_output_stream(path.rstrip("/") + "/" + MANIFEST_NAME) as stream:
            stream.write(body)
        return
    directory = path_of(uri)
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(directory, MANIFEST_NAME)
    staged = final + ".tmp"
    with open(staged, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, final)


def read_manifest(uri: str) -> dict | None:
    """Return a validated commit manifest, or ``None`` on absence, corruption, or auth failure."""
    try:
        if is_object_uri(uri):
            fs, path = object_fs(uri)
            with fs.open_input_file(path.rstrip("/") + "/" + MANIFEST_NAME) as stream:
                raw = stream.read()
        else:
            with open(os.path.join(path_of(uri), MANIFEST_NAME), "rb") as stream:
                raw = stream.read()
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001 — uncertainty is an uncommitted handoff, never a cache hit
        return None
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if (not isinstance(doc, dict) or doc.get("format") != MANIFEST_FORMAT
            or not isinstance(doc.get("runId"), str) or not doc["runId"]
            or not isinstance(rows, int) or isinstance(rows, bool) or rows < 0):
        return None
    return doc


def discard_attempt(uri: str) -> None:
    """Best-effort removal of one failed immutable attempt; never accepts a stable output URI."""
    if not is_attempt_uri(uri):
        return
    try:
        if is_object_uri(uri):
            fs, path = object_fs(uri)
            fs.delete_dir(path.rstrip("/"))
        else:
            path = path_of(uri)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    except Exception:  # noqa: BLE001 — cleanup is best-effort; the terminal run status stays authoritative
        pass


def _positive_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _mtime_seconds(info) -> float | None:
    ns = getattr(info, "mtime_ns", None)
    if isinstance(ns, int) and ns > 0:
        return ns / 1_000_000_000
    value = getattr(info, "mtime", None)
    try:
        return value.timestamp() if value is not None else None
    except (AttributeError, OSError, ValueError):
        return None


def prune_object_attempts(root_uri: str, *, protected: Iterable[str] = (),
                          now: float | None = None) -> int:
    """Sweep stale object-store attempts under one region tier.

    The age floor protects readers that already resolved an older cache pointer while a concurrent run
    publishes a replacement. Operators should set ``DP_REGION_HANDOFF_GC_MIN_AGE_SECONDS`` above their
    maximum run duration. Current durable cache pointers are passed in ``protected``. Work per successful
    materialization is capped by ``DP_REGION_HANDOFF_GC_BATCH`` so object-store cleanup cannot dominate a
    foreground run; repeated materializations drain any backlog.
    """
    if not is_object_uri(root_uri):
        return 0
    import pyarrow.fs as pafs

    min_age = _positive_env("DP_REGION_HANDOFF_GC_MIN_AGE_SECONDS", _DEFAULT_GC_MIN_AGE_SECONDS)
    batch = _positive_env("DP_REGION_HANDOFF_GC_BATCH", _DEFAULT_GC_BATCH)
    if batch == 0:
        return 0
    try:
        fs, root = object_fs(root_uri)
        root = root.rstrip("/")
        infos = fs.get_file_info(pafs.FileSelector(root, recursive=True, allow_not_found=True))
        protected_paths = set()
        for uri in protected:
            if is_object_uri(uri):
                # object_fs returns the scheme-stripped bucket/key. Parse it directly so protecting 1000
                # durable cache pointers does not instantiate 1000 credentialed filesystem clients.
                protected_paths.add(uri.partition("://")[2].rstrip("/"))
    except Exception:  # noqa: BLE001 — GC is best-effort and must never fail a completed run
        return 0

    groups: dict[str, list[float | None]] = {}
    prefix = root + "/"
    for info in infos:
        # Object-store selectors synthesize directory entries without a meaningful mtime. Age the
        # prefix from its real objects; including a synthetic directory would retain every prefix forever.
        if getattr(info, "type", None) == pafs.FileType.Directory:
            continue
        if not info.path.startswith(prefix):
            continue
        child = info.path[len(prefix):].split("/", 1)[0]
        if ATTEMPT_MARKER not in child:
            continue
        groups.setdefault(prefix + child, []).append(_mtime_seconds(info))

    cutoff = (time.time() if now is None else now) - min_age
    candidates = []
    for path, mtimes in groups.items():
        # Unknown object mtimes are retained: deleting uncertain/live data is worse than leaking it.
        if path in protected_paths or not mtimes or any(m is None for m in mtimes):
            continue
        newest = max(mtimes)
        if newest <= cutoff:
            candidates.append((newest, path))

    removed = 0
    for _, path in sorted(candidates)[:batch]:
        try:
            fs.delete_dir(path)
            removed += 1
        except Exception:  # noqa: BLE001 — one undeletable prefix must not block later candidates
            continue
    return removed
