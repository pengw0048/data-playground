# Plugin SPI reference

If you are deciding what to extend, start with the task-first
[plugin onboarding guide](PLUGIN_ONBOARDING.md). It maps common integration jobs to the right
extension boundary, a smallest shipped example, and a verification command. This document is the
complete Plugin SPI reference: use it after you have chosen that boundary.

A plugin can add nodes, dataset adapters, execution backends, capabilities, a catalog, or a pipeline
importer. Register a node and it shows up on the canvas typed, wired, and previewable — no core or
frontend change. The shipped [`examples/plugins/dp_example/`](../examples/plugins/dp_example/)
demonstrates the smallest typed node package.

Plugins are trusted code, not a sandbox boundary. Plugin modules and registration hooks execute in every
trusted Data Playground process that loads the plugin registry, including the hub and per-canvas kernels;
execution drivers may load them too. Node, adapter, backend, or worker code can access the process
capabilities available to it. Install packages only from parties trusted with the workspace, and read the
canonical [supported deployments and trust model](SUPPORT.md) before exposing a plugin to a shared
service.

The neutral [`dp_sidecar_fixture`](../examples/plugins/dp_sidecar_fixture/) is the public
conformance example for a sidecar-producing node. It retains an explicit logical identity and
emits one derived payload column. It has no merge or destination authority: publish its ordinary
output, then use the Write inspector's managed-sidecar merge to select an exact core-owned base,
explicit mappings, and a server-certified preflight.

Install and run the fixture as an entry-point plugin:

```bash
cd kernel
uv run --with ../examples/plugins/dp_sidecar_fixture/ dataplay --workspace /absolute/path/to/workspace
```

Add a **derive sidecar column** node, set `identity`, `value`, and `output`, then run it to an
ordinary managed-local Write. The fixture fails if any of those fields is absent; it never passes a
wide input through as a deceptive sidecar. Its output is only a candidate. Core validates the exact
sidecar/base identities, coverage, schema, current head, and final receipt when the researcher
separately configures the managed-sidecar merge.

## The shape of a plugin

A plugin is a Python package with a `register(reg)` function. Each process that loads the plugin calls
the hook with its process-local `Registry`; do not assume one global invocation across the deployment:

```python
# examples/plugins/dp_example/__init__.py
from hub.sdk import NodeSpec, ParamSpec, PortSpec, ctx, identifier, quote_identifier

SPEC = NodeSpec(
    kind="redact", title="redact", category="compute", tag="redact",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[ParamSpec(name="column", type="string", label="column to redact"),
            ParamSpec(name="keep", type="int", default=0, label="keep first N chars (rest → *)")],
    blurb="mask a text column (PII) — keep the first N chars, replace the rest with *",
)

def build(engine, node, inputs):
    cfg = node.data.get("config", {})
    col = (cfg.get("column") or "").strip()
    if not col:
        return inputs[0]                      # not configured yet → passthrough
    keep = int(cfg.get("keep") or 0)
    column = quote_identifier(identifier(col, inputs[0].columns, label="redact column"))
    s = f"CAST({column} AS VARCHAR)"
    masked = f"left({s}, {keep}) || rpad('', CAST(greatest(length({s}) - {keep}, 0) AS INTEGER), '*')"
    return ctx.sql(inputs[0], f"SELECT * REPLACE ({masked} AS {column}) FROM input")

def register(reg):
    reg.add_node(SPEC, build)
```

Two pieces:

`NodeSpec` is the typed declaration. `kind` is the unique id. Ports use `wire` types
(`dataset`, `sample`, `selection`, `sql-view`, `metric`, `value`); `accepts` lists which wires an input
allows. Params are `string`, `text`, `code`, `int`, `float`, `bool`, `select`, or `columns`. The SPA
renders the card from this schema.

`build(engine, node, inputs)` contributes one step to the logical plan. `inputs[0]` is the upstream
relation; return a relation. A lazy DuckDB relation pushes down and runs out of core like a built-in —
on a preview sample or at full scale.

### The `ctx` builders

`ctx` turns relations into relations without materializing:

- `ctx.sql(rel, "… FROM input …")` — one validated `SELECT` over `rel`, referenced by the query-scope
  CTE `input` (no textual placeholder substitution)
- `ctx.arrow_map(rel, fn)` — `fn(pa.RecordBatch) -> RecordBatch | list[dict]` over Arrow batches
- `ctx.polars(rel, fn)` — `fn(polars.DataFrame) -> polars.DataFrame`
- `ctx.resource(key, factory)` — a warm handle built once by `factory()` and reused across batches and
  runs on the same per-canvas kernel. Namespace `key` (for example `f"{pack}:{model}"`). Thread-safe.
  For plugin nodes with an explicit resource lifecycle, not transform cells; neither is a security
  sandbox. Give the object `close()` / `__exit__` and the kernel releases it on graceful shutdown. See
  `dp_warm_resource`.

Prefer `ctx.sql` when it suffices — it stays in the engine and spills to disk.

## Loading it

Three discovery paths (see `kernel/hub/deps.py`):

1. Drop-in — copy the folder into `<workspace>/plugins/<pack>/` and restart:

   ```bash
   mkdir -p /absolute/path/to/workspace/plugins
   cp -r examples/plugins/dp_example /absolute/path/to/workspace/plugins/
   ```

2. `DP_PLUGINS` — comma-separated importable module names, for example `DP_PLUGINS=dp_example`.

