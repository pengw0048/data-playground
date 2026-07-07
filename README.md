# Data Playground

**Like ComfyUI, but for data.** A node-based canvas where edges carry *typed columnar tables*:
wire datasets and operators into a graph, see the **real rows out of every step** on a bounded
sample, and run the **same graph at scale** — out-of-core on your laptop (bigger-than-RAM is fine),
or, via the **ExecutionBackend plugin SPI**, on a cluster with no rewrite (a reference multi-worker
pool backend ships; Ray/pod/queue runners are plugin territory).

Clone it and it works: **no cloud account, no external services, no mock mode.** Point it at your
Parquet / CSV / JSON / Arrow / Lance files and you're doing real data work in five minutes.

![the canvas](docs/screenshot.png)

> **Prereqs:** [uv](https://docs.astral.sh/uv/) and Node 20+ (uv fetches the pinned Python 3.12
> automatically). Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`

```bash
make setup && make run          # → http://127.0.0.1:8471 (seeds sample data on first run)
```

`make setup` installs the `dataplay` command into the local venv, so from then on it's one command:

```bash
cd kernel && uv run dataplay              # serves the canvas + engine in one process, opens your browser
cd kernel && uv run dataplay --workspace ./my-proj --port 8471
```

(Not yet published to PyPI — install from a clone with `uv pip install -e 'kernel[agent]'` for a bare
`dataplay` on your PATH.)

**New here?** The **[5-minute tour](docs/TUTORIAL.md)** builds a real pipeline on the seeded data —
events → keep purchases → total per user → save.

---

## What you get, offline, out of the box

- **Open real data** — Parquet, CSV, JSON, Arrow/Feather, Lance, and directories-of-files. The
  workspace catalog is your local files; `POST /api/catalog/register` (or a `source` node) adds more.
- **Explore & transform** — `filter`, `select`, `join`, `aggregate`, `sort`, `dedup`, `sql`, `sample`,
  `metric`, `chart`, `vector-search`, and `transform` (arbitrary Python) nodes that **actually execute**.
- **See inside every step** — click a node's eye to see the real rows + schema flowing out of it, on
  a bounded sample, instantly. Media columns render thumbnails; vector columns get an inspector.
- **See how tables relate** — the catalog detects join keys, measures cardinality on real data
  (1:1 / 1:N / N:M), and suggests how two datasets join; declare keys/relationships by hand and view
  them as an ER/UML diagram.
- **Run at scale, out-of-core** — the same graph runs over the full dataset. The default engine is
  DuckDB + Polars + Arrow: joins/aggregations/sorts spill to disk instead of crashing, so a dataset
  bigger than RAM sorts under a bounded memory cap rather than OOM-ing.
- **Honest previews** — global aggregates, writes, and opaque ops say *"needs a full pass"*
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
         (DuckDB · Polars · Arrow · Lance). Everything specific is a plugin (see Plugins below).
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
  instance can report it). For a dedicated execution tier, add an `ExecutionBackend` plugin (the plugin SPI —
  a Ray/pod/queue runner); that step also wants per-run instance ownership + a heartbeat so one
  instance's startup reconcile can't cancel another's live runs (a `TODO` marks the spot in
  `metadb.reconcile_orphaned_runs`).

**Run with Docker.** `docker compose up` builds one image (Vite SPA baked in) and runs it against
Postgres — the shared-metadata, restart-durable setup the bullets above describe. `Dockerfile` is the
single-image build (`docker build -t dataplay .`); `docker-compose.yml` adds Postgres + volumes and
documents `deploy.replicas` + sticky routing for the multi-instance case. Set `DP_AUTH_SECRET` and
`DP_DATASET_ROOTS` for a multi-user deployment; TLS/reverse-proxy is operator-specific (front it with
nginx/Caddy). The app is on `http://localhost:8471`.

