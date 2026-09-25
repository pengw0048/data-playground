# Backup and restore

This runbook defines what a Data Playground backup must capture, how to take and restore
it for each supported storage profile, and how the repository proves that an isolated
restore works. Disaster-recovery takeover of an existing object-store namespace is **not**
implemented and must not be inferred from this procedure — see [RAY.md](RAY.md).
Run the repository commands below from the checkout root.

## What to back up

Every profile that stores durable work must capture the same **logical** set. The physical
steps below differ by profile.

| Item | Why it matters |
|---|---|
| **Metadata database** | Canvases (`canvases.doc` JSON), canvas version snapshots, run history and run state, catalog entries, columns, lineage facts, publication receipts, embeddings, settings and Cred rows, the local-result artifact registry, managed object-attempt lifecycle rows, and the `installation_identity` singleton (owner token + storage namespace). For versioned data this explicitly includes `catalog_logical_datasets` (including unregistered tombstones), `managed_local_file_revisions`, `run_input_admissions`, `run_records.input_manifest`, `local_result_artifacts`, `local_result_references`, and the object-attempt ref / lease / inventory tables. Exact execution history additionally requires `execution_manifests` and every `execution_manifest_sha256` owner on admissions, run state/history, receipts/revisions, lineage, durable Tasks/Attempts, and Inbox items. Durable task recovery also requires `durable_tasks`, `durable_task_attempts`, `durable_task_inbox_items`, `durable_external_waits`, `durable_checkpoints`, and the bounded fan-out plan, unit, attempt, and slot tables. The applicable family-specific state is also metadata: `distribution_report_envelopes`, `merge_columns_task_envelopes`, `restore_revision_task_envelopes`, and `keyed_upsert_task_envelopes`; provider placement metadata is in `workspace_provider_datasets` and `workspace_provider_bindings`. These rows are one consistency unit: never reconstruct identity or references from a path or display name after restore. Publication receipts are required to preserve exact-replay tombstones after facts or catalog entries are unregistered. |
| **Workspace files** | Under `DP_WORKSPACE`: `dataplay.db` when using SQLite, `outputs/` (run results plus immutable core-owned revision artifacts under `.dp-results/`), and `plugins/` (operator-installed packs). Preserve the complete `.dp-results` namespace metadata and record hashes for every core-owned artifact referenced by `managed_local_file_revisions`; copying only current catalog heads loses retained history. |
| **Object-store generations + namespace marker** | When `DP_STORAGE_URL` points at `s3://` / `gs://` (or compatible), retain object generations under the installation's storage namespace **and** the conditional marker at `_dp_control/namespaces/<namespace>.json`. The metadata DB alone is not enough: `local_result_artifacts` and attempt rows reference exact URIs. For the SeaweedFS profile, use native filer replication configured before protected writes, then freeze the verified replica for each recovery point; an S3 copy of current objects is insufficient. See Profile B. |
| **Provider-owned history evidence (not provider bytes)** | The database retains opaque registration / dataset IDs, exact provider revision IDs, pinned Source refs, and admitted run manifests. The logical backup does **not** include provider-owned Lance or plugin-provider bytes. Back those up through the provider's own system if required, and classify each restored exact read as available or unavailable instead of treating a current same-name/path dataset as the old revision. |
| **Credential references** | Cred rows and plugin `secret` settings contain SecretRefs plus non-secret connection metadata, not resolved credential values. Back up the referenced environment, files, or external secret manager separately; a metadata restore cannot recreate them. |
| **Catalog mount configuration** | `DP_CATALOG_MOUNTS` is operator deployment configuration, not metadata or workspace state, so it is deliberately **not** in a backup. Restore the same JSON from the protected deployment configuration separately. Its `env:` / `file:` references are safe to record there; restore the referenced secret environment or files separately. Together with provider bytes and resolved credentials, it remains under the external operator/provider ownership boundary and is never backed up by core. |
| **Release identity** | Record `GET /api/version` (`sha`, `db` dialect, `storage` scheme) and the Alembic revision stored in the metadata DB (`alembic_version.version_num`, also exposed by `metadb.expected_schema_head()` / `metadb.require_schema_at_head()`). A restore must land on a release that understands that schema. |

### Consistency ordering

