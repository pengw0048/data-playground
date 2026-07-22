# Versioned data and durable execution

Data Playground is for repeatable data processing, not for treating a file path or a browser tab as
the record of an analysis. Its core workflow is:

> Workspace and Catalog → exact Source → typed Canvas transforms → Write → dataset revision → Jobs,
> receipts, lineage, and reusable results.

This guide explains what that workflow guarantees today, where those guarantees stop, and why the
boundaries matter to a researcher. It is not an API reference or a second product roadmap.

## The model in practice

Suppose a researcher starts with a dataset, computes two new columns for it, and publishes the
result. A useful record of that work answers four ordinary questions:

1. Which data did the run actually read?
2. What did it write, and did that publication happen once?
3. Can the result and its evidence still be found after the browser or hub restarts?
4. Where does the result live in the researcher’s Workspace without confusing that location with the
   data’s identity?

The product answers those questions with dataset revisions, admitted run inputs, typed write
receipts, durable task records, and local Workspace placement. They are related, but they are not
interchangeable identifiers.

## What is available now

### Datasets, revisions, and admitted inputs

A **dataset** is the logical thing a researcher recognizes and names. A **revision** is one immutable
state of that dataset. A dataset may acquire a newer revision without changing the meaning of an
older one.

Built-in Lance datasets and managed-local publications expose revision history and exact revision
reads. An ordinary local file used by a supported local run is admitted as an immutable managed input
snapshot before execution. In both cases, the run records the exact dataset and revision it was
allowed to read; a later change to the catalog head does not rewrite that record.

This distinction matters when reopening work. A Run History entry, a Job, and a write receipt can
refer to the same admitted input or published revision even when the catalog has since advanced.
They should not silently fall back to “whatever is latest.”

### Typed writes and receipts

The Write card is not a generic overwrite switch. Before a managed publication runs, Data Playground
admits a typed operation with a destination and the evidence it needs. A successful publication has a
receipt containing the dataset identity, the newly published revision, and its outcome evidence.

The default managed-local path can create or replace file outputs and append to managed Lance
datasets. The current product also has two deliberately narrower data-processing paths:

- **Add or replace columns** uses an exact managed-local Parquet base revision and a complete sparse
  sidecar. It rejects unsupported shapes instead of pretending that an arbitrary source or destination
  can be merged safely.
- **Keyed upsert** is available for its measured managed-local workflow. Declared keys are checked
  before publication; duplicate, null, or ambiguous keys and a moved head require a new admission,
  not an automatic rebase.

An idempotent retry or recovery reconciles the original admitted operation with its receipt. It must
not turn an uncertain response into a second published revision or a made-up success.

### Sparse enrichment without copying a wide table through a transform

Many research transforms use a few input columns but produce only one or two new ones. For the
supported local add-or-replace-columns workflow, workers can produce a compact sidecar of row identity
plus derived payload columns. Publication merges that complete sidecar into the exact base revision and
creates a new revision of the full dataset.

This is intentionally stricter than “join some files later.” The base revision, identity columns,
output schema, coverage, expected head, and provenance are frozen at admission. Incomplete coverage,
schema conflicts, or a moved destination head stop the operation before it can publish. The result is
still a normal dataset revision and ordinary write receipt; it is not a special sidecar browsing
product.

### Durable work is owned by the service, not by a page

Supported submitted work is persisted as a Task and Attempt with its admitted inputs, checkpoints or
publication evidence where applicable, and terminal outcome. Jobs, Canvas Run History, and Inbox show
different views of that durable record. A user may leave a Canvas, reload the browser, or restart the
hub without making the task identity disappear.

Durability does not mean every asynchronous API is automatically orchestrated. The built-in task kinds
have explicit recovery, cancellation, and publication rules. A backend or provider that cannot preserve
the promised input, result, or cancellation behavior must fail clearly rather than inherit a stronger
claim from the local implementation.

### Workspace placement is organization, not data identity

Workspace folders can place datasets, canvases, and saved views together. A Canvas can therefore sit
beside the data it explains without becoming that dataset’s owner. Moving or renaming a Workspace item
changes its local placement and presentation; it does not retarget revision, run, or receipt identity.

