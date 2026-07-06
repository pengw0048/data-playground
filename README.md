# Data Playground

**Like ComfyUI, but for data.** A node-based canvas where edges carry *typed columnar tables*:
wire datasets and operators into a graph, see the **real rows out of every step** on a bounded
sample, and run the **same graph at scale** — out-of-core on your laptop (bigger-than-RAM is fine),
or on a cluster via a runner plugin — with no rewrite.

Clone it and it works: **no cloud account, no external services, no mock mode.** Point it at your
Parquet / CSV / JSON / Arrow / Lance files and you're doing real data work in five minutes.

![the canvas](docs/screenshot.png)

```bash
make setup && make run          # → http://127.0.0.1:8471 (seeds sample data on first run)
```

Once installed as a package, it's one command:

```bash
dataplay                        # serves the canvas + engine in one process, opens your browser
dataplay --workspace ./my-proj --port 8471
```

---

## What you get, offline, out of the box

- **Open real data** — Parquet, CSV, JSON, Arrow/Feather, Lance, and directories-of-files. The
  workspace catalog is your local files; `POST /api/catalog/register` (or a `source` node) adds more.
- **Explore & transform** — `filter`, `select`, `join`, `aggregate`, `sort`, `dedup`, `sql`, `sample`,
  `metric`, `vector-search`, and `transform` (arbitrary Python) nodes that **actually execute**.
- **See inside every step** — click a node's eye to see the real rows + schema flowing out of it, on
  a bounded sample, instantly. Media columns render thumbnails; vector columns get an inspector.
- **Run at scale, out-of-core** — the same graph runs over the full dataset. The default engine is
  DuckDB + Polars + Arrow: joins/aggregations/sorts spill to disk instead of crashing. Verified at
  8M rows in ~0.1s on a laptop.
- **Honest previews (P8)** — global aggregates, writes, and opaque ops say *"needs a full pass"*
  rather than computing a misleading answer on a sample.
- **Extend it like ComfyUI** — drop a Python package in `<workspace>/plugins/` and your typed node
  appears in the Add-node menu, **rendered and wired with no frontend code** (§ Plugins).
- **Save, undo, export** — the canvas is diff-friendly JSON, auto-persisted; `⌘Z`/`⌘⇧Z` undo/redo;
  export a node's rows (JSON/CSV) or the whole canvas.

---

## The load-bearing idea: a node lowers to a logical plan

A node does **not** run Python per-row on the server. It **lowers to a step in a typed logical plan**:

- a **relational op** (`filter`/`select`/`join`/`aggregate`/`sort`/`dedup`/`sql`) → a DuckDB relation,
  so it's pushed down, optimized, and out-of-core; or
- a **Python batch UDF** over Arrow `RecordBatch`es (the `transform` escape hatch) → portable to any
  runner.

The runner lowers + executes the whole plan. That's what makes *same graph, sample and scale* real:
the identical plan runs on a bounded sample (instant preview) or over the full dataset out-of-core
(local) — and a runner plugin (Ray/Dask) would bind the same plan to a cluster, no rewrite.

