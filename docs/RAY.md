# Ray backend: support and production readiness

`dp_ray` is a working distributed execution backend for Data Playground. Its supported data path is
fail-closed: large Parquet inputs and simple Parquet overwrite outputs stay off the driver, while every
remaining compatibility collect has an explicit byte ceiling. The multi-node differential in
[`ray-validation.yml`](../.github/workflows/ray-validation.yml) verifies that contract on Ray and MinIO.

The Compose and KubeRay files in this repository are validation harnesses, not deployment manifests. A
green differential does not certify an operator's IAM, capacity, KubeRay configuration, or incident
procedures. The final section separates backend guarantees from deployment responsibilities.

## Current execution contract

Install the Ray extra, load [`examples/plugins/dp_ray`](../examples/plugins/dp_ray/), and select
`ray-data` in Settings or with `DP_EXECUTION=ray-data`. A placed region is claimed only when its resolved
requirements contain `labels.engine=ray`. Set `DP_RAY_REMOTE=1` when Ray workers do not share the
kernel's filesystem; remote placement then requires a configured object-storage tier.

The hub cannot inspect cluster resources without connecting a driver, so operators declare admission
capacity with `DP_RAY_NUM_CPUS`, `DP_RAY_MEM`, `DP_RAY_GPUS`, `DP_RAY_GPU_TYPE`, and optional labels such
as `DP_RAY_LABELS=pool=a100,zone=use1`. Each non-engine label value must also exist as a Ray custom
resource on the matching node (for example `--resources='{"a100": 1}'`). An explicit Ray placement that
does not match this advertised capacity fails before dispatch instead of becoming an unschedulable task.

The backend conservatively falls back to the local DuckDB runner when it cannot prove that a graph is
safe to distribute.

| operation | distributed today | important boundary |
|---|---|---|
| `map`, `filter`, `flat_map`, `map_batches` | yes | uses the same compiled transform operator as the local engine |
| grouped `aggregate` | yes, for bare-column group keys | global, expression-key, and order-sensitive aggregates fall back |
| `window` | yes, for a bare-column partition key | order-sensitive forms without sufficient ordering fall back |
| full-row `dedup` | yes | keyed dedup and schemas containing floating-point values fall back |
| `join` | broadcast `inner`, `left`, and `cross` | the materialized right side must fit the configured driver fallback limit |
| `sort` | plain-column keys | the final ordered result is coalesced to one worker; Ray 2.56 cannot resource-pin its range shuffle, so GPU/custom-resource sorts fail before dispatch |
| SQL, sections, metrics/charts, opaque plugin nodes | no | local fallback |

### Data movement

| path | current behavior |
|---|---|
| local/shared Parquet file or parts directory | same-host Ray workers read it directly; a remote cluster uses only the bounded driver stream or falls back before dispatch |
| object-store Parquet file or shard prefix | only the exact built-in `DuckDBAdapter` may use `ray.data.read_parquet`; it receives a credentials-aware filesystem, exact fragment list, full adapter-oracle schema, selected columns, and typed dataset-rooted Hive partitioning |
| built-in CSV/JSON/local IPC or a Parquet layout whose native proof fails | compatibility scan only when stored and decoded sizes are known below the driver fallback limit |
| object IPC, Lance, Iceberg, Hugging Face, or a plugin adapter | no implicit driver scan; local fallback (or fail-loud for an explicit Ray pin) until the adapter exposes a dedicated bounded/distributed capability |
| placed-region output | workers write an immutable directory/prefix of Parquet shards; `_DP_SUCCESS.json` is written last |
| whole-graph unpartitioned Parquet overwrite | workers write an immutable attempt prefix; the returned/catalog URI is that completed prefix, not the stable logical filename |
| append, partitioned overwrite, CSV, JSON, Arrow, Lance, or plugin sink | normal adapter/sink semantics through the bounded compatibility path |

