"""Commit contract for immutable region handoffs.

A distributed writer owns one unique ``.attempt-*`` prefix. Data shards are written first and the
manifest is written last. The controller publishes an attempt URI only after validating that manifest;
failed attempts remain unreferenced and can be expired without touching a committed sibling.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil

from hub.plugins.adapters import is_object_uri, object_fs, path_of

ATTEMPT_MARKER = ".attempt-"
MANIFEST_NAME = "_DP_SUCCESS.json"
MANIFEST_FORMAT = "data-playground-ray-handoff-v2"
_MAX_SHARDS = 200_000
_DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_DELETE_GRACE_SECONDS = 24 * 60 * 60


def is_attempt_uri(uri: str) -> bool:
    """Whether ``uri`` names an immutable region-attempt prefix (not an arbitrary parent path)."""
    return ATTEMPT_MARKER in uri.rstrip("/").rsplit("/", 1)[-1]


def _object_manifest_path(path: str) -> str:
    """Commit records use a separate prefix so storage lifecycle can expire them before data."""
    parent, name = path.rstrip("/").rsplit("/", 1)
    return f"{parent}/_dp_commits/{name}/{MANIFEST_NAME}"


def _object_commit_dir(path: str) -> str:
    return _object_manifest_path(path).rsplit("/", 1)[0]


def claim_attempt(uri: str, *, logical_uri: str, kind: str, run_id: str) -> None:
    """Register one exact object attempt in the shared control-plane DB before dispatch."""
    if not is_object_uri(uri):
        return
    if not is_attempt_uri(uri) or kind not in ("region", "sink"):
        raise ValueError("object attempt claims require an immutable attempt URI and region/sink kind")
    if not is_object_uri(logical_uri):
        raise ValueError("an object attempt requires an object-store logical target")
    from hub import metadb
    metadb.claim_object_attempt(uri.rstrip("/"), logical_uri.rstrip("/"), kind, str(run_id))


def mark_attempt_published(uri: str, *, reference_key: str | None = None) -> list[str]:
    """Publish the DB pointer and return exact prior sink attempts selected for retirement."""
    if not is_object_uri(uri):
        return []
    from hub import metadb
    return metadb.publish_object_attempt(uri.rstrip("/"), reference_key=reference_key)


def _delete_if_present(fs, path: str) -> None:
    import pyarrow.fs as pafs
    info = fs.get_file_info(path)
    try:
        if info.type == pafs.FileType.Directory:
            fs.delete_dir(path)
        elif info.type == pafs.FileType.File:
            fs.delete_file(path)
    except Exception:
        if fs.get_file_info(path).type != pafs.FileType.NotFound:
            raise


def _discard_object_attempt(fs, path: str, *, data: bool = True) -> None:
    _delete_if_present(fs, _object_commit_dir(path))
    if data:
        _delete_if_present(fs, path.rstrip("/"))


def _seconds_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return max(0.0, value) if math.isfinite(value) else default
    except ValueError:
        return default


def reap_attempts(*, retention_seconds: float | None = None,
                  delete_grace_seconds: float | None = None, limit: int = 100) -> dict[str, list[str]]:
    """Run one bounded, DB-indexed GC batch; never list an object-store parent prefix."""
    from hub import metadb
    deadline = max(1.0, _seconds_env("DP_RUN_DEADLINE_S", 3600))
    retention = (max(deadline, _seconds_env(
        "DP_ATTEMPT_RETENTION_SECONDS", _DEFAULT_RETENTION_SECONDS)) if retention_seconds is None
                 else max(0.0, float(retention_seconds)))
    grace = (max(deadline, _seconds_env(
        "DP_ATTEMPT_DELETE_GRACE_SECONDS", _DEFAULT_DELETE_GRACE_SECONDS))
             if delete_grace_seconds is None
             else max(0.0, float(delete_grace_seconds)))
    batch = metadb.object_attempt_gc_batch(retention, grace, limit=limit)
    result = {"retired": [], "deleted": []}
    for item in batch:
        uri, action = item["uri"], item["action"]
        try:
            fs, path = object_fs(uri)
            if action == "retire":
                if item.get("kind") == "sink":
                    metadb.catalog_delete_entry(uri)
                _discard_object_attempt(fs, path, data=False)
                metadb.mark_object_attempt_retired(uri)
                result["retired"].append(uri)
            elif action in ("discard", "delete"):
                _discard_object_attempt(fs, path)
                metadb.delete_object_attempt(uri)
                result["deleted"].append(uri)
        except Exception:  # noqa: BLE001 — one provider failure must not block the rest of the batch
            logging.getLogger("hub").warning("object attempt GC failed for %s (continuing)", uri,
                                             exc_info=True)
    return result


def _list_shards(uri: str) -> list[dict]:
    """Current Parquet objects under one attempt prefix."""
    shards: list[dict] = []
    if is_object_uri(uri):
        import pyarrow.fs as pafs
        fs, path = object_fs(uri)
        base = path.rstrip("/")
        prefix = base + "/"
        infos = fs.get_file_info(pafs.FileSelector(base, recursive=True, allow_not_found=True))
        for info in infos:
            if info.type == pafs.FileType.File and info.path.lower().endswith((".parquet", ".pq")):
                shards.append({"path": info.path[len(prefix):], "size": int(info.size)})
    else:
        base = path_of(uri)
        for root, _, files in os.walk(base):
            for name in files:
                if name.lower().endswith((".parquet", ".pq")):
                    path = os.path.join(root, name)
                    relative = os.path.relpath(path, base).replace(os.sep, "/")
                    shards.append({"path": relative, "size": os.path.getsize(path)})
    shards.sort(key=lambda item: item["path"])
    return shards


def _shard_inventory(uri: str) -> list[dict]:
    """Exact Parquet objects that make up an attempt, captured before the commit record is written."""
    shards = _list_shards(uri)
    if not shards:
        raise RuntimeError("region handoff produced no Parquet shard")
    if len(shards) > _MAX_SHARDS:
        raise RuntimeError(
            f"region handoff produced {len(shards):,} shards (limit {_MAX_SHARDS:,}); compact the region")
    return shards


def write_manifest(uri: str, *, run_id: str, rows: int, schema: object) -> None:
    """Write the commit marker last. A partial marker is invalid and is never published."""
    body = json.dumps({
        "format": MANIFEST_FORMAT,
        "runId": run_id,
        "rows": int(rows),
        "schema": str(getattr(schema, "base_schema", schema)),
        "shards": _shard_inventory(uri),
    }, sort_keys=True).encode()
    if is_object_uri(uri):
        fs, path = object_fs(uri)
        with fs.open_output_stream(_object_manifest_path(path)) as stream:
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
            with fs.open_input_file(_object_manifest_path(path)) as stream:
                raw = stream.read()
        else:
            with open(os.path.join(path_of(uri), MANIFEST_NAME), "rb") as stream:
                raw = stream.read()
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001 — uncertainty is an uncommitted handoff, never a cache hit
        return None
    rows = doc.get("rows") if isinstance(doc, dict) else None
    shards = doc.get("shards") if isinstance(doc, dict) else None
    valid_shards = isinstance(shards, list) and 0 < len(shards) <= _MAX_SHARDS and all(
        isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]
        and not item["path"].startswith("/") and ".." not in item["path"].split("/")
        and isinstance(item.get("size"), int) and not isinstance(item["size"], bool) and item["size"] >= 0
        for item in (shards or []))
    if (not isinstance(doc, dict) or doc.get("format") != MANIFEST_FORMAT
            or not isinstance(doc.get("runId"), str) or not doc["runId"]
            or not isinstance(rows, int) or isinstance(rows, bool) or rows < 0 or not valid_shards):
        return None
    return doc


def attempt_has_commit_record(uri: str) -> bool:
    """Whether any commit object exists, valid or corrupt; uncertainty is treated as occupied."""
    try:
        if is_object_uri(uri):
            import pyarrow.fs as pafs

            fs, path = object_fs(uri)
            return fs.get_file_info(_object_manifest_path(path)).type == pafs.FileType.File
        return os.path.lexists(os.path.join(path_of(uri), MANIFEST_NAME))
    except Exception:  # noqa: BLE001 — never overwrite a prefix whose commit state is unknown
        return True


def validate_shards(uri: str, manifest: dict) -> bool:
    """Fail closed unless the current Parquet path/size set exactly matches the committed inventory."""
    try:
        return _shard_inventory(uri) == manifest["shards"]
    except Exception:  # noqa: BLE001 — missing/auth/metadata uncertainty is never a cache hit
        return False


def attempt_has_shards(uri: str) -> bool:
    """Whether an unpublished prefix already contains data; uncertainty fails closed as occupied."""
    try:
        return bool(_list_shards(uri))
    except Exception:  # noqa: BLE001 — never overwrite a prefix whose state cannot be proven empty
        return True


def attempt_has_contents(uri: str) -> bool:
    """Whether an unpublished data prefix contains any object, not only a recognized Parquet shard."""
    try:
        if is_object_uri(uri):
            import pyarrow.fs as pafs

            fs, path = object_fs(uri)
            infos = fs.get_file_info(
                pafs.FileSelector(path.rstrip("/"), recursive=True, allow_not_found=True)
            )
            return any(info.type == pafs.FileType.File for info in infos)
        path = path_of(uri)
        if os.path.isfile(path) or os.path.islink(path):
            return True
        return any(files for _root, _dirs, files in os.walk(path))
    except Exception:  # noqa: BLE001 — never overwrite a prefix whose occupancy is uncertain
        return True


def discard_attempt(uri: str) -> None:
    """Best-effort removal of one failed immutable attempt; never accepts a stable output URI."""
    if not is_attempt_uri(uri):
        return
    try:
        if is_object_uri(uri):
            from hub import metadb
            if not metadb.begin_discard_object_attempt(uri.rstrip("/")):
                return
            fs, path = object_fs(uri)
            _discard_object_attempt(fs, path)
            metadb.delete_object_attempt(uri.rstrip("/"))
        else:
            path = path_of(uri)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    except Exception:  # noqa: BLE001 — cleanup is best-effort; the terminal run status stays authoritative
        pass
