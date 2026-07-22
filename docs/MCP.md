# Drive Data Playground from your own agent (MCP)

Data Playground speaks [MCP](https://modelcontextprotocol.io), so an external client such as Claude
Code can work directly with a workspace. Use it to explore the catalog, open a canvas, wire typed
nodes, write `transform` Python, preview steps against real rows, run the graph, and read the output.
Canvases show up in the browser like any other. Data Playground itself requires no MCP API key,
though an MCP client or a hosted model it uses may require its own credentials.

## Two ways to connect

Both transports use the same server and tools. Pick the one that fits.

### HTTP — watch it build live

The web app serves MCP at `/mcp`. Tools run on the app's real deps, runner, and auth, so behavior
cannot drift from the UI. Edits appear live in an open browser tab. Start the app, then:

```bash
cd kernel && uv run dataplay                              # → http://127.0.0.1:8471
claude mcp add --transport http dataplay http://127.0.0.1:8471/mcp
```

Open a canvas and ask the agent to build a pipeline. The endpoint is gated like the rest of the API,
so HTTP MCP is a local / open-mode feature today. With `DP_AUTH_SECRET` set, a CLI cannot present a
token yet — use stdio in that case.

### stdio — headless / CI

`dataplay mcp` runs the same server over stdio with no web app. No extra install beyond the kernel.
Best for scripts, CI, or a machine without a browser:

```bash
claude mcp add dataplay -- \
  uv --directory /absolute/path/to/data-playground/kernel run dataplay mcp \
  --workspace /absolute/path/to/workspace
```

Use absolute paths; the MCP client may launch the command from another directory. Choose the workspace
that should own the metadata database, catalog, canvases, and outputs.

Canvases persist to the shared workspace DB and appear in Workspace. If a canvas is already open,
reload to pick up stdio edits — an out-of-process client is not in the browser's live collab room.

Optional flags:

- `--workspace` — project directory (default: CWD)
- `--data-dir` — dataset directory (default: `<workspace>/data`)
- `--base-url` — URL used only to build clickable canvas links (default `$DP_BASE_URL` or
  `http://127.0.0.1:8471`)
- `--user` — which user id the server acts as (default: the local user). This is an identity selector,
  not an auth boundary: no password check. Whoever can run the command already has workspace access.
  An unknown id is rejected rather than silently falling back.
- `--no-seed` — skip first-run sample data

## How it fits together

`run_canvas` starts a run through the same path as web `POST /run` (`runs.start_run`): the same
confirm gate on real size (`needsConfirm` until `confirm: true`), the same placement and capability
routing, and the same run ownership. An agent-launched run matches a browser-launched one. Over HTTP
it is the same process, so the run is visible in the UI. The workspace's configured execution backend
applies — the per-canvas kernel by default. For a pure in-process run with no lingering kernel, set
`DP_EXECUTION=local-out-of-core`.

When an MCP tool edits a canvas over HTTP, open browser tabs in that collab room refetch and re-apply.
stdio cannot do that; reload to see its edits.

`run_canvas` waits for the result. If the poll window expires it returns `timedOut: true` with a
`runId`; follow with `run_status` or stop with `cancel_run`. Read materialized rows with
`sample_result`.

Both transports share the workspace metadata DB, catalog, and storage. Graph-edit, preview, catalog,
and canvas tools reuse the same building blocks as the HTTP API.

## Tools

Catalog and discovery:

- `list_datasets` — name, uri, columns (name + type), row count, primary-key candidates
- `sample_dataset` — a few real rows by catalog name/id or uri
- `join_hints` — key pairs and cardinality measured on the data (1:1 / 1:N / …)
- `list_node_kinds` — every built-in and plugin kind with params and ports

Canvas CRUD and edits:

- `list_canvases` — canvases you can access, each with a browser URL
- `create_canvas` — empty canvas; returns id + url
- `get_canvas` — nodes, edges, url
- `add_node` — `config` maps param → value; a `source` needs `config.uri`
- `connect` — wire an output into an input (`targetHandle` for multi-input nodes such as `join`)
- `set_node_config` — merge config values into a node
- `remove_node` — delete a node and its edges
- `set_transform` — write or update a `transform` node's Python and preview it in the same call
- `preview_node` — node output over a bounded real sample
- `validate_canvas` — typed-wire errors and join / fan-out warnings without running

Runs:

- `run_canvas` — run up to a sink out of core and wait (large/unknown runs return `needsConfirm`; a
  long run returns `timedOut` + `runId`)
- `run_status` — poll by `runId`
- `cancel_run` — cancel an in-flight run
- `sample_result` — sample the output dataset a run materialized

Datasets and canvases are also MCP resources (`dataplay://dataset/<id>`, `dataplay://canvas/<id>`) for
clients that pull context that way.

## Writing transforms, verified

`set_transform` is the author-then-verify loop:

1. Use `sample_dataset` / `preview_node` to see real columns and values.
2. Call `set_transform` with a Python cell. For default `map` mode, `def fn(row): ...` returning the
   row. The node is added (wired to `upstreamNodeId`) and previewed in the same call.
3. Success returns output columns; a code error returns a human-readable `reason`. Fix and call again
   with `nodeId` to update in place.

Prefer relational nodes (`filter`, `select`, `sql`, `aggregate`, `join`) when they suffice — they push
down and run out of core. Use `transform` when the logic needs code.
