# Upgrade Data Playground in place

This runbook covers a stopped, in-place upgrade from the published `v0.1.0` release to
`v0.2.0`. It applies to the supported local SQLite workspace and trusted-team PostgreSQL
metadata profiles. It does not support a live upgrade or a database downgrade.

The automated release drill performs these same steps with the published v0.1.0 wheel and
the exact candidate wheel. It retains a bounded evidence document for both metadata backends.

## 1. Stop and identify the source

Block new requests, stop every hub, MCP, CLI run, worker, and scheduler process that can use
the workspace, and wait for them to exit. Do not take an in-place backup while any writer is
running.

Before stopping the final hub, record its public identity:

```bash
BACKUP=/secure/backups/data-playground-v0.1.0-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP"
curl -fsS http://127.0.0.1:8471/api/version | tee "$BACKUP/source-version.json"
```

Then record the metadata schema after all processes are stopped:

```bash
# SQLite
sqlite3 "$DP_WORKSPACE/dataplay.db" 'SELECT version_num FROM alembic_version;' \
  | tee "$BACKUP/source-schema.txt"

# PostgreSQL (use a libpq URL or normal PG* environment variables)
psql "$DP_DATABASE_URL_LIBPQ" -Atc 'SELECT version_num FROM alembic_version;' \
  | tee "$BACKUP/source-schema.txt"
```

For this upgrade, the source identity must be `0.1.0` at
`0038_inbox_dataset_scoped`. Investigate any different version or schema before proceeding.

## 2. Take one complete pre-upgrade backup

Name the backup for the source release and protect it as operationally sensitive. The backup
must keep metadata, managed data bytes, workspace configuration, plugin files, and credential
references together. Credential rows contain references such as `env:NAME` or `file:/path`, not
the referenced secret values; back up the secret provider separately.

For SQLite, copy the whole stopped workspace:

```bash
cp -a "$DP_WORKSPACE/." "$BACKUP/workspace/"
```

For PostgreSQL, dump metadata and copy the stopped workspace, including managed `outputs/`,
`data/`, `plugins/`, and local configuration:

```bash
pg_dump --format=custom --file "$BACKUP/metadata.dump" "$DP_DATABASE_URL_LIBPQ"
cp -a "$DP_WORKSPACE/." "$BACKUP/workspace/"
```

Record checksums for the backup set and keep the source version and schema records beside it.
Do not resume old processes after this point.

## 3. Install and migrate the candidate

Install the exact candidate artifact into a new environment. Keep the old release environment
available for full-backup rollback, but never run the two releases against the workspace at the
same time.

```bash
uv venv /opt/data-playground-v0.2.0
uv pip install --python /opt/data-playground-v0.2.0 /path/to/data_playground-0.2.0-py3-none-any.whl

# PostgreSQL only: install the release's supported driver into the same environment.
uv pip install --python /opt/data-playground-v0.2.0 'psycopg[binary]>=3.1.18,<4'

# SQLite
/opt/data-playground-v0.2.0/bin/dataplay migrate --workspace "$DP_WORKSPACE"

# PostgreSQL
DP_DATABASE_URL="$DP_DATABASE_URL" \
  /opt/data-playground-v0.2.0/bin/dataplay migrate --workspace "$DP_WORKSPACE"
```

`dataplay migrate` is a one-shot operation. It must finish successfully at
`0039_folder_replays` before any v0.2.0 service starts. Do not start the hub to perform an
implicit PostgreSQL migration.

## 4. Start and verify

Start v0.2.0 with the same workspace, metadata database, data directory, storage, and config.
Keep traffic blocked until all checks pass:

1. `GET /api/version` reports `0.2.0`, the expected candidate SHA, database dialect, and storage.
2. `alembic_version.version_num` is `0039_folder_replays`.
3. Catalog tables and a bounded sample of their contents open successfully.
4. Saved Canvas identities, documents, and version history are retained.
5. Managed revision identities and history are retained; exact old revisions reopen with the
   same content, including the revision restored as a new head before the upgrade.
6. Run history, Jobs, Inbox outcomes, Cred references, and plugin settings are retained.

Only unblock users after these checks succeed. Save the target `/api/version`, schema, and
verification output with the backup record.

## Failure and rollback

There is no supported downgrade. Never run an older `dataplay migrate`, edit Alembic state, or
start v0.1.0 against metadata already migrated by v0.2.0.

If migration or verification fails, stop every v0.2.0 process. Restore the **entire** pre-upgrade
set—SQLite workspace or PostgreSQL dump plus workspace managed bytes/config—and then start the
old v0.1.0 release against that restored set. A database-only or files-only restore is not a
rollback because metadata identities and managed revision bytes are one consistency unit.

For general backup handling, restore isolation, object-store profiles, and credential-reference
requirements, see [Backup and restore](BACKUP_RESTORE.md).