The `dataset` wire is therefore a **lazy, Arrow-schema'd table handle** (a DuckDB relation), so wires
are schema-aware. Data wires are `dataset` / `sample` / `selection` / `sql-view`; `metric` and
`value` are leaf/value wires (a computed scalar driving another node's parameter). Connection
validity is enforced on **both** sides — the canvas blocks incompatible ports, and the kernel
rejects a type-invalid graph too (defense-in-depth for API/agent/plugin clients).

## Architecture (one process)

```
web/     React + React Flow + zustand — the canvas, uniform node card, typed wires, panels
         (data / run / history / code / lineage), agent dock. Renders ANY node — built-in or
         plugin — generically from the /api/nodes schema.

kernel/  FastAPI (one server, serves the SPA + API + WS + engine). graph → COMPILER → logical
         plan → runner.execute(). The default runner is the local out-of-core engine
         (DuckDB · Polars · Arrow · Lance). Everything specific is a plugin (§8 SPI).
```

## Scaling out: multiple stateless web instances

One process is the default and is all most people need. To run several web instances behind a load
balancer, the app's runtime coordination state is now shared, not process-local:

- **Metadata** (users, canvases, shares, settings, versions, run history) — in the SQL metadata DB.
  Point `DP_DATABASE_URL` at Postgres so every instance shares it.
- **Run status** — mirrored to the DB (`run_states`), so `GET /run/{id}` and the status WebSocket are
  answerable from any instance and survive a restart.
- **Catalog** (registered datasets + written outputs + lineage) — write-through to the DB
  (`catalog_entries` / `catalog_edges`), so a dataset registered on one instance is visible to all.
- **Object storage** (`s3://` / `gs://`) holds the data itself; each instance's own DuckDB reads it.

Two things are deployment-side, not app config:

- **Collab** uses one in-memory room per canvas, so route each canvas to a consistent instance —
  a sticky hash on the `/ws/collab/{canvas_id}` path (Figma-style). Example nginx:
  `hash $arg_canvas consistent;` keyed on the canvas id, or any LB with path/consistent-hash routing.
- **Execution** currently runs in the accepting instance (status is shared via `run_states`, so any
  instance can report it). For a dedicated execution tier, add an `ExecutionBackend` plugin (§8 SPI —
  a Ray/pod/queue runner); that step also wants per-run instance ownership + a heartbeat so one
  instance's startup reconcile can't cancel another's live runs (a `TODO` marks the spot in
  `metadb.reconcile_orphaned_runs`).

## Plugins — a stranger's node appears typed & wired, no core edit

Drop a package in `<workspace>/plugins/<pack>/` (or pip-install one exposing a `dataplay.plugins`
entry point). It registers nodes / adapters / runners / capabilities / catalog:

```python
# plugins/upcase/__init__.py
from kernel.sdk import NodeSpec, PortSpec, ParamSpec, ctx

SPEC = NodeSpec(kind="upcase", title="uppercase", category="compute",
                inputs=[PortSpec(id="in", wire="dataset")], outputs=[PortSpec(id="out", wire="dataset")],
                params=[ParamSpec(name="column", type="string", default="name")])

def lower(engine, node, inputs):                      # contribute one step to the plan
    col = node.data.get("config", {}).get("column", "name")
    return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM _')

def register(reg):
    reg.add_node(SPEC, lower)
```

Restart the server → `uppercase` is in the Add-node menu, typed, wired, previewable, runnable — the
frontend rendered it from `/api/nodes`, with no JS written. See `kernel/README.md` for every SPI axis.

## Develop

```bash
make setup     # kernel deps (uv) + sample data + web deps (npm)
make run       # dataplay: build SPA + serve SPA+API on :8471, open browser
make dev-web   # optional: Vite hot-reload on :5173 (proxies /api -> the kernel)
make test      # kernel end-to-end tests (real engine on real files)
make e2e       # browser end-to-end tests (Playwright on the real UI) — see docs/TESTING.md
```

## The agent

The agent is an actor: describe an outcome and it **builds real, inspectable, typed nodes** on the
canvas. It is **provider-agnostic** — a server-side tool-use loop via [LiteLLM](https://docs.litellm.ai),
so you point it at whatever model you have (the key lives in the kernel, never the browser):

```bash
pip install 'data-playground[agent]'

# pick any provider LiteLLM supports via DP_AGENT_MODEL + its key:
export DP_AGENT_MODEL=anthropic/claude-opus-4-8   && export ANTHROPIC_API_KEY=sk-ant-...   # default
# export DP_AGENT_MODEL=openai/gpt-4o             && export OPENAI_API_KEY=sk-...
# export DP_AGENT_MODEL=gemini/gemini-1.5-pro     && export GEMINI_API_KEY=...
# export DP_AGENT_MODEL=ollama/llama3             && export DP_AGENT_BASE_URL=http://localhost:11434  # local, no key
```

Without a configured model it falls back to a built-in offline keyword planner, so the feature
degrades cleanly. The dock shows which is active.

## License

Apache-2.0 — permissive, for adoption and commercial embedding. The engine deps (DuckDB, Polars,
Arrow, Lance) are all MIT/Apache/BSD. Organization-specific backends (a managed catalog, a cluster
runner, private model pipelines) are an optional plugin pack, never a dependency of the core.
