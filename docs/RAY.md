# Ray backend: support and production readiness

`dp_ray` is a working **reference execution backend** for Data Playground. It proves that the
engine-neutral IR can execute on a real multi-node Ray Data cluster, and it is continuously checked by
the multi-node differential in [`ray-validation.yml`](../.github/workflows/ray-validation.yml).

It is not yet a production-capable backend. The Compose and KubeRay files in this repository are
validation harnesses, not deployment manifests. This page defines the current boundary so a green
differential is not mistaken for production readiness.

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
| `window` | yes, for a bare-column partition key | order-sensitive forms without a sufficient order fall back |
| full-row `dedup` | yes | keyed dedup and schemas containing floating-point values fall back |
| `join` | broadcast `inner`, `left`, and `cross` | the complete right side is collected by the driver and must be bounded |
| `sort` | plain-column keys | the final ordered result is coalesced to one worker; Ray 2.56 cannot resource-pin its range shuffle, so GPU/custom-resource sorts fail before dispatch |
| SQL, sections, metrics/charts, opaque plugin nodes | no | local fallback |

### Data movement

| path | current behavior |
|---|---|
| same-host local Parquet file or parts directory | Ray workers read Parquet directly |
| object-store Parquet | the driver reads through the dataset adapter, materializes Arrow, then creates a Ray Dataset |
| CSV, Lance, Iceberg, Hugging Face, or another adapter | the driver reads through the adapter, then creates a Ray Dataset |
| placed-region output | workers write a directory of Parquet shards directly to the selected storage tier |
| whole-graph write sink | Ray blocks are collected to the driver, then committed through the normal adapter/sink contract |

Object-store reads and whole-graph sinks are therefore **driver-funneled today**. They are semantically
supported but are not a scale-out path for data larger than driver memory.

## What the validation gate proves

The automated Compose gate starts a Ray head, two worker containers, a separate driver node, and MinIO.
It requires:

1. a real hash-shuffle to span at least two Ray node IDs;
2. distributed GROUP BY and broadcast join results to match DuckDB in Arrow schema and row values;
3. the schema, aggregate-row, and join-row fault controls to each fail;
4. a fresh run to pass after one worker is stopped between runs.

The last check proves that the remaining cluster accepts a **new degraded-cluster run**. It does not kill
a worker during an active job and does not prove in-flight task reconstruction. KubeRay validation is
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
| object-store scale-out reads | missing | worker-direct distributed reads, plus bounded fallback for adapters without that capability |
| durable job lifecycle | missing | persisted Ray submission/attempt ID, restart reconciliation, acknowledged cancel, timeout, and fencing |
| atomic region publication | implemented | distributed and local-fallback handoffs use immutable per-attempt prefixes; the controller validates the success manifest before cache publication |
| workload isolation | missing | explicit environment allowlist, scoped storage identity, per-run namespace, and cluster policy boundary |
| cluster health and placement truth | missing | live resource/health discovery, backpressure, and fail-loud behavior for an explicit Ray pin |
| runtime compatibility | partial | one supported Ray range and a driver/worker/core/plugin version handshake |
| resilience | partial | active-job worker/head/driver failure tests, retry policy, and orphan cleanup |
| observability | missing | durable job IDs, queue/retry/spill/storage metrics, structured logs, traces, and alerts |
| deployment security and HA | operator-owned | immutable images, secrets/IAM, TLS/network policy, pod security, quotas, autoscaling, and HA storage |

## Remaining P0 work

The following changes are required before calling the backend production-capable:

1. Remove the object-store Parquet driver funnel and introduce an explicit distributed-read capability
   for adapters, with a fail-loud or size-bounded fallback.
2. Replace local subprocess ownership with a durable Ray job lifecycle that survives kernel/hub restarts
   and supports idempotent submission, cancellation, timeout, retry, and attempt fencing.
3. Scope each workload's environment and storage identity. Arbitrary canvas code must not inherit
   control-plane credentials or another tenant's object prefix.
4. Discover live cluster capacity and health, enforce admission/backpressure, and reject an explicit Ray
   placement when its requirements cannot be honored.
5. Pin and verify the supported Ray/runtime image contract across the submitting process and every worker.
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
an existing partial/mismatched prefix fails closed and is never overwritten.

Repository changes can make the backend production-capable and provide repeatable validation. A specific
deployment is production-ready only after its IAM, network, storage, KubeRay, capacity, and operational
gates also pass.