`DP_RAY_DRIVER_FALLBACK_MAX_BYTES` is an integer byte count with a default of `67108864` (64 MiB).
Built-in compatibility sources are checked against stored physical bytes before `adapter.scan`, then
decoded in small record batches whose cumulative Arrow bytes are checked and transferred to Ray one
batch at a time; the driver never concatenates the source into one Arrow table. Ray Dataset sinks
and broadcast sides are materialized (and may spill) and checked with `Dataset.size_bytes()` before
block/reference collection. An unknown size or a value above the limit fails with guidance; it never
means "collect anyway."

Native Parquet discovery processes at most 10,000 files and reads each physical footer before dispatch.
`pyarrow.unify_schemas(..., promote_options="permissive")` preserves compatible drift such as `int32` to
`int64`; incompatible drift takes the bounded built-in path or falls back. The exact `DuckDBAdapter`
metadata schema is the semantic oracle for physical and partition column order/types. Ray 2.56 native
Hive is limited to proven `int64` and string partition fields with consistent, unique keys. The Hive
directory-key order must also match the exact adapter metadata order; otherwise Ray 2.56 reorders the
materialized columns and the source takes the bounded/local path. The Hive default-partition sentinel,
DATE/other partition types, duplicate keys, and inconsistent layouts also take the bounded/local path. A
flat dataset below an ancestor such as `tenant=acme` remains native without leaking that ancestor. A
genuinely Hive-partitioned dataset below a Hive-looking root/ancestor falls back because DuckDB parses the
ancestor while exact-root Ray intentionally does not.
Compact prefixes before the 10,000-file ceiling. The ceiling bounds retained metadata and footer work,
but PyArrow's object-store listing API may materialize the provider's prefix response before the count is
known; data bytes remain worker-direct, while very large metadata listings still require compaction or a
future catalog-backed fragment manifest.

Increasing this limit trades compatibility for driver-memory risk. Prefer Parquet on shared/object
storage or the local backend instead. A remote Ray cluster must use an object-store destination for
worker-direct Parquet output; a local destination makes the graph fall back before Ray starts.

Append, partitioning, destination selection, and non-Parquet formats retain the shared sink contract;
they are not silently converted into overwrite Parquet. Empty Parquet results publish one typed empty
shard plus the manifest so their schema remains readable. Filters, relational operators, and full-row
deduplication preserve or derive empty-result schemas. Schema-changing Python transforms must declare
`outputSchema`; without that contract, an empty result fails instead of publishing a misleading schema.

## What the validation gate proves

The automated Compose gate starts a Ray head, two worker containers, a separate driver node, and MinIO.
It requires:

1. a real hash-shuffle to span at least two Ray node IDs;
2. native MinIO Parquet reads feeding distributed GROUP BY and broadcast join results to match DuckDB in
   Arrow schema and row values;
3. native Parquet reads to unify compatible physical footer drift, exclude a flat-root Hive-looking
   ancestor, and preserve typed numeric/string Hive columns through a real aggregate and broadcast join;
4. a whole-graph Parquet overwrite to publish an immutable, manifested, worker-written prefix and
   register that actual URI;
5. the schema, aggregate-row, and join-row fault controls to each fail;
6. a fresh run to pass after one worker is stopped between runs.

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
driver run. This prevents a differential from recreating the head service and replacing the GCS cluster
identity underneath the persistent workers.

## Production-readiness matrix

