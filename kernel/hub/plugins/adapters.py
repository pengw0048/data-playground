"""Dataset adapters — what a `dataset` can be.

Each adapter turns a uri into a LAZY DuckDB relation (out-of-core: DuckDB streams and spills,
never forcing a full in-memory materialization). Built-ins: Parquet, CSV, JSON, Arrow/Feather,
Lance, and directory-of-files. Plugins add Iceberg, Delta, warehouse tables, etc.

The `dataset` wire is therefore a lazy, Arrow-schema'd table handle — a DuckDB relation — that
carries its schema so wires are schema-aware.
"""

from __future__ import annotations

import glob
import hashlib
import os
import uuid
from urllib.parse import urlparse

import duckdb

from hub import db
from hub.models import ColumnSchema
from hub.plugins.capabilities import tag_columns

Relation = duckdb.DuckDBPyRelation

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
    p = urlparse(uri)
    return p.path if p.scheme in ("file", "") else uri


def object_fs(uri: str):
    """A pyarrow filesystem + in-bucket path for an object-store uri, reading the SAME `objectStore`
    setting DuckDB's httpfs uses. Only needed for Arrow/Feather (IPC), which DuckDB cannot read or write
    as files — parquet/csv/json go straight through DuckDB+httpfs. Returns (filesystem, "bucket/key").

    S3/R2 credential parity is full (explicit keys / endpoint for MinIO·R2·any S3-compatible store, else
    the AWS chain). For GCS, pyarrow has NO HMAC-key parameter — only the GCP default chain
    (ADC / GOOGLE_APPLICATION_CREDENTIALS) or an access token — so HMAC keys configured for DuckDB can't
    be forwarded; rather than silently authenticate as a different (anonymous/ADC) identity, we fail with
    a clear message. A custom GCS endpoint (emulator) IS forwarded."""
    import pyarrow.fs as pafs

    from hub import metadb
    cfg = metadb.get_setting("objectStore", "global", default={}) or {}
    scheme, _, rest = uri.partition("://")
    scheme = scheme.lower()
    endpoint = str(cfg.get("endpoint") or "").strip()
    if scheme in ("s3", "r2"):
        kw: dict = {}
        if cfg.get("accessKeyId") and cfg.get("secretAccessKey"):
            kw["access_key"], kw["secret_key"] = cfg["accessKeyId"], cfg["secretAccessKey"]
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


def _stream_ipc(rel: "Relation", sink) -> None:
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
    with ipc.new_file(sink, reader.schema) as w:
        for batch in reader:
            w.write_batch(batch)


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
    cols = [ColumnSchema(name=n, type=display_type(str(t))) for n, t in zip(rel.columns, rel.types)]
    return tag_columns(cols)


