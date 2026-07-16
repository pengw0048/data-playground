"""Dataset adapters — what a `dataset` can be.

Each adapter turns a uri into a LAZY DuckDB relation (out-of-core: DuckDB streams and spills,
never forcing a full in-memory materialization). Built-ins: Parquet, CSV, JSON, Arrow/Feather,
Lance, and directory-of-files. Plugins add Iceberg, Delta, warehouse tables, etc.

The `dataset` wire is therefore a lazy, Arrow-schema'd table handle — a DuckDB relation — that
carries its schema so wires are schema-aware.
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import os
import threading
import uuid
from collections.abc import Callable

import duckdb

from hub import db, paths
from hub.models import ColumnSchema
from hub.plugins.capabilities import tag_columns
from hub.sqlpolicy import identifier, identifier_list, quote_identifier

Relation = duckdb.DuckDBPyRelation
CancelCheck = Callable[[], bool]


class BoundedPreviewUnsupported(RuntimeError):
    """The adapter cannot prove that this URI can be read within the interactive preview budget."""


class RevisionUnavailable(RuntimeError):
    """An exact provider-native revision cannot be opened; callers must never fall back to head."""


def _raise_if_cancelled(cancelled: CancelCheck | None) -> None:
    """Fence a staged write before its externally visible publish point."""
    if cancelled is not None and cancelled():
        raise RuntimeError("run cancelled before output commit")


_TYPE_MAP = {
    "VARCHAR": "string", "BIGINT": "int", "INTEGER": "int", "HUGEINT": "int", "UBIGINT": "int",
    "SMALLINT": "int", "TINYINT": "int", "DOUBLE": "float", "FLOAT": "float", "REAL": "float",
    "BOOLEAN": "bool", "DATE": "date", "TIME": "time", "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamp", "BLOB": "bytes", "UUID": "string", "JSON": "json",
}


def display_type(duckdb_type: str) -> str:
    t = str(duckdb_type).upper()
    if t.endswith("[]"):
        return f"{display_type(t[:-2])}[]"
    if t.startswith(("DECIMAL", "NUMERIC")):
        return "float"
    if t.startswith("MAP"):
        return "map"  # a MAP arrives on the wire as [[k,v],…]; distinct from STRUCT so the UI renders it right
    if t.startswith("STRUCT"):
        return "struct"
    if t.startswith("LIST"):
        return "list"
    return _TYPE_MAP.get(t, t.lower())


_OBJECT_SCHEMES = ("s3://", "gs://", "gcs://", "r2://")


def is_object_uri(uri: str) -> bool:
    """An object-store uri (s3://, gs://, …) — read/written via DuckDB httpfs, not the local FS."""
    return uri.startswith(_OBJECT_SCHEMES)


def path_of(uri: str) -> str:
    from hub.paths import local_path

    path = local_path(uri)
    return uri if path is None else path


def _read_uri(uri: str) -> str:
    """Normalize a remote scheme only at adapter read boundaries, never at write/control ingress."""
    from hub.paths import canonical_data_uri

    return canonical_data_uri(uri)


def object_fs(uri: str):
    """A pyarrow filesystem + in-bucket path for an object-store uri, reading the same default Cred
    DuckDB's httpfs uses. Only needed for Arrow/Feather (IPC), which DuckDB cannot read or write
    as files — parquet/csv/json go straight through DuckDB+httpfs. Returns (filesystem, "bucket/key").

    S3/R2 credential parity is full (explicit keys / endpoint for MinIO·R2·any S3-compatible store, else
    the AWS chain). For GCS, pyarrow has NO HMAC-key parameter — only the GCP default chain
    (ADC / GOOGLE_APPLICATION_CREDENTIALS) or an access token — so HMAC keys configured for DuckDB can't
    be forwarded; rather than silently authenticate as a different (anonymous/ADC) identity, we fail with
    a clear message. A custom GCS endpoint (emulator) IS forwarded."""
    import pyarrow.fs as pafs

    from hub import metadb
    from hub.secrets import resolve_object_store
    scheme, _, rest = uri.partition("://")
    scheme = scheme.lower()
    cfg = metadb.cred_object_store_config(None)  # default Cred or deliberate ambient chain
    if not cfg:
        # Only a one-shot workload (subrun / Ray driver) with no hub settings DB falls back to the
        # allowlisted data-plane environment; a hub keeps its ambient credential chain.
        from hub.workload_env import data_plane_object_store_config, is_ephemeral_workload
        if is_ephemeral_workload():
            cfg = data_plane_object_store_config(scheme=scheme)
    cfg = resolve_object_store(cfg)  # Cred fields hold SecretRefs; resolve in-process
    endpoint = str(cfg.get("endpoint") or "").strip()
    if scheme in ("s3", "r2"):
        kw: dict = {}
        if cfg.get("accessKeyId") and cfg.get("secretAccessKey"):
            kw["access_key"], kw["secret_key"] = cfg["accessKeyId"], cfg["secretAccessKey"]
        if cfg.get("sessionToken"):
            kw["session_token"] = cfg["sessionToken"]
        if cfg.get("region"):
            kw["region"] = cfg["region"]
        if endpoint:  # MinIO / R2 / custom S3-compatible endpoint
            use_ssl = not endpoint.startswith("http://") if cfg.get("useSsl") is None else bool(cfg.get("useSsl"))
            kw["endpoint_override"] = endpoint.split("://", 1)[-1].rstrip("/")
            kw["scheme"] = "https" if use_ssl else "http"
        return pafs.S3FileSystem(**kw), rest
    if scheme in ("gs", "gcs"):
        if cfg.get("accessKeyId") and cfg.get("secretAccessKey"):
            raise NotImplementedError(
                "Arrow/Feather (.arrow/.feather/.ipc) over gs:// can't use the configured HMAC keys — "
                "pyarrow's GCS filesystem supports only Application Default Credentials "
                "(GOOGLE_APPLICATION_CREDENTIALS / gcloud auth) or an access token. Use parquet/csv/json "
                "on GCS (which do use the HMAC keys via DuckDB), or configure ADC.")
        kw = {}
        if endpoint:  # a GCS emulator (fake-gcs-server)
            use_ssl = not endpoint.startswith("http://") if cfg.get("useSsl") is None else bool(cfg.get("useSsl"))
            kw["endpoint_override"] = endpoint.split("://", 1)[-1].rstrip("/")
            kw["scheme"] = "https" if use_ssl else "http"
        return pafs.GcsFileSystem(**kw), rest
    raise ValueError(f"unsupported object-store scheme for Arrow/Feather: {scheme}://")


_ARROW_BATCH_TARGET_BYTES = 128 << 20  # ~128 MiB target per streamed RecordBatch
_ARROW_BATCH_MAX_ROWS = 65536          # cap rows/batch (narrow rows); a byte target derives the actual count


def _read_ipc(con: "duckdb.DuckDBPyConnection", source, filesystem=None) -> "Relation":
    """Read an Arrow-IPC (.arrow/.feather) file as a LAZY, re-scannable pyarrow Dataset → DuckDB relation.
    DuckDB streams batches from the dataset on demand (out-of-core READ) — NOT feather.read_table, which
    would load the whole file into RAM. A Dataset (unlike a one-shot RecordBatchReader) can be scanned
    more than once, so a query that reads the source twice still works."""
    import pyarrow.dataset as pds
    return con.from_arrow(pds.dataset(source, format="ipc", filesystem=filesystem))


def _stream_ipc(rel: "Relation", sink) -> int:
    """Write a DuckDB relation to an Arrow-IPC file `sink` (a local path or an open output stream) by
    STREAMING RecordBatches — never draining the whole relation into one in-RAM Arrow table (out-of-core
    WRITE). `sink` as a stream is used for the object-store temp-key upload. The batch size is BYTE-budgeted
    from the estimated row width (vectors/lists counted), so peak RAM is ~constant regardless of row width:
    a wide 4096-dim embedding gets far fewer rows/batch than a narrow int table, not a fixed 65536 rows."""
    import pyarrow.ipc as ipc
    from hub.estimate import _row_width
    width = max(8, _row_width(relation_columns(rel)))  # bytes/row; relation_columns is schema-only (no scan)
    batch_rows = max(1024, min(_ARROW_BATCH_MAX_ROWS, _ARROW_BATCH_TARGET_BYTES // width))
    reader = rel.to_arrow_reader(batch_rows)
    rows = 0
    with ipc.new_file(sink, reader.schema) as w:
        for batch in reader:
            w.write_batch(batch)
            rows += batch.num_rows
    return rows


def _copy_relation(rel: "Relation", path: str, options: str) -> int:
    """COPY a possibly one-shot relation once and return DuckDB's emitted row count.

    Counting first and writing second drains Arrow RecordBatchReader-backed relations (for example a
    Lance nearest-neighbour result), producing an empty artifact after reporting a non-zero count.
    DuckDB's COPY result carries the count from the same execution that wrote the bytes.
    """
    view = db.unique_view("write")
    rel.create_view(view)
    escaped = path.replace("'", "''")
    try:
        row = db.conn().execute(
            f"COPY {quote_identifier(view)} TO '{escaped}' ({options})"
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        db.conn().execute(f"DROP VIEW IF EXISTS {quote_identifier(view)}")


def _csv_kwargs(options: dict | None) -> dict:
    """Map a source node's CSV parse overrides to DuckDB read_csv kwargs. Empty → auto-detect (default).
    `delimiter` accepts a literal char or the words 'tab'/'\\t'; `header` is an explicit bool."""
    if not options:
        return {}
    kw: dict = {}
    d = str(options.get("delimiter") or "").strip()
    if d:
        kw["delimiter"] = {"tab": "\t", "\\t": "\t"}.get(d.lower(), d)
    h = str(options.get("header") or "").strip().lower()
    if h in ("yes", "true", "1"):
        kw["header"] = True
    elif h in ("no", "false", "0"):
        kw["header"] = False
    return kw


def relation_columns(rel: Relation) -> list[ColumnSchema]:
    # A local relation exposes logical and physical DuckDB types, but not durable
    # field identities, nullability, or defaults. Keep those facts explicitly unknown.
    cols = [ColumnSchema(name=n, type=display_type(str(t)), physical_type=str(t), provenance="inferred")
            for n, t in zip(rel.columns, rel.types)]
    return tag_columns(cols)


def _fingerprint_path(p: str) -> str:
    checked = paths.checked_local_path(p)
    if checked is None:
        return "unknown"
    p = checked
    try:
        # `p` is the realpath returned after root containment and shared-mode glob rejection.
        # codeql[py/path-injection]
        if os.path.isdir(p):
            # Best-available, bounded directory identity: the directory inode/mtime usually changes for
            # immediate membership updates, but does not identify arbitrary descendant contents. Never
            # recursively enumerate a dataset during profile preflight/recovery; strong versioned source
            # identity remains #226.
            # codeql[py/path-injection]
            st = os.stat(p)
            observed = f"dir:{p}:{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
            return hashlib.sha256(observed.encode()).hexdigest()[:16]
        # codeql[py/path-injection]
        st = os.stat(p)
        return hashlib.sha256(f"{p}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
    except OSError:
        return "unknown"


class DuckDBAdapter:
    """Parquet / CSV / JSON / Arrow-Feather / directory, via DuckDB + PyArrow. Fully out-of-core."""

    name = "duckdb"
    _EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc")

    # per-output-base locks (process-wide) serializing the shared-DIRECTORY mutations of a local append —
    # the publish + compaction dir-swap — across concurrent runs (kernel /run spawns daemon threads, and a
    # fan-in appends several pipelines to ONE output). Without this, a compaction's rmtree+swap races
    # another append's publish/read → lost/failed writes. Same-process only; cross-process (pod) append to
    # one base needs a directory lease / table format — documented as out of scope for the file adapter.
    _base_locks_guard = threading.Lock()
    _base_locks: "dict[str, threading.Lock]" = {}

    @classmethod
    def _base_lock(cls, base: str) -> "threading.Lock":
        key = os.path.abspath(base.rstrip("/"))
        with cls._base_locks_guard:
            lk = cls._base_locks.get(key)
            if lk is None:
                lk = cls._base_locks[key] = threading.Lock()
            return lk

    def matches(self, uri: str) -> bool:
        uri = _read_uri(uri)
        if uri.startswith("mem://") or is_object_uri(uri):
            return True
        p = uri.lower()
        local = paths.checked_local_path(uri)
        if local is not None and os.path.isdir(local):
            return True
        return p.endswith(self._EXTS)

    def scan(self, uri: str, columns: list[str] | None = None,
             predicate: str | None = None, limit: int | None = None,
             options: dict | None = None) -> Relation:
        con = db.conn()
        normalized = _read_uri(uri)
        if (is_object_uri(normalized)
                and normalized.lower().endswith((".arrow", ".feather", ".ipc"))
                and limit is not None):
            if int(limit) != 0:
                raise BoundedPreviewUnsupported(
                    "remote Arrow/Feather/IPC has no bounded row reader — needs a full pass"
                )
            # Schema-only callers need the IPC footer, not any record batch. Avoid the eager full-object
            # feather.read_table path used by an actual durable run.
            import pyarrow as pa
            import pyarrow.ipc as ipc
            fs, path = object_fs(normalized)
            with fs.open_input_file(path) as stream:
                schema = ipc.open_file(stream).schema
            rel = con.from_arrow(pa.Table.from_batches([], schema=schema))
            if columns:
                selected = [identifier(c, rel.columns, label="projection column") for c in columns]
                rel = rel.project(", ".join(quote_identifier(c) for c in selected))
            return rel
        rel = self._read(con, uri, options)
        if columns:
            selected = [identifier(c, rel.columns, label="projection column") for c in columns]
            rel = rel.project(", ".join(quote_identifier(c) for c in selected))
        if predicate:
            rel = rel.filter(predicate)
        if limit is not None:
            rel = rel.limit(int(limit))
        return rel

    def preview_scan(self, uri: str, columns: list[str] | None = None,
                     limit: int = 2000, options: dict | None = None) -> Relation:
        """A hard-bounded source read for interactive preview.

        Directory/prefix/glob datasets can require enumerating an arbitrary number of files before a row
        limit applies. Arrow IPC can place the entire file in one record batch, so even its local lazy
        reader has no strict row-level source bound. Refuse these shapes instead of pretending an outer
        LIMIT bounds namespace or batch work; durable full runs retain the ordinary scan path.
        """
        normalized = _read_uri(uri)
        low = normalized.lower()
        if glob.has_magic(normalized):
            raise BoundedPreviewUnsupported(
                "glob sources have no bounded namespace preview — needs a full pass"
            )
        if low.endswith((".arrow", ".feather", ".ipc")):
            raise BoundedPreviewUnsupported(
                "Arrow/Feather/IPC record batches have no strict bounded preview — needs a full pass"
            )
        local: str | None = None
        if is_object_uri(normalized):
            if not low.endswith((".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson")):
                raise BoundedPreviewUnsupported(
                    "object-store prefixes have no bounded namespace preview — needs a full pass"
                )
        else:
            local = paths.checked_local_path(normalized)
            # `local` is the checked realpath; the read below also consumes this exact spelling.
            # codeql[py/path-injection]
            if local is not None and os.path.isdir(local):
                raise BoundedPreviewUnsupported(
                    "directory datasets have no bounded namespace preview — needs a full pass"
                )
        # The read must consume the exact canonical path that crossed the containment check. Reusing the
        # original file URI/symlink here would split check from use; remote URIs keep their normalized form.
        return self.scan(local if local is not None else normalized,
                         columns=columns, limit=int(limit), options=options)

    def _read(self, con: duckdb.DuckDBPyConnection, uri: str, options: dict | None = None) -> Relation:
        uri = _read_uri(uri)
        csv = _csv_kwargs(options)  # explicit CSV parse overrides (delimiter / header); else auto-detect
        if uri.startswith("mem://"):
            return con.table(uri[len("mem://"):])
        if is_object_uri(uri):
            db.ensure_object_store()  # load httpfs + credentials
            low = uri.lower()
            if low.endswith((".csv", ".tsv")):
                return con.read_csv(uri, **csv)
            if low.endswith((".json", ".ndjson")):
                return con.read_json(uri)
            if low.endswith((".arrow", ".feather", ".ipc")):
                # DuckDB has no Arrow-IPC file reader, so pull the object through pyarrow's own S3/GCS
                # filesystem (same creds). Read EAGERLY here: a lazy pyarrow Dataset over an object store is
                # unreliable two ways — a bare `s3://…` string makes pyarrow build its OWN filesystem that
                # ignores the endpoint override (region-resolution HeadObject → ACCESS_DENIED on MinIO), and
                # even with our explicit filesystem, DuckDB defers the arrow scan to its own thread where the
                # S3 access fails (NETWORK_CONNECTION). An object feather is network-fetched regardless — the
                # out-of-core streaming READ win is on LOCAL files (below); the streaming WRITE covers both.
                import pyarrow.feather as feather
                fs, p = object_fs(uri)
                with fs.open_input_file(p) as f:
                    return con.from_arrow(feather.read_table(f))
            if low.endswith((".parquet", ".pq")):
                return con.read_parquet(uri)
            # a prefix of parts (append / worker-direct shards / a Hive-partitioned write): union_by_name
            # reconciles per-shard schema drift (an all-null column degrades to parquet NULL type, and a
            # plain multi-file read fails "cast X to NULL"). hive_partitioning surfaces dir=val partition
            # columns + prunes, but ONLY when the prefix genuinely has key=val subdirs — otherwise a flat
            # part prefix whose PATH incidentally contains `key=val` would get a spurious partition column
            # injected. (union_by_name alone disables hive detection, so it must be explicit when wanted.)
            return con.read_parquet(uri.rstrip("/") + "/**/*.parquet", union_by_name=True,
                                    hive_partitioning=self._is_hive_dir(uri, obj=True))
        local = paths.checked_local_path(uri)
        p = local if local is not None else uri
        low = p.lower()
        if os.path.isdir(p):
            return self._read_dir(con, p)
        if low.endswith((".csv", ".tsv")):
            return con.read_csv(p, **csv)
        if low.endswith((".json", ".ndjson")):
            return con.read_json(p)
        if low.endswith((".arrow", ".feather", ".ipc")):
            return _read_ipc(con, p)  # lazy IPC dataset — streamed, not the whole file into RAM
        return con.read_parquet(p)

    @staticmethod
    def _is_hive_dir(d: str, obj: bool) -> bool:
        """True iff the dir/prefix's IMMEDIATE children are Hive `key=val` partition dirs (a partitioned
        write), vs flat `part-*.<ext>` files (append / worker-direct shards). Gates hive_partitioning so a
        flat dataset whose absolute path incidentally contains a `key=val` segment (a workspace/dest prefix)
        doesn't get a spurious partition column injected. (A genuinely-partitioned dataset UNDER a `key=val`
        base is a rarer residual — DuckDB still parses the base segment; documented.)"""
        base = d.rstrip("/")
        if obj:
            try:
                import pyarrow.fs as pafs
                fs, p = object_fs(base + "/")
                infos = fs.get_file_info(pafs.FileSelector(p.rstrip("/"), recursive=False, allow_not_found=True))
                return any(i.type == pafs.FileType.Directory and "=" in os.path.basename(i.path.rstrip("/"))
                           for i in infos)
            except Exception:  # noqa: BLE001 — can't list → treat as flat (no spurious partition column)
                return False
        return any(os.path.isdir(x) for x in glob.glob(os.path.join(base, "*=*")))

    def _read_dir(self, con: duckdb.DuckDBPyConnection, d: str) -> Relation:
        # cover every extension the append writer can emit (a dir of part-*.<ext>): parquet/pq, csv/tsv, json
        # parquet uses union_by_name so per-shard schema drift (an all-null column degrading to NULL type in
        # one worker-direct shard) reconciles by column name instead of failing on read order.
        hive = self._is_hive_dir(d, obj=False)  # only a genuine key=val partitioned dir enables hive parsing
        for ext in (".parquet", ".pq"):
            if glob.glob(os.path.join(d, f"**/*{ext}"), recursive=True):
                # hive_partitioning surfaces dir=val partition columns + prunes; scoped to a real partitioned
                # dir so a flat part dir isn't given a spurious column from a `key=val` path segment.
                return con.read_parquet(os.path.join(d, f"**/*{ext}"), union_by_name=True, hive_partitioning=hive)
        # csv/json parts ALSO union_by_name: appends can drift the column SET across parts (a later append
        # adds/drops a column), and a plain positional multi-file read would misalign or error — reconcile
        # by column name, exactly like the parquet path.
        for ext, reader in ((".csv", con.read_csv), (".tsv", con.read_csv), (".json", con.read_json)):
            if glob.glob(os.path.join(d, f"**/*{ext}"), recursive=True):
                return reader(os.path.join(d, f"**/*{ext}"), union_by_name=True)
        raise ValueError(f"no parquet/csv/json files under {d}")

    def _maybe_compact(self, base: str, ext: str) -> None:
        """When a LOCAL append part dir accumulates more than DP_APPEND_COMPACT_PARTS committed parts,
        rewrite them into ONE compacted part — bounding the unbounded small-file growth of a repeatedly-
        appended dataset (many tiny parts kill read performance + inode/list budgets). Read ALL parts
        (union_by_name → schema-drift safe) into one part in base.compact-*, then swap via TWO atomic
        renames (base→base.old, tmp→base) + rmtree the old — so the window where `base` is absent is two
        renames wide (microseconds), NOT a whole rmtree. The full read is materialized by the write BEFORE
        any rename (no read-after-delete). MUST be called under _base_lock(base): that serializes it against
        concurrent same-base appends (a compaction swap racing another append's publish/read would lose or
        fail writes) — note the potentially large rewrite runs while holding that lock, so same-base appends
        block for its duration. Same-process only — a concurrent READER in the tiny two-rename window still
        gets a transient 'no files' error (retryable); cross-process append/compaction of one base needs a
        directory lease or a table format (out of scope for the file adapter). If the process crashes
        between the two renames, `base` is momentarily absent and the data sits in base.old-* — recovered
        automatically at next startup by LocalStorage.recover_orphans() (or manually renaming it back)."""
        try:
            threshold = int(os.environ.get("DP_APPEND_COMPACT_PARTS", "200") or 200)
        except ValueError:
            threshold = 200
        if threshold <= 0:
            return  # 0/negative disables auto-compaction
        d = base.rstrip("/")
        # one exact extension per dataset (enforced by _reject_mixed_part_format), so count only THIS ext —
        # a single glob, not one per _PART_EXTS, since this runs under the lock on every append.
        parts = glob.glob(os.path.join(d, f"**/*{ext}"), recursive=True)
        if len(parts) <= threshold:
            return
        import shutil
        tmp_dir = d + f".compact-{uuid.uuid4().hex[:8]}"
        old_dir = d + f".old-{uuid.uuid4().hex[:8]}"
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            # use the CURRENT connection/cursor (db.conn() = the run's scope cursor if any, else base) —
            # NOT a nested db.run_scope(), which would clobber the enclosing run's thread-local cursor.
            rel = self._read_dir(db.conn(), d)                       # union of all parts (fully read by the write)
            self._write_part(rel, os.path.join(tmp_dir, f"part-compacted-{uuid.uuid4().hex[:12]}{ext}"), ext)
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)               # a failed read/write leaves the parts intact
            raise
        os.replace(d, old_dir)                                       # base → base.old (base briefly absent…
        os.replace(tmp_dir, d)                                       # …until this rename — a two-rename window)
        shutil.rmtree(old_dir, ignore_errors=True)                   # the originals, now safely superseded

    def schema(self, uri: str) -> list[ColumnSchema]:
        with db.base_guard():  # executes on the base connection when off a run_scope (catalog probe)
            return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
            with db.base_guard():  # serialize the base-connection fetch (register runs on daemon threads)
                return int(self.scan(uri).aggregate("count(*) AS n").fetchone()[0])
        except Exception:  # noqa: BLE001
            return None

    def metadata_count(self, uri: str) -> int | None:
        """Exact count from one local Parquet footer; arbitrary directories stay unknown.

        Recursively listing a partitioned dataset and opening every footer is data-page-free but still
        unbounded admission work (millions of objects/partitions are possible). A future bounded summary
        metadata capability can opt such datasets back in explicitly.
        """
        normalized = _read_uri(uri)
        if is_object_uri(normalized) or normalized.startswith("mem://"):
            return None
        local = paths.checked_local_path(normalized)
        if local is None:
            return None
        try:
            import pyarrow.parquet as pq
            # `local` is the checked realpath; shared-mode glob patterns have already failed closed.
            # codeql[py/path-injection]
            if os.path.isfile(local) and local.lower().endswith((".parquet", ".pq")):
                return int(pq.ParquetFile(local).metadata.num_rows)
        except Exception:  # noqa: BLE001 - metadata uncertainty means unknown, never a fallback scan
            return None
        return None

    def fingerprint(self, uri: str) -> str:
        uri = _read_uri(uri)
        if uri.startswith("mem://"):
            return "mem"
        if is_object_uri(uri):
            return "obj:" + hashlib.sha256(uri.encode()).hexdigest()[:12]  # can't stat; key by uri
        return _fingerprint_path(uri)

    def write(self, uri: str, rel: Relation, mode: str = "overwrite", partition_by: str | None = None,
              cancelled: CancelCheck | None = None) -> dict:
        obj = is_object_uri(uri)
        if obj:
            db.ensure_object_store()  # load httpfs + credentials
        target = uri if obj else path_of(uri)  # object stores keep the full s3://… uri
        low = target.lower()
        _raise_if_cancelled(cancelled)
        pcols = identifier_list(partition_by, rel.columns, label="partitionBy column")
        if pcols:
            return self._write_partitioned(target, rel, pcols, mode, low, obj, cancelled)
        if mode == "append":
            # append = a DIRECTORY / prefix of part files (out-of-core; the reader reads them all back via
            # _read_dir). Only for row formats that have a directory-scan reader — parquet/csv/tsv/json;
            # feather/arrow have no directory-scan reader. Each part is written TRANSACTIONALLY: locally to
            # a `.tmp-<uuid>` sibling the reader glob (`**/*.<ext>`) can't match, then os.replace'd to the
            # committed name — so a crashed/cancelled/OOM'd append never leaves a partial part the next read
            # would pick up. (Object stores write direct: DuckDB's httpfs upload finalizes only on the
            # multipart Complete, so no partial object is visible — same guarantee the overwrite path uses.)
            if not low.endswith((".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson")):
                raise NotImplementedError(f"append is only supported for parquet/csv/json outputs, not {os.path.splitext(target)[1] or 'this'}")
            base, ext = os.path.splitext(target)  # name.parquet -> prefix "name", ext ".parquet"
            if obj and ext.lower() in (".csv", ".tsv", ".json", ".ndjson"):
                # the object read dispatches a bare prefix to read_parquet, so a csv/json part prefix would
                # write fine but read back as an IOException — reject up front rather than silently produce
                # an unreadable dataset. Object-store append is parquet-only; csv/json append is local.
                raise NotImplementedError(
                    "object-store append supports parquet only (a csv/json part prefix reads back as parquet"
                    " → unreadable) — use parquet for object-store append, or append on the local FS")
            part_name = f"part-{uuid.uuid4().hex[:12]}{ext}"
            if obj:
                self._reject_mixed_part_format(base, ext, obj)  # one exact extension per dataset (read picks one)
                _raise_if_cancelled(cancelled)  # enter the object append commit phase before moving/publishing
                self._migrate_singlefile_into_dir(target, base, ext, obj)  # overwrite→append: fold prior file in
                part = base.rstrip("/") + "/" + part_name
                rows = self._write_part(rel, part, ext)
            else:
                import contextlib
                # stage the part as a SIBLING of base (NOT inside it) so a concurrent compaction's rmtree of
                # base can't destroy this thread's unpublished part. The slow write is UNLOCKED (unique name,
                # no contention); makedirs + migrate + publish + compaction run under the per-base lock, so
                # they're serialized against a concurrent same-base compaction's dir swap. The staging name
                # carries NO data extension (format is chosen by the `ext` arg, not the path) — so a crash-
                # orphaned staging file can't be mistaken for a published dataset by list_outputs / a source
                # glob / destination browse (same `.tmp-*`-not-`.parquet` discipline as overwrite/partitioned).
                staging = base + f".parttmp-{uuid.uuid4().hex[:10]}"
                try:
                    rows = self._write_part(rel, staging, ext)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.remove(staging)
                    raise
                with self._base_lock(base):
                    try:
                        # This is the authoritative format check: it MUST share the publish lock. Two
                        # different-format appends may both finish staging while the dataset is empty; only
                        # the first may publish, and the second must observe that committed extension before
                        # its os.replace. Staging stays outside the lock so same-format writes remain parallel.
                        self._reject_mixed_part_format(base, ext, obj=False)
                        _raise_if_cancelled(cancelled)  # staging complete; fence before any visible mutation
                        os.makedirs(base, exist_ok=True)
                        self._migrate_singlefile_into_dir(target, base, ext, obj)  # overwrite→append fold-in
                        os.replace(staging, os.path.join(base, part_name))  # publish INTO base (a committed part)
                    except BaseException:
                        with contextlib.suppress(OSError):
                            os.remove(staging)
                        raise
                    self._maybe_compact(base, ext)            # bound unbounded small-part growth (under the lock)
            return {"uri": base, "rows": rows}
        if mode not in ("overwrite", None):
            raise NotImplementedError(f"write mode '{mode}' is not supported — use overwrite or append")
        if not obj:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        # Local overwrite: write to a temp sibling then os.replace, so a failed or cancelled write never
        # truncates the existing dataset. On object stores DuckDB writes parquet/csv/json as a single
        # object (a PUT — the prior object is replaced only once the new one lands); feather goes through
        # pyarrow and gets its own temp-key + server-side move below (its streamed multipart upload would
        # otherwise finalize a partial object on close). The format is chosen by `low` (the real
        # extension) while the bytes go to `wtarget`, renamed to `target` on success.
        wtarget = target if obj else f"{target}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            _raise_if_cancelled(cancelled)
            if low.endswith((".csv", ".tsv")):
                rows = _copy_relation(rel, wtarget, "FORMAT CSV")
            elif low.endswith((".json", ".ndjson")):
                # DuckDB writes JSON out-of-core via COPY; ARRAY true emits a top-level [] read_json reads back
                rows = _copy_relation(rel, wtarget, "FORMAT JSON, ARRAY true")
            elif low.endswith((".arrow", ".feather", ".ipc")):
                # Arrow-IPC has no DuckDB writer; go through pyarrow, STREAMING RecordBatches (not
                # to_arrow_table, which materializes the whole result in RAM → OOM on a big write). On an
                # object store, pyarrow.feather given a raw "s3://…" string would write a LOCAL file of that
                # name (silent corruption), so open a real object stream via pyarrow's filesystem.
                # open_output_stream finalizes its multipart upload on close() even on an error/cancel — so
                # stream to a TEMP key and promote with a server-side move only on success, leaving the
                # prior object intact if the write fails partway.
                if obj:
                    fs, p = object_fs(target)
                    tmp = f"{p}.tmp-{uuid.uuid4().hex[:8]}"
                    try:
                        with fs.open_output_stream(tmp) as f:
                            rows = _stream_ipc(rel, f)
                        _raise_if_cancelled(cancelled)
                        fs.move(tmp, p)  # server-side copy+delete; the destination is replaced only now
                    except BaseException:
                        import contextlib
                        with contextlib.suppress(Exception):
                            fs.delete_file(tmp)
                        raise
                else:
                    rows = _stream_ipc(rel, wtarget)
            else:
                rows = _copy_relation(rel, wtarget, "FORMAT PARQUET")
            if not obj:
                _raise_if_cancelled(cancelled)
                os.replace(wtarget, target)
        except BaseException:
            if not obj:
                import contextlib
                with contextlib.suppress(OSError):
                    os.remove(wtarget)
            raise
        return {"uri": uri, "rows": rows}

    def _write_partitioned(self, target: str, rel: Relation, pcols: list, mode: str, low: str,
                           obj: bool, cancelled: CancelCheck | None = None) -> dict:
        """A Hive-partitioned parquet DIRECTORY (dir=val/… layout), read back partition-pruned (the read
        path passes hive_partitioning=True). Parquet + overwrite only. Local publication uses a recoverable
        old/new directory swap: the previous version is parked before the staged version is published, and
        startup recovery restores or removes those siblings based on whether the base exists. Object-store
        partition overwrite is rejected because this file adapter has no atomic multi-object commit primitive.
        NB: per the Hive layout, a partition column's type is RE-INFERRED from the string dir name on read —
        an int widens to BIGINT, and a boolean / all-numeric-string partition key comes back as VARCHAR /
        BIGINT. This is inherent to Hive partitioning (the interop point); partition by a low-cardinality
        categorical / int / date column, not a boolean."""
        import contextlib
        import shutil
        if mode == "append":
            raise NotImplementedError("partitioned write does not support append — use overwrite")
        if not low.endswith((".parquet", ".pq")):
            raise NotImplementedError("partitionBy is parquet-only (a Hive-partitioned directory)")
        base = os.path.splitext(target)[0]  # a DIRECTORY (Hive layout), not a single file
        cols_sql = ", ".join(quote_identifier(c) for c in pcols)

        def _copy(dst: str) -> int:
            return _copy_relation(
                rel, dst,
                f"FORMAT PARQUET, PARTITION_BY ({cols_sql}), OVERWRITE_OR_IGNORE",
            )
        if obj:
            # Replacing a Hive prefix spans many independent objects. Delete-then-write loses the old
            # version on failure; writing in place mixes old and new partitions when the partition set
            # shrinks. Without an immutable version prefix plus an atomic catalog pointer, neither path is
            # an overwrite. Fail before listing, deleting, or writing any object.
            raise NotImplementedError(
                "object-store partition overwrite requires an atomic table format or catalog commit; "
                "this file adapter supports unpartitioned object-store overwrite and append only")

        token = uuid.uuid4().hex[:8]
        tmp = base + f".partition-new-{token}"
        old = base + f".partition-old-{token}"
        parked = False
        try:
            rows = _copy(tmp)                             # fully write and validate the staged version first
            with self._base_lock(base):
                _raise_if_cancelled(cancelled)             # fence before parking the visible prior version
                if os.path.lexists(base):
                    os.replace(base, old)                 # preserve the complete prior version for rollback
                    parked = True
                try:
                    os.replace(tmp, base)                 # publish the complete staged version
                except BaseException:
                    # Restore synchronously for ordinary write/cancellation failures. A hard process crash
                    # between the two renames is handled by LocalStorage.recover_orphans() at startup.
                    if parked and not os.path.lexists(base) and os.path.lexists(old):
                        with contextlib.suppress(OSError):
                            os.replace(old, base)
                    raise
        except BaseException:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp, ignore_errors=True)
            raise
        if parked:
            with contextlib.suppress(OSError):
                if os.path.isdir(old) and not os.path.islink(old):
                    shutil.rmtree(old)
                else:
                    os.remove(old)
        return {"uri": base, "rows": rows}

    # -- append part-directory helpers (transactional; row formats only) ---------------------------- #
    @staticmethod
    def _write_part(rel: Relation, path: str, ext: str) -> int:
        """Write ONE append part (parquet/csv/json — the row formats _read_dir can scan back)."""
        el = ext.lower()
        if el in (".csv", ".tsv"):
            return _copy_relation(rel, path, "FORMAT CSV")
        elif el in (".json", ".ndjson"):  # out-of-core COPY, ARRAY true → a top-level [] read_json reads back
            return _copy_relation(rel, path, "FORMAT JSON, ARRAY true")
        return _copy_relation(rel, path, "FORMAT PARQUET")

    _PART_EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson")

    def _existing_part_exts(self, base: str, obj: bool) -> set:
        """The EXACT part-file extensions already committed under this dir/prefix (a `.tmp-*` part in
        flight is ignored — it doesn't match the *.<ext> suffix). Exact, NOT collapsed into a format
        family: _read_dir globs each concrete extension SEPARATELY and returns on the first match, so a
        dir holding both `.parquet` and `.pq` (same format!) would silently read back only the .parquet
        parts — the .pq data lost."""
        if obj:
            try:
                import pyarrow.fs as pafs
                fs, p = object_fs(base.rstrip("/") + "/")
                infos = fs.get_file_info(pafs.FileSelector(p, recursive=True, allow_not_found=True))
                names = [i.path for i in infos if i.type == pafs.FileType.File]
            except Exception:  # noqa: BLE001 — can't list → treat as empty (append proceeds)
                names = []
            return {e for e in self._PART_EXTS if any(n.endswith(e) for n in names)}
        d = base.rstrip("/")
        return {e for e in self._PART_EXTS if glob.glob(os.path.join(d, f"**/*{e}"), recursive=True)}

    def _reject_mixed_part_format(self, base: str, ext: str, obj: bool) -> None:
        """_read_dir globs each concrete extension separately and returns on the FIRST match, so a dataset
        dir holding more than one extension silently DROPS the non-winning parts on read. Enforce ONE exact
        extension per dataset — reject an append whose extension differs from the committed parts (incl.
        same-format aliases like .parquet vs .pq, which the reader still globs separately)."""
        others = self._existing_part_exts(base, obj) - {ext.lower()}
        if others:
            raise NotImplementedError(
                f"cannot append {ext} to a dataset that already holds {sorted(others)} parts — "
                "one file extension per output dataset")

    def _migrate_singlefile_into_dir(self, target: str, base: str, ext: str, obj: bool) -> None:
        """overwrite writes a single FILE (name.parquet, registered uri=name.parquet); append writes into a
        DIRECTORY (name/, registered uri=name). Switching a write node overwrite→append repoints the catalog
        at the dir and would ORPHAN the prior single file (a sibling of the dir, not under its prefix). Fold
        that file in as the first part so no data is lost. No-op if there's no pre-existing single file."""
        part = f"part-migrated-{uuid.uuid4().hex[:12]}{ext}"
        if obj:
            import pyarrow.fs as pafs
            try:
                fs, src = object_fs(target)
                info = fs.get_file_info(src)
            except Exception:  # noqa: BLE001 — can't PROBE the prior object → nothing to migrate; append proceeds
                return
            if info.type != pafs.FileType.File:
                return  # no pre-existing single object at this exact key
            _, dst = object_fs(base.rstrip("/") + "/" + part)
            fs.move(src, dst)  # a real MOVE failure PROPAGATES — silently orphaning the prior data is worse
        elif os.path.isfile(target):  # target == name.parquet (a file), base == name (a dir): coexist OK
            os.makedirs(base, exist_ok=True)
            os.replace(target, os.path.join(base, part))


class ManagedLocalFileRevisionAdapter:
    """Revision provider for core-owned local artifacts recorded by the catalog ledger."""

    name = "managed-local-file"
    retention_owner = "core"

    def revision_history(self, uri: str, *, limit: int, cursor: str | None = None) -> tuple[list[dict], str | None]:
        from hub import metadb

        try:
            return metadb.managed_local_file_revision_history(uri, limit=limit, cursor=cursor)
        except (KeyError, ValueError) as exc:
            raise RevisionUnavailable("revision_unavailable") from exc

    def resolve_revision(self, uri: str, *, as_of: datetime.datetime | None = None) -> dict:
        from hub import metadb

        try:
            return metadb.managed_local_file_revision_resolve(uri, as_of=as_of)
        except KeyError as exc:
            raise RevisionUnavailable("revision_unavailable") from exc

    def open_revision(self, uri: str, revision_id: str) -> Relation:
        from hub import metadb

        try:
            artifact_uri = metadb.managed_local_file_revision_open(uri, revision_id)
            return DuckDBAdapter().scan(artifact_uri)
        except (KeyError, OSError, duckdb.Error) as exc:
            raise RevisionUnavailable("revision_unavailable") from exc

    def revision_detail(self, uri: str, revision_id: str, *, preview_limit: int) -> dict:
        """Read bounded facts and preview from one exact immutable local Parquet artifact."""
        from hub import metadb

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq

            detail = metadb.managed_local_file_revision_detail(uri, revision_id)
            artifact_uri = detail["artifact_uri"]
            parquet = pq.ParquetFile(artifact_uri)
            bounded = max(1, min(int(preview_limit), 100))
            batch = next(parquet.iter_batches(batch_size=bounded + 1), None)
            preview = (pa.Table.from_batches([batch]) if batch is not None
                       else pa.Table.from_batches([], schema=parquet.schema_arrow))
            table = detail["table"]
            return {
                "revision_id": detail["revision_id"],
                "committed_at": detail["committed_at"],
                "parent_revision_id": detail["parent_revision_id"],
                "producer_operation": "overwrite",
                "columns": table.columns,
                "row_count": parquet.metadata.num_rows,
                "data_file_count": 1,
                "total_bytes": os.path.getsize(artifact_uri),
                "fragment_count": None,
                "preview_table": preview,
            }
        except RevisionUnavailable:
            raise
        except Exception as exc:
            raise RevisionUnavailable("revision_unavailable") from exc


def managed_local_file_revision_adapter(uri: str) -> ManagedLocalFileRevisionAdapter | None:
    """Select the ledger-backed provider only for the exact current managed local head."""
    from hub import metadb

    try:
        metadb.managed_local_file_revision_history(uri, limit=1)
    except (KeyError, ValueError):
        return None
    return _MANAGED_LOCAL_FILE_REVISION_ADAPTER


_MANAGED_LOCAL_FILE_REVISION_ADAPTER = ManagedLocalFileRevisionAdapter()


class LanceAdapter:
    """Lance is open source, so it is a CORE adapter. pylance loaded lazily.

    Scans STREAM into DuckDB via a Lance scanner → Arrow RecordBatchReader (out-of-core: batches are
    pulled on demand, never materializing the whole dataset in RAM), with column/limit pushdown.
    """

    name = "lance"

    def matches(self, uri: str) -> bool:
        return _read_uri(uri).lower().rstrip("/").endswith(".lance")

    def _dataset(self, uri: str, **kwargs):
        try:
            import lance  # lazy — only if the optional `lance` extra is installed
        except ModuleNotFoundError as e:  # a clear remediation, not a raw "No module named 'lance'"
            raise ModuleNotFoundError("Lance support is not installed — run: uv pip install -e 'kernel[lance]'") from e
        normalized = _read_uri(uri)
        local = paths.checked_local_path(normalized)
        return lance.dataset(local if local is not None else normalized, **kwargs)

    def scan(self, uri: str, columns: list[str] | None = None,
             predicate: str | None = None, limit: int | None = None,
             options: dict | None = None) -> Relation:  # options (CSV knobs) don't apply to Lance — ignored
        # stream batches into DuckDB instead of ds.to_table() (which loads the ENTIRE dataset into RAM
        # before handing it over — a real-scale Lance run/write would OOM and defeat out-of-core).
        ds = self._dataset(uri)
        selected = ([identifier(c, ds.schema.names, label="projection column") for c in columns]
                    if columns else None)
        if predicate:
            # PUSH the filter into Lance's scanner → fragment/scalar-index pruning + correct filter-THEN-
            # limit order — BUT only for a predicate with NO double-quote: Lance's datafusion dialect reads
            # a double-quoted `"col"` as a STRING LITERAL (not an identifier) and ACCEPTS it, silently
            # returning the WRONG rows (an under-selection the engine's downstream re-filter can't recover).
            # A `"` almost always means a quoted identifier (a space/reserved-word column); SQL string
            # literals use single quotes. If present, or if Lance rejects the predicate, use a DuckDB-side
            # filter — correct, just no pushdown.
            if '"' not in predicate:
                try:
                    reader = ds.scanner(columns=selected, filter=predicate, limit=limit).to_reader()
                    return db.conn().from_arrow(reader)
                except Exception:  # noqa: BLE001 — a datafusion dialect gap → DuckDB fallback below
                    pass
            # DuckDB-side filter: read ALL columns (the predicate may reference a column not in `columns`,
            # which a projected scan would fail to bind on), filter, THEN project + limit (filter before
            # limit is what makes a limited filtered scan correct).
            rel = db.conn().from_arrow(ds.scanner().to_reader()).filter(predicate)
            if selected:
                rel = rel.project(", ".join(quote_identifier(c) for c in selected))
            return rel.limit(int(limit)) if limit is not None else rel
        reader = ds.scanner(columns=selected, limit=limit).to_reader()
        return db.conn().from_arrow(reader)

    def preview_scan(self, uri: str, columns: list[str] | None = None,
                     limit: int = 2000, options: dict | None = None) -> Relation:
        """Use Lance's scanner row limit directly; preview never takes the full-scan filter fallback."""
        ds = self._dataset(uri)
        selected = ([identifier(c, ds.schema.names, label="projection column") for c in columns]
                    if columns else None)
        reader = ds.scanner(columns=selected, limit=int(limit)).to_reader()
        return db.conn().from_arrow(reader)

    def nearest(self, uri: str, column: str, query, k: int = 10) -> Relation:
        """Top-k nearest rows to a query vector via Lance's native search (a vector index if one exists,
        else a flat scan) — pushed into Lance rather than a brute-force cosine over every row. Streams
        the result and exposes `_score` = cosine similarity (1 − distance), matching the generic path."""
        ds = self._dataset(uri)
        selected = identifier(column, ds.schema.names, label="vector column")
        reader = ds.scanner(
            nearest={"column": selected, "q": list(query), "k": int(k), "metric": "cosine"}).to_reader()
        rel = db.conn().from_arrow(reader)
        return rel.project("* EXCLUDE (_distance), (1 - _distance) AS _score")  # Lance ranks by distance asc

    def schema(self, uri: str) -> list[ColumnSchema]:
        with db.base_guard():  # scan() feeds a Lance reader into the base DuckDB connection off-scope
            return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
            return int(self._dataset(uri).count_rows())  # pure pylance — no shared DuckDB connection
        except Exception:  # noqa: BLE001
            return None

    def fingerprint(self, uri: str) -> str:
        try:
            return f"lance-v{self._dataset(uri).version}"
        except Exception:  # noqa: BLE001
            return _fingerprint_path(_read_uri(uri))

    @staticmethod
    def _revision_id(value) -> str:
        try:
            version = int(value)
        except (TypeError, ValueError) as exc:
            raise RevisionUnavailable("revision_unavailable") from exc
        if version < 1 or str(version) != str(value):
            raise RevisionUnavailable("revision_unavailable")
        return str(version)

    def revision_history(self, uri: str, *, limit: int, cursor: str | None = None) -> tuple[list[dict], str | None]:
        """Return a bounded newest-first native page; the cursor is the last native version seen."""
        try:
            bounded = max(1, min(int(limit), 100))
            before = int(self._revision_id(cursor)) if cursor is not None else None
            rows = []
            for entry in reversed(self._dataset(uri).versions()):
                revision_id = self._revision_id(entry.get("version"))
                if before is not None and int(revision_id) >= before:
                    continue
                rows.append({"revision_id": revision_id, "committed_at": entry.get("timestamp")})
                if len(rows) == bounded + 1:
                    break
            has_more = len(rows) > bounded
            items = rows[:bounded]
            return items, (items[-1]["revision_id"] if has_more and items else None)
        except RevisionUnavailable:
            raise
        except Exception as exc:
            raise RevisionUnavailable("revision_unavailable") from exc

    def resolve_revision(self, uri: str, *, as_of: datetime.datetime | None = None) -> dict:
        try:
            if as_of is None:
                dataset = self._dataset(uri)
            else:
                dataset = self._dataset(uri, asof=as_of)
            return {"revision_id": self._revision_id(dataset.version), "committed_at": None}
        except RevisionUnavailable:
            raise
        except Exception as exc:
            raise RevisionUnavailable("revision_unavailable") from exc

    def open_revision(self, uri: str, revision_id: str) -> Relation:
        try:
            dataset = self._dataset(uri, version=int(self._revision_id(revision_id)))
            dataset.schema  # Lance can defer a missing-version error until the first metadata access.
        except RevisionUnavailable:
            raise
        except Exception as exc:
            raise RevisionUnavailable("revision_unavailable") from exc
        return db.conn().from_arrow(dataset.scanner().to_reader())

    def revision_detail(self, uri: str, revision_id: str, *, preview_limit: int) -> dict:
        """Read bounded, exact-version facts without consulting the mutable current head."""
        try:
            import pyarrow as pa

            exact_id = self._revision_id(revision_id)
            dataset = self._dataset(uri, version=int(exact_id))
            dataset.schema  # Force a retained-version check before exposing any facts.
            versions = list(self._dataset(uri).versions())
            entry = next((item for item in versions
                          if self._revision_id(item.get("version")) == exact_id), None)
            if entry is None:
                raise RevisionUnavailable("revision_unavailable")
            metadata = entry.get("metadata") or {}
            parent_id = str(int(exact_id) - 1) if int(exact_id) > 1 else None
            parent = (parent_id if parent_id is not None and any(
                self._revision_id(item.get("version")) == parent_id for item in versions) else None)

            def metadata_int(name: str) -> int | None:
                value = metadata.get(name)
                try:
                    return int(value) if value is not None and int(value) >= 0 else None
                except (TypeError, ValueError):
                    return None

            bounded = max(1, min(int(preview_limit), 100))
            reader = dataset.scanner(limit=bounded + 1).to_reader()
            table = pa.Table.from_batches(list(reader), schema=reader.schema)
            empty = pa.Table.from_batches([], schema=dataset.schema)
            return {
                "revision_id": exact_id,
                "committed_at": entry.get("timestamp"),
                "parent_revision_id": parent,
                # Lance's version metadata does not identify the producing job/operation.
                "producer_operation": None,
                "columns": relation_columns(db.conn().from_arrow(empty)),
                "row_count": int(dataset.count_rows()),
                "data_file_count": metadata_int("total_data_files"),
                "total_bytes": metadata_int("total_files_size"),
                "fragment_count": metadata_int("total_fragments"),
                "preview_table": table,
            }
        except RevisionUnavailable:
            raise
        except Exception as exc:
            raise RevisionUnavailable("revision_unavailable") from exc

    def write(self, uri: str, rel: Relation, mode: str = "overwrite", partition_by: str | None = None,
              cancelled: CancelCheck | None = None) -> dict:
        if partition_by and partition_by.strip():
            raise NotImplementedError("partitionBy is not supported for Lance output (Hive partitioning is parquet-only)")
        try:
            import lance
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError("Lance support is not installed — run: uv pip install -e 'kernel[lance]'") from e
        mode = mode or "overwrite"  # None → overwrite (matches the signature default + DuckDBAdapter)
        if mode not in ("overwrite", "append"):
            # NOT silently degraded to append (the old `"overwrite" if mode=="overwrite" else "append"`
            # turned a typo — or an unimplemented merge/update/delete — into a stray append: a correctness
            # landmine). Lance mutation modes (merge_insert/update/delete) are a future capability.
            raise NotImplementedError(f"Lance write mode '{mode}' is not supported — use overwrite or append")
        _raise_if_cancelled(cancelled)
        # Count while Lance consumes the one stream. A separate aggregate first drains a DuckDB relation
        # backed by a one-shot Arrow reader and silently publishes an empty dataset on the second pass.
        import pyarrow as pa

        source = rel.to_arrow_reader(1 << 16)
        rows = 0

        def counted_batches():
            nonlocal rows
            while True:
                _raise_if_cancelled(cancelled)
                try:
                    batch = source.read_next_batch()
                except StopIteration:
                    return
                rows += batch.num_rows
                yield batch

        reader = pa.RecordBatchReader.from_batches(source.schema, counted_batches())
        lance.write_dataset(reader, path_of(uri), mode=mode)
        return {"uri": uri, "rows": rows}


def default_adapters() -> list:
    return [LanceAdapter(), DuckDBAdapter()]
