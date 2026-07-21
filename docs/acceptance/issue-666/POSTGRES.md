# Issue #666 — keyed-upsert acceptance on the shared PostgreSQL profile

Closes the "browser journey ran on SQLite only" gap at the acceptance-suite level: the existing
`keyed-upsert-journey.spec.ts` (issue #639) and `default-write-journey.spec.ts` (issue #635) run,
unforked, against a hub whose metadata database is **PostgreSQL 16** (CI service
`postgres:16-alpine`; local validation used PostgreSQL 16.14). The specs are unchanged in what they
assert — the metadata backend is swapped by harness parameterization only.

## How the variant is wired

The `postgres-journey` job in [`ux-acceptance.yml`](../../../.github/workflows/ux-acceptance.yml) boots
the shared kernel on a live Postgres server and runs the two journeys under the `full` fixture profile:

- `DP_E2E_DATABASE_URL` — the shared kernel's metadata DB. `playwright.config.ts` reads it (default
  stays the throwaway SQLite file), adds the `psycopg` extra, and runs an explicit `dataplay migrate`
  before boot. A local SQLite DB auto-migrates on first run; a production Postgres DB does not, so the
  migrate is a required up-front step.
- `DP_E2E_RESTART_DATABASE_URL` — the metadata DB for the SIGKILL restart-recovery test's own spawned
  durable owner. The keyed-upsert restart test reads it (default stays SQLite) and, because a live
  Postgres server persists across the restart, resets it to a clean migrated schema at the start of
  each attempt — the isolation the throwaway SQLite file gets for free from `rm -rf`.

The job runs `playwright test --no-deps --project=chromium` on the two spec files only, so it neither
runs the `@ux-smoke` dependency suite nor expands any per-PR gate. It stays a scheduled/on-demand tier.

## Coverage

| Spec | On Postgres |
| --- | --- |
| `keyed-upsert-journey.spec.ts` — Write-Inspector journey vs. recomputed evidence | Yes (shared hub) |
| `keyed-upsert-journey.spec.ts` — headless API parity + response-loss replay | Yes (shared hub) |
| `keyed-upsert-journey.spec.ts` — **SIGKILL hub-restart recovery** | Yes (dedicated Postgres owner) |
| `default-write-journey.spec.ts` — Source → transform → Write → revision → Jobs/Inbox | Yes (shared hub) |
| `default-write-journey.spec.ts` — typed 4xx unknown destination | Yes (shared hub) |
| `default-write-journey.spec.ts` — managed Lance append retry converges | Yes (shared hub) |
| `default-write-journey.spec.ts` — managed-write hub-restart recovery | No — its own spawned owner stays SQLite |

`default-write-journey.spec.ts` runs unchanged: its first three tests exercise the managed-write
durable path on the Postgres shared hub. Its fourth test spawns its own restart hub on SQLite; pointing
that one at Postgres would be a further spec change, so it is deferred per the issue's guidance. The
Postgres-backed durable-owner SIGKILL restart recovery is already certified by the keyed-upsert restart
test above, which is the same risk class.

## Local validation (PostgreSQL 16.14)

Run against a local Postgres cluster with databases `dp_e2e` and `dp_e2e_restart`:

- Both journeys together (`keyed-upsert-journey.spec.ts default-write-journey.spec.ts`): **7 passed**,
  including the Postgres-backed keyed-upsert restart recovery.
- Retry safety: the keyed-upsert journey ran twice back-to-back against the same (non-fresh) Postgres
  databases and stayed green — the restart test's per-attempt schema reset isolates a retry.
- SQLite regression: the same spec with no Postgres environment variables still passed on the default
  SQLite path, confirming the harness change is inert outside the Postgres variant.

Live per-run evidence (Playwright report, screenshots, `visual-review.json`) is uploaded by the
`postgres-journey` job as the `ux-acceptance-postgres-*` artifact.
