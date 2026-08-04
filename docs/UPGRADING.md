# Upgrade Data Playground in place

This runbook covers a stopped, in-place upgrade from any published `0.2.x` release to
one exact candidate artifact. It applies to the supported local SQLite workspace and
trusted-team PostgreSQL metadata profiles. It does not support a live upgrade, a database
downgrade, or mixing two builds against the same workspace.

The release upgrade drill's oldest certified source fixture is the published `v0.1.0`
wheel. That fixture proves a complete historical upgrade, but it is not a separate fixture
for every source release. Published `0.2.0`, `0.2.2`, and `0.2.3` use the same linear
forward migration chain and are supported sources under this policy; `v0.2.1` was not
published. The published `0.2.x` artifacts are at `0039_folder_replays`.

The drill installs its exact candidate wheel, obtains the candidate's schema head from
that installed wheel, and compares the migrated database with it. Follow the same rule in
operations: the candidate is the authority for its target schema. Never transcribe a
development schema-head constant into this runbook or an upgrade procedure.

Deployments whose `DP_STORAGE_URL` uses object storage must also follow Profile B in
[Backup and restore](BACKUP_RESTORE.md).

## 1. Stop and identify the source

Block new requests and stop every writer except the final hub used to identify the source.
While that hub is still running, record its public identity:

```bash
BACKUP=/secure/backups/data-playground-0.2x-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP"
curl -fsS http://127.0.0.1:8471/api/version | tee "$BACKUP/source-version.json"
```

Now stop the final hub and confirm that every hub, MCP, CLI run, worker, and scheduler process
that can use the workspace has exited. Do not read the schema or take an in-place backup while
any writer is running. After all processes are stopped, record the metadata schema:

```bash
# SQLite
sqlite3 "$DP_WORKSPACE/dataplay.db" 'SELECT version_num FROM alembic_version;' \
  | tee "$BACKUP/source-schema.txt"

# PostgreSQL (use a libpq URL or normal PG* environment variables)
psql "$DP_DATABASE_URL_LIBPQ" -Atc 'SELECT version_num FROM alembic_version;' \
  | tee "$BACKUP/source-schema.txt"
```

The source `/api/version` must identify a published `0.2.x` artifact, and its schema must be
the schema shipped by that artifact. For the published `0.2.0`, `0.2.2`, and `0.2.3` artifacts,
that is `0039_folder_replays`. Investigate a different version or schema before proceeding.

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

If `DP_STORAGE_URL` uses object storage, a PostgreSQL dump and workspace copy are not complete.
Before upgrading, use [Backup and restore](BACKUP_RESTORE.md) Profile B to verify a
version-preserving replica, its object-generation manifest, and the installation namespace marker;
include all of them in the same consistency backup set.

## 3. Install, identify, and migrate the exact candidate

Install the exact candidate artifact into a new environment. Record its wheel checksum and the
full commit SHA supplied by the candidate build. Keep the old release environment available for
full-backup rollback, but never run the two releases against the workspace at the same time.

```bash
set -euo pipefail

# Set these to the exact candidate artifact and the full commit SHA recorded by its build.
CANDIDATE_WHEEL="/absolute/path/to/exact-candidate-wheel.whl"
CANDIDATE_SHA="replace-with-the-full-candidate-commit-sha"
CANDIDATE_VENV=/opt/data-playground-candidate

uv venv "$CANDIDATE_VENV"
uv pip install --python "$CANDIDATE_VENV" "$CANDIDATE_WHEEL"

# PostgreSQL only: install the candidate's supported driver into the same environment.
uv pip install --python "$CANDIDATE_VENV" 'psycopg[binary]>=3.1.18,<4'
```

Read the installed candidate's package version and expected schema head before migrating. The
probe verifies that both the module and migration files come from the candidate environment;
it must not import a checkout or another environment.