1. **Stop every metadata writer** before the snapshot window begins: hub replicas, per-canvas kernels, MCP servers, headless runs, and any external worker using the same database or writing into the same object-store namespace. Wait until they have exited; scaling a Deployment is asynchronous.
2. Snapshot the **metadata database** and the **artifact / object store** as close together as possible. Prefer: freeze writers → dump DB → verify and freeze the version-preserving object replica and namespace marker → copy local workspace files that are not already in the DB dump.
3. Resume writers only after the backup set is complete and verified (checksums or byte sizes recorded).

A backup taken while writers are still active can leave dangling artifact URIs, a retention ref
without its immutable artifact, or a manifest revision that does not match the files on disk. Do
not synthesize expired read leases after restore; durable refs and tombstones come from the database
snapshot, while a new process acquires fresh DB-clock leases for new reads.

## Profile A — SQLite + local files

Runnable on a laptop with no extra infrastructure.

### Backup

Assume the hub was started with `DP_WORKSPACE=/data` (default: the kernel package root) and
no `DP_DATABASE_URL` / `DP_STORAGE_URL` overrides.

```bash
# 1. Record release identity while the old process is still readable:
mkdir -p backup
curl -sS http://127.0.0.1:8471/api/version | tee backup/version.json
# 2. Stop every process writing this workspace (hub, kernels, MCP, headless), then copy:
cp -a "$DP_WORKSPACE/dataplay.db" backup/dataplay.db
cp -a "$DP_WORKSPACE/outputs" backup/outputs
cp -a "$DP_WORKSPACE/plugins" backup/plugins 2>/dev/null || true
# Bind the frozen DB schema to the recorded release identity:
uv run --project kernel python - <<'PY'
import json
from pathlib import Path
from hub import metadb
path = Path("backup/version.json")
doc = json.loads(path.read_text())
doc["alembic"] = metadb.require_schema_at_head()
path.write_text(json.dumps(doc, sort_keys=True) + "\n")
PY
# Record content evidence relative to outputs/ (use `shasum -a 256` where sha256sum is unavailable):
(cd "$DP_WORKSPACE/outputs" && \
  find ./.dp-results -type f ! -path '*/.locks/*' -print0 | sort -z | \
  xargs -0 sha256sum) > backup/core-artifacts.sha256
# Alembic revision is inside dataplay.db (table alembic_version). Keep version.json with the set.
```

### Restore (isolated clone — default)

Restore into a **fresh** workspace. Never point the clone at the live workspace path while the
source installation is still running.

```bash
RESTORE=/tmp/dp-restore-$$
BACKUP="$(cd backup && pwd)"
mkdir -p "$RESTORE"
cp -a "$BACKUP/dataplay.db" "$RESTORE/dataplay.db"
cp -a "$BACKUP/outputs" "$RESTORE/outputs"
cp -a "$BACKUP/plugins" "$RESTORE/plugins" 2>/dev/null || true
cp -a "$BACKUP/version.json" "$RESTORE/version.json"
(cd "$RESTORE/outputs" && sha256sum -c "$BACKUP/core-artifacts.sha256")

# Built-in local-result and managed-revision URIs are exact absolute paths. For an isolated
# clone, mount the copied outputs tree at the original recorded output path inside the clone's
# container/sandbox. A plain copy to a different host path does not rebind those identities.
# Set DP_STORAGE_URL to that mounted outputs root (the parent of the recorded
# local_result_artifacts.storage_root) so exact reads acquire the restored DB read lease.
# If the original path cannot be reproduced, exact reads must report unavailable and the
# mismatch evidence below must name the expected URI and copied candidate; never rewrite or
# follow the latest dataset implicitly.

# Assign a fresh storage namespace and isolate BEFORE any provider / object-attempt access.
export DP_WORKSPACE="$RESTORE"
export DP_DATABASE_URL="sqlite:///$RESTORE/dataplay.db"
export DP_STORAGE_URL="<mounted original outputs root>"
# Read the source namespace from the restored DB, then isolate:
uv run --project kernel python - <<'PY'
from hub import metadb
metadb.init_db()
expected = metadb.object_storage_namespace()  # only safe before DP_STORAGE_NAMESPACE is set wrongly
replacement = "restore-" + __import__("uuid").uuid4().hex[:16]
assert metadb.isolate_cloned_object_storage(expected, replacement) == replacement
print("isolated:", replacement)
PY
export DP_STORAGE_NAMESPACE="<replacement printed above>"

# Start the hub against the clone only.
dataplay --workspace "$RESTORE"
```