**Multi-user isolation.** When auth is on (`DP_AUTH_SECRET` set), runs default to the **subprocess
runner** — each run executes in its own OS process, so a user's arbitrary Python (transform / section
scripts) can't crash, hang, or OOM the shared kernel, and a runaway loop can be hard-killed. Paired
with `DP_DATASET_ROOTS`, filesystem access is confined to the allowed roots via DuckDB's native
sandbox — covering every node uniformly, including raw `sql` (`read_csv`/`COPY` can't escape) — when
no object store is configured (`s3://`/`gs://` need network access, which the sandbox disables, so
the two are mutually exclusive). Be honest about the rest of the
boundary, though: subprocesses still run as the **same OS user on the same filesystem**, and the code
"sandbox" is a soft guard, not a security boundary — this is crash/DoS isolation, **not** a
multi-tenant jail. Real tenant isolation needs OS-level sandboxing (containers, per-user accounts, or
a pod/queue `ExecutionBackend` plugin). Open single-user mode stays in-process (trusted + faster);
override either way in Settings → Execution.

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
    return ctx.sql(inputs[0], f'SELECT * REPLACE (upper("{col}") AS "{col}") FROM {{input}}')

def register(reg):
    reg.add_node(SPEC, lower)
```

Restart the server → `uppercase` is in the Add-node menu, typed, wired, previewable, runnable — the
frontend rendered it from `/api/nodes`, with no JS written. A complete, tested example lives in
[`examples/plugins/dp_example/`](examples/plugins/dp_example/); **[docs/PLUGINS.md](docs/PLUGINS.md)**
walks through it and the full SPI. See also `kernel/README.md`.

## Control flow — sections + driver scripts

There are no `branch` / `loop` / `variable` node types. Instead, control flow lives inside a
**`section`**: a composite node whose body is a small **driver script** (Python) that calls its
contained nodes with real `for` / `while` / `if` and an `emit(...)` for the output — so iteration and
branching are just code over typed nodes, bounded and inspectable. This is the as-built model that
replaced the original branch/loop nodes.

## Keyboard shortcuts

`⌘Z` / `⌘⇧Z` (or `⌘Y`) undo / redo · `⌘A` select all · `⌘C` / `⌘X` / `⌘V` copy / cut / paste ·
`⌘D` duplicate · `Delete` remove · `B` bypass · `D` disable · `Esc` clear selection or close a panel.
Click a node's **output port** to open the connect menu (or drag to wire).

## Develop

```bash
make setup     # kernel deps (uv) + sample data + web deps (npm)
make run       # dataplay: build SPA + serve SPA+API on :8471, open browser
make dev-web   # optional: Vite hot-reload on :5173 (proxies /api -> the kernel)
make test      # kernel end-to-end tests (real engine on real files)
make e2e       # browser end-to-end tests (Playwright on the real UI)
```

## The agent

The agent is an actor: describe an outcome and it **builds real, inspectable, typed nodes** on the
canvas. It is **provider-agnostic** — a server-side tool-use loop run in-process by
[Pydantic AI](https://ai.pydantic.dev) (LiteLLM is used only to detect provider keys + parse the
model string), so you point it at whatever model you have (the key lives in the kernel, never the
browser):

```bash
uv pip install -e 'kernel[agent]'     # from a clone (not yet on PyPI)

# pick a provider via DP_AGENT_MODEL + its key:
export DP_AGENT_MODEL=anthropic/claude-opus-4-8   && export ANTHROPIC_API_KEY=sk-ant-...   # default
# export DP_AGENT_MODEL=openai/gpt-4o             && export OPENAI_API_KEY=sk-...
# export DP_AGENT_MODEL=gemini/gemini-1.5-pro     && export GEMINI_API_KEY=...
# export DP_AGENT_MODEL=ollama/llama3             && export DP_AGENT_BASE_URL=http://localhost:11434  # local, no key
```

The agent is optional: with no `DP_AGENT_MODEL` configured, the dock shows "Agent unavailable" and
the rest of the app works unchanged — there is deliberately no rule-based stand-in that pretends to
be an LLM. Everything else (build graphs, run, preview) works fully offline without it.

## License

Apache-2.0 — permissive, for adoption and commercial embedding. The engine deps (DuckDB, Polars,
Arrow, Lance) are all MIT/Apache/BSD. Organization-specific backends (a managed catalog, a cluster
runner, private model pipelines) are an optional plugin pack, never a dependency of the core.