3. pip entry point — publish a package exposing the `dataplay.plugins` group.

Restart the kernel and a **redact** node appears in the toolbar under category `compute`.

## The manifest (`dataplay.toml`)

A drop-in pack may include a manifest. `name` and `version` are required; `min_core_api` is optional:

```toml
name = "dp-example"
version = "0.1.0"
# min_core_api = 1       # refuse to load if the kernel's CORE_API_VERSION is older
```

`min_core_api` is a forward-compat guard. If the plugin needs a newer core than it is running on, the
kernel logs the error and skips the pack. Drop-in packs declare it in `dataplay.toml`; entry-point and
`DP_PLUGINS` modules use a module-level `MIN_CORE_API` or `min_core_api` attribute. A pack with no
manifest loads versionless.

## The rest of the SPI

`register(reg)` can add more than nodes.

`reg.add_node(spec, build[, ir])` registers a canvas node. Optional `ir=ir(node) -> {"op", "config"} |
None` emits an engine-neutral IR op (for example a clean `map` with inlined `code`) instead of
`opaque`, so a distributed backend can run it. See the IR section and `dp_upper`.

`reg.add_adapter(adapter)` claims a URI scheme. Implement `DatasetAdapter` in
`kernel/hub/backends.py`: `name`, `matches`, `scan`, `schema`, `count`, `fingerprint`, `write`, and
optional `nearest`. Interactive sampling is a separate `DatasetPreviewAdapter` capability: add
`preview_scan` only when the adapter can enforce the supplied row limit at the source. Full-run-only
adapters remain valid and the UI will direct users to a durable run instead of silently scanning them.
An adapter may also expose `metadata_count(uri)` only when it is an exact, bounded metadata lookup: it
must not scan rows or perform an unbounded namespace listing. Preflight and recovery also call
`fingerprint(uri)`; keep it bounded and metadata-only, and return the best available revision token rather
than promising a content hash. Missing capabilities or uncertain metadata are handled as unknown cost.

### Field metadata and typed row references

`schema(uri)` returns the current `ColumnSchema` wire model. An adapter may attach up to 32
`annotations` to a field. Each annotation has a non-empty key of at most 128 UTF-8 bytes with no control
characters, an explicit `utf8` or canonical `base64` value encoding, and `provider` or `declared`
provenance. A decoded value is limited to 4 KiB, decoded annotations are limited to 16 KiB per field and
256 KiB per schema, keys are unique, and core serializes them in UTF-8 byte order. Native Arrow/Lance
metadata that is malformed, excessive, or unsafe is dropped as a whole entry; an explicit plugin/wire DTO
with the same defect is rejected. Core never emits a redaction placeholder.

The safety policy is finite and reviewable. Dot, colon, and slash delimit paths; spaces and hyphens inside
one segment normalize to underscores. Core rejects only these exact normalized segments (or an adjacent
pair that joins to one of them, such as `storage.options` -> `storage_options`):
`access_key`, `access_key_id`, `access_token`, `api_key`, `api_token`, `auth_token`, `authorization`,
`authorization_envelope`, `bearer_token`, `client_secret`, `credential`, `credential_envelope`,
`credentials`, `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`,
`google_application_credentials`, `id_token`, `password`, `private_key`, `refresh_token`, `secret`,
`secret_access_key`, `secret_key`, `security_token`, `session_token`, `storage_options`, and `token`. It
does not use substring or entropy matching: keys such as `tokenized.algorithm`, `token_count`, and
`tokenizer` are ordinary
metadata. Decoded UTF-8 values are also rejected when they contain an Authorization Basic/Bearer envelope,
a private-key PEM header, a JSON object with one of those exact credential/storage key segments, or a URI
with userinfo or a credential query key. Credential URI queries additionally reject exact normalized
`sig`, `signature`, `x_amz_credential`, `x_amz_security_token`, `x_amz_signature`,
`x_goog_credential`, and `x_goog_signature` keys. Non-UTF-8 safe values use bounded canonical base64.

A field may carry one `rowReference` with `provider`, `declared`, or `lineage` evidence. Its `target` is
either an exact dataset/revision identity or a canonical catalog `datasetId`; `keyFields` is an ordered,
unique tuple of 1-16 non-empty names (at most 256 UTF-8 bytes each), and `semanticType` is optional and at
most 128 UTF-8 bytes. Do not infer this contract from a column name. An Arrow/Lance adapter may optionally
define `normalize_field_reference(column)`: core calls it only after annotations are sanitized, and it
must return only the generic `TypedRowReference` DTO (or `None`). Core validates the result again; the hook
must not add organization-specific decoding to public core or mutate field identity/type facts.

`reg.add_runner(runner)` adds an execution backend. Implement `ExecutionBackend`: `name`, `can_run`,
`estimate`, `run`, `status`, `cancel`. A backend that can honor a destination-specific or configured
default Cred must also implement `supports_selected_destination_credentials() -> True`. Core treats a
missing or false capability as ambient-identity-only and rejects the run before dispatch; never claim
the capability unless the backend uses the selected Cred rather than silently falling back to ambient
credentials. A remote backend also owns truthful cancellation, deadlines, resource limits, worker trust,
and operational documentation for the shapes it claims.

Plugin registration finishes before the built-in local runner is constructed. If a backend delegates to
that runner, register `reg.add_runner_factory(factory)` instead; core calls `factory(deps)` after catalog
selection and local-runner wiring are complete. Use `reg.add_runner(runner)` when construction needs only
the composition-time objects already available during `register()`.

