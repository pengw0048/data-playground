# Supported local merge-columns acceptance — issue #585

## Scope and identity

- Underlying product commit audited: `b48be6f28fb14a505ffae79b7a3155516d0b7c5b` (`feat: run certified column merges from Write inspector (#612)`), the released #583/#607/#584 product path on `main`.
- Acceptance evidence commit: `ffb9ab43c6a0460a6dcf00a1682747f515a008f8`. This immutable parent contains the exact executable tests, fixture contract, and audit content exercised by the focused commands. The enclosing child changes only this identity metadata, so the reference is stable and non-self-referential.
- Fixture profile: `sqlite-local-managed`; the static sanitized contract is [fixture-manifest.json](fixture-manifest.json).
- The fixture is a three-row, full-width managed-local base with logical identity `id`, two untouched columns, a replacement payload, and an added payload. Checksums cover logical rows and columns only.

## Focused evidence

| Contract | Command | Result |
| --- | --- | --- |
| SQLite/local durable merge | `cd kernel && uv run pytest -q hub/tests/test_merge_columns.py hub/tests/test_merge_columns_api.py` | Passed: 52 passed, 2 PostgreSQL-boundary tests skipped without `DP_TEST_DATABASE_URL` (16.36s). |
| Web static checks | `cd web && npm run typecheck && npm test -- --run && npm run build` | Passed: type-check; 53 files / 595 tests; production build (7.60s for unit tests). |
| Shipped SPA journey | `cd web && npx playwright test e2e/merge-columns.spec.ts --project=chromium-ux-smoke` | Passed: 1 browser test (6.1s), including cleanup of its managed-local catalog binding and backend selection. |
| Static checks | `make preflight` | Passed: artifact hygiene, Ruff, basedpyright (0 errors), OpenAPI check, and 139 core-contract tests (10.85s). |

The kernel golden verifies add and replace, output schema order, exact equality of untouched columns, a sidecar containing only logical identity plus selected payload, immutable base/final reopen, same-submission durable replay, one receipt, and exactly one new revision. Its invalid sidecar counterpart verifies no publication and no revision.

The browser journey creates a normal managed-local base, saves an exact Source → Select → Write canvas, and uses only the shipped Write Inspector to check and run the merge. It reopens the durable task in Jobs and both base and published exact revisions through normal APIs.

## Measured profile and boundaries

- Measured fixture scale: 3 base rows, 4 base columns, 3 sidecar rows, 3 sidecar columns, and 5 final columns. Counts and value-only checksums are recorded in the manifest and asserted by the kernel golden.
- Supported profile exercised: SQLite metadata with local filesystem managed-local publication.
- Existing PostgreSQL boundary remains the focused, opt-in `DP_TEST_DATABASE_URL` merge test coverage; this audit does not add a database matrix.
- Not claimed or exercised: buckets, distributed execution, Ray, provider writes, external/shared artifacts, or organization-specific provider workflows.

This is release evidence only. It does not introduce product behavior, workflow YAML, CI matrices, accessibility expansion, or a generic release dashboard.
