# Contributing to Data Playground

Thanks for helping out. This is a generic, offline-first, node-graph data tool — contributions
should keep it that way: no cloud account required to run or test, no provider-specific code in the
core (put that behind a plugin seam — see below).

## Dev loop

```bash
make setup     # kernel deps (uv) + sample data + web deps (npm) — one time
make run       # build the web app + serve it with the API on :8471, opens the browser
make dev-web   # optional: Vite hot-reload on :5173 (proxies /api → the kernel)
make test      # kernel tests (real engine on real files)  ·  cd kernel && uv run pytest -q
make e2e       # browser end-to-end tests (Playwright on the real UI)
```

Frontend checks (from `web/`): `npx tsc --noEmit`, `npx vitest run`, `npm run build`.

Reproduce CI's clean environment locally: `cd kernel && uv sync --extra dev --frozen && uv run --no-sync pytest`.

Data Playground has not published its first release. Until that happens, metadata schema changes edit
`kernel/hub/migrations/versions/0001_schema_baseline.py` and its fresh-database tests directly instead
of accumulating upgrade revisions for unreleased databases. Never use `Base.metadata.create_all()` as a
migration substitute. Once a public tag exists, this policy ends and every schema change requires a new
forward migration from the released head.

## Before you open a PR

- `make preflight` is green. It mirrors the required CI checks — artifact hygiene, ruff, basedpyright,
  the OpenAPI contract, migrations, the full kernel suite, and (when their toolchain is present) web
  and PostgreSQL — so a green run is the same thing CI gates on. `make preflight-fast` is a quick
  static-only tier for iteration; run the full `make preflight` before submitting.
- New behavior has a test. The kernel suite (`kernel/hub/tests/test_kernel.py`) tests against the
  real engine and the real seeded data — mirror the nearest existing test.
- Keep the change focused. Prefer the smallest patch that solves the problem; don't expand scope.
- The core stays provider-agnostic and offline-first. Anything specific to one backend, store, or
  vendor belongs in a plugin, not in `kernel/hub/`.

## Adding a plugin

Plugins extend the tool through public registration seams (`add_node`, `add_adapter`,
`add_destination`, `add_runner`, `set_catalog`, `add_capability`, `add_telemetry_sink`, …) — no
core edits, no frontend code for a typed node. Start from a reference plugin and its test:

- `examples/plugins/dp_example/` — a `redact` compute node (the smallest `add_node`).
- `examples/plugins/dp_upper/` — a node whose DuckDB build + engine-neutral `ir` hook share one
  operator, so it also runs on a distributed backend.
- `docs/PLUGINS.md` — the full SPI reference and a table of all twelve reference plugins, each with
  a copyable test in `kernel/hub/tests/test_kernel.py`.

Drop a plugin folder into `<workspace>/plugins/` and it loads on kernel start.

## Reporting bugs / ideas

Open an issue (templates provided). For anything security-related, see
[SECURITY.md](SECURITY.md) — do not file a public issue.