An external catalog may be mounted for discovery. A read-only provider stays read-only: Data Playground
can keep locally owned Canvas placement and presentation around it, but does not infer permission to
write back into the provider. If a provider object is gone or inaccessible, the Workspace reports that
state instead of quietly attaching the local item to another same-named object.

### Extensions stay capability-led

Plugins can add typed nodes, dataset adapters, catalogs, search providers, destinations, execution
backends, and telemetry. Core remains usable with its built-in local implementations; an extension does
not need to be copied into core to participate.

The boundary is explicit. An extension declares the behavior it can actually preserve, including
revision evidence, preview bounds, credential handling, admitted-input transport, durable
publication, and cancellation where relevant. Missing support is an admission failure or an unavailable
capability, never a quiet downgrade to mutable latest data. Plugins are trusted code in the workspace
processes that load them; they are not a sandbox or a tenant-isolation mechanism.

## Important limits

| Area | Current boundary |
| --- | --- |
| External sources | Exact reopening and revision history require real immutable revision evidence from the adapter. A mutable-only source cannot be described into reproducibility. |
| Provider write-back | Browsing or using a provider source does not grant provider-native write permission. A destination must implement and attest its own publication contract. |
| Distributed execution | Optional backends are supported only for the shapes documented and tested by that backend. In particular, the bundled Ray Jobs path does not independently carry the hub’s admitted exact-revision manifest, so it is not an alternative way to run an exact admitted source. |
| General orchestration | Core has durable task lifecycles, not a general scheduler or a universal multi-provider submit/wait/gather language. |
| Domain data models | Timestamp columns remain ordinary table data. Core does not provide a compound-temporal, episode, stream, clock, or domain-viewer product line. |
| Deployment | Local workstations and trusted-team shared services are the supported profiles. Plugins, user Python, workers, and administrators are trusted with the workspace; hostile multi-tenant isolation is not a product claim. |

These limits protect the useful guarantees above. A familiar label such as “version,” “merge,” or
“distributed” is not evidence that an arbitrary provider has the same semantics as a managed-local
operation.

## Invariants worth preserving

- An admitted run records the concrete inputs it was allowed to read; later catalog movement cannot
  change that record.
- A published managed write creates a revision and a receipt, or remains recoverable/failed. It does
  not report success without durable publication evidence.
- A retry reconciles one semantic operation. It does not duplicate a revision merely because a client
  lost a response.
- A sparse enrichment joins a frozen, complete sidecar to one exact base revision. It does not
  silently fill gaps or rebase onto a new head.
- Workspace placement is presentation. Dataset, revision, run, artifact, and provider identities stay
  stable when an item is moved or renamed.
- An optional backend or provider can claim only the capabilities it implements and tests.

## Evidence and related guides

These are executable contracts as well as product descriptions. The core coverage includes
[dataset revisions](../kernel/hub/tests/test_dataset_revisions.py),
[admitted local inputs](../kernel/hub/tests/test_local_run_input_admission.py),
[write admission](../kernel/hub/tests/test_write_admission.py),
[sparse merge publication](../kernel/hub/tests/test_merge_columns.py),
[keyed upsert](../kernel/hub/tests/test_keyed_upsert_api.py),
[durable recovery](../kernel/hub/tests/test_durable_local_write_tasks.py), and
[Workspace storage](../kernel/hub/tests/test_workspace_storage.py). The cross-surface browser journey
also exercises Source → Write → receipt/revision → Jobs/Inbox → Workspace → restart recovery in
[`web/e2e/default-write-journey.spec.ts`](../web/e2e/default-write-journey.spec.ts).

For implementation detail, use the focused guides rather than expanding this page:

- [Catalog](CATALOG.md) for discovery, revision inspection, lineage, and views.
- [Plugins](PLUGINS.md) for extension contracts and conformance expectations.
- [Supported deployments and trust model](SUPPORT.md) for security and operational boundaries.
- [Ray](RAY.md) and [Durable Ray Jobs](RAY_JOBS.md) for the optional backend’s exact support matrix.
- [CI and release gates](CI.md) for the evidence required on a release commit.

## Roadmap

The only public product roadmap is [#175](https://github.com/pengw0048/data-playground/issues/175).
It records the current release- and demand-gated work. This document intentionally contains no phase
checklists, closed-issue bookkeeping, provider-specific plans, or proposed APIs: update the roadmap
when a real supported workflow activates new product work, then update this guide when that work ships.
