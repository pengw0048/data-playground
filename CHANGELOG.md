# Changelog

All notable changes to Data Playground are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses semver-shaped versions from `kernel/pyproject.toml` / `web/package.json`.

Every release candidate must retain a passing [researcher UX acceptance](docs/UX_ACCEPTANCE.md) result
for its exact commit before publication.

## [0.1.0] — 2026-07-21

First public release, certified against commit `e510bec3a7c325a6f3585e2b9a7456ae694415eb` (see #663).
Shipped as a Python wheel (`data-playground`) and an application container image. Supported profiles:
Profile A (local workstation — single user or trusted collaborators, SQLite + local storage) and
Profile B (trusted-team shared service — `DP_DEPLOYMENT_MODE=shared`, PostgreSQL). MCP (HTTP + stdio)
is in scope; the dp_ray distributed backend (Profile C) ships as an optional plugin, outside the
release gate.

### Supported platforms

- **Python:** 3.11–3.13 (`requires-python = ">=3.11,<3.14"` in `kernel/pyproject.toml`).
- **Browsers:** modern desktop Chromium, Firefox, and Safari (desktop-first; the Playwright e2e suite
  runs Chromium). Mobile viewports are not a release support claim.
- **Deployment profiles:** Profile A (local workstation) and Profile B (trusted-team shared service,
  PostgreSQL) are supported this release. Profile C (distributed Ray) ships as an optional plugin and
  remains outside the release gate — see `docs/PROJECT_ACCEPTANCE_AND_ROADMAP.md`.

### Metadata schema

- **Current Alembic history:** `0001_schema_baseline` through `0038_inbox_dataset_scoped`
  (head), a linear chain of forward migrations.
- Databases created by pre-baseline commits (before `0001_schema_baseline`) are intentionally
  unsupported. Recreate the workspace/SQLite database or PostgreSQL schema; there is no upgrade or
  backfill path into this baseline.
- **Required release step (non-SQLite):** stop metadata writers, run one `dataplay migrate`, then
  start hubs/kernels. Services fail closed when the schema is not at this build's exact head
  (`metadb.require_schema_at_head`). Local SQLite auto-migrates on startup.

### Added

- Release artifact build-and-smoke workflow (wheel + image, offline starter-canvas smoke, version
  identity).
- Tagged release workflow: GitHub Release with wheel, `SHA256SUMS`, SBOMs; GHCR image push; build
  provenance attestations.
- Fresh-schema smoke tests for SQLite, PostgreSQL, concurrent startup, and the installed wheel.

### Breaking changes

- All metadata databases created before `0001_schema_baseline` must be recreated. This destructive
  reset is allowed only because no public release exists.
- Callers that scraped `GET /api/version` should expect a `version` field (package version) in
  addition to the existing `sha` / backend identity fields.

### Known limitations

- Soft sandbox: canvas code runs as the hub/kernel OS user; Profile A trusts the local machine.
- The baseline downgrade deletes every metadata table and exists only for schema/startup tests; it is
  not an operational rollback path.
- Profile B/C gates (OIDC, multi-replica collab certification, Ray production matrix, SBOM for the
  Ray worker image) are out of scope for the first release.
- Wheel/image publication targets GitHub Releases + GHCR only (not PyPI).

### Rollback constraints

1. Prefer restore-from-backup of the metadata database over Alembic downgrade.
2. Application image / wheel must match the schema head they were built for; mixing a newer schema
   with an older binary (or the reverse) is unsupported.
3. After a failed migrate, do not start hub replicas until the database is restored or migration is
   completed successfully.

### Verify a release candidate

```bash
# Download assets from the GitHub Release, then:
sha256sum -c SHA256SUMS

# Build provenance (public repo → public Sigstore):
gh attestation verify ./data_playground-0.1.0-py3-none-any.whl \
  --repo pengw0048/data-playground
gh attestation verify oci://ghcr.io/pengw0048/data-playground:0.1.0 \
  --repo pengw0048/data-playground
```
