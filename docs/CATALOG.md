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
- `catalog_edges` — `(parent, child, column, pipeline)` lineage edges
- `catalog_embeddings` — `(uri, model, dim, vec)` when an embedder is registered

A browse request is one filtered `SELECT … ORDER BY … LIMIT … OFFSET …` with `EXISTS` subqueries for
tags and columns. Facets are `GROUP BY` over the same filters. The folder tree aggregates distinct
folders. `get` and `resolve` are indexed lookups. Lineage expands breadth-first until `depth` or
`max_nodes`. Nothing on the read path is O(catalog size).

Key endpoints under `/api`:

- `GET /catalog/tables?q&folder&tags&owner&hasColumns&uris&sort&order&limit&offset` — page body plus
  `X-Total-Count` / `X-Has-More`
- `GET /catalog/facets?<same filters>` — folder / tag / owner values and counts, plus
  `semanticAvailable`
- `GET /catalog/tree?prefix=` — one level of the folder tree (`totalTables` / `truncated` when
  direct tables are truncated)
- `GET /catalog/search?q&mode=lexical|semantic|hybrid&limit`
- `GET /catalog/lineage?uri&depth&maxNodes` and `GET /catalog/edges?limit&offset`
- `PUT /catalog/tables/{id}/metadata` — set folder, tags, owner, description; only present fields
  change; explicit `null` clears owner or description
- `DELETE /catalog/tables/{id}` — unregister and drop its lineage edges, key, and relationships

Stress a large catalog with:

```bash
dataplay seed-catalog --count 5000
```

Open Tables afterward. `--remove` cleans the synthetic entries up.

## Semantic search

Semantic and hybrid search need a pluggable embedder. Core ships none. A plugin calls
`reg.add_embedder(fn, model)` with `fn(list[str]) -> list[list[float]]`. The catalog stores a vector
per dataset, ranks by cosine similarity, and fuses with lexical results (reciprocal-rank fusion) for
hybrid. With no embedder, search falls back to lexical. See `examples/plugins/dp_semantic_catalog` for
a local `sentence-transformers` implementation.

## External catalogs and lineage

Larger orgs often already have a central metadata service. Data Playground can sit in front of one
without forking core, because two seams line up on purpose.

The whole catalog is one swappable `CatalogProvider`. The built-in catalog is itself a first-party
plugin (`hub/plugins/default_catalog.py`) that calls `reg.set_catalog(InMemoryCatalog(...))` before
workspace plugins load. A later plugin replaces it with anything implementing the protocol in
`kernel/hub/backends.py`. The discovery surface — `list_page`, `facets`, `browse`, `search`,
`lineage`, `get_table`, `resolve_ref` — matches bounded remote metadata APIs, so a provider maps each
call onto paginated backend endpoints (`resolve_ref` runs on every preview and run). A read-only
external catalog can subclass the built-in provider and override only how rows are fetched.

Lineage edges are URI-keyed: `{parent_uri, child_uri, column, pipeline}`. That identity maps cleanly
onto OpenLineage-style stores. A bridge plugin can sync via paginated `GET /catalog/edges` or push on
`register_output`, and pull external edges back through the same provider seam. Core does not emit the
OpenLineage event envelope; that belongs in the bridge.

Organization fields are generic on purpose — `folder` (delimiter-joined namespace path), `tags`,
`owner`, `description` — so they round-trip to common catalog models. A bridge to a specific vendor is
a plugin on `CatalogProvider` and `add_embedder`, not a core patch.
