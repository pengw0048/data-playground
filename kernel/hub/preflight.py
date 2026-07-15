"""Source pre-flight — a cheap "will this source blow up?" check surfaced in the run-plan.

Object-store datasets fail two ways that a normal run only discovers by hanging or OOMing: a table with
tens of thousands of tiny fragments takes ~forever to read, and a file that a lifecycle policy has tiered
to cold storage (Glacier / Deep Archive) stalls or times out mid-run. This enumerates the fragment/file
count and flags cold-tier objects BEFORE the full run, so the run-plan can warn (or an operator can
compact / restore first) instead of finding out the hard way. Everything is best-effort: any probe error
yields no warning and never blocks the plan.
"""

from __future__ import annotations

import glob as _glob
import os

from hub import db
from hub.plugins.adapters import is_object_uri, path_of

_FRAGMENT_WARN = int(os.environ.get("DP_PREFLIGHT_FRAGMENTS", "10000"))  # files/fragments above this → warn
_COLD_CLASSES = {"GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"}
_DATA_EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".arrow", ".feather", ".lance")


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _object_store_configured() -> bool:
    """Whether the workspace has object storage configured — we only probe object URIs when it is, so an
    unconfigured/mistyped s3:// source can't hang the plan on a credential-less LIST to a real endpoint."""
    try:
        from hub import metadb
        return bool(metadb.cred_object_store_config(None))
    except Exception:  # noqa: BLE001
        return False


def _count_fragments(uri: str, cap: int) -> "int | None":
    """How many files a scan of `uri` would touch, counted cheaply and CAPPED (we only need to know it
    exceeds the threshold, not the exact huge number). None if it can't / shouldn't be determined."""
    if is_object_uri(uri):
        if not _object_store_configured():
            return None  # don't fire a credential-less LIST at a real endpoint on every plan call
        # DuckDB's glob() over httpfs (the engine's own read path) — CAPPED so a huge bucket can't block
        # the plan, and on the base connection's lock like every other base-conn access.
        pattern = uri if uri.endswith(_DATA_EXTS) else uri.rstrip("/") + "/**/*"
        q = f"SELECT count(*) FROM (SELECT 1 FROM glob({_lit(pattern)}) LIMIT {int(cap)})"
        with db.lock():
            return int(db.conn().sql(q).fetchone()[0])
    p = path_of(uri)
    if os.path.isdir(p):
        n = seen = 0
        for f in _glob.iglob(os.path.join(p, "**", "*"), recursive=True):
            seen += 1
            if os.path.isfile(f):
                n += 1
            if n >= cap or seen >= cap * 4:  # bound BOTH the file count and the entries walked
                break
        return n
    return 1 if os.path.exists(p) else None


def _cold_objects(uri: str, cap: int) -> int:
    """Count objects under an s3:// prefix in a cold storage class. Best-effort via boto3 (NOT a core
    dep — skipped, returning 0, when boto3 is absent); uses the workspace's object-store credentials."""
    if not uri.startswith("s3://") or not _object_store_configured():
        return 0  # only probe when object storage is configured — no credential-less round-trip per plan
    try:
        import boto3
    except ImportError:
        return 0
    from hub import metadb
    from hub.secrets import resolve_object_store
    cfg = resolve_object_store(metadb.cred_object_store_config(None))
    kw: dict = {}
    if cfg.get("endpoint"):
        kw["endpoint_url"] = cfg["endpoint"]
    if cfg.get("region"):
        kw["region_name"] = cfg["region"]
    if cfg.get("accessKeyId"):
        kw["aws_access_key_id"] = cfg["accessKeyId"]
        kw["aws_secret_access_key"] = cfg.get("secretAccessKey")
    s3 = boto3.client("s3", **kw)
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    cold = seen = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix.rstrip("*")):
        for o in page.get("Contents", []):
            seen += 1
            if o.get("StorageClass") in _COLD_CLASSES:
                cold += 1
            if seen >= cap:
                return cold
    return cold


def source_preflight(uri: str, fragment_warn: "int | None" = None, cap: int = 200_000) -> dict:
    """A cheap pre-run probe of one source uri → {uri, fragments, cold, warnings}. Best-effort throughout."""
    from hub.paths import canonical_data_uri

    read_uri = canonical_data_uri(uri)
    warn_at = _FRAGMENT_WARN if fragment_warn is None else fragment_warn
    warnings: list[str] = []
    frags = None
    try:
        frags = _count_fragments(read_uri, cap)
    except Exception:  # noqa: BLE001 — a probe failure must never block the plan
        frags = None
    if frags is not None and frags >= warn_at:
        warnings.append(f"{frags:,} files/fragments — many small files make reads slow and can OOM; compact first")
    cold = 0
    try:
        cold = _cold_objects(read_uri, cap)
    except Exception:  # noqa: BLE001
        cold = 0
    if cold:
        warnings.append(f"{cold} object(s) in cold storage (Glacier/Archive) — a full read will stall or time out")
    return {"uri": uri, "fragments": frags, "cold": cold, "warnings": warnings}
