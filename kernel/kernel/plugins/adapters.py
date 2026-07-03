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
                    parts.append(f"{fp}:{st.st_size}:{int(st.st_mtime)}")
            return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
        st = os.stat(p)
        return hashlib.sha256(f"{p}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:16]
    except OSError:
        return "unknown"


class DuckDBAdapter:
    """Parquet / CSV / JSON / Arrow-Feather / directory, via DuckDB + PyArrow. Fully out-of-core."""

    name = "duckdb"
    _EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc")

    def matches(self, uri: str) -> bool:
        if uri.startswith("mem://"):
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
        return _fingerprint_path(path_of(uri))

    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict:
        p = path_of(uri)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        low = p.lower()
        if low.endswith((".csv", ".tsv")):
            rel.write_csv(p)
        elif low.endswith((".arrow", ".feather", ".ipc")):
            import pyarrow.feather as feather
            feather.write_feather(rel.to_arrow_table(), p)
        else:
            rel.write_parquet(p)
        return {"uri": uri, "rows": rel.aggregate("count(*)").fetchone()[0]}


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
        tbl = rel.to_arrow_table()
        lance.write_dataset(tbl, path_of(uri), mode="overwrite" if mode == "overwrite" else "append")
        return {"uri": uri, "rows": tbl.num_rows}


def default_adapters() -> list:
    return [LanceAdapter(), DuckDBAdapter()]
