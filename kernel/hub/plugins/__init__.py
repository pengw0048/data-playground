"""Plugin SPI — where the extensibility contract actually lives.

The kernel core is extended by `register(reg)` packs (see hub.deps.Registry). Rather than a
single aspirational "contract" file (the old plugins/base.py, which drifted out of sync with the
real code and was imported nowhere), each contract lives next to the code that USES it:

- Dataset adapters — `DuckDBAdapter` / `LanceAdapter` in `hub.plugins.adapters`. An adapter
  implements: `name`; `matches(uri) -> bool`; `scan(uri, columns=None, ..., limit=None, options=None)
  -> duckdb.DuckDBPyRelation`; `schema(uri) -> list[ColumnSchema]`; `count(uri) -> int | None`;
  `fingerprint(uri) -> str`; `write(uri, rel, mode="overwrite") -> dict`. (Lance adds an optional
  `nearest(...)` for native ANN.) Register via `reg.add_adapter(...)`.
- Execution backends — the `ExecutionBackend` Protocol in `hub.backends` (runtime-checkable).
  `LocalRunner` / `SubprocessRunner` implement it. Register via `reg.add_runner(...)`.
- Node kinds + their build — `NodeSpec` in `hub.nodespecs`; the build callable is the
  `NodeBuilder` Protocol in `hub.backends`. Register via `reg.add_node(spec, build)`.
- Capabilities — an `id` + `label` object (see `hub.plugins.capabilities`); register via
  `reg.add_capability(...)`. Column-tag DETECTION lives in `capabilities.tag_columns`; per-capability
  viewer UI is registered on the frontend (`web/src/nodes/capabilities.tsx`).
- Processors / catalog / pipeline importer — see `hub.plugins.{processors,catalog,importer}`.

Plugins declare a `dataplay.toml` manifest (name/version + an optional `min_core_api`) and are
version-negotiated against `hub.deps.CORE_API_VERSION` at load.
"""
