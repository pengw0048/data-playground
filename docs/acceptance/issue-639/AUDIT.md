# Keyed-upsert release acceptance — issue #639

## Scope and identity

- Product commit audited: `f338c73d8dd2e08e25299918e86c6344581d757f` (`main` after the #489 upsert wave: #636 keyed-upsert service, #637 HTTP admission + durable lifecycle, #638 Write-inspector UX). This leaf adds acceptance evidence only; it introduces no product behavior.
- Certified path: the shipped `Source → Write` keyed upsert into a managed-local-file dataset, driven only through the shipped Write Inspector and the public HTTP API — no endpoint is mocked. Publication, compare-and-swap, and response-loss recovery stay with the certified durable managed-write owner (#637).
- Fixture profile: `sqlite-local-managed`; the sanitized fixture contract is [fixture-manifest.json](fixture-manifest.json). The per-theme console/error review is [visual-review.json](visual-review.json).
- Environment: macOS 26 / arm64, CPython 3.12.13, DuckDB 1.5.4, Playwright chromium (full fixture profile).

## Executed journeys

`web/e2e/keyed-upsert-journey.spec.ts` (full-profile gated, `@acceptance-keyed-upsert`), against a fresh managed-local base (events slice `id < 3`, ids `{0,1,2}`) and an exact payload revision (`2 ≤ id < 5`, ids `{2,3,4}`), keyed on `id`.

| Journey | Certified path | Result |
| --- | --- | --- |
| Golden inspector upsert | Bootstrap base + payload → Write Inspector `Check eligibility` (projection: `1 matched · 2 inserted · 2 unchanged`, `Eligible keyed upsert`) → `Run keyed upsert` → `Published exact revision` → `Open exact revision` (`Parent` = base head) | Passed |
| Independent evidence | Reopen the immutable base + payload + final revisions through the ordinary revision APIs and recompute matched/inserted/unchanged from the key sets and the head from their union; assert they equal the UI projection (`1/2/2`) and the five-key union `{0,1,2,3,4}` with the payload winning on `id 2` | Passed |
| Headless-API parity | Submit the same intent via `POST /api/catalog/upsert`, poll `GET /api/keyed-upsert/{id}`; the terminal receipt (`parentHead` = base, `childRevisionId` = receipt revision) and evidence counts are identical to the browser run | Passed |
| Response-loss replay | Re-submit the same submission id → the same durable task and child revision; the target keeps exactly two revisions (base + one upserted head) | Passed |
| Hub-restart recovery | Submit the upsert to a spawned hub, SIGKILL the process group, restart on the same DB → the durable owner recovers the task to one terminal receipt (`parentHead` = base) | Passed |

The golden journey verifies against **independently recomputed** expectations, not the inspector's own numbers. Backend invariants (no in-place mutable-head update, key validation, no auto-rebase on a moved head, delete absent, exactly-once reconciliation) are the certified service/API tests `hub/tests/test_keyed_upsert.py` + `hub/tests/test_keyed_upsert_api.py`.

## Machine validation

| Contract | Command | Result |
| --- | --- | --- |
| Acceptance journey + durability + parity, full profile | `cd web && DP_E2E_FIXTURE_PROFILE=full npx playwright test e2e/keyed-upsert-journey.spec.ts --project=chromium --no-deps` | Passed: 3 browser tests. |
| Service + HTTP/durable-lifecycle goldens (incl. Postgres boundary) | `cd kernel && uv run pytest -q hub/tests/test_keyed_upsert.py hub/tests/test_keyed_upsert_api.py` | Passed on SQLite; the one Postgres admission-race test runs with `DP_TEST_DATABASE_URL`. |
| Web static checks | `cd web && npm run typecheck && npm test -- --run` | Passed: type-check; unit suite. |
| Static gates | `make preflight` | Passed: artifact hygiene, Ruff, basedpyright, OpenAPI check, core-contract. |

The acceptance spec runs under the scheduled/on-demand policy (docs/CI.md): gated on the `full` fixture profile like `default-write-journey.spec.ts`, so the required per-PR e2e job skips it and the daily `ux-acceptance.yml` workflow exercises it. Traces are retained on first retry per the shared Playwright config; the workflow uploads the report, `web/test-results`, and the fixture workspace as artifacts.

## SQLite and Postgres

The journey is green from a fresh workspace on SQLite (the e2e default + the kernel goldens). The Postgres boundary is the opt-in `hub/tests/test_keyed_upsert_api.py::test_postgres_submission_serializes_on_the_owner_row` (runs with `DP_TEST_DATABASE_URL`), which certifies that admission serializes on the owner row under a real concurrent revoke/submit race.

## Visual review

Named 1440×900 screenshots (light and dark) of the certified surfaces are in [screenshots/](screenshots/): `1440x900-light-canvas.png`, `1440x900-light-revision.png`, and their `dark` counterparts. The canvas capture shows the shipped `Certified keyed upsert` inspector control and the published exact revision; the revision capture shows the exact-revision detail. Each captured surface recorded zero console or page errors ([visual-review.json](visual-review.json)). Human review of these images is required before closing #639 / #489.

## Findings

No new P0/P1 findings reproduced. One prior boundary is unchanged: the keyed-upsert durable task is dataset-scoped and canvas-less (like restore-revision), so it is not a canvas Jobs row; its evidence, receipt, and exact revision are surfaced on the Write Inspector and through the revision APIs rather than the Jobs list. A canvas Jobs projection would be a separate follow-up, not a release blocker.

## Boundaries

Not claimed or exercised: per-PR CI expansion, provider capability, performance benchmarking, non-`id`/multi-column key matrices beyond the certified goldens, buckets, distributed execution, or provider writes. This is release evidence only.
