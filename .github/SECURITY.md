# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting: on the repository's **Security** tab, click
**Report a vulnerability** ([Security Advisories](https://github.com/pengw0048/data-playground/security/advisories/new)).
We'll acknowledge the report and work with you on a fix and disclosure timeline.

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