Every public `RunStatus` uses the same named-output contract. `outputs` is a declaration-ordered array
of `RunOutput` snapshots (`nodeId`, `portId`, `portLabel`, `wire`, `publicationKind`, `outcome`, and the
committed publication fields); there are no singular `outputUri` or `outputTable` fields. An ordinary
run exposes its expected output as `pending` in the first observable status, and every terminal status
settles it as `committed`, `failed`, `cancelled`, or `skipped`. A successful targeted run must contain a
complete committed output set. `version` is the exact catalog revision attested at publication; it is
allowed only on a committed catalog output, never on a pending/non-committed or non-catalog result.
Catalog outputs without an attested version remain valid status records but cannot enter the result
cache. `totalRows` projects the row count only for a single committed output; multi-output cardinality
remains on each `RunOutput`. Profile jobs are inspection jobs: they set `jobType="profile"`, keep
`outputs=[]` and `totalRows=null`, and report their row count only in `profile.rowCount`.

The in-process `LocalRunner` can materialize and own every declared target output. Other execution
backends must opt in with `supports_named_multi_output_runs() -> True` only when execution, terminal
publication, cache ownership, restart recovery, cancellation, and cleanup all preserve the exact set.
A missing, false, or broken probe fails closed before run identity, worker, job, or artifact allocation;
it must never choose the first port as a fallback. Full runs with multiple independent write sinks remain
unsupported on every backend. The private Ray Jobs v4 result artifact still contains `output_uri`,
`output_table`, and step-level `outputs` for its versioned worker/supervisor protocol; those keys are not
plugin SPI and must be translated into public `RunOutput` values only after publication is attested.

`reg.add_capability(cap)` declares a column capability. Optional `detect(col) -> bool` tags matching
columns via `tag_columns`. Optional `viewer = {"kind": …}` adds a declarative viewer tab the SPA
renders generically (`grid` for media, `json` for pretty-printed cells), surfaced through
`KernelInfo.capability_views`. See `kernel/hub/plugins/capabilities.py` and
`web/src/nodes/capabilities.tsx`.

`reg.add_processor(proc)` adds a reusable transform to the library picker — a `Processor` with
`id`, `title`, `mode`, and `build(params)`. See `kernel/hub/plugins/processors.py`.

`reg.set_catalog(catalog)` replaces the dataset catalog provider. The protocol in `backends.py` is
the source of truth: bounded discovery via `list_page(CatalogQuery)`, `facets`, `browse(prefix)`,
`search(q, mode, limit, query=CatalogQuery)`, `search_modes`, plus `get_table`,
`lineage(uri, depth, max_nodes)`,
`relationships`, `resolve_ref`, and write-back / curation (`register`, `register_output`,
`set_metadata`, `unregister`, `set_declared_key`, `add_relationship`, `remove_relationship`).
Every discovery implementation must push its filters and finite window into the backing store;
`search` must apply the supplied structured query before ranking. `get_table` must raise `KeyError` on
a miss. `reg.set_catalog` validates the complete protocol during registration and rejects an incomplete
provider with the missing method names. An application-wide replacement can read discovery rows from an
external service while subclassing `InMemoryCatalog`: `dp_sql_catalog` overrides `get_table`,
`list_page`, `facets`, `browse`, and `search` so SQL rows appear on every read surface. In that pattern,
the inherited managed-local publication, curation, and lineage methods remain core behavior; they do
not write back into the external SQL source.

`lineage` returns a `LineageResult` whose `root_uri` is the canonical identity used by every returned
node and edge. A provider that accepts aliases must resolve the requested alias into this field; clients
use it to identify the root and do not guess from names or provider-specific identifiers.

`reg.set_catalog` selects the one catalog provider for the application instance. The kernel completes all
plugin registration before constructing catalog-dependent runners, profile supervision, and run control,
so catalog reads and execution publication use that same object. Registration happens once at startup;
calling `set_catalog` later is not a hot-swap mechanism. A fully replacing provider must therefore
implement its write-back contract as well as its reads. It may retain `InMemoryCatalog`'s built-in
managed-local lineage and publication behavior, or deliberately implement provider-native write-back;
the latter is never implied merely by external discovery. This is a different boundary from the
read-only Workspace mounts below.

Immutable fact export and cache-reuse recording are optional, runtime-checkable protocols rather than
members of the required `CatalogProvider` contract:

- `CatalogLineageFactExporter.lineage_facts_page(limit=, after_id=)` returns a bounded, monotonic
  `LineageFactsPage`. `/catalog/lineage/facts` delegates to the selected provider and returns `501` if
  this capability is absent; core never reads its own metadata side store behind another provider. The
  route rejects pages larger than the requested limit, IDs that do not advance beyond `after_id`, IDs
  that are not strictly increasing, and inconsistent or non-progressing `hasMore` / `nextAfterId`
  continuations with `502`.
- `CatalogLineageRecorder.record_lineage(name=, uri=, version=, parents=, lineage=)` atomically records
  facts for a previously published exact output and returns the non-negative number inserted (`0` for
  an exact replay).

Fact export is a sequence of bounded page snapshots, not a CDC stream or a transactionally frozen
multi-page snapshot. Unregister deletes every touching fact and the export has no deletion feed. A
consumer that mirrors facts must periodically scan from the beginning and reconcile its complete view;
retaining only the last cursor will miss deletions.

