# Changelog

All notable changes to Data Playground are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses semver-shaped versions from `kernel/pyproject.toml` / `web/package.json`.

Every release candidate must retain a passing [researcher UX acceptance](docs/UX_ACCEPTANCE.md) result
for its exact commit before publication.

## [Unreleased]

No public tag or release has been published. The planned first release targets Profile A (local
workstation) with a Python wheel (`data-playground`) and application container image.

### Supported platforms

- **Python:** 3.11–3.13 (`requires-python = ">=3.11,<3.14"` in `kernel/pyproject.toml`).
- **Browsers:** modern desktop Chromium, Firefox, and Safari (desktop-first; the Playwright e2e suite
  runs Chromium). Mobile viewports are not a release support claim.
- **Deployment profiles:** Profile A (local workstation) is the first-release target. Profiles B
  (trusted-team shared service) and C (distributed Ray) remain certification gates — see
  `docs/PROJECT_ACCEPTANCE_AND_ROADMAP.md`.

### Metadata schema

- **Current Alembic history:** one fresh-database revision, `0001_schema_baseline` (head).
- Databases created by earlier pre-release commits are intentionally unsupported. Recreate the
  workspace/SQLite database or PostgreSQL schema; there is no upgrade or backfill path into this
  baseline.
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
