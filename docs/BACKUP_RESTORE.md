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
| **Metadata database** | Canvases (`canvases.doc` JSON), canvas version snapshots, run history and run state, catalog entries, columns, lineage facts, publication receipts, embeddings, settings and Cred rows, the local-result artifact registry, managed object-attempt lifecycle rows, and the `installation_identity` singleton (owner token + storage namespace). For versioned data this explicitly includes `catalog_logical_datasets` (including unregistered tombstones), `managed_local_file_revisions`, `run_input_admissions`, `run_records.input_manifest`, `local_result_artifacts`, `local_result_references`, and the object-attempt ref / lease / inventory tables. Exact execution history additionally requires `execution_manifests` and every `execution_manifest_sha256` owner on admissions, run state/history, receipts/revisions, lineage, durable Tasks/Attempts, and Inbox items. Durable task recovery also requires `durable_tasks`, `durable_task_attempts`, `durable_task_inbox_items`, `durable_external_waits`, `durable_checkpoints`, and the bounded fan-out plan, unit, attempt, and slot tables. These rows are one consistency unit: never reconstruct identity or references from a path or display name after restore. Publication receipts are required to preserve exact-replay tombstones after facts or catalog entries are unregistered. |
| **Workspace files** | Under `DP_WORKSPACE`: `dataplay.db` when using SQLite, `outputs/` (run results plus immutable core-owned revision artifacts under `.dp-results/`), and `plugins/` (operator-installed packs). Preserve the complete `.dp-results` namespace metadata and record hashes for every core-owned artifact referenced by `managed_local_file_revisions`; copying only current catalog heads loses retained history. |
| **Object-store generations + namespace marker** | When `DP_STORAGE_URL` points at `s3://` / `gs://` (or compatible), retain object generations under the installation's storage namespace **and** the conditional marker at `_dp_control/namespaces/<namespace>.json`. The metadata DB alone is not enough: `local_result_artifacts` and attempt rows reference exact URIs. For MinIO, this means a version-preserving replica configured before protected writes, not `mc cp --recursive`; see Profile B. |
| **Provider-owned history evidence (not provider bytes)** | The database retains opaque registration / dataset IDs, exact provider revision IDs, pinned Source refs, and admitted run manifests. The logical backup does **not** include provider-owned Lance or plugin-provider bytes. Back those up through the provider's own system if required, and classify each restored exact read as available or unavailable instead of treating a current same-name/path dataset as the old revision. |
| **Credential references** | Cred rows and plugin `secret` settings contain SecretRefs plus non-secret connection metadata, not resolved credential values. Back up the referenced environment, files, or external secret manager separately; a metadata restore cannot recreate them. |
| **Release identity** | Record `GET /api/version` (`sha`, `db` dialect, `storage` scheme) and the Alembic revision stored in the metadata DB (`alembic_version.version_num`, also exposed by `metadb.expected_schema_head()` / `metadb.require_schema_at_head()`). A restore must land on a release that understands that schema. |

### Consistency ordering

1. **Stop every metadata writer** before the snapshot window begins: hub replicas, per-canvas kernels, MCP servers, headless runs, and any external worker using the same database or writing into the same object-store namespace. Wait until they have exited; scaling a Deployment is asynchronous.
2. Snapshot the **metadata database** and the **artifact / object store** as close together as possible. Prefer: freeze writers → dump DB → verify the version-preserving object replica and namespace marker → copy local workspace files that are not already in the DB dump.
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

Runnable against the repository harnesses:

```bash
docker compose up -d postgres
docker compose -f docker-compose.ray.yml up -d minio createbucket
```

### Backup

```bash
# 1. Record release identity while the old process is still readable:
mkdir -p backup
curl -sS http://127.0.0.1:8471/api/version | tee backup/version.json

# 2. Stop every metadata / object-store writer for this installation.
# 3. Dump Postgres (custom format keeps restore flexible):
pg_dump --format=custom --no-owner --no-acl \
  "$DP_DATABASE_URL_LIBPQ" -f backup/dataplay.dump
uv run --project kernel python - <<'PY'
import json
from pathlib import Path
from hub import metadb
path = Path("backup/version.json")
doc = json.loads(path.read_text())
doc["alembic"] = metadb.require_schema_at_head()
doc["namespace"] = metadb.object_storage_namespace()
path.write_text(json.dumps(doc, sort_keys=True) + "\n")
PY
# Example libpq URL for the compose harness:
#   postgresql://dp:dp@127.0.0.1:5432/dataplay

# 4. Record the already-configured version-preserving replica's exact manifest.
#    The replica must have been created before this installation wrote protected objects.
mc alias set dp "$DP_S3_ENDPOINT" "$DP_S3_KEY" "$DP_S3_SECRET"
mc alias set dp-backup "$DP_OBJECT_BACKUP_ENDPOINT" \
  "$DP_OBJECT_BACKUP_KEY" "$DP_OBJECT_BACKUP_SECRET"
mc ls --json --versions --recursive "dp/${DP_S3_BUCKET}" \
  | jq -S -c '{key, versionId, isDeleteMarker: (.isDeleteMarker // false), size, etag}' \
  | sort \
  > backup/object-versions.primary.jsonl
mc ls --json --versions --recursive "dp-backup/${DP_OBJECT_BACKUP_BUCKET}" \
  | jq -S -c '{key, versionId, isDeleteMarker: (.isDeleteMarker // false), size, etag}' \
  | sort \
  > backup/object-versions.replica.jsonl
cmp backup/object-versions.primary.jsonl backup/object-versions.replica.jsonl

# 5. Workspace plugins (optional but recommended):
cp -a "$DP_WORKSPACE/plugins" backup/plugins 2>/dev/null || true
# If this deployment also has built-in local outputs, copy and fingerprint the complete tree exactly
# as in Profile A; PostgreSQL does not move local revision bytes into the object store.
```

