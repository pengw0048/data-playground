# Durable Ray Jobs execution

`dp_ray` supports two driver modes over the same IR:

- With no `DP_RAY_JOBS_ADDRESS`, it keeps the development `Popen` driver and the manifest-v2 region
  handoff path. This is zero-configuration and single-host.
- With `DP_RAY_JOBS_ADDRESS`, whole-graph runs use Ray's official Jobs API plus SQL and shared object
  storage. A replacement hub can reattach to the same submission. Multi-region parent orchestration is
  still in-memory, so Jobs mode deliberately does not claim placed child regions. Explicit Ray/GPU pins
  use whole-graph admission instead and therefore cannot silently fall back to local execution.

Ray Jobs does not yet serialize or deliver the hub's admitted exact-revision input manifest. Until
[#303](https://github.com/pengw0048/data-playground/issues/303) is implemented, a run carrying that
manifest is rejected before run/attempt identity, envelope or result-artifact allocation, driver start,
or Jobs API submission. It cannot independently reopen mutable source head under the durable v4
contract.

This is a durable lifecycle implementation, not a claim that the whole Ray backend or the example
deployment is production-ready. The remaining scale, health, resilience, and operator gates are in
[`RAY.md`](RAY.md). It operates within the trusted-workspace boundary in
[Supported deployments and trust model](SUPPORT.md): workers, runtime images, plugins, storage
administrators, and operators are trusted with the workspace.

## Real-service acceptance gate

The restart contract has a separate acceptance workflow so unrelated pull requests and the Ray data
differential do not pay for it. It runs when a pull request changes an owned Jobs lifecycle path,
weekly, on demand, and before publishing a release. See [CI and release gates](CI.md). Run it locally
with:

```bash
scripts/ray-jobs-acceptance.sh
```

It builds one image for the hub-side Jobs client, Ray head, worker, and remote entrypoint, then starts
PostgreSQL 16 and a versioned MinIO bucket. The gate proves that a submitting hub can exit while the
official Ray job is `RUNNING`, a new process reattaches to the same deterministic submission, and the
terminal run history and logical catalog state converge to one publication. Before that restart, it
replaces a managed source's catalog generation and proves the recovered job still reads its hash-bound
generation, then releases the durable source pin at terminal publication. Separate scenarios require an
official `STOPPED`
cancellation acknowledgement and prove that a `SUCCEEDED` job with a missing or corrupt result receipt
is published as failed without exposing its output. Logs, service state, image identity, and disk/Docker
diagnostics are retained as workflow artifacts on both success and failure.

## Required contract

```bash
DP_RAY_JOBS_ADDRESS=https://ray-dashboard.example:8265
DP_RAY_JOBS_CLUSTER_REF=production-ray-east

# Both paths are baked into the Ray image; source is not uploaded per run.
DP_RAY_JOBS_ENTRYPOINT='/app/kernel/.venv/bin/python /app/examples/plugins/dp_ray/_driver.py'
DP_RAY_JOBS_MODULE=/app/examples/plugins/dp_ray/__init__.py
DP_RAY_JOBS_CODE_REF='sha256:0123456789abcdef...'
DP_RAY_JOBS_WORKSPACE=/app/kernel
DP_RAY_JOBS_DATA_DIR=/app/kernel/data

DP_STORAGE_URL=s3://data-playground/outputs
# Optional; defaults to $DP_STORAGE_URL/__ray_jobs__.
DP_RAY_JOBS_ARTIFACT_PREFIX=s3://data-playground/control/ray-jobs
```

`DP_RAY_JOBS_MODULE` has the reference-image default shown above, but setting it explicitly makes a
custom image contract easier to audit. The Jobs address must be HTTP(S) and must not contain userinfo,
query parameters, or fragments. The reference client does not expose token/header/cookie, client-certificate,
or custom-CA configuration. Its endpoint must therefore be protected by a private network or authenticated
service mesh and use the process's ordinary TLS trust; a Jobs endpoint that requires application-level
credentials or direct mTLS is not supported yet. Workload credentials authenticate data access only, not
submit/status/stop control. The job envelope contains the complete graph and arbitrary plugin configuration,
and `result.dpresult` can retain raw workload exception text and output metadata. Operators must treat the
SQL copy plus both object artifacts as sensitive control-plane data and use secret references instead of
embedding credential values in graph configuration.

`DP_RAY_JOBS_CODE_REF` and `DP_RAY_JOBS_CLUSTER_REF` are operator assertions that the implementation
hash-binds and compares; Data Playground does not independently read a container digest or Ray cluster UUID.
The deployment system must inject an immutable image digest and a stable cluster identity, then verify that
attestation outside this plugin. See the remaining runtime-image gate in
[`RAY.md`](RAY.md#production-ownership-gates).

The durable Jobs sink contract is deliberately narrow: it supports only the built-in file adapter
writing non-partitioned Parquet in overwrite mode, backed by a core-managed immutable object attempt.
CSV, JSON, custom-adapter, and adapter-compatibility sinks remain available to the local/Popen Ray path;
an explicit Jobs placement rejects them before allocating or submitting a remote job. Jobs mode also
rejects incomplete image/cluster identity and host-local input or output paths before submission. A
future transactional external sink can provide a stronger idempotency contract, but that extension is
not implemented by the reference Jobs backend today.

## Execution envelope and TOCTOU boundary

The hub resolves sinks and schema references, then creates an exact-field JSON envelope containing the
complete graph, target, target-cone resources, code/module/entrypoint identity, remote paths, and a frozen
allowlisted semantic environment. Unknown or missing fields are corruption. The canonical contract
produces an attempt ID; the complete envelope produces a second SHA-256 binding. The allowlisted
environment excludes known secrets, but the hub cannot prove that arbitrary plugin configuration is
secret-free; artifact and metadata-store access controls remain mandatory.

Durable Jobs currently supports contract version 4 only. That version freezes catalog publication
identity and each sink's canonical, bounded source-parent set alongside its writer mode, logical URI,
and physical attempt URI before submission. The parent map is keyed by the exact sink-target set; each
list is bounded, sorted, unique, and contains only non-empty canonical URIs. A source-free generator may
legitimately freeze an empty list. This also preserves the original sources of a controller-created
region cut: rebuilding the public graph from the artifact may expose only its temporary materialization,
so catalog publication never recomputes lineage parents from that reconstructed graph.
Earlier experimental versions were never released and are intentionally not replayed: an unsupported
version is quarantined and stopped rather than submitted under semantics that cannot satisfy the current
ownership contract.

A release must bump the contract version for any incompatible envelope or result change. The current
implementation has no mixed-version decoder, so that release requires draining active Jobs runs before
upgrading the hubs and Ray image. Rolling hub replacement is supported only when no metadata migration is
required, every process uses the same exact Alembic head, every replacement keeps the v4 reader, and the
image asserted by each active run's `code_ref` remains available. For any schema or contract change,
stop new submissions, drain active Jobs, stop all metadata writers, run the one-shot migration, and then
upgrade the hubs and Ray image.

The Ray entrypoint receives four independent arguments: job URI, expected attempt ID, expected submission
ID, and expected envelope hash. `_driver.py` rereads the object and verifies the exact field set, both
hashes, the semantic-environment hash, and all four arguments **before** importing Ray or workload/plugin
code. A hub validation followed by an object replacement therefore cannot redirect execution.

These hashes are integrity bindings, not signatures. An ordinary S3/GCS object is mutable unless IAM or
retention policy says otherwise. The implementation applies write-once semantics at the application
boundary (an existing different envelope/result is rejected), but production must also enforce the
storage boundary:

- only the control plane may create/read `*.dpjob`; Ray workloads must not update or delete them;
- only the bound workload identity may create its `*.dpresult`, and unrelated attempts must not access
  that prefix;
- the control plane may read results, while overwrite/delete is denied after creation;
- bucket administrators remain trusted, and retention/lifecycle rules must outlive maximum recovery time.

Prefer distinct hub and Ray service identities. The current workload allowlist can pass `AWS_*`,
`DP_S3_*`, or Google credential references for compatibility; those remain broad capabilities. This is
compatible with the trusted-team profile, but it does not provide mutually hostile tenant isolation.

## Frozen semantics and credential rotation

Settings that change execution meaning—plugin selection, memory/shuffle/runtime flags, storage endpoints,
workspace/data paths, and Ray labels—are frozen in the envelope and participate in the attempt hash.
Reattachment and replay use that snapshot even if the replacement hub's environment changed.

Credential values and credential-file/profile selectors are excluded from the semantic snapshot. Each
launch uses the operator's current allowlisted data credentials, so key rotation does not create a new
logical attempt. Hub metadata identity (`DP_DATABASE_URL`), auth signing material, and provider/control
API keys are never sent to the driver.

## Durable state machine

1. The hub first commits `run_id`, authorized `created_by`, and `auth_canvas_id`. Only then may it allocate
   a write-once job envelope. A future workload-identity provider observes that principal in the hub-side
   launch context. The authorization principal and `auth_canvas_id` are not copied into Ray artifacts or
   submission metadata; the graph's own ID remains part of the complete graph envelope.
2. One SQL transaction binds `run_id` to backend, cluster, submission, attempt, object URIs, code ref,
   the non-secret Jobs control address, and a private canonical copy of the hash-bound job envelope while
   updating the prebound queued `run_states` row. The JSON artifact is capped at 64 MiB; its metadata-row
   copy has a separate 8 MiB ceiling. System-generated identity fields and the semantic environment omit
   owner/auth data and known credential variables, but arbitrary graph/plugin configuration is preserved
   verbatim and may itself be sensitive. Oversized jobs fail before the binding transaction. Only after
   that commit does the hub materialize `job.dpjob`; a replacement supervisor recreates the exact bytes if
   the first hub crashes between phases.
3. Status plus a successful Jobs listing distinguish an absent job from an ambiguous API failure. Only
   authoritative absence opens submission/replay. SQL then linearizes exactly one request with a
   DB-clock lease and a CAS that requires `cancel_requested=false`; an expired-owner reclaim moves
   directly to the new owner in that same CAS, and recovery must query Ray again before attempting it.
   A timeout/disconnect never releases the claim after one immediate missing check because the HTTP
   request may still be accepted later. Official Jobs API requests carry a finite connect/read timeout,
   and lease renewal stops after a bounded continuous ownership window; a wedged caller therefore cannot
   exclude a replacement supervisor forever.
4. A result cannot beat an explicit `PENDING`/`RUNNING` Jobs status. Readable result corruption while the
   job is live triggers stop/quarantine, and terminal failure is published only after Ray reports terminal
   or successful listing proves the job absent. A valid hash-bound result is terminal evidence when Ray's
   job metadata is authoritatively gone and no submit request remains unsettled. If an expired `submitting`
   lease could still deliver a delayed request, SQL first records `result_fencing`; the hub races an inert
   fence under the same deterministic ID, stops whichever request Ray accepted, records
   `result_submitted` or `result_stop_fenced`, and re-reads the result only after that writer is terminal.
   A terminated fixed fence settles to the publication-eligible `result_fenced` state so its provenance
   survives a crash before publication; it is never reinterpreted as workload metadata. This internal
   reconciliation never fabricates user cancellation intent and survives process restart.
5. On `SUCCEEDED`, the hub reads and verifies the exact result envelope. Missing results receive a bounded
   consistency grace period; transport/auth failures remain non-terminal and retry. A failed/cancelled
   envelope may retain a hash-bound subset of committed sink URIs as private cleanup evidence, but it must
   not name a primary output and those URIs never enter public status or catalog publication. Whole-graph
   non-write targets are rejected before Jobs allocation because this lifecycle currently publishes only
   an explicit write sink.
6. Supervisors compete for a renewable SQL publication lease. Before any catalog mutation, the winner
   commits exact object inventory, probes the output schema, and freezes one canonical terminal plan in
   `publication_doc`. The same transaction moves the job from `pending` to `effects_started`; successful
   plans pin their exact sink generations, while failed/cancelled plans terminalize every bound sink and
   release its writer leases. Pending publication and submit/fence claims are
   mutually exclusive: a remote
   observation atomically invalidates a pre-effects terminal candidate, while `effects_started` prevents a
   stale observer from submitting or stopping work. Raw remote failure text is never copied into this
   recovery plan. Publication renewal uses the same bounded continuous ownership window, so an owner
   blocked in provider preflight eventually yields its lease; a late return remains fenced by the owner
   CAS, while an `effects_started` winner replays only the frozen idempotent SQL plan.
7. `effects_started` is a write-ahead catalog barrier. A replacement supervisor replays only the prepared
   SQL plan: it does not reread Ray state, job/result artifacts, manifests, or output schemas. Catalog
   pointer, lineage, attempt state, and exact output and lineage publication receipts commit together;
   run-level usage has a separate stable-identity receipt. A later overwrite or unregister waits for the
   temporary publication reference. If it committed before this plan reached the barrier, the stale
   publication fails with an explicit conflict instead of reporting `done` without a readable catalog
   projection.
8. After every required receipt exists, one database transaction CAS-publishes the backend result, public
   terminal `RunState`, and run-history row. It transfers output ownership to the retained run state before
   releasing temporary publication and source references; if bounded detail retention prunes that state,
   the exact attempt becomes eligible for the normal supersession/GC policy while the compact run-ID fence
   still prevents resurrection. Telemetry is best-effort only after that terminal barrier.

Jobs v4 managed outputs currently require the built-in DB-backed catalog. A graph with a write sink rejects
an external catalog before object allocation or Ray submission, even if that provider implements the older
`DurableCatalogPublisher` capability. That interface can acknowledge an at-least-once write, but it cannot
freeze a pre-probed plan, participate in the core object-attempt barrier, or replay exact
output/lineage/usage receipts without rereading mutable artifacts. A future external integration needs an
explicit prepared-plan protocol with those semantics; accepting the older interface here would fail only
after remote execution had already completed.

Output publication keys are per sink, and the same stable effect identity reserves that sink's complete
lineage publication. Every per-source fact from the sink shares one exported `publicationKey`. An exact
supervisor replay is a no-op, while a changed source set, exact source/destination identity, execution
identity, provenance, or mappings is a collision rather than a partial retry. The receipt remains as a
tombstone after unregister, so delayed recovery cannot resurrect removed evidence.

One separate run-level usage event aggregates every distinct parent for the run. Parent aliases are
resolved to stable logical or exact-URI identities before the write-ahead barrier: a generation
replacement receives the usage on its logical dataset, while an unregister becomes an idempotent no-op
and never resurrects the dataset. Two real runs therefore increment popularity twice, while a crash/retry
does not increment it again; each run's immutable lineage facts are separate from the run-usage event.
The current Jobs v4 backend admits at most one write sink and rejects a multi-sink graph before
allocation. The catalog publisher primitive can represent multiple outputs, but it does not provide a
cross-dataset transaction, so the reference Jobs backend will not expose that shape until atomic batch
publication exists.

Terminal `RunState` and backend-detail rows share the normal bounded retention policy. Pruning happens in
the same terminal publication transaction and leaves `run_records` history intact. A separate compact,
permanent terminal run-ID fence is not pruned. It retains terminal status plus the creator, authorized
canvas, and legacy operational canvas identifiers needed to apply the same current authorization policy
after detailed state is gone; these fields stay in SQL and are never copied to Ray artifacts. Deleting a
canvas clears those authorization identifiers while preserving the opaque anti-resurrection fence, so a
new canvas using the same ID cannot inherit an old run. The fence prevents stale supervisors or duplicate
binds from resurrecting a completed run even when no history row exists or bounded history has aged out.
A stale supervisor that loses its publication claim or finds the backend row already pruned consults this
fence, converges its local status, and stops supervising instead of restarting forever.

## Recovery and cancellation

The SQL binding stores the original control address, canonical job envelope, and `cancel_requested`.
Restart recovery does not depend on a PID, local temp directory, or process-local event. Missing local
Jobs configuration is reported
as a non-terminal configuration-unavailable state; it is never reclassified as artifact tampering and does
not stop or replay a real job. A malformed active binding or `RunStatus` document is also fail-closed: the
hub persists a bounded `recovery blocked` diagnosis, emits a structured warning, exposes the run as
non-terminal, and starts no supervisor that could replay it. Cancellation records durable intent but cannot
claim a remote stop until an operator repairs the binding enough to recover its control route.

Backend stall detection is anchored to the last successful Jobs status/list observation, not to generic
`RunState.updated_at`. Healthy same-state polls advance that durable clock at a bounded write rate, while
repeated error-message writes do not hide a control-plane outage. When a successful poll clears a stale
live error without changing `queued`/`running`, the hub persists that clear exactly once rather than
rewriting the full status document on every poll.

Cancellation is the exception: it uses the durable SQL address/submission ID even while local config or
the job artifact is missing/unavailable. Intent is committed before the process-local event. Once intent
exists, a not-yet-claimed attempt cannot submit. If the submit CAS linearized first, that already-authorized
request may finish; cancellation waits for it to appear, then stops and polls it rather than publishing a
prematurely cancelled terminal. `stop_job` is followed until Ray is terminal; an ambiguous control failure
remains non-terminal. `STOPPED`, or authoritative proof that neither a job nor an already-linearized submit
exists, publishes `cancelled`. Lease expiry permits idempotent takeover of the deterministic submission ID;
it does not prove an earlier HTTP request can no longer arrive. For a crashed owner plus concurrent cancel,
the hub therefore submits a fixed, inert, stoppable fence job under that exact same ID after lease expiry.
Ray accepts either the delayed workload or the fence, never both; the winner is then stopped and observed.
The hub validates the winner's Ray Job metadata before atomically recording whether the inert fence or
the bound workload owns that ID. A live workload enters `stopping`; an accepted inert fence enters
`fence_stopping`. Both states exclude terminal publication until `stop_job` is followed by terminal status
or authoritative absence, at which point SQL settles them to `submitted` or `stop_fenced`. A lost submit
response therefore cannot turn an instantly successful fence into a missing-result workload failure, and
a stale publisher cannot cross the effects barrier between winner observation and remote stop. A
concurrent real `SUCCEEDED`/`FAILED` is reconciled normally. If metadata disappears for a previously
accepted ID, cancellation still verifies the hash-bound terminal result; trusted completion wins over a
later cancel request, while a never-linearized queued attempt can cancel directly from authoritative
absence.

Cancellation cannot undo a sink committed before stop. `cancelled` means execution was authoritatively
stopped, not that no physical side effect occurred.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `DP_RAY_JOBS_POLL_S` | `1` | Jobs/control/artifact retry interval |
| `DP_RAY_JOBS_CANCEL_TIMEOUT_S` | `30` | Synchronous wait for durable stop acknowledgement |
| `DP_RAY_JOBS_RESULT_TIMEOUT_S` | `30` | Missing-result grace period after `SUCCEEDED` |
| `DP_RAY_JOBS_SUBMISSION_LEASE_S` | `30` | DB-clock lease around an already-linearized Jobs submit |
| `DP_RAY_JOBS_PUBLICATION_LEASE_S` | `60` | Renewable single-publisher lease |
| `DP_RAY_JOBS_REQUEST_TIMEOUT_S` | `30` | Connect/read timeout for every official Jobs API request |
| `DP_RAY_JOBS_MAX_LEASE_HOLD_S` | `300` | Maximum continuous renewal window for one submit/publication owner |

## Logs and operator access

Shared status exposes only a stable failure code/type. It does not copy Ray Job messages or driver logs,
because remote text can contain credentials that have since rotated and can no longer be reliably
redacted. `RayRunner.logs` likewise returns no remote payload until a protected operator diagnostics
surface exists. Use a Ray Dashboard/log backend protected by TLS, network policy, and the cluster's
authentication boundary. Do not expose port 8265 directly to end users.

## Current limits

- Durable Jobs covers whole-graph execution only. Region data publication uses manifest v2, but the
  multi-region parent is not restart-durable.
- Object-store reads and whole-graph file sinks remain driver-funneled in several paths.
- Live capacity/health discovery, admission backpressure, active-job fault injection, HA/upgrade
  runbooks, and production observability remain open readiness gates. Scoped per-attempt identity is
  defense in depth for a trusted team and would be required for the unsupported hostile-tenant profile.
- Operators must configure finite metadata-database connection and statement timeouts. The bounded owner
  window prevents application keepalives from renewing forever, but it cannot interrupt a database driver
  call that the deployment itself permits to block indefinitely.
- `DP_RAY_JOBS_REQUEST_TIMEOUT_S` covers only Jobs API HTTP. Artifact/object-store calls currently use
  provider defaults, and whole-graph Jobs runs have no automatic execution deadline.
  `DP_RUN_DEADLINE_S` applies to the local/Popen driver, not a submitted Ray Job; provider deadlines,
  admission quotas, and operator cancellation for runaway work remain production readiness gates.
- Every hub that loads the durable backend currently supervises every active Jobs row. At larger hub ×
  active-job counts this amplifies Jobs API/object-store polling; sharded ownership/backoff is remaining
  control-plane operations work.
- Job/result artifacts, including raw negative result details, are retained until operator lifecycle rules
  remove them. No foreground recursive cleanup scans a shared bucket.
- Prepared Jobs publication writes catalog metadata directly in SQL and does not invoke the optional
  semantic embedder. The output is immediately available to catalog/lexical reads, but semantic search
  picks it up only when the catalog's background reindex runs (for example after embedder setup/restart).
- Dynamic `working_dir` and per-run pip upload are intentionally unsupported; code is image-baked and
  bound to the operator-supplied `DP_RAY_JOBS_CODE_REF` assertion.
