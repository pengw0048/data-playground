"""Reference plugin — an **Apache Iceberg** table source adapter (lakehouse).

Point a `source` at `iceberg://<catalog>/<namespace>.<table>` (e.g. `iceberg://prod/sales.orders`) and the
Iceberg table is read via `pyiceberg` → Arrow → DuckDB, flowing through the graph like any dataset. The
catalog named in the uri is resolved by pyiceberg from its usual config (`~/.pyiceberg.yaml` or
`PYICEBERG_CATALOG__*` env vars), so credentials/warehouse live in your pyiceberg config, not here.

It demonstrates the `DatasetAdapter` seam (`reg.add_adapter`) for a warehouse-style source: `matches`
claims `iceberg://`, `scan` returns a lazy DuckDB relation (column/limit applied in DuckDB — a fuller
version would push `row_filter`/`selected_fields` into pyiceberg's scan), `schema`/`count`/`fingerprint`
round it out, `write` raises (read-only). `pyiceberg` is imported lazily, so the plugin loads without it.

Install: `uv pip install -e 'kernel[iceberg]'`. Verify against your own Iceberg catalog/warehouse —
the shipped test exercises the adapter's logic against a stand-in, not a live catalog.
"""

from __future__ import annotations

import hashlib

from hub import db
from hub.plugins.adapters import Relation, relation_columns


def _parse(uri: str) -> tuple[str, str]:
    """iceberg://<catalog>/<identifier> → (catalog, identifier). The identifier is pyiceberg's
    dotted `namespace.table` (kept whole; only the FIRST '/' separates the catalog name)."""
    rest = uri[len("iceberg://"):]
    catalog, _, identifier = rest.partition("/")
    return catalog, identifier


def _to_arrow(uri: str):
    try:
        from pyiceberg.catalog import load_catalog  # lazy — only when an iceberg:// uri is used
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError("Iceberg support is not installed — run: uv pip install -e 'kernel[iceberg]'") from e
    catalog, identifier = _parse(uri)
    table = load_catalog(catalog).load_table(identifier)
    return table.scan().to_arrow()


class IcebergAdapter:
    name = "iceberg"

    def matches(self, uri: str) -> bool:
        return uri.startswith("iceberg://")

    def scan(self, uri: str, columns: list[str] | None = None, predicate: str | None = None,
             limit: int | None = None, options: dict | None = None) -> Relation:
        rel = db.conn().from_arrow(_to_arrow(uri))
        if columns:
            rel = rel.project(", ".join(f'"{c}"' for c in columns))
        if predicate:
            rel = rel.filter(predicate)
        if limit is not None:
            rel = rel.limit(int(limit))
        return rel

    def schema(self, uri: str):
        with db.base_guard():
            return relation_columns(self.scan(uri, limit=0))

    def count(self, uri: str) -> int | None:
        try:
            with db.base_guard():
                return int(self.scan(uri).aggregate("count(*) AS n").fetchone()[0])
        except Exception:  # noqa: BLE001
            return None

    def fingerprint(self, uri: str) -> str:
        return "iceberg:" + hashlib.sha256(uri.encode()).hexdigest()[:12]  # snapshot-agnostic; key by uri

    def write(self, uri: str, rel: Relation, mode: str = "overwrite") -> dict:
        raise NotImplementedError("iceberg:// tables are read-only here; write to a local/object-store uri instead")


def register(reg) -> None:
    reg.add_adapter(IcebergAdapter())  # claims iceberg:// — safe to register even without pyiceberg (lazy)