### Read-only external catalog mounts

`hub.catalog_provider` is the public contract for a provider package that can be placed more than
once in the local/trusted-team Workspace. It is intentionally separate from `reg.set_catalog`: the
existing catalog SPI selects one application-wide, writable catalog, while a mount provider supplies
only bounded reads. Multiple mounts compose with the local Workspace browse surface. A mount is not a
destination: it never writes, curates, or otherwise mutates its source catalog, and a run using a mounted
source still publishes through the separately configured managed destination and execution backend.
Workspace persistence and mixed Workspace browse are consumers of this contract, not responsibilities
of a provider wheel.

Import only these public types: `CatalogMount`, `CatalogResource`, `CatalogDatasetDetail`,
`ProviderCapabilities`, `ProviderCapabilitiesResult`, `ProviderPage`, `ProviderSearchPage`,
`ProviderResourceResult`, `ProviderDatasetDetailResult`, `ProviderAncestors`,
`ReadOnlyCatalogProvider`, `bounded_capabilities`, `bounded_list_children`, `bounded_search`,
`bounded_resolve`, `bounded_dataset_detail`, and `bounded_ancestors`.
A `CatalogMount(id, provider, config)` identifies a local placement; `id` is not a provider resource
ID. Every browse or search occurrence has an opaque provider-scoped `placementId`, with an optional
`parentPlacementId`; display names and presentation paths are never identity. A dataset occurrence also
has a required opaque provider-scoped `datasetId`. The same canonical dataset may appear under several
containers through different placement IDs, but every occurrence must agree on its canonical binding URI
and schema facts. A container has no `datasetId`, URI, or columns. A single-placement provider still
returns one explicit placement.

Implement `capabilities`, `list_children`, `resolve`, `ancestors`, and `dataset_detail` using the
identity the provider owns: `list_children(parentPlacementId)`, `resolve(placementId)`, and
`ancestors(placementId)` operate on occurrences, while `dataset_detail(datasetId)` returns a
`CatalogDatasetDetail` containing only canonical dataset facts. Search returns occurrences, including
their placement and parent-placement identities as path evidence. Children take a finite `limit`
(1–500) and an opaque cursor; order must be deterministic. A provider returns
`ready`, `partial`, `unavailable`, or `unsupported` rather than pretending an offline or unsupported
read succeeded. Use the matching `bounded_*` helper at a consumer boundary when a synchronous provider
must not delay local work: deadline, cancellation, and availability failures are normalized to an
honest unavailable result, while a provider can return an explicit truthful partial result itself.
For `resolve`, set `ProviderResourceResult.failure` to `offline`, `permission_lost`, `not_found`, or
`provider_error` when the result is not ready. Workspace treats `not_found` as a terminal detached
binding: a later object with the same display name or provider ID does not revive the old reference;
the user must explicitly relink it. Providers must not put credentials or unauthorized metadata in
`reason`. Workspace keeps passive browse on a short one-second deadline. Explicit user actions — search,
resource resolve, relink, and admitting a selected provider dataset as a Canvas Source — use a separate
five-second deadline. Both are bounded by the same isolated provider workers; this is not a retry policy
or a provider-specific setting, so providers should still enforce suitable bounds on their own I/O.

Lexical search is optional and must be declared with `ProviderCapabilities(search=True)`. A declared
provider implements `search(mount, query, limit=, cursor=)` and returns `ProviderSearchPage` of
placement occurrences, including
`freshness` as `current`, `stale`, or `unknown`. Workspace search never probes an undeclared method: a
mount without this capability is labeled unsupported, while a declared search uses the bounded explicit
interaction deadline and keeps its errors and continuation separate from every other source.

Distribute a provider wheel through the `dataplay.catalog_providers` entry-point group. The entry point
is a zero-argument factory returning the provider; mount configuration is passed on each call. Verify
an installed wheel, without source imports, with:

```bash
python -m hub.catalog_provider_conformance your-provider \
  --mount-id local-provider-a --config root=/path/to/catalog
```

The command verifies capability discovery, bounded pagination, placement-specific resolve and
ancestors, two placements of one canonical dataset, canonical dataset detail, and stable identities
after a provider restart. The reference
[`dp_file_catalog_provider`](../examples/plugins/dp_file_catalog_provider/) reads a `catalog.json`
document from its `root` mount config and never writes it.

Place installed providers into bounded Workspace browse with `DP_CATALOG_MOUNTS`, a JSON array of
local mount configuration. Mounts default to the local Workspace root; `containerId` can place a
mount's root resources in another local overlay container. For example:

```bash
export DP_CATALOG_MOUNTS='[
  {"id":"research-a","provider":"dp-file-catalog","config":{"root":"/data/catalog-a"}},
  {"id":"research-b","provider":"dp-file-catalog","config":{"root":"/data/catalog-b"}}
]'
```

`id` is the stable local mount identity and must be unique. Configuration is passed only to the
installed provider and is never returned by the API; changing mount identity or configuration
invalidates outstanding mixed-browse cursors. A malformed entry or failed provider activation is
reported as an unavailable source while local and other healthy sources remain browseable.

Use a conformance fixture whose first two deterministic root containers share a display name but have
distinct placement IDs, and give each a dataset occurrence with the same dataset ID, canonical URI, and
schema. This makes the command exercise duplicate-name identity, placement-specific ancestors, and
canonical dataset detail instead of accepting a no-op.

