"""Commit contract for immutable region handoffs.

A distributed writer owns one unique ``.attempt-*`` prefix. Data shards are written first and the
manifest is written last. The controller publishes an attempt URI only after validating that manifest;
failed attempts can therefore be removed without touching a committed sibling.
"""

from __future__ import annotations

import json
import os
import shutil

from hub.plugins.adapters import is_object_uri, object_fs, path_of

ATTEMPT_MARKER = ".attempt-"
MANIFEST_NAME = "_DP_SUCCESS.json"
MANIFEST_FORMAT = "data-playground-ray-handoff-v1"


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
