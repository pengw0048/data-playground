# Versioned data and durable execution

This document records both the delivered foundation and the remaining product and architecture roadmap
for reproducible dataset work, column-oriented enrichment, long-running jobs, and heterogeneous
research data. It is intentionally capability-led: the sections below distinguish behavior on `main`
from the semantics that still need a numbered implementation issue.

The goal is not a thin first release. Each delivery phase below is a complete, supportable product
slice with an explicit exit gate. Later phases extend the same identities and state machines instead
of introducing parallel concepts for local, distributed, or provider-backed execution.

## Product decisions

The roadmap is built around six decisions.

1. A dataset is a stable logical identity with an ordered revision history. A physical URI is an
   implementation detail, not the dataset identity.
2. A run resolves every input to an exact revision before execution. Moving catalog heads cannot
   change an admitted run.
3. A write is a typed, idempotent intent that creates a new revision. Append, replace, upsert, and
   merge-columns are different operations, not variants of an ambiguous overwrite switch.
4. Sparse enrichment is a first-class transform. Computing a few new columns must not require
   copying every untouched column through every worker.
5. The browser is an observer of durable work. Closing a tab, changing canvases, or reconnecting
   must not own the lifetime of a submitted run.
6. Heterogeneous temporal data is a collection of related streams, spans, and assets. It should not
   be forced into one giant table before it can be discovered, inspected, filtered, or analyzed.

These decisions share one foundation: stable identities for resources, revisions, run manifests,
tasks, attempts, checkpoints, and artifacts.

## North-star workflows

The architecture is successful when these workflows feel ordinary.

**Reproduce an analysis.** A researcher opens a dataset, compares its revisions, pins one on a Source
card, runs a canvas, and later reopens the exact inputs, code, output revision, and distribution
report. A newer append to the dataset does not alter the old run.

**Enrich a wide dataset.** A 50-column dataset feeds a transform that reads two columns and produces
one derived column. Workers write a small row-identity sidecar. The Write card validates coverage,
offers Add/update columns, and publishes a new dataset revision without sending the other 48 columns
through the transform.

**Leave a batch running.** A canvas fans out asynchronous requests, records their external handles,
waits without occupying workers, collects and verifies results, and gathers them into a revision. The
researcher closes the browser, watches progress later in Jobs, and receives a completion or
attention item in Inbox.

**Understand heterogeneous data before training.** A researcher filters episodes by interaction,
motion, viewpoint, stream coverage, and quality; opens synchronized camera, depth, pose, action, and
annotation streams; saves a reproducible subset; and compares distribution reports across revisions.

## Delivered foundation and remaining gaps

