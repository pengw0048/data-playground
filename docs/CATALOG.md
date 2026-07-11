# The data catalog

Data Playground's catalog is the index of every dataset you can point a `source` node at: its schema,
row count, keys, lineage, and organization (folder / tags / owner / description). It is built to stay
fast and navigable whether you have ten tables or tens of thousands.

The one idea everything follows from: **the catalog is browsed server-side.** No view ever loads the
whole catalog into memory. Every read — browse, search, facet, folder tree, lineage — is a bounded,
indexed query against the metadata DB, and the payload is capped by a page limit, not by how big the
catalog is.

## What you can do

- **Browse** a paginated, virtualized list — filter by folder, tag, owner, or "has column X"; sort by
  name, size, recency, or **most-used**.
- **Search** across name / folder / description / column names (lexical), or by meaning
  (**semantic**, when an embedder plugin is installed), or both (**hybrid**).
- **Organize** into a folder hierarchy (a namespace path like `prod/images/curated`), tag datasets,
  assign an owner, and write a description — all from a dataset's detail drawer.
- **Facet** — the right-hand rail shows each tag/owner with a live count, scoped to the current
  filters, so a click narrows the set.
- **Trace lineage** — a dataset's upstream/downstream datasets, recorded as runs write outputs. The
  graph is depth- and node-capped so even a densely-connected component renders fast.
- **Relate** — declare primary keys and join relationships; view them as an ER diagram (scoped to a
  folder so it stays readable at scale).

## How it scales (the pushdown model)

The metadata DB (SQLite locally, Postgres in a deployment — see the deployment docs) is the single
source of truth. The filterable/sortable fields are **promoted out of the stored JSON doc into indexed
columns**, with join tables for the many-valued ones:

| Table | Purpose |
|---|---|
| `catalog_entries` | one row per dataset — `uri` (PK), `name`, `folder`, `owner`, `description`, `row_count`, `usage`, `tbl_id`, and the full `CatalogTable` as `doc` |
| `catalog_tags` | `(uri, tag)` — indexed tag membership (tag filter + facet) |
| `catalog_columns` | `(uri, column)` — indexed column names ("has column X" + column search) |
| `catalog_edges` | `(parent, child, column, pipeline)` — lineage, one row per edge |
| `catalog_embeddings` | `(uri, model, dim, vec)` — semantic vectors (only when an embedder is registered) |

A browse request becomes one `SELECT … WHERE … ORDER BY … LIMIT … OFFSET …` with `EXISTS` subqueries
for tags/columns; facet counts are `GROUP BY` over the same filter set; the folder tree aggregates over
the (small) set of distinct folders; `get` / `resolve` are single indexed lookups; lineage is a
breadth-first expansion that stops at `depth` / `max_nodes`. Nothing is O(catalog size) on the read
path.

Key endpoints (all under `/api`):

- `GET /catalog/tables?q&folder&tags&owner&hasColumns&uris&sort&order&limit&offset` → a page (bare list
  body; `X-Total-Count` / `X-Has-More` headers)
- `GET /catalog/facets?<same filters>` → folder / tag / owner values + counts
- `GET /catalog/tree?prefix=` → one level of the folder tree
- `GET /catalog/search?q&mode=lexical|semantic|hybrid&limit`
- `GET /catalog/lineage?uri&depth&maxNodes`
- `PUT /catalog/tables/{id}/metadata` → set folder / tags / owner / description

Try it at scale: `dataplay seed-catalog --count 5000` registers synthetic datasets across a
folder/tag/owner space, then open the **Tables** view.

## Semantic search (opt-in)

Semantic and hybrid search are powered by a pluggable embedder. The core ships **no** embedding model —
that's a heavy, opinionated dependency. A plugin registers one via `reg.add_embedder(fn, model)`, where
`fn(list[str]) -> list[list[float]]`; the catalog stores a vector per dataset and ranks by cosine
similarity, fusing with lexical results (reciprocal-rank fusion) for hybrid. With no embedder installed,
search transparently falls back to lexical, so it always works offline. See
`examples/plugins/dp_semantic_catalog` for a local `sentence-transformers` implementation.

## Connecting an external catalog / lineage system

Larger organizations usually already run a central metadata service — a catalog with namespaces, tags,
owners, and a lineage graph. Data Playground is designed to sit in front of one **without forking the
core**, because two things line up on purpose:

1. **The whole catalog is one swappable provider.** `reg.set_catalog(obj)` replaces the built-in
   provider with anything that implements the `CatalogProvider` protocol
   (`kernel/hub/backends.py`). The discovery surface a UI needs — `list_page(query)`, `facets(query)`,
   `browse(prefix)`, `search(q, mode)`, `lineage(uri, depth, max_nodes)`, `get_table`, `resolve_ref` —
   is exactly the set of **bounded, pushed-down** operations a remote metadata API also exposes, so a
   provider maps each call onto its backend's paginated/filtered endpoints (cache reads; `resolve_ref`
   runs on every preview/run). A read-only external catalog can subclass the built-in provider and
   override only how rows are fetched, inheriting browse/search/lineage/curation machinery.

2. **Lineage is URI-keyed and OpenLineage-shaped.** An edge is
   `{parent_uri, child_uri, column, pipeline}` plus a free-form `metadata` map — the same shape as the
   emerging cross-tool lineage standards. Because datasets are identified by URI (not an internal id),
   the edges a canvas produces can be pushed to an external lineage store, and edges that store already
   holds can be read back, with no translation of identity.

The organization primitives are intentionally generic — `folder` (a delimiter-joined namespace path),
`tags`, `owner`, `description` — precisely so they round-trip cleanly onto the namespace/tag/owner model
that mature catalogs expose. None of it is tied to any particular vendor; a bridge to a specific system
is a **plugin**, built entirely on the public `CatalogProvider` + `add_embedder` seams, never a patch to
the core.
