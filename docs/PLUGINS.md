# Writing your first plugin

Data Playground is extensibility-first: a plugin adds **nodes, dataset adapters, execution backends,
capabilities, a catalog, or a pipeline importer** — and a node you register shows up on the canvas,
**typed, wired, and previewable, with no change to the core**. This guide builds one from the shipped
example in [`examples/plugins/dp_example/`](../examples/plugins/dp_example/).

## The shape of a plugin

A plugin is a Python package with a `register(reg)` function. The kernel calls it once at startup,
passing a `Registry` you use to add things:

```python
# examples/plugins/dp_example/__init__.py
from hub.sdk import NodeSpec, ParamSpec, PortSpec, ctx

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
    s = f'CAST("{col}" AS VARCHAR)'
    masked = f"left({s}, {keep}) || repeat('*', greatest(length({s}) - {keep}, 0))"
    return ctx.sql(inputs[0], f'SELECT * REPLACE ({masked} AS "{col}") FROM {{input}}')

def register(reg):
    reg.add_node(SPEC, build)
```

That's the whole plugin. Two pieces:

- **`NodeSpec`** — the typed declaration: `kind` (unique id), typed input/output **ports** (`wire`
  is the port's type — `dataset`/`sample`/`selection`/`sql-view`/`metric`/`value`; `accepts` lists
  which wires an input port allows), and **params** (`string`/`text`/`code`/`int`/`float`/`bool`/
  `select`/`columns`). The SPA renders the card generically from this — no frontend code.
- **`build(engine, node, inputs)`** — contributes one step to the logical plan. `inputs[0]` is the
  upstream relation; return a relation. Because it returns a lazy DuckDB relation, it pushes down and
  runs out-of-core exactly like a built-in node — on a preview sample or at full scale.

### The `ctx` builders

`ctx` turns relations into relations without materializing:

