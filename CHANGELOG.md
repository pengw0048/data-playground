# Changelog

All notable changes to Data Playground are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
uses semver-shaped versions from `kernel/pyproject.toml` / `web/package.json`.

Every release candidate must retain passing core CI, CodeQL, Gitleaks, and
[researcher UX acceptance](docs/UX_ACCEPTANCE.md) results for its exact commit before publication.

## Unreleased

## [0.3.0] — 2026-08-03

This release turns the versioned-data and durable-execution foundation into a more coherent
researcher workflow: browse and organize work in Workspace, build and reopen a Canvas, inspect or
recover its latest results, and follow runs and publications through Jobs, Inbox, and dataset
history. The supported profiles remain a local workstation and a trusted-team shared service.
Remote execution adapters remain optional; this release does not claim that a remote execution
target is configured or certified on Kiwi.

### Added

- Workspace now behaves as one file browser for datasets, folders, and Canvases, with list and grid
  views, paging, sorting where the selected source supports it, multi-select actions, contextual
  menus, and capability-gated rename, move, copy, and delete actions.
- Canvas results produced by `0.3.0` survive hub and kernel restarts. The latest result for each
  executed target node is kept with the Canvas, while workspace defaults and per-Canvas settings can
  additionally retain a bounded number of recent result versions for a bounded number of days.
- Jobs default to the current user's work while preserving an explicit workspace-wide view for
  trusted collaborators and operators. Inbox outcomes remain owner-scoped.
- Canvas-level execution-target selection is persisted with the Canvas and refuses unavailable or
  incompatible targets before a run starts.
- Dataset lineage opens in the dataset context, preserves shareable focus in the URL, links related
  dataset cards back to their details, and expands field-level evidence when the provider supplies it.

- Field metadata, bounded field-lineage evidence, and typed row references make it possible to
  inspect the provenance of a field without guessing from names or exposing unbounded metadata.
- **Join with related data** offers a provenance-aware reviewed-join path that preserves the
  selected source and relationship evidence, and truthfully labels cardinality as `available`
  or `unmeasured` rather than presenting an exact candidate without a scan as measured.
- Media cells render public image and video values directly. Byte-backed media remains truthful raw
  data until a provider offers an exact-cell addressing capability.
- Plugin authors have a bounded immediate-input seam for checking directly wired, already-proved
  upstream identities without receiving a graph traversal or mutable display-name shortcut.

### Changed

- Catalog, Canvas, and MCP discovery now expose bounded, truthful context: canonical dataset facts,
  relationship and lineage windows, field evidence, explicit availability states, and truncation where
  a result is incomplete.
- The Workspace and Canvas make recovery states actionable: reopening a saved Canvas fits the
  viewport to the saved work, conflicts offer recovery actions, invalid cycles are refused before a
  run, and write/revision status explains what blocked or completed publication.
- The web app detects hub outages, marks server state as unknown, gates server-backed actions until
  reconnection, and keeps local Canvas edits available for retry, export, or recovery.
- Catalog mount credentials accept `env:` and `file:` SecretRefs that are resolved only while the hub
  constructs the provider. Active installed plugins may declaratively forward one vetted secret to
  workload children; children receive only that target value and cannot reopen the hub's reference.
- Managed Write admission compares structural schema changes with the exact current head and requires
  explicit confirmation before publishing drift.
- Managed dataset names are validated before submission and rejected with an actionable field error;
  invalid names are no longer silently sanitized.
- Managed-sidecar merge work is durable and exact. Researchers choose an exact core-owned base and
  explicit mappings; plugins can produce a sidecar candidate but cannot claim destination authority.
- Source, Transform, Write, Chart, Jobs, Inbox, Settings, and dataset-detail surfaces use task-first
  controls and plain user-facing language; diagnostics and implementation identities remain secondary
  evidence instead of dominating routine work.
- Chart configuration starts from known input columns and a runnable default aggregation while still
  allowing an explicit SQL expression; chart results use the full-input execution path rather than a
  misleading sample preview.
