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
from kernel.sdk import NodeSpec, ParamSpec, PortSpec, ctx

SPEC = NodeSpec(
    kind="redact", title="redact", category="compute", tag="redact",
    inputs=[PortSpec(id="in", wire="dataset", accepts=["dataset", "sample", "selection"])],
    outputs=[PortSpec(id="out", wire="dataset")],
    params=[ParamSpec(name="column", type="string", label="column to redact"),
            ParamSpec(name="keep", type="int", default=0, label="keep first N chars (rest → *)")],
    blurb="mask a text column (PII) — keep the first N chars, replace the rest with *",
)

def lower(engine, node, inputs):
    cfg = node.data.get("config", {})
    col = (cfg.get("column") or "").strip()
    if not col:
        return inputs[0]                      # not configured yet → passthrough
    keep = int(cfg.get("keep") or 0)
    s = f'CAST("{col}" AS VARCHAR)'
    masked = f"left({s}, {keep}) || repeat('*', greatest(length({s}) - {keep}, 0))"
    return ctx.sql(inputs[0], f'SELECT * REPLACE ({masked} AS "{col}") FROM {{input}}')

def register(reg):
    reg.add_node(SPEC, lower)
```

That's the whole plugin. Two pieces:

- **`NodeSpec`** — the typed declaration: `kind` (unique id), typed input/output **ports** (`wire`
  is the port's type — `dataset`/`sample`/`selection`/`sql-view`/`metric`/`value`; `accepts` lists
  which wires an input port allows), and **params** (`string`/`text`/`code`/`int`/`float`/`bool`/
  `select`/`columns`). The SPA renders the card generically from this — no frontend code.
- **`lower(engine, node, inputs)`** — contributes one step to the logical plan. `inputs[0]` is the
  upstream relation; return a relation. Because it returns a lazy DuckDB relation, it pushes down and
  runs out-of-core exactly like a built-in node — on a preview sample or at full scale.

### The `ctx` builders

`ctx` turns relations into relations without materializing:

- `ctx.sql(rel, "… {input} …")` — run SQL over `rel`, referenced as the placeholder token `{input}`.
  (Use `{input}`, not a bare name — it can't occur in valid SQL, so it never rewrites a real token.)
- `ctx.arrow_map(rel, fn)` — apply a Python `fn(pa.RecordBatch) -> RecordBatch | list[dict]` over
  Arrow batches (the escape hatch when SQL isn't enough).
- `ctx.polars(rel, fn)` — apply `fn(polars.DataFrame) -> polars.DataFrame`.

Prefer `ctx.sql` when it suffices — it stays in the engine and spills to disk.

## Loading it

Three discovery paths (see `kernel/kernel/deps.py`):

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
| `reg.add_node(spec, lower)` | a canvas node | `NodeSpec` + `lower(engine, node, inputs) -> relation` |
| `reg.add_adapter(adapter)` | a dataset source/sink (claim a URI scheme) | `matches/scan/schema/count/fingerprint/write` (see `kernel/kernel/backends.py`) |
| `reg.add_runner(runner)` | an execution backend (pod/Ray/queue) | `ExecutionBackend`: `name/can_run/estimate/run/status/cancel` |
| `reg.add_capability(cap)` | a declared column capability (id + label) | see `kernel/kernel/plugins/capabilities.py` |
| `reg.add_processor(proc)` | a reusable transform in the library picker | a `Processor` (`id/title/mode/build(params)`); see `kernel/kernel/plugins/processors.py` |
| `reg.set_catalog(catalog)` | the dataset catalog provider | replaces the default `InMemoryCatalog` |
| `reg.set_importer(importer)` | `/pipelines/import` (import a foreign pipeline format) | default is a `NullImporter` (501) |

Adapters `insert(0)` so a plugin claims a URI before the built-in DuckDB adapter; runners are picked
by `pick_runner` (respects the Settings → Execution choice, else the first that `can_run`).

## Verifying it

The example is covered by a test that loads it via drop-in discovery and runs its node
(`test_example_plugin_loads_and_runs` in `kernel/kernel/tests/test_kernel.py`) — a good template for
testing your own. `GET /api/plugins` lists loaded packs (with any load error), and `GET /api/nodes`
shows your node's schema the SPA renders from.