After isolation the clone has a new owner token and namespace; inherited managed object attempts
are quarantined and their catalog/cache visibility revoked. Local canvases, ordinary catalog
rows, run history, lineage among non-attempt URIs, revision ledgers, manifests, tombstones, and
retention references remain present. A core-owned exact revision is readable only when its copied
artifact is present at the recorded exact URI (normally by reproducing the original mount path in
the isolated environment). Provider-owned exact history is independently available or unavailable
according to provider read-back; it is never supplied by this backup set.

### Documented limitation (skipped isolation)

If a restored clone is pointed at a **fresh** `DP_STORAGE_NAMESPACE` **without** calling
`isolate_cloned_object_storage`, object-attempt access fails closed with:

> `DP_STORAGE_NAMESPACE does not match this metadata database; isolate an offline metadata clone explicitly before allocating object attempts`

A clone that copies the source configuration **unchanged** (same DB contents and same
`DP_STORAGE_NAMESPACE` as the live installation) is indistinguishable from the source without the
provider-side namespace marker — which is why the restore procedure must always assign a fresh
namespace and run isolation first. Do not skip that step.

## Profile B — PostgreSQL + S3-compatible object store

The repository harness uses SeaweedFS 4.47; [RAY.md](RAY.md) documents its Compose setup.
This profile preserves native S3 version IDs and delete markers through **SeaweedFS-to-SeaweedFS
filer replication**. It does not migrate an existing MinIO data directory, reconstruct versions
through S3 PUT, or certify another S3 provider's backup mechanism.

### Configure native replication before protected writes

Provision an independent SeaweedFS cluster with its own master, filer metadata store, and volume
storage in a separate failure domain. Use the same tested release and bucket names on both sides.
The source and replica must not share volume files or a filer database. Keep the replica free of
application writes. Save service configuration and referenced secrets through the deployment
backup, separately from the data.

Run one persistent active-passive sync process before the first protected object write:

```bash
# Native filer addresses, not the S3 endpoints. Run this as a supervised long-lived process.
# Both filers and both clusters' volume servers must be reachable by this process.
weed filer.sync \
  -a="$DP_PRIMARY_FILER" -b="$DP_BACKUP_FILER" \
  -a.path=/buckets -b.path=/buckets -isActivePassive \
  -concurrency=1 -chunkConcurrency=2
```

This copies native bucket metadata, version entries, deletion metadata, and their chunks; it does
not issue a new S3 PUT for every historical version. The `/buckets` scope includes the installation's
`_dp_control/namespaces/<namespace>.json` object inside its bucket. This procedure does not rename
buckets or paths. Enable versioning on the new source bucket before application writes; confirm
`get_bucket_versioning` reports `Enabled` on both endpoints before relying on the replica:

```bash
uv run --project kernel python - <<'PYTHON'
import os
import boto3
s3 = boto3.client("s3", endpoint_url=os.environ["DP_S3_ENDPOINT"],
                  aws_access_key_id=os.environ["DP_S3_KEY"],
                  aws_secret_access_key=os.environ["DP_S3_SECRET"],
                  region_name=os.environ.get("AWS_REGION", "us-east-1"))
s3.put_bucket_versioning(Bucket=os.environ["DP_S3_BUCKET"],
                         VersioningConfiguration={"Status": "Enabled"})
PYTHON
```

