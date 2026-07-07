# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added
- Catalog-driven join hints: key detection, measured join cardinality (1:1 / 1:N / N:M), and ranked
  join-key suggestions for the join node and for any two catalog datasets.
- Grain propagation: a filtered/sampled/aggregated dataset is tracked as still joinable on its key.
- Owner-declared primary keys and relationships, with an ER / relationship view (React Flow).
- Relationship-aware agent tools (`list_catalog` with keys, `join_hints`, `validate`).
- Real execution layer: capability-based placement planner, `RunController`, content-addressed
  result store, and a reference multi-worker pool backend (`DP_POOL_WORKERS`).

### Fixed
- Repository now ships the Apache-2.0 `LICENSE` text.
- `make setup` works on a fresh clone (creates `web/dist` before the kernel install).
- Lance support is included in the `dev` extra; a missing `pylance` yields an actionable error.
- Numerous adversarial-review fixes across the execution and catalog layers (see git history).

## [0.1.0] — unreleased

Initial public preview: node-based canvas, out-of-core DuckDB engine, plugin SPI, optional LLM agent.
