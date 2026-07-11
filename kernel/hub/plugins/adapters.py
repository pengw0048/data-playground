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
                # filesystem (same creds) rather than the httpfs path used for parquet/csv/json.
                import pyarrow.feather as feather
                fs, p = object_fs(uri)
                with fs.open_input_file(p) as f:
                    return con.from_arrow(feather.read_table(f))
            if low.endswith((".parquet", ".pq")):
                return con.read_parquet(uri)
            # a prefix of parts (append / worker-direct shards): union_by_name reconciles per-shard schema
            # drift — an all-null column in one shard degrades to parquet NULL type, and a plain multi-file
            # read fails "cast X to NULL" depending on which shard is read first.
            return con.read_parquet(uri.rstrip("/") + "/**/*.parquet", union_by_name=True)
        p = path_of(uri)
        low = p.lower()
        if os.path.isdir(p):
            return self._read_dir(con, p)
        if low.endswith((".csv", ".tsv")):
            return con.read_csv(p, **csv)
        if low.endswith((".json", ".ndjson")):
            return con.read_json(p)
        if low.endswith((".arrow", ".feather", ".ipc")):
            import pyarrow.feather as feather
            return con.from_arrow(feather.read_table(p))
        return con.read_parquet(p)

    def _read_dir(self, con: duckdb.DuckDBPyConnection, d: str) -> Relation:
        # cover every extension the append writer can emit (a dir of part-*.<ext>): parquet/pq, csv/tsv, json
        # parquet uses union_by_name so per-shard schema drift (an all-null column degrading to NULL type in
        # one worker-direct shard) reconciles by column name instead of failing on read order.
        for ext in (".parquet", ".pq"):
            if glob.glob(os.path.join(d, f"**/*{ext}"), recursive=True):
                return con.read_parquet(os.path.join(d, f"**/*{ext}"), union_by_name=True)
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

    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict:
        obj = is_object_uri(uri)
        if obj:
            db.ensure_object_store()  # load httpfs + credentials
        target = uri if obj else path_of(uri)  # object stores keep the full s3://… uri
        low = target.lower()
        rows = int(rel.aggregate("count(*)").fetchone()[0])
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
            self._reject_mixed_part_format(base, ext, obj)   # a dir of csv parts + a parquet append → read
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
                # Arrow-IPC has no DuckDB writer; go through pyarrow. On an object store, pyarrow.feather
                # given a raw "s3://…" string would write a LOCAL file of that name (silent corruption),
                # so open a real object stream via pyarrow's filesystem. open_output_stream finalizes its
                # multipart upload on close() even on an error/cancel — so write to a TEMP key and promote
                # with a server-side move only on success, leaving the prior object intact if the write
                # (or to_arrow_table, which materializes the whole table in RAM) fails partway.
                import pyarrow.feather as feather
                if obj:
                    fs, p = object_fs(target)
                    tmp = f"{p}.tmp-{uuid.uuid4().hex[:8]}"
                    try:
                        with fs.open_output_stream(tmp) as f:
                            feather.write_feather(rel.to_arrow_table(), f)
                        fs.move(tmp, p)  # server-side copy+delete; the destination is replaced only now
                    except BaseException:
                        import contextlib
                        with contextlib.suppress(Exception):
                            fs.delete_file(tmp)
                        raise
                else:
                    feather.write_feather(rel.to_arrow_table(), wtarget)
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

    _PART_FAMILY = {".pq": ".parquet", ".tsv": ".csv", ".ndjson": ".json"}  # read the same way

    def _existing_part_families(self, base: str, obj: bool) -> set:
        """The row-format families (parquet/csv/json) that already have COMMITTED parts under this
        dir/prefix — a `.tmp-*` part in flight is ignored (it doesn't match the *.<ext> suffix)."""
        fams: set = set()
        exts = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson")
        if obj:
            try:
                import pyarrow.fs as pafs
                fs, p = object_fs(base.rstrip("/") + "/")
                infos = fs.get_file_info(pafs.FileSelector(p, recursive=True, allow_not_found=True))
                names = [i.path for i in infos if i.type == pafs.FileType.File]
            except Exception:  # noqa: BLE001 — can't list → treat as empty (append proceeds)
                names = []
            for e in exts:
                if any(n.endswith(e) for n in names):
                    fams.add(self._PART_FAMILY.get(e, e))
        else:
            d = base.rstrip("/")
            for e in exts:
                if glob.glob(os.path.join(d, f"**/*{e}"), recursive=True):
                    fams.add(self._PART_FAMILY.get(e, e))
        return fams

    def _reject_mixed_part_format(self, base: str, ext: str, obj: bool) -> None:
        """_read_dir picks ONE format by precedence (parquet > csv > json), so mixing formats in one part
        dir silently DROPS the non-winning parts' data on read. Reject an append whose format differs from
        the parts already there — one format per output dataset."""
        want = self._PART_FAMILY.get(ext.lower(), ext.lower())
        others = self._existing_part_families(base, obj) - {want}
        if others:
            raise NotImplementedError(
                f"cannot append {ext} to a dataset that already holds {sorted(others)} parts — one format per output")

    def _migrate_singlefile_into_dir(self, target: str, base: str, ext: str, obj: bool) -> None:
        """overwrite writes a single FILE (name.parquet, registered uri=name.parquet); append writes into a
        DIRECTORY (name/, registered uri=name). Switching a write node overwrite→append repoints the catalog
        at the dir and would ORPHAN the prior single file (a sibling of the dir, not under its prefix). Fold
        that file in as the first part so no data is lost. No-op if there's no pre-existing single file."""
        part = f"part-migrated-{uuid.uuid4().hex[:12]}{ext}"
        if obj:
            try:
                import pyarrow.fs as pafs
                fs, src = object_fs(target)
                info = fs.get_file_info(src)
                if info.type != pafs.FileType.File:
                    return
                _, dst = object_fs(base.rstrip("/") + "/" + part)
                fs.move(src, dst)  # server-side; the single object becomes the first part of the dir
            except FileNotFoundError:
                return
            except Exception:  # noqa: BLE001 — pafs raises its own not-found types; nothing to migrate
                return
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

    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict:
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
