# Deploying the per-canvas kernel POD substrate

Data Playground can run each canvas's execution kernel as its own Kubernetes **Pod + Service**
(`DP_KERNEL_SPAWNER=pod`) instead of a local process — so a hub can run a kernel on another host, and
any hub can reach/kill it by resolving the canvas → Service DNS from the shared DB. This directory is a
**reference** deployment + an end-to-end verification you can run locally on [kind](https://kind.sigs.k8s.io).

## Verify it locally (kind)

```bash
brew install kind           # or see kind's install docs
deploy/verify-pod-substrate.sh
```

The script builds the image, creates a throwaway kind cluster, deploys the hub + Postgres + RBAC
(`k8s/pod-substrate.yaml`), then drives a real run through the API and asserts that **PodSpawner spawned
a per-canvas kernel Pod, the run completed on it, and "restart kernel" tore the Pod down**. It uses only
a local, disposable cluster (its own kube-context) and never touches your other contexts. `KEEP=1` leaves
the cluster up for poking; otherwise it self-tears-down.

## What the pieces are

- `k8s/pod-substrate.yaml` — Namespace, Postgres (the shared metadata DB), RBAC (the hub's ServiceAccount
  may create/delete Pods+Services), and the hub Deployment+Service. The hub runs with
  `DP_KERNEL_SPAWNER=pod`, `DP_KERNEL_IMAGE`, `DP_KERNEL_NAMESPACE`.
- `k8s/Dockerfile.podverify` — the app image + the sample datasets baked in read-only, a local stand-in
  for "data on shared storage" so the hub and every kernel pod see the same data at the same path.

## Adapt before real use (this is a reference, not a production chart)

- **Data must be reachable from the kernel pods.** The verify image bakes seed data into the image; a
  real deployment points `DP_STORAGE_URL` at object storage (`s3://` / `gs://`) so every pod reads the
  same data. A local per-pod path only works because it's baked read-only.
- **Image**: `DP_KERNEL_IMAGE` must be an image where `python -m hub.kernel` runs — i.e. `hub` importable
  by the image's `python`. The bundled Dockerfile puts the venv on `PATH` for exactly this.
- **Shared Postgres** (`DP_DATABASE_URL`) reachable from the hub and all kernel pods.
- Harden for your cluster: a real image registry + pull secrets, resource requests/limits, a
  NetworkPolicy for the kernel command channel, and pod cleanup (the hub's periodic reaper tears down a
  dead kernel's Pod+Service, but set sensible `DP_KERNEL_IDLE_TTL`).

## Notes from verifying this end-to-end on a real cluster

Things that only surface on a real (Postgres + k8s) cluster, now handled:

- A pod's cold-start (schedule + heavy imports) exceeds a local process's — the ready-wait is
  configurable via `DP_KERNEL_READY_TIMEOUT_S` (the manifest sets it to 180s).
- The kernel marks its lease *ready* as soon as it serves, but k8s only routes the Service to the pod
  once its readiness probe passes — so the hub's first request can race that registration; the hub
  retries the connection briefly.