SeaweedFS documents [active-passive filer synchronization](https://github.com/seaweedfs/seaweedfs/wiki/Filer-Active-Active-cross-cluster-continuous-synchronization)
as asynchronous native change-log replication. It copies chunks and metadata, persists checkpoints,
and requires access to both clusters. It is not an immutable archive: subsequent source deletions
also propagate. If using encryption, retain the provider's required encryption keys for the replica.
Do not assume that starting sync on an existing populated bucket proves complete historical catch-up;
that migration needs separate evidence. The certified procedure starts replication before protected
writes and verifies a bounded recovery point as described below.

### Capture a recovery point

Stop all installation writers, including hub replicas, per-canvas kernels, remote jobs, and any
lifecycle/GC process that can delete protected objects. Leave filer sync running long enough to catch
up. Then capture the database and compare the full object-version manifests:

```bash
mkdir -p backup
curl -sS http://127.0.0.1:8471/api/version | tee backup/version.json
# Stop all writers before the following dump and inventory operations.
pg_dump --format=custom --no-owner --no-acl \
  "$DP_DATABASE_URL_LIBPQ" -f backup/dataplay.dump
uv run --project kernel python - <<'PYTHON'
import json
from pathlib import Path
from hub import metadb
path = Path("backup/version.json")
doc = json.loads(path.read_text())
doc["alembic"] = metadb.require_schema_at_head()
doc["namespace"] = metadb.object_storage_namespace()
path.write_text(json.dumps(doc, sort_keys=True) + "\n")
PYTHON
```

Run this comparison from the checkout root. It includes every version and delete marker, the latest
flags, sizes, and ETags. A mismatch is an incomplete backup; allow replication to catch up and repeat
while writers remain stopped. Include every bucket referenced by the database, including a separate
Ray Jobs artifact bucket if configured:

```bash
uv run --project kernel python - <<'PYTHON'
import json
import os
from pathlib import Path
import boto3
from scripts.verify_versioned_object_backup import manifest

bucket = os.environ["DP_S3_BUCKET"]
results = {}
for name, prefix in (("primary", "DP_S3"), ("replica", "DP_OBJECT_BACKUP")):
    client = boto3.client("s3", endpoint_url=os.environ[prefix + "_ENDPOINT"],
                          aws_access_key_id=os.environ[prefix + "_KEY"],
                          aws_secret_access_key=os.environ[prefix + "_SECRET"],
                          region_name=os.environ.get("AWS_REGION", "us-east-1"))
    if client.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise RuntimeError(f"{name}: bucket versioning is not enabled")
    results[name] = manifest(client, bucket)
    Path(f"backup/{bucket}.{name}.versions.json").write_text(
        json.dumps(results[name], sort_keys=True, indent=2) + "\n")
if results["primary"] != results["replica"]:
    raise RuntimeError("Source and replica version manifests differ; backup is incomplete")
PYTHON
```

**Stop the dedicated filer sync process after the manifests match, then repeat the comparison.**
Keep that replica's native metadata and volumes unchanged as the object component of this database
backup. Record its deployment/storage identity with the backup set. Do not resume syncing into a
replica retained for this recovery point: later deletes would invalidate the saved database's exact
references. Continuing live replication requires a separate replica or a separately validated native
snapshot/retention procedure. A metadata export alone does not contain the object chunks.

Copy workspace plugins and any built-in local outputs as in Profile A. Resume source writers only
after the backup set is complete and the replica is frozen. The manifest JSON files are evidence;
the independent replica holds the versioned bytes. As an object-level recovery check, stop access to
the source, restart the replica from its own persistent storage, and read a selected non-current
version with S3 `get_object(Bucket=..., Key=..., VersionId=...)`. Confirm the bytes and current delete
markers against the recorded evidence before accepting the recovery point.

An ordinary recursive current-object copy (`mc cp`, `aws s3 cp`, or GET followed by PUT) loses
non-current versions and delete markers and assigns new version IDs. It cannot replace native
history preservation, even if every current file is byte-identical.

### Executable version-history drill

```bash
# Optional: set DOCKER_CONTEXT to the intended Docker daemon.
DP_OBJECT_BACKUP_EVIDENCE_DIR=/tmp/dp-object-backup-evidence \
  bash scripts/verify_versioned_object_backup.sh
```

The drill uses the same pinned SeaweedFS 4.47 image as the Ray harness and the kernel's existing
Boto3 dependency. It creates disposable source, replica, and ordinary-copy instances with independent
storage, and one native sync process. It retains the original four-entry test (two versions of one
key, another historical value, and its delete marker), plus a namespace marker in a separate bucket.
The historical value exceeds 2 MiB so recovery exercises volume chunks. It then stops both source
and sync, restarts the replica, compares exact manifests, and reads all retained values. The ordinary
copy negative control preserves the current value but loses history.

`VERSIONED_OBJECT_BACKUP_EVIDENCE` is printed only after the independent recovery and history checks
pass. `VERSIONED_OBJECT_BACKUP_CP_CONTROL=lost_history` requires the negative control to demonstrate
history loss. Manifests and service logs are saved in the evidence directory; disposable containers,
volumes, and network are removed. This is an operator/release drill, not part of the ordinary CI matrix.
It proves this fresh-cluster workflow on the pinned release, not arbitrary migration, historical
backfill, encryption configurations, or automatic Data Playground namespace takeover.

### Restore (isolated clone — default)

```bash
# Fresh database and fresh object-store prefix/namespace — never the live ones.
RESTORE=/tmp/dp-restore-$$
mkdir -p "$RESTORE"
createdb -U dp dataplay_restore   # or restore into an empty database owned by the drill
pg_restore --clean --if-exists --no-owner --no-acl -d "$RESTORE_DATABASE_URL_LIBPQ" backup/dataplay.dump
cp -a backup/version.json "$RESTORE/version.json"

# Do not restore current-object copies: they cannot preserve exact version IDs.
# The exact generations remain on the verified, frozen independent replica. This isolated clone still must
# NOT claim the source namespace marker; isolation creates a new claim under the replacement namespace.
# Restore any built-in local `outputs/` tree at the same absolute mount path recorded by
# `local_result_artifacts.storage_root`, and set DP_STORAGE_URL to its parent outputs directory;
# a new Postgres database does not rewrite file URIs or local read-lease ownership.

export DP_DATABASE_URL="postgresql+psycopg://..."   # the restore DB only
export DP_STORAGE_URL="s3://..."                    # bucket/prefix the clone may use
unset DP_STORAGE_NAMESPACE                          # until after isolation
uv run --project kernel python - <<'PY'
from hub import metadb
metadb.init_db()
expected = metadb.object_storage_namespace()
replacement = "restore-" + __import__("uuid").uuid4().hex[:16]
metadb.isolate_cloned_object_storage(expected, replacement)
print(replacement)
PY
export DP_STORAGE_NAMESPACE="<replacement>"
# Start hub / migrate-at-head checks against the restore DB only.
```

The replica preserves object bytes and version IDs for an audited recovery point, but it does **not**
turn this isolated-clone procedure into disaster recovery. Data Playground currently rejects a clone
that tries to claim the original namespace marker, so operating the replica as the original
installation remains unsupported. Use S3 `get_object(..., VersionId=<id>)` against the replica only for object-level verification
or provider-level recovery until an audited takeover workflow exists.

## What restore is not

- **Not disaster-recovery takeover.** `isolate_cloned_object_storage` rotates owner and
  namespace, quarantines inherited attempts, and clears copied marker claims. It does not
  acquire the original `_dp_control/namespaces/<old>.json` marker. Audited takeover of an
  existing namespace is unimplemented; changing `DP_STORAGE_NAMESPACE` alone is rejected
  ([RAY.md](RAY.md)).
- **Not a secret-backend backup.** The metadata database restores Cred and plugin SecretRefs,
  but it does not restore referenced environment variables, mounted secret files, or an external
  secret manager. Restore those through the deployment system before exercising the credentials.
- **Not a provider-data backup.** Provider-owned revision IDs and exact references are retained as
  evidence, but provider files, object versions, credentials, and retention policy remain owned by
  that provider. A 410 exact-unavailable result is truthful; opening provider head instead is not.
- **Not identity repair.** Do not recreate a missing catalog registration from a restored display
  name or path. The restored opaque dataset ID and revision ID either resolve together or remain
  unavailable. Unregistered logical rows stay tombstones and must not be projected as current.
- **Not cross-schema conversion.** This is the current pre-1.0 logical schema contract. Restore the
  recorded schema with a release that understands it; do not drop, rename, or infer revision rows as
  an unpublished compatibility migration.

## Revision recovery verification

Before allowing runs or edits against a restore, verify the revision consistency unit and retain
the output with the drill evidence:

1. Compare the restored Alembic head and release identity with `version.json`.
2. Confirm every pinned Source and every `run_input_admissions.manifest` /
   `run_records.input_manifest` pair has the original ordered opaque dataset/revision IDs.
3. Join `managed_local_file_revisions` to `local_result_artifacts` and
   `local_result_references`. Verify every retained core artifact exists at its exact URI and matches
   the hash recorded with the backup. An active head, historical revision, or unregistered tombstone
   without its retention row is a restore mismatch.
4. Open each core-owned selected revision by its original dataset/revision pair. Opening current
   head is not a substitute. Confirm the read holds a fresh `read_lease` reference against the
   restored artifact. Missing or changed bytes are an actionable backup failure.
5. Exact-read each provider-owned selected revision. Report `available`, `unavailable`,
   `permission_lost`, or `provider_offline` from that exact read. Do not copy provider bytes into the
   core set and do not retry by resolving latest.
6. Confirm unregistered `catalog_logical_datasets` still have `current_uri IS NULL` and state
   `unregistered`; a same path/name must not silently acquire the old opaque identity.

The automated drill emits `BACKUP_RESTORE_REVISION_EVIDENCE` on success. A failure emits
`BACKUP_RESTORE_REVISION_MISMATCH` JSON entries with a `subject`, `expected`, and `actual` value so
operators can distinguish a missing DB row, identity drift, a missing/corrupt core artifact, and an
unexpected provider result.

## Execution manifest recovery verification

Treat `execution_manifests` and its surviving owners as one database consistency unit. The canonical
document is content-addressed metadata, not provider bytes and not a second backup format. The drill
retains one ordinary write manifest through its admission, run state, history, managed-file receipt,
and lineage fact, then edits the live Canvas before backup to prove restore never substitutes it. It
also retains one distinct manifest for terminal managed-local, terminal linear
checkpoint, terminal bounded fan-out, and nonterminal external-wait Tasks through their Task/Attempt,
receipt, Inbox, and Jobs projections where those owners apply.

After loading the database, but before recovery or normal traffic:

1. For every surviving owner, read its original `execution_manifest_sha256`; do not rebuild it from
   the live Canvas, current plugin descriptors, a source head, or the external provider request.
2. Resolve that digest through `metadb.execution_manifest()`. This validates canonical JSON, schema
   version, secret-free bounds, and the document digest before returning the document.
3. Compare the returned document with the exact document recorded at the backup recovery point.
   Matching a digest-shaped string or making all projections agree is insufficient if the document
   row is absent or corrupt.
4. Pruning is valid only when the existing retention lifecycle removed every owner. A missing owner
   that was expected to survive, a surviving owner with no document, or a digest/document mismatch
   rejects the restore. Do not drop the remaining owner or substitute mutable current state to make
   the clone start.

The automated drill emits `BACKUP_RESTORE_EXECUTION_MANIFEST_EVIDENCE` with the ordinary and four
Task digests plus the verified owner count. Failure emits
`BACKUP_RESTORE_EXECUTION_MANIFEST_MISMATCH` entries with `subject`, `code`, `expected`, and `actual`.
The stable codes distinguish `execution_manifest_owner_pruned`,
`execution_manifest_document_missing`, `execution_manifest_document_corrupt`, and reference or exact
document mismatch.

## Durable task recovery verification

Treat each durable task as a whole recovery unit, not as a task status row that can be restored on
its own. Of the nine shipped durable task families, the drill currently exercises four:
`managed_local_write`, `linear_checkpoint_write`, `bounded_fanout_write`, and `external_wait`.

- A terminal `managed_local_write` task retains its frozen logical graph, exact write receipt and
  publication identity, ordinary local input manifest, `local_file_input_revisions` mapping,
  task-owned `local_result_references` row, and the hashed snapshot artifact. Private execution-only
  artifact bindings must not appear in `durable_tasks.graph_doc`.
- A terminal `linear_checkpoint_write` task retains its attempt, committed `durable_checkpoints`
  evidence, exact candidate generation, ready artifact, hash, inode evidence, and sole
  `durable_checkpoint` owner.
- A terminal `bounded_fanout_write` task retains the same committed parent checkpoint plus the
  immutable plan, complete child/gather unit and attempt set, released four-slot pool, exact result
  hashes, and one `bounded_fanout_child` or `bounded_fanout_gather` owner for each done unit.
- A nonterminal `external_wait` task retains its attempt, provider-neutral handle and monotonic
  checkpoint in `durable_external_waits`. Before provider success it must have no write receipt,
  Inbox item, download evidence, staged artifact identity, or publication.

The drill does not yet certify the same backup/restore fixture matrix for `merge_columns_write`,
`distribution_report`, `restore_revision_write`, `keyed_upsert_write`, or
`keyed_upsert_write`. That acknowledged coverage gap does not change what the metadata backup
must retain; extending the drill is outside this runbook.

After loading the database and artifact set, but before enabling normal traffic:

1. Read the restored `version.json`, compare its `version`, `sha`, database, and storage fields with the
   selected restore profile, and compare its Alembic revision with
   `metadb.require_schema_at_head()`. For the object-store profile, also compare its namespace with
   the restored `installation_identity` before isolation. The presence of the file alone is not a
   release/schema check.
2. Run `metadb.linear_checkpoint_restore_audit()` and `bounded_fanout.restore_audit()`. A failed
   audit rejects the restore; report it as structured mismatch evidence rather than dropping an
   owner, checkpoint, unit, or artifact to make the restore start.
3. Verify every terminal task and attempt has the original status, receipt identity, artifact hash,
   owner references, and exactly one Inbox item. Preserve both read and unread Inbox state. A
   nonterminal external wait has no Inbox item.
4. Start recovery once with the external provider adapter unavailable. The task must remain
   recoverable with its original handle/checkpoint and an `adapter_unavailable` diagnostic; it must
   not be resubmitted or fabricated as successful.
5. Restore the adapter and restart recovery. It must poll the existing handle from the retained
   checkpoint. Concurrent supervisors converge through the restored DB lease, so only one polls;
   `submit` remains uncalled, and late task, fan-out, or external-wait owners remain fenced.
6. Confirm recovery did not add a second publication or leave a fan-out slot held. New work uses
   fresh DB-clock leases; copied lease timestamps never authorize a stale owner.

The automated drill emits `BACKUP_RESTORE_DURABLE_TASK_EVIDENCE` on success. A consistency failure
emits `BACKUP_RESTORE_DURABLE_TASK_MISMATCH` entries with `subject`, `expected`, and `actual` fields;
release/schema failures use `BACKUP_RESTORE_IDENTITY_MISMATCH` with the same shape.

## Automated restore drill

The repository owns an automated drill in `kernel/hub/tests/test_backup_restore_drill.py`.

```bash
# SQLite + local files (also runs in the kernel-tests CI job):
cd kernel && uv run pytest -q hub/tests/test_backup_restore_drill.py -k sqlite

# PostgreSQL + object-store isolation variant (CI job / local harness):
docker compose up -d postgres
# Optional S3 service: start the SeaweedFS profile documented in docs/RAY.md.
export DP_TEST_DATABASE_URL=postgresql+psycopg://dp:dp@127.0.0.1:5432/dataplay_test
cd kernel && uv run pytest -q hub/tests/test_backup_restore_drill.py -k postgres
```

### How to read RPO / RTO evidence

Each passing drill prints five structured lines (also captured in CI logs):

```
BACKUP_RESTORE_REVISION_EVIDENCE: <JSON exact-read and identity summary>
BACKUP_RESTORE_EXECUTION_MANIFEST_EVIDENCE: <JSON exact documents and owner summary>
BACKUP_RESTORE_DURABLE_TASK_EVIDENCE: <JSON task identities and reattach summary>
BACKUP_RESTORE_DRILL RPO: <human summary of which fixture writes the backup captured>
BACKUP_RESTORE_DRILL RTO_MS: <integer milliseconds from restore start to verified restore>
```

- **RPO (recovery point)** — which durable writes the fixture backup is known to contain
  (canvas id, catalog URIs, run/admission id, lineage fact and publication receipt, artifact URI,
  managed dataset/revision identity, unregistered tombstone, Alembic head, release sha).
  Anything written *after* that freeze is outside the recovery point by definition.
- **RTO (recovery time)** — wall-clock duration from the moment restore begins (copy/load of
  the backup set into the clone) through isolation and the verification assertions. It is a
  measured drill duration on the runner, not a product SLA.

## Security warning

Metadata backups contain credential identifiers, connection metadata, and SecretRefs. They should
still be treated as operationally sensitive: protect backup media with access control and encryption,
and do not commit dumps to git. Resolved secret values live in the referenced environment, file, or
external resolver and require their own backup and rotation procedures.