- The optional `lance` dependency now requires `pylance>=0.38.0` for the native transaction,
  recovery, and exact-revision statistics contracts used by managed local datasets.

### Metadata and upgrade

- **Alembic history:** `0001_schema_baseline` through `0052_rejected_run_owner` (head), advancing
  published `0.2.x` workspaces from `0039_folder_replays` through one linear forward chain.
- Follow [the stopped in-place upgrade runbook](docs/UPGRADING.md): identify and stop every writer,
  take one consistency backup of metadata, managed bytes, configuration, and credential references,
  run one `dataplay migrate` with the exact `0.3.0` artifact, and verify the candidate-reported schema
  head before reopening traffic.
- Live upgrade, database downgrade, and running a `0.2.x` binary against metadata migrated to `0.3.0`
  are not supported. Rollback means restoring the complete pre-upgrade consistency set.
- The `0051_canvas_result_latest` migration creates retention metadata but cannot reconstruct output
  artifacts from historical `0.2.x` runs. Re-run a Canvas after upgrading to establish its retained
  current-result projection.

### Breaking changes

- Catalog-provider integrations must distinguish a canonical dataset identity from each placement.
  Browse, resolve, and ancestry operate on opaque placement IDs; dataset detail operates on the
  canonical dataset ID. Display names, paths, URIs, and a single placement ID are not substitutes.
- The MCP catalog surface replaces `list_datasets` with bounded metadata tools: `search_catalog`,
  `get_dataset_context`, `get_relationship_graph`, and `get_dataset_lineage`. Agents must respect
  cursors, availability states, and truncation before treating metadata as complete.
- MCP clients connecting a Join must now provide `targetHandle: "a" | "b"`; an unqualified Join input
  is rejected instead of being assigned implicitly.
- Dataset MCP resource URIs are returned by `search_catalog` and `get_dataset_context`, but are no
  longer enumerated by `resources/list`, whose protocol response has no continuation field.

### Known limitations

- The supported deployment boundary remains a trusted workspace, not mutually distrusting tenants.
  User code, installed plugins, workers, and operators are trusted with that workspace.
- The bundled local execution targets are the only targets certified for the Kiwi demo deployment.
  Pod, Ray Jobs, MultiKueue, and LAX execution require separately registered runners, data transport,
  admission, cancellation, and operator-link validation before the UI may offer them as available.
- External catalog discovery does not imply provider write-back. Provider sorting, mutation, exact
  reopen, lineage, and media behavior are exposed only when that provider reports the capability.
- Repository Compose, Kubernetes, KubeRay, and deployment examples are validation references, not
  production manifests. Supported browser use remains desktop-first.
- Wheel and image publication targets GitHub Releases and GHCR, not PyPI.

### Verify the published release

```bash
# After downloading all assets from the GitHub Release:
sha256sum -c SHA256SUMS

gh attestation verify ./data_playground-0.3.0-py3-none-any.whl \
  --repo pengw0048/data-playground
gh attestation verify oci://ghcr.io/pengw0048/data-playground:0.3.0 \
  --repo pengw0048/data-playground
```

## [0.2.3] — 2026-07-22

### Fixed

- Durable Tasks rebind exact Workspace-provider inputs after worker reconstruction. (#757, #758)

## [0.2.2] — 2026-07-22

### Fixed

- Managed Write admission preserves declared signed and unsigned integer schema widths.

## [0.2.1] — 2026-07-22

This candidate was rejected before publication because its upgrade drill incorrectly expected `0.2.0`
after the candidate correctly reported `0.2.1`. The `v0.2.1` tag remains immutable evidence; no
release artifacts were published.

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
- Follow [the in-place upgrade runbook](docs/UPGRADING.md): record the source identity; stop and confirm
  every hub, kernel, MCP process, CLI run, worker, and scheduler that can write the workspace has exited;
  then take one complete consistency backup, run one `dataplay migrate`, and start `v0.2.0` only after
  the schema reaches `0039_folder_replays`.
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
