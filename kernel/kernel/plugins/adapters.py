"""Dataset adapters — what a `dataset` can be (PRD §8.2).

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

from kernel import db
from kernel.models import ColumnSchema
from kernel.plugins.capabilities import tag_columns

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
    if t.startswith(("STRUCT", "MAP")):
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
             predicate: str | None = None, limit: int | None = None) -> Relation:
        con = db.conn()
        rel = self._read(con, uri)
        if columns:
            rel = rel.project(", ".join(f'"{c}"' for c in columns))
        if predicate:
            rel = rel.filter(predicate)
        if limit is not None:
            rel = rel.limit(int(limit))
        return rel

    def _read(self, con: duckdb.DuckDBPyConnection, uri: str) -> Relation:
        if uri.startswith("mem://"):
            return con.table(uri[len("mem://"):])
        if is_object_uri(uri):
            db.ensure_object_store()  # load httpfs + credentials
            low = uri.lower()
            if low.endswith((".csv", ".tsv")):
                return con.read_csv(uri)
            if low.endswith((".json", ".ndjson")):
                return con.read_json(uri)
            if low.endswith((".parquet", ".pq")):
                return con.read_parquet(uri)
            return con.read_parquet(uri.rstrip("/") + "/**/*.parquet")  # a prefix of parts (append output)
        p = path_of(uri)
        low = p.lower()
        if os.path.isdir(p):
            return self._read_dir(con, p)
        if low.endswith((".csv", ".tsv")):
            return con.read_csv(p)
        if low.endswith((".json", ".ndjson")):
            return con.read_json(p)
        if low.endswith((".arrow", ".feather", ".ipc")):
            import pyarrow.feather as feather
            return con.from_arrow(feather.read_table(p))
        return con.read_parquet(p)

    def _read_dir(self, con: duckdb.DuckDBPyConnection, d: str) -> Relation:
        for ext, reader in ((".parquet", con.read_parquet), (".csv", con.read_csv), (".json", con.read_json)):
            if glob.glob(os.path.join(d, f"**/*{ext}"), recursive=True):
                return reader(os.path.join(d, f"**/*{ext}"))
        raise ValueError(f"no parquet/csv/json files under {d}")

    def schema(self, uri: str) -> list[ColumnSchema]:
        return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
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
            # append = a DIRECTORY / prefix of part files (out-of-core; the reader reads them all
            # back). Only for row formats — parquet/csv; feather/arrow have no directory-scan reader.
            if not low.endswith((".parquet", ".pq", ".csv", ".tsv")):
                raise NotImplementedError(f"append is only supported for parquet/csv outputs, not {os.path.splitext(target)[1] or 'this'}")
            base, ext = os.path.splitext(target)  # name.parquet -> prefix "name", ext ".parquet"
            part_name = f"part-{uuid.uuid4().hex[:12]}{ext}"
            if obj:
                part = base.rstrip("/") + "/" + part_name
            else:
                os.makedirs(base, exist_ok=True)
                part = os.path.join(base, part_name)
            (rel.write_csv if ext.lower() in (".csv", ".tsv") else rel.write_parquet)(part)
            return {"uri": base, "rows": rows}
        if mode not in ("overwrite", None):
            raise NotImplementedError(f"write mode '{mode}' is not supported — use overwrite or append")
        if not obj:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if low.endswith((".csv", ".tsv")):
            rel.write_csv(target)
        elif low.endswith((".arrow", ".feather", ".ipc")):
            import pyarrow.feather as feather
            feather.write_feather(rel.to_arrow_table(), target)
        else:
            rel.write_parquet(target)
        return {"uri": uri, "rows": rows}


class LanceAdapter:
    """Lance is open source, so it is a CORE adapter (PRD Appendix A note). pylance loaded lazily.

    NOTE: a full scan (limit=None) currently materializes the dataset via ds.to_table() before
    handing it to DuckDB — unlike the Parquet path it is not yet streaming. Column/limit pushdown
    (preview, vector-search) is lazy. Streaming full scans are a follow-up.
    """

    name = "lance"

    def matches(self, uri: str) -> bool:
        return path_of(uri).lower().rstrip("/").endswith(".lance")

    def _dataset(self, uri: str):
        import lance  # lazy — only if the optional `lance` extra is installed
        return lance.dataset(path_of(uri))

    def scan(self, uri: str, columns: list[str] | None = None,
             predicate: str | None = None, limit: int | None = None) -> Relation:
        ds = self._dataset(uri)
        tbl = ds.to_table(columns=columns, limit=limit)  # lazy column/limit pushdown
        rel = db.conn().from_arrow(tbl)
        if predicate:
            rel = rel.filter(predicate)
        return rel

    def schema(self, uri: str) -> list[ColumnSchema]:
        return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
            return int(self._dataset(uri).count_rows())
        except Exception:  # noqa: BLE001
            return None

    def fingerprint(self, uri: str) -> str:
        try:
            return f"lance-v{self._dataset(uri).version}"
        except Exception:  # noqa: BLE001
            return _fingerprint_path(path_of(uri))

    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict:
        import lance
        rows = int(rel.aggregate("count(*)").fetchone()[0])
        # stream RecordBatches into Lance (bounded memory) instead of materializing the whole table
        reader = rel.record_batch(1 << 16)
        lance.write_dataset(reader, path_of(uri), mode="overwrite" if mode == "overwrite" else "append")
        return {"uri": uri, "rows": rows}


def default_adapters() -> list:
    return [LanceAdapter(), DuckDBAdapter()]