def _fingerprint_path(p: str) -> str:
    try:
        if os.path.isdir(p):
            parts = []
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    st = os.stat(fp)
                    parts.append(f"{fp}:{st.st_size}:{st.st_mtime_ns}")
            return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
        st = os.stat(p)
        return hashlib.sha256(f"{p}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
    except OSError:
        return "unknown"


class DuckDBAdapter:
    """Parquet / CSV / JSON / Arrow-Feather / directory, via DuckDB + PyArrow. Fully out-of-core."""

    name = "duckdb"
    _EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc")

    def matches(self, uri: str) -> bool:
        if uri.startswith("mem://") or is_object_uri(uri):
            return True
        p = path_of(uri).lower()
        if os.path.isdir(path_of(uri)):
            return True
        return p.endswith(self._EXTS)

    def scan(self, uri: str, columns: list[str] | None = None,
             predicate: str | None = None, limit: int | None = None,
             options: dict | None = None) -> Relation:
        con = db.conn()
        rel = self._read(con, uri, options)
        if columns:
            rel = rel.project(", ".join(f'"{c}"' for c in columns))
        if predicate:
            rel = rel.filter(predicate)
        if limit is not None:
            rel = rel.limit(int(limit))
        return rel

    def _read(self, con: duckdb.DuckDBPyConnection, uri: str, options: dict | None = None) -> Relation:
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
        p = path_of(uri)
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

    def schema(self, uri: str) -> list[ColumnSchema]:
        with db.base_guard():  # executes on the base connection when off a run_scope (catalog probe)
            return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
            with db.base_guard():  # serialize the base-connection fetch (register runs on daemon threads)
                return int(self.scan(uri).aggregate("count(*) AS n").fetchone()[0])
        except Exception:  # noqa: BLE001
            return None

    def fingerprint(self, uri: str) -> str:
        if uri.startswith("mem://"):
            return "mem"
        if is_object_uri(uri):
            return "obj:" + hashlib.sha256(uri.encode()).hexdigest()[:12]  # can't stat; key by uri
        return _fingerprint_path(path_of(uri))

    def write(self, uri: str, rel: Relation, mode: str = "overwrite", partition_by: str | None = None) -> dict:
        obj = is_object_uri(uri)
        if obj:
            db.ensure_object_store()  # load httpfs + credentials
        target = uri if obj else path_of(uri)  # object stores keep the full s3://… uri
        low = target.lower()
        rows = int(rel.aggregate("count(*)").fetchone()[0])
        pcols = [c.strip() for c in (partition_by or "").split(",") if c.strip()]
        if pcols:
            return self._write_partitioned(target, rel, pcols, mode, low, obj, rows)
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
            self._reject_mixed_part_format(base, ext, obj)   # one exact extension per dataset (read picks one)
            self._migrate_singlefile_into_dir(target, base, ext, obj)  # overwrite→append: fold prior file in
            part_name = f"part-{uuid.uuid4().hex[:12]}{ext}"
            if obj:
                part = base.rstrip("/") + "/" + part_name
                self._write_part(rel, part, ext)
            else:
                os.makedirs(base, exist_ok=True)
                final = os.path.join(base, part_name)
                tmp = final + f".tmp-{uuid.uuid4().hex[:8]}"  # NOT matched by the reader glob until promoted
                try:
                    self._write_part(rel, tmp, ext)
                    os.replace(tmp, final)                    # only a COMPLETE part ever becomes visible
                except BaseException:
                    import contextlib
                    with contextlib.suppress(OSError):
                        os.remove(tmp)
                    raise
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
            if low.endswith((".csv", ".tsv")):
                rel.write_csv(wtarget)
            elif low.endswith((".json", ".ndjson")):
                # DuckDB writes JSON out-of-core via COPY; ARRAY true emits a top-level [] read_json reads back
                rel.query("_w", f"COPY _w TO '{wtarget.replace(chr(39), chr(39) * 2)}' (FORMAT JSON, ARRAY true)")
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
                            _stream_ipc(rel, f)
                        fs.move(tmp, p)  # server-side copy+delete; the destination is replaced only now
                    except BaseException:
                        import contextlib
                        with contextlib.suppress(Exception):
                            fs.delete_file(tmp)
                        raise
                else:
                    _stream_ipc(rel, wtarget)
            else:
                rel.write_parquet(wtarget)
            if not obj:
                os.replace(wtarget, target)
        except BaseException:
            if not obj:
                import contextlib
                with contextlib.suppress(OSError):
                    os.remove(wtarget)
            raise
        return {"uri": uri, "rows": rows}

    def _write_partitioned(self, target: str, rel: Relation, pcols: list, mode: str, low: str,
                           obj: bool, rows: int) -> dict:
        """A Hive-partitioned parquet DIRECTORY (dir=val/… layout), read back partition-pruned (the read
        path passes hive_partitioning=True). Parquet + overwrite only. Local: write a fresh temp dir then
        atomically swap it in (a partitioned dir can't os.replace a non-empty target, so rmtree-then-rename
        — a brief non-atomic window, but the temp dir is fully written first). Object: DuckDB httpfs writes
        the prefix directly — a partitioned object write spans many objects, so it is NOT atomic (documented).
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
        missing = [c for c in pcols if c not in rel.columns]
        if missing:
            raise ValueError(f"partitionBy columns not in the data: {missing}")
        base = os.path.splitext(target)[0]  # a DIRECTORY (Hive layout), not a single file
        cols_sql = ", ".join(f'"{c}"' for c in pcols)

        def _copy(dst: str) -> None:
            rel.query("_w", f"COPY _w TO '{dst.replace(chr(39), chr(39) * 2)}' "
                            f"(FORMAT PARQUET, PARTITION_BY ({cols_sql}), OVERWRITE_OR_IGNORE)")
        if obj:
            # CLEAN overwrite: delete the target prefix first, else a re-run whose partition set SHRANK
            # would leave stale partition objects (cat=2 from a prior write) that the read then merges in
            # → wrong row count. Not atomic (delete-then-write; a mid-failure loses the prior data — the
            # inherent limitation of a multi-object partitioned overwrite without a table format), but
            # CORRECT on success, unlike OVERWRITE_OR_IGNORE-into-a-live-prefix which is wrong even on success.
            import pyarrow.fs as pafs
            fs, p = object_fs(base.rstrip("/") + "/")
            infos = fs.get_file_info(pafs.FileSelector(p.rstrip("/"), recursive=True, allow_not_found=True))
            for i in infos:  # explicit per-object delete (delete_dir_contents no-ops on S3's synthetic dirs)
                if i.type == pafs.FileType.File:
                    fs.delete_file(i.path)
            _copy(base.rstrip("/"))  # httpfs writes the prefix
            return {"uri": base, "rows": rows}
        tmp = base + f".tmp-{uuid.uuid4().hex[:8]}"
        try:
            _copy(tmp)                                    # fully write the temp partitioned dir first
            if os.path.isdir(base):
                shutil.rmtree(base)
            elif os.path.exists(base):
                os.remove(base)                           # a prior single-file output at this base name
            os.replace(tmp, base)                         # swap in (brief non-atomic window: rmtree→rename)
        except BaseException:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp, ignore_errors=True)
            raise
        return {"uri": base, "rows": rows}

    # -- append part-directory helpers (transactional; row formats only) ---------------------------- #
    @staticmethod
    def _write_part(rel: Relation, path: str, ext: str) -> None:
        """Write ONE append part (parquet/csv/json — the row formats _read_dir can scan back)."""
        el = ext.lower()
        if el in (".csv", ".tsv"):
            rel.write_csv(path)
        elif el in (".json", ".ndjson"):  # out-of-core COPY, ARRAY true → a top-level [] read_json reads back
            rel.query("_w", f"COPY _w TO '{path.replace(chr(39), chr(39) * 2)}' (FORMAT JSON, ARRAY true)")
        else:
            rel.write_parquet(path)

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


class LanceAdapter:
    """Lance is open source, so it is a CORE adapter. pylance loaded lazily.

    Scans STREAM into DuckDB via a Lance scanner → Arrow RecordBatchReader (out-of-core: batches are
    pulled on demand, never materializing the whole dataset in RAM), with column/limit pushdown.
    """

    name = "lance"

    def matches(self, uri: str) -> bool:
        return path_of(uri).lower().rstrip("/").endswith(".lance")

    def _dataset(self, uri: str):
        try:
            import lance  # lazy — only if the optional `lance` extra is installed
        except ModuleNotFoundError as e:  # a clear remediation, not a raw "No module named 'lance'"
            raise ModuleNotFoundError("Lance support is not installed — run: uv pip install -e 'kernel[lance]'") from e
        return lance.dataset(path_of(uri))

    def scan(self, uri: str, columns: list[str] | None = None,
             predicate: str | None = None, limit: int | None = None,
             options: dict | None = None) -> Relation:  # options (CSV knobs) don't apply to Lance — ignored
        # stream batches into DuckDB instead of ds.to_table() (which loads the ENTIRE dataset into RAM
        # before handing it over — a real-scale Lance run/write would OOM and defeat out-of-core).
        reader = self._dataset(uri).scanner(columns=columns, limit=limit).to_reader()
        rel = db.conn().from_arrow(reader)
        if predicate:
            rel = rel.filter(predicate)
        return rel

    def nearest(self, uri: str, column: str, query, k: int = 10) -> Relation:
        """Top-k nearest rows to a query vector via Lance's native search (a vector index if one exists,
        else a flat scan) — pushed into Lance rather than a brute-force cosine over every row. Streams
        the result and exposes `_score` = cosine similarity (1 − distance), matching the generic path."""
        reader = self._dataset(uri).scanner(
            nearest={"column": column, "q": list(query), "k": int(k), "metric": "cosine"}).to_reader()
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
            return _fingerprint_path(path_of(uri))

    def write(self, uri: str, rel: Relation, mode: str = "overwrite", partition_by: str | None = None) -> dict:
        if partition_by and partition_by.strip():
            raise NotImplementedError("partitionBy is not supported for Lance output (Hive partitioning is parquet-only)")
        try:
            import lance
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError("Lance support is not installed — run: uv pip install -e 'kernel[lance]'") from e
        rows = int(rel.aggregate("count(*)").fetchone()[0])
        # stream RecordBatches into Lance (bounded memory) instead of materializing the whole table
        reader = rel.record_batch(1 << 16)
        lance.write_dataset(reader, path_of(uri), mode="overwrite" if mode == "overwrite" else "append")
        return {"uri": uri, "rows": rows}


def default_adapters() -> list:
    return [LanceAdapter(), DuckDBAdapter()]
