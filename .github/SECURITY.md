# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting: on the repository's **Security** tab, click
**Report a vulnerability** ([Security Advisories](https://github.com/pengw0048/data-playground/security/advisories/new)).
We'll acknowledge the report and work with you on a fix and disclosure timeline.

## Automated scanning

Repository-owned GitHub Actions enforce the following supply-chain and security gates
(see `.github/workflows/`):

| Gate | Workflow | When it runs |
| --- | --- | --- |
| Dependency review | `dependency-review.yml` | Every pull request — OSV-Scanner diffs lockfiles vs the PR base and fails on newly introduced vulnerabilities at severity **high** or above (`FAIL_ON_SEVERITY`; switches to `actions/dependency-review-action` once Dependency Graph is enabled) |
| SAST (CodeQL) | `codeql.yml` | Pull requests, pushes to `main`, weekly schedule, and `workflow_dispatch` — results on the **Code scanning** tab |
| Secret scanning (CI) | `secret-scan.yml` | Pull requests and pushes to `main` — [gitleaks](https://github.com/gitleaks/gitleaks) fails the job on detected secrets |
| Application image scan | `image-scan.yml` | Path-gated on `Dockerfile` / lockfile changes, weekly schedule, and `workflow_dispatch` — Trivy fails on fixable **CRITICAL/HIGH** |

### GitHub-native secret scanning (admin settings)

In addition to the gitleaks CI job, maintainers should keep these repository settings enabled
(Settings → Code security):

- **Secret scanning** — alert on known secret patterns in the default branch and history
- **Push protection** — block pushes that contain known secrets

These toggles are admin-only and cannot be expressed in YAML; record changes here when the
enabled state is confirmed or deliberately changed.

### Known accepted advisories

Advisories the dependency-review gate reports but does not block (severity below its HIGH threshold),
kept here rather than silenced in CI (per [#148](https://github.com/pengw0048/data-playground/issues/148)):

- **`dompurify` (moderate/low)** — pulled in transitively by `monaco-editor` in `web/package-lock.json`
  (~16 moderate/low DOMPurify sanitizer/mXSS advisories; no HIGH/CRITICAL). Accepted: the app's only
  consumer is the Monaco code editor's own internal rendering with a non-attacker-controlled config, so
  the exposure is low, and a transitive bump is pinned by `monaco-editor`. Revisit when `monaco-editor`
  ships a `dompurify` ≥ 3.4.11, or force it via a `package.json` override if a concrete exploit path
  through Monaco is found.

## Scope — what is and isn't a security boundary

Data Playground is a local-first tool. Read this before deciding whether a behavior is a
vulnerability:

- **The code "sandbox" is not a security boundary.** A canvas's transform / section code runs as the
  **same OS user on the same filesystem** as the kernel. `DP_DATASET_ROOTS` + DuckDB's native sandbox
  confine *file* access to allowed roots (uniformly, including raw `sql`), but arbitrary Python is
  arbitrary Python. Running an untrusted canvas is equivalent to running untrusted code on your
  machine — this is by design, not a bug. Real multi-tenant isolation needs OS-level sandboxing
  (containers, per-user accounts, or a pod-per-canvas `ExecutionBackend`). See the "Execution
  isolation — and its limits" section of the README.
- **In scope:** authentication / authorization bypass (unauthorized canvas access across users when
  `DP_AUTH_SECRET` is set), leakage of secrets (the LLM key, object-store credentials) to the
  browser or logs, a path-traversal escape from `DP_DATASET_ROOTS`, or a cross-site / cross-origin
  hole in the API or the collaboration WebSocket.
- **Out of scope:** a transform executing code you put on a canvas; anything requiring an attacker to
  already have write access to a shared canvas or the workspace filesystem.

## Supported versions

This is a young project; security fixes land on `main`. Please test against the latest `main`.
