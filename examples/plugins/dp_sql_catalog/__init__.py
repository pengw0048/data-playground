"""Reference plugin — a dataset **catalog backed by a SQL table**.

A common integration: a team already has a `datasets(name, uri)` table (in Postgres, MySQL, SQLite, …).
This plugin surfaces those rows as Data Playground datasets, so `source` nodes can pick them by name and
previews/runs read the `uri` through whatever adapter matches it.

It demonstrates the `CatalogProvider` seam (`reg.set_catalog`) via the **read-external catalog** pattern
the SPI documents: subclass the built-in `InMemoryCatalog`, OVERRIDE only the reads (`list_tables` /
`get_table`) to sync from SQL, and INHERIT everything else — `resolve_ref`, `lineage`, `relationships`,
declared keys, and `register_output` — from the parent's local side-store. So an external catalog needs
no reimplementation of the lineage/ER/write machinery; it only maps its rows to `CatalogTable`s (columns
+ row count are probed from the `uri` by the inherited adapter path).

Config (env): `DP_SQL_CATALOG_URL` = any SQLAlchemy URL (e.g. `postgresql+psycopg://…`, `sqlite:///…`);
`DP_SQL_CATALOG_TABLE` = the table name (default `datasets`, columns `name`, `uri`). Unset → this plugin
is a no-op and the default local catalog stands.

Drop this folder into `<workspace>/plugins/` (or install it as a `dataplay.plugins` entry point).
Requires SQLAlchemy, which the kernel already depends on — no extra install for SQLite/bundled drivers.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from hub.plugins.catalog import InMemoryCatalog


class SqlCatalog(InMemoryCatalog):
    """A CatalogProvider whose datasets come from a SQL `(name, uri)` table. Reads are synced from SQL;
    lineage / relationships / declared-keys / register_output are inherited (local side-store)."""

    name = "sql-catalog"

    def __init__(self, data_dir: str, resolve_adapter, db_url: str, table: str = "datasets"):
        self._engine = create_engine(db_url)
        self._table = table
        super().__init__(data_dir, resolve_adapter)  # __init__ calls _seed() → our SQL sync

    def _rows(self) -> list[tuple[str, str]]:
        with self._engine.connect() as c:
            return [(r[0], r[1]) for r in c.execute(text(f"SELECT name, uri FROM {self._table}"))]

    def _sync(self) -> None:
        """Register any SQL row not yet in the catalog (probes the uri for columns/row-count via the
        inherited adapter path). Cheap + idempotent: known names are skipped."""
        for name, uri in self._rows():
            try:
                super().get_table(name)
            except KeyError:
                try:
                    self.register_output(name=name, uri=uri, version="v1", parents=[])
                except Exception:  # noqa: BLE001 — a bad/unreadable row shouldn't break the catalog
                    pass

    def _seed(self) -> None:
        super()._seed()  # keep local data_dir discovery too (harmless if the dir is empty)
        self._sync()

    def list_tables(self, q=None):  # override: sync from SQL first, then serve from the cache
        self._sync()
        return super().list_tables(q)

    def get_table(self, id_or_name):  # override: sync then delegate (inherits the KeyError-on-miss contract)
        self._sync()
        return super().get_table(id_or_name)


def register(reg) -> None:
    url = os.environ.get("DP_SQL_CATALOG_URL")
    if not url:
        return  # not configured → leave the default InMemoryCatalog in place
    table = os.environ.get("DP_SQL_CATALOG_TABLE", "datasets")
    reg.set_catalog(SqlCatalog(reg.deps.data_dir, reg.deps.resolve_adapter, url, table))
