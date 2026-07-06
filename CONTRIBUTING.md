# Contributing

Thanks for helping build Data Playground — an open-source, node-based canvas for data ("ComfyUI, but
for typed columnar data"). It's provider-agnostic, offline-capable, and extensibility-first.

## Layout

```
kernel/   FastAPI app + the out-of-core engine (DuckDB · Polars · Arrow · Lance). One server serves
          the SPA + API + WebSocket + engine. Tests in kernel/kernel/tests/. Benchmark in kernel/bench/.
web/      React + Vite + Zustand + React Flow + shadcn/ui. Renders ANY node from /api/nodes.
docs/     PRD, CONTROL_FLOW, TESTING, BENCHMARK, PLUGINS. FEATURES.md (repo root) is the feature tree.
examples/ Runnable example plugins (start here to extend the kernel).
```

## Setup & the loop

Requires Python 3.11–3.12 (via [uv](https://docs.astral.sh/uv/)) and Node 20+.

```bash
make setup      # kernel deps (uv) + sample data + web deps (npm)
make run        # build the SPA + serve everything on http://localhost:8471
make dev-web    # optional: Vite hot-reload on :5173, proxying /api → the kernel
```

Before you push, run the same checks CI does:

```bash
make test       # kernel end-to-end tests (real engine on real files) — cd kernel && uv run pytest -q
make e2e        # browser tests (Playwright drives the real UI) — see docs/TESTING.md
cd web && npm run build   # tsc -b && vite build (an invalid type / stale dist fails here)
make bench      # optional: the out-of-core validation harness (docs/BENCHMARK.md)
```

Note: `make e2e` rebuilds the SPA first — the kernel serves `web/dist`, so a stale `dist` makes E2E
test the wrong build. Rebuild after switching branches.

## Conventions

- **Minimal, in-style changes.** Match the surrounding code's naming, comment density, and idioms.
  Don't add helper layers, speculative config, or special-casing that isn't needed.
- **Tests with behavior changes.** A kernel change gets a `pytest` case; a UI-interaction change gets
  a Playwright case. Tests encode the invariant so a regression fails CI, not the user.
- **Type-safe + typed wires.** Connection validity is enforced on both sides (canvas + kernel) — keep
  it that way; don't loosen a wire type to make something connect.
- **Be honest in docs and status.** If something is partial or by-design-omitted, say so (see
  `FEATURES.md` for the tone). Don't claim a capability that isn't verified.

## Extending the kernel (plugins)

The most common contribution is a **plugin** — a node, adapter, runner, capability, catalog, or
importer — with no core edit. Read **[docs/PLUGINS.md](docs/PLUGINS.md)** and copy
[`examples/plugins/dp_example/`](examples/plugins/dp_example/) as a starting point. If it's broadly
useful, a PR that adds it under `examples/` (with a test like `test_example_plugin_loads_and_runs`)
is welcome.

## Pull requests

- Branch off `main`; keep the PR focused on one change.
- Green `make test` + `make e2e` + `npm run build` locally before opening the PR.
- Describe what changed and why; call out any deliberate non-fix or known limitation.
