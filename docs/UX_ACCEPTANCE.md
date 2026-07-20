# Researcher UX acceptance

This is the task-based release contract for Data Playground. It complements component tests: a control
can pass in isolation while the research task still presents stale data, hides its scope, loses context,
or makes an irreversible change unclear.

## Users and golden workflow

The contract serves a first-time local researcher, a returning researcher with many datasets and
canvases, a researcher inspecting data quality and lineage, a read-only collaborator, an administrator,
and a user returning to a failed, cancelled, or recovered job.

Every acceptance run follows this workflow:

1. Discover or register a dataset.
2. Inspect a sample and profile.
3. Explore relationships and lineage.
4. Add the dataset to an explicitly chosen canvas.
5. Build and preview a transformation.
6. Change the graph and see prior results invalidated.
7. Estimate, run, monitor, cancel, or recover full work.
8. Inspect and export a complete artifact.
9. Leave and return through a durable, shareable link without losing context.

## Deterministic fixtures

Build fixtures with the product environment so they use the same starter-data formats as a real local
workspace:

```bash
cd kernel
uv run python ../scripts/build_ux_fixtures.py --profile smoke --output /tmp/dp-ux-smoke/data
uv run python ../scripts/build_ux_fixtures.py --profile full --output /tmp/dp-ux-full/data
```

`smoke` contains the standard starter data. `full` additionally contains a 120-dataset catalog and 24
relationship-dense datasets. The generated
`ux-fixtures/manifest.json` records the fixture matrix, including route/browser-injected slow,
unavailable, permission-denied, stale-reference, partial-failure, and recovery scenarios. These fixtures
need neither external credentials nor a private service.

## Invariants

- A result is never presented as current after its graph or canvas scope changed.
- Sample, estimate, page, full result, and durable artifact have distinct labels and behavior.
- Export identifies its scope; a preview download cannot imply that it contains the full dataset.
- Destructive actions name their target and require confirmation before mutating it.
- Full work exposes estimate, progress, cancellation, terminal state, and recovery evidence.
- A reproducible view has durable navigation state, and all core actions have a keyboard path.
- Supported desktop viewports preserve access to essential controls without silently hiding them.

## Result scope and export contract

Every data view names the scope it can prove:

- **Preview sample** is computed with a bounded prefix from each upstream source. A join, unnest, or
  other transform can reorder, remove, or create output rows, so the result is not described as the
  first N rows of the final dataset. Paging or exporting it never implies that the full dataset was
  scanned.
- **Dataset preview**, **published-dataset page**, and **full-result page** are distinct interactive
  scopes. Their CSV/JSON actions say `Export this page`, and filenames include both the scope and row
  range. A write output reports rows written by that mutation, never that number as the table total.
- **Full result** is a committed non-catalog run artifact. Its interactive pages and native export are
  resolved by run, node, and port identity rather than accepting a client-provided storage URI.
  `Export full result` preflights access and streams the original single-file artifact without leaving
  the application; it does not synchronously convert a large result or silently download the first
  file of a multi-file artifact.
- Interactive artifact reads stop at 2,000 rows. The response and UI distinguish a complete result,
  an ordinary page, an unknown total, and the interactive cap. A grouped chart may draw at most 2,000
  groups, but its durable artifact and downstream dataset retain every group.
- Page navigation distinguishes a proven end from an unknown next page. When an adapter cannot prove
  either state, the UI names that uncertainty and lets the user try the next bounded offset.
- Preview-profile metrics are exact only within the preview sample. Whole-dataset profiles scan every
  row for count, null, min, max, and mean; distinct counts are estimates and display `≈` inline.

These labels are product semantics, not decoration. A release fails this contract if a filename,
toast, chart, profile cell, or disabled paging control makes a smaller scope look complete.

## Gate tiers and evidence

| Tier | When | Required evidence |
| --- | --- | --- |
| PR smoke | Normal CI | `@ux-smoke` Playwright tests cover explicit canvas targeting, preview invalidation, sample/export scope, destructive disclosure, keyboard navigation, and serious/critical axe findings. |
| Full matrix | Daily schedule, manual dispatch, and release candidate | Full Playwright suite on the full fixture profile, including the documented minimum viewport. The full-only browser specs search the 120-entry catalog, render declared relationship-dense data, and inject slow, unavailable, permission-denied, stale-reference, partial-failure, and recovery states. Report, traces, test results, fixture manifest, workspace, and test DB are retained as an artifact. |
| Release candidate | Tag-triggered release workflow | The full matrix plus the P0/P1 golden-workflow issue gate must pass before publishing release assets. |

The full matrix does not run on pull requests or after merging to `main`; the tagged smoke in required
browser CI is the PR gate. The release workflow calls `.github/workflows/ux-acceptance.yml`, and its
result is required release evidence. See [CI and release gates](CI.md) for the trigger policy. The
[changelog](../CHANGELOG.md) also records this requirement for release readers.

## Severity and sign-off

- **P0:** prevents a golden workflow or risks data loss, disclosure, or incorrect result use. Block the
  release immediately.
- **P1:** materially breaks a golden workflow. An open issue carrying both `P1` and `ux` blocks release
  publication, except this tracking issue itself.
- **P2:** important but does not block release by itself; it remains visible in the acceptance report.

A release candidate is not accepted until the workflow evidence identifies the exact commit, environment,
and outcome, and no linked P0/P1 golden-workflow defect remains open.
