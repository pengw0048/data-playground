# Drive Data Playground from your own agent (MCP)

Data Playground ships an [MCP](https://modelcontextprotocol.io) server, so you can point your **own
Claude Code** (or any MCP client) at your workspace and have it build the whole pipeline for you —
explore the catalog, open a canvas, wire typed nodes, **write the `transform` Python**, preview each
step against real rows, and run it. The canvas it builds shows up in the browser like any other.

This is the mirror image of the [built-in agent](../README.md#the-agent-optional): there the kernel
calls a model; here a model calls the kernel. It needs **no API key and no extra install** — the
server is stdlib-only and talks over stdio.

## Connect it

Run the web app as usual in one terminal (so you have a browser to watch):

```bash
cd kernel && uv run dataplay        # → http://127.0.0.1:8471
```

Then register the MCP server with your client. For Claude Code:

```bash
# from the same workspace directory you serve from
claude mcp add dataplay -- uv run dataplay mcp
```

or point it at any workspace explicitly:

```bash
claude mcp add dataplay -- uv run dataplay mcp --workspace /path/to/proj
```

Now ask your agent things like *"open a canvas, keep only purchase events, total the amount per user,
and save it"* — it will call the tools below to build a real, typed, runnable canvas.

> **Config (all optional).** `--workspace` / `--data-dir` pick the project dir (default: CWD).
> `--base-url` is the URL the web app is served at, used only to build clickable canvas links
> (default `$DP_BASE_URL` or `http://127.0.0.1:8471`). `--user` acts as a specific user id (default:
> the local user). `--no-seed` skips first-run sample data.

## How it fits together

The MCP server shares the workspace's metadata DB, catalog, and storage with the running web app
(the same "several instances, one shared state" model the README describes). So:

- A canvas the agent builds is persisted and appears in the browser's **Files** list. If you already
  have that canvas open, **reload** to pick up the agent's changes — live collaboration is a
  per-web-process room an out-of-process MCP client isn't part of.
- `run_canvas` executes **in-process on the local out-of-core runner** (deterministic, no per-canvas
  kernel spawn). Its output dataset + run history land in the shared stores, so the UI sees them too.

Because the tools reuse the exact catalog, graph-edit, preview, and canvas-CRUD building blocks the
HTTP API and the built-in agent use, behavior can't drift between the three surfaces.

## Tools

| Tool | What it does |
| --- | --- |
| `list_datasets` | Catalog datasets: name, uri, columns (name + type), row count, primary-key candidates. |
| `sample_dataset` | A few real rows of a dataset (by catalog name/id or uri) — see the actual shape. |
| `join_hints` | How two datasets join: key pairs + cardinality **measured** on the data (1:1 / 1:N / …). |
| `list_node_kinds` | Every node kind (built-in + plugin) with its params and ports. |
| `list_canvases` | Canvases you can access, each with a browser url. |
| `create_canvas` | Make a new, empty canvas; returns its id + url. |
| `get_canvas` | A canvas's nodes / edges / url. |
| `add_node` | Add a node (`config` maps param → value; a `source` needs `config.uri`). |
| `connect` | Wire an output into an input (`targetHandle` for a multi-input node like `join`). |
| `set_node_config` | Merge config values into a node. |
| `remove_node` | Delete a node and its edges. |
| `set_transform` | **Write (or update) a `transform` node's Python and immediately preview it** — the author-then-verify loop. |
| `preview_node` | A node's output over a bounded real sample — verify each step (incl. transform code) works. |
| `validate_canvas` | Typed-wire errors + per-join cardinality / fan-out warnings, without running. |
| `run_canvas` | Run up to a sink, out-of-core, and wait for the result (large/unknown runs return `needsConfirm`). |

Datasets and canvases are also exposed as MCP **resources** (`dataplay://dataset/<id>`,
`dataplay://canvas/<id>`) for clients that pull context that way.

## Writing transforms, verified

The point of `set_transform` is the tight loop that lets an agent write correct code without you
touching Python:

1. `sample_dataset` / `preview_node` to see the real columns and values.
2. `set_transform` with a Python cell — for the default `map` mode, `def fn(row): ...` returning the
   row. It's added (and wired to `upstreamNodeId`) and **previewed in the same call**.
3. The result carries the output columns on success, or a human-readable `reason` on a code error —
   so the agent fixes it and calls `set_transform` again with `nodeId` to update in place.

Prefer relational nodes (`filter` / `select` / `sql` / `aggregate` / `join`) when they suffice — they
push down and run out-of-core; reach for `transform` when the logic genuinely needs code.
