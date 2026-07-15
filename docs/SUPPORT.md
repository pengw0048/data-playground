# Supported deployments and trust model

Data Playground's supported deployment targets are a local workstation and a shared service operated
by a trusted team. "Supported" means the repository defines and tests the application contract for
that profile. It does not certify a particular host, cluster, identity system, or storage account.

This document is the canonical public boundary for deployment and security claims. The
[security policy](../.github/SECURITY.md) explains how to report a vulnerability; the
[release gates](CI.md) explain which evidence is required before publishing a version.

## Deployment profiles

| Profile | Support boundary |
| --- | --- |
| **Local workstation** | Primary profile. One trusted OS account runs the hub, SQLite, and local storage. Loopback HTTP and open development identity are the zero-configuration defaults. |
| **Trusted-team shared service** | Supported application profile when `DP_DEPLOYMENT_MODE=shared`, authentication, TLS, Postgres, durable storage, backups, and the documented topology are configured. The operator owns the deployment and everyone allowed to edit or run code is trusted with the workspace. |
| **Optional distributed execution** | Supported only for the shapes claimed by an installed execution backend. The bundled `dp_ray` reference has an exact [support matrix](RAY.md); repository Compose and KubeRay files are validation harnesses, not production manifests. |
| **Mutually distrusting tenants** | Not supported. The project does not provide a hostile-code sandbox, per-tenant data plane, or a zero-trust control plane. |

The trusted-team profile is not anonymous or unauthenticated. Canvas roles, session revocation, path
policy, credential handling, and other ordinary application controls remain part of the supported
contract. They protect a trusted workspace from mistakes, stale authorization, and unintended
exposure; they do not turn collaborators who can run arbitrary code into untrusted tenants.

## Trust inside a workspace

The following principals and components are trusted with the workspace data and the capabilities
needed to process it:

- operators and administrators;
- users allowed to edit a canvas or start arbitrary Python or section code;
- installed plugins, plugin dependencies, and per-canvas Python dependencies;
- execution backends, workers, runtime images, and their administrators; and
- storage/database administrators and configured external services.

Python transforms, section scripts, and plugins execute with the kernel or worker's process
permissions. A plugin's `register()` function also runs during startup. Install them only from sources
you trust. A plugin SPI, subprocess, container, or PodSpawner is an extension or deployment mechanism;
none is a tenant-isolation guarantee by itself.

Read-only canvas roles and API authorization are still enforced. However, canvas sharing is not
dataset isolation: catalog discovery, data adapters, credentials, and execution resources are scoped to
the workspace or deployment, not to mutually hostile tenants.

## Application guarantees

Within the profiles above, supported core paths preserve these requirements:

- Shared mode fails before binding unless signed sessions, Secure cookies, and a direct-TLS or trusted
  reverse-proxy declaration are configured. Canvas and administrative actions remain role-gated.
- Cred secret fields and plugin settings marked `secret` persist SecretRefs such as `env:NAME` or
  `file:/path`, not resolved values. Supported APIs redact residual plaintext, and one-shot subruns and
  Ray drivers do not receive the hub's session-signing or metadata-database credentials.
- If a destination selects a Cred, an execution backend must explicitly declare that it can honor that
  selection. Otherwise the run fails before dispatch; ambient identity is used only when no Cred is
  selected.
- Dataset-root and SQL policies fail closed for the declarative data paths they govern. They are defense
  in depth, not a boundary around arbitrary Python or plugin code.
- Built-in previews, compatibility transfers, queues, histories, and resource-sensitive operations have
  documented bounds. Built-in isolated local runs use cancellation, a run deadline, and process-group
  fencing on POSIX; non-POSIX systems can guarantee only direct-child termination.
- A backend may report `cancelled` as acknowledged only after it proves its worker can no longer
  publish. Durable backends must keep ambiguous remote outcomes non-terminal and recoverable.
- Schema migrations, backups, dependency scans, deterministic acceptance checks, and release evidence
  remain part of operating and publishing the product.

Plugin authors must preserve these properties for the capabilities they claim. A plugin that cannot
honor a selected credential, bounded preview, durable cancellation, or another optional contract must
omit that capability or fail explicitly; it must not silently substitute weaker behavior.

## Operator responsibilities

The repository cannot prove a concrete deployment production-ready. Operators are responsible for:

- TLS termination, trusted-proxy configuration, host and network access, and external identity policy;
- secret distribution and rotation, least-privilege storage/database IAM, and protecting backups;
- database and object-store durability, finite client timeouts, lifecycle rules, restore drills, and
  one-shot migration/upgrade sequencing;
- resource quotas, capacity planning, observability, alerting, incident response, and high availability;
- validating plugin packages, Python dependencies, runtime images, worker access, and external services;
  and
- running the relevant release and staging gates on the exact commit and intended topology.

For a shared service, WebSocket peers for one canvas must reach the same hub instance because
collaboration rooms are process-local. For distributed execution, the operator must additionally
protect the scheduler/control API, attest the cluster and immutable code image, configure data-plane
identity, and validate worker loss, upgrades, rollback, and capacity on the real cluster.

## Ray Jobs boundary

When `DP_RAY_JOBS_ADDRESS` is configured, `dp_ray` can own an admitted **whole graph** through Ray's
official Jobs API. Before submission, it persists the run, deterministic submission binding, canonical
job envelope, and source/output ownership in SQL, then writes the same bound envelope to shared object
storage. A replacement hub can reconstruct supervision after a hub-process restart, reattach to the same
submission, honor durable cancel intent, verify the bound result, and publish terminal run history and
idempotently keyed catalog effects as one logical terminal publication.

That guarantee is intentionally narrower than "Ray is production-ready":

- restart durability does not cover the process-local multi-region parent controller;
- the durable write path currently accepts one built-in, non-partitioned Parquet overwrite sink backed
  by a core-managed immutable object attempt;
- whole-graph Jobs has Jobs-API request timeouts but no automatic execution deadline; the
  `DP_RUN_DEADLINE_S` process deadline applies to the local/Popen driver, not a submitted Ray Job;
- live capacity discovery, admission backpressure, active-job failure injection, sharded supervision,
  protected log integration, and complete production observability remain open backend/operations work;
  and
- Jobs endpoint access, immutable image and cluster identity, database/object-store timeouts, artifact
  retention, IAM, quotas, HA, and upgrade/runbook validation are operator responsibilities.

See [Durable Ray Jobs execution](RAY_JOBS.md) for the exact state machine and
[Ray backend support](RAY.md) for supported operators, data movement, acceptance evidence, and current
production-ownership gates.

## What this project does not claim

- Arbitrary Python, installed plugins, or user-supplied dependencies are sandboxed from the kernel,
  workspace, or credentials.
- `DP_DATASET_ROOTS`, SQL validation, a container, or one Pod per canvas is sufficient tenant isolation.
- Canvas ACLs isolate workspace catalog entries or data-plane credentials from hostile collaborators.
- A green repository acceptance harness certifies an operator's IAM, topology, capacity, backups, HA, or
  incident procedures.

Supporting mutually hostile tenants would require a separately designed identity, isolation, data,
scheduling, and operations model. It is not an implicit extension of the profiles supported here.
