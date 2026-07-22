#!/usr/bin/env bash
# Verify the only object-history preservation mechanism documented in
# docs/BACKUP_RESTORE.md. This is an operator/release drill, not a CI job.
set -euo pipefail

MINIO_IMAGE="minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e"
MC_IMAGE="minio/mc:RELEASE.2025-04-16T18-13-26Z@sha256:aead63c77f9db9107f1696fb08ecb0faeda23729cde94b0f663edf4fe09728e3"
RUN_ID="dp-object-backup-${RANDOM}-${RANDOM}"
NETWORK="${RUN_ID}-network"
SOURCE="${RUN_ID}-source"
REPLICA="${RUN_ID}-replica"
COPY_TARGET="${RUN_ID}-copy-target"

cleanup() {
  docker rm -f "$SOURCE" "$REPLICA" "$COPY_TARGET" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$NETWORK" >/dev/null
for container in "$SOURCE" "$REPLICA" "$COPY_TARGET"; do
  docker run -d --name "$container" --network "$NETWORK" \
    -e MINIO_ROOT_USER=dp-backup-drill \
    -e MINIO_ROOT_PASSWORD=dp-backup-drill-password \
    "$MINIO_IMAGE" server /data >/dev/null
done

for _ in $(seq 1 30); do
  if docker run --rm --network "$NETWORK" "$MC_IMAGE" \
    alias set source "http://${SOURCE}:9000" dp-backup-drill dp-backup-drill-password \
    >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker run --rm --network "$NETWORK" --entrypoint /bin/sh "$MC_IMAGE" -ceu "
  mc alias set source http://${SOURCE}:9000 dp-backup-drill dp-backup-drill-password
  mc alias set replica http://${REPLICA}:9000 dp-backup-drill dp-backup-drill-password
  mc alias set copy http://${COPY_TARGET}:9000 dp-backup-drill dp-backup-drill-password

  mc mb source/production
  mc mb replica/backup
  mc version enable source/production
  mc version enable replica/backup

  # Replication must exist before the protected writes. The control below proves
  # why a later mc cp backup cannot replace it.
  mc replicate add source/production \\
    --remote-bucket http://dp-backup-drill:dp-backup-drill-password@${REPLICA}:9000/backup \\
    --replicate 'existing-objects,delete,delete-marker' --priority 1 --sync

  printf 'first-generation\\n' >/tmp/first.txt
  printf 'second-generation\\n' >/tmp/second.txt
  printf 'deleted-generation\\n' >/tmp/deleted.txt
  mc cp /tmp/first.txt source/production/history/object.txt
  mc cp /tmp/second.txt source/production/history/object.txt
  mc cp /tmp/deleted.txt source/production/tombstone/object.txt
  mc rm source/production/tombstone/object.txt

  # Strip the endpoint-only URL field before comparing exact object facts.
  # The pinned mc image intentionally has no general-purpose text processor;
  # url is the seventh comma-delimited JSON field emitted by this mc release.
  mc ls --json --versions --recursive source/production \\
    | cut -d, -f1-6,8- >/tmp/source-versions.jsonl
  mc ls --json --versions --recursive replica/backup \\
    | cut -d, -f1-6,8- >/tmp/replica-versions.jsonl
  test \"\$(sha256sum /tmp/source-versions.jsonl | cut -d' ' -f1)\" = \\
    \"\$(sha256sum /tmp/replica-versions.jsonl | cut -d' ' -f1)\"

  historical_version=\$(head -n 2 /tmp/source-versions.jsonl | tail -n 1 | cut -d, -f7 | cut -d'\"' -f4)
  test -n \"\$historical_version\"
  mc cp --version-id \"\$historical_version\" replica/backup/history/object.txt /tmp/recovered-historical.txt
  test \"\$(sha256sum /tmp/first.txt | cut -d' ' -f1)\" = \\
    \"\$(sha256sum /tmp/recovered-historical.txt | cut -d' ' -f1)\"

  # Negative control: the former runbook command copies only the current object.
  mkdir -p /tmp/cp-backup
  mc cp --recursive source/production/ /tmp/cp-backup/
  mc mb copy/restore
  mc version enable copy/restore
  mc cp --recursive /tmp/cp-backup/ copy/restore/
  mc ls --json --versions --recursive copy/restore \\
    | cut -d, -f1-6,8- >/tmp/cp-versions.jsonl
  case \"\$(cat /tmp/cp-versions.jsonl)\" in
    *\"\\\"versionId\\\":\\\"\$historical_version\\\"\"*)
    echo 'mc cp unexpectedly retained a historical version ID' >&2
    exit 1
    ;;
  esac
  if [ \"\$(wc -l </tmp/cp-versions.jsonl)\" -ge \"\$(wc -l </tmp/source-versions.jsonl)\" ]; then
    echo 'mc cp control did not lose version or delete-marker history' >&2
    exit 1
  fi

  echo \"VERSIONED_OBJECT_BACKUP_EVIDENCE source=source/production replica=replica/backup historical_version=\$historical_version\"
  echo \"VERSIONED_OBJECT_BACKUP_CP_CONTROL=lost_history\"
  mc --version
"