Status verified against `main` on 2026-07-18. The public release tracker is
[#174](https://github.com/pengw0048/data-playground/issues/174); the ordered product work is
[#175](https://github.com/pengw0048/data-playground/issues/175). This table is a navigation aid, not a
second issue tracker.

| Area | Delivered on `main` | Remaining boundary |
| --- | --- | --- |
| Workspace | A local Workspace composes canvases, datasets, folders, local placement, browse, search, and read-only provider mounts. | Complete provider dataset-to-Source and overlay workflow remains in [#468](https://github.com/pengw0048/data-playground/issues/468). |
| Exact reads | Native revision-capable adapters expose bounded revision history and exact reads; ordinary local file Sources are admitted as immutable managed snapshots before preview or execution. | A complete researcher-facing DatasetView/report model is [#311](https://github.com/pengw0048/data-playground/issues/311); a mutable-only provider must still be labeled and cannot be made reproducible by UI wording. |
| Managed writes | Typed managed-local create and replace writes freeze destination, expected head, schema, provenance, and idempotency; managed Lance append is separately admitted. Receipts reconcile publication evidence. | Key-based upsert and sparse merge-columns require certified row identity and are intentionally deferred to [#310](https://github.com/pengw0048/data-playground/issues/310) and [#489](https://github.com/pengw0048/data-playground/issues/489). |
| Durable work | Task/Attempt state supports managed local writes, external waits, checkpoints, bounded fan-out, restart/retry/cancel recovery, Jobs, and Inbox. The browser observes rather than owns durable work. | Backup/restore certification is active in [#485](https://github.com/pengw0048/data-playground/issues/485). This is not yet a general scheduler or arbitrary provider-job platform. |
| Extension boundary | Installed-wheel plugin contracts and conformance cover catalog composition, read-only mounts, adapters, nodes, destinations, runners, capabilities, and telemetry. | New SPIs need a real consumer and deterministic conformance; provider-specific operational behavior remains outside core. |
| Research semantics | The catalog can retain bounded lineage, schemas, previews, relationships, and native revision facts where an adapter supplies them. | Exact DatasetViews/distribution reports, sparse sidecars, and compound temporal data remain the scoped tracks [#311](https://github.com/pengw0048/data-playground/issues/311), [#310](https://github.com/pengw0048/data-playground/issues/310), and [#312](https://github.com/pengw0048/data-playground/issues/312). |

The rest of this document keeps the north-star semantics for the remaining work. It does not imply that
every type, operation, or UX described below is available today.

## One workspace for data and work

The Files and Catalog experiences should converge on a single workspace resource hierarchy.

### Resource model

A WorkspaceResource has:

- a stable resource ID;
- a resource type such as folder, dataset, canvas, saved view, report, or artifact collection;
- a parent resource ID and display name;
- an optional provider mount and provider-native stable ID;
- a target reference appropriate to the resource type; and
- local presentation and sharing metadata.

Paths are presentation. They are never identity. Moving or renaming a resource changes its placement,
not every reference to it.

Folders may contain datasets and canvases together. A researcher can place a cleaning canvas beside
the dataset it explains, keep a distribution report next to both, and filter the tree by resource
type without losing the shared context.

### Provider mounts and overlays

An external catalog hierarchy is mounted into the workspace rather than copied into core metadata.
Core stores an overlay for local resources and presentation:

- provider-owned datasets remain read-only when the provider is read-only;
- a canvas may be placed virtually inside a mounted folder without writing it into the provider;
- local tags, bookmarks, saved views, and reports may decorate a provider dataset;
- provider rename and move events follow the provider stable ID;
- a deleted or inaccessible provider object becomes an explicit orphan or tombstone, never a silent
  retarget to a same-named object; and
- name collisions are displayed with provider and resource-type context.

The overlay model is also the public plugin pattern. A provider supplies identity, hierarchy,
capabilities, and resource events. Core supplies placement, canvases, reports, search composition, and
the common UX.

### Workspace acceptance

This part is complete only when:

- rename and move preserve all references;
- external read-only folders behave truthfully;
- provider deletion and permission loss produce recoverable orphan states;
- global search returns datasets, canvases, views, and reports with type and mount context;
- a canvas can link several datasets without becoming owned by one physical revision; and
- backup and restore preserve the local overlay without claiming to back up provider-owned data.

## First-class dataset revisions

Versioning is an identity model, not a label on a card.

The current foundation covers native revision reads where an adapter provides them and exact admission
for ordinary local files. The complete cross-provider model specified in this section remains future
work; in particular, UI pinning, as-of resolution, retention, and history are not implied for an
adapter that has no corresponding capability.

### Core types

| Type | Meaning |
| --- | --- |
| DatasetId | Stable logical dataset identity across revisions and physical locations. |
| RevisionId | Immutable identity for one committed dataset state. Provider-native when safe, otherwise core-managed. |
| DatasetRef | User intent: follow latest, pin one revision, or resolve as of a time or catalog event. |
| ResolvedDatasetRef | DatasetId plus the exact RevisionId and physical read binding chosen at admission. |
| DatasetRevision | Parentage, schema snapshot, content identity, row statistics, commit metadata, producer, and physical artifacts. |
| RevisionLease | A durable reason that a historical revision cannot be reclaimed. |

Logical metadata and revision metadata are separate. Folder placement, description, ownership, and
default policies normally belong to DatasetId. Schema, rows, fingerprints, parents, producer run, and
physical artifacts belong to DatasetRevision.

### Provider capability classes

Providers fall into three honest categories:

1. Native revision providers can open an exact provider revision and enumerate history.
2. Core-managed providers publish immutable physical generations and let core own the revision
   ledger and current pointer.
3. Mutable-only providers cannot guarantee historical reads. They may follow latest, but the UI and
   run manifest must say that the source was not pinnable.

Lance is the native-revision reference implementation: it exposes exact dataset versions. Managed
immutable files provide the parallel core-owned path. The public types and acceptance suite remain
format-neutral.

Core must never synthesize a reassuring version such as v1 when the provider supplied no revision.
Unknown means unknown.

Mutable-only providers are source-only in the revisioned write model. A write must either publish
through a core-managed immutable snapshot, use a provider that returns a real revision receipt, or be
rejected before execution. Core does not claim reproducible writes against a mutable head.

### Admission and reproducibility

Run admission performs one resolution protocol:

1. validate every DatasetRef and the provider capability needed by the graph;
2. resolve follow-latest or as-of references through bounded provider calls, recording each
   provider's revision evidence and resolution time;
3. acquire revision leases where core controls retention;
4. atomically save the complete resolved input set and its leases in an immutable run manifest;
5. build preview, profile, cache, and execution keys from that same set; and
6. dispatch only after the manifest and ownership are durable.

Providers do not share a transaction, so a multi-provider manifest does not claim one global
wall-clock snapshot. It does guarantee that every recorded binding is exact and will not move after
admission.

Preview and full execution use the same resolver and identity rules. A full run launched from a
current preview reuses that preview's resolved input set by default. Refresh to latest is an explicit
action: if it resolves a different revision, the UI shows the drift and invalidates the old preview
before execution. Independent runs may legitimately resolve follow-latest to different revisions. A
run that starts on revision R7 still reads R7 even if R8 becomes current one millisecond later.

### Catalog and Source UX

The Catalog detail page should provide:

- paginated revision history;
- parent and merge lineage;
- schema, row-count, and statistics diffs;
- producer run, write intent, and commit receipt;
- pin, bookmark, restore-as-new-revision, and retention actions;
- the reason a revision cannot be deleted; and
- a clear warning for mutable sources that cannot reopen history.

Restore never rewinds the current pointer to an old identity. It creates a new revision whose content
comes from the selected historical revision, whose parent is the current head, and whose lineage also
records restoredFrom. Publication still uses expected-head compare-and-swap.

The Source card should show one of:

- Following latest, with the currently resolved revision shown on each run;
- Pinned to revision, with commit time and parent context; or
- As of, with both the requested time and resolved revision.

Changing the reference invalidates downstream previews and results, shows the schema diff, and
requires a new run manifest. Canvas history and dataset revision history remain separate timelines;
the run manifest connects them.

### Revision invariants

- A RevisionId never changes meaning.
- An admitted run has one immutable resolved input set.
- Cache, lineage, history, reports, and artifacts record RevisionIds, not only current URIs.
- A revision cannot be reclaimed while referenced by a run, canvas pin, view, report, checkpoint, or
  retention policy.
- Unregistering a dataset does not silently destroy retained historical artifacts.
- Backup and restore preserve the revision ledger, references, and current pointer consistently.

## Typed write intents

Adding more strings to writeMode will not produce safe retries. The execution boundary should use a
typed WriteIntent.

Today, the managed-local contract implements create and replace for files and append for managed Lance
destinations. The broader operation table below defines the intended extension boundary; it does not
advertise upsert or merge-columns before their row-identity prerequisites land.

### WriteIntent

A WriteIntent records:

- target DatasetId, or a request to create one;
- operation;
- expected base RevisionId or explicit no-base condition;
- input artifact identities;
- schema evolution policy;
- key and row-identity policy where required;
- conflict policy;
- idempotency key;
- publication and retention policy;
- requester and immutable run manifest identity; and
- provider capability evidence used at admission.

The user-facing operations are:

| Operation | User meaning | Required guarantees |
| --- | --- | --- |
| Create dataset | Publish a new logical dataset and first revision. | Name/identity conflict handling and idempotent create. |
| Replace with new revision | Make a complete new state current. | Expected-head compare-and-swap; the previous revision remains historical. |
| Append rows | Add a deterministic row set. | Idempotent commit or a receipt that can reconcile an unknown response. |
| Upsert or update rows | Change rows selected by a declared key. | Key uniqueness, match policy, delete semantics, and conflict checks. |
| Add or update columns | Merge a sparse sidecar into a base revision. | Stable row identity, coverage policy, and deterministic merge plan. |

The UI says what will happen: target dataset, base revision, new-revision behavior, schema changes,
conflict policy, and estimated data movement. It does not use overwrite or merge without explaining
the resulting revision.

### Attempts and receipts

A WriteAttempt may be retried; a WriteIntent is stable. A successful publication returns a
PublicationReceipt containing:

- intent and attempt identity;
- new RevisionId and parent RevisionId;
- provider commit or transaction identity;
- schema and row summary;
- physical artifacts and checksums;
- current-pointer compare-and-swap outcome; and
- enough information to reconcile a response lost after commit.

If the provider committed but the response was lost, recovery asks for the receipt by idempotency key.
It must not append again. If the catalog head moved beyond the expected base, the operation fails with
a conflict and offers abort, rebase, or recompute. It never silently overwrites the newer revision.

### Capability discovery

Adapters declare exact-version read, supported write operations, transaction scope, idempotency,
compare-and-swap, stable row identity, schema evolution, and reconciliation capabilities. Admission
and UI are derived from that declaration. A provider that cannot make append retry-safe does not
advertise distributed retryable append.

## Sparse sidecars and merge-columns

Many research transforms read a small subset of a wide dataset and produce one or two derived
columns. The platform should represent that directly.

### Artifacts and plans

A TransformArtifact is an immutable sidecar containing:

- base DatasetId and base RevisionId;
- one stable row-identity column;
- only the derived or replacement columns;
- producer code, model, configuration, and input identities;
- schema, row count, uniqueness, coverage, and checksums; and
- partition or bucket metadata.

A MergePlan binds:

- base revision and sidecar artifact;
- target column mapping;
- row-identity strategy;
- expected one-to-one, one-to-many, or partial coverage cardinality;
- duplicate, missing, extra, null, and column-collision policies;
- deterministic bucket algorithm and count;
- expected current head;
- output schema; and
- one idempotency key for the complete intent.

Provider-native row identities may be used only when their revision scope and stability are explicit.
A physical row offset is not assumed stable across compaction or revision changes. A declared business
key is acceptable only after its uniqueness and null policy are validated.

### Deterministic bucketed execution

The generic distributed algorithm is:

1. validate the sidecar against the exact base revision;
2. canonicalize row identities and assign them to deterministic hash buckets;
3. write immutable sidecar buckets with counts and checksums;
4. join each base bucket with its sidecar bucket using the declared cardinality policy;
5. produce bucket results and receipts without moving untouched columns through transform workers;
6. assemble or commit the new dataset revision; and
7. compare-and-swap the current pointer only after the complete publication is attested.

Hash function, seed, null encoding, byte normalization, and bucket-count rules are public contracts so
local and distributed implementations produce identical results.

### Checkpoint and resume

A BucketCheckpoint stores the complete MergePlan identity, input checksums, bucket result checksum,
row counts, attempt state, and publication receipt. Resume is admitted only when the new request has
the same base revision, sidecar, code/config identity, schema policy, and bucket plan.

Resume means continuing the same deterministic intent. It never means applying old temporary output
to a newer dataset. If the head advanced, the user chooses:

- Abort and keep the sidecar for inspection;
- Rebase, after proving row identities and changed columns are still compatible; or
- Recompute against the new base.

The UI offers Resume only after validation finds reusable checkpoints. Before execution it shows key
coverage, duplicates, missing and extra rows, column conflicts, estimated shuffle, bytes rewritten,
and the revision that will be created.

### Merge release gate

- Local and distributed results pass the same golden differential suite.
- Duplicate, missing, extra, null, and collision policies have explicit fixtures.
- Killing every bucket and publication boundary reproduces the same intent and content checksums,
  publishes at most one revision, and reconciles response loss to the original receipt.
- Retrying an unknown commit never publishes duplicate changes.
- A concurrent head update never loses data.
- Partial bucket artifacts are inspectable but cannot masquerade as a published dataset revision.

## Durable background runs and external waits

A long run belongs to the service, not the open canvas tab. The durable control plane should support
local work, distributed workers, and asynchronous provider jobs with one model.

The current Task/Attempt foundation already covers managed local writes, one external-wait contract,
linear checkpoints, bounded fan-out, Jobs, and Inbox. The provider-neutral state machine and HA
semantics below describe the remaining extension contract, not an implicit scheduler or guarantee for
every backend.

### Durable entities

| Entity | Responsibility |
| --- | --- |
| Run | User intent, immutable graph snapshot, resolved input revisions, terminal outcome. |
| RunAttempt | One orchestration attempt and its owner lease. |
| Task | Durable unit in the run dependency graph. |
| TaskAttempt | One execution attempt, retry classification, timing, and logs. |
| ExternalTaskBinding | Provider name, opaque external handle, reconciliation identity, and expiry. Never credentials. |
| Artifact | Immutable input, intermediate, sidecar, report, or final result with ownership. |
| Checkpoint | Validated resume state bound to an exact task intent. |
| TaskEvent | Append-only state evidence used for audit, recovery, and UI updates. |
| Notification | Durable completion or attention event for Jobs and Inbox. |

Terminal outcome and current phase are separate. Useful phases include queued, executing,
waiting_external, retry_wait, collecting, publishing, recovery_blocked, cancelling, and terminal.
Expected external waiting is not shown as stalled. Unknown progress is not rendered as a fabricated
percentage.

### Provider task SPI

An asynchronous task provider implements bounded, idempotent operations:

- prepare and validate admission;
- submit with an idempotency key;
- reconcile an unknown submit result;
- poll one known external handle;
- optionally reconcile or poll a bounded batch of known handles when the provider has an efficient
  batch API;
- accept an authenticated provider callback as a scheduling hint, then reconcile durable provider
  state before advancing the task;
- request cancellation;
- collect result artifacts with checksums; and
- release or expire provider resources.

Each external handle retains its own ExternalTaskBinding even when calls are batched. Batching is an
I/O optimization, not a shared lifecycle identity.

A callback carries a provider event ID, the binding identity, and authenticity evidence. Core records
event deduplication before scheduling reconciliation. The callback is never sufficient evidence of a
terminal result by itself.

Each call is bounded. Waiting does not hold a kernel thread, worker slot, or database transaction. The
database stores next_poll_at, retry class, backoff, jitter, provider rate-limit state, and the exact
external handle. Credentials are resolved at call time from a credential reference and never written
into the task ledger. Claims are scheduled fairly across provider and credential bindings so one large
batch cannot starve unrelated work or bypass a provider quota.

### Dispatcher and high availability

SQLite supports one durable dispatcher in the local process. PostgreSQL supports multiple dispatchers
using database time, row-level claims, and expiring owner leases. A dispatcher claims due work, makes
one bounded provider call, records the result, and releases the claim.

No separate queue is required until measured scale demonstrates that the database-backed dispatcher
is insufficient. The platform owns durable intent, task state, artifacts, and UX; it is not trying to
replace every provider scheduler.

Wait, fan-out, and gather are durable dependencies:

- fan-out creates child tasks with stable identities;
- wait schedules reconciliation without sleeping a worker;
- gather starts only after its declared dependency policy is satisfied; and
- partial failure and retry policy are visible at the task level.

### Browser and canvas behavior

- Closing or navigating away from a canvas does not cancel an admitted run.
- Canvas edits after admission do not mutate the run's graph snapshot.
- Polling is fenced by workspace, canvas, node, run ID, and local generation so a late response cannot
  update a different canvas that reused a node ID.
- Network loss leaves the run in an unknown-observation state, not idle.
- Cancel shows requested, stopping, and a verified terminal outcome. A failed cancel request remains
  visible and retryable.
- Deleting a canvas with active work requires an explicit detach, block, or cancel policy.
- Hub restart recovers leases, external bindings, due polls, and artifact publication. Machine
  shutdown guarantees remain backend-specific and are stated honestly.

### Jobs and Inbox

A global Jobs surface shows work across canvases:

- run, task, canvas, and node context;
- phase, attempt, last contact, and next retry;
- expected wait versus suspected loss of contact;
- provider and log links where available;
- input revisions and produced artifacts;
- cancel, retry, resume, and reconcile actions allowed by current state; and
- attention items such as conflicts, expired provider results, missing credentials, or recovery blocks.

Completion notifications land in an Inbox and link back to the exact run manifest and results. A
researcher should never have to remember which canvas tab was open when a long job finished.

### Durable task release gate

- Kill and restart at every state transition and publication boundary.
- Exercise duplicate dispatchers, late polls, duplicate callbacks, provider outage, rate limiting,
  expired results, missing credentials, response loss, and cancel/complete races.
- Prove browser close, canvas switch, and hub restart recovery.
- Prove there is no busy-wait worker for waiting tasks.
- Publish terminal state and artifact ownership exactly once, with receipt-based recovery.
- Resume artifact downloads and verify checksums before publication.

## Heterogeneous and robotics data

Robotics and embodied-data research adds multiple cameras, depth, poses, actions, state, calibration,
annotations, and clocks. The product should model those relationships without embedding one
organization's dataset schema in core.

### Compound dataset

A compound dataset revision contains:

- episodes or sessions;
- named temporal spans;
- stream descriptors;
- media and array assets;
- relationships among episodes, streams, subjects, tasks, and environments; and
- annotations, saved views, and reports that reference exact revision-scoped identities.

A compound revision binds every member asset to an exact identity, provider revision where available,
and checksum. A StreamDescriptor records modality, clock or time domain, timestamp unit and epoch,
monotonicity and uncertainty, sampling rate, coverage intervals, encoding, shape, unit, coordinate
frame, handedness, pose convention, calibration reference and validity interval, and asset mapping.
The compound manifest contains a coordinate-frame graph rather than isolated frame-name strings.
Action and state streams declare whether values are absolute, delta, velocity, or another control
semantic, plus units and normalization. Streams may include multiple cameras, depth, audio, pose,
state, action, force, annotation, or derived features.

Large media stays in external assets with frame and range mappings. It is not copied into a metadata
table merely to fit the catalog.

### Time and alignment

Raw timestamps and clock mappings are preserved. Alignment and resampling are explicit derived
operations that record:

- source and target time domains;
- offset and drift model;
- tolerance;
- interpolation or nearest-sample policy;
- gap behavior; and
- the exact input revisions.

Missing intervals remain visible. A derived aligned view never erases the raw timing evidence.

### Quality, filtering, and discovery

Quality assessment and filtering are separate:

- quality signals are non-destructive annotations with producer and revision identity;
- a DatasetView stores parent revision, predicate, sampling seed, strata, feature or embedding
  revision, and whether it is virtual or materialized; and
- filtering produces a reproducible view or new revision rather than deleting evidence silently.

Discovery facets should include task, object interaction, motion, embodiment, viewpoint, modality
coverage, quality, source, duration, and revision. Embedding and motion projections always display the
model, parameters, input revision, approximation, and freshness. They support exploration; they are
not presented as objective ground truth.

### Distribution analysis as a product

Researchers need to understand data before training. Distribution reports should cover:

- stream coverage and missingness;
- episode and span length;
- sample rates and temporal gaps;
- task, embodiment, environment, and viewpoint balance;
- motion direction and magnitude;
- object interactions;
- duplicates and near-duplicates;
- clusters, outliers, and bias slices; and
- differences between dataset revisions or saved subsets.

A report is a versioned artifact. It can be saved in the workspace, compared with another report,
opened at the underlying samples, and referenced by a canvas. Every chart states its revision,
sampling method, filters, approximation, and unknown-data handling.

### Research UX

The primary inspector for a compound dataset is a synchronized, multi-pane timeline:

- linked camera and depth playback;
- signal plots for action, state, pose, and derived features;
- annotations and selected spans;
- calibration and coordinate-frame context;
- coverage and missing-data bands; and
- direct navigation from a distribution slice to representative episodes.

A table remains useful for episode metadata and summaries, but it is not the only representation.

### Extension and conformance model

Providers extend compound data through stream manifest readers, converters, modality viewers,
feature extractors, quality assessors, distribution metrics, and search facets. Core owns stable
descriptors, revision identity, the timeline contract, reports, and plugin composition.

The public test kit includes a synthetic multi-stream fixture with:

- multiple cameras and sample rates;
- clock offset and drift;
- gaps and a missing modality;
- a corrupt frame;
- calibration changes;
- long and short episodes; and
- deterministic motion, duplicate, and distribution ground truth.

An integration is supported only after it passes this local fixture and its provider-specific live
suite.

## Public plugin and integration strategy

Core contracts remain provider-neutral. Provider or deployment-specific packages live outside the
core repository when they bring private schemas, credentials, infrastructure clients, or release
cadence.

The public repository should ship:

- typed SPIs for resource mounts, revision reads, transactional writes, merge capabilities, task
  providers, stream manifests, search facets, viewers, and metrics;
- a conformance test kit with deterministic fakes;
- capability negotiation and a compatibility manifest;
- example plugins that use only public data and local services; and
- contract versioning and deprecation policy.

Every new SPI ships with its deterministic fake and conformance suite in the same delivery phase.
The later compatibility matrix expands coverage across versions and live providers; it is not the
first validation of an already-published contract.

An external plugin repository should run four layers of tests:

1. deterministic conformance tests on every change;
2. core and plugin version-matrix tests;
3. provider emulator or ephemeral integration tests where available; and
4. live credentialed tests on a protected schedule and before release.

Core CI must not require private credentials. Live suites report the exact core commit, plugin commit,
provider environment, and capabilities tested. A provider-specific success cannot broaden the public
core support claim beyond the generic contract it exercised.

## Delivery plan

The remaining phases are ordered by invariants, not calendar estimates. Completed foundations are
[#277](https://github.com/pengw0048/data-playground/issues/277) (exact revisions/admission),
[#308](https://github.com/pengw0048/data-playground/issues/308) (typed managed writes), and
[#309](https://github.com/pengw0048/data-playground/issues/309) (durable Tasks/Attempts). The immediate
release closeout is backup/restore certification [#485](https://github.com/pengw0048/data-playground/issues/485);
it remains open and must not be inferred from the Task implementation alone. The current leaf order and
cross-product dependencies belong to [#175](https://github.com/pengw0048/data-playground/issues/175).

The phases below describe remaining completion work. Local resume depends on the durable task and
checkpoint foundation. Distributed merge additionally depends on revisioned writes and local merge
correctness.

### Historical Phase 0 — truthful current behavior

Deliver:

- fence run polling by canvas, run, and generation;
- make cancel and network failure states truthful;
- define active-run behavior for navigation, graph edits, and canvas deletion;
- distinguish expected waiting, retrying, publishing, and loss of contact in the public status model;
- remove synthetic unknown versions from the UI; and
- record performance and support baselines for local and optional execution profiles.

Exit gate:

- no late response can update another canvas;
- cancel failure never appears as confirmed cancellation;
- active work survives browser navigation where the backend claims durability; and
- current docs and UX make no claim that exceeds tested behavior.

### Phase 1 — complete revision and workspace identity

Deliver:

- DatasetId, RevisionId, DatasetRef, ResolvedDatasetRef, DatasetRevision, and RevisionLease;
- exact-revision adapter capability, Lance exact-version reads, and a managed immutable-file
  implementation;
- run-manifest resolution shared by preview, profile, cache, and execution;
- catalog history, revision diff, pin, retention, and restore-as-new-revision;
- Source follow-latest, pinned, and as-of UX;
- WorkspaceResource hierarchy and provider overlay persistence;
- Canvas, dataset, view, and report placement in the same tree; and
- a deterministic fake revision provider and public revision conformance suite.

Exit gate:

- concurrent head changes cannot alter admitted reads;
- a full run launched from a preview either reuses its resolved inputs or surfaces revision drift
  before execution;
- history, cache, lineage, backup, restore, and GC preserve exact revisions; and
- provider move, rename, deletion, and permission-loss fixtures preserve or explicitly orphan overlays.

### Phase 2 — extend transactional write intents

Deliver:

- WriteIntent, WriteAttempt, PublicationReceipt, capability discovery, and reconciliation;
- complete local create, replace, and append flows for managed revisions and Lance;
- expected-head compare-and-swap and user-visible conflicts;
- schema evolution and key policies;
- revision-aware write lineage and history;
- crash recovery at prepare, write, commit, response, and catalog publication boundaries; and
- deterministic fake write providers and a conformance suite for every advertised operation.

Exit gate:

- retries never duplicate append;
- a lost response reconciles to the original receipt;
- concurrent writes cannot silently lose data;
- each success creates one inspectable revision; and
- unsupported provider semantics are unavailable in UI and rejected before allocation.

### Phase 3 — extend durable task and checkpoint foundation

Deliver:

- durable RunAttempt, Task, TaskAttempt, ExternalTaskBinding, Artifact, Checkpoint, Event, and
  Notification records;
- database-backed dispatcher for local SQLite and multi-instance PostgreSQL;
- asynchronous provider SPI with single and bounded-batch reconcile/poll, cancel, callback hints,
  and collect;
- durable wait, fan-out, and gather;
- bounded polling, fairness, backoff, jitter, rate limits, and result expiry;
- Jobs and Inbox UX;
- migration of compatible existing durable backends to the common contract; and
- a deterministic fake task provider and state-machine conformance suite.

Exit gate:

- browser and hub restart recovery pass;
- every phase boundary passes kill/restart and duplicate-supervisor tests;
- waiting consumes no execution worker;
- cancel and completion races have one receipt-proven terminal winner; and
- collected artifacts are resumable and checksum-verified.

### Phase 4 — Local sparse enrichment

Deliver:

- TransformArtifact and MergePlan contracts;
- stable row-identity validation;
- local add/update-columns execution;
- duplicate, missing, extra, null, and collision policies;
- deterministic buckets using the shared Task, TaskAttempt, Artifact, and Checkpoint entities;
- validated resume, rebase, recompute, and abort UX; and
- sidecar and merge lineage in Catalog.

Exit gate:

- local crash injection reproduces the same intent and content checksums, publishes at most one
  revision, and reconciles response loss to its original receipt;
- all cardinality policies have golden fixtures;
- head conflicts are explicit;
- no untouched column must pass through the transform worker; and
- incomplete sidecars remain inspectable but cannot become published revisions accidentally.

### Phase 5 — Distributed merge and provider adapters

Deliver:

- distributed execution of the same public bucket plan;
- per-bucket leases, receipts, retries, and progress;
- deterministic local/distributed differential tests;
- provider-native fast paths behind the generic capability;
- a Lance merge-columns fast path behind the same generic MergePlan and receipt contract;
- large revision-history pagination and retention operations; and
- plugin version-matrix and protected live integration suites.

Exit gate:

- local and distributed outputs match exactly;
- retrying or duplicating any worker does not duplicate publication;
- coordinator loss resumes from durable checkpoints;
- current-head conflicts never lose data; and
- failure of one provider cannot corrupt another provider's revision or artifact ownership.

### Phase 6 — Compound temporal data and distribution analysis

Deliver:

- compound dataset, episode, span, stream, asset, and clock descriptors;
- explicit alignment, window, and resample operations;
- synchronized multi-pane exploration;
- reproducible DatasetView filtering and sampling;
- versioned distribution and diversity reports;
- search facets for interaction, motion, embodiment, coverage, and quality; and
- plugin converters, viewers, feature extractors, and metrics.

Exit gate:

- the synthetic multi-stream fixture round-trips without losing timestamps, gaps, or calibration;
- alignment drift and gap golden tests pass;
- saved views and reports reproduce against the same revision;
- reports retain reproducible slice and query provenance and open representative or matching samples;
- long-episode and large-catalog performance budgets are met; and
- approximate embeddings and sampling remain labeled with model, revision, parameters, and freshness.

### Cross-cutting production Definition of Done

This is not a final hardening phase. Every Phase 1–6 exit gate includes the applicable migration,
backup, observability, compatibility, and failure-recovery work below. The combined product is not
declared mature until:

- forward and rollback or forward-only migration policies are documented and drilled;
- backup and restore cover revision ledgers, workspace overlays, task state, checkpoints, receipts,
  and artifact ownership;
- queue age, task lease, poll failure, publication conflict, revision retention, and GC metrics have
  actionable dashboards and alerts;
- quota, rate-limit, storage-pressure, and provider-outage behavior degrades truthfully;
- supported browser, local, shared-service, and distributed profiles have explicit scale limits;
- upgrade and mixed-version plugin compatibility tests pass;
- chaos tests cover database, process, worker, object-store, and provider failures; and
- release evidence records exact commits, environments, commands, and specialized acceptance results.

## Failure-injection matrix

Phase exit gates use one shared adversarial matrix rather than separate happy-path certifications.

| Domain | Required adversarial cases |
| --- | --- |
| Revisions | Concurrent append, replace, compaction, and catalog refresh; mutable-only sources; historical retention, tombstone, unregister, backup, restore, and GC. |
| Writes and merge | Failure before and after artifact write, provider commit, response, catalog update, and current-pointer publication; duplicate requests and supervisors; schema, key, null, cardinality, checkpoint-intent, and column conflicts. |
| Durable tasks | Late and duplicate poll or callback, callback authentication and event dedupe, provider outage, rate limit, missing credential, expired result, failed collection, database-time lease expiry, and cancel/complete races. |
| Heterogeneous data | Multiple clocks, rates, cameras, drift, gaps, corrupt media, missing modalities, calibration changes, timeline seek accuracy, and deterministic distribution fixtures. |

Across every row, partial artifacts remain owned and reclaimable, no terminal result depends on an
in-memory owner or browser, and recovery distinguishes commit unknown from commit rejected using
durable receipts.

## Tradeoffs and explicit non-goals

- Core will not promise exactly-once behavior for a provider that lacks idempotency, reconciliation,
  or transaction support. It will expose the limitation and restrict unsafe retry.
- Merge-columns creates a new revision. It is not presented as an in-place mutation even when a
  provider implements it efficiently.
- A provider-native row identifier is not treated as globally stable unless its revision scope is
  part of the capability contract.
- The durable task plane is not a general-purpose workflow service. It owns data-work intent,
  dependencies, external waits, artifacts, and user-facing recovery.
- Large media remains referenced by assets; core does not require embedding it in relational cells.
- The public core remains useful offline. Provider-specific packages cannot become mandatory
  dependencies.
- The supported trust model remains local users and trusted collaborators. This roadmap does not add
  adversarial multi-tenant isolation or a zero-trust control plane.
- Current-pointer compatibility for unknown pre-release consumers is not a reason to preserve an
  incorrect identity model. Schema changes still require explicit migration and recovery plans.

## Before implementation issues are opened

This roadmap intentionally stops before creating implementation issues. The first planning pass should
turn each phase into a tracking issue only after the following contracts are reviewed together:

- stable resource, dataset, revision, run, task, and artifact identities;
- exact-revision provider capabilities;
- WriteIntent and PublicationReceipt;
- row-identity and deterministic bucket rules;
- task phase, retry, cancellation, and receipt semantics;
- workspace overlay ownership; and
- compound stream and clock descriptors.

Each implementation issue should name the invariant it establishes, its supported provider and
deployment profile, failure injection, migration impact, UI outcome, and measurable release gate.
That keeps the roadmap general enough for public plugins while making every delivered slice
production-grade and testable.
