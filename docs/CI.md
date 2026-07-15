# Continuous integration and release gates

Data Playground separates fast pull-request feedback from environment-heavy production acceptance.
The distinction is intentional: a protected pull request should fail quickly on a regression, while a
release must prove the complete artifact, UX, and optional distributed-execution contracts.

## Trigger matrix

| Gate | Pull request | Push to `main` | Schedule or manual | `v*` release |
| --- | --- | --- | --- | --- |
| Core kernel, web, type, migration, backup, and browser tests | Required | No duplicate run | Manual | Already proved by the required, up-to-date PR |
| UX golden-workflow smoke | Required inside normal browser CI | No duplicate run | Manual | Already proved by the required, up-to-date PR |
| Full researcher UX fixture matrix and P0/P1 issue gate | No | No | Daily or manual | Required before publish |
| Wheel and application-image clean-install smoke | No | No | Manual | Required before publish |
| Real multi-container Ray differential | No | No | Weekly or manual | Required before publish |
| Ray Jobs restart/cancel/result acceptance | No | No | Weekly or manual | Required before publish |
| CodeQL, Gitleaks, dependency review, and path-gated image scan | Relevant PRs | No duplicate run | Scheduled/manual where configured | A release does not bypass an unresolved required result |

`main` requires an up-to-date pull request and does not accept direct changes, so `push: main` would
repeat evidence for the same reviewed tree. If repository protection is ever relaxed, restore a default-
branch validation trigger before allowing direct pushes.

## Pull-request feedback

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is the required functional gate. Its browser
job runs the tagged `@ux-smoke` scenarios first for a focused failure, then runs the remaining browser
suite. Heavy Docker clusters and release builds are deliberately absent from the PR event.

Security workflows remain PR-scoped because they inspect the proposed diff or source tree. Superseded
PR heads use `cancel-in-progress` so only the current revision consumes runners.

## Heavy acceptance

The following workflows retain independent `workflow_dispatch` entry points and scheduled health runs
where an external runtime can drift:

- [`ux-acceptance.yml`](../.github/workflows/ux-acceptance.yml)
- [`release-artifacts.yml`](../.github/workflows/release-artifacts.yml)
- [`ray-validation.yml`](../.github/workflows/ray-validation.yml)
- [`ray-jobs-acceptance.yml`](../.github/workflows/ray-jobs-acceptance.yml)

Run one manually from the Actions page or with, for example:

```bash
gh workflow run ray-validation.yml --ref main
```

Scheduled results are health evidence, not required PR statuses. A failure should become a tracked
regression; it must not be ignored until the next release.

## Release contract

A `v*` tag starts [`.github/workflows/release.yml`](../.github/workflows/release.yml). The release
workflow calls all four heavy acceptance workflows in parallel. Publishing the wheel, image, SBOMs,
checksums, and attestations cannot start until every called gate succeeds. A manual run exercises the
same acceptance graph but never publishes.