The minimal JSON shape consumed by the reference provider is:

```json
{
  "resources": [
    {"placementId": "north", "kind": "container", "name": "Shared"},
    {"placementId": "south", "kind": "container", "name": "Shared"},
    {"placementId": "north-sales", "parentPlacementId": "north", "kind": "dataset",
     "datasetId": "sales", "name": "Sales", "uri": "sales.parquet",
     "columns": [{"name": "id", "type": "int64"}]},
    {"placementId": "south-sales", "parentPlacementId": "south", "kind": "dataset",
     "datasetId": "sales", "name": "Sales", "uri": "sales.parquet",
     "columns": [{"name": "id", "type": "int64"}]}
  ]
}
```

All lineage methods exposed by a catalog plugin must agree on authority. An `InMemoryCatalog` subclass
may serve discovery rows externally while deliberately retaining its inherited core-metadata lineage
graph, exporter, and recorder as one side store. If it moves any lineage surface elsewhere, it must
override the other inherited lineage methods too (or omit the optional ones); otherwise it would combine
an external graph with local facts. A fully replacing provider may omit either optional capability and
receive the fail-closed behavior above.

When a runner writes back an output, it passes a `LineagePublication` to its runner-bound catalog. The
idempotency key reserves the complete output publication, and all per-source facts share the resulting
`publicationKey`. The reservation fingerprint binds the raw parent tokens after canonicalization but
before catalog resolution, the caller's explicit destination URI and version, and the canonical lineage
identity (execution identity, provenance, and mappings). Reusing the key with any changed caller request
is a collision and rolls back. The first apply's facts retain the source and destination projections
resolved at that commit, with versions when available; those mutable projections are not part of replay
matching. An exact replay therefore remains a no-op after a projection changes or is unregistered and
does not restore deleted facts. Empty-source publications reserve the same complete header. The durable
reservation remains after unregister as a retry tombstone, so old work cannot recreate removed evidence.
Because the current `field_mappings` shape (`fieldMappings` on the wire) does not identify a source
dataset, non-empty
mappings require exactly one source; multi-source mappings fail closed instead of being inferred or
duplicated.

The built-in local result cache considers a catalog output reusable only when its cached `RunOutput`
contains an exact `version`, the artifact still exists, and the runner-bound catalog reads back the same
URI, name, and version. It then uses that authority's `CatalogLineageRecorder` to attach the current
run's provenance before reporting a hit. An absent recorder or a stale read-back becomes a cache miss
and recomputation; a provider that advertises the recorder but returns an absent, boolean, negative, or
otherwise malformed durable receipt fails the run instead of hiding incomplete lineage.

`reg.set_managed_object_provider(factory)` sets lifecycle operations for managed object attempts.
`factory(uri) -> ManagedObjectProvider`. The provider must set `complete_inventory=True` and
`conditional_namespace_claims=True`, enumerate every visible object and incomplete multipart upload
plus every version and delete marker under the exact attempt data/commit roots, assign stable member
IDs, delete or abort each member by exact identity, and read/conditionally write the namespace
ownership marker. Core has a `boto3` implementation for `s3://` (including compatible endpoints that
pass this API contract); `r2://`, `gs://`, and other schemes fail closed unless a provider is
registered. Select the same factory headlessly with
`DP_MANAGED_OBJECT_PROVIDER=pkg.module:Provider`.

`reg.add_embedder(fn, model)` powers semantic and hybrid catalog search.
`fn(list[str]) -> list[list[float]]`. The built-in catalog embeds name/folder/description/tags/columns,
stores a vector per dataset (`catalog_embeddings`), background-reindexes existing entries, and ranks
by cosine (plus RRF fusion for `hybrid`). Core ships no model; see `dp_semantic_catalog`. With no
embedder, `search` falls back to lexical and `facets.semanticAvailable` stays false.

`reg.set_importer(importer)` powers `/pipelines/import`. Implement `Importer` in
`plugins/importer.py`: `name` plus `import_pipeline(config, params) -> PipelineImport`. Populate
`PipelineImport.graph` with a runnable canvas `Graph`; the SPA drops it onto a fresh canvas and runs
it. Default is `NullImporter` (501). Core auto-lays out an imported graph left unpositioned.

`reg.add_destination(backend)` adds a save/open-dialog place. Implement `DestinationBackend` in
`destinations.py`: `kind`, `browse(root, path)` → `{path, entries:[{name, kind, uri}], error?}`, and
`target_uri(root, path, filename)`. A user adds a preset (backend + root) in Settings → Destinations.
Built-in `local` / `s3` / `gs` go through the same registry.

`reg.add_telemetry_sink(fn)` exports finished-run telemetry. `fn(record: dict)` is called once per
finished run with `canvas_id`, `run_id`, `request_id`, `job_type`, `status`, `rows`, `ms`, `error`,
declaration-ordered `outputs`, `placement`, and `per_node` (`[{node_id, label, status, rows, ms}]`).
Each output snapshots `node_id`, `port_id`, its declared label/wire, publication kind, outcome, and only
after commit its URI/table/catalog version/row count. Core ships no exporter. A sink that
raises is caught and logged, never failing the run. Delivery is best-effort and asynchronous through a
finite per-sink queue; overload drops the newest event with a rate-limited warning. It stays alongside
the typed `MetricEvent` / `AuditEvent` sinks below — see [`OBSERVABILITY.md`](OBSERVABILITY.md) and
`dp_run_log`.

