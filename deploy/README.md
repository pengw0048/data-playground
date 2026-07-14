# Deploying the per-canvas kernel Pod substrate

Data Playground can run each canvas kernel as its own Kubernetes Pod and Service
(`DP_KERNEL_SPAWNER=pod`) instead of a local process. The hub then starts kernels on other hosts and
reaches or kills them through Service DNS resolved from the shared database.

This directory is a reference deployment plus an end-to-end check you can run locally on
[kind](https://kind.sigs.k8s.io). It is not a production chart.

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

The script builds the image, creates a throwaway kind cluster, deploys the hub, Postgres, and RBAC
from `k8s/pod-substrate.yaml`, then drives a real run through the API. It asserts that PodSpawner
created a per-canvas kernel Pod, the run completed on it, and “restart kernel” tore the Pod down. It
uses its own kube-context and does not touch your other clusters. Set `KEEP=1` to leave the cluster
up; otherwise it tears itself down.

## Metadata migration release step

Postgres-backed services never run schema DDL at startup. Before deploying a new hub or kernel image:

1. Stop every process that can write metadata: hub replicas, per-canvas kernel Pods, MCP servers,
   headless runs, and any external worker using the same database. Wait until they have exited;
   scaling a Deployment is asynchronous.
2. Run exactly one `dataplay migrate` process from the new release image with the normal
   `DP_DATABASE_URL`. Supply `DP_AUTH_SECRET` and the one-time `DP_AUTH_PASSWORD` here when
   bootstrapping the first admin. Do not put `DP_AUTH_PASSWORD` in the application Deployment.
3. Start the new application replicas. Server, MCP, headless, and kernel processes fail closed unless
   the database is already at the build's exact Alembic head. `/api/readyz` reports the same check.

`k8s/migrate-job.yaml` is a reference pre-deploy Job. Give each real release Job a unique name, or
delete the completed reference Job before reapplying it. Wait for completion, then roll out the app.
`k8s/pod-substrate.yaml` keeps the hub at zero replicas on purpose; the verification script runs the
Job and scales the hub to one only after it succeeds. Keep migration as a release-level Job, not a
per-Pod initContainer — replicas must not race to migrate the same database.

The reference script proves that ordering inside its disposable kind cluster. An operator still has to
stop MCP, headless, and other writers outside that cluster. Local file-backed SQLite remains
zero-config and serializes automatic first-run migration with a lock derived from the resolved
database path. A non-empty database without a recognized Alembic revision is rejected rather than
guessed or auto-stamped; recover it from a versioned backup (see
[BACKUP_RESTORE.md](../docs/BACKUP_RESTORE.md)) or do an explicit, audited conversion.

## What the pieces are

`k8s/pod-substrate.yaml` defines the Namespace, Postgres (shared metadata DB), RBAC so the hub
ServiceAccount can create and delete Pods and Services, and the hub Deployment and Service. The hub
runs with `DP_KERNEL_SPAWNER=pod`, `DP_KERNEL_IMAGE`, and `DP_KERNEL_NAMESPACE`.

`k8s/Dockerfile.podverify` builds the app image with sample datasets baked in read-only. That stands
in for “data on shared storage” so the hub and every kernel Pod see the same path.

## Adapt before real use

Data must be reachable from the kernel Pods. The verify image bakes seed data into the image; a real
deployment points `DP_STORAGE_URL` at object storage (`s3://` or `gs://`) so every Pod reads the same
data. A local per-Pod path only works here because the image is read-only.

`DP_KERNEL_IMAGE` must be an image where `python -m hub.kernel` runs — `hub` importable by that
image's `python`. The bundled Dockerfile puts the venv on `PATH` for this.

Shared Postgres (`DP_DATABASE_URL`) must be reachable from the hub and all kernel Pods.

Harden for your cluster: a real image registry and pull secrets, resource requests and limits, a
NetworkPolicy for the kernel command channel, and pod cleanup. The hub's periodic reaper tears down a
dead kernel's Pod and Service; still set a sensible `DP_KERNEL_IDLE_TTL`.

Store credential-bearing settings as secret references (`env:VAR` or `file:/path`) — the agent API
key, object-store keys, and plugin secret fields — never plaintext, and inject the material values
through the Pod environment or mounted secret files. After upgrading past the SEC-03 migration,
re-enter any cleared legacy plaintext settings as references (see the root README).

## Notes from real-cluster verification

A Pod cold start (schedule plus heavy imports) is slower than a local process. Ready-wait is
configurable via `DP_KERNEL_READY_TIMEOUT_S`; the manifest sets it to 180s.

The kernel marks its lease ready as soon as it serves, but Kubernetes only routes the Service once
the readiness probe passes. The hub's first request can race that registration, so the hub retries
the connection briefly.