| gate | status | production-capable requirement |
|---|---|---|
| selected-operator semantic parity | partial | extend multi-node differentials to every claimed operator and edge type |
| object-store Parquet scale-out reads | implemented | operate with least-privilege credentials and validate representative production datasets |
| bounded adapter compatibility | implemented | tune or disable the limit from measured workload/driver memory; native connectors are preferable |
| whole-graph Parquet overwrite data path | implemented | use shared object storage for remote clusters; garbage-collect superseded attempt prefixes |
| durable job lifecycle | missing | persisted Ray submission/attempt ID, restart reconciliation, acknowledged cancel, timeout, and fencing |
| atomic region publication | implemented | distributed and local-fallback handoffs use immutable per-attempt prefixes; the controller validates the success manifest before cache publication |
| workload isolation | partial | environment/control-plane separation exists; replace broad data credentials with attempt-scoped identity and enforce cluster policy |
| cluster health and placement truth | missing | live resource/health discovery, backpressure, and fail-loud behavior for an explicit Ray pin |
| runtime compatibility | partial | one supported Ray range and a driver/worker/core/plugin version handshake |
| resilience | partial | active-job worker/head/driver failure tests, retry policy, and orphan cleanup |
| observability | partial | durable job IDs, queue/retry/spill/storage metrics, retained structured logs, traces, and alerts |
| deployment security and HA | operator-owned | immutable images, secrets/IAM, TLS/network policy, pod security, quotas, autoscaling, and HA storage |

## What remains before production ownership

The data-plane paths above are production-safe within their stated contract. The backend as a whole
still needs the following before it should own production workloads:

1. Replace local subprocess ownership with a durable Ray job lifecycle that survives kernel/hub restarts
   and supports idempotent submission, cancellation, timeout, retry, and attempt fencing.
2. Replace broad data-plane credentials with attempt/dataset-scoped identities and enforce per-run
   namespace, network, pod-security, and quota boundaries on the target cluster.
3. Discover live cluster capacity and health, enforce admission/backpressure, and reject an explicit Ray
   placement when its requirements cannot be honored.
4. Pin and verify the supported Ray/runtime image contract across the submitting process and every worker.
5. Add lifecycle management for abandoned/superseded immutable attempt prefixes and alerts for failed
   manifests, object-store errors, spill pressure, queue delay, retries, and resource saturation.
6. Pass staging gates on the intended production topology: active-job failure injection, representative
   large workloads, SLOs/alerts, upgrade/rollback, and recovery runbooks.

Object-store data attempts live under `<DP_STORAGE_URL>/regions/`; their authoritative commit records live
under the sibling `regions/_dp_commits/` subprefix. The manifest contains the exact shard path and size
inventory, and every cache lookup verifies it. Configure two native lifecycle rules: expire
`regions/_dp_commits/` first, then expire all `regions/` data after an additional grace period longer than
the maximum run duration plus the storage provider's lifecycle-evaluation skew. Once the commit disappears,
new readers recompute; already-resolved readers retain every shard throughout the grace window. Failed
attempts have no commit and leave with the later data rule. The hub deliberately does not recursively scan
and delete a shared bucket in the foreground: it cannot prove ownership across deployments with separate
metadata databases, and a full shared-prefix listing is not bounded at production fragment counts.

Local region handoffs are retained for the same correctness reason. The hub does not evict files by age or
directory count: an mtime cannot prove that a cache entry, catalog version, concurrent hub, or active reader
no longer references an artifact. Monitor local region capacity until an ownership-aware artifact ledger with
exact-key cleanup is implemented.

`run_unit` mints a random attempt ID by default and enforces one owner per ID inside a runner. A caller that
supplies deterministic IDs must fence ownership in its durable control plane. A committed retry reattaches;
an existing partial/mismatched prefix or a manifest owned by another raw attempt ID fails closed and is
never overwritten. Attempt paths keep a readable run slug (capped at 64 characters) plus a 128-bit SHA-256
suffix over the complete raw attempt ID and unmodified logical URI. Whole-graph sinks also include the
write-step ID in that digest, so fan-out writes and `.parquet`/`.pq` targets cannot collide after extension
stripping. The manifest keeps the overall raw attempt ID for restart reattachment and auditability.

Repository changes can make the backend production-capable and provide repeatable validation. A specific
deployment is production-ready only after its IAM, network, storage, KubeRay, capacity, and operational
gates also pass.
