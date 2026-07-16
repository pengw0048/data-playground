# Backup and restore

This runbook defines what a Data Playground backup must capture, how to take and restore
it for each supported storage profile, and how the repository proves that an isolated
restore works. Disaster-recovery takeover of an existing object-store namespace is **not**
implemented and must not be inferred from this procedure — see [RAY.md](RAY.md).

## What to back up

Every profile that stores durable work must capture the same **logical** set. The physical
steps below differ by profile.

| Item | Why it matters |
|---|---|
| **Metadata database** | Canvases (`canvases.doc` JSON), canvas version snapshots, run history and run state, catalog entries, columns, lineage facts, publication receipts, embeddings, settings and Cred rows, the local-result artifact registry, managed object-attempt lifecycle rows, and the `installation_identity` singleton (owner token + storage namespace). Publication receipts are required to preserve exact-replay tombstones after facts or catalog entries are unregistered. |
| **Workspace files** | Under `DP_WORKSPACE`: `dataplay.db` when using SQLite, `outputs/` (run results and local-result artifacts), and `plugins/` (operator-installed packs). |
| **Object-store generations + namespace marker** | When `DP_STORAGE_URL` points at `s3://` / `gs://` (or compatible), back up object generations under the installation's storage namespace **and** the conditional marker at `_dp_control/namespaces/<namespace>.json`. The metadata DB alone is not enough: `local_result_artifacts` and attempt rows reference exact URIs. |
| **Credential references** | Cred rows and plugin `secret` settings contain SecretRefs plus non-secret connection metadata, not resolved credential values. Back up the referenced environment, files, or external secret manager separately; a metadata restore cannot recreate them. |
| **Release identity** | Record `GET /api/version` (`sha`, `db` dialect, `storage` scheme) and the Alembic revision stored in the metadata DB (`alembic_version.version_num`, also exposed by `metadb.expected_schema_head()` / `metadb.require_schema_at_head()`). A restore must land on a release that understands that schema. |

### Consistency ordering

1. **Stop every metadata writer** before the snapshot window begins: hub replicas, per-canvas kernels, MCP servers, headless runs, and any external worker using the same database or writing into the same object-store namespace. Wait until they have exited; scaling a Deployment is asynchronous.
2. Snapshot the **metadata database** and the **artifact / object store** as close together as possible. Prefer: freeze writers → dump DB → copy object generations and the namespace marker → copy local workspace files that are not already in the DB dump.
3. Resume writers only after the backup set is complete and verified (checksums or byte sizes recorded).

A backup taken while writers are still active can leave dangling artifact URIs or a metadata
revision that does not match the files on disk.

## Profile A — SQLite + local files

Runnable on a laptop with no extra infrastructure.

### Backup

Assume the hub was started with `DP_WORKSPACE=/data` (default: the kernel package root) and
no `DP_DATABASE_URL` / `DP_STORAGE_URL` overrides.

```bash
# 1. Stop every process writing this workspace (hub, kernels, MCP, headless).
# 2. Record release identity while the old process is still readable, or from the frozen tree:
curl -sS http://127.0.0.1:8471/api/version | tee backup/version.json
# After stop:
mkdir -p backup
cp -a "$DP_WORKSPACE/dataplay.db" backup/dataplay.db
cp -a "$DP_WORKSPACE/outputs" backup/outputs
cp -a "$DP_WORKSPACE/plugins" backup/plugins 2>/dev/null || true
# Alembic revision is inside dataplay.db (table alembic_version). Keep version.json with the set.
```

### Restore (isolated clone — default)

Restore into a **fresh** workspace. Never point the clone at the live workspace path while the
source installation is still running.

