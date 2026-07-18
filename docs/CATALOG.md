# The data catalog

The catalog is the index of every dataset a `source` node can bind: schema, row count, keys, lineage,
and organization (folder, tags, owner, description).

Reads are server-side and bounded. Browse, search, facets, the folder tree, and lineage each query the
metadata database with a page or depth limit. No view loads the whole catalog into memory.

## What you can do

Browse a paginated, virtualized list. Filter by folder, tag, owner, or “has column X” (click a column
in the detail drawer). Sort by name, size, recency, or most-used.

Search across name, folder, description, tags, and column names. Lexical search requires every
whitespace token to match somewhere (`curated images` finds `demo/images/curated`; `%` and `_` are
literal). With an embedder plugin installed, the search box also offers meaning (semantic) and hybrid
modes.

Organize datasets from the detail drawer: folder path (`prod/images/curated`), tags, owner, and
description. The facet rail shows tag and owner counts for the current filters. Lineage follows
upstream and downstream datasets recorded when runs write outputs; the graph is depth- and node-capped.
Declare primary keys and join relationships, then view them as an ER diagram scoped to a folder.

## How it scales

The metadata database (SQLite locally, Postgres in a shared deploy) is the source of truth.
Filterable fields are promoted from the stored JSON document into indexed columns, with join tables for
multi-valued fields:

- `catalog_entries` — one row per dataset (`uri` PK, name, folder, owner, description, row count,
  usage, `tbl_id`, and the full `CatalogTable` as `doc`)
- `catalog_tags` — `(uri, tag)` for tag filter and facets
- `catalog_columns` — `(uri, column)` for “has column X” and column search
- `catalog_lineage_facts` — immutable, idempotent source-to-destination publication facts with the
  source and destination projections resolved on first commit (including versions when available), a
  shared `publication_key` (`publicationKey` on the wire), run/attempt/producer identity, and bounded
  field mappings
- `catalog_publication_events` — durable output, lineage-publication, and usage receipts; lineage
  receipts remain after unregister as retry tombstones
- `catalog_embeddings` — `(uri, model, dim, vec)` when an embedder is registered

A browse request is one filtered `SELECT … ORDER BY … LIMIT … OFFSET …` with `EXISTS` subqueries for
tags and columns. Facets are `GROUP BY` over the same filters. The folder tree aggregates distinct
folders. `get` and `resolve` are indexed lookups. Lineage expands breadth-first until `depth` or
`max_nodes`. Nothing on the read path is O(catalog size).

Key endpoints under `/api`:

- `GET /catalog/tables?q&folder&tags&owner&hasColumns&uris&sort&order&limit&offset` —
  `{items, total, offset, limit, hasMore}` page body
- `GET /catalog/facets?<same filters>` — folder / tag / owner values and counts, plus
  `semanticAvailable`
- `GET /catalog/tree?prefix=` — one level of the folder tree (`totalTables` / `truncated` when
  direct tables are truncated)
- `GET /catalog/search?q&mode=lexical|semantic|hybrid&limit`
- `GET /catalog/lineage?uri&depth&maxNodes` — bounded graph projection; `rootUri` is the canonical
  current identity used by every returned node/edge, and each edge includes the number of durable
  facts it summarizes
- `GET /catalog/lineage/facts?limit&afterId` — bounded, keyset-paginated fact snapshots. IDs and
  cursors are decimal strings on the wire so JavaScript does not lose 64-bit precision;
  `publicationKey` groups every source fact created by one output publication. The route rejects a
  provider page that exceeds `limit`, returns IDs at or before `afterId`, is not strictly increasing,
  or advertises a continuation that does not advance to the last returned ID.
- `GET /catalog/tables/{id}/revisions?limit&cursor` — bounded newest-first native revision history
  for adapters that support it. `datasetId`, `revisionId`, and the cursor are opaque; `retentionOwner`
  distinguishes provider-native history from core-retained managed files.
- `GET /catalog/tables/{id}/revisions/resolve?asOf=` — resolves latest or an RFC 3339 instant to one
  immutable native version. `GET /catalog/revisions/{datasetId}/{revisionId}` returns bounded schema,
  row/file/byte facts, a retained parent when known, and a fixed 100-row preview from that exact
  version. Producer operation is `null` when the provider does not record it. Missing, compacted,
  unregistered, or unsupported revisions return a stable unavailable result and never fall back to
  current head.
- `POST /data/sample` — bounded dataset rows plus explicit `completeness`, `rowLimit`,
  `limitReason`, and `limitScope` metadata. `limitScope=result-window` identifies the 2,000-row
  interactive artifact window; graph previews use `each-source` instead because their source budget
  is not a promise about which output rows a transform produces. Neither path falls back to an
  unbounded count or scan just to fill in an unknown total. `hasMore` is tri-state: `true` and `false`
  are proven within that interactive scope, while `null` means the adapter cannot establish whether a
  next page exists without doing unbounded work.
- `PUT /catalog/tables/{id}/metadata` — set folder, tags, owner, description; only present fields
  change; explicit `null` clears owner or description
- `DELETE /catalog/tables/{id}` — unregister and drop every touching lineage fact, declared key, and
  relationship. Re-registering the same URI does not inherit the removed dataset's evidence. Durable
  publication receipts remain as retry tombstones, so an old request cannot resurrect deleted facts.

Stress a large catalog with:

```bash
dataplay seed-catalog --count 5000
```

Open **Workspace** afterward. `--remove` cleans the synthetic entries up.

## Semantic search