```bash
set -euo pipefail

CANDIDATE_PROBE_LOG="$BACKUP/candidate-probe.stderr.log"
: > "$CANDIDATE_PROBE_LOG"

CANDIDATE_VERSION="$(
  "$CANDIDATE_VENV/bin/python" -I -c '
import sys
from importlib.metadata import version
from pathlib import Path
from hub import metadb

venv = Path(sys.prefix).resolve()
assert Path(metadb.__file__).resolve().is_relative_to(venv)
assert Path(metadb._MIGRATIONS_DIR).resolve().is_relative_to(venv)
print(version("data-playground"))
' 2>>"$CANDIDATE_PROBE_LOG"
)" || {
  cat "$CANDIDATE_PROBE_LOG" >&2
  exit 1
}
printf '%s\n' "$CANDIDATE_VERSION" | tee "$BACKUP/candidate-version.txt"

CANDIDATE_SCHEMA="$(
  "$CANDIDATE_VENV/bin/python" -I -c '
import sys
from pathlib import Path
from hub import metadb

venv = Path(sys.prefix).resolve()
assert Path(metadb.__file__).resolve().is_relative_to(venv)
assert Path(metadb._MIGRATIONS_DIR).resolve().is_relative_to(venv)
print(metadb.expected_schema_head())
' 2>>"$CANDIDATE_PROBE_LOG"
)" || {
  cat "$CANDIDATE_PROBE_LOG" >&2
  exit 1
}
printf '%s\n' "$CANDIDATE_SCHEMA" | tee "$BACKUP/candidate-expected-schema.txt"
```

Probe diagnostics are retained separately in `candidate-probe.stderr.log`, so the two value files
remain single-value comparison inputs. A failed probe exits before either migration or startup.

The `v0.3.3` release uses the exact clean `0.3.3` package identity. Use the installed
candidate's recorded value, not a hand-written version string, for every verification below.

Run the one-shot migration with the candidate. Use the block for the deployment's metadata profile.

For SQLite:

```bash
set -euo pipefail

DP_GIT_SHA="$CANDIDATE_SHA" \
  "$CANDIDATE_VENV/bin/dataplay" migrate --workspace "$DP_WORKSPACE" \
  2>&1 | tee "$BACKUP/candidate-migrate.txt"
```

For PostgreSQL:

```bash
set -euo pipefail

DP_GIT_SHA="$CANDIDATE_SHA" DP_DATABASE_URL="$DP_DATABASE_URL" \
  "$CANDIDATE_VENV/bin/dataplay" migrate --workspace "$DP_WORKSPACE" \
  2>&1 | tee "$BACKUP/candidate-migrate.txt"
```

`dataplay migrate` is a one-shot operation. It must finish successfully before any candidate
service starts. Do not start the hub to perform an implicit PostgreSQL migration.

## 4. Start and verify

Start the candidate hub with the same workspace, metadata database, data directory, storage, and
config. Its environment must retain `DP_GIT_SHA="$CANDIDATE_SHA"`; an installed wheel has no checkout
from which `/api/version` can recover that commit identity. Keep traffic blocked until all checks pass:

```bash
set -euo pipefail

# Retain DP_DATABASE_URL and every other deployment setting used by the old hub.
DP_GIT_SHA="$CANDIDATE_SHA" \
  "$CANDIDATE_VENV/bin/dataplay" --workspace "$DP_WORKSPACE"
```

1. `GET /api/version` reports the version in `candidate-version.txt`, the recorded candidate SHA,
   database dialect, and storage.
2. `alembic_version.version_num` equals the sole value in `candidate-expected-schema.txt`.
3. Catalog tables and a bounded sample of their contents open successfully.
4. Saved Canvas identities, documents, and version history are retained.
5. Managed revision identities and history are retained; exact old revisions reopen with the
   same content, including the revision restored as a new head before the upgrade.
6. Run history, Jobs, Inbox outcomes, Cred references, and plugin settings are retained.

The migration retains historical run records, but it cannot reconstruct result artifacts that a
`0.2.x` workspace did not already own. Re-run each Canvas whose latest output should be available
after restart; the successful `0.3.3` run establishes that Canvas's retained current-result
projection and applies its configured result-history policy.

Only unblock users after these checks succeed. Save the target `/api/version`, schema, and
verification output with the backup record.

## Failure and rollback

There is no supported downgrade. Never run an older `dataplay migrate`, edit Alembic state, or
start the old `0.2.x` build against metadata already migrated by the candidate.

If migration or verification fails, stop every candidate process. Restore the **entire** pre-upgrade
set—SQLite workspace or PostgreSQL dump plus workspace managed bytes/config—and then start the
old `0.2.x` release against that restored set. A database-only or files-only restore is not a
rollback because metadata identities and managed revision bytes are one consistency unit.
For object storage, rollback is certified only when candidate verification remained read-only and
the original object store and namespace marker are intact. Keep traffic blocked and do not write.
After restoring the PostgreSQL metadata dump and workspace, continue using that unchanged original
store; never use the replica to reclaim its namespace. If the original object store itself needs
recovery, stop: Profile B provides backup evidence, not a supported disaster-recovery takeover.

For general backup handling, restore isolation, object-store profiles, and credential-reference
requirements, see [Backup and restore](BACKUP_RESTORE.md).
