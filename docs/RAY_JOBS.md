# Durable Ray Jobs execution

`dp_ray` supports two driver modes over the same IR:

- With no `DP_RAY_JOBS_ADDRESS`, it keeps the development `Popen` driver and the manifest-v2 region
  handoff path. This is zero-configuration and single-host.
- With `DP_RAY_JOBS_ADDRESS`, whole-graph runs use Ray's official Jobs API plus SQL and shared object
  storage. A replacement hub can reattach to the same submission. Multi-region parent orchestration is
  still in-memory, so Jobs mode deliberately does not claim placed child regions. Explicit Ray/GPU pins
  use whole-graph admission instead and therefore cannot silently fall back to local execution.

This is a durable lifecycle implementation, not a claim that the whole Ray backend or the example
deployment is production-ready. The remaining scale, isolation, health, and resilience gates are in
[`RAY.md`](RAY.md).

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
query parameters, or fragments; credentials belong in the Jobs client's supported configuration, not in
SQL, artifacts, logs, or URLs.

Jobs mode rejects incomplete image/cluster identity, host-local input or output paths, partitioned file
sinks, and append file sinks before submission. Replay after authoritative Ray job-metadata loss is safe
only for overwrite/idempotent sinks. A transactional external sink may provide a stronger contract.

## Execution envelope and TOCTOU boundary

The hub resolves sinks and schema references, then creates an exact-field JSON envelope containing the
graph, target, target-cone resources, code/module/entrypoint identity, remote paths, and a frozen non-secret
semantic environment. Unknown or missing fields are corruption. The canonical contract produces an
attempt ID; the complete envelope produces a second SHA-256 binding.

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
`DP_S3_*`, or Google credential references for compatibility; those remain broad capabilities, so a
deployment that forwards one shared credential has not achieved tenant isolation.

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
   launch context; raw principal/canvas identity is not copied into Ray artifacts or submission metadata.
2. One SQL transaction binds `run_id` to backend, cluster, submission, attempt, object URIs, code ref,
   and the non-secret Jobs control address while updating the prebound queued `run_states` row.
3. Status plus a successful Jobs listing distinguish an absent job from an ambiguous API failure. Only
   authoritative absence opens submission/replay. SQL then linearizes exactly one request with a
   DB-clock lease and a CAS that requires `cancel_requested=false`; an expired-owner reclaim moves
   directly to the new owner in that same CAS, and recovery must query Ray again before attempting it.
   A timeout/disconnect never releases the claim after one immediate missing check because the HTTP
   request may still be accepted later.
4. A result cannot beat an explicit `PENDING`/`RUNNING` Jobs status. Readable result corruption while the
   job is live triggers stop/quarantine, and terminal failure is published only after Ray reports terminal
   or successful listing proves the job absent. A valid hash-bound result is terminal evidence when Ray's
   job metadata is authoritatively gone.
5. On `SUCCEEDED`, the hub reads and verifies the exact result envelope. Missing results receive a bounded
   consistency grace period; transport/auth failures remain non-terminal and retry.
6. Supervisors compete for a renewable SQL publication lease. Catalog projection is a required,
   idempotent effect and is retried on failure. Every output must return a durable receipt only after its
   catalog reference is readable; a method return without that receipt cannot publish terminal success.
   One database transaction then CAS-publishes the backend result, public terminal `RunState`, and
   run-history row. Telemetry is best-effort after that barrier.

External catalog providers must implement the runtime-checkable `DurableCatalogPublisher` capability:
`register_output_idempotent(idempotency_key, ...)` and
`record_usage_idempotent(idempotency_key, parents)` with durable idempotency. Publication is at-least-once
at that provider boundary. `register_output_idempotent` must not return until the output reference is
durably readable and must return a matching `CatalogPublicationReceipt`; Jobs validates its key and URI.
Multiple independent sinks are not one cross-dataset transaction. Output keys remain per sink, while one
separate run-level usage event aggregates every distinct parent across all sinks. Thus two real runs
increment popularity twice, a two-sink run increments a shared parent once, and a crash/retry does not
increment it again. Permanent lineage-edge existence is not a run-usage event.

Terminal `RunState` and backend-detail rows share the normal bounded retention policy. Pruning happens in
the same terminal publication transaction and leaves `run_records` history intact. A separate compact,
permanent terminal run-ID fence is not pruned; it prevents stale supervisors or duplicate binds from
resurrecting a completed run even when no history row exists or bounded history has aged out.

## Recovery and cancellation

The SQL binding stores the original control address and `cancel_requested`. Restart recovery does not
depend on a PID, local temp directory, or process-local event. Missing local Jobs configuration is reported
as a non-terminal configuration-unavailable state; it is never reclassified as artifact tampering and does
not stop or replay a real job.

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
A concurrent real `SUCCEEDED`/`FAILED` is reconciled normally. If metadata disappears for a previously
accepted ID, cancellation still verifies the hash-bound terminal result; trusted completion wins over a
later cancel request, while a never-linearized queued attempt can cancel directly from authoritative absence.

Cancellation cannot undo a sink committed before stop. `cancelled` means execution was authoritatively
stopped, not that no physical side effect occurred.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `DP_RAY_JOBS_POLL_S` | `1` | Jobs/control/artifact retry interval |
| `DP_RAY_JOBS_CANCEL_TIMEOUT_S` | `30` | Synchronous wait for durable stop acknowledgement |
| `DP_RAY_JOBS_RESULT_TIMEOUT_S` | `30` | Missing-result grace period after `SUCCEEDED` |
| `DP_RAY_JOBS_SUBMISSION_LEASE_S` | `30` | DB-clock lease around an already-linearized Jobs submit |
| `DP_RAY_JOBS_PUBLICATION_LEASE_S` | `60` | Renewable single-publisher lease |

## Logs and operator access

Failed shared status contains the bounded Jobs API message; it does not copy driver logs. `RayRunner.logs`
is an internal/operator helper with best-effort credential redaction, not an authenticated application
route. Use a Ray Dashboard/log backend protected by TLS, network policy, and the cluster's authentication
boundary. Do not expose port 8265 directly to end users.

## Current limits

- Durable Jobs covers whole-graph execution only. Region data publication uses manifest v2, but the
  multi-region parent is not restart-durable.
- Object-store reads and whole-graph file sinks remain driver-funneled in several paths.
- Live capacity/health discovery, admission backpressure, scoped per-attempt identity, active-job fault
  injection, HA/upgrade runbooks, and production observability remain open readiness gates.
- Every hub that loads the durable backend currently supervises every active Jobs row. At larger hub ×
  active-job counts this amplifies Jobs API/object-store polling; sharded ownership/backoff is remaining
  control-plane operations work.
- Job/result artifacts are retained until operator lifecycle rules remove them. No foreground recursive
  cleanup scans a shared bucket.
- Dynamic `working_dir` and per-run pip upload are intentionally unsupported; code is image-baked and
  identified by `DP_RAY_JOBS_CODE_REF`.