`reg.add_metric_sink(fn)` exports ops metrics (OPS-01). `fn(MetricEvent)` receives low-cardinality
counters, histograms, and gauges. `reg.add_audit_sink(fn)` exports the security/ops audit trail
(OPS-01): `fn(AuditEvent)` for auth, sharing, settings, and job submit/cancel events, with agent-egress,
secret-ref, and policy-denial schemas defined for later issues. Core ships no exporter for either; both
are documented in [`OBSERVABILITY.md`](OBSERVABILITY.md).

A catalog used by an at-least-once durable runner also implements the runtime-checkable
`DurableCatalogPublisher` capability from `backends.py`: one idempotency key per output through
`register_output_idempotent`, plus one run-level `record_usage_idempotent` call over all distinct source
parents. The output method must return a matching `CatalogPublicationReceipt` only after the provider can
durably read the registered reference; returning `None` or an optimistic in-memory object blocks terminal
publication. A provider that does not need popularity can make the usage call a durable idempotent no-op.
The built-in implementation commits the output receipt, catalog entry and child indexes, local ownership,
and lineage header/facts in one transaction. Its effect fingerprint covers only caller-known, stable
request semantics: name, URI, the caller-requested version (when supplied), canonical raw parent tokens,
pipeline, and canonical lineage identity. The winning first apply probes the artifact and persists its
exact table projection and resulting version in the receipt, but those mutable observed bytes are not
re-probed to match an exact retry. Mutable catalog governance such as folder, tags, owner, and description
is excluded as well. An exact retry after the artifact advances or disappears, or after catalog curation
or unregister, is therefore a side-effect-free receipt replay, not a projection rollback or resurrection.
Core also reserves every managed logical dataset's logical URI, logical ID, and stable catalog key for
that identity, including after unregister. A first unmanaged publication at any reserved alias fails in
the same transaction as its receipt; an exact receipt created before a later managed reservation remains
replayable but cannot recreate its old projection. External publishers should enforce an equivalent
single-owner namespace rule if they combine managed and unmanaged datasets.

That capability alone does not opt an external catalog into managed-output publication for the bundled Ray
Jobs v4 backend. Jobs v4 freezes a pre-probed catalog plan in SQL and coordinates it with core
object-attempt references, lineage, usage, and terminal publication. A graph with a write sink therefore
requires the Ray runner's publication authority to be the built-in DB-backed catalog and fails before
allocation or remote submission when that runner was constructed with another catalog. Supporting an
external provider here needs a future prepared-plan/replay protocol, not just the two idempotent methods
above.

Adapters `insert(0)` so a plugin claims a URI before the built-in DuckDB adapter. Runners are picked by
`pick_runner` (Settings → Execution, else the first that `can_run`). Built-ins go through these same
seams — DuckDB/Lance adapters, InMemoryCatalog, and local runners are the first implementations
registered. Managed immutable-attempt publication is the exception: lifecycle ownership, catalog
pointer/ref swaps, and deletion fences remain core authority.

An execution backend that preserves inputs bound by a hub-admitted exact-revision manifest implements
the optional `AdmittedInputManifestBackend.supports_admitted_input_manifests()` capability. Missing,
false, or broken probes fail closed before run identity or resource allocation, including when the
backend owns one region in a `RunController` plan. Claiming support means every allocation and worker
boundary reopens the admitted revisions; independently resolving mutable source head is not conformant.
The built-in Local, Kernel, and same-host Subprocess transports advertise this capability.

A distributed runner that places work on typed workers can also implement optional `PlaceableBackend`
(`backends.py`): `workers()`, `place(requires)`, `run_unit(graph, output_node, output_uri)`, and
`reachable_tiers()`. Core feature-detects these. A run splits into regions (maximal same-backend
subgraphs, cut at a backend change, fan-out, or `checkpoint`). A region is placed from a cost estimate
— `hub/estimate.py` raises a memory requirement when a blocking region's working set exceeds
`DP_MEMORY_LIMIT`, which `place()` routes to a capable worker. Manual `config.requires` mem wins.
A boundary materializes to the cheapest tier both backends can reach (`reachable_tiers()` ∩): local for
local→local, shared object storage (`DP_STORAGE_URL`) when a remote backend is involved. Placement
activates only when a `place()`-capable backend is registered (`DP_POOL_WORKERS`, or a plugin —
`dp_ray` claims a region tagged `config.requires.labels.engine=ray`). With only the local kernel it is
a no-op. `POST /graph/plan` returns the plan; the Inspector's Run plan preview renders it.

A backend that can durably own a pinned graph but cannot yet recover multi-region orchestration can
instead implement `WholeGraphRequirementBackend.accepts_whole_graph(requires)`. This admission seam
routes the whole graph to that backend without making `place()` claim a region. Once claimed, unsupported
pinned work must fail explicitly; it must not fall back to an engine that does not satisfy the
requirement.

A backend that allocates workload identity or artifacts can implement
`PreboundRunIdentityBackend.preallocate_run_id()`. The method only mints an ID and must have no external
side effects. The hub durably binds that ID to the authorized creator/canvas before calling
`run(..., run_id=...)`, and the backend must preserve the supplied ID. This ordering gives identity
providers a reliable principal without exposing hub database credentials to workers.

Two substrates are selected by setting rather than `register(reg)` — a built-in keyword or a dotted
path to your class (`pkg.module:Class`):

