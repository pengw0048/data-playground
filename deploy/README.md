# Deploying the per-canvas kernel POD substrate

Data Playground can run each canvas's execution kernel as its own Kubernetes **Pod + Service**
(`DP_KERNEL_SPAWNER=pod`) instead of a local process — so a hub can run a kernel on another host, and
any hub can reach/kill it by resolving the canvas → Service DNS from the shared DB. This directory is a
**reference** deployment + an end-to-end verification you can run locally on [kind](https://kind.sigs.k8s.io).

## Release artifact smoke (local)

After building a wheel or running a container, the same offline starter-canvas smoke that CI runs in
`.github/workflows/release-artifacts.yml` is:

```bash
# Hub already serving (e.g. dataplay --no-open --workspace /tmp/ws):
python3 scripts/release_smoke.py --base-url http://127.0.0.1:8471 --expect-version 0.1.0

# Version surfaces must agree (pyproject / package.json / wheel /api/version / image label):
python3 scripts/check_release_versions.py \
  --pyproject kernel/pyproject.toml --package-json web/package.json \
  --require pyproject,package_json
```

Build order for a shippable wheel: `cd web && npm ci && npm run build`, then `cd kernel && uv build`.
`scripts/check_wheel_has_spa.py` fails if the wheel still contains the `hatch_build.py` placeholder UI.

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

## Metadata migration release step

Postgres-backed services never run schema DDL at startup. Before deploying a new hub/kernel image:

1. Stop **every** process that can write metadata: hub replicas, per-canvas kernel Pods, MCP servers,
   headless runs, and any external worker using the same database. Wait until they have actually exited;
   scaling a Deployment is asynchronous.
2. Run exactly one `dataplay migrate` process using the new release image and the normal
   `DP_DATABASE_URL`. Supply `DP_AUTH_SECRET` and the one-time `DP_AUTH_PASSWORD` here when bootstrapping
   the first admin; do not put `DP_AUTH_PASSWORD` in the application Deployment.
3. Start the new application replicas. Server, MCP, headless, and kernel processes fail closed unless
   the database is already at the build's exact Alembic head; `/api/readyz` reports the same check.

`k8s/migrate-job.yaml` is a reference pre-deploy Job. Give each real release Job a unique name (or
delete the completed reference Job before reapplying it), wait for it to complete, and only then roll
out the application. `k8s/pod-substrate.yaml` intentionally keeps the hub at zero replicas; the
verification script runs the Job and scales the hub to one only after it succeeds. Keep migration a
release-level Job, not a per-Pod initContainer: replicas must not race to migrate the same database.
The reference script can prove this ordering inside its disposable kind cluster, but an operator must
still stop MCP/headless/external writers that are outside that cluster. Local file-backed SQLite remains
zero-config and serializes automatic first-run migration with a lock derived from the resolved database
file path. A non-empty database without a recognized Alembic revision is rejected rather than guessed or
auto-stamped; recover it from a versioned backup or perform an explicit, audited conversion.

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
