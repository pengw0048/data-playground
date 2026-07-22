# Plugin onboarding

Use a plugin when one package should add a typed canvas operation or connect Data Playground to a
system it does not own. Start with the narrowest boundary below. Each shipped example is a working
reference, not a provider that is enabled by default.

Plugins are trusted Python code. Loading one gives its registration hook and execution code the
capabilities of every trusted Data Playground process that loads it. Install packages only from
parties trusted with the Workspace; see the [deployment and trust model](SUPPORT.md) before putting a
plugin in a shared service.

## Choose the boundary

| You need to… | Start with | Do not start with |
| --- | --- | --- |
| Add a typed operation to the canvas | A node | An execution backend or catalog replacement |
| Show an external catalog in Workspace without changing it | A read-only catalog mount | `set_catalog` |
| Replace the application's catalog contract | A full catalog provider | A mount, unless the source is read-only |
| Read a new URI scheme or data format | A dataset adapter | A catalog provider |
| Offer a new save location | A destination | A dataset adapter |
| Dispatch durable work to another engine | An execution backend | A node or adapter |
| Record finished runs for an external observer | A telemetry sink | An execution backend |

The [Plugin SPI reference](PLUGINS.md) is the source of truth for every API signature and capability.
The sections below are intentionally smaller: pick one boundary, run its test, then read the linked
reference section before extending it.

## Add a typed operation

**Use when:** users should place an operation on the canvas, connect typed inputs and outputs, and
configure its parameters in the card.

**Do not use when:** the change only makes a new source, destination, catalog, or remote execution
engine available. Those are integration boundaries below, even if a later node uses them.

**Smallest reference:** [`dp_example`](../examples/plugins/dp_example/) registers the `redact` node
with `reg.add_node(...)`. Its drop-in package is the shortest path to seeing a node appear in the
toolbar.

**Verify:**

```bash
cd kernel && uv run pytest -q hub/tests/test_kernel.py::test_example_plugin_loads_and_runs
```

