# Release-candidate certification — issue #663 (v0.1.0)

## Verdict

**Fail — the release must re-freeze after the fix.** The researcher journeys and the installed
wheel are green on the frozen commit, but the tag-triggered release **cannot publish** `v0.1.0`
from it: the reusable `release-artifacts.yml` wheel-smoke migration self-check fails. That is a
release-blocking finding reported as a separate issue and, per this leaf's directive, is **not
fixed in-leaf**.

- Release blocker: [#670](https://github.com/pengw0048/data-playground/issues/670) (P1) — `release-artifacts.yml` asserts the packaged Alembic migrations are a single squashed baseline, but the shipped wheel contains 38 incremental migrations, so the wheel-smoke job exits non-zero and `release.yml`'s `publish` (which `needs: artifacts`) never runs.

Everything else in scope passed. This audit records what was exercised so the blast radius is
explicit: the product a user installs works; the break is in the release automation's own
migration self-check.

## Scope and identity

- Frozen commit certified: `5a2943691a9cab196efaf6f33b889f2108981c71` (`origin/main`; "test: run the keyed-upsert acceptance journey on the shared Postgres profile (#669)").
- Re-freeze record naming this commit: [#174 release re-freeze](https://github.com/pengw0048/data-playground/issues/174#issuecomment-5029540360) (documented-P2 list empty).
- Version under certification: `0.1.0` (pyproject, package.json, wheel METADATA, and `/api/version` all agree; no tag exists yet — tags = 0).
- Executed 2026-07-20 on macOS 26.5.2 (25F84), Node v26.5.0, uv 0.10.10, host Python 3.12.13, Playwright 1.61.1. The wheel install runs under Python 3.13.12 with DuckDB 1.5.4 / PyArrow 25.0.0.
- Wheel: `data_playground-0.1.0-py3-none-any.whl`, SHA-256 `03b784c5bd4a00f7fc684f0419fc8e73882484c35594276b718f122b2a68ec8a`.
- Fixture manifest (full profile): [fixture-manifest.json](fixture-manifest.json), SHA-256 `a9adacf3588126d8d3fa414c3fcd32713a6703e477dc9f3ba765e9fd3591f636`.
- Machine-readable results: [conformance.json](conformance.json). Visual-review sessions: [visual-review.json](visual-review.json).

## Fresh-install evidence (wheel)

Reproduced the `release-artifacts.yml` wheel-smoke path locally: built the SPA and wheel from the
frozen commit, installed the wheel into a fresh venv **outside** the checkout, and started
`dataplay` in a clean workspace with `DP_GIT_SHA` pinned to the frozen commit.

| Check | Result |
| --- | --- |
| `cd web && npm ci && npm run build` | pass |
| `cd kernel && uv build --wheel` | pass — `data_playground-0.1.0-py3-none-any.whl` |
| `scripts/check_wheel_has_spa.py` | pass — ships a real SPA (1018 bytes `hub/_web/index.html`) |
| Fresh clean-venv install | pass — `importlib.metadata` reports `0.1.0` |
| `scripts/release_smoke.py --expect-version 0.1.0` | pass — `sha` = frozen commit; aggregate committed 50 rows; one named output; sample columns `{user_id,total,n}` |
| `scripts/check_release_versions.py --require pyproject,package_json,wheel,api` | pass — all sources `0.1.0` |
| Packaged schema head vs `metadb.expected_schema_head()` | pass — both `0038_inbox_dataset_scoped` |
| **`release-artifacts.yml` "Verify the wheel-installed baseline migration"** | **fail — see [#670](https://github.com/pengw0048/data-playground/issues/670)** |

The one failing step asserts `revisions == ["0001_schema_baseline.py"]`; the installed wheel ships
38 migration files (`0001`…`0038`). The adjacent `stored_head == expected_schema_head()` half of
the same step passes, so the product's schema is at its correct head — only the release automation's
single-baseline assumption is stale.

**Docker image + container smoke was not exercised** — Docker is unavailable in this audit
environment. That path lives in `release-artifacts.yml` `image-smoke` (build image, assert OCI
version label, non-root uid 10001, starter-canvas smoke, SHA not `unknown`) and must be run there.

## Golden browser journeys (local profile, SQLite, default settings)

Ran the full researcher matrix from the source tree against a deterministic full-profile fixture
workspace: `cd web && CI=true DP_E2E_FIXTURE_PROFILE=full npm run e2e`.

**Result: 116 passed in 3.1m**; `web/test-results/.last-run.json` reported `status: passed`,
`failedTests: []`.

| Journey | Covering test(s) | Result |
| --- | --- | --- |
| Workspace discovery | `workspace.spec.ts`, `workspace-datasets.spec.ts` | pass |
| Catalog browse / search / paging / folders | `ux-full-matrix.spec.ts` (120-dataset + relationship-dense) | pass |
| Canvas build + preview + invalidation | `ux-golden-workflows.spec.ts`, `canvas.spec.ts` | pass |
| Write → managed revision + receipt | `default-write-journey.spec.ts`, `canvas.spec.ts` | pass |
| Jobs | `jobs.spec.ts` | pass |
| Inbox | `inbox.spec.ts` | pass |
| Revision history — exact reopen | `default-write-journey.spec.ts`, `workspace.spec.ts` | pass |
| Revision history — restore-as-new-head (#307) | `restore-revision.spec.ts` | pass |
| Revision history — keyed upsert (#489) | `keyed-upsert-journey.spec.ts`, `upsert.spec.ts`, `merge-columns.spec.ts` | pass |
| Durable recovery (hub restart mid-run) | `default-write-journey.spec.ts` (test 4), `keyed-upsert-journey.spec.ts` (test 3) | pass |

## Headless parity (wheel install)

`dataplay run "Purchases per user" --workspace <ws> --json` against the wheel install completed
`status: done`, `total_rows: 50`, `placement: local`, all five nodes `done`, output committed to
`top_users.parquet` (`table: top_users`, `version: v51c4ffa1ee`). Headless execution matches the
browser journey.

## P0/P1 UX release gate

`scripts/ux_release_gate.py --repo pengw0048/data-playground` → **"No open P0/P1 UX
golden-workflow defects."** Confirmed against the live issue list (no open `ux`+`P0`/`P1` issue
other than the tracker #174).

## Visual review

Named screenshots for every in-scope journey surface (Workspace, Catalog, Canvas, Jobs, Inbox) were
captured from the running product (wheel install) at **1440×900 and 1280×720, in light and dark** —
20 screenshots under [screenshots/](screenshots/). [visual-review.json](visual-review.json) records
the four sessions (2 viewports × 2 themes).

Each session recorded exactly one console error: `GET /api/catalog/tables/<id>/revisions/capabilities`
→ `501 (Not Implemented)` for the plain-file `events` dataset. This is the existing API contract — a
dataset URI with no revision adapter returns `501 NOT_IMPLEMENTED` (`kernel/hub/routers/catalog.py`
`_revision_adapter`), which the catalog surface probes as feature-detection. It is devtools-only
(no visible or functional impact; the surfaces render correctly), it is not a regression, and the
full matrix — including the axe/console checks — passes with it present. Recorded as a **P3**
polish observation, not a release finding.

> **Human screenshot review is still required** (maintainer) before this leaf may close. This audit
> automates the capture; a maintainer must review the 20 screenshots and record the sign-off here.

## Documented-P2 status

The re-freeze record declares the documented-P2 list **empty** (the prior three P2s landed via
#665/#664/#666). None reproduced in this run:

- P2-1 (first-attempt spec flakes) — **absent**: the full matrix passed with no retries surfaced in `.last-run.json`.
- P2-2 (canvas-less durable task projection into Jobs/Inbox) — **absent**: `default-write-journey.spec.ts` / `keyed-upsert-journey.spec.ts` project their tasks into Jobs and Inbox.
- P2-3 (keyed-upsert journey on Postgres) — **absent locally / out of local scope**: runs in the scheduled `ux-acceptance.yml` postgres-journey tier (no Postgres in this audit environment).

## Not exercised locally (environment limits)

- **Docker image build + container smoke** — no Docker; belongs to `release-artifacts.yml` `image-smoke`.
- **Shared profile (Compose + Postgres) condensed journey and backup/restore drill** — no Postgres/Compose in this environment; the keyed-upsert-on-Postgres journey runs in the scheduled tier per #666/#669. This audit certifies the local SQLite profile only.
- **Maintainer human screenshot review** — pending (see above).

## Findings

| Severity | Finding | Disposition |
| --- | --- | --- |
| **P1** | [#670](https://github.com/pengw0048/data-playground/issues/670) — `release-artifacts.yml` migration self-check asserts a single squashed baseline; wheel ships 38 migrations, so tag `v0.1.0` cannot publish. | Filed; not fixed in-leaf. **Preempts the release.** |
| P2 | `CHANGELOG.md` `[Unreleased]` states Alembic head `0012_linear_checkpoint_admission`; actual head is `0038_inbox_dataset_scoped`. Same root cause as #670. | Folded into #670. |
| P3 | `revisions/capabilities` returns `501` for plain-file datasets → benign catalog console noise. | Recorded; existing contract, no action required for release. |

## Outcome and next step

Per the directive ("if the frozen commit fails, the release re-freezes on a new commit after
fixes"), this certification does **not** authorize the release. The release owner should land the
fix for #670 (and the CHANGELOG head), re-freeze on the post-fix `main` via a new #174 comment, and
re-run this certification against the new exact commit. Because the sole blocker is in release
automation and every researcher journey is green, re-certification after the fix is expected to be
narrow.