Semantic and hybrid search need a pluggable embedder. Core ships none. A plugin calls
`reg.add_embedder(fn, model)` with `fn(list[str]) -> list[list[float]]`. The catalog stores a vector
per dataset, ranks by cosine similarity, and fuses with lexical results (reciprocal-rank fusion) for
hybrid. With no embedder, search falls back to lexical. See `examples/plugins/dp_semantic_catalog` for
a local `sentence-transformers` implementation.

## Lineage evidence and external catalogs

Larger orgs often already have a central metadata service. Data Playground can sit in front of one
without forking core, because two seams line up on purpose.

The catalog API is served by one selected `CatalogProvider`. The built-in catalog is itself a first-party
plugin (`hub/plugins/default_catalog.py`) that calls `reg.set_catalog(InMemoryCatalog(...))` before
workspace plugins load. A later plugin replaces it with anything implementing the protocol in
`kernel/hub/backends.py`. The discovery surface — `list_page`, `facets`, `browse`, `search`,
`lineage`, `get_table`, `resolve_ref` — matches bounded remote metadata APIs, so a provider maps each
call onto paginated backend endpoints (`resolve_ref` runs on every preview and run). A read-only
external catalog can subclass the built-in provider, but it must deliberately choose whether lineage
stays in the core metadata side store or moves to the external authority.

The effective catalog is selected during startup before catalog-dependent runners are constructed.
`reg.add_runner_factory(factory)` is the public seam for a runner that needs the selected catalog or
the local runner; its factory receives the finished dependency set. `reg.set_catalog(...)` is therefore
startup composition, not a live catalog hot-swap. A fully replacing provider must implement the
write-back and lineage authority required by the execution paths it enables; inheriting
`InMemoryCatalog` remains a practical way to retain the built-in behavior. The composition order is
covered by the installed-plugin and kernel contract tests.

The built-in catalog stores one immutable fact per source involved in an output publication. A fact
keeps the source and destination catalog projections resolved by the first commit, including their
URIs, stable keys, and versions when available, plus run, attempt, producer, step, provenance, and field
mappings when supplied. All facts from that publication share a `publicationKey` derived from the
caller's `LineagePublication.idempotency_key`
(`idempotencyKey` on the wire); the raw caller key is not copied into the export.

Idempotency reserves the complete publication, not each fact independently. The durable receipt binds
the key to the canonical caller request: the complete raw parent-token set (canonicalized before, and
without, resolving it against mutable catalog entries), the caller's explicit destination URI and
version, and the canonical lineage identity (run/attempt/producer/step, provenance, and mappings). The
facts created by the first apply separately preserve the source and destination projections resolved at
that commit, with versions when available. An exact replay inserts nothing and does not re-resolve or
restore a later catalog projection, even if that projection has changed or been unregistered. Reusing the
key with any changed request fails the whole transaction, including a retry with only a subset of the
original sources. An empty source set is still a complete reserved publication. Unregister removes facts
but retains the receipt as a tombstone, so an old retry cannot attach to or recreate a new registration;
a genuinely new publication after re-registration needs a new idempotency key.

`LineagePublication.field_mappings` (`fieldMappings` on the wire) currently identifies only source and
destination column names; it has no source-dataset field. Non-empty mappings are therefore accepted only
for a publication with exactly one source. A multi-source publication with mappings fails closed instead
of copying or guessing which source owns a mapping. Multi-source publications without mappings still
create one fact per distinct source.

`GET /catalog/lineage` aggregates these facts into a compact graph. Its `rootUri` resolves a managed
logical URI, catalog key, or other provider alias to the same current identity used by the returned
nodes and edges, so clients never have to infer which node represents the requested root. Meanwhile,
`GET /catalog/lineage/facts` exports the evidence without collapsing distinct runs. Fact export is an
optional `CatalogLineageFactExporter` capability of the selected provider. The route delegates to that
provider only; it returns `501` when the capability is absent and never falls back to the built-in
metadata side store. Each response is a bounded snapshot of the facts visible for that page; the API is
not CDC and does not promise one transactionally frozen snapshot across a multi-page scan. Unregister
deletes touching facts, and the export has no deletion events or tombstones. An external mirror must
therefore periodically perform a full scan from `afterId=0` and reconcile its complete local view rather
than treating an ever-increasing cursor as a permanent sync log.

`CatalogLineageRecorder` is a separate optional capability for attaching the current run's facts to an
already-published exact catalog output during a cache reuse. A catalog cache candidate is reusable only
when its `RunOutput.version` is present and the runner-bound publication authority reads back the same
URI, name, and version; otherwise the runner recomputes.

The lineage graph, fact export, and cache-reuse recorder exposed by one provider must use one intentional
lineage authority. An `InMemoryCatalog` subclass may use external discovery rows while deliberately
retaining all three inherited core-metadata lineage surfaces as a side store. If it moves any lineage
surface to an external store, it must override the other inherited lineage methods too (or omit the
optional ones). A fully external provider that omits fact export gets the explicit `501` behavior above
rather than local facts.

Those generic facts map cleanly onto OpenLineage-style stores. An export bridge can periodically
reconcile the keyset pages. A publication bridge can forward the `LineagePublication` passed to
`register_output` only when it is part of the runner-bound publication authority; selecting it later as
the route provider does not make runners call it. Core does not emit a vendor or OpenLineage event
envelope; that belongs in the bridge.

Organization fields are generic on purpose — `folder` (delimiter-joined namespace path), `tags`,
`owner`, `description` — so they round-trip to common catalog models. A bridge to a specific vendor is
a plugin on `CatalogProvider` and `add_embedder`, not a core patch; execution write-back remains subject
to the runner-binding limitation above.