- `DP_KERNEL_SPAWNER` — per-canvas kernel substrate (`KernelSpawner` in `backends.py`). Built-ins:
  `local` (detached process), `pod` (Kubernetes Pod + Service). Custom: `pkg.mod:Cls` →
  `Cls(workspace, data_dir)`.
- `DP_STORAGE` (else `DP_STORAGE_URL`) — where run outputs persist (`Storage` in `storage.py`). Local
  dir / `s3://` / `gs://` via `DP_STORAGE_URL`, or `DP_STORAGE=pkg.mod:Cls` → `Cls(workspace)`.

## Verifying it

The example has a test that loads it via drop-in discovery and runs its node
(`test_example_plugin_loads_and_runs` in `kernel/hub/tests/test_kernel.py`). `GET /api/plugins` lists
loaded packs (with any load error). `GET /api/nodes` shows the schema the SPA renders.

For an installed telemetry plugin, build the core candidate and plugin wheel, install both into a clean
environment, then run the conformance kit that ships in the core wheel. It discovers the plugin only from
its `dataplay.plugins` entry point, checks activation, delivers one finished-run telemetry record, and
stops sink workers before exit. Its failures are stage-labelled (`activation`, `capability`, or `cleanup`)
and intentionally do not echo loader details or configured paths.

```bash
(
  cd kernel
  uv build --wheel
)
(
  cd examples/plugins/dp_run_log
  uv build --wheel
)
uv venv /tmp/dp-plugin-conformance
uv pip install --python /tmp/dp-plugin-conformance/bin/python \
  kernel/dist/data_playground-*.whl \
  examples/plugins/dp_run_log/dist/dp_run_log-*.whl
/tmp/dp-plugin-conformance/bin/python -m hub.plugin_conformance dp-run-log \
  --workspace /tmp/dp-plugin-workspace \
  --telemetry-log /tmp/dp-plugin-workspace/run-telemetry.jsonl
```

Private or third-party plugins can reuse `python -m hub.plugin_conformance` for the telemetry capability
and add their own capability-specific tests. Live integration tests then validate the intended provider or
cluster; they do not replace deterministic contract tests.

### External-wait adapter contract

An optional external-wait plugin imports the immutable DTOs and `ExternalWaitAdapter` protocol from
`hub.external_wait`, then calls `reg.add_external_wait_adapter(adapter)`. Provider kinds are normalized
lowercase slugs and are unique per application instance; duplicate registrations fail closed. Requests,
handles, checkpoints, outcomes, diagnostics, retry hints, and download evidence are bounded and non-secret.
Core supplies the stable submit idempotency key and the isolated download target.

Build the core candidate and the deterministic fixture wheel, install only those candidates into a clean
environment, and run the packaged contract check:

```bash
python -m hub.external_wait_conformance dp-external-wait-fixture \
  --provider-kind fixture-local
```

The command clears ambient `DP_*` routing, discovers the adapter only through its `dataplay.plugins`
entry point, and checks response-loss replay, monotonic polling, terminal replay, cancellation, download
evidence, malformed returns, and instance isolation. Its failure output contains only a stage and canonical
error code. The fixture is an offline conformance dependency, not a default provider or product feature.

## Reference plugins

`examples/plugins/` contains working reference plugins. The product examples below are small, supported
demonstrations of public seams; they are not a bundled provider marketplace. Deterministic conformance
fixtures exercise public contracts without becoming default product services.

- [`dp_example`](../examples/plugins/dp_example/) — `add_node`: `redact` compute node (mask a PII
  column)
- [`dp_external_wait_fixture`](../examples/plugins/dp_external_wait_fixture/) — deterministic installed-
  wheel fixture for the public external-wait adapter contract (not installed by default).
- [`dp_sql_catalog`](../examples/plugins/dp_sql_catalog/) — `set_catalog`: SQL-backed
  `CatalogProvider` subclassing `InMemoryCatalog` and overriding reads.
  `DP_SQL_CATALOG_URL` / `DP_SQL_CATALOG_TABLE`. Uses `sqlalchemy` (core dep).
- [`dp_hf_datasets`](../examples/plugins/dp_hf_datasets/) — `add_adapter`:
  `hf://<id>[@<config>][:<split>]`. Install with `uv pip install -e 'kernel[hf]'`.
- [`dp_iceberg`](../examples/plugins/dp_iceberg/) — `add_adapter`:
  `iceberg://<catalog>/<namespace>.<table>` from your pyiceberg config. Install with
  `uv pip install -e 'kernel[iceberg]'`.
- [`dp_json_pipeline`](../examples/plugins/dp_json_pipeline/) — `set_importer`: tiny JSON pipeline
  (`source` / `steps` / `write`) into a runnable canvas graph.
- [`dp_ray`](../examples/plugins/dp_ray/) — `add_runner` (+ `PlaceableBackend`): Ray Data reference
  backend. See the [support matrix](RAY.md). Install with `uv pip install -e 'kernel[ray]'`.
- [`dp_datasets_place`](../examples/plugins/dp_datasets_place/) — `add_destination`: place
  `kind='datasets'` that browses only dataset files, path-fenced to its root.
- [`dp_json_view`](../examples/plugins/dp_json_view/) — `add_capability`: tags JSON-doc columns and
  declares `viewer={"kind":"json"}` so the SPA shows a JSON tab with no frontend code.
