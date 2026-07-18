# AGENTS.md

## Cursor Cloud specific instructions

Data Playground is a **single product** with two build components managed from the root `Makefile`:

- **Kernel** (`kernel/`) — Python 3.12 / FastAPI backend + execution engine (DuckDB, Polars, Arrow) + the `dataplay` CLI. Managed by **uv** (`uv.lock`).
- **Web** (`web/`) — React + TypeScript + Vite SPA. Managed by **npm** (`package-lock.json`). Built to `web/dist` and served by the kernel.

By default the whole product runs as **one process** on `http://127.0.0.1:8471`. The metadata DB (SQLite) and storage (local filesystem) are embedded, not separate services. Postgres, object storage/MinIO, Ray, Kubernetes, and an LLM agent are all optional scale-out add-ons and are not needed for local development or testing.

### Running / building / testing

Standard commands live in the root `Makefile`, `web/package.json`, and `kernel/pyproject.toml`. Key ones:

- Run the app: `make run` (builds the SPA, then serves API + engine + SPA on `:8471`). Or `cd kernel && uv run dataplay --workspace /workspace/kernel --port 8471`.
- Backend autoreload dev: `make dev-kernel` (uvicorn on `:8471`).
- Frontend HMR dev: `make dev-web` (Vite on `:5173`, proxies `/api` + `/ws` to `:8471`).
- Kernel tests: `make test` (`cd kernel && uv run pytest -q`).
- Web unit tests: `cd web && npm test` (vitest).
- Web type-check: `cd web && npm run typecheck` (`tsc -b`).
- Browser E2E: `make e2e` (Playwright; needs `make e2e-install` once to fetch the chromium browser).
- Seed sample data: `make seed`.
- **Before opening/updating a PR: `make preflight`** (~15s). A fast static gate — artifact hygiene, ruff, basedpyright, the OpenAPI contract, and migrations/core-contract — that catches the mechanical CI misses cheaply. It intentionally does NOT run the full kernel suite, web, or PostgreSQL; push and let CI run those heavy suites.

### Non-obvious caveats

- **`uv` is not a repo file** — it lives at `~/.local/bin/uv` (installed during environment setup) and is on `PATH` via `~/.bashrc`/`~/.profile`. The update script refreshes dependencies with it.
- **Build ordering:** `web/dist` is gitignored, so a fresh clone lacks it. The kernel wheel force-includes `../web/dist`, so `web/dist` must exist before `uv sync` (a `hatch_build.py` hook creates an empty one, and `make setup` does `mkdir -p web/dist` first). `make run` rebuilds `web/dist` for real before serving.
- **Static gates:** kernel uses `ruff` + `basedpyright` (the `python-quality` CI job; `make lint`) plus `pytest` with `error::DeprecationWarning:hub.*` treated as errors; web uses `tsc`. `make preflight` runs them all. There is no black/eslint.
- **First run auto-seeds** sample data (`events.parquet`, `movies.csv`, `images.parquet`) and creates a default canvas; the DB migrates on startup via Alembic.
- **`dataplay` tries to auto-open a browser** on startup. In a headless VM this prints harmless `dbus`/`gpu`/`gcm` Chrome errors to the log — the server itself is fine (check `GET /api/livez` → `{"ok":true}`).
- E2E (`npm run e2e`) boots its own `dataplay` instance on `:8899` with a throwaway SQLite DB, so it does not collide with a `make run` server on `:8471`.
