# Catalog and Workspace

Use **Workspace** to find data before making a Canvas. It combines locally placed datasets and
Canvases with the catalog capabilities supplied by the connected provider. The catalog records the
metadata that makes a dataset useful in a research workflow: schema and basic profile facts, revision
availability, organization, relationships, and lineage from published outputs.

This first part is a researcher guide. The [reference section](#reference-built-in-catalog-api-and-provider-boundaries)
below preserves the API, database, and provider contracts for operators and extension authors.

## Find a dataset

Open **Workspace**. **All Workspace** shows datasets, Canvases, and folders together; **Datasets**
narrows the list to data. Search matches dataset and Workspace names, then open an item to inspect it.
The built-in catalog serves browse, search, facets, folder tree, and lineage requests in bounded pages
or graphs, so opening a Workspace view does not require loading an entire catalog into the browser.

With the built-in catalog, search covers names, folders, descriptions, tags, and column names. Lexical
search requires every whitespace token to match somewhere (`curated images` finds
`demo/images/curated`; `%` and `_` are literal). An installed embedder can additionally offer semantic
and hybrid search; without one, search remains lexical.

## Read context before adding data to a Canvas

Open a dataset from Workspace. Its detail panel shows its registered name and location, row and column
counts (the basic profile), columns, and current version information when the adapter has it. Use
**Preview** to check rows and schema, but read the status alongside the table: it states the bound,
completeness, and input revision where available. A bounded prefix is useful for checking shape; it is
not automatically a representative sample or a whole-dataset result.

The same detail panel is where you keep organization metadata close to the data: name, folder, tags,
owner, and description. Those fields are generic rather than a claim that every connected catalog uses
the same taxonomy.

## Understand relationships and lineage

Dataset details list upstream parents and downstream children recorded for published outputs. **View
relationship graph** opens the bounded graph around the selected dataset. When your provider supports
declared keys and relationships, use the relationship view to inspect the ER diagram in its current
folder scope.

Lineage answers a different question from folder placement: it records which source datasets
contributed to a publication. In the [Workspace-to-Canvas tour](TUTORIAL.md), running the example
creates `top_users`; opening it shows `events` as a parent. The graph is deliberately depth- and
node-capped, so use it to orient an investigation rather than assuming it is an unbounded history
export.

## Inspect an exact revision when it exists

Some adapters expose immutable revision history. For those datasets, the detail panel offers
**Revision history**; choose **Open revision** to inspect that exact retained version. If history is
unavailable, Data Playground says so and does not substitute the current head for a missing revision.

Managed local outputs expose a retained core revision after a successful write. Open the result from
Workspace, Jobs, or the write receipt, then use its revision entry when you need to verify the published
state. See [Versioned data and durable execution](VERSIONED_DATA_AND_DURABLE_EXECUTION.md) for what
admitted inputs, managed revisions, and receipts guarantee.

## Add the exact dataset you chose

Click **Use** in a dataset detail. Choose **Explore in a new Canvas** to create an editable Canvas in
the current Workspace, **Add to this Canvas** when there is an editable current Canvas, or **Choose a
Canvas** to select an exact destination. The selected sources are applied atomically under one Canvas
version precondition; opening a dataset never silently changes an unrelated graph.

For the complete path from discovery through a managed write and back to Job, Inbox, revision, and
lineage evidence, follow the [Workspace-to-Canvas tour](TUTORIAL.md).

## Reference: built-in catalog, API, and provider boundaries

The rest of this page is reference material. It describes the built-in metadata store, bounded HTTP
API, and extension boundaries; it is not a second Workspace tutorial.

## Built-in metadata store and bounded API

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

### Semantic search

Semantic and hybrid search need a pluggable embedder. Core ships none. A plugin calls
`reg.add_embedder(fn, model)` with `fn(list[str]) -> list[list[float]]`. The catalog stores a vector
per dataset, ranks by cosine similarity, and fuses with lexical results (reciprocal-rank fusion) for
hybrid. With no embedder, search falls back to lexical. See `examples/plugins/dp_semantic_catalog` for
a local `sentence-transformers` implementation.

### Lineage evidence and external catalogs

Larger orgs often already have a central metadata service. Data Playground offers two deliberately
separate integration models, so reading an external catalog never silently grants permission to change
it.

- A [`ReadOnlyCatalogProvider` Workspace mount](PLUGINS.md#read-only-external-catalog-mounts) adds one
  bounded external source to the mixed Workspace browse surface. Multiple mounts can coexist with local
  data and with each other. They are discovery-only: they do not write, curate, or publish lineage into
  their source system. A run using a mounted source continues to publish through its separately selected
  managed destination and execution backend.
- `reg.set_catalog(CatalogProvider)` replaces the one application-wide catalog. The selected provider
  serves both discovery and the write-back/curation contract used by enabled execution paths. A provider
  may fetch discovery rows from an external service while inheriting `InMemoryCatalog`'s core-managed
  publication and lineage behavior; that preserves local output handling and does not write to the
  external service. Provider-native write-back is a deliberate implementation responsibility, not a
  capability inferred from external discovery.

The built-in catalog is itself a first-party plugin (`hub/plugins/default_catalog.py`) that calls
`reg.set_catalog(InMemoryCatalog(...))` before workspace plugins load. A later plugin can replace it
with an implementation of the protocol in `kernel/hub/backends.py`. The discovery surface —
`list_page`, `facets`, `browse`, `search`, `lineage`, `get_table`, `resolve_ref` — maps onto bounded
remote metadata APIs (`resolve_ref` runs on every preview and run).

The effective application-wide catalog is selected during startup before catalog-dependent runners are
constructed. `reg.add_runner_factory(factory)` is the public seam for a runner that needs the selected
catalog or the local runner; its factory receives the finished dependency set. `reg.set_catalog(...)` is
therefore startup composition, not a live catalog hot-swap. A fully replacing provider must implement the
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