- [`dp_upper`](../examples/plugins/dp_upper/) — `add_node` (+ `ir`): `upper` node whose DuckDB build
  and IR hook share one generated operator, so it runs on Ray too.
- [`dp_similarity_dedup`](../examples/plugins/dp_similarity_dedup/) — `add_node`: cluster near-duplicate
  rows by embedding cosine distance; adds `dup_group` + `is_representative`. Brute-force O(n²) —
  preview on a `sample` first.
- [`dp_run_log`](../examples/plugins/dp_run_log/) — `add_telemetry_sink`: appends one JSON line per
  finished run to `DP_RUN_LOG`.
- [`dp_warm_resource`](../examples/plugins/dp_warm_resource/) — `add_node` (+ `ctx.resource`): builds
  an expensive handle once and reuses it across batches and runs on the warm kernel.
- [`dp_semantic_catalog`](../examples/plugins/dp_semantic_catalog/) — `add_embedder`: local semantic
  and hybrid catalog search; install its optional model dependency explicitly.
- [`dp_file_catalog_provider`](../examples/plugins/dp_file_catalog_provider/) — installed
  `dataplay.catalog_providers` reference for a bounded, read-only Workspace mount.
- [`dp_descriptor_contract`](../examples/plugins/dp_descriptor_contract/) — installed-wheel fixture for
  the NodeSpec and PortSpec descriptor contract; it is a conformance dependency, not a default pack.

The adapters are read-only sources (`write` raises) and import their heavy dependency lazily, so the
pack loads without the extra installed and only errors when its URI scheme is used. Adapter tests use
an in-memory stand-in (`importorskip` skips in CI without the extra).

### Running on another engine — the execution IR

A distributed backend must not re-read node configs and re-implement lowering. It lowers from
`hub.ir`: `lower_to_ir(graph, target)` reads configs once into a `CompiledIR` — a topological list of
`IRStep`s, each a normalized `op`, resolved portable config, and input wiring. A backend pattern-matches
on `op`. `CompiledIR.is_clean()` / `plan_is_clean(plan)` mark the portable map-style subset (`read`,
`write`, `passthrough`, and per-row/-batch `map` / `filter` / `flat_map` / `map_batches`). Relational
ops are not clean by default, but a backend may claim a smaller proven-safe set through
`plan_is_distributable` and conservative config/schema gates. Reducing (`metric` / `chart`) and opaque
steps (`section`, and a plugin node with no `ir` hook) still fall back to DuckDB. `dp_ray` is the
worked example; its supported shapes are in [RAY.md](RAY.md).

Two properties keep this honest:

- One config resolver. `hub.ir.resolve_config(node)` is the single place built-in node config is read
  and key-normalized. The DuckDB engine (`BuildEngine._lower`) reads through it too, so the engine and
  every backend see the same config.
- Plugin nodes can run distributed. An `ir` hook (`reg.add_node(spec, build, ir=…)`) lowers to a real
  op instead of `opaque`. `dp_upper`'s `build` and `ir` share one generated operator. The plan carries
  each step's `op`, so a backend's `can_run` gates on the plugin node's real op too.

The default engine still lowers directly — it is the reference IR interpreter — rather than executing
the `CompiledIR` object. That internal convergence is future work.

## Configuring a plugin

A plugin declares settings in `dataplay.toml` as `[[config]]` fields. Core renders them into
Settings → Plugins with no frontend code:

```toml
[[config]]
key = "url"                       # → the setting plugin.<pack>.url
type = "string"                   # string · text · int · float · bool · select · password
label = "SQLAlchemy URL"
env = "DP_SQL_CATALOG_URL"         # env-var fallback (headless / 12-factor)
placeholder = "postgresql+psycopg://…"
help = "shown under the field"
# also: default, secret = true (store env:/file: reference only), options = [...] (for a select)
```

Read values in `register(reg)` with `reg.config(key, default=None)`. Precedence: UI setting
(`plugin.<pack>.<key>`) > declared `env` var > declared `default` > the arg default. A plugin works
from the UI and headless via env:

```python
def register(reg):
    url = reg.config("url")                     # Settings value, else DP_SQL_CATALOG_URL, else None
    if not url:
        return                                  # not configured → stay inactive
    reg.set_catalog(SqlCatalog(url, reg.config("table", "datasets")))
```

When `secret = true`, the Settings UI and `PUT /api/settings` accept only a secret reference
(`env:VAR_NAME` or `file:/path/to/secret`), never the material token. `reg.config` resolves the
reference in-process during `register()`. To add a third-party backend (such as a secret manager), call
`reg.add_secret_resolver("aws-sm", resolve_fn)` — core ships only `env` and `file`. Scheme names are
case-insensitive and follow the URI grammar `[A-Za-z][A-Za-z0-9+.-]*`; conflicting registrations,
including attempts to replace a built-in, are rejected.

`GET /api/plugins` surfaces each discovered pack's package/source/version, activation state
(`active`, `inactive`, `degraded`, `conflict`, or `failed`), effective capabilities, relevant process
placement, and whether a failure is startup-blocking or an optional degradation. Failure summaries are
sanitized; implementation exceptions and tracebacks stay in server logs. The endpoint also includes each
pack's schema and current values (for secrets, the reference string, not the resolved credential).

A changed setting applies on the next kernel start — plugins register once at startup, same as their env
fallbacks. Config fields need a drop-in `dataplay.toml`; `DP_PLUGINS` / entry-point packs still read env
directly. `dp_sql_catalog` is the worked example.
