"""The default dataset catalog — shipped in-core, but registered as the FIRST plugin.

`register(reg)` installs the built-in DB-backed `InMemoryCatalog` through the very same
`reg.set_catalog(...)` seam a third-party catalog would use. `Deps` runs this bundled registration
BEFORE any workspace / entry-point plugin, so:

- the built-in catalog is NOT a privileged core instantiation — it is the first implementation
  through the public `CatalogProvider` seam (the same status the docs claim for the built-in
  adapters), which is exactly what makes swapping in an external catalog a first-class operation; and
- because it loads first, a plugin loaded afterwards can still REPLACE it with another `set_catalog`
  (a remote metadata service, a SQL-backed catalog, …) — the default simply wins when nothing else
  registers one, so "clone it and it works" is unchanged.

There is intentionally no config here: the default catalog needs none. It's the reference for how
a catalog provider plugs in.
"""

from __future__ import annotations

from hub.plugins.catalog import InMemoryCatalog


def register(reg) -> None:
    reg.set_catalog(InMemoryCatalog(reg.deps.data_dir, reg.deps.resolve_adapter))
