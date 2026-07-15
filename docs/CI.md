# Continuous integration and release gates

Data Playground separates fast pull-request feedback from environment-heavy production acceptance.
The distinction is intentional: a protected pull request should fail quickly on a regression, while a
release must prove the complete artifact, UX, and optional distributed-execution contracts.

## Trigger matrix

| Gate | Pull request | Push to `main` | Schedule or manual | `v*` release |
| --- | --- | --- | --- | --- |
| Core kernel, web, type, migration, backup, and browser tests | Required | Required integration run | Manual | Latest `main` result is release evidence |
| UX golden-workflow smoke | Required inside normal browser CI | Repeated inside the integration run | Manual | Latest `main` result is release evidence |
| Full researcher UX fixture matrix and P0/P1 issue gate | No | No | Daily or manual | Required before publish |
| Wheel and application-image clean-install smoke | No | No | Manual | Required before publish |
| Real multi-container Ray differential | Relevant execution-contract PRs | No | Weekly or manual | Required before publish |
| Ray Jobs restart/cancel/result acceptance | Relevant lifecycle-contract PRs | No | Weekly or manual | Required before publish |
| CodeQL and Gitleaks | Required | Required integration run | Scheduled/manual where configured | A release does not bypass an unresolved result |
| Dependency review and path-gated image scan | Relevant PRs | Path/workflow-specific | Scheduled/manual where configured | A release does not bypass an unresolved result |

Direct changes to `main` remain blocked, but pull requests do not have to be rebased after every
unrelated merge. Core CI, CodeQL, and Gitleaks therefore run again on `main` to validate the integrated
tree. This default-branch run does not start environment-heavy acceptance workflows.

## Pull-request feedback

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is the required functional gate. Its browser
job runs the tagged `@ux-smoke` scenarios first for a focused failure, then runs the remaining browser
suite. Release builds and the full UX fixture matrix are deliberately absent from the PR event.

Ray and Ray Jobs use explicit `pull_request.paths` ownership instead of running for every change. Both
suites run when the shared image, `dp_ray`, execution, storage, destination, or plugin contracts change.
The differential also owns its Compose/KubeRay fixtures and cluster checks; Ray Jobs additionally owns
its admission, migrations, durable lifecycle, and real-service harness. Documentation-only and pure Web
changes start neither workflow. These conditional jobs are not required branch-protection contexts:
the required core CI remains the stable merge gate when a Ray workflow is legitimately skipped.

CodeQL and Gitleaks also run on the integrated `main` tree. Superseded PR heads use
`cancel-in-progress` so only the current revision consumes runners.

## Heavy acceptance

The following workflows retain independent `workflow_dispatch` entry points. Runtime-backed suites also
have scheduled health runs because external services and base images can drift:

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

Path ownership is intentionally conservative around shared monoliths such as `metadb.py`, `settings.py`,
and the locked dependency set. That can run both Ray suites for a change whose semantic effect is local,
but omitting those files could miss a real storage or lifecycle regression. Conversely, a path match is
not a coverage claim: the current differential does not certify KubeRay manifest semantics or the
multi-region controller. Weekly runs catch environmental drift, and the release workflow calls every
heavy gate against the exact release revision regardless of changed paths.

## Release contract

A `v*` tag starts [`.github/workflows/release.yml`](../.github/workflows/release.yml). The release
workflow calls all four heavy acceptance workflows in parallel. Publishing the wheel, image, SBOMs,
checksums, and attestations cannot start until every called gate succeeds. A manual run exercises the
same acceptance graph but never publishes.
