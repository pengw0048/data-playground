# Exact-revision researcher workflow certification — issue #282

## Scope and identity

- Product commit certified: `dbc872f4b8a9a99666ad857fb9f3c4a1c109ab79` (`Stabilize object result cache wait (#388)`).
- Execution: 2026-07-17 on macOS 26.5.1 (25F80), Python 3.12.13, uv 0.10.10, Node 26.5.0, npm 11.17.0, and Playwright 1.61.1.
- Workspaces: backend cases used per-test temporary paths and metadata stores; the browser matrix deleted and rebuilt its SQLite DB and full fixture workspace; the PostgreSQL drill used an isolated disposable PostgreSQL 18 container and removed it after the run.
- Machine-readable capability and evidence record: [conformance.json](conformance.json).
- Full-fixture manifest SHA-256: `3bbaab41ae122661ff71a2eb0f7d0de5ac1abbc2768665953d7d405c74748b0d`.
- Integrated [CI](https://github.com/pengw0048/data-playground/actions/runs/29556678473), [CodeQL](https://github.com/pengw0048/data-playground/actions/runs/29556678454), and [Secret scan](https://github.com/pengw0048/data-playground/actions/runs/29556678429) identify this exact `main` commit and passed before publication.

## Supported boundary

The default local product supports admitted exact-revision manifests through its in-process, Kernel, and same-host Subprocess transports. Built-in Lance provides bounded history plus latest, exact, and UTC as-of selection. Core-managed immutable files provide latest/exact reads under core retention.

Ray and Ray Jobs do not claim admitted-manifest transport. #302 and #303 remain deferred; exact-revision runs routed to those optional placements fail before controller/run/attempt identity, worker, artifact, envelope, driver, or remote-job allocation. The fail-closed boundary is documented in `docs/RAY.md`, `docs/RAY_JOBS.md`, and `docs/PLUGINS.md` and covered by `test_admitted_input_manifest_transport.py`.

## Required journeys

| Journey | Evidence | Result |
| --- | --- | --- |
| Browse Lance history, inspect exact, pin, preview/profile/run, reopen, inspect admitted inputs | Real-Lance router/inspector tests, Source/history Playwright, golden run/reopen Playwright, and Run History component coverage | Passed |
| Concurrent append between selection, preview, admission, and explicit refresh | Pinned/as-of reload, drift reporting, preview-manifest reuse, and same-submission adoption tests | Passed; no silent latest substitution |
| Multiple Sources with deterministic manifest order | Inspector/cache identity and local admission manifest contract tests | Passed |
| Provider-owned and core-owned retention/loss | Provider unavailable/permission/compaction tests plus managed-file leases/GC tests | Passed |
| Unregister/re-register same path | Stable tombstone and same-path ABA tests | Passed; identity was not rebound |
| Restart across resolution, admission, dispatch, and publication | DB restart, isolated-local revalidation, response-loss adoption, and publication recovery tests | Passed |
| Back up and restore revision identity/evidence | SQLite/local-file and PostgreSQL `pg_dump`/`pg_restore` drills | Passed |
| Optional transports before allocation | Direct runner and placed-controller capability-probe adversarial tests | Passed fail-closed |

## Validation

- `uv run pytest -q hub/tests/test_dataset_revisions.py hub/tests/test_local_run_input_admission.py hub/tests/test_local_run_input_transport.py hub/tests/test_managed_local_file_revisions.py hub/tests/test_admitted_input_manifest_transport.py hub/tests/test_backup_restore_drill.py hub/tests/test_migrations.py` — 82 passed; the one PostgreSQL environment skip was superseded by the dedicated run below.
- `DP_TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest -q -s hub/tests/test_backup_restore_drill.py::test_postgres_object_store_isolated_restore_drill` with PostgreSQL 18 and matching `pg_dump`/`pg_restore` — 1 passed. Evidence reported the core revision verified, the retained provider revision available, the intentionally removed provider revision unavailable, and namespace isolation applied.
- `npm test` — 38 files, 396 tests passed.
- `npm run typecheck` — passed.
- `npm run build` — passed.
- `CI=true DP_E2E_FIXTURE_PROFILE=full npm run e2e` — 86 Playwright tests passed with one worker against a freshly rebuilt full workspace, including 1280×720 and 1440×900 projects.
- After advancing the certified commit through #388, `uv run pytest -q hub/tests/test_object_lifecycle.py` — 116 passed, 17 skipped; #388 changed only this test's result-cache wait bound.

## Browser evidence

The new browser adversarial case keeps one exact Source binding unchanged while its provider returns a normalized 503, tells the researcher that latest was not substituted, and retries that same identity successfully. Both screenshots are 1280×720, the documented minimum supported viewport.

- [Provider offline; exact binding preserved](screenshots/1280x720-provider-offline.png), SHA-256 `b7f012bd1abae5a6ac9a17fa9b1e2da6809fe2528ece7c6b1f7bce583a6b03ec`.
- [Same exact binding recovered](screenshots/1280x720-provider-recovered.png), SHA-256 `4cf656f88b4e814cad9b809ce5c26c75c546f3b76f0dcc1efb21f5d3b233e0aa`.

The first full browser run exposed an evidence-test defect rather than a product defect: prior serial cases can create enough canvases to push the target dataset beyond the Workspace first page. The exact-history case now uses the existing bounded pagination helper. The complete matrix then passed without retry.

## Findings and tradeoffs

- [#383](https://github.com/pengw0048/data-playground/issues/383) was the only product blocker found. It was fixed and closed by the certified commit before this audit resumed.
- The Workspace first-page assumption was corrected in this evidence change; it did not affect product behavior.
- No further exact-revision correctness, data-integrity, recovery, or supported-viewport finding reproduced.

This certification intentionally does not implement #302/#303, Workspace hierarchy, typed writes, durable tasks, sparse merge, robotics acceptance, mobile support, or hostile-tenant isolation. It certifies the supported local-workstation and trusted-team exact-revision profile only.
