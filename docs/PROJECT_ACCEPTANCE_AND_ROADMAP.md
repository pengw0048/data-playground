# Project acceptance and roadmap

This document is the evidence-backed acceptance report and forward plan for Data Playground. It is
not a feature-count inventory. It answers four different questions separately:

1. What is implemented and accepted on `main`?
2. What defects are confirmed in the current code?
3. What deployment-specific certification work remains before a production claim is justified?
4. Which capabilities are valuable next, but are not current defects?

The separation matters. A working backend is not automatically a production-certified deployment,
and a long-term product idea is not a release blocker.

## Audit snapshot

| Field | Snapshot |
| --- | --- |
| Audit date | 2026-07-13 |
| Accepted source baseline | `origin/main` at `fb9c76f96738a048cb8e66aaafb56bf10c249281` |
| README narrative cross-check | Ready and green [PR #5](https://github.com/pengw0048/data-playground/pull/5) at `052da1ce3edf12d678d017dac73199532a4f61e0`; not part of accepted `main` until merged |
| Pending Ray Jobs evidence | Ready and mergeable [PR #79](https://github.com/pengw0048/data-playground/pull/79) at `a841dd8f5440608ac9c1e28969f822b5ba3c9bad`; all seven exact-head GitHub checks passed |
| Product stage | Pre-1.0 (`0.1.0`), with a local or trusted-team scope |
| Audit method | Source, tests, CI workflows, deployment references, and documentation were inspected together |

README PR #5 was used only to align the product framing and security language; it is not evidence that a
runtime capability has landed. PR #79 is reviewed as **pending evidence**, not as accepted `main`
behavior. Its durable Ray Jobs lifecycle and `docs/RAY_JOBS.md` are not counted as complete until the PR
is merged. All seven required checks passed on the named final head; merging is the remaining acceptance
gate. Refresh all three SHAs before using this document as release evidence after any branch changes.

### Status vocabulary

Every item in this report uses one of these states:

- **Accepted** — implemented on the accepted source baseline and supported by code plus an appropriate
  test, validation path, or explicit product boundary.
- **Confirmed gap** — a defect or missing control observed in the current implementation. Priorities
  apply only to confirmed gaps: P1 blocks a relevant production profile, P2 is important hardening or
  material product debt, and P3 is lower-risk polish.
- **Certification gate** — implementation may exist, but a specific deployment cannot be called
  production-ready until this topology-dependent gate has passed with retained evidence.
- **Future capability** — useful product or platform work that is not a defect in the current stated
  scope.
- **Documentation change** — lands in this PR and is not part of the accepted source baseline until the
  PR merges.
- **Non-goal** — deliberately outside the present product boundary. It must not be implied by product
  language or security claims.

## Executive verdict

Data Playground has a coherent and differentiated core: a local-first visual workbench in which typed
table edges connect real, executable data operations. It is substantially beyond a demo. The local
engine is out-of-core, the graph is inspectable, the catalog is server-paginated, collaboration uses
Yjs, execution and storage have formal extension seams, and the repository contains meaningful unit,
integration, browser, PostgreSQL migration, Kubernetes reference, and multi-node Ray validation paths.

The repository is not one undifferentiated form of “production ready.” Readiness depends on the
deployment profile:

| Profile | Intended use | Current verdict | What prevents a broader claim |
| --- | --- | --- | --- |
| **A — Local workstation** | One user, or users who fully trust one another and the machine | **Capable beta; nearest to releasable** | Release artifacts, install/upgrade evidence, supported-version CI, and backup/restore procedures are not yet a complete release contract |
| **B — Trusted-team shared service** | Authenticated team sharing a workspace, metadata DB, storage, and compute trust boundary | **Architecturally supported, not production-certified** | Catalog/data authorization, secret storage, SSO/identity integration, auditability, observability, recovery drills, and deployment hardening remain incomplete |
| **C — Distributed Ray execution** | Selected supported graph shapes on a controlled Ray cluster | **Validated reference backend, not a production deployment** | `main` lacks a durable Jobs lifecycle; ready PR #79 is pending merge. Live capacity/backpressure, scoped identity, active-failure tests, observability, HA/upgrade runbooks, and operator topology certification remain open |
| **D — Mutually distrusting tenants** | Users must be isolated from one another despite arbitrary code and data access attempts | **Non-goal** | The Python and SQL controls are not an OS security boundary; kernels share deployment credentials and the workspace data plane is shared |

The highest-value path is therefore not to add more surface area immediately. It is to make Profiles A
and B explicit, finish the small set of cross-cutting security/release/operations controls, complete the
Ray lifecycle gate, and then prove any generic integration contracts before connecting internal systems
through adapters rather than embedding organization-specific behavior in the core.

## Product acceptance

### Product promise

The strongest concise promise remains:

> A local-first visual data workbench where a typed graph previews real intermediate rows and runs the
> same graph over the full dataset, with optional adapters for external data, catalogs, and execution
> systems.

This promise is supported by the implementation. The project should optimize for four users:

1. **Data practitioner** — visually explores and transforms real local or object-store data, then runs
   the saved graph headlessly.
2. **Trusted team** — shares canvases, catalog context, and a controlled execution environment.
3. **Platform integrator** — connects an existing catalog, search service, scheduler, or compute plane
   through stable ports.
4. **Operator** — deploys a declared profile with explicit security, recovery, observability, and
   upgrade evidence.

The product should not attempt to replace every mature scheduler, metadata platform, search engine, or
cluster controller. Data Playground should own graph authoring, typed plan construction, interactive
inspection, and normalized lifecycle presentation. Existing systems should remain authoritative for
the capabilities they already own.

### Product-management findings

#### PM-01 — Deployment maturity is now explicit

- **Status:** Accepted on the baseline and formalized here; keep it current.
- **Evidence:** [README scope and status](../README.md) already limits the product to a single user or
  trusted team and calls the project alpha/beta. [Ray readiness](RAY.md) already distinguishes backend
  guarantees from deployment ownership.
- **Acceptance:** Every release note and top-level readiness statement names Profile A, B, C, or D.
  “Production ready” without a profile and evidence date is rejected in review.
- **Suggested PR slice:** Documentation-only updates when a profile changes; never mix this with backend
  implementation.

#### PM-02 — First-class operations are intentionally future work

- **Status:** Future capability.
- **Evidence:** The CLI supports headless runs and parameter substitution in [README.md](../README.md),
  but there is no first-class schedule, backfill, retry policy, SLA, owner notification, or incident
  workflow in the product model.
- **Impact:** Building all of those inside the project would duplicate established orchestration
  systems and widen the operational surface substantially.
- **Acceptance:** Introduce a generic trigger/orchestrator port first. Add scheduling UI only after at
  least one real adapter proves the contract and the ownership boundary is documented.
- **Suggested PR slice:** Contract RFC and fake adapter; a real orchestrator adapter is a separate PR.

#### PM-03 — Mutually distrusting multi-tenancy is not an implied feature

- **Status:** Non-goal for the current product.
- **Evidence:** The [security policy](../.github/SECURITY.md), [`sandbox.py`](../kernel/hub/sandbox.py),
  and [`sqlpolicy.py`](../kernel/hub/sqlpolicy.py) all describe the current trust boundary.
- **Acceptance:** Security and marketing text continue to say “single user or trusted team.” Any change
  to this boundary starts with a threat model and architecture RFC, not a configuration flag.

## Accepted capabilities on `main`

These are real strengths to preserve. Later findings narrow their deployment boundary; they do not
erase the accepted implementation.

| ID | Accepted capability | Evidence and acceptance boundary |
| --- | --- | --- |
| AC-01 | Offline-first runnable product | The quickstart, sample data, canvas, hub, and local engine require no cloud service; see [README.md](../README.md), [`Makefile`](../Makefile), and [`kernel/hub/cli.py`](../kernel/hub/cli.py) |
| AC-02 | Typed executable graph | Pydantic wire models, graph validation, compiler, and DuckDB relation builders are implemented in [`models.py`](../kernel/hub/models.py), [`graph.py`](../kernel/hub/graph.py), [`compiler.py`](../kernel/hub/compiler.py), and [`executors/`](../kernel/hub/executors/) |
| AC-03 | Out-of-core local execution | DuckDB/Arrow execution, spill configuration, bounded preview, and full runs are covered by the local runner and kernel tests; see [`db.py`](../kernel/hub/db.py), [`plugins/runner.py`](../kernel/hub/plugins/runner.py), and [`test_kernel.py`](../kernel/hub/tests/test_kernel.py) |
| AC-04 | Per-canvas kernel lifecycle | A warm kernel can outlive the hub; run state is mirrored to shared metadata, and local or Pod spawners use a formal `KernelSpawner` seam; see [`kernel_backend.py`](../kernel/hub/kernel_backend.py), [`pod_spawner.py`](../kernel/hub/pod_spawner.py), and [deployment verification](../deploy/README.md) |
| AC-05 | Server-bounded catalog discovery | Catalog browse, facets, lexical search, bounded lineage, and paginated APIs push down to metadata storage; see [catalog documentation](CATALOG.md), [`CatalogProvider`](../kernel/hub/backends.py), and [`plugins/catalog.py`](../kernel/hub/plugins/catalog.py) |
| AC-06 | Real plugin seams | Nodes, adapters, runners, capabilities, catalogs, managed-object providers, embedders, importers, destinations, and telemetry sinks register through the composition root; see [`backends.py`](../kernel/hub/backends.py), [`deps.py`](../kernel/hub/deps.py), and [plugin documentation](PLUGINS.md) |
| AC-07 | Authentication and route gating | Signed epoch-bound sessions, scrypt password hashes, password-work admission, fail-closed API routing, request-size limits, localhost CORS, and live WebSocket revocation are implemented; see [`auth.py`](../kernel/hub/auth.py), [`auth_admission.py`](../kernel/hub/auth_admission.py), [`main.py`](../kernel/hub/main.py), and [`routers/workspace.py`](../kernel/hub/routers/workspace.py) |
| AC-08 | Honest SQL security boundary | User-authored SQL is parsed and validated fail-closed against a pinned DuckDB AST contract, while documentation explicitly states that this is not an OS sandbox; see [`sqlpolicy.py`](../kernel/hub/sqlpolicy.py) |
| AC-09 | Real-time conflict-free canvas editing | The browser mirrors nodes, edges, and metadata into a Yjs document with CRDT-aware undo; see [`collab.ts`](../web/src/collab/collab.ts) and [`ydoc.ts`](../web/src/collab/ydoc.ts). The server remains a relay, which is a separate topology gate |
| AC-10 | Safe migration start contract | PostgreSQL writers require the exact Alembic head; release migration is explicit and startup/readiness fail closed on schema mismatch; see [deployment documentation](../deploy/README.md), [`metadb.py`](../kernel/hub/metadb.py), and [the PostgreSQL CI job](../.github/workflows/ci.yml) |
| AC-11 | Meaningful automated validation | The repository runs Python tests, web unit/build/browser tests, PostgreSQL migration smoke tests, and a multi-node Ray differential with fault controls and a degraded fresh run; see [CI](../.github/workflows/ci.yml) and [Ray validation](../.github/workflows/ray-validation.yml) |
| AC-12 | Ray distributed correctness for a documented subset | The reference plugin fails closed for unsupported explicit placement, validates driver/worker compatibility, and has a documented supported-operation/data-movement matrix plus real multi-node differential; see [`dp_ray`](../examples/plugins/dp_ray/), [Ray documentation](RAY.md), and [Ray validation](../.github/workflows/ray-validation.yml) |
| AC-13 | Local MCP shares product behavior | HTTP and stdio MCP use the same graph/run primitives as the UI; HTTP is explicitly limited to local/open mode until an authenticated client flow exists; see [MCP documentation](MCP.md) and [`mcp.py`](../kernel/hub/mcp.py) |
| AC-14 | Application image hardening baseline | The application [`Dockerfile`](../Dockerfile) pins base-image digests, installs the frozen project lock without dev dependencies, and runs as a non-root user. The installer itself is not pinned, and the Ray image is assessed separately |

## Findings register

This summary is the handoff index. Detailed evidence, impact, acceptance criteria, and review-sized PR
scopes follow in the domain sections.

| ID | Domain | Status | Priority/profile | Summary |
| --- | --- | --- | --- | --- |
| SEC-01 | Privacy | Confirmed gap | P1 / B | The built-in agent can send catalog metadata and up to eight real rows to an external model without an enforceable workspace egress policy or in-product preflight disclosure |
| SEC-02 | Authorization | Confirmed gap | P1 / B | Catalog and data-plane access are shared across authenticated workspace users; provider calls carry no principal or tenant context |
| SEC-03 | Secrets | Confirmed gap | P1 / B, C | Agent, object-store, and plugin secrets are redacted on reads but stored as plaintext setting values |
| SEC-04 | Transport | Confirmed gap | P2 / B | The authentication cookie `Secure` flag is opt-in, with no production-mode guard that rejects an HTTPS deployment missing it |
| QA-01 | Quality | Confirmed gap | P1 / A-C | Repository-owned CI has strong functional suites but no supported-Python matrix, Python lint/static-type gate, coverage floor, or dependency/SAST/secret-scanning workflow |
| REL-01 | Release | Confirmed gap | P1 / A-C | The repository has no tagged release, changelog/release workflow, published-artifact smoke test, SBOM, signing, or provenance contract |
| OPS-01 | Operations | Confirmed gap | P1 / B, C | Built-in production metrics, traces, audit events, SLOs, alerts, and incident runbooks are incomplete |
| OPS-02 | Supply chain/deploy | Confirmed gap | P1 / B, C | Ray and reference service images use mutable tags or non-frozen installs; Compose includes development credentials |
| OPS-03 | Recovery | Certification gate | A-C | Backup/restore and disaster-recovery behavior is described only partially and has no repository-retained restore-drill evidence |
| OPS-04 | Availability | Certification gate | B, C | PostgreSQL upgrades intentionally stop all writers; zero-downtime migration and HA are not current guarantees |
| ARCH-01 | Execution | Future capability | Integration enabler | After Ray proves a durable lifecycle, extract a generic controller only for providers whose confirmed boundary fits it |
| ARCH-02 | Extension API | Confirmed gap | P1 / B and integrations | Catalog, dataset, and execution ports lack a shared request identity, tenant, idempotency, and capability context |
| API-01 | Compatibility | Confirmed gap | P2 generally; P1 before deep integrations | The unversioned `/api` DTOs and plugin API v1 lack OpenAPI/SPI snapshots, deprecation policy, and a standalone conformance kit |
| CODE-01 | Maintainability | Confirmed gap | P2 | Metadata, Ray, graph-store, and core test modules are large change-risk hotspots |
| COLLAB-01 | Collaboration | Certification gate | B | Yjs is real, but the server is an in-memory relay that requires sticky routing and uses a time-based first-client hydration heuristic |
| DATA-01 | Lineage | Confirmed gap | P1 / B and catalog integrations | Lineage is unique only by parent-child, so distinct run, pipeline, and column events between the same datasets are collapsed |
| DATA-02 | Governance | Confirmed gap | P2 / B and catalog integrations | Schema metadata lacks stable field identity, nullability, constraints, classification, and explicit compatibility semantics |
| DATA-03 | Search scale | Future capability | Large catalogs and integrations | Local semantic search loads and scores the full embedding matrix in each process; a remote provider boundary is a future scale path, not a defect in the local profile |
| UX-01 | Accessibility | Confirmed gap | P2 / A-B | Some clickable cards are not keyboard semantics, canvas focus outlines are removed, very small text is common, and no automated accessibility gate exists |
| UX-02 | Viewport support | Certification gate | A-B | The workbench is effectively desktop-first, but the minimum supported viewport/input model is neither declared nor tested |
| RAY-01 | Lifecycle | Certification gate | C | Durable whole-graph Ray Jobs is pending PR #79 and must not be represented as accepted `main` behavior |
| RAY-02 | Deployment | Certification gate | C | Live capacity/backpressure, scoped workload identity, active-job failure injection, operational telemetry, and HA/upgrade evidence remain open |
| DOC-01 | Documentation | Documentation change | This PR | The stale numeric feature inventory is retired in favor of maintained capability and acceptance documents |

No P0 defect was confirmed at this snapshot. The P1 items above block only the named production
profiles; they do not invalidate the accepted local/trusted-user implementation.

## Security and privacy

The security baseline is materially stronger than a typical alpha project: route gating is centralized,
sessions are signed and revocable, password work is bounded, request bodies are limited by actual bytes,
WebSockets re-check authorization, and SQL policy fails closed. The remaining problems are mostly where a
trusted local product crosses into a shared service or an external model/provider.

### SEC-01 — Agent data egress needs a policy boundary

- **Status:** Confirmed gap, P1 for Profile B.
- **Evidence:** [`list_catalog`](../kernel/hub/agent.py) returns dataset names, URIs, columns, keys, and
  row counts to the model tool loop. [`preview`](../kernel/hub/agent.py) returns up to eight real rows.
  The same module supports hosted model providers. No corresponding data-egress policy, sample-value
  control, or field-classification check exists in the runtime or agent UI in
  [`AgentDock.tsx`](../web/src/panels/AgentDock.tsx). README PR #5 adds an accurate documentation warning,
  but it does not add an enforceable policy or a preflight disclosure in the product.
- **Impact:** Enabling a hosted model can disclose sensitive values and dataset topology outside the
  workspace even when the operator expected only graph metadata to leave. Redacting the API key from
  the browser does not address this data path.
- **Acceptance criteria:**
  1. A workspace policy defaults hosted models to metadata-only tools; sample values require an explicit
     administrator opt-in.
  2. The UI names the selected provider/model and states whether row values may leave the deployment
     before the first tool call.
  3. Optional field classification/redaction can remove or mask values before the tool result is sent.
  4. An audit event records provider, model, tool, dataset identity, column names, and row count without
     recording raw values.
  5. Tests prove that the default path sends no sample values and that local-model mode can be configured
     separately.
- **Suggested PR slice:** Add an `AgentDataPolicy` and tool-result sanitizer with tests and UI disclosure.
  Do not add a general DLP engine, provider proxy, or catalog ACL in the same PR.

### SEC-02 — Workspace authentication is not dataset authorization

- **Status:** Confirmed gap, P1 for Profile B.
- **Evidence:** [`Deps`](../kernel/hub/deps.py) explicitly states that the catalog is shared by every
  user and that per-user boundaries exist at the canvas/share/settings layer, not the data engine. The
  [`CatalogProvider`](../kernel/hub/backends.py) methods and
  [`DatasetAdapter`](../kernel/hub/backends.py) methods receive no principal, tenant, role, or policy
  context. `CatalogTable.owner` is descriptive curation metadata, not an access-control rule.
- **Impact:** Any authenticated user in a shared workspace can potentially enumerate the shared catalog
  and execute reads with the kernel's data credentials. This prevents a truthful enterprise shared-
  service claim and prevents a policy-preserving external catalog integration without another hidden
  authorization mechanism.
- **Acceptance criteria:**
  1. Plugin API v2 defines `RequestContext(principal, tenant, roles, request_id)` and requires it on every
     catalog discovery, resolve, read, mutation, lineage, and search boundary.
  2. Dataset execution binds the authorized `DatasetRef` used at planning time to the execution request;
     a plugin cannot silently resolve a different object at run time.
  3. The default local profile has an explicit allow-all policy rather than a hidden bypass.
  4. Shared-service mode is deny-by-default when the policy provider is unavailable.
  5. Contract tests cover cross-principal list, resolve, preview, run, mutation, and lineage denial.
- **Suggested PR slice:** First introduce the context and fake deny-by-default policy contract without
  changing UI concepts. Migrate built-ins in a follow-up; implement a provider-specific policy mapping
  only after its owning contract is known.

### SEC-03 — Secret redaction is not secret storage

- **Status:** Confirmed gap, P1 for Profiles B and C.
- **Evidence:** [`routers/workspace.py`](../kernel/hub/routers/workspace.py) redacts `agentApiKey`, object-
  store access keys, and plugin fields marked secret on `GET /settings`, but
  [`Setting`](../kernel/hub/metadb.py) stores the JSON value in the metadata database. The agent reads
  `agentApiKey` directly from that setting in [`agent.py`](../kernel/hub/agent.py).
- **Impact:** Database readers, backups, support bundles, or accidental diagnostics can recover provider,
  object-store, and plugin credentials. Rotating one secret also remains coupled to application settings
  rather than a dedicated secret system.
- **Acceptance criteria:**
  1. Persistent settings contain a secret reference, never the secret value.
  2. A generic `SecretResolver` supports at least environment/file references for OSS use and allows an
     organization-specific resolver plugin.
  3. Resolution happens only in the process that needs the capability and returned values are not
     serialized into settings, run envelopes, logs, telemetry, or API responses.
  4. A migration removes existing plaintext values or fails loudly with operator instructions.
  5. Tests search exported settings, database rows, logs, and job envelopes for fixture secrets.
- **Suggested PR slice:** Add the resolver, reference schema, destructive migration, and built-in
  environment resolver. Do not add Vault, AWS Secrets Manager, or a Luma-specific implementation to
  the core PR.

### SEC-04 — Production transport settings should fail closed

- **Status:** Confirmed gap, P2 for Profile B.
- **Evidence:** Session cookies are `HttpOnly` and `SameSite=Lax`, but
  [`routers/workspace.py`](../kernel/hub/routers/workspace.py) sets `Secure` only when
  `DP_AUTH_SECURE_COOKIE` is present. README PR #5 correctly tells HTTPS operators to enable that flag,
  but the application has no shared/production-mode guard that rejects an unsafe configuration.
- **Impact:** A TLS-terminated shared deployment can be misconfigured so the browser accepts the session
  cookie over plaintext transport. The current default is reasonable for localhost but unsafe as an
  implicit production default.
- **Acceptance criteria:**
  1. A declared production/shared-service mode refuses startup unless secure-cookie and trusted-proxy/TLS
     expectations are explicit.
  2. Localhost HTTP remains simple for Profile A.
  3. Integration tests cover direct HTTPS, trusted proxy headers, and rejection of an unsafe shared-mode
     configuration.
- **Suggested PR slice:** Add only the deployment-mode guard and tests. Do not build certificate or
  ingress management into the application.

### Accepted and non-goal security boundaries

- **Accepted:** Authenticated API and WebSocket access is centrally gated; permission/session revocation
  is checked while sockets are live. Preserve the secure-default router pattern in
  [`main.py`](../kernel/hub/main.py).
- **Accepted:** User SQL is constrained by [`sqlpolicy.py`](../kernel/hub/sqlpolicy.py) and DuckDB
  external access is restricted by [`db.py`](../kernel/hub/db.py).
- **Non-goal:** CPython transforms, section scripts, installed plugins, and per-canvas dependency
  installation are not safe against a malicious tenant. The existing “soft sandbox” language is
  accurate and must remain prominent.
- **Certification gate:** HTTP MCP remains a local/open-mode feature until a real authenticated client
  flow exists; [MCP.md](MCP.md) is explicit about this. Do not weaken API authentication to make remote
  MCP convenient.
- **Future capability:** OIDC/SSO, group/role mapping, service identities, and short-lived workload
  credentials are required for a mature shared-service integration, but should be implemented behind
  generic identity and credential ports.

## UX and product design

The product has a strong information model: canvas, table catalog, inspector, previews, run state, and
lineage are distinct but connected. Empty-state examples, bounded loading, retry states, virtualized
catalog rows, undo/redo, and inline errors show deliberate UX work. The remaining review should focus on
accessibility and explicit support boundaries rather than a visual rewrite.

### UX-01 — Complete the keyboard and accessibility contract

- **Status:** Confirmed gap, P2.
- **Evidence:** Recent-file and example cards in
  [`Shell.tsx`](../web/src/views/Shell.tsx) are clickable `div` elements without button/link semantics,
  `tabIndex`, or keyboard activation. [`index.css`](../web/src/index.css) removes the React Flow node
  focus outline. The UI contains many 9.5–11 px labels. No axe/WCAG automation is configured in
  [`web/package.json`](../web/package.json) or [CI](../.github/workflows/ci.yml).
- **Impact:** Keyboard and low-vision users can miss primary navigation or lose focus location. Future UI
  changes can regress silently.
- **Acceptance criteria:**
  1. Every interactive element uses native semantics where possible, with keyboard activation and a
     visible focus indicator.
  2. Canvas nodes and controls have an intentional focus model that survives mouse and keyboard use.
  3. Essential text meets the project's documented minimum size and contrast tokens.
  4. An automated axe smoke test covers Files, Canvas, Tables, Settings, sharing, and run/error states.
  5. A manual checklist covers screen-reader names, keyboard-only graph construction, zoom, and modal
     focus trapping.
- **Suggested PR slice:** Fix semantics/focus for Files and Canvas and add one axe smoke gate. Typography
  normalization can be a second PR after screenshots are reviewed.

### UX-02 — Declare the supported viewport and input model

- **Status:** Certification gate for Profiles A and B.
- **Evidence:** The workbench uses a fixed navigation rail and multiple desktop-oriented panels. CI runs
  a desktop Chromium Playwright suite, but no minimum-supported viewport/input statement or dedicated
  boundary-viewport coverage was found.
- **Impact:** “Web application” can be read as mobile/tablet support even though graph editing is
  currently desktop-first. Responsive retrofits would be expensive and may harm the dense workbench.
- **Acceptance criteria:**
  1. Product documentation declares desktop-first support, minimum viewport, supported browsers, and
     keyboard/mouse expectations.
  2. Playwright runs one minimum-supported viewport and checks that navigation, canvas, inspector, data,
     run, and settings surfaces remain reachable.
  3. Tablet/mobile remains a future capability unless a real use case justifies a separate interaction
     design.
- **Suggested PR slice:** Support statement plus one viewport smoke test. Do not implement broad responsive
  behavior in the same PR.

### Future UX capabilities

- **Operational ownership:** Show schedule/backfill/incident state only after a real orchestrator port
  exists; do not invent UI state that the backend cannot own durably.
- **Remote execution:** Surface normalized queue state, retries, cluster link, log link, and cancellation
  acknowledgement once the durable-job contract provides them.
- **Governance:** Display classification and access state only when catalog policy is authoritative; do
  not infer access from the descriptive `owner` field.
- **Accessibility:** Add documented shortcuts and a command palette only after the existing keyboard path
  is complete.

## Code quality and API governance

The codebase has extensive behavior tests and useful type-bearing models. The primary risk is not lack of
tests; it is that a few very large modules carry many unrelated invariants while CI does not independently
check the full supported runtime matrix or static contracts.

### QA-01 — Add missing quality dimensions without slowing every PR path

- **Status:** Confirmed gap, P1 for release confidence.
- **Evidence:** [`kernel/pyproject.toml`](../kernel/pyproject.toml) declares Python 3.11, 3.12, and 3.13,
  while [CI](../.github/workflows/ci.yml) runs one Ubuntu interpreter. The audited full-suite runs passed
  879 Python tests with 27 skips and 94 web tests, in addition to PostgreSQL, browser, and multi-node Ray
  jobs. No repository-owned Python lint/static-type job, coverage floor, dependency review, SAST, or
  secret-scanning workflow is configured. Frontend compilation provides a useful TypeScript check
  through `npm run build`.
- **Impact:** A release may fail on a supported Python version, accumulate type/API drift, or introduce a
  vulnerable dependency or committed secret without a dedicated gate.
- **Acceptance criteria:**
  1. Python 3.11/3.12/3.13 run a fast install/import/core-contract matrix; the pinned primary version
     keeps the full Python suite.
  2. Ruff and a pragmatic static-type baseline cover new/changed Python code without requiring a
     repository-wide annotation rewrite.
  3. Coverage is reported and a ratcheting floor prevents material regression; critical auth, lineage,
     migration, publication, and cancellation modules have explicit branch tests.
  4. Dependency review, static security analysis, and secret scanning run on pull requests.
  5. Heavy browser and Ray checks remain path-gated or scheduled where appropriate, with clear required-
     check rules.
- **Suggested PR slices:** One PR for Python lint/type baseline, one for the runtime matrix, and one for
  supply-chain/security scans. Do not combine tool adoption with broad mechanical refactoring.

### CODE-01 — Reduce hotspot risk along domain boundaries

- **Status:** Confirmed gap, P2.
- **Evidence:** At this snapshot, [`metadb.py`](../kernel/hub/metadb.py) is roughly 4,448 lines,
  [`dp_ray/__init__.py`](../examples/plugins/dp_ray/__init__.py) roughly 2,249 lines,
  [`test_kernel.py`](../kernel/hub/tests/test_kernel.py) roughly 11,876 lines,
  [`test_object_lifecycle.py`](../kernel/hub/tests/test_object_lifecycle.py) roughly 4,258 lines, and
  [`graph.ts`](../web/src/store/graph.ts) roughly 1,414 lines.
- **Impact:** Reviewers must hold unrelated invariants in one diff; merge conflicts and accidental coupling
  grow; ownership and coverage gaps become harder to see.
- **Acceptance criteria:**
  1. New behavior is placed behind an existing or newly tested domain boundary rather than extending a
     hotspot by default.
  2. Extracted modules preserve behavior through contract tests before callers move.
  3. Metadata extraction follows coherent domains such as identity/session, catalog/lineage, run state,
     and artifact lifecycle; it is not a file-size-only rewrite.
  4. Ray extraction separates lifecycle/control-plane logic from IR/data-plane execution.
- **Suggested PR slice:** Extract one domain at a time with no schema or external behavior change. Do not
  run a repository-wide “clean architecture” rewrite.

### API-01 — Establish explicit API and plugin contracts before internal adapters

- **Status:** Confirmed gap, P2 generally and P1 for deep integrations.
- **Evidence:** [`models.py`](../kernel/hub/models.py) says its shapes “ARE the contract,” but HTTP routes
  are under unversioned `/api`. [`deps.py`](../kernel/hub/deps.py) exposes plugin API major 1 and performs
  structural feature detection, while no standalone plugin conformance suite, serialized OpenAPI snapshot,
  error taxonomy, or deprecation process is present.
- **Impact:** A core refactor or internal-system adapter can accidentally change wire behavior. Plugin
  authors cannot distinguish stable contract from incidental Python internals.
- **Acceptance criteria:**
  1. Commit a reviewed OpenAPI snapshot or equivalent schema compatibility check for supported HTTP
     surfaces.
  2. Define stable error codes and retryability separately from human messages.
  3. Publish a small plugin conformance kit using fake catalog, search, dataset, and execution providers.
  4. Introduce Plugin API v2 deliberately and migrate built-ins in one bounded window. Because the
     project is pre-1.0, prefer a clean documented break over indefinite compatibility shims.
  5. Document which Python modules are public SDK and which are internal.
- **Suggested PR slice:** Contract snapshots and conformance fixtures only. The RequestContext and API v2
  implementation are separate architecture PRs.

### DOC-01 — Replace feature counting with capability evidence

- **Status:** Documentation change; lands with this PR.
- **Evidence:** The former [`FEATURES.md`](../FEATURES.md) mixed fragile source-line citations, stale
  migration counts, partial/missing item counts, and implementation claims. It now points to maintained
  product, integration, and readiness documents instead of duplicating them.
- **Acceptance:** No manually maintained global feature total. A capability claim links to its owning
  document and executable gate. Snapshot reports include a date and SHA.

## Architecture and extensibility

The architecture already has the right high-level shape: one graph model and compiler, replaceable data
and execution providers, durable metadata, and a local default implementation. The next architecture work
should strengthen context and lifecycle contracts, not add parallel organization-specific paths.

### ARCH-01 — Extract a generic durable-job control contract

- **Status:** Future capability and integration enabler. Profile C's current lifecycle gate is RAY-01;
  a generic controller is not required to certify a Ray-only deployment.
- **Evidence:** `main` exposes generic [`ExecutionBackend`](../kernel/hub/backends.py) and optional
  [`PlaceableBackend`](../kernel/hub/backends.py), but no generic restart-durable job-control contract.
  Pending [PR #79](https://github.com/pengw0048/data-playground/pull/79) now freezes a Ray-specific,
  restart-durable whole-graph Jobs lifecycle while explicitly leaving the multi-region parent in memory.
- **Impact:** A future provider whose confirmed boundary matches durable job control should not copy
  Ray-specific retry, cancellation, fencing, and publication semantics. Providers that own only trigger,
  queue, placement, or data behavior need a smaller port instead.
- **Acceptance criteria:**
  1. Reuse PR #79's tested invariants: prepare/bind before submit, deterministic idempotency,
     authoritative absence rules, observable status normalization, cancel intent before stop, stop
     acknowledgement, recovery, result verification, and single-publisher fencing.
  2. Confirm at least one non-Ray provider has the same durable lifecycle boundary; otherwise keep the
     Ray implementation specific and define a smaller port for the real integration need.
  3. Define a generic `DurableJobBackend`/`JobController` with operations equivalent to
     `prepare`, `bind`, `submit`, `observe`, `cancel`, and `recover`.
  4. Define `LaunchContext` with principal/tenant, attempt and idempotency identities, requirements,
     immutable code/config references, input/output references, and trace correlation.
  5. Normalize states and errors without erasing provider-native details or operator links.
  6. Prove the contract with a deterministic fake backend; then make Ray the first real adapter.
- **Suggested PR slice:** Contract types, state machine tests, and fake backend only. Moving Ray behind
  the contract is a second PR; any internal adapter is a later independent PR after owner confirmation.

### ARCH-02 — Carry identity and capability context through every external port

- **Status:** Confirmed gap, P1 for Profile B and internal integrations.
- **Evidence:** [`CatalogProvider`](../kernel/hub/backends.py),
  [`DatasetAdapter`](../kernel/hub/backends.py), and [`ExecutionBackend`](../kernel/hub/backends.py) are
  useful typed seams but do not share a request/principal/tenant context. Catalog queries use offset
  pagination and descriptive metadata, and execution requests do not expose a stable generic
  idempotency/capability envelope.
- **Impact:** Adapters cannot enforce caller-sensitive policy, propagate trace identity, select workload
  credentials, or make consistent retry decisions without hidden globals.
- **Acceptance criteria:**
  1. Plugin API v2 defines `RequestContext`, `DatasetRef`, `ExecutionRequest`, `CapabilitySet`, and a
     normalized provider error model.
  2. Context is created at authenticated entry points and propagated unchanged through plan, catalog,
     dataset, search, and execution boundaries.
  3. Background/recovery work uses an explicit service principal and original tenant, never an implicit
     local user.
  4. Built-in local implementations remain simple through explicit local defaults.
  5. Contract tests prove context propagation and deny behavior when context is absent.
- **Suggested PR slice:** API v2 types plus built-in no-op/local context propagation. Do not include an
  internal provider implementation until its contract is confirmed.

### COLLAB-01 — Certify one collaboration topology before claiming HA

- **Status:** Certification gate for Profile B.
- **Evidence:** The browser uses Yjs in [`collab.ts`](../web/src/collab/collab.ts) and
  [`ydoc.ts`](../web/src/collab/ydoc.ts). The server in [`main.py`](../kernel/hub/main.py) is an in-memory
  room relay; [README.md](../README.md) requires consistent routing by canvas. First-client hydration
  uses an 800 ms peer-response delay. A stale server comment still describes LWW/future Yjs even though
  the client is already CRDT-based.
- **Impact:** A relay restart or routing split can temporarily separate peers, and a timer is not a durable
  proof that no peer has newer state. The CRDT merge algorithm is sound, but delivery/topology ownership
  is not HA-certified.
- **Acceptance criteria:** Choose and certify one of two honest profiles:
  1. **Single relay owner:** one active room owner per canvas, documented sticky routing, reconnect tests,
     and operational drain behavior; or
  2. **Durable fan-out:** shared pub/sub or persisted Yjs updates with replay, deduplication, ownership,
     and multi-instance failure tests.
  In both cases, replace the 800 ms ownership heuristic with an explicit handshake/room state contract
  and correct the stale server comment.
- **Suggested PR slice:** Documentation/comment correction and deterministic handshake test first. A
  durable relay is a separate architecture PR only if Profile B requires multi-instance room HA.

### Accepted architecture boundaries

- `ExecutionBackend`, `KernelSpawner`, `PlaceableBackend`, `DatasetAdapter`, and `CatalogProvider` are
  valuable seams and should evolve rather than be bypassed.
- The local engine and catalog must remain first-class implementations through the same public ports.
- Organization-specific systems belong in optional plugin packages. Core may define generic contracts,
  capability discovery, and conformance tests, but must not import internal clients or branch on a
  `LUMA_*` environment variable.
- Dynamic per-run code upload for Ray is not required. Image-baked code with an immutable code reference
  is an accepted production direction.

## Data-user and governance assessment

The current product is strong for exploration: it shows real rows, types, run results, lineage, join
hints, and owner-entered catalog organization. Production data operations need stronger identity,
history, and contracts than the current local catalog model.

### DATA-01 — Preserve lineage event identity

- **Status:** Confirmed gap, P1 for Profile B and external catalog integration.
- **Evidence:** [`CatalogEdge`](../kernel/hub/metadb.py) has a uniqueness constraint only on
  `(parent, child)` even though it stores `column` and `pipeline`. `catalog_add_edge` searches by that pair,
  inserts only once, and can fill only an initially empty column. Catalog publication follows the same
  pair-level deduplication.
- **Impact:** Multiple pipeline runs, different transformations, or multiple column-level derivations
  between the same datasets collapse into one row. Exported lineage cannot faithfully answer “which run,
  version, pipeline, or column produced this edge?”
- **Acceptance criteria:**
  1. Separate durable lineage **events** from a derived dataset-to-dataset graph view.
  2. Event identity includes an idempotency key plus run/attempt, pipeline, input and output dataset
     identities/versions, and optional field mappings.
  3. Replaying the same event is idempotent; distinct events between the same datasets are preserved.
  4. The bounded UI graph can aggregate events without deleting source evidence.
  5. Migration and contract tests cover two pipelines, two runs, multiple columns, retries, unregister,
     and dataset-version changes.
- **Suggested PR slice:** New schema/event write API and migration with compatibility read projection.
  UI visualization changes and an external lineage publisher are separate PRs.

### DATA-02 — Evolve schema metadata into a real contract

- **Status:** Confirmed gap, P2 for Profile B and external catalog integration.
- **Evidence:** [`ColumnSchema`](../kernel/hub/models.py) contains `name`, `type`, and capabilities.
  [`CatalogTable`](../kernel/hub/models.py) adds owner-entered organization fields, but no stable field
  identity, nullability, constraints, classification, source authority, or compatibility policy.
- **Impact:** Renames look like drop/add, schema compatibility cannot be evaluated reliably, sensitive
  columns cannot drive egress/access policy, and mapping an authoritative external catalog becomes lossy.
- **Acceptance criteria:**
  1. Define stable dataset and field references, nullability, logical/physical types, constraints,
     classification, provenance/authority, and schema version.
  2. Define compatibility outcomes such as backward-compatible, forward-compatible, breaking, and
     unknown without pretending all adapters can prove them.
  3. Preserve unknown/provider-specific metadata in a namespaced extension field.
  4. Keep the local inferred schema simple; unproven properties remain unknown rather than guessed.
  5. Add round-trip contract tests against a fake external catalog.
- **Suggested PR slice:** Schema v2 DTOs and compatibility evaluator only. Policy UI and a
  provider-specific mapping follow separately.

### DATA-03 — Keep local semantic search, add a scalable remote search port

- **Status:** Future capability for large catalogs and internal integration; current behavior is
  accepted for Profile A.
- **Evidence:** [`plugins/catalog.py`](../kernel/hub/plugins/catalog.py) loads all vectors for a model,
  stacks them into a NumPy matrix, caches that matrix per process, and scores the entire candidate set.
  [`metadb.catalog_embeddings_for`](../kernel/hub/metadb.py) returns every embedding for the model.
- **Impact:** This is a functional local path, but it has no documented scale target or semantic-search
  benchmark. Its memory, refresh, and per-process consistency model should not be extrapolated to an
  organization-scale search service without evidence.
- **Acceptance criteria:**
  1. Preserve the current local embedder path and document its scale target.
  2. Add an optional `CatalogSearchProvider` or allow `CatalogProviderV2.search` to delegate remotely.
  3. The port carries structured filters, tenant/context, stable page token, ranking mode, and an opaque
     provider score/explanation.
  4. Search results are re-authorized by canonical dataset identity before display or use.
  5. Contract tests cover pagination, filters, stale/deleted IDs, partial provider failure, and lexical
     fallback.
- **Suggested PR slice:** Port plus fake provider and local adapter. Any provider-specific implementation
  is separate; do not assume search-api implements catalog discovery.

### Future data capabilities

- **Data quality operations:** Durable expectation suites, run-level results, trends, ownership, and
  notifications. The existing row-level `assert` node is accepted but is not a full quality platform.
- **Operational metadata:** Dataset freshness, SLA, deprecation, incident, and certified/authoritative
  state, sourced from an owning system rather than inferred locally.
- **Cursor pagination:** Prefer provider-stable page tokens for external catalogs; the current offset UI
  deduplication remains adequate for the local profile.
- **Transactional multi-output publication:** Add only when a real workflow requires atomic publication
  across multiple sinks and the authoritative catalog/storage contracts can support it.

## Operations, release, and supply chain

The repository has good correctness fences: explicit migrations, readiness checks, non-root application
images, digest-pinned GitHub actions, durable run state, and carefully documented object-attempt cleanup.
Production ownership also requires repeatable release artifacts, observation, recovery, and deployment
evidence that cannot be inferred from unit tests.

### REL-01 — Create a release contract, not just a runnable checkout

- **Status:** Confirmed gap, P1 for Profiles A through C.
- **Evidence:** [`kernel/pyproject.toml`](../kernel/pyproject.toml) and
  [`web/package.json`](../web/package.json) are version `0.1.0`; the audit snapshot has no Git tags,
  changelog, release workflow, wheel/container publication smoke test, SBOM, signing, or provenance
  configuration. Existing workflows are [CI](../.github/workflows/ci.yml) and
  [Ray validation](../.github/workflows/ray-validation.yml).
- **Impact:** Users cannot identify an immutable supported release, reproduce its artifacts, verify what
  was shipped, or follow a tested upgrade/rollback path.
- **Acceptance criteria:**
  1. Tag, Python package version, web version, image label, `/api/version`, and release notes agree.
  2. CI builds the wheel and application image from a clean checkout, installs/runs each artifact in a
     clean environment, and exercises an offline starter canvas.
  3. The release publishes checksums, SBOM, build provenance, and signed artifacts/images.
  4. Release notes name supported Python/browser/deployment profiles, migrations, breaking API changes,
     known limitations, and rollback constraints.
  5. An upgrade smoke test starts the previous supported metadata schema, runs the release migration,
     verifies data, and exercises rollback only where rollback is explicitly supported.
- **Suggested PR slice:** Build-and-smoke workflow plus version consistency first. Publishing/signing and
  upgrade fixtures may be separate reviewable PRs.

### OPS-01 — Add a stable observability and audit baseline

- **Status:** Confirmed gap, P1 for Profiles B and C.
- **Evidence:** [`deps.py`](../kernel/hub/deps.py) exposes a finished-run telemetry callback, and run
  history/status are durable, but no built-in HTTP metrics, distributed traces, security audit-event
  schema, SLO dashboard, or alert policy is present. [Ray readiness](RAY.md) still lists queue, retry,
  spill, storage, logs, traces, and alerts as open production requirements.
- **Impact:** Durable run records and the finished-run telemetry sink support diagnosis and custom
  integrations, but the repository provides no standard proactive detection or alerting path for queue
  growth, stuck runs, publication/GC failure, auth abuse, collaboration splits, or restore regressions.
  Without a provider integration, incident reconstruction depends on application logs and manual review.
- **Acceptance criteria:**
  1. Define low-cardinality metrics for request/run/job state, queue delay, execution duration, retries,
     cancellation latency, publication, storage/GC, kernel health, and provider errors.
  2. Define structured audit events for authentication/admin changes, sharing, dataset access/mutation,
     agent egress, job submission/cancel, secret-reference changes, and policy denial.
  3. Propagate request/run/attempt trace IDs across hub, kernel, adapter, scheduler, and provider ports.
  4. Publish starter SLOs and alerts for the certified profile; retain logs/metrics for the stated
     investigation window.
  5. Tests validate event shape, redaction, and bounded cardinality; telemetry failure never changes data
     correctness.
- **Suggested PR slices:** Telemetry/audit schemas and in-memory test sink; then a generic OpenTelemetry or
  Prometheus adapter; then dashboards/runbooks. Do not hardwire one internal observability vendor.

### OPS-02 — Separate validation conveniences from production-safe artifacts

- **Status:** Confirmed gap, P1 for Profiles B and C.
- **Evidence:** The application [`Dockerfile`](../Dockerfile) is digest-pinned, uses the frozen project
  lock, and runs non-root, but still installs `uv` without a pinned version.
  [`docker/ray/Dockerfile`](../docker/ray/Dockerfile) additionally uses mutable `python:3.12-slim` and
  runs `uv sync --extra ray` without `--frozen --no-dev`.
  [`docker-compose.ray.yml`](../docker-compose.ray.yml) uses mutable MinIO images and documented test
  credentials. [`docker-compose.yml`](../docker-compose.yml) uses mutable `postgres:16` and a hard-coded
  `dp` database password. These files identify themselves partly as reference/validation harnesses, but
  their defaults are easy to copy into a real deployment.
- **Impact:** Rebuilds can change without a source diff, dependency drift can enter the Ray runtime, and
  reference credentials or mutable services can escape into a shared environment.
- **Acceptance criteria:**
  1. All release images and validation service images are pinned by digest and lockfile installs are
     frozen with no dev dependencies unless explicitly needed.
  2. Production examples require externally supplied secrets and immutable application images; they do
     not ship a working default password.
  3. Validation-only credentials and images are visibly scoped and network-isolated.
  4. CI performs clean image builds, non-root checks, vulnerability scanning, and a runtime version
     handshake.
  5. Reference manifests state which security, IAM, storage, quota, TLS, and HA choices remain operator-
     owned.
- **Suggested PR slices:** Ray image reproducibility; pinned validation services; then split production
  Compose example from developer convenience. Avoid adding a full infrastructure-management layer.

### OPS-03 — Prove backup and restore

- **Status:** Certification gate for every profile that stores work users care about.
- **Evidence:** [Deployment documentation](../deploy/README.md) tells operators to recover an unrecognized
  database from a versioned backup, while [Ray documentation](RAY.md) explains managed object namespace
  ownership and says audited disaster-recovery takeover is not implemented. No repository-owned backup
  tool, restore drill, recovery-time objective, or cross-store consistency procedure is present.
- **Acceptance criteria:**
  1. Document exactly what must be backed up for each profile: metadata DB, workspace/canvas files,
     object-store generations/manifests, secret references, and release identity.
  2. Provide a backup and restore procedure for SQLite/local storage and PostgreSQL/object storage.
  3. Restore into an isolated namespace by default so a clone cannot mutate the source installation.
  4. Run a scheduled drill that restores a fixture, verifies canvases/catalog/runs/lineage/artifact
     references, and records RPO/RTO evidence.
  5. Document disaster-recovery takeover separately from clone isolation; do not imply that changing a
     namespace environment variable performs takeover.
- **Suggested PR slice:** Runbook, fixture, and automated isolated restore verification. Provider-specific
  takeover remains a separate design.

### OPS-04 — Keep downtime semantics honest

- **Status:** Certification gate for Profiles B and C; current behavior is accepted for Profile A.
- **Evidence:** [Deployment documentation](../deploy/README.md) requires stopping every metadata writer,
  running one migration job, and then starting the new version. Application processes fail closed when
  the schema is not at the exact head.
- **Impact:** This is a safe and understandable pre-1.0 contract, but it does not support zero-downtime
  upgrades or mixed-version writers.
- **Acceptance criteria:**
  1. Release notes state planned downtime and verify all writer classes are drained.
  2. Upgrade and rollback drills run on the intended topology.
  3. If Profile B later requires zero downtime, introduce expand/migrate/contract schemas, mixed-version
     compatibility windows, and rollout fencing through a dedicated RFC.
- **Suggested PR slice:** Drain/upgrade verification and runbook now. Online migration is future work, not
  an incidental change to the current migration command.

## Ray backend acceptance

### What `main` proves

The accepted baseline includes a real distributed reference backend for a conservative subset. The
[support matrix](RAY.md) documents distributed map/filter/batch operations and selected aggregate,
window, full-row dedup, broadcast join, and sort shapes. Unsupported explicit Ray placement fails rather
than silently running somewhere else. The [multi-node workflow](../.github/workflows/ray-validation.yml)
starts a Ray head, two workers, a separate driver node, and MinIO; it checks semantic parity, fault-control
sensitivity, worker-direct Parquet behavior, and a fresh degraded-cluster run after a worker stops.

That gate proves backend correctness for the tested shapes. It does not prove active-job reconstruction,
cluster IAM, admission control, production storage, KubeRay policy, or incident response.

### RAY-01 — Pending durable Jobs lifecycle

- **Status:** Certification gate for Profile C; pending PR #79, not Accepted.
- **Pending evidence:** Ready, mergeable [PR #79](https://github.com/pengw0048/data-playground/pull/79) at the snapshot
  SHA proposes official Ray Jobs API submission, durable SQL binding, recovery, cancellation intent,
  authoritative-absence rules, result-envelope verification, publication leasing, and whole-graph
  restart reattachment. Its own
  [`RAY_JOBS.md` snapshot](https://github.com/pengw0048/data-playground/blob/a841dd8f5440608ac9c1e28969f822b5ba3c9bad/docs/RAY_JOBS.md)
  explicitly limits the contract to whole-graph execution; multi-region parent orchestration remains in
  memory. The branch was rebuilt on the accepted `main` baseline, independently release-gate reviewed,
  locally passed the evidence matrix below, and passed all seven final-head GitHub checks. None of this is
  accepted `main` behavior until the exact head merges.
- **Integration and final-fix local evidence:** full kernel/SQLite `1036 passed, 43 skipped`; PostgreSQL migration
  `2 passed`; clean PostgreSQL Ray Jobs lifecycle `96 passed, 1 skipped`; PostgreSQL object/GC and
  backend-publication lock races `24 passed`; retained-auth paths `8 passed, 1 SQLite-only skipped`;
  and a freshly built real Ray head/worker + PostgreSQL 16 + versioned MinIO acceptance run passed
  restart reattachment, frozen-source generation, exactly-once catalog/history projection, cancellation,
  missing-result, and corrupt-result scenarios. After the final Popen sink-binding fix, all 11 Ray
  compatibility tests and the focused Jobs driver tests passed, and a freshly rebuilt head, two workers,
  and MinIO differential run passed through the worker-direct whole-graph overwrite gate that had exposed
  the regression. The terminal-result follow-up then made `SUCCEEDED`, `STOPPED`, and post-reconciliation
  corruption share one durable quarantine contract, while retaining autonomous retry through a transient
  control outage; the complete Ray Jobs unit file passed `98 passed, 2 skipped`, nine focused quarantine
  tests passed, and independent final review found no P0/P1/P2.
- **Acceptance criteria before merge:**
  1. Keep the current-main rebuild and complete-diff regression audit intact; re-run them if the head or
     base changes.
  2. All requested review threads are resolved with tests for the exact lifecycle invariant changed.
  3. Unit, PostgreSQL lifecycle, real Ray Jobs service, and existing multi-node differential checks pass
     on the final PR head.
  4. Restart, duplicate supervisor, cancel race, ambiguous control failure, result corruption, catalog
     retry, and stale/missing configuration cases remain fail-closed.
  5. Documentation clearly separates whole-graph durability from multi-region parent durability and
     backend correctness from deployment readiness.
  6. After merge, update this document's baseline and move only the proven lifecycle item to Accepted.

### RAY-02 — Remaining production certification

- **Status:** Certification gate for Profile C.
- **Evidence:** [Ray readiness](RAY.md) lists partial or missing live cluster truth, workload isolation,
  active-job resilience, observability, and operator-owned deployment security/HA. Pending PR #79 does
  not claim to close those gates.
- **Acceptance criteria:**
  1. **Capacity and backpressure:** discover live resources/health, impose bounded admission/queueing, and
     fail an explicit placement when requirements cannot be honored.
  2. **Identity and isolation:** use attempt/dataset-scoped workload identity, namespace and network
     policy, pod security, quota, and separate control/data credentials.
  3. **Resilience:** inject worker, head, driver, hub, metadata, and object-store failures during active
     work; prove retry/fencing/orphan behavior and document the retry policy.
  4. **Compatibility:** pin immutable images and verify core/plugin/Ray/runtime identity on every worker;
     exercise upgrade and rollback.
  5. **Observability:** retain job IDs, normalized state, queue/retry/spill/storage metrics, structured
     logs, trace links, and alerts.
  6. **Supervision:** bound and shard supervisor ownership so every hub does not supervise every job at
     scale.
  7. **Topology evidence:** run representative data/scale, SLO, IAM, network, storage lifecycle, DR, and
     KubeRay tests on the actual target environment.
- **Suggested PR slices:** Live health/admission; scoped launch identity; active-failure matrix; telemetry
  and operator links; supervisor ownership; staging certification report. Keep each independent.

## Production certification by deployment profile

No profile is certified by prose alone. A release should link the exact CI run, image digest, deployment
configuration, test data class, and date for each gate.

### Profile A — Local workstation

Required exit gates:

- Clean install from the published artifact on every supported Python version and supported desktop
  browser.
- Offline quickstart, example canvas, preview, full run, output reopen, restart reattachment, and
  headless CLI smoke tests.
- SQLite migration and upgrade test from the previous supported release.
- Documented workspace backup and isolated restore drill.
- No default external data egress; agent provider/data behavior is explicit.
- Known limitations include soft sandbox, local process credentials, desktop viewport, and lack of
  zero-downtime migration.

### Profile B — Trusted-team shared service

Profile A plus:

- PostgreSQL and shared object storage on the intended topology with immutable images.
- OIDC/SSO or an explicitly supported identity source; secure cookies, TLS, trusted proxy, CSRF/origin,
  and session-revocation behavior verified end to end.
- Principal/tenant context and catalog/dataset policy enforced on list, resolve, preview, run, mutation,
  search, and lineage.
- Secret references and short-lived/scoped credentials; no plaintext secrets in metadata backups.
- Auditable admin/share/data/agent/job events; metrics, traces, SLOs, alerts, and incident runbooks.
- Collaboration topology certified as single-owner/sticky or durable multi-instance fan-out.
- Backup/restore and upgrade/rollback drills with recorded RPO/RTO and planned downtime.
- Explicit statement that canvas collaborators share a trusted kernel/data credential boundary.

### Profile C — Distributed Ray execution

Profile B for the control plane plus:

- PR #79 or its successor merged and the durable whole-graph contract accepted on the release SHA.
- Supported graph/sink/data-path matrix enforced fail-closed and tested against representative data.
- Live capacity, admission/backpressure, immutable runtime identity, and attempt-scoped credentials.
- Active-job failure injection and deterministic retry/cancel/publication/cleanup evidence.
- Bounded supervisor ownership, retained logs/metrics/traces, operator links, and actionable alerts.
- Target KubeRay/Ray, object-store, metadata, network, IAM, quota, autoscaling, upgrade, and DR gates
  certified by the deployment owner.

### Profile D — Mutually distrusting tenants

This is a **Non-goal**, not a backlog checkbox. If product strategy changes, certification would require a
new threat model and at least tenant-scoped metadata/storage/credentials, OS-level workload isolation,
network egress control, resource quotas, safe package/code policy, audit, deletion guarantees, adversarial
testing, and incident response. The current soft sandbox and shared workspace architecture cannot be
incrementally relabeled as this profile.

## Generic integration architecture for Luma systems

The desired integrations include LAX, MultiKueue, Ray, data-api catalog capabilities, and search-api.
This audit did not assume or invent their private APIs. The correct repository boundary is a set of
generic ports with organization-specific adapters in optional packages.

### Design rules

1. **The core owns normalized intent and invariants.** It defines graph identity, authenticated request
   context, dataset references, execution attempts, idempotency, cancellation meaning, publication
   fencing, and user-visible status.
2. **The owning service retains authority.** A scheduler owns placement/queue state, a catalog owns
   canonical dataset metadata and policy, and a search service owns indexing/ranking. Data Playground
   must not mirror authority without an explicit consistency contract.
3. **Adapters map; they do not bypass.** An adapter converts generic requests and provider responses,
   preserves opaque provider IDs/details, and passes the same conformance suite as local implementations.
4. **No internal dependencies in OSS core.** Internal clients, auth libraries, endpoints, and environment
   names stay in separately distributed plugins. Core tests use fakes.
5. **Fail closed on uncertainty.** Ambiguous submit/cancel/status, missing identity, stale policy, or an
   unrecognized dataset version is not silently treated as success or public visibility.
6. **Pre-1.0 changes can be clean.** Plugin API v2 may intentionally break API v1 with release notes and
   an in-tree migration. Do not preserve unsafe context-free semantics through permanent shims.

### Proposed generic contracts

These names are design targets, not claims that the interfaces already exist.

#### `RequestContext`

Minimum fields:

- authenticated principal and tenant/workspace;
- roles or policy claims as opaque, verifiable input;
- request, trace, and causation IDs;
- service/delegation identity for background recovery;
- data-egress and classification policy reference.

It must be created once at an authenticated boundary and propagated through every catalog, search, data,
execution, telemetry, and recovery call.

#### `DatasetRef`

Minimum fields:

- provider and namespace;
- canonical dataset ID plus optional immutable version/snapshot;
- logical display name separate from physical URI;
- optional schema reference and policy token;
- physical access details resolved late by an authorized adapter, not embedded in public metadata.

This replaces reliance on an arbitrary URI as the only durable identity while preserving URI-backed local
datasets.

#### `ExecutionRequest` and `LaunchContext`

Minimum fields:

- run, attempt, idempotency, and parent/child identities;
- immutable graph/IR and code/config references;
- authenticated request context;
- required resources/capabilities and placement constraints;
- input/output `DatasetRef`s and publication contract;
- deadline, retry class, cancellation semantics, and trace links.

#### `DurableJobBackend` and `JobController`

Required behavior:

- prepare and durably bind identity before provider submission;
- idempotent submit with authoritative absence rules;
- observe provider state without inventing terminal success;
- persist cancel intent before requesting stop;
- acknowledge stop only when the provider proves writers cannot continue;
- recover after process restart and fence duplicate supervisors/publishers;
- verify and publish results through an idempotent catalog boundary.

Ray should prove this controller from the invariants frozen in PR #79. Another integration should use the
controller only if its owner confirms that the lifecycle boundary fits; otherwise add the smallest
generic execution, trigger, or queue port proven by its contract rather than copying the state machine.

#### `CatalogProviderV2`

Required behavior:

- identity-aware list/get/resolve with stable cursor pagination;
- namespaces, schemas, owners, tags, classification, versions, and policy results;
- lineage event ingestion/query with durable idempotency;
- mutation/write-back capability discovery rather than method-name guessing;
- explicit consistency, cache, and stale-data behavior.

A data-api catalog integration is a candidate adapter for this port only if the owning team confirms
that its identity, authority, pagination, mutation, and lineage contracts match. The local metadata
catalog should remain a complete, simple implementation for Profile A.

#### `CatalogSearchProvider`

Required behavior:

- query text plus structured filters, context, page token, and requested ranking mode;
- canonical dataset IDs in results, with opaque score/explanation and next-page token;
- authorization re-check before returning or executing a result;
- explicit timeout/partial/fallback semantics.

This port is for catalog discovery providers. Do not assume an internal service with “search” in its
name implements this contract: a search-api integration needs a capability RFC first. Local lexical and
NumPy semantic search remain the offline implementation.

#### `IdentityProvider`, `CredentialProvider`, and `SecretResolver`

- `IdentityProvider` maps authenticated application identity to generic principals, tenants, and roles.
- `CredentialProvider` obtains short-lived, scoped workload/data credentials for a bound execution.
- `SecretResolver` resolves administrative secret references without putting values in metadata.

These ports allow an internal identity or credential system to integrate without changing open-source
auth or requiring its dependencies in core.

### Candidate adapter map

The rows below are integration hypotheses, not descriptions of known internal APIs. The owning team must
confirm each candidate port and responsibility split through the contract questions in the next section.

| System | Candidate generic port | Core-owned behavior | Questions for the adapter/provider owner |
| --- | --- | --- | --- |
| Ray | `DurableJobBackend` | Attempt identity, lifecycle invariants, normalized state, cancel meaning, publication fence | Ray Jobs submission/status/stop/log links, Ray runtime and cluster behavior |
| LAX | Execution backend, graph/IR importer-exporter, node pack, or another confirmed port | Only the normalized contract for one owner-confirmed use case | Whether the desired integration is job execution, graph translation, data processing, or another boundary |
| MultiKueue | Queue-placement/admission adapter or another confirmed port | Placement requirements and normalized admission state only where the confirmed boundary needs them | Admission, quota, cluster selection, status/events, and which other system owns job lifecycle |
| data-api catalog | `CatalogProviderV2`, `DatasetAdapter`, or confirmed subset | Canonical DTOs, context propagation, fail-closed policy contract, and UI behavior | Dataset identity/version, metadata authority, permissions, lineage, and physical-access ownership |
| search-api | Capability RFC first; then a dataset, query, vector-search, node, or catalog-search port only if confirmed | Only the normalized contract for the confirmed capability | Whether it owns catalog discovery, analytical query, samples/stats, vector search, or another function; plus identity, pagination, freshness, and authorization |
| Internal identity/secrets | `IdentityProvider`, `CredentialProvider`, and/or `SecretResolver` if required | Generic principal/tenant and scoped-credential lifecycle | Authentication, token exchange, policy claims, secret backend, and actual integration need |

### Contracts that remain TBD

Do not implement a real internal adapter until its owner confirms these items. Record answers in an
integration RFC and contract fixtures.

| Contract area | Questions that must be answered |
| --- | --- |
| Identity | What are the canonical principal and tenant IDs? How are user, service, and delegated identities authenticated and refreshed? |
| Authorization | Is policy checked by data-api, a separate service, or the adapter? Is list filtering authoritative? How are denials and stale policy represented? |
| Dataset identity | What is the canonical dataset ID, namespace, version/snapshot, and physical-location relationship? Can a logical ID move? |
| Catalog pagination | Cursor format, sort stability, mutation behavior between pages, consistency window, cache invalidation, and rate limits |
| Catalog mutation | Which fields are authoritative? Are writes supported? What are idempotency, transaction, conflict, and read-after-write semantics? |
| Lineage | Event schema, run/attempt identity, dataset/field versions, idempotency key, ordering, retention, and correction/deletion behavior |
| Search/query capability | Is the service for catalog discovery, analytical query, samples/stats, vector similarity, or another function? What are its filters, result identity, pagination, freshness, authorization, timeout, and partial-result semantics? |
| Job lifecycle | Native states, submit idempotency, authoritative absence, retry ownership, cancel acknowledgement, terminal retention, and result contract |
| Resources | CPU/GPU/memory/labels representation, quota, admission, backpressure, queue priority, preemption, and scheduling timeout |
| Workload identity | Token exchange, scope, credential lifetime/rotation, secret delivery, network boundary, and revocation |
| Observability | Canonical trace/job links, log access authorization, metrics dimensions, audit ownership, and retention |
| Topology | Network reachability, TLS/service auth, regional affinity, failover, latency budgets, and dependency SLOs |

### Integration acceptance

Every internal adapter must pass the same minimum review:

- no internal dependency imported by the core package;
- fake-server contract tests run in public CI without credentials;
- real integration tests run in the owning environment and retain versioned evidence;
- all provider IDs and unknown fields survive round trips where needed for diagnosis;
- retries occur only for explicitly retryable errors and preserve idempotency;
- missing identity/policy/configuration fails closed;
- logs, telemetry, fixtures, and errors contain no tokens or raw sensitive row values;
- degraded provider behavior is visible to the user and does not silently fall back to a less secure
  authority.

## Roadmap

The time bands are sequencing guides, not date commitments. Security/release work can proceed in parallel
with PR #79's merge, but adapter implementation depends on generic contracts and owner-confirmed internal
semantics.

### Near term: 0–6 weeks

**Goal:** make Profile A releasable, make Profile B's boundaries explicit, and merge PR #79's reviewed,
exact-head-green current-main rebuild.

1. **Merge PR #79's current clean, exact-head-green head**; re-run the regression audit if the head or
   base changes, then update the Ray baseline here.
2. **Land this acceptance report** and retire the stale feature-count inventory.
3. **Close agent egress risk** with metadata-only default, explicit sample-value policy, UI disclosure,
   and audit events.
4. **Replace plaintext stored secrets** with generic references and a `SecretResolver`.
5. **Create release hygiene:** version consistency, clean wheel/image smoke, changelog/release notes,
   SBOM, provenance, and signed artifacts.
6. **Make builds reproducible:** pin the Ray base and validation services, freeze Ray dependencies, keep
   runtime images non-root, and separate test credentials from production examples.
7. **Add focused quality gates:** supported-Python smoke matrix, Ruff/type baseline, coverage reporting,
   dependency/SAST/secret scans, and an initial axe accessibility smoke.
8. **Start operations evidence:** stable telemetry/audit schemas, an in-memory conformance sink, backup/
   restore runbook, and an automated isolated restore fixture.
9. **Declare product support:** desktop viewport/browser matrix, Profile A release support, planned
   migration downtime, and current MCP/auth/sandbox boundaries.

**Near-term exit:** a tagged Profile A release can be installed, run, upgraded, backed up, restored, and
diagnosed from published artifacts without relying on an untracked checkout.

### Mid term: 6–16 weeks

**Goal:** establish the contracts needed for a trustworthy Profile B and reusable internal integration.

1. Design and land **Plugin API v2** with `RequestContext`, `DatasetRef`, normalized errors, capability
   discovery, and conformance fixtures.
2. Enforce **identity-aware catalog and dataset policy**, including deny-by-default provider failure and
   cross-principal tests.
3. If an owner-confirmed non-Ray provider shares the proven lifecycle, extract the **generic durable job
   controller** and migrate Ray as the first adapter. Otherwise keep Ray specific and add the smaller
   generic port the integration actually needs.
4. Confirm the desired **LAX** and **MultiKueue** capabilities and their owning boundaries, then
   implement separate adapters behind the smallest applicable generic ports.
5. Introduce **lineage event v2** and richer schema/governance metadata, preserving a bounded derived
   lineage graph for the UI.
6. Implement a **data-api catalog adapter** after its authority and lineage contracts are confirmed.
   Complete a **search-api capability RFC** before selecting its adapter port. Each real integration gets
   public fake-contract tests and a private integration gate.
7. Close Ray deployment gaps: **live capacity/admission, backpressure, scoped launch identity, active-job
   failure injection, observability links, and supervisor ownership sharding**.
8. Certify collaboration as an explicit single-owner/sticky topology. If a real Profile B deployment
   cannot use that topology, create a separate durable-fan-out RFC and follow-up PRs.
9. Complete keyboard/focus/contrast work and publish an accessibility support statement.

**Mid-term exit:** one trusted-team deployment and at least one internal catalog/search/job adapter pass
their contract, policy, recovery, observability, and upgrade gates without adding internal code to core.

### Long term: after 16 weeks

Prioritize these only when product evidence supports them:

- first-class orchestration UX for schedules, backfills, retry policy, ownership, SLA, and notifications,
  backed by an external orchestrator port;
- durable data-quality suites, result history, trends, incidents, and ownership;
- large-catalog/search benchmarks, cache/freshness SLOs, and provider failover behavior;
- transactional multi-output publication where an authoritative storage/catalog system can guarantee it;
- durable multi-instance collaboration fan-out only if a real deployment cannot use single-owner sticky
  routing and can fund replay, ownership, and failure testing;
- online expand/migrate/contract upgrades and HA only if Profile B downtime is no longer acceptable;
- stable public SDK, plugin compatibility certification, governance, and a release/support policy after
  real external adoption;
- mutually distrusting tenant isolation only after a product decision, threat model, and dedicated
  security architecture program.

## Review-sized PR backlog

This backlog contains 38 independently reviewable slices, numbered 00–37. Dependencies express
sequencing, not permission to combine rows. A PR may be split further when its behavioral diff becomes
hard to review; unrelated rows should not be combined merely because they touch the same file.

| Order | PR theme | Depends on | Acceptance focus | Explicitly out of scope |
| ---: | --- | --- | --- | --- |
| 00 | Acceptance report and capability pointers | None | Snapshot, evidence links, status vocabulary, no stale counts | Product implementation |
| 01 | Finish and merge durable Ray Jobs PR #79 | Existing PR | Preserve the completed current-main regression audit, exact lifecycle invariants, final-head CI, honest limits | Generic controller extraction, new sinks |
| 02 | Agent data-egress policy | None | Metadata-only default, value opt-in, sanitizer, disclosure, agent-egress event fixture adopted by 12 | Full DLP, catalog ACL, cross-domain audit schema |
| 03 | Secret references and `SecretResolver` | None | No plaintext DB secrets, destructive migration, redaction tests | Vendor-specific secret manager |
| 04 | Shared-mode transport guard | None | Secure-cookie/TLS/proxy startup checks | Certificate/ingress management |
| 05 | Ray image reproducibility | None | Digest-pinned base, pinned uv, frozen/no-dev install, non-root smoke | KubeRay production chart |
| 06 | Reference-service image and credential cleanup | None; coordinate with 05 | Pinned Postgres/MinIO, test-only credentials, production example requires secrets | Infrastructure provisioning |
| 07 | Python quality baseline | None | Ruff, pragmatic type baseline, changed-code enforcement | Repository-wide annotation rewrite |
| 08 | Supported-runtime matrix | None; coordinate with 07 | Python 3.11–3.13 import/core smoke; one full suite | Ray matrix on every Python version |
| 09 | Security/supply-chain CI | None | Dependency review, SAST, secret scan, image scan | Fixing unrelated historical findings in one PR |
| 10 | Release build and clean-install smoke | None | Wheel/application-image version agreement, offline starter smoke | Ray image, public publishing/signing |
| 11 | Release provenance and notes | 10 | Tag, changelog, checksums, SBOM, signing/attestation | New product functionality |
| 12 | Telemetry and audit event contracts | None | Stable redacted schemas, trace IDs, test sink | Internal telemetry backend |
| 13 | Backup/isolated-restore verification | None | Local and PostgreSQL/object-store fixture, RPO/RTO evidence | Destructive DR takeover |
| 14 | Files/Canvas accessibility baseline | None | Native semantics, focus visibility, axe smoke | Responsive redesign |
| 15 | Viewport/browser support smoke | None; coordinate with 14 | Declared minimum viewport and Playwright coverage | Mobile graph editor |
| 16 | API/OpenAPI compatibility snapshots | None | Reviewed schema snapshot, error codes, change detection | Plugin API v2 implementation |
| 17 | Plugin API v2 context types | 16 | `RequestContext`, `DatasetRef`, errors, fake conformance kit | Real internal adapter |
| 18 | Context propagation through built-ins | 17 | Entry point to catalog/data/execution/recovery, explicit local defaults | Enterprise policy model |
| 19 | Dataset/catalog authorization policy | 18 | Deny-by-default shared mode, cross-principal contract tests | Luma-specific authorization client |
| 20 | Lineage event model v2 | 17 | Durable idempotent events, migration, aggregate graph projection | External lineage publisher/UI redesign |
| 21 | Schema/governance model v2 | 17 | Stable field IDs, nullability, constraints, classification, compatibility | Policy UI or data-api mapping |
| 22 | Catalog-search provider port | 17 | Cursor/filter/context contract, local adapter, fake remote provider | Assuming search-api implements catalog search |
| 23 | Generic durable-job controller | 01, 17, confirmed second-provider fit | State machine contract and deterministic fake backend | Moving Ray or adding an internal adapter |
| 24 | Ray adapter over generic controller | 23 | No lifecycle regression; same Jobs acceptance | New operator/data-path coverage |
| 25 | LAX adapter | Owner-confirmed target capability; 23 only if applicable | Exactly one confirmed execution, graph/IR, processor, or other mapping plus private integration gate | Combining several possible LAX boundaries; LAX behavior in core |
| 26 | MultiKueue admission adapter | Owner-confirmed control-path contract; 17/23 only if applicable | Admission/placement/state mapping with explicit external job-lifecycle ownership | Owning workload lifecycle or cluster policy |
| 27 | data-api catalog adapter | 19, 20, 21, confirmed data-api contract | Identity, cursor, and metadata round trip; lineage events only if both sides support event identity | data-api client in core |
| 28 | search-api adapter for one confirmed capability | Owner-approved capability RFC; 17 plus 19/22 only if applicable | One confirmed query, vector, dataset, node, or catalog-search mapping | Combining the RFC with implementation; assuming ranking/page-token or catalog semantics |
| 29 | Ray live health and backpressure | 01; 24 if the generic controller lands | Resource truth, bounded admission, explicit-pin failure | Autoscaler ownership |
| 30 | Ray scoped workload identity | 03, 19, 01; 24 if the generic controller lands | Short-lived attempt/dataset scope and revocation | Organization IAM policy provisioning |
| 31 | Ray active-failure matrix | 01; 24 if the generic controller lands | Worker/head/driver/hub/store failures during active work | General chaos platform |
| 32 | Ray operator telemetry and links | 12, 01; 24 if the generic controller lands | Queue/retry/spill/storage metrics, authorized logs/trace links | Log backend deployment |
| 33 | Ray supervisor ownership sharding | 01, 29; 24 if the generic controller lands | Bounded ownership, failover, duplicate fencing | Global scheduler replacement |
| 34 | Collaboration handshake correction | None | Remove timer as ownership proof, deterministic sync tests, correct stale comment | Durable multi-instance relay |
| 35 | Collaboration single-owner topology certification | 34 | Sticky routing, reconnect, drain, and split-prevention evidence | Durable replay/fan-out architecture |
| 36 | Orchestrator trigger port | 17 | Schedule/backfill/retry/notification intent contract and fake | Built-in scheduler or a real orchestrator adapter |
| 37 | Data-quality result model | 20, 21 | Durable suite/run results and trend queries | Incident UI and notifications |

### PR handoff standard

Every implementation PR should make independent review possible by including:

1. the finding ID and the exact invariant or user outcome being changed;
2. source evidence and a failing test or reproducible pre-change behavior;
3. the chosen design and rejected alternative with concrete tradeoffs;
4. explicit in-scope and out-of-scope lists;
5. migration, security, privacy, performance, and rollback effects;
6. unit/contract/integration commands run and retained external evidence where required;
7. documentation updated in the same PR only when it describes that PR's behavior;
8. no compatibility shim by default for an intentionally breaking pre-1.0 API change; document the
   migration instead.

## Verification matrix

These commands describe the repository's current evidence paths. A release or certification report must
record the exact SHA, environment, result, and external service/image versions rather than merely copying
the command.

### Core and web

```bash
cd kernel
uv sync --extra dev
uv run pytest -q

cd ../web
npm ci
npm test
npm run build
npm run e2e
```

### PostgreSQL migration contract

The authoritative automated path is the `postgres-migration` job in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). A release should also exercise upgrade from the
previous supported release fixture, not only a clean schema.

### Per-canvas Kubernetes substrate

```bash
deploy/verify-pod-substrate.sh
```

This validates the reference PodSpawner path in a disposable kind cluster. It does not certify a real
cluster's registry, storage, IAM, network policy, resource limits, or HA.

### Multi-node Ray differential

```bash
docker compose -f docker-compose.ray.yml build ray-head
docker compose -f docker-compose.ray.yml up -d --no-build --scale ray-worker=2 \
  ray-head ray-worker minio createbucket
docker compose -f docker-compose.ray.yml run --rm --no-deps driver
docker compose -f docker-compose.ray.yml down -v
```

The complete fault-control and degraded-run sequence is owned by
[`ray-validation.yml`](../.github/workflows/ray-validation.yml) and documented in [RAY.md](RAY.md).

### Pending Ray Jobs

Until PR #79 merges, use only its exact-head checks and test instructions as pending evidence. Its final
head adds a path-gated real Ray Jobs service acceptance workflow alongside unit/fake-control tests, but
that workflow is not repository-owned `main` behavior until merge. Never copy a green run from another
SHA into release certification.

### Documentation integrity

For this report and future edits:

- every relative Markdown link must resolve from the document's directory;
- external snapshot links should be immutable when used as audit evidence;
- claims about an unmerged PR must say pending and name its head SHA;
- counts are generated for an audit snapshot only and are never treated as a maintained feature total;
- status changes require both implementation evidence and the relevant test/certification result.

## Evidence map

| Area | Primary evidence |
| --- | --- |
| Product scope and deployment boundary | [README.md](../README.md) |
| Plugin/execution/catalog protocols | [`kernel/hub/backends.py`](../kernel/hub/backends.py), [`kernel/hub/deps.py`](../kernel/hub/deps.py), [PLUGINS.md](PLUGINS.md) |
| Graph and wire contracts | [`kernel/hub/models.py`](../kernel/hub/models.py), [`kernel/hub/graph.py`](../kernel/hub/graph.py), [`kernel/hub/compiler.py`](../kernel/hub/compiler.py) |
| Authentication and web security | [`kernel/hub/auth.py`](../kernel/hub/auth.py), [`kernel/hub/auth_admission.py`](../kernel/hub/auth_admission.py), [`kernel/hub/main.py`](../kernel/hub/main.py), [`kernel/hub/routers/workspace.py`](../kernel/hub/routers/workspace.py) |
| SQL boundary | [`kernel/hub/sqlpolicy.py`](../kernel/hub/sqlpolicy.py), [`kernel/hub/db.py`](../kernel/hub/db.py), [`kernel/hub/sandbox.py`](../kernel/hub/sandbox.py) |
| Agent egress | [`kernel/hub/agent.py`](../kernel/hub/agent.py), [`web/src/panels/AgentDock.tsx`](../web/src/panels/AgentDock.tsx) |
| Catalog/search/lineage | [CATALOG.md](CATALOG.md), [`kernel/hub/plugins/catalog.py`](../kernel/hub/plugins/catalog.py), [`kernel/hub/metadb.py`](../kernel/hub/metadb.py) |
| Collaboration | [`web/src/collab/collab.ts`](../web/src/collab/collab.ts), [`web/src/collab/ydoc.ts`](../web/src/collab/ydoc.ts), [`kernel/hub/main.py`](../kernel/hub/main.py) |
| UX and accessibility | [`web/src/views/Shell.tsx`](../web/src/views/Shell.tsx), [`web/src/index.css`](../web/src/index.css), [`web/package.json`](../web/package.json) |
| Release and deployment | [`Dockerfile`](../Dockerfile), [`docker-compose.yml`](../docker-compose.yml), [`deploy/README.md`](../deploy/README.md), [`kernel/pyproject.toml`](../kernel/pyproject.toml) |
| Ray backend | [RAY.md](RAY.md), [`examples/plugins/dp_ray`](../examples/plugins/dp_ray/), [`docker/ray/Dockerfile`](../docker/ray/Dockerfile), [`ray-validation.yml`](../.github/workflows/ray-validation.yml) |
| Pending durable Ray Jobs | [PR #79](https://github.com/pengw0048/data-playground/pull/79) and its immutable [`RAY_JOBS.md` snapshot](https://github.com/pengw0048/data-playground/blob/a841dd8f5440608ac9c1e28969f822b5ba3c9bad/docs/RAY_JOBS.md) |
| CI and test ownership | [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), [`.github/workflows/ray-validation.yml`](../.github/workflows/ray-validation.yml), [`kernel/hub/tests`](../kernel/hub/tests/), [`web/e2e`](../web/e2e/) |

## Decision record and non-goals

- **Keep the local path excellent.** External providers must remain optional; the repository must still
  clone, run offline, and use local catalog/search/execution implementations.
- **Prefer ports over product forks.** Luma integration is an adapter package, not a private branch of
  the open-source core.
- **Do not equate authentication with tenant isolation.** Profile B is trusted-team until dataset policy,
  scoped identity, and workload isolation prove otherwise.
- **Do not build a full scheduler before integrating one.** Own trigger intent and normalized status;
  let an orchestrator own queueing, placement, retry, and fleet behavior.
- **Do not claim Ray production readiness from the validation harness.** Backend tests and deployment
  certification are separate evidence sets.
- **Do not promise zero downtime yet.** The current stop-migrate-start contract is safe and explicit.
  Change it only for a measured availability requirement.
- **Do not preserve unsafe pre-1.0 contracts indefinitely.** A clean Plugin API v2 break with migration
  notes is preferable to a compatibility path that silently drops identity or policy.
- **Do not maintain feature counts.** Maintain capabilities through owning documentation, contracts, and
  executable gates.

## Revalidation triggers

Re-run this acceptance review when any of the following occurs:

- PR #79 merges or its lifecycle contract changes;
- Plugin API v2 or a first Luma adapter lands;
- a tagged release is prepared;
- Profile B is proposed for real users or sensitive data;
- a deployment topology changes metadata, storage, collaboration, or execution ownership;
- an external catalog/search/identity provider becomes authoritative;
- the project changes its multi-tenant or sandbox claim;
- a security incident, restore drill, or active-failure test invalidates an accepted assumption.

The revalidation output should update the snapshot SHA/date, move only evidence-backed items between
states, retain unresolved findings, and keep future capabilities separate from defects.
