# Data Playground kernel

The kernel is the Python backend for Data Playground.  In the default local
setup, one FastAPI process serves the built web app, the HTTP and WebSocket
APIs, and the local execution engine.  It uses SQLite and the local filesystem
by default; PostgreSQL, object storage, Ray, Kubernetes, and agent integration
are optional deployment or extension paths.

For the product overview and the normal quick start, begin at the
[repository README](../README.md).  This page is the backend contributor's
map: where a request enters, where a canvas is executed, and where to make a
change without treating the package layout as public API.

## Run and test

From this directory:

```bash
uv sync --extra dev
uv run python -m hub.seed
uv run dataplay --workspace "$PWD" --port 8471
uv run pytest -q
```

`dataplay` creates the workspace and seeds sample data when needed.  The
repository root also provides `make run`, `make test`, `make e2e`, and the
fast pre-submission gate `make preflight`; see
[Contributing](../.github/CONTRIBUTING.md) for the complete development loop.

## Start here

| Need | Entry point | Notes |
| --- | --- | --- |
| Start the application or run a saved canvas headlessly | [`hub/cli.py`](hub/cli.py) | Implements the `dataplay` command, including `mcp` and headless runs. |
| Add or trace an HTTP, WebSocket, or SPA-serving route | [`hub/main.py`](hub/main.py), [`hub/routers/`](hub/routers/) | `main.py` creates the FastAPI app and mounts routers and the built SPA. |
| Understand startup composition and plugin loading | [`hub/deps.py`](hub/deps.py) | Builds the default dependencies and discovers plugin packs. |
| Change graph validation, planning, placement, or run lifecycle | [`hub/compiler.py`](hub/compiler.py), [`hub/planner.py`](hub/planner.py), [`hub/run_controller.py`](hub/run_controller.py) | Keep admission, execution ownership, and persisted run state aligned. |
| Change built-in node semantics or preview/full execution | [`hub/nodespecs.py`](hub/nodespecs.py), [`hub/executors/`](hub/executors/) | The engine builds lazy DuckDB relations; preview has explicit bounded-input rules. |
| Add a file format, execution backend, catalog behavior, or node capability | [`hub/plugins/`](hub/plugins/), [`hub/backends.py`](hub/backends.py), [`hub/sdk.py`](hub/sdk.py) | Core extension seams; the public contract is documented separately. |
| Change metadata models or schema | [`hub/metadb.py`](hub/metadb.py), [`hub/migrations/`](hub/migrations/) | Add a forward Alembic revision; do not edit historical migrations. |
| Find the nearest automated contract | [`hub/tests/`](hub/tests/) | Tests use isolated metadata storage by default; focused tests sit beside their domain. |

## Execution path

A browser, MCP client, or headless invocation reaches the same core flow:

1. `hub/main.py` or `hub/cli.py` establishes the workspace and dependencies.
2. A route or command validates the canvas graph and asks the planner to choose
   an admissible execution path.
3. `hub/run_controller.py` creates and owns the run state; the selected backend
   executes the graph.
4. `hub/executors/engine.py` turns built-in nodes into lazy DuckDB relation
   operations or bounded Arrow-batch transforms.  Dataset adapters and sinks
   handle reads and writes.
5. Metadata, catalog facts, outputs, and run status are persisted through the
   metadata and storage layers so the UI and API can report the same result.

The default runner is deliberately local and out-of-core.  Distributed runners
are optional backends with their own support and validation boundaries; do not
infer their availability from the local execution path.

## Boundaries worth preserving

- Keep transport concerns in `main.py` and `routers/`; put reusable execution
  and data semantics behind the kernel and backend interfaces.
- Keep the default product usable offline.  Provider-specific catalogs,
  destinations, runners, and model integrations belong in plugin packs rather
  than in the core.
- Treat run identity, output ownership, dataset revisions, and catalog lineage
  as persisted facts.  A UI convenience must not manufacture or overwrite
  those facts.
- Migrations move forward only.  When repairing old data, add a new repair
  migration rather than changing a revision that users may already have run.

## Owning references

- [HTTP API contract](../docs/API.md) — generated OpenAPI snapshot and its
  drift check.
- [Plugin guide](../docs/PLUGINS.md) — supported extension seams, manifests,
  discovery, and reference plugins.
- [Catalog guide](../docs/CATALOG.md) — catalog, lineage, and provider-facing
  behavior.
- [Versioned data and durable execution](../docs/VERSIONED_DATA_AND_DURABLE_EXECUTION.md)
  — current revision/write/task guarantees and remaining limits.
- [Ray](../docs/RAY.md) and [Ray Jobs](../docs/RAY_JOBS.md) — distributed
  execution support boundaries and production gates.
- [CI and release gates](../docs/CI.md) — which checks run where and why.
- [Supported deployments and trust model](../docs/SUPPORT.md) — supported
  operator profiles and responsibilities.
