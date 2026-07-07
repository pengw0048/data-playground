# Data Playground — kernel

One FastAPI server that serves the SPA + API + WebSocket **and** runs the engine.
Backend-agnostic core; the default setup runs fully offline and out-of-core.

## Run

```bash
uv sync --extra dev
uv run python -m kernel.seed         # generic sample datasets into ./data
uv run dataplay --workspace . --port 8471   # serve SPA+API+engine, open browser
uv run pytest -q                     # end-to-end tests (real engine on real files)
```

Optional Lance support: `uv sync --extra lance`.

## The engine (default runner) — a node lowers to a plan

`dataset` = a lazy **DuckDB relation**. Each node lowers to a relation transform (out-of-core,
spills) or, for `transform`, a Python UDF over Arrow batches. The runner executes the composed
relation; preview runs the SAME lowering with a bounded source sample (faithful — honest previews).

| op | lowering |
|---|---|
| filter/select/sort/dedup | `rel.filter/.project/.order/.distinct` |
| join / aggregate | DuckDB out-of-core hash join / group-by |
| sql | `rel` as a view → `duckdb.sql(query)` (references `input`) |
| sample | `USING SAMPLE n ROWS (reservoir)` |
| transform | Python over Arrow `RecordBatch`es (map/map_batches/filter/flat_map) |
| vector-search | cosine similarity top-K (Lance ANN when available) |
| write | streaming sink → Parquet/CSV/Lance, registered in the catalog |

Not sample-previewable: `aggregate`, `write`, `opaque`, `loop` → "needs a full pass".

## Layout

```
kernel/
  cli.py           `dataplay` — one-command launcher (seed + serve + open browser)
  main.py          FastAPI routes + /ws
  nodespecs.py     built-in node schemas served at /api/nodes (powers generic rendering)
  compiler.py      graph → typed logical plan
  sandbox.py       ad-hoc cell compiler (soft sandbox + time budget)
  seed.py          generic sample datasets
  sdk.py           dataplay.sdk — what a plugin author imports (NodeSpec/Port/Param + ctx)
  deps.py          composition root + plugin discovery (plugins/ folder + entry points)
  db.py            shared DuckDB connection
  executors/
    engine.py      the lowering engine (relation per node, out-of-core)
    preview.py     sample-preview (same lowering, bounded source)
  plugins/
    adapters.py    Parquet/CSV/JSON/Arrow + Lance adapters (lazy scan + fingerprint)
    runner.py      local out-of-core runner + estimate/placement + content-addressed cache
    catalog.py     workspace catalog + lineage
    capabilities.py media + vector
    processors.py  processor registry (promote-to-library)
```

## API

`GET /api/kernel` · `GET /api/nodes` (every node's schema) · `GET /api/plugins` ·
`GET /api/catalog/tables[/{id}]` · `POST /api/catalog/register` · `GET /api/catalog/lineage?uri=` ·
`POST /api/data/sample` · `POST /api/graph/compile` · `POST /api/run/preview` ·
`POST /api/run/estimate` · `POST /api/run` · `GET /api/run/{id}` · `POST /api/run/{id}/cancel` ·
`WS /ws/run/{id}`.

## Plugins — the extension model

A plugin is a Python package with a `register(reg)` that adds nodes/adapters/runners/capabilities/
catalog/planner. Discovered two ways:

- **drop-in:** `<workspace>/plugins/<pack>/__init__.py` (+ `dataplay.toml` manifest) — restart to load.
- **installed:** a pip package exposing a `dataplay.plugins` entry point, or `DP_PLUGINS=mod1,mod2`.

`reg.add_node(spec, lower)` registers a typed node; `lower(engine, node, inputs) -> relation` builds
its plan step with the `ctx` helpers (`ctx.sql`, `ctx.arrow_map`, `ctx.polars`). The SPA renders it
from `/api/nodes` — no frontend code. Org-specific backends (managed catalog, cluster runner, private
model pipelines) belong in such a pack, never in the core.
