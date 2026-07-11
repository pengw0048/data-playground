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

## What "PASS" proves

- `multi-node OK: N distinct Ray node ids executed work` — the shuffle really crossed nodes (N ≥ 2), not
  a single-host multiprocess stand-in.
- `distributed GROUP BY byte-identical to DuckDB` — the Ray hash-aggregate over the object-store exchange
  equals the DuckDB oracle (column names + Arrow **types** AND rows as a sorted multiset, NULL group +
  count-null semantics included), read back from the worker-direct object-store output.

**Trusting a green run.** The check compares schema *and* rows and propagates its real exit code (the
Compose driver escapes `$rc`; the KubeRay Job condition above gates on success). To prove the differential
would actually *catch* a mismatch, run the deliberate failing control — it perturbs the oracle so the
comparison must fail: `DP_MULTINODE_FAULT=1 docker compose -f docker-compose.ray.yml run --rm driver`
should exit **nonzero**.
