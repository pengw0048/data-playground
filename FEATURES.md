# Data Playground capability map

The previous item-by-item feature inventory was retired because manual totals and source line numbers
drifted as the project changed. Use the owning documents and executable checks instead:

- [README.md](README.md) — product promise, current user-facing capabilities, architecture, and scope.
- [Project acceptance and roadmap](docs/PROJECT_ACCEPTANCE_AND_ROADMAP.md) — public scope,
  deployment profiles, release evidence, and generic extension boundaries.
- [Catalog](docs/CATALOG.md) — catalog discovery, search, curation, and lineage behavior.
- [Plugins](docs/PLUGINS.md) — public extension seams and reference plugins.
- [Ray](docs/RAY.md) — exact distributed support boundary and production-readiness gates.
- [MCP](docs/MCP.md) — agent integration, transports, tools, and authentication boundary.
- [Tutorial](docs/TUTORIAL.md) — runnable first workflow.
- [Browser and viewport support](docs/BROWSER_SUPPORT.md) — desktop-first viewport, browsers, and input model.
- [Contributing](.github/CONTRIBUTING.md) and [security policy](.github/SECURITY.md) — development,
  review, and vulnerability-reporting paths.

The machine-verifiable baseline is owned by [CI](.github/workflows/ci.yml) and
[Ray validation](.github/workflows/ray-validation.yml). This page intentionally does not duplicate a
feature count or claim that an unmerged pull request is available on `main`.
