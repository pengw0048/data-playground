# Durable Ray Jobs execution

`dp_ray` has two driver modes with the same IR and result semantics:

- **Development default:** when `DP_RAY_JOBS_ADDRESS` is unset, the hub starts `_driver.py` with local
  `Popen`. This preserves the existing zero-configuration, single-host workflow.
- **Durable remote mode:** when `DP_RAY_JOBS_ADDRESS` is set, the hub uses Ray's official
  `JobSubmissionClient` for submit, status, logs, and stop. The Ray cluster, SQL metadata DB, and object
  store become the durable control/data planes; the submitting Python process is disposable.

The lifecycle follows Ray's [Python SDK for job submission](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/sdk.html)
and its documented [job status API](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/doc/ray.job_submission.JobSubmissionClient.get_job_status.html).

## Required production contract

Configure the submitting hub/kernel with:

```bash
DP_RAY_JOBS_ADDRESS=https://ray-dashboard.example:8265
# Stable, non-secret identity for this cluster. Keep it unchanged if the endpoint/DNS name moves.
DP_RAY_JOBS_CLUSTER_REF=production-ray-east

# This command must already exist in the Ray head image. No source tree is uploaded at run time.
DP_RAY_JOBS_ENTRYPOINT='/app/kernel/.venv/bin/python /app/examples/plugins/dp_ray/_driver.py'
# Use an immutable image digest or release identifier. It participates in the attempt hash.
DP_RAY_JOBS_CODE_REF='sha256:0123456789abcdef...'
DP_RAY_JOBS_WORKSPACE=/app/kernel
DP_RAY_JOBS_DATA_DIR=/app/kernel/data

# Hub, Ray driver, and every worker must reach the same object store.
DP_STORAGE_URL=s3://data-playground/outputs
# Optional; defaults to $DP_STORAGE_URL/__ray_jobs__.
DP_RAY_JOBS_ARTIFACT_PREFIX=s3://data-playground/control/ray-jobs
```

The Ray image must contain the `hub` package, `dp_ray`, and the same dependency versions used by its
workers. `docker/ray/Dockerfile` is the reference image shape; production should publish it by immutable
digest and use that digest as `DP_RAY_JOBS_CODE_REF`.

Ray Jobs mode fails before submission when any code-path or cluster identity setting is absent, when control artifacts are
not on `s3://`, `r2://`, `gs://`, or `gcs://`, or when a graph contains host-local inputs/outputs. Remote
file-adapter append is also rejected: Ray remembers submission IDs, but a total cluster-state loss could
require replaying a durable attempt, and replaying append would duplicate rows. Use overwrite or a
transactional/table-format sink for retriable production writes.

Object-store credentials are passed through the workload allowlist (`AWS_*`, `DP_S3_*`, or Google ADC).
`DP_DATABASE_URL`, auth signing material, provider API keys, and other hub control-plane secrets are not
sent to Ray. Configure Ray Jobs API authentication with Ray's supported client environment/configuration;
do not put API tokens in the shared job artifact.

## Durable lifecycle

1. The hub resolves physical sink URIs and inlines control-plane schema references.
2. It canonicalizes the graph, target, resources, resolved sinks, remote paths, and immutable code ref.
   The SHA-256 digest is the attempt ID; the Ray submission ID is derived deterministically from the
   logical run ID plus that digest.
3. An immutable `job.dpjob` is written to shared object storage. A `run_backend_jobs` row atomically binds
   the logical run to that one attempt, while `RunStatus.backendRef` makes the handle visible and durable.
4. The official Jobs client checks status/list before submit. If another submitter already created the
   deterministic ID—or accepted the request while its response was lost—the existing Job is polled.
5. The image-baked driver reads `job.dpjob`, executes the IR, and writes one terminal `result.dpresult`
   envelope containing the contract version, attempt ID, and submission ID.
6. On success/failure/STOPPED, supervisors compete for a renewable SQL publication lease. One winner
   commits the canonical terminal RunStatus; other supervisors load it. Catalog projection carries a
   deterministic idempotency key per attempt/write step. The built-in catalog honors it; external catalog
   providers used with Ray Jobs must implement `register_output_idempotent(idempotency_key, ...)` durably.

On hub/kernel restart, boot-time orphan reaping preserves runs with a valid backend binding. When the
plugin loads, it reconstructs queued/running runs from SQL, reads the shared job artifact, and reattaches
to the deterministic Ray submission. No old PID, local temp directory, or in-memory Python dictionary is
part of the recovery contract.

## Cancellation and failure behavior

`cancel()` asks the supervisor to call `stop_job`, then waits up to
`DP_RAY_JOBS_CANCEL_TIMEOUT_S` (default 30 seconds) for `STOPPED`, `SUCCEEDED`, or `FAILED`. It never labels
a still-running remote job cancelled. If the timeout expires, the run remains non-terminal with an
actionable error and polling continues. A successful race is published as success; `STOPPED` becomes
cancelled. Cancellation cannot roll back a sink the driver committed before it received the stop signal;
`cancelled` therefore means execution stopped, not that no physical side effect occurred. This is why the
mode accepts only overwrite/replay-safe sinks. Failed-job messages come from the Jobs API; driver logs are
fetched separately and known credential values are redacted rather than copied into shared RunStatus.

Relevant tuning knobs:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `DP_RAY_JOBS_POLL_S` | `1` | Job status poll interval |
| `DP_RAY_JOBS_CANCEL_TIMEOUT_S` | `30` | Wait for remote stop acknowledgement |
| `DP_RAY_JOBS_RESULT_TIMEOUT_S` | `30` | Wait for the terminal result object after `SUCCEEDED` |
| `DP_RAY_JOBS_PUBLICATION_LEASE_S` | `60` | Renewable single-publisher lease |

## Tradeoffs and non-goals

- The SQL binding and publication fence require the same shared metadata DB used by `run_states`.
  PostgreSQL is the production choice; SQLite remains appropriate for one local hub.
- Job/result control artifacts are small but durable. Configure a bucket lifecycle policy consistent with
  run-state/history retention; this change does not delete Ray's own job history or object-store artifacts.
- The publication fence makes catalog/history side effects single-winner and retryable. It does not turn
  several independent write nodes into one cross-dataset transaction. Crash recovery is at-least-once at
  the provider call boundary, so an external provider must enforce the supplied idempotency key.
- Durable Jobs currently covers whole-graph `RayRunner.run`. It deliberately does not advertise the
  in-memory multi-region `RunController` placement path: making only a child region durable would lose the
  parent orchestration on restart and would be misleading.
- A cancel request is not acknowledged until Ray reports a terminal status. The request intent itself is
  currently process-local; if that hub dies before acknowledgement, the run remains non-terminal and the
  caller/operator must retry cancel after reattachment. Persisting `cancel_requested` is follow-up work.
- Ray Jobs mode intentionally uses image-baked code. Dynamic per-run `working_dir` or pip uploads are not
  implemented because they weaken reproducibility, startup latency, and supply-chain control.
- Live cluster validation remains opt-in. Cluster-free tests use a deterministic fake
  `JobSubmissionClient` to cover submit ambiguity, duplicate recognition, stop acknowledgement/timeouts,
  failed logs, restart reattachment, and one-time publication.