- `ctx.sql(rel, "… {input} …")` — run SQL over `rel`, referenced as the placeholder token `{input}`.
  (Use `{input}`, not a bare name — it can't occur in valid SQL, so it never rewrites a real token.)
- `ctx.arrow_map(rel, fn)` — apply a Python `fn(pa.RecordBatch) -> RecordBatch | list[dict]` over
  Arrow batches (the escape hatch when SQL isn't enough).
- `ctx.polars(rel, fn)` — apply `fn(polars.DataFrame) -> polars.DataFrame`.
- `ctx.resource(key, factory)` — a WARM handle: an expensive-to-construct object (a loaded model, a media
  decoder, a DB connection pool, a GPU context) built ONCE by `factory()` and reused across batches AND
  across runs on the same warm per-canvas kernel, instead of paying the cost per batch. Namespace `key`
  (e.g. `f"{pack}:{model}"`); thread-safe; for trusted PLUGIN nodes, not the sandboxed `transform` cell.
  Give the object a `close()`/`__exit__` and the kernel releases it on graceful shutdown. See `dp_warm_resource`.

Prefer `ctx.sql` when it suffices — it stays in the engine and spills to disk.

## Loading it

Three discovery paths (see `kernel/hub/deps.py`):

1. **Drop-in** — copy the folder into `<workspace>/plugins/<pack>/`. Restart; it's picked up.
   ```bash
   cp -r examples/plugins/dp_example "$DP_WORKSPACE/plugins/"
   ```
2. **`DP_PLUGINS`** — a comma-separated list of importable module names: `DP_PLUGINS=dp_example`.
3. **pip entry point** — publish a package exposing the `dataplay.plugins` entry-point group.

Restart the kernel and a **redact** node appears in the toolbar (category `compute`), fully wired.

## The manifest (`dataplay.toml`)

A drop-in pack may include a manifest. `name` + `version` are required; `min_core_api` is optional:

```toml
name = "dp-example"
version = "0.1.0"
# min_core_api = 1       # refuse to load if the kernel's CORE_API_VERSION is older
```

`min_core_api` is a forward-compat guard: if your plugin needs a newer core than it's running on, the
kernel logs it and skips the pack rather than loading a broken plugin. (Enforced for drop-in packs;
entry-point / `DP_PLUGINS` modules currently bypass it.) A pack with no manifest loads versionless.

## The rest of the SPI

`register(reg)` can add more than nodes — the same `reg`:

| call | extends | contract |
|---|---|---|
| `reg.add_node(spec, build[, ir])` | a canvas node | `NodeSpec` + `build(engine, node, inputs) -> relation`. OPTIONAL `ir=ir(node) -> {"op", "config"} \| None`: an engine-neutral emit hook so the node lowers to a real IR op (e.g. a clean `map` with inlined `code`) instead of `opaque` — then a DISTRIBUTED backend runs it, not just DuckDB. See the IR section + `dp_upper`. |
| `reg.add_adapter(adapter)` | a dataset source/sink (claim a URI scheme) | `DatasetAdapter` Protocol in `kernel/hub/backends.py`: `name/matches/scan/schema/count/fingerprint/write` (+ optional `nearest`) |
| `reg.add_runner(runner)` | an execution backend (pod/Ray/queue) | `ExecutionBackend` Protocol (`backends.py`): `name/can_run/estimate/run/status/cancel` |
| `reg.add_capability(cap)` | a declared column capability (+ optional viewer tab) | `id`+`label`; an OPTIONAL `detect(col)->bool` (present → `tag_columns` tags matching columns with the id, no core edit); and an OPTIONAL `viewer = {"kind": …}` — a declarative viewer TAB the SPA renders with a generic renderer (`grid` = media/image grid, `json` = pretty-printed cell), surfaced via `KernelInfo.capability_views`. So a plugin adds a viewer tab with **no frontend code**. See `kernel/hub/plugins/capabilities.py` + `web/src/nodes/capabilities.tsx`. |
| `reg.add_processor(proc)` | a reusable transform in the library picker | a `Processor` (`id/title/mode/build(params)`); see `kernel/hub/plugins/processors.py` |
| `reg.set_catalog(catalog)` | the whole dataset catalog provider | `CatalogProvider` Protocol (`backends.py`, the source of truth): the bounded discovery surface `list_page(CatalogQuery)/facets/browse(prefix)/search(q, mode)/search_modes` + `list_tables/get_table/lineage(uri, depth, max_nodes)/relationships/resolve_ref` + write-back/curation `register/register_output/set_metadata/unregister/set_declared_key/add_relationship/remove_relationship`. **`get_table` MUST raise `KeyError` on a miss.** A provider written against the pre-scale protocol (no `list_page`/`facets`/`browse`/`search`) still works — `reg.set_catalog` wraps it in `CatalogCompat`, which synthesizes those from bounded `list_tables()` calls. A read-only external catalog can subclass `InMemoryCatalog` and override only the reads (as `dp_sql_catalog` does — note it overrides `list_page/facets/browse/search` too, so SQL rows appear while browsing). A catalog that *fully* replaces the built-in — not subclassing `InMemoryCatalog` — won't automatically receive run-completion `register_output` write-backs (runners hold the catalog they were built with), so either subclass `InMemoryCatalog` or forward `register_output` to your store. |
| `reg.add_embedder(fn, model)` | semantic + hybrid catalog search | `fn(list[str]) -> list[list[float]]`. The built-in catalog embeds each dataset's name/folder/description/tags/columns, stores a vector per dataset (`catalog_embeddings`), background-reindexes existing entries, and ranks by cosine (+ RRF fusion for `hybrid`). Core ships **no** model; see `dp_semantic_catalog`. With no embedder, `search` falls back to lexical and `facets.semanticAvailable` stays false (the UI hides its "meaning" toggle). |
| `reg.set_importer(importer)` | `/pipelines/import` (import a foreign pipeline format) | `Importer` Protocol (`plugins/importer.py`): `name` + `import_pipeline(config, params) -> PipelineImport`. Populate `PipelineImport.graph` with a runnable canvas `Graph` (nodes/edges of built-in or plugin kinds) and the SPA drops it onto a fresh canvas and runs it — this is what makes *import a pipeline → runnable canvas* real. Default is a `NullImporter` (501, honest). The core auto-lays-out an imported graph left unpositioned. |
| `reg.add_destination(backend)` | a save/open-dialog **"place"** (a browsable/writable location) | `DestinationBackend` Protocol (`destinations.py`): `kind` + `browse(root, path)` (→ `{path, entries:[{name, kind, uri}], error?}`) + `target_uri(root, path, filename)`. Claims a place `kind`; a user adds a preset (backend + root) in Settings → Destinations. Built-in `local`/`s3`/`gs` go through the same registry. |
| `reg.add_telemetry_sink(fn)` | run observability (export finished-run telemetry) | `fn(record: dict)` invoked once per FINISHED run with a normalized record: `canvas_id/run_id/status/rows/ms/error/output_table/placement/per_node` (`per_node` = `[{node_id, label, status, rows, ms}]`). Core ships **no** exporter (offline-first) — an OTel/StatsD/warehouse sink is a plugin; a sink that raises is caught + logged, never failing the run. See `dp_run_log`. |

Adapters `insert(0)` so a plugin claims a URI before the built-in DuckDB adapter; runners are picked
by `pick_runner` (respects the Settings → Execution choice, else the first that `can_run`). **The
built-ins go through these same seams — the DuckDB/Lance adapters, the InMemoryCatalog, and the local
runners are just the first implementations registered, not a privileged core path.**

A distributed runner that places work on typed workers (GPU / region routing) can additionally implement
the optional `PlaceableBackend` Protocol (`backends.py`): `workers()` (advertise capacities), `place(requires)`
(pick a worker, or None), `run_unit(graph, output_node, output_uri)` (run one placed **region** — reading its
input from the given tier URI and writing its output to `output_uri`, so workers touch shared storage
directly), and `reachable_tiers()` (which storage tiers this backend can read/write — e.g. `("object",)` for
a remote cluster, `("local","object")` for one sharing the hub's disk). The core feature-detects these, so a
non-distributed backend omits them.

**How placement + tiering drive `run_unit`.** A run splits into regions (maximal same-backend subgraphs, cut
at a backend change / fan-out / `checkpoint`). A region is placed by a cost estimate — a conservative per-node
size estimate (`hub/estimate.py`) raises a memory requirement when a blocking region's working set exceeds the
local budget (`DP_MEMORY_LIMIT`), which `place()` routes to a worker with the memory; a manual `config.requires`
mem is authoritative. A boundary materializes to the cheapest tier both the producing and consuming backend can
reach (`reachable_tiers()` ∩), local for a local→local handoff, a shared object store (`DP_STORAGE_URL`) when a
remote backend is involved. Placement activates only when a `place()`-capable backend is registered
(`DP_POOL_WORKERS`, or a plugin — `dp_ray` claims a region tagged `config.requires.labels.engine=ray`); with
only the local kernel it's a no-op. `POST /graph/plan` returns this plan (regions + backend + tier + estimate)
— the Inspector's *Run plan* preview renders it.

Two substrates are selected by a setting rather than `register(reg)` — set it to a built-in keyword or a
**dotted path to your own class** (`pkg.module:Class`), so a third implementation needs no core patch:

| setting | selects | built-ins · custom |
|---|---|---|
| `DP_KERNEL_SPAWNER` | the per-canvas kernel substrate (`KernelSpawner` Protocol, `backends.py`) | `local` (detached process) · `pod` (k8s Pod+Service) · `pkg.mod:Cls` → `Cls(workspace, data_dir)` |
| `DP_STORAGE` (else `DP_STORAGE_URL`) | where run outputs persist (`Storage` Protocol, `storage.py`) | local dir / `s3://`·`gs://` via `DP_STORAGE_URL` · `DP_STORAGE=pkg.mod:Cls` → `Cls(workspace)` |

## Verifying it

The example is covered by a test that loads it via drop-in discovery and runs its node
(`test_example_plugin_loads_and_runs` in `kernel/hub/tests/test_kernel.py`) — a good template for
testing your own. `GET /api/plugins` lists loaded packs (with any load error), and `GET /api/nodes`
shows your node's schema the SPA renders from.

## Reference plugins

`examples/plugins/` ships twelve working plugins — each exercises a different seam end-to-end and has a
test in `kernel/hub/tests/test_kernel.py` you can copy:

| plugin | seam | what it does | extra |
|---|---|---|---|
| [`dp_example`](../examples/plugins/dp_example/) | `add_node` | a `redact` compute node (mask a PII column) | — |
| [`dp_sql_catalog`](../examples/plugins/dp_sql_catalog/) | `set_catalog` | a `CatalogProvider` backed by a SQL `datasets(name, uri)` table — subclasses `InMemoryCatalog`, overrides only the reads; `DP_SQL_CATALOG_URL` / `DP_SQL_CATALOG_TABLE` | uses `sqlalchemy` (core dep) |
| [`dp_hf_datasets`](../examples/plugins/dp_hf_datasets/) | `add_adapter` | read a Hugging Face Hub dataset as a source: `hf://<id>[@<config>][:<split>]` | `uv pip install -e 'kernel[hf]'` |
| [`dp_iceberg`](../examples/plugins/dp_iceberg/) | `add_adapter` | read an Apache Iceberg table as a source: `iceberg://<catalog>/<namespace>.<table>` (catalog from your pyiceberg config) | `uv pip install -e 'kernel[iceberg]'` |
| [`dp_json_pipeline`](../examples/plugins/dp_json_pipeline/) | `set_importer` | parse a tiny JSON pipeline (`source`/`steps`/`write`) into a runnable canvas graph — import → canvas → run | — |
| [`dp_ray`](../examples/plugins/dp_ray/) | `add_runner` (+ `PlaceableBackend`) | a **Ray Data reference backend** for clean transforms plus conservatively gated grouped aggregate, partitioned window, full-row dedup, broadcast `inner`/`left`/`cross` join, and plain-key sort. Unpinned choices may fall back to DuckDB; an explicit `config.requires.labels.engine=ray` instead fails loudly when the shape or resources are unsupported. Region outputs are worker-direct Parquet shards; object-store/non-Parquet reads and whole-graph sinks are driver-funneled. Driver and workers must use the pinned Ray 2.56.0 runtime, verified by a startup handshake. See the [support/readiness matrix](RAY.md) | `uv pip install -e 'kernel[ray]'` |
| [`dp_datasets_place`](../examples/plugins/dp_datasets_place/) | `add_destination` | a save/open "place" (`kind='datasets'`) that browses only dataset files, hiding clutter; path-fenced to its root | — |
| [`dp_json_view`](../examples/plugins/dp_json_view/) | `add_capability` | tags JSON-doc columns (name-based detector) + declares `viewer={"kind":"json"}` → the SPA shows a JSON tab that pretty-prints those cells, no frontend code | — |
| [`dp_upper`](../examples/plugins/dp_upper/) | `add_node` (+`ir`) | an `upper` node whose DuckDB build + engine-neutral `ir` hook share one generated operator, so it runs on Ray too (a clean `map`), not just DuckDB | — |
| [`dp_similarity_dedup`](../examples/plugins/dp_similarity_dedup/) | `add_node` | a `similarity-dedup` node: cluster near-duplicate rows by embedding cosine distance → adds `dup_group` + `is_representative` (filter downstream to keep one per cluster). Brute-force O(n²) — preview on a `sample` first; honest scale/accuracy limits in its docstring | — |
| [`dp_run_log`](../examples/plugins/dp_run_log/) | `add_telemetry_sink` | a telemetry sink that appends one JSON line per finished run to a log file (`DP_RUN_LOG`) — the reference for where an OTel/warehouse exporter plugs in (core ships none; offline-first) | — |
| [`dp_warm_resource`](../examples/plugins/dp_warm_resource/) | `add_node` (+`ctx.resource`) | a `warm-map` node that builds an expensive handle (a model / decoder / pool) ONCE via `ctx.resource` and reuses it across batches + runs on the warm kernel, instead of reconstructing per batch | — |

The adapters are read-only sources (`write` raises) and import their heavy dependency lazily, so the
pack loads even without the extra installed and only errors when its URI scheme is actually used. Both
adapter tests run against an in-memory stand-in (`importorskip` → skipped in CI without the extra), so
they prove the wiring; verify the network path against your own Hub/warehouse.

### Running on another engine — the execution IR

A distributed backend must not re-read node configs and re-implement lowering. Instead it lowers from
`hub.ir`: `lower_to_ir(graph, target)` reads the configs ONCE into a `CompiledIR` — a topological list of
`IRStep`s, each a normalized `op` + resolved portable config + input wiring. A backend pattern-matches on
`op`. `CompiledIR.is_clean()` / `plan_is_clean(plan)` mark the portable map-style subset
(`read`, `write`, `passthrough`, and per-row/-batch `map`/`filter`/`flat_map`/`map_batches`). Relational
ops are not clean by default, but a backend may explicitly claim a smaller, proven-safe set through
`plan_is_distributable` and conservative config/schema gates. Reducing (`metric`/`chart`) and opaque
steps (`section`, and a plugin node with no `ir` hook) still fall back to DuckDB. `dp_ray` is the worked
example: it reuses the same `sandbox.compile_operator` for transforms and the same DuckDB relational SQL
inside complete Ray partitions; its exact supported shapes and scale limits are documented in
[RAY.md](RAY.md).

Two properties make this real rather than a side artifact:

- **One config resolver.** `hub.ir.resolve_config(node)` is the single place built-in node config is read
  + key-normalized, and the DuckDB engine (`BuildEngine._lower`) reads through it too — so the engine and
  every backend see the same config and can't diverge.
- **Plugin nodes can run distributed.** A plugin node that provides an `ir` hook (`reg.add_node(spec,
  build, ir=…)`) lowers to a real op instead of `opaque`, so it runs on a distributed backend — see
  `dp_upper`, whose `build` and `ir` share one generated operator. The plan carries each step's `op`, so a
  backend's `can_run` gates on the plugin node's real op too (not just built-ins).

(The default engine still lowers directly — it's the reference IR interpreter — rather than executing the
`CompiledIR` object; that internal convergence is future work.)

## Configuring a plugin

A plugin declares its settings in `dataplay.toml` as `[[config]]` fields (VSCode `contributes.configuration`
style) — the core renders them into a form in **Settings → Plugins**, no frontend code:

```toml
[[config]]
key = "url"                       # → the setting plugin.<pack>.url
type = "string"                   # string · text · int · float · bool · select · password
label = "SQLAlchemy URL"
env = "DP_SQL_CATALOG_URL"         # env-var fallback (headless / 12-factor)
placeholder = "postgresql+psycopg://…"
help = "shown under the field"
# also: default, secret = true (never echoed to the UI), options = [...] (for a select)
```

Read the values in `register(reg)` with **`reg.config(key, default=None)`**. Precedence:
**UI setting (`plugin.<pack>.<key>`) > declared `env` var > declared `default` > the arg default** — so a
plugin is configurable from the UI *and* still works headless via env:

```python
def register(reg):
    url = reg.config("url")                     # Settings value, else DP_SQL_CATALOG_URL, else None
    if not url:
        return                                  # not configured → stay inactive
    reg.set_catalog(SqlCatalog(url, reg.config("table", "datasets")))
```

`GET /api/plugins` surfaces each pack's schema + current values (secrets report only *whether* set, never
the value). A changed setting applies on the **next kernel start** — plugins register once at startup, same
as the env vars they fall back to. (Config fields need a drop-in `dataplay.toml`; `DP_PLUGINS`/entry-point
packs still read env directly.) `dp_sql_catalog` is the worked example.
