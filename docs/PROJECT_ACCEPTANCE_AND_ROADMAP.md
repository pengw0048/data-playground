# Project acceptance and public roadmap

Data Playground is a local-first visual workbench for building typed dataflow graphs and inspecting
real data. This document records its public product boundary and the acceptance evidence that matters
for releases. It deliberately does not describe private deployments, provider-specific integrations, or
unimplemented adapters.

For the current user-facing feature set, start with the [README](../README.md). Details for individual
subsystems live in [Catalog](CATALOG.md), [Plugins](PLUGINS.md), [Ray](RAY.md), [Ray Jobs](RAY_JOBS.md),
[MCP](MCP.md), and [browser support](BROWSER_SUPPORT.md). The canonical deployment and trust boundary is
[Supported deployments and trust model](SUPPORT.md).

The delivered foundation and remaining product/architecture roadmap for exact dataset revisions,
transactional writes, sparse enrichment, durable background work, and a unified workspace is
[Versioned data and durable execution](VERSIONED_DATA_AND_DURABLE_EXECUTION.md).

## Product scope

The primary path is a local workstation: one process, embedded metadata and storage, and no required
cloud account. A researcher can discover a dataset, inspect bounded samples and profiles, build a graph,
preview an operation, and run the same graph over the full data from the browser or CLI.

The product is designed for research data. Dataset, graph, preview, run, artifact, and lineage views
must retain enough context for a researcher to understand what was inspected and how a result was
produced.

The repository is pre-1.0. It supports a single user or trusted collaborators; it is not a hardened,
multi-tenant service. User-authored code, installed plugins, execution workers, and administrators are
trusted with the workspace and its process capabilities.

## Deployment profiles

| Profile | Public support boundary |
| --- | --- |
| Local workstation | Primary supported path. SQLite and local files provide a zero-configuration workspace. |
| Trusted shared service | Supported at the application layer with shared-mode auth/transport checks and durable service components; operators remain responsible for TLS, IAM, backups, capacity, operations, and topology validation. |
| Distributed execution | Optional backends are supported only for their documented shapes. The Ray plugin's whole-graph Jobs path is restart-durable, while its multi-region parent is not. |
| Mutually distrusting tenants | Not supported. A future change would require a new threat model and dedicated isolation architecture. |

This table is a planning summary. [SUPPORT.md](SUPPORT.md) owns the full boundary and separates core
application guarantees from deployment responsibilities.

## Public extension boundary

Core exposes provider-neutral plugin contracts for typed nodes, datasets, catalog and search providers,
execution backends, admission or placement providers, destinations, viewers, processors, and telemetry
sinks. A plugin can be distributed separately as an out-of-tree or third-party package; the core must remain
usable offline with its local implementations.

Plugins execute trusted code in the kernel or worker; the SPI is a compatibility boundary, not a
security sandbox. External packages remain responsible for the capabilities and operational limits they
claim.

Extensions must preserve the core's user-visible contracts:

- declare capability and failure behavior rather than silently substituting a result;
- keep provider credentials and private configuration out of committed source and public test fixtures;
- preserve dataset, run, and artifact identity across provider boundaries;
- keep extension dependencies out of the core package; and
- validate generic contracts with deterministic local fakes before relying on a live provider.

This repository documents only the contracts and implementations it contains. An external provider is
not a supported integration until its implementation, compatibility expectations, and validation are
published with it.

## Product acceptance

A release should demonstrate the following outcomes on its release commit:

| Outcome | Evidence |
| --- | --- |
| A new researcher can create or open a workspace and complete a starter dataflow | [Tutorial](TUTORIAL.md), browser end-to-end tests, and the default example data |
| Dataset discovery, previews, profiles, lineage, and exports state their scope truthfully | [Catalog](CATALOG.md) and kernel/web tests |
| Saved canvases can be run headlessly without creating a second pipeline definition | [README](../README.md) and CLI tests |
| Deployment and security claims remain within the supported trust model | [Supported deployments and trust model](SUPPORT.md), [Security policy](../.github/SECURITY.md), and shared-mode tests |
| Optional distributed execution stays within a tested support boundary | [Ray](RAY.md), [Ray Jobs](RAY_JOBS.md), and Ray validation |
| Public plugins can extend the product without forking the core | [Plugins](PLUGINS.md), plugin examples, and contract tests |
| Core researcher workflows remain usable on supported desktop browsers and viewports | [Browser support](BROWSER_SUPPORT.md) and browser tests |

The normal repository checks are the baseline, not a substitute for release evidence:

```bash
make test
cd web && npm test && npm run typecheck
make e2e
```

Release evidence should identify the exact commit, commands, environment, and result. A failing or
incomplete optional-system check must be reported as such rather than represented as success.

## Public roadmap principles

Public planning is issue-driven and limited to work that can be implemented and validated in this
repository. Near-term priorities are improving the researcher workflow, release evidence, truthful
data/result semantics, and the stability of the local-first path.

As needs become concrete, generic contracts may be added for external catalog, search, execution, or
placement providers. Those changes should be separately reviewable, preserve local behavior, document
their compatibility boundary, and avoid embedding provider-specific code in core.

## Documentation integrity

- Link only to documentation and behavior that exists in this repository.
- Describe optional components as optional and state their validation boundary.
- Keep examples free of real credentials, private endpoint details, and provider-specific plans.
- Update the owning document and executable checks in the same change when public behavior changes.
