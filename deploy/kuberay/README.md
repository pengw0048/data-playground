# Multi-node validation of the distributed engine (dp_ray)

The distributed backend's promise is *byte-identical to single-node DuckDB, on a real multi-node
cluster*. Two ways to prove it — both run the same check (`hub/ray_multinode_check.py`): a distributed
GROUP BY over Ray Data's hash shuffle, written worker-direct to object storage, compared to DuckDB.

## 1. docker-compose (fastest — a head + 2 worker containers + MinIO)

```bash
docker compose -f docker-compose.ray.yml up -d --scale ray-worker=2 --build
docker compose -f docker-compose.ray.yml run --rm driver     # → "[multinode] PASS: … byte-identical …"
docker compose -f docker-compose.ray.yml down -v
```

Kill-a-worker recovery: with the cluster up, `docker kill data-playground-ray-worker-2` and re-run the
`driver` — the degraded cluster still returns byte-identical output (Ray reschedules / reconstructs from
lineage). Validated: 4 nodes → PASS; after killing a worker, 3 nodes → PASS.

## 2. KubeRay on Kubernetes (e.g. kind — the pods path)

```bash
kind create cluster
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ && helm repo update
helm install kuberay-operator kuberay/kuberay-operator

docker build -f docker/ray/Dockerfile -t dp-ray:local .
kind load docker-image dp-ray:local

kubectl apply -f deploy/kuberay/raycluster.yaml
kubectl apply -f deploy/kuberay/minio.yaml
kubectl apply -f deploy/kuberay/differential-job.yaml
kubectl logs -f job/dp-ray-multinode-check           # → "[multinode] PASS: … byte-identical …"

# Make it a real GATE, not just a log tail: wait for the Job to actually SUCCEED (nonzero = FAIL).
kubectl wait --for=condition=complete --timeout=600s job/dp-ray-multinode-check \
  || { echo "multinode differential FAILED"; kubectl logs job/dp-ray-multinode-check; exit 1; }
```

Both paths use the same `docker/ray/Dockerfile` image, which bakes in the Ray-2.56 hash-shuffle compat
shim (`hub.ray_compat`) via the worker-setup-hook env var, so no per-node tuning is needed.

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
  DP_MULTINODE_FAULT=$f docker compose -f docker-compose.ray.yml run --rm driver   # expect NONZERO each
done
docker compose -f docker-compose.ray.yml run --rm driver                            # clean → PASS (exit 0)
```

(`DP_MULTINODE_FAULT=1` is accepted as an alias for `rows`, back-compat.)
