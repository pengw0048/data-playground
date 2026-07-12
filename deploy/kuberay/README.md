# Multi-node validation of the Ray reference backend (`dp_ray`)

This harness checks selected `dp_ray` operations against single-node DuckDB on a real multi-node
cluster. Both paths below run `hub/ray_multinode_check.py`: a distributed GROUP BY and broadcast join,
written worker-direct to object storage, compared with DuckDB. It does not certify every supported
operation or make `dp_ray` production-capable; see the [support/readiness matrix](../../docs/RAY.md).
The optional whole-graph Ray Jobs control-plane lifecycle is documented separately in
[Durable Ray Jobs execution](../../docs/RAY_JOBS.md). Expose and protect the head Dashboard/Jobs endpoint
according to cluster policy; these validation manifests deliberately do not publish it.

## 1. docker-compose (fastest — a head + 2 worker containers + MinIO)

```bash
docker compose -f docker-compose.ray.yml build ray-head
docker compose -f docker-compose.ray.yml up -d --no-build --scale ray-worker=2 \
  ray-head ray-worker minio createbucket
docker compose -f docker-compose.ray.yml run --rm --no-deps driver  # → "[multinode] PASS: … byte-identical …"
docker compose -f docker-compose.ray.yml down -v
```

**Degraded-cluster rerun:** stop one worker *between* runs and start the driver again. A PASS proves the
remaining cluster accepts fresh work and still returns the same result. This is deliberately not called
in-flight recovery: the harness does not kill a worker during an active job and does not prove lineage
reconstruction for that job.

```bash
worker="$(docker compose -f docker-compose.ray.yml ps -q ray-worker | tail -1)"
docker kill "$worker"
docker compose -f docker-compose.ray.yml run --rm --no-deps driver
```

## 2. KubeRay on Kubernetes (e.g. kind — the pods path)

These manifests are a disposable validation environment: fixed workers, ephemeral MinIO, and test
credentials. They are not a secure or highly available production deployment. Their CPU/memory
**requests** let a scheduler place one head, two workers, MinIO, and the driver on the validated
4-CPU/8-GiB single-node kind profile; limits and Ray logical capacity can exceed those requests, so this
is neither a peak-capacity guarantee nor production sizing guidance.

```bash
kind create cluster
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ && helm repo update
helm install kuberay-operator kuberay/kuberay-operator

KIND_CLUSTER=kind ./deploy/kuberay/validate.sh
# → all three RayCluster pods and the Job must become Ready/Complete
# → "[multinode] PASS: … byte-identical …"
```

The script is intentionally re-runnable. Each invocation obtains an isolated kubeconfig directly from
the named kind cluster, builds and loads a unique image tag, foreground-deletes the prior RayCluster and
immutable Jobs, proves their pods are gone, and then creates fresh pod templates. Reusing
`dp-ray:local` with `imagePullPolicy: IfNotPresent` plus `kubectl apply` can otherwise leave old cluster
pods and an old Job running even after a new image was loaded. Set `KIND_CLUSTER=<name>` for a
non-default kind cluster; `DP_RAY_VALIDATION_IMAGE=<unique-tag>` can override the generated tag, and
`DP_RAY_VALIDATION_TIMEOUT_SECONDS=<seconds>` controls the differential deadline. A failed Job is
reported immediately with its logs instead of waiting through the deadline.

Both paths use the same `docker/ray/Dockerfile` image. The optional dependency, image, and KubeRay
`rayVersion` are pinned to **Ray 2.56.0**, the only version currently validated against dp_ray's private
hash-shuffle ABI. At startup the driver runs a node-affine version handshake against every alive node;
an unsupported or mixed cluster fails before any Dataset source or operator executes. Every worker also
validates the private shuffle attributes before installing the compatibility shim.

## What "PASS" proves (and what it doesn't)

- `multi-node OK: a hash-shuffle exchange spanned N distinct Ray node ids` — the **cluster** runs the
  aggregate's exact mechanism (repartition-by-key → per-partition map) across N ≥ 2 nodes, not a
  single-host multiprocess stand-in.
- `distributed GROUP BY (placement=distributed) byte-identical to DuckDB` — the tested query ran on the
  **Ray path** (`status.placement == "distributed"`, *not* dp_ray's silent single-node fallback — without
  this a local fallback would match the DuckDB oracle trivially and the gate would prove nothing), and its
  worker-direct object-store output equals the DuckDB oracle (column names + Arrow **types** AND rows as a
  sorted multiset, NULL group + count-null semantics included). Same for the broadcast join.

**Honest scope of "N nodes".** N is credited to the **cluster/shuffle** (measured by the probe), not to
the specific GROUP BY/join — a query's own per-task node spread isn't observable from the driver process
(the dp_ray run executes in its own subprocess). `placement=distributed` is what proves the tested query
ran distributed; the probe proves the cluster genuinely spreads a shuffle across nodes.

**Trusting a green run.** The check compares schema *and* rows and propagates its real exit code (the
Compose driver escapes `$rc`; the KubeRay Job condition above gates on success). Each of the **three
oracles** (schema parity, aggregate rows, join rows) has its own fault-injection control, so none can be
silently inert. Run the harness once per fault target — each **must exit nonzero** — plus once clean
(**must pass**):

```bash
for f in schema rows join; do
  DP_MULTINODE_FAULT=$f docker compose -f docker-compose.ray.yml run --rm --no-deps driver  # expect NONZERO each
done
docker compose -f docker-compose.ray.yml run --rm --no-deps driver  # clean → PASS (exit 0)
```

(`DP_MULTINODE_FAULT=1` is accepted as an alias for `rows`, back-compat.)
