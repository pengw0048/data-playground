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
| Secret scanning (CI) | `secret-scan.yml` | Pull requests, pushes to `main`, and `workflow_dispatch` — [gitleaks](https://github.com/gitleaks/gitleaks) fails the job on detected secrets |
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

Data Playground's canonical profiles and trust assumptions are in
[Supported deployments and trust model](../docs/SUPPORT.md). The primary profiles are a local
workstation and a shared service operated by a trusted team; mutually distrusting tenants are not
supported.

- **Arbitrary code is trusted.** Transform and section code runs with the kernel or worker's process
  permissions. Installed plugins and their `register()` hooks do too. `DP_DATASET_ROOTS` and SQL policy
  constrain the declarative paths they govern, but a subprocess, container, or PodSpawner does not turn
  arbitrary Python into a tenant boundary by itself.
- **Ordinary application controls remain in scope.** Please report authentication or authorization
  bypass, session-forgery or revocation failures, cross-site/cross-origin or collaboration-WebSocket
  holes, traversal outside `DP_DATASET_ROOTS` through a supported declarative data path, unsafe SQL
  policy bypass, plaintext-secret persistence or disclosure, selected-credential fallback to the wrong
  identity, or redaction failures in browser/API/log surfaces.
- **Trusted-code behavior is not a vulnerability by itself.** A user who may edit and run arbitrary
  Python, an installed plugin, an execution worker, or a workspace administrator is trusted with the
  workspace data and process capabilities. Likewise, the absence of mutually hostile tenant isolation
  is an explicit unsupported profile, not an undisclosed sandbox escape.

If a report crosses these categories—for example, read-only access becomes code execution, or an
unauthenticated request reaches a trusted-code capability—please report it privately so we can assess
the actual boundary crossing.

## Supported versions

This is a young project; security fixes land on `main`. Please test against the latest `main`.
