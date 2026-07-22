# Changelog

All notable changes to Data Playground are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses semver-shaped versions from `kernel/pyproject.toml` / `web/package.json`.

Every release candidate must retain passing core CI, CodeQL, Gitleaks, and
[researcher UX acceptance](docs/UX_ACCEPTANCE.md) results for its exact commit before publication.

## [0.2.0] — 2026-07-22

This release makes the existing versioned-data and durable-execution foundation easier to use as one
researcher workflow: organize data and Canvases in Workspace, inspect and run a Canvas, then follow its
publication through Jobs, Inbox, receipts, and exact dataset revisions. It does not introduce a new
orchestration system or widen the deployment trust boundary.

The supported profiles remain a local workstation (SQLite + local storage) and a trusted-team shared
service (`DP_DEPLOYMENT_MODE=shared`, PostgreSQL, operator-provided TLS and durable storage). MCP remains
in scope. Ray and Ray Jobs remain optional backends and release gates, not default deployment profiles.

### Added

- Local Workspace Folder creation, rename, and empty-folder deletion, with replay-safe creation and
  retained placement context for datasets and Canvases.
- Capability-driven Workspace actions for starting a Canvas from a folder or dataset and adding
  supported data and transforms without losing the originating Workspace context.
- Cross-surface links among Canvas runs, Jobs, Inbox outcomes, managed Write receipts, and published
  revisions, including a direct exact-revision reopen path.
- Release-tier in-place upgrade coverage that installs the published `v0.1.0` wheel and the exact
  candidate wheel, then compares retained SQLite and PostgreSQL state.

### Improved

- Canvas first-run choices, toolbar legibility, deep-linked node reveal, and navigation ownership while
  a saved Canvas hydrates or Workspace context changes.
- Jobs filtering and outcome-first inspection, Inbox terminal-outcome language, and Write publication
  summaries so completion, failure, and produced data are easier to distinguish.
- Cross-surface acceptance for the default managed Write journey: revision and receipt publication,
  Jobs/Inbox visibility, exact-revision reopen, and hub-restart recovery.
- Researcher onboarding, plugin onboarding, versioned-data guidance, observability, Ray operations,
  backup/restore, deployment, and contributor documentation.
- Release certification now binds core CI, CodeQL, Gitleaks, artifact smoke, full UX acceptance, Ray,
  Ray Jobs, and the upgrade drill to one recorded candidate commit before publication.

### Metadata and upgrade

- **Alembic history:** `0001_schema_baseline` through `0039_folder_replays` (head), advancing released
  workspaces from `0038_inbox_dataset_scoped` with one forward migration.
- Follow [the in-place upgrade runbook](docs/UPGRADING.md): take one complete consistency backup, stop
  every hub, kernel, MCP process, CLI run, worker, and scheduler that can write the workspace, run one
  `dataplay migrate`, and start `v0.2.0` only after the schema reaches `0039_folder_replays`.
- An object-backed deployment also needs the version-preserving replica and namespace evidence in
  [Backup and restore](docs/BACKUP_RESTORE.md); a database dump and workspace copy alone are incomplete.
- Live or zero-downtime upgrade and database downgrade are not supported. On failure, stop the new
  version and restore the complete pre-upgrade database plus workspace, managed bytes, and configuration.

### Breaking changes

- `DP_AUTH_DIRECT_TLS` no longer satisfies shared-mode startup. The hub does not terminate TLS; shared
  mode requires Secure cookies and a real TLS-terminating reverse proxy named by an exact
  `DP_TRUSTED_PROXIES` IP/CIDR allow-list.
- The root Compose file is now an authenticated PostgreSQL-backed loopback harness, not a shared-service
  or production manifest. Operators must supply the documented proxy, storage, backup, IAM, capacity,
  and topology controls for a trusted-team deployment.
- Core-owned revision timestamps now retain an explicit UTC offset. API clients that compared timestamp
  strings should parse their ISO-8601 meaning instead.
- A `v0.1.0` binary must not run against metadata migrated to `0039_folder_replays`; restore the complete
  pre-upgrade backup instead of attempting an Alembic downgrade.

### Known limitations

- The project supports trusted workspaces, not mutually distrusting tenants. User Python, installed
  plugins, workers, and operators are trusted with the workspace; they are not sandboxed from it.
- Repository Compose, Kubernetes, and KubeRay files are validation references, not production manifests.
- External catalog discovery does not imply provider write-back. Exact reopen requires immutable revision
  evidence from the provider or a core-managed revision.
- Ray and Ray Jobs retain their documented narrow support matrices. In particular, Jobs does not carry
  the hub's admitted exact-revision manifest, supports only its bounded Parquet overwrite shape, has no
  automatic execution deadline, and does not make the multi-region parent restart-durable.
- Supported browser use is desktop-first; mobile viewports are not a release support claim.
- Object-store disaster-recovery takeover is not certified by the backup evidence in this release.
- Wheel and image publication targets GitHub Releases and GHCR, not PyPI.

### Verify the published release

```bash
# After downloading all assets from the GitHub Release:
sha256sum -c SHA256SUMS

gh attestation verify ./data_playground-0.2.0-py3-none-any.whl \
  --repo pengw0048/data-playground
gh attestation verify oci://ghcr.io/pengw0048/data-playground:0.2.0 \
  --repo pengw0048/data-playground
```

## [0.1.0] — 2026-07-21

First public release. The annotated `v0.1.0` tag points to
`172866586a503d3df7e9a2ed399bc20b9e510129`; its release workflow built and published the wheel and
application image from that commit. Release-candidate certification had previously covered the frozen
product surface at `e510bec3a7c325a6f3585e2b9a7456ae694415eb` (see #663); the only repository change
between those commits is this Changelog entry. Supported profiles:
Profile A (local workstation — single user or trusted collaborators, SQLite + local storage) and
Profile B (trusted-team shared service — `DP_DEPLOYMENT_MODE=shared`, PostgreSQL). MCP (HTTP + stdio)
is in scope. The `dp_ray` distributed backend (Profile C) is optional and outside the supported A/B
deployment profiles, but Ray and Ray Jobs acceptance are release-publication gates.

### Supported platforms

- **Python:** 3.11–3.13 (`requires-python = ">=3.11,<3.14"` in `kernel/pyproject.toml`).
- **Browsers:** modern desktop Chromium, Firefox, and Safari (desktop-first; the Playwright e2e suite
  runs Chromium). Mobile viewports are not a release support claim.
- **Deployment profiles:** Profile A (local workstation) and Profile B (trusted-team shared service,
  PostgreSQL) are supported this release. Profile C (distributed Ray) is optional and outside those
  supported profiles; Ray and Ray Jobs acceptance remain required before release publication — see
  `docs/PROJECT_ACCEPTANCE_AND_ROADMAP.md`.

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
  reset was permitted before the first public release; it is not an upgrade path for released databases.
- Callers that scraped `GET /api/version` should expect a `version` field (package version) in
  addition to the existing `sha` / backend identity fields.

### Known limitations

- Soft sandbox: canvas code runs as the hub/kernel OS user; Profile A trusts the local machine.
- The baseline downgrade deletes every metadata table and exists only for schema/startup tests; it is
  not an operational rollback path.
- Profile B still lacks OIDC and multi-replica collaboration certification. The Ray worker-image SBOM
  is also outside this release; Ray and Ray Jobs acceptance nevertheless gate publication.
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
