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

The backend conservatively falls back to the local DuckDB runner when it cannot prove that a graph is
safe to distribute.

| operation | distributed today | important boundary |
|---|---|---|
| `map`, `filter`, `flat_map`, `map_batches` | yes | uses the same compiled transform operator as the local engine |
| grouped `aggregate` | yes, for bare-column group keys | global, expression-key, and order-sensitive aggregates fall back |
| `window` | yes, for a bare-column partition key | order-sensitive forms without a sufficient order fall back |
| full-row `dedup` | yes | keyed dedup and schemas containing floating-point values fall back |
| `join` | broadcast `inner`, `left`, and `cross` | the complete right side is collected by the driver and must be bounded |
| `sort` | plain-column keys | the final ordered result is coalesced to one worker |
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
docker compose -f docker-compose.ray.yml up -d --build --scale ray-worker=2 \
  ray-head ray-worker minio createbucket
docker compose -f docker-compose.ray.yml run --rm driver

for fault in schema rows join; do
  if DP_MULTINODE_FAULT="$fault" docker compose -f docker-compose.ray.yml run --rm driver; then
    echo "ERROR: $fault control unexpectedly passed"
    exit 1
  fi
done

worker="$(docker compose -f docker-compose.ray.yml ps -q ray-worker | tail -1)"
docker kill "$worker"
docker compose -f docker-compose.ray.yml run --rm driver  # fresh degraded-cluster run
docker compose -f docker-compose.ray.yml down -v
```

## Production-readiness matrix

| gate | status | production-capable requirement |
|---|---|---|
| selected-operator semantic parity | partial | extend multi-node differentials to every claimed operator and edge type |
| object-store scale-out reads | missing | worker-direct distributed reads, plus bounded fallback for adapters without that capability |
| durable job lifecycle | missing | persisted Ray submission/attempt ID, restart reconciliation, acknowledged cancel, timeout, and fencing |
| atomic region publication | missing | immutable attempt prefixes and a validated success manifest/pointer before readers see output |
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
3. Publish region output through an immutable attempt prefix plus a success manifest or atomic pointer;
   never expose a partially overwritten shard directory.
4. Scope each workload's environment and storage identity. Arbitrary canvas code must not inherit
   control-plane credentials or another tenant's object prefix.
5. Discover live cluster capacity and health, enforce admission/backpressure, and reject an explicit Ray
   placement when its requirements cannot be honored.
6. Pin and verify the supported Ray/runtime image contract across the submitting process and every worker.
7. Pass staging gates on the intended production topology: active-job failure injection, representative
   large workloads, SLOs/alerts, upgrade/rollback, and recovery runbooks.

Repository changes can make the backend production-capable and provide repeatable validation. A specific
deployment is production-ready only after its IAM, network, storage, KubeRay, capacity, and operational
gates also pass.