Then read [node declarations and `ctx` builders](PLUGINS.md#the-shape-of-a-plugin). Add an `ir` hook
only if the operation must run on a backend that supports its engine-neutral operation; see
[execution IR](PLUGINS.md#running-on-another-engine--the-execution-ir).

## Mount a read-only external catalog in Workspace

**Use when:** Workspace should browse, search, and resolve data from an external catalog while the
plugin does not own its writes, curation, or publication. Multiple mounts can appear alongside the
local Workspace.

**Do not use when:** the package must become the one application-wide catalog for every read and
write, or when the user merely needs a new URI scheme. Use a full catalog provider or dataset adapter
respectively.

**Smallest reference:** [`dp_file_catalog_provider`](../examples/plugins/dp_file_catalog_provider/)
is an installed `dataplay.catalog_providers` wheel. It reads a configured `catalog.json` and never
writes it.

**Verify:** the shipped example's installed-wheel test builds the wheel, installs it into a clean
environment, and runs the public conformance check without importing provider source files:

```bash
cd kernel && uv run pytest -q hub/tests/test_catalog_provider.py::test_file_provider_wheel_passes_public_conformance
```

After installing your own provider wheel into an environment containing Data Playground, run the same
public check against it:

```bash
python -m hub.catalog_provider_conformance your-provider \
  --mount-id local-provider-a --config root=/path/to/catalog
```

Read the [read-only mount contract](PLUGINS.md#read-only-external-catalog-mounts) for bounded pages,
stable identities, availability states, configuration, and the exact entry-point contract.

## Replace the application catalog only when you own the contract

**Use when:** one package deliberately owns the application's complete catalog behavior: discovery,
lookup, lineage, curation, and the write-back/published-output semantics. There is one selected
application catalog, not one per Workspace placement.

**Do not use when:** external data should be shown read-only beside local Workspace data. Do not use it
to make a single data format readable. Those jobs are a mount and an adapter respectively.

**Smallest reference:** [`dp_sql_catalog`](../examples/plugins/dp_sql_catalog/) demonstrates the
hybrid pattern: it replaces read surfaces from SQL while inheriting Data Playground's managed-local
publication, curation, and lineage behavior. It is not an example of writing back into SQL.

**Verify:**

```bash
cd kernel && uv run pytest -q hub/tests/test_kernel.py::test_sql_catalog_reference_plugin
```

Read the [`set_catalog` contract](PLUGINS.md#the-rest-of-the-spi). If the external system owns
write-back too, implement every required catalog method and its publication semantics rather than
copying the hybrid example unchanged.

## Connect data

### Add a source or data format

**Use when:** a URI scheme or format should become an input dataset. A dataset adapter owns bounded
metadata, scans, schema, fingerprints, and only the preview capability it can honestly provide.

**Do not use when:** the task is to organize catalog resources, choose a destination, or run a graph
elsewhere. An adapter is a source boundary, not an external catalog overlay or scheduler.

**Smallest reference:** [`dp_hf_datasets`](../examples/plugins/dp_hf_datasets/) registers an
`hf://` adapter and intentionally supports full runs without claiming source-limited interactive
preview.

**Verify:** install the optional dependency, then run the deterministic adapter test:

```bash
cd kernel
uv sync --extra dev --extra hf
uv run pytest -q hub/tests/test_kernel.py::test_hf_datasets_adapter_reference_plugin
```

Read [`add_adapter`](PLUGINS.md#the-rest-of-the-spi) for the required methods and honest preview,
cost, fingerprint, and write behavior.

### Add a destination

**Use when:** users need a bounded, selectable place for saved outputs, such as a fenced dataset
directory.

**Do not use when:** the task only reads a new source format. A destination does not make that format
readable; pair it with an adapter only when the product genuinely needs both directions.

**Smallest reference:** [`dp_datasets_place`](../examples/plugins/dp_datasets_place/) registers the
`datasets` destination and restricts browsing to its configured root.

**Verify:**

```bash
cd kernel && uv run pytest -q hub/tests/test_kernel.py::test_datasets_place_destination_reference_plugin
```

Read [`add_destination`](PLUGINS.md#the-rest-of-the-spi) before adding credentials, URI rules, or
publication behavior.

## Run on another execution backend

**Use when:** a package owns dispatch, status, cancellation, deadlines, resource limits, and truthful
publication for a class of durable runs on another engine.

**Do not use when:** a local transform simply needs to process a new source or output. Do not add a
remote backend as a wrapper around the local runner; use `reg.add_runner_factory(...)` only when a
backend needs the constructed local runner as a dependency.

**Smallest reference:** [`dp_ray`](../examples/plugins/dp_ray/) is the optional Ray Data backend. It
is a reference for a specific supported operational shape, not a general-purpose remote-execution
template; check its [support matrix](RAY.md) before adopting it.

**Verify:** the base test checks backend gating and local fallback without requiring a Ray cluster:

```bash
cd kernel && uv run pytest -q hub/tests/test_kernel.py::test_ray_backend_operator_gating_and_fallback
```

Read [`add_runner` and `add_runner_factory`](PLUGINS.md#the-rest-of-the-spi) and then the
[execution IR contract](PLUGINS.md#running-on-another-engine--the-execution-ir). A live backend also
needs integration tests against the engine it dispatches to; the local gating test does not prove a
cluster deployment.

## Record finished-run telemetry

**Use when:** an external observer needs one bounded record after each run finishes, without changing
how the graph is dispatched or where its outputs are published.

**Do not use when:** the external system must schedule, execute, cancel, or publish a run. Those are
execution-backend responsibilities, even if that system also emits logs or metrics.

**Smallest reference:** [`dp_run_log`](../examples/plugins/dp_run_log/) registers a telemetry sink
that appends one JSON line per finished run to the configured `DP_RUN_LOG` path.

**Verify:** the installed-wheel conformance test builds the core and plugin wheels, installs only
those candidates into a clean environment, delivers a finished-run record, and stops the sink worker:

```bash
cd kernel && uv run pytest -q hub/tests/test_plugin_wheel_conformance.py::test_run_log_wheel_conformance_uses_only_its_entry_point
```

Read the [telemetry conformance reference](PLUGINS.md#verifying-it) before adding a sink. It defines
the installed-wheel test boundary; capability-specific integration tests remain the plugin's
responsibility.

## Before publishing a plugin

Keep the onboarding boundary small, then use the reference for advanced behavior rather than copying
internal implementation details:

- [Plugin discovery and manifests](PLUGINS.md#loading-it)
- [Plugin settings and secret references](PLUGINS.md#configuring-a-plugin)
- [Catalog transactions, lineage, and write-back](PLUGINS.md#the-rest-of-the-spi)
- [Execution IR and durable backend outputs](PLUGINS.md#running-on-another-engine--the-execution-ir)
- [Installed-wheel telemetry conformance and external-wait lifecycle](PLUGINS.md#verifying-it)
- [All shipped reference plugins](PLUGINS.md#reference-plugins)

For a plugin distributed as a wheel, test the installed wheel in a clean environment. The
[telemetry conformance example](PLUGINS.md#verifying-it) shows the pattern for `dataplay.plugins`;
the catalog-provider command above is the corresponding public check for Workspace mounts.