`mc cp --recursive` is intentionally absent: MinIO documents that it copies only the latest or a
specified version, without version information. The old command therefore loses non-current
generations and delete markers, and writes new version IDs on a target bucket. A matching current
object is not an exact-generation restore.

### Configure the object replica before protected writes

The documented object-history mechanism is MinIO bucket replication to an independent, versioned
bucket. Configure it **before** the first Data Playground object write for the installation. The
replica is a warm backup: it is not a one-time `mc cp` archive and it must be in a distinct failure
domain from the source object store.

```bash
# Run while the source bucket is new/empty, before protected writes.
mc alias set dp "$DP_S3_ENDPOINT" "$DP_S3_KEY" "$DP_S3_SECRET"
mc alias set dp-backup "$DP_OBJECT_BACKUP_ENDPOINT" \
  "$DP_OBJECT_BACKUP_KEY" "$DP_OBJECT_BACKUP_SECRET"
mc mb --ignore-existing "dp-backup/${DP_OBJECT_BACKUP_BUCKET}"
mc version enable "dp/${DP_S3_BUCKET}"
mc version enable "dp-backup/${DP_OBJECT_BACKUP_BUCKET}"
mc replicate add "dp/${DP_S3_BUCKET}" \
  --remote-bucket "dp-backup/${DP_OBJECT_BACKUP_BUCKET}" \
  --replicate 'existing-objects,delete,delete-marker' --priority 1 --sync
```

Do not treat `existing-objects` as a retrofit guarantee. This runbook certifies the procedure only
when the replication rule is in place before the objects it protects are written. For an existing
bucket, establish a provider-supported migration with its own evidence before relying on the
replica.

Consistency ordering for this profile: configure and continuously verify the replica → stop writers
→ `pg_dump` → compare primary and replica version manifests (including the namespace marker) →
workspace plugins. Resume writers only after the manifests match. The two manifest files are
operator evidence; the replica holds the versioned bytes.

The repository's executable drill uses the same digest-pinned MinIO and `mc` images as the Ray
compose harness. It writes two versions of one key and a delete marker, proves a specific
non-current version is readable from the replica, and proves that the former `mc cp` sequence loses
history:

```bash
bash scripts/verify_versioned_object_backup.sh
```

It prints `VERSIONED_OBJECT_BACKUP_EVIDENCE` only after the exact version manifest matches, and
`VERSIONED_OBJECT_BACKUP_CP_CONTROL=lost_history` only when the negative control has lost the
historical version and delete-marker entries. Run it as release/operator evidence; it intentionally
does not run in the ordinary CI matrix because it starts three disposable MinIO servers.

MinIO's [mc cp reference](https://docs.min.io/community/minio-object-store/reference/minio-mc/mc-cp.html)
documents the current-version limitation. Its [bucket replication reference](https://docs.min.io/community/minio-object-store/administration/bucket-replication.html)
is the supported version/history mechanism. Use versions of MinIO and `mc` compatible with the
release you operate; the drill's pinned versions are listed in its output.

### Restore (isolated clone — default)

```bash
# Fresh database and fresh object-store prefix/namespace — never the live ones.
RESTORE=/tmp/dp-restore-$$
mkdir -p "$RESTORE"
createdb -U dp dataplay_restore   # or restore into an empty database owned by the drill
pg_restore --clean --if-exists --no-owner --no-acl -d "$RESTORE_DATABASE_URL_LIBPQ" backup/dataplay.dump
cp -a backup/version.json "$RESTORE/version.json"

# Do not restore `backup/objects` with mc cp: that archive cannot preserve exact version IDs.
# The exact generations remain on the already-verified replica. This isolated clone still must
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
installation remains unsupported. Use `mc cp --version-id <id>` against the replica only for
object-level verification or provider-level recovery until an audited takeover workflow exists.

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
its own. The drill keeps one bounded fixture for each currently certified kind:

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
# optional MinIO: docker compose -f docker-compose.ray.yml up -d minio createbucket
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
