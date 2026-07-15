"""Reference plugin — a dataset **catalog backed by a SQL table**.

A common integration: a team already has a `datasets(name, uri)` table (in Postgres, MySQL, SQLite, …).
This plugin surfaces those rows as Data Playground datasets, so `source` nodes can pick them by name and
previews/runs read the `uri` through whatever adapter matches it.

It demonstrates the `CatalogProvider` seam (`reg.set_catalog`) via the **read-external catalog** pattern
the SPI documents: subclass the built-in `InMemoryCatalog`, OVERRIDE the bounded read surfaces to sync
from SQL, and INHERIT everything else — `resolve_ref`, `lineage`, `relationships`, declared keys, and
`register_output` — from the parent's local side-store. So an external catalog needs no reimplementation
of the lineage/ER/write machinery; it only maps its rows to `CatalogTable`s (columns + row count are
probed from the `uri` by the inherited adapter path).

Config: `url` = any SQLAlchemy URL (e.g. `postgresql+psycopg://…`, `sqlite:///…`); `table` = the table
name (default `datasets`, columns `name`, `uri`). Both are declared in `dataplay.toml [[config]]`, so
they're editable in Settings → Plugins AND fall back to the `DP_SQL_CATALOG_URL` / `DP_SQL_CATALOG_TABLE`
env vars (headless). Unset → this plugin is a no-op and the default local catalog stands.

Drop this folder into `<workspace>/plugins/` (or install it as a `dataplay.plugins` entry point).
Requires SQLAlchemy, which the kernel already depends on — no extra install for SQLite/bundled drivers.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from hub.plugins.catalog import InMemoryCatalog


class SqlCatalog(InMemoryCatalog):
    """A CatalogProvider whose datasets come from a SQL `(name, uri)` table. Reads are synced from SQL;
    lineage / relationships / declared-keys / register_output are inherited (local side-store)."""

    name = "sql-catalog"

    def __init__(self, data_dir: str, resolve_adapter, db_url: str, table: str = "datasets"):
        self._engine = create_engine(db_url)
        self._table = table
        self._last_sync = 0.0  # throttle: browse fires list_page + facets per keystroke
        super().__init__(data_dir, resolve_adapter)  # __init__ calls _seed() → our SQL sync

    def _rows(self) -> list[tuple[str, str]]:
        with self._engine.connect() as c:
            return [(r[0], r[1]) for r in c.execute(text(f"SELECT name, uri FROM {self._table}"))]

    def _sync(self) -> None:
        """Register any SQL row not yet in the catalog (probes the uri for columns/row-count via the
        inherited adapter path). Cheap + idempotent: known names are skipped, and back-to-back reads
        within a couple of seconds reuse the last sync (a browse keystroke = list + facets)."""
        import time
        now = time.monotonic()
        if now - self._last_sync < 2.0:
            return
        self._last_sync = now
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

    def get_table(self, id_or_name):  # inherits the KeyError-on-miss contract
        self._sync()
        return super().get_table(id_or_name)

    def list_page(self, query):
        self._sync()
        return super().list_page(query)

    def facets(self, query):
        self._sync()
        return super().facets(query)

    def browse(self, prefix=""):
        self._sync()
        return super().browse(prefix)

    def search(self, q, mode="hybrid", limit=50, *, query=None):
        self._sync()
        return super().search(q, mode=mode, limit=limit, query=query)


def register(reg) -> None:
    # reg.config reads the dataplay.toml [[config]] fields: a UI-set value (Settings → Plugins) wins,
    # else the declared env var (DP_SQL_CATALOG_URL / _TABLE), else the default. So it's configurable
    # from the UI AND still works headless via env.
    url = reg.config("url")
    if not url:
        return  # not configured → leave the default InMemoryCatalog in place
    table = reg.config("table", "datasets")
    reg.set_catalog(SqlCatalog(reg.deps.data_dir, reg.deps.resolve_adapter, url, table))
