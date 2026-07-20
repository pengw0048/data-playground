# Default fresh-workspace write journey acceptance — issue #635

## Scope and identity

- Underlying product commit audited: `dc96425fb8010ac690fc01bc3edc2623540b3955` (`main` after the #631 wave: #632 kernel managed writes, #633 Lance append contract, #634 pre-dispatch terminalization).
- Backend under test: the shipped **default** execution backend — the per-canvas kernel — on unmodified settings. The e2e webServer sets no `DP_EXECUTION`, so the journey runs on the default kernel; managed-local create/replace and Lance append are published by the certified durable-Task owner.
- Fixture profile: `full` (scheduled/on-demand). The sanitized fixture contract is [fixture-manifest.json](fixture-manifest.json); the per-viewport console/error review is [visual-review.json](visual-review.json).
- Environment: macOS 26.5.2, Node v26.5.0, uv 0.10.10, Playwright 1.61.1.

## Executed journeys

| Journey | Certified path | Result |
| --- | --- | --- |
| Golden default-kernel write | Workspace discovery → Source (`events`) → typed `select` transform → Write → managed revision + receipt (Write Inspector) → Jobs evidence → Inbox → exact-revision reopen | Passed |
| Unknown destination error path | Write to an unknown `destId` → typed `400 unknown destination '…'` from both `/api/run/write-admission` and `/api/run` (never a 500); the UI contains it without crashing (app shell alive, the Write card never certifies the unresolved destination) | Passed |
| Managed Lance append retry | Register the pre-seeded Lance dataset → append (managed-local-lance) → replay the same submission → converge on exactly one appended version | Passed |
| Hub-restart recovery | Submit a managed write to a spawned hub, SIGKILL the process group, restart on the same DB → the durable owner recovers the run to a terminal receipt, visible in the reloaded Jobs surface | Passed |

## Machine validation

| Contract | Command | Result |
| --- | --- | --- |
| Golden journey + error paths + restart, full profile | `cd web && DP_E2E_FIXTURE_PROFILE=full npx playwright test default-write-journey.spec.ts --project=chromium` | Passed: 4 browser tests. |
| Static gates | `make preflight` | Passed: artifact hygiene, Ruff, basedpyright, OpenAPI check, core-contract. |

The spec runs under the scheduled/on-demand acceptance policy (docs/CI.md), gated on the `full` fixture profile like `ux-full-matrix.spec.ts`, so the required per-PR e2e job skips it and the daily `ux-acceptance.yml` workflow exercises it. Traces are retained on first-retry per the shared Playwright config; the workflow uploads the report, `web/test-results`, and the fixture workspace as artifacts.

## Visual review

Named 1440×900 screenshots (light and dark) of the certified surfaces are in [screenshots/](screenshots/):
`1440x900-light-canvas.png`, `1440x900-light-jobs.png`, `1440x900-light-revision.png`, and their `dark` counterparts. Each captured surface recorded zero console or page errors ([visual-review.json](visual-review.json)). Human review of these images is required before closing #635 / #631.

## Findings

- **Filed separately (P1, #649):** re-admitting an already-published managed-local Lance append (`POST /api/run/write-admission` with the same lineage after the head advanced) raises an unhandled `RuntimeError: managed local write idempotency key collision` → HTTP 500, where the parquet path returns a recovered receipt. Data integrity is unaffected — the run itself converges on one version — so the golden journey retries via the cached intent.

## Boundaries

Not claimed or exercised: per-PR CI expansion, performance measurement, non-default backends/profiles, multi-user, buckets, distributed execution, or provider writes. This is release evidence only; it introduces no product behavior.