```bash
RESTORE=/tmp/dp-restore-$$
mkdir -p "$RESTORE"
cp -a backup/dataplay.db "$RESTORE/dataplay.db"
cp -a backup/outputs "$RESTORE/outputs"
cp -a backup/plugins "$RESTORE/plugins" 2>/dev/null || true

# Assign a fresh storage namespace and isolate BEFORE any provider / object-attempt access.
export DP_WORKSPACE="$RESTORE"
export DP_DATABASE_URL="sqlite:///$RESTORE/dataplay.db"
# Read the source namespace from the restored DB, then isolate:
python - <<'PY'
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
rows, run history, lineage among non-attempt URIs, and local-result artifact registry rows remain
readable. The source installation's files and DB are untouched because the clone never opened them.

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
# 1. Stop every metadata / object-store writer for this installation.
# 2. Record release identity:
curl -sS http://127.0.0.1:8471/api/version | tee backup/version.json

# 3. Dump Postgres (custom format keeps restore flexible):
pg_dump --format=custom --no-owner --no-acl \
  "$DP_DATABASE_URL_LIBPQ" -f backup/dataplay.dump
# Example libpq URL for the compose harness:
#   postgresql://dp:dp@127.0.0.1:5432/dataplay

# 4. Copy object generations for the installation namespace AND the marker.
#    Marker path: s3://<bucket>/_dp_control/namespaces/<namespace>.json
#    With the MinIO harness (endpoint from DP_S3_ENDPOINT):
mc alias set dp "$DP_S3_ENDPOINT" "$DP_S3_KEY" "$DP_S3_SECRET"
NS="$(python -c 'from hub import metadb; metadb.init_db(); print(metadb.object_storage_namespace())')"
mc cp --recursive "dp/${DP_S3_BUCKET}/" "backup/objects/"
# Prefer filtering to the namespace prefix + _dp_control/namespaces/${NS}.json when the bucket is shared.

# 5. Workspace plugins (optional but recommended):
cp -a "$DP_WORKSPACE/plugins" backup/plugins 2>/dev/null || true
```

Consistency ordering for this profile: stop writers → `pg_dump` → object-store copy (generations
+ `_dp_control/namespaces/<namespace>.json`) → workspace plugins. Reversing DB and object copy
while writers are stopped is acceptable only if both finish before any writer resumes; do not
interleave either with live writes.

### Restore (isolated clone — default)

```bash
# Fresh database and fresh object-store prefix/namespace — never the live ones.
createdb -U dp dataplay_restore   # or restore into an empty database owned by the drill
pg_restore --clean --if-exists --no-owner --no-acl -d "$RESTORE_DATABASE_URL_LIBPQ" backup/dataplay.dump

# Copy objects into a scratch prefix if you need file presence for local checks; the clone must
# NOT claim the source namespace marker. Isolation creates a new claim under the replacement namespace.

export DP_DATABASE_URL="postgresql+psycopg://..."   # the restore DB only
export DP_STORAGE_URL="s3://..."                    # bucket/prefix the clone may use
unset DP_STORAGE_NAMESPACE                          # until after isolation
python - <<'PY'
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

## What restore is not

- **Not disaster-recovery takeover.** `isolate_cloned_object_storage` rotates owner and
  namespace, quarantines inherited attempts, and clears copied marker claims. It does not
  acquire the original `_dp_control/namespaces/<old>.json` marker. Audited takeover of an
  existing namespace is unimplemented; changing `DP_STORAGE_NAMESPACE` alone is rejected
  ([RAY.md](RAY.md)).
- **Not a secret-backend backup.** The metadata database restores Cred and plugin SecretRefs,
  but it does not restore referenced environment variables, mounted secret files, or an external
  secret manager. Restore those through the deployment system before exercising the credentials.

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

Each passing drill prints two structured lines (also captured in CI logs):

```
BACKUP_RESTORE_DRILL RPO: <human summary of which fixture writes the backup captured>
BACKUP_RESTORE_DRILL RTO_MS: <integer milliseconds from restore start to verified restore>
```

- **RPO (recovery point)** — which durable writes the fixture backup is known to contain
  (canvas id, catalog URIs, run id, lineage fact and publication receipt, artifact URI, Alembic head,
  release sha).
  Anything written *after* that freeze is outside the recovery point by definition.
- **RTO (recovery time)** — wall-clock duration from the moment restore begins (copy/load of
  the backup set into the clone) through isolation and the verification assertions. It is a
  measured drill duration on the runner, not a product SLA.

## Security warning

Metadata backups contain credential identifiers, connection metadata, and SecretRefs. They should
still be treated as operationally sensitive: protect backup media with access control and encryption,
and do not commit dumps to git. Resolved secret values live in the referenced environment, file, or
external resolver and require their own backup and rotation procedures.
