# Researcher UX baseline audit — issue #317

## Scope and identity

- Product commit audited: `09339e9634a41660bd2f88f2ddc9c5c0803675d7` (`origin/main`, `Use atomic dirty-only Settings saves (#286)`).
- Audit execution: 2026-07-16 on macOS 26.5.1 (25F80), Node 26.5.0, npm 11.17.0, uv 0.10.10, Python 3.12.13 in the uv environment, Chromium supplied by Playwright 1.61.1.
- Workspace: a clean disposable local workspace; fixture profile `full` and SQLite metadata database were rebuilt for the run.
- Fixture manifest: [fixture-manifest.json](fixture-manifest.json), SHA-256 `3bbaab41ae122661ff71a2eb0f7d0de5ac1abbc2768665953d7d405c74748b0d`.
- Retained remote diagnostics: [UX acceptance run 29513425571](https://github.com/pengw0048/data-playground/actions/runs/29513425571). The `full researcher workflow matrix` passed in 3m28s and uploaded its Playwright HTML report, test results, fixture manifest, disposable workspace, and SQLite diagnostics (14-day retention). The overall workflow remains failed only because its separate P0/P1 issue gate correctly reports #284 and #285 as open.

## Executed journeys and outcome

| Journey | Evidence | Result |
| --- | --- | --- |
| First use | Full matrix creates/reopens canvases, uses seeded data, and runs source/preview workflows. | No new finding. |
| Truthful inspection | Golden workflow proves bounded preview export; stale graph work invalidates old output; catalog matrix exercises inspection labels. | No new finding. |
| Discovery | Full catalog search/paging, table detail, lineage and relationship rendering ran against the 120-dataset and relationship-dense fixtures. | No new finding. |
| Execution | Golden workflow runs a graph, observes completion, reopens history, and downloads the native Parquet artifact. | No new finding. |
| Failure and recovery | Deterministic slow, unavailable, permission-denied, stale-reference, partial-failure, retry, and cancellation paths ran. | No new finding. |
| Settings/admin | Settings surface and current staged-save behavior ran; known dirty-dismissal and typed-default defects are recorded below rather than hidden. | Existing findings #284 and #285. |
| Read-only role | Existing end-to-end role/control coverage ran in the same full matrix. | No new finding. |

## Machine validation

- `cd web && npm run build` — passed.
- `cd web && CI=true DP_E2E_FIXTURE_PROFILE=full npm run e2e` — passed: 72 Playwright tests, one worker, clean workspace. `web/test-results/.last-run.json` reported `status: passed` with no failed tests. The same full matrix also passed remotely on the audited SHA.
- `cd web && npm test` — passed: 29 files, 286 tests.
- `cd kernel && uv run pytest -q hub/tests/test_ux_release_gate.py` — passed: 2 tests.

The full matrix uses the existing #192 fixture/workflow foundation. No audit-specific product test framework or CI path was added. A failed run would retain console/page errors and first-retry traces; none were generated in this successful local run.

## Visual review

The Canvas, Catalog table-detail, and Settings surfaces were opened from the running product at each required desktop viewport/theme. The manual review found no clipping, overlap, blocked scrolling/reachability, misleading hover-only action, or missing visible failure/recovery state beyond the known Settings issues.

- [1440×900 light canvas](screenshots/1440x900-light-canvas.png), [catalog](screenshots/1440x900-light-catalog.png), [settings](screenshots/1440x900-light-settings.png)
- [1440×900 dark canvas](screenshots/1440x900-dark-canvas.png), [catalog](screenshots/1440x900-dark-catalog.png), [settings](screenshots/1440x900-dark-settings.png)
- [1280×720 light canvas](screenshots/1280x720-light-canvas.png), [catalog](screenshots/1280x720-light-catalog.png), [settings](screenshots/1280x720-light-settings.png)
- [1280×720 dark canvas](screenshots/1280x720-dark-canvas.png), [catalog](screenshots/1280x720-dark-catalog.png), [settings](screenshots/1280x720-dark-settings.png)

`visual-review.json` records that the four visual-review sessions captured no console or page errors.

## Findings

No new reproducible product finding emerged from this baseline. The known, reproducible Settings failures remain explicitly in scope as existing issues:

- [#284](https://github.com/pengw0048/data-playground/issues/284) — dirty Settings edits can be lost on dismissal or unload (P1 UX).
- [#285](https://github.com/pengw0048/data-playground/issues/285) — typed plugin defaults/inheritance are not preserved (P1 UX).

This audit does not fix those issues, does not declare release readiness, and does not certify Workspace, versioned data, durable Jobs, compound temporal data, mobile, remote providers, or hostile-tenant behavior.

## Follow-up

After this evidence PR merges, update #174 and #229 with this exact audited SHA and the retained-evidence link, then close #317 and #229 as directed by their completion criteria. The remote full matrix is already successful; the release gate remains intentionally blocked by the explicitly recorded P1 issues #284 and #285.
