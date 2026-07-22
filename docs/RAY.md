# Ray backend: support and production readiness

`dp_ray` is an opt-in distributed execution backend for Data Playground. Its supported data path fails
closed: large Parquet inputs and simple Parquet overwrite outputs stay off the driver, and every
remaining compatibility collect has an explicit byte ceiling.

These claims use the trusted-workspace boundary in [Supported deployments and trust model](SUPPORT.md).
Workers, runtime images, execution plugins, and cluster administrators are trusted with the workspace;
the backend does not claim mutually hostile tenant isolation.

The Compose and KubeRay files in this repository are validation harnesses, not deployment manifests.
The reference evidence below separates the backend contract from deployment responsibilities.

## Choose an execution path

- **Use the per-canvas kernel (the default)** for ordinary local or trusted-team work, exact-revision
  input runs, and any graph that does not fit the Ray shapes below. It needs no Ray plugin or cluster;
  it is the product default and retains the core managed-write and durable-task behavior.
- **Use Ray Data (`ray-data`)** when the graph has a documented distributable shape and the data can be
  read and written under the [Ray Data contract](#reference-ray-data-contract). This is an explicit
  backend selection, not an automatic scale-out path. It is appropriate for the supported Parquet,
  map/filter, constrained aggregate/window/dedup, broadcast-join, and plain-key-sort cases; unsupported
  or semantically uncertain shapes fall back or fail before Ray dispatch as described below.
- **Use durable Ray Jobs** only when a supported *whole graph* must continue through a hub restart. It
  needs the Ray Data plugin plus SQL, shared object storage, an operator-protected Jobs endpoint, and
  an immutable image/cluster assertion. The current durable sink is one built-in, non-partitioned
  Parquet overwrite; it does not make placed multi-region orchestration durable. See
  [Durable Ray Jobs execution](RAY_JOBS.md).

Neither Ray path transports the hub's admitted exact-revision input manifest. Such a run is rejected
before Ray allocation or Jobs submission; choose the default kernel instead. All three paths operate
inside the [trusted-workspace boundary](SUPPORT.md), not a hostile-tenant isolation model.

## Reference: Ray Data contract

### Execution contract

Install the Ray extra, load [`examples/plugins/dp_ray`](../examples/plugins/dp_ray/), and select
`ray-data` in Settings or with `DP_EXECUTION=ray-data`. A placed region is claimed only when its
resolved requirements contain `labels.engine=ray`. Set `DP_RAY_REMOTE=1` when Ray workers do not share
the kernel's filesystem; remote placement then requires a configured object-storage tier.

Ray does not yet transport the hub's admitted exact-revision input manifest. Until
[#302](https://github.com/pengw0048/data-playground/issues/302) is implemented, a run carrying that
manifest cannot use Ray as either the selected whole-graph backend or a placed region. Core rejects the
run before controller/run identity, worker, artifact, driver, or remote-job allocation. The built-in
in-process, Kernel, and same-host Subprocess transports continue to support exact-revision runs.

With no Ray Jobs address, the local `Popen` driver uses the same workload process-scope contract as
ordinary isolated runs. On POSIX, the direct child owns a new process group; cancellation and
`DP_RUN_DEADLINE_S` send TERM followed by a bounded KILL, and terminal status waits for the group fence
and direct-child reap. On non-POSIX platforms, both paths can stop only the direct child and therefore
cannot guarantee that descendants stop. This limitation does not change the durable Ray Jobs contract.

The hub cannot inspect cluster resources without connecting a driver, so operators declare admission
capacity with `DP_RAY_NUM_CPUS`, `DP_RAY_MEM`, `DP_RAY_GPUS`, `DP_RAY_GPU_TYPE`, and optional labels
such as `DP_RAY_LABELS=pool=a100,zone=use1`. Each non-engine label value must also exist as a Ray
custom resource on the matching node (for example `--resources='{"a100": 1}'`). An explicit Ray
placement that does not match this advertised capacity fails before dispatch instead of becoming an
unschedulable task. `gpuType` is a hard placement pin: it is canonicalized to Ray's exact
`accelerator_type` resource, not reduced to a generic `num_gpus` request. One execution region cannot
combine different GPU types.

GPU clean transforms and broadcast joins use a finite row batch. `DP_RAY_GPU_BATCH_ROWS` defaults to
`4096`; it must be positive and is capped at `65536`. The parsed value is frozen into a durable Jobs
attempt, including the default. GPU-pinned aggregate, window, and dedup fail before dispatch because
their correctness requires one complete hash partition (`batch_size=None`); splitting it to satisfy GPU
batching would change results. GPU/custom-pinned sort remains unsupported by Ray 2.56's public API.

The backend falls back to the local DuckDB runner when it cannot prove that a graph is safe to
distribute.

Supported operations today:

- `map`, `filter`, `flat_map`, `map_batches` — distributed; uses the same compiled transform operator
  as the local engine
- grouped `aggregate` — yes for bare-column group keys; global, expression-key, order-sensitive, and
  GPU-pinned aggregates fail or fall back as appropriate
- `window` — yes for a bare-column partition key; order-sensitive forms without sufficient ordering
  fall back, and GPU pins fail loud
- full-row `dedup` — yes; keyed dedup and schemas with floating-point values fall back, and GPU pins
  fail loud
- `join` — broadcast `inner`, `left`, and `cross`; the materialized right side must fit the configured
  driver fallback limit, and GPU maps use finite batches
- `sort` — plain-column keys; the final ordered result is coalesced to one worker. Ray 2.56 cannot
  resource-pin its range shuffle, so GPU/custom-resource sorts fail before dispatch
- SQL, sections, metrics/charts, opaque plugin nodes — not distributed; local fallback

### Data movement

Local or shared Parquet (file or parts directory): same-host Ray workers read it directly. A remote
cluster uses only the bounded driver stream or falls back before dispatch.

Object-store Parquet (file or shard prefix): only the exact built-in `DuckDBAdapter` may use
`ray.data.read_parquet`. It receives a credentials-aware filesystem, exact fragment list, full
adapter-oracle schema, selected columns, and typed dataset-rooted Hive partitioning.

Built-in CSV/JSON/local IPC, or a Parquet layout whose native proof fails: compatibility scan only
when stored and decoded sizes are known below the driver fallback limit.

Object IPC, Lance, Iceberg, Hugging Face, or a plugin adapter: no implicit driver scan. Local fallback
(or fail-loud for an explicit Ray pin) until the adapter exposes a dedicated bounded/distributed
capability.

Placed-region output: workers write an immutable directory/prefix of Parquet shards;
`_DP_SUCCESS.json` is written last.

Whole-graph non-write target: Ray does not yet own a durable result-publication lifecycle. Automatic
placement delegates this shape to the local backend; an explicit Ray/GPU/custom-resource pin or Ray
Jobs configuration fails before allocation or submission. Add a write sink to publish through Ray, or
run the selected node locally to receive a committed non-catalog `RunOutput`.

Whole-graph unpartitioned Parquet overwrite: workers write an immutable attempt prefix; the
returned/catalog URI is that completed prefix, not the stable logical filename.

Append, partitioned overwrite, CSV, JSON, Arrow, Lance, or plugin sink: normal adapter/sink semantics
through the bounded compatibility path.

`DP_RAY_DRIVER_FALLBACK_MAX_BYTES` is an integer byte count with a default of `67108864` (64 MiB).
Built-in compatibility sources are checked against stored physical bytes before `adapter.scan`, then
decoded in small record batches whose cumulative Arrow bytes are checked and transferred to Ray one
batch at a time; the driver never concatenates the source into one Arrow table. Ray Dataset sinks and
broadcast sides are materialized (and may spill) and checked with `Dataset.size_bytes()` before
block/reference collection. An unknown size or a value above the limit fails with guidance; it never
means “collect anyway.”

Native Parquet discovery processes at most 10,000 files and reads each physical footer before dispatch.
`pyarrow.unify_schemas(..., promote_options="permissive")` preserves compatible drift such as `int32`
to `int64`; incompatible drift takes the bounded built-in path or falls back. The exact
`DuckDBAdapter` metadata schema is the semantic oracle for physical and partition column order/types.
Ray 2.56 native Hive is limited to proven `int64` and string partition fields with consistent, unique
keys. The Hive directory-key order must also match the exact adapter metadata order; otherwise Ray
2.56 reorders the materialized columns and the source takes the bounded/local path. The Hive
default-partition sentinel, DATE/other partition types, duplicate keys, and inconsistent layouts also
take the bounded/local path. A flat dataset below an ancestor such as `tenant=acme` remains native
without leaking that ancestor. A genuinely Hive-partitioned dataset below a Hive-looking root/ancestor
falls back because DuckDB parses the ancestor while exact-root Ray intentionally does not.

Compact prefixes before the 10,000-file ceiling. The ceiling bounds retained metadata and footer work,
but PyArrow's object-store listing API may materialize the provider's prefix response before the count
is known; data bytes remain worker-direct, while very large metadata listings still require compaction
or a future catalog-backed fragment manifest.

Increasing this limit trades compatibility for driver-memory risk. Prefer Parquet on shared/object
storage or the local backend instead. A remote Ray cluster must use an object-store destination for
worker-direct Parquet output; a local destination makes the graph fall back before Ray starts.

### Managed object publication and deletion

Each Ray run currently supports at most one write sink, regardless of sink type. A graph with two or
more write sinks fails before any attempt is allocated or writer is dispatched; atomic batch
publication is required before that limit can be lifted. Managed object writes require the core catalog
authority that can atomically swap the logical pointer, ownership reference, and attempt state.
Unmanaged writes require a catalog with durable registration and exact read-back attestation.

Core provides a built-in lifecycle provider for S3 and compatible endpoints that implement its complete
API contract. [R2's S3 compatibility API](https://developers.cloudflare.com/r2/api/s3/api/) currently
omits the bucket-versioning/version-list operations this provider uses, so `r2://`, GCS, or another
scheme must register a `ManagedObjectProvider` as documented in [PLUGINS.md](PLUGINS.md). A plain
PyArrow filesystem is read-capable but intentionally fails managed writes because it cannot prove
hidden versions, delete markers, incomplete multipart uploads, or conditional namespace ownership.

The S3 identity needs, in addition to ordinary read/write permissions, permission to read bucket
versioning, list object versions and multipart uploads, get/conditionally put the
`_dp_control/namespaces/<namespace>.json` marker, delete exact object versions and delete markers, and
abort multipart uploads. In AWS IAM terms this includes `s3:GetBucketVersioning`,
`s3:ListBucketVersions`, `s3:ListBucketMultipartUploads`, `s3:GetObject`, `s3:PutObject`,
`s3:DeleteObject`, `s3:DeleteObjectVersion`, and `s3:AbortMultipartUpload`; the endpoint must preserve
`If-Match` and `If-None-Match` semantics on the marker write.

The metadata database owns a stable storage namespace. Before managed allocation/commit validation or
GC, core verifies that namespace against the provider-side conditional marker. An offline metadata
clone must call `isolate_cloned_object_storage(expected, replacement)` before provider access. This
destructive clone isolation rotates both owner and namespace, quarantines inherited attempts, removes
inherited refs/cache/catalog visibility, and clears copied marker claims; it does not touch the
original installation's marker. The isolated clone cannot read or delete the original namespace. An
audited disaster-recovery takeover of an old namespace is a separate capability and is not
implemented; changing `DP_STORAGE_NAMESPACE` alone is rejected.

Superseded/abandoned attempts become eligible only after ownership refs and leases are gone and the
configured retention/grace has elapsed. The reaper inventories only that exact generation, persists
stable member identities, deletes versions/delete markers/uploads exactly, and then requires two
database-clock-separated empty observations. A late shard, version, marker, or upload during deletion
quarantines the attempt instead of declaring it deleted. `writing` attempts are never reaped from age
or `RunState`; they still require an authoritative writer-stop transition.

Append, partitioning, destination selection, and non-Parquet formats retain the shared sink contract;
they are not silently converted into overwrite Parquet. Empty Parquet results publish one typed empty
shard plus the manifest so their schema remains readable. Filters, relational operators, and full-row
deduplication preserve or derive empty-result schemas. Schema-changing Python transforms must declare
`outputSchema`; without that contract, an empty result fails instead of publishing a misleading schema.
These transforms materialize once inside Ray so non-empty downstream operators use the actual runtime
schema rather than a stale declaration; this adds a stage boundary but does not collect data to the
driver. Transforms with `enforceSchema=true` fall back to the local engine; an explicit Ray pin fails
before dispatch until distributed schema enforcement is implemented.

## Reference: Ray Data validation and release evidence

For a version release, [`release.yml`](../.github/workflows/release.yml) invokes this Ray differential
and the separate [Ray Jobs acceptance](RAY_JOBS.md#repository-evidence-real-service-acceptance) against
the exact candidate SHA. The Ray differential certifies only the backend contract exercised below: the
pinned image/version handshake, selected multi-node operators, worker-direct object-store output, and
the stated fault/degraded-rerun controls. It does **not** certify a particular operator cluster,
network policy, autoscaler, storage account, KubeRay installation, capacity plan, or incident runbook.
Those remain operator responsibilities under [Supported deployments and trust model](SUPPORT.md).

This real-cluster matrix is a path-gated pull-request check, not an unconditional PR or post-merge
check. Required PR unit and contract tests provide fast feedback for every change; the complete
differential additionally runs when its owned execution contract changes, on schedule, on demand, and
before publishing a release. See [CI and release gates](CI.md).

The automated Compose gate starts a Ray head, two worker containers, a separate driver node, and MinIO.
Before that CPU-only topology starts, a logical-resource Ray check (no NVIDIA runtime) proves typed
accelerator affinity, a wrong-GPU task remaining pending, finite GPU `map_batches`, and Ray 2.56's typed
read/write remote options. The distributed gate then requires:

1. a real hash-shuffle to span at least two Ray node IDs
2. native MinIO Parquet reads feeding distributed GROUP BY and broadcast join results to match DuckDB
   in Arrow schema and row values
3. native Parquet reads to unify compatible physical footer drift, exclude a flat-root Hive-looking
   ancestor, and preserve typed numeric/string Hive columns through a real aggregate and broadcast join
4. a whole-graph Parquet overwrite to publish an immutable, manifested, worker-written prefix and
   register that actual URI
5. the schema, aggregate-row, and join-row fault controls to each fail
6. a fresh run to pass after one worker is stopped between runs

The last check proves that the remaining cluster accepts a new degraded-cluster run. It does not kill a
worker during an active job and does not prove in-flight task reconstruction. KubeRay validation is
manual and uses the same differential; see [`deploy/kuberay/README.md`](../deploy/kuberay/README.md).

Run the Compose gate locally:

```bash
docker compose -f docker-compose.ray.yml build ray-head
docker compose -f docker-compose.ray.yml up -d --no-build --scale ray-worker=2 \
  ray-head ray-worker minio createbucket
docker compose -f docker-compose.ray.yml run --rm --no-deps driver

for fault in schema rows join; do
  if DP_MULTINODE_FAULT="$fault" docker compose -f docker-compose.ray.yml run --rm --no-deps driver; then
    echo "ERROR: $fault control unexpectedly passed"
    exit 1
  fi
done

worker="$(docker compose -f docker-compose.ray.yml ps -q ray-worker | tail -1)"
docker kill "$worker"
docker compose -f docker-compose.ray.yml run --rm --no-deps driver  # fresh degraded-cluster run
docker compose -f docker-compose.ray.yml down -v
```

Build the shared image before creating any cluster container, and keep `--no-deps` on every ephemeral
driver run. This prevents a differential from recreating the head service and replacing the GCS
cluster identity underneath the persistent workers.

## Reference: readiness matrix

Current status versus what production ownership still needs:

- Selected-operator semantic parity — partial; extend multi-node differentials to every claimed
  operator and edge type
- Object-store Parquet scale-out reads — implemented; operate with least-privilege credentials and
  validate representative production datasets
- Bounded adapter compatibility — implemented; tune or disable the limit from measured
  workload/driver memory; native connectors are preferable
- Whole-graph Parquet overwrite data path — implemented; use shared object storage for remote
  clusters and tune the built-in deletion grace to the workload
- Durable whole-graph Jobs lifecycle — implemented for the documented v4 contract: persisted
  attempt/submission/control routing, restart reconciliation, durable acknowledged cancel, exact
  artifact fencing, and atomic terminal SQL publication; this does not extend to the multi-region parent
- Atomic region publication — implemented; distributed and local-fallback handoffs use immutable
  per-attempt prefixes; the controller validates the success manifest before cache publication
- Trusted-workspace process hygiene — implemented in core: one-shot drivers use an explicit environment
  allowlist and a private metadata DB, without hub auth or metadata credentials; cluster/IAM enforcement
  remains operator-owned
- Cluster health and placement truth — partial; target-cone requirements, static advertised-capacity
  admission, GPU/custom task options, and fail-loud unsupported shapes are implemented; live
  discovery/backpressure remain open
- Runtime compatibility — partial; one supported Ray range and a driver/worker/core/plugin version
  handshake
- Resilience — partial; active-job worker/head/driver failure tests, retry policy, and orphan cleanup
- Observability — partial; durable job IDs, control-observation liveness, shared status, and visible
  recovery-blocked diagnoses exist; queue/retry/spill/storage metrics, authenticated log integration,
  traces, and alerts remain open
- Deployment security and HA — operator-owned; immutable images, secrets/IAM, TLS/network policy, pod
  security, quotas, autoscaling, and HA storage

## Reference: production ownership gates

The correctness fences above are implemented and tested, but production ownership is specific to a
deployment and workload shape. Within the supported trusted-team profile, separate remaining backend
work from checks that only an operator can perform.

### Remaining backend work

1. Make the multi-region parent orchestration durable before using placed multi-region graphs where hub
   restart survival is required. Whole-graph Jobs already has the narrower durable lifecycle.
2. Exercise head, worker, and driver loss during active jobs with bounded retry, recovery, and orphan
   assertions on the real-service gate.
3. Discover live cluster capacity and health, enforce admission/backpressure, and reject an explicit
   placement when current—not only statically advertised—resources cannot honor it.
4. Expand semantic differentials over every claimed operator/edge shape and verify the supported
   Ray/core/plugin version handshake across submitter and workers.
5. Add a protected operator log/metrics path, automatic whole-graph execution deadlines, bounded
   object-store calls, and alerts for failed manifests, GC errors, spill pressure, queue delay, retries,
   and resource saturation.
6. Shard durable supervision or add ownership/backoff before large hub fleets: every current hub polls
   every active Jobs row, so request volume grows with hubs × active jobs.

### Operator acceptance for a concrete deployment

1. Protect the Ray Jobs endpoint; attest immutable runtime images and the stable cluster identity bound
   by `DP_RAY_JOBS_CODE_REF` and `DP_RAY_JOBS_CLUSTER_REF`.
2. Configure TLS/network policy, data-plane IAM, secret rotation, finite database and object-store
   timeouts, quotas, and an infrastructure deadline or cancellation policy for runaway Jobs.
3. Configure storage lifecycle rules for incomplete/versioned objects and control artifacts, then prove
   backup/restore and orphan cleanup against the deployment's recovery window.
4. Validate representative large workloads, capacity and saturation behavior, SLOs/alerts, HA,
   upgrade/rollback, failure recovery, and incident runbooks on the intended topology.

Attempt- or dataset-scoped identities, per-user cluster namespaces, and adversarial workload isolation
can be useful defense in depth. They become required if the project ever expands to mutually hostile
tenants, which [is not a supported profile](SUPPORT.md#deployment-profiles); they are not an implicit
prerequisite for a trusted-team deployment.

Object storage holds immutable shards plus `_dp_commits/<attempt>/` manifests. Ownership and lifecycle
state live in the shared metadata database's indexed attempt, lease, ref, and exact-member inventory
tables. The parent registers physical attempt URIs before dispatch. Region attempts publish in the same
transaction as their result-cache pointer; whole-graph overwrites atomically advance a monotonic
logical catalog pointer, release the provisional publication lease, and supersede only the prior
published generation. A committed attempt whose publisher crashes remains fenced by its durable
publication lease and becomes abandoned only after that lease expires and retention eligibility is
re-evaluated. GC never lists a shared parent prefix or chooses a winner from object mtimes/client
clocks.

Failed/cancelled object attempts are deleted only after durable backend reconciliation proves every
writer stopped; local driver exit alone is insufficient because remote Ray tasks can outlive it. The
periodic reaper never infers that a `writing` attempt is dead from `RunState`, age, or a local
deadline: an independent driver or durable Ray Job can survive a hub crash. Unreconciled attempts
therefore require backend terminal/stop acknowledgement or a provider lifecycle rule; time alone is
not a safe write fence. `DP_ATTEMPT_RETENTION_SECONDS` does not authorize deleting an unacknowledged
writer.

The immutable generation remains for `DP_ATTEMPT_DELETE_GRACE_SECONDS` (one day by default) after it
loses visibility so readers that already resolved and leased the old URI can finish. The automatic
grace cannot be configured below the run deadline; raise it for longer external readers. The built-in
S3 provider deletes versioned history and incomplete uploads by exact identity rather than issuing an
unversioned delete. A provider lifecycle rule remains useful as a last-resort bound for crash-orphaned
writers that never receive terminal proof, but it is not treated as lifecycle acknowledgement by core.

Local region handoffs are retained for the same correctness reason. The hub does not evict files by age
or directory count: an mtime cannot prove that a cache entry, catalog version, concurrent hub, or
active reader no longer references an artifact. Monitor local region capacity until an ownership-aware
artifact ledger with exact-key cleanup is implemented.

`run_unit` mints a random attempt ID by default and enforces one owner per ID inside a runner. A caller
that supplies deterministic IDs must fence ownership in its durable control plane. A committed retry
reattaches; an existing partial/mismatched prefix or a manifest owned by another raw attempt ID fails
closed and is never overwritten. Attempt paths keep a readable run slug (capped at 64 characters) plus
a 128-bit SHA-256 suffix over the complete raw attempt ID and unmodified logical URI. Whole-graph
sinks also include the write-step ID in that digest, so fan-out writes and `.parquet`/`.pq` targets
cannot collide after extension stripping. The manifest keeps the overall raw attempt ID for restart
reattachment and auditability.

Repository changes can make the backend production-capable and provide repeatable validation. A
specific deployment is production-ready only after its IAM, network, storage, KubeRay, capacity, and
operational gates also pass.
