# Contributing to Data Playground

Thanks for helping out. Data Playground runs locally with embedded metadata and storage. Keep changes
focused, and keep provider-specific integrations behind a plugin boundary rather than in the core.

## Dev loop

```bash
make setup     # kernel deps (uv) + sample data + web deps (npm) — one time
make run       # build the web app + serve it with the API on :8471, opens the browser
make dev-kernel # optional: autoreloading kernel on :8471
make dev-web   # optional: Vite hot-reload on :5173 (proxies /api → the kernel)
make test      # kernel tests (real engine on real files)  ·  cd kernel && uv run pytest -q
make e2e-install # one time: install the Playwright browser
make e2e       # browser end-to-end tests (Playwright on the real UI)
```

For a focused web change, run from `web/`: `npm run typecheck`, `npm test`, and `npm run build`.
After that build, a focused `npm run e2e -- --project=... path/to/spec.ts` serves that exact built SPA;
the Playwright server builds a disposable packaged-kernel wheel from it, so no manual uv cache cleanup is needed.
For a focused kernel test, use `cd kernel && uv run pytest -q path/to/test.py::test_name`.

To reproduce the kernel dependency environment without changing the lockfile, use
`cd kernel && uv sync --extra dev --frozen`, then `uv run --no-sync pytest -q`.

## Development version identity

Development branches carry a distinct `X.Y.Z-dev.N` identity in package manifests; Python package
metadata normalizes it to `X.Y.Z.devN`. This marks source builds as development snapshots and does
not cut or promise a release. Release closeout replaces the suffix with the exact clean tag version
across the release-owned manifests; `scripts/check_release_versions.py --release` rejects a
development identity for a tagged release.

Metadata schema changes add a new forward Alembic migration from the current head — a linear chain
rooted at `kernel/hub/migrations/versions/0001_schema_baseline.py` — and extend its fresh-database
tests (`kernel/hub/tests/test_migrations.py` pins the full chain and every migration's content hash).
Never use `Base.metadata.create_all()` as a migration substitute. Databases created before
`0001_schema_baseline` are unsupported: recreate the workspace/SQLite database or PostgreSQL schema;
there is no backfill path into the baseline.

## Before you open a PR

- `make preflight` is green (~15s). It checks artifact hygiene, ruff, basedpyright, the OpenAPI
  contract, and migrations/core-contract. It intentionally does not run the full kernel suite, web
  checks, browser E2E, or PostgreSQL; run focused evidence locally and let required CI gates and any
  path-owned specialized suites cover the rest.
- New behavior has focused coverage: extend the nearest owning kernel test or browser journey, rather
  than adding everything to one catch-all file.
- Keep the change focused. Prefer the smallest patch that solves the problem; don't expand scope.
- The core stays provider-agnostic and offline-first. Anything specific to one backend, store, or
  vendor belongs in a plugin, not in `kernel/hub/`.

## Dependency constraints

Library dependencies use a verified minimum version and no upper bound. This keeps plugin packs
installable alongside the stacks their downstream users already have; an arbitrary ceiling can make
that co-installation impossible. Do not add upper bounds while updating a dependency floor.

The only exceptions are exact pins with a tested version-specific contract: `duckdb==1.5.4` because
`hub.sqlpolicy` audits that release's JSON AST and function catalog, and
`ray[data,default]==2.56.0` because `dp_ray` validates its private hash-shuffle ABI. Keep those pins
until their corresponding contracts are revalidated for a deliberate patch release update.

## Adding a plugin

Start with the task-first [plugin onboarding guide](../docs/PLUGIN_ONBOARDING.md): choose the
narrowest boundary, run its reference test, then consult the linked SPI contract. The core remains
provider-agnostic; a plugin owns the backend, store, or vendor capability it adds.

## Reporting bugs / ideas

Open an issue (templates provided). For a security-related bug, read
[SECURITY.md](SECURITY.md) first; it explains what can be shared safely in an issue.
