# Drive Data Playground from your own agent (MCP)

Data Playground speaks [MCP](https://modelcontextprotocol.io), so you can point your **own Claude
Code** (or any MCP client) at your workspace and have it build the whole pipeline for you — explore
the catalog, open a canvas, wire typed nodes, **write the `transform` Python**, preview each step
against real rows, run it, and read the rows it produced. The canvas it builds shows up in the
browser like any other.

This is the mirror image of the [built-in agent](../README.md#the-agent-optional): there the kernel
calls a model; here a model calls the kernel. No API key required.

## Two ways to connect

There is **one** server (the same tools, the same code); pick the transport that fits.

### HTTP — in the running web app (recommended: watch it build live)

The web app itself serves the MCP endpoint at **`/mcp`**. Point your client at it and every tool
runs on the app's *real* deps, runner, and auth — there is no second engine and no behavior can drift
from the UI, and **an edit shows up live in an open browser tab** (nodes appear as the agent wires
them). Run the app, then:

```bash
cd kernel && uv run dataplay                              # → http://127.0.0.1:8471
claude mcp add --transport http dataplay http://127.0.0.1:8471/mcp
```

Now open a canvas in the browser and ask your agent *"open a canvas, keep only purchase events, total
the amount per user, and save it"* — watch the nodes land as it goes. Because the endpoint is gated
exactly like the rest of the API, this is a **local / open-mode** feature today: a multi-user
deployment (`DP_AUTH_SECRET` set) needs a real token a CLI can't present yet, so use stdio there.

### stdio — standalone, no server (headless / CI)

`dataplay mcp` runs the same server over stdio with **no web app running** — stdlib-only, zero extra
install. Best for scripts, CI, or a no-browser box:

```bash
claude mcp add dataplay -- uv run dataplay mcp                       # workspace = CWD
claude mcp add dataplay -- uv run dataplay mcp --workspace /path/to/proj
```

A canvas it builds is persisted to the shared workspace DB, so it appears in the browser's **Files**
list; if that canvas is already open, **reload** to pick up the changes (an out-of-process client
isn't in the browser's live collab room — that's the HTTP transport's advantage).

> **Config (all optional).** `--workspace` / `--data-dir` pick the project dir (default: CWD).
> `--base-url` is the URL the web app is served at, used only to build clickable canvas links
> (default `$DP_BASE_URL` or `http://127.0.0.1:8471`). `--user` selects which user id the server acts
> as (default: the local user); it is an **identity selector, not an auth boundary** — it does no
> password check, so it assumes whoever can run the command already has workspace access (the local
> single-user model). An unknown id is rejected rather than silently falling back. `--no-seed` skips
> first-run sample data.

## How it fits together

- **No drift.** `run_canvas` starts a run through the *same* code path as the web `POST /run`
  (`runs.start_run`): the same confirm gate on real size (a large full pass returns `needsConfirm`
  until you pass `confirm: true`), the same cost-based placement / capability routing, the same run
  ownership. An agent-launched run behaves identically to a browser-launched one — and over the HTTP
  transport it's the *same process*, so the run is visible in the UI too.
- **Watch it build (HTTP).** When an MCP tool edits a canvas, the app nudges every open browser tab in
  that canvas's collab room to refetch and re-apply — nodes appear live, no reload. (stdio can't do
  this; reload to see its edits.)
- **Long runs are recoverable.** `run_canvas` waits for the result, but a run still going after the
  poll window returns `timedOut: true` with a `runId`; follow it with `run_status` or stop it with
  `cancel_run`. Read the rows a run materialized with `sample_result`.
- **One workspace.** Both transports share the workspace's metadata DB, catalog, and storage, and the
  graph-edit / preview / catalog / canvas-CRUD tools reuse the exact building blocks the HTTP API and
  the built-in agent use — behavior is inherited, not re-implemented.

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
| `run_canvas` | Run up to a sink, out-of-core, and wait for the result (large/unknown runs return `needsConfirm`; a long run returns `timedOut` + `runId`). |
| `run_status` | Poll a run by its `runId` — follow a `timedOut` run to completion. |
| `cancel_run` | Cancel an in-flight run by its `runId`. |
| `sample_result` | Sample the OUTPUT dataset a run materialized (by `runId`) — read what it produced. |

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
