#!/usr/bin/env bash
# Verify the version-history preservation mechanism documented in
# docs/BACKUP_RESTORE.md. This is an operator/release drill, not a CI job.
set -euo pipefail

MINIO_IMAGE="minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e"
MC_IMAGE="minio/mc:RELEASE.2025-04-16T18-13-26Z@sha256:aead63c77f9db9107f1696fb08ecb0faeda23729cde94b0f663edf4fe09728e3"
RUN_ID="dp-object-backup-${RANDOM}-${RANDOM}"
NETWORK="${RUN_ID}-network"
SOURCE="${RUN_ID}-source"
REPLICA="${RUN_ID}-replica"
COPY_TARGET="${RUN_ID}-copy-target"
CLIENT="${RUN_ID}-mc"
WORK_DIR="$(mktemp -d)"

cleanup() {
  docker rm -f "$SOURCE" "$REPLICA" "$COPY_TARGET" "$CLIENT" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

docker network create "$NETWORK" >/dev/null
for container in "$SOURCE" "$REPLICA" "$COPY_TARGET"; do
  docker run -d --name "$container" --network "$NETWORK" \
    -e MINIO_ROOT_USER=dp-backup-drill \
    -e MINIO_ROOT_PASSWORD=dp-backup-drill-password \
    "$MINIO_IMAGE" server /data >/dev/null
done
docker run -d --name "$CLIENT" --network "$NETWORK" \
  --entrypoint /bin/sh "$MC_IMAGE" -c 'while :; do sleep 3600; done' >/dev/null

mc() {
  docker exec \
    -e DP_DRILL_SOURCE="$SOURCE" \
    -e DP_DRILL_REPLICA="$REPLICA" \
    -e DP_DRILL_COPY_TARGET="$COPY_TARGET" \
    "$CLIENT" /bin/sh -ceu '
    mc alias set source "http://${DP_DRILL_SOURCE}:9000" dp-backup-drill dp-backup-drill-password >/dev/null
    mc alias set replica "http://${DP_DRILL_REPLICA}:9000" dp-backup-drill dp-backup-drill-password >/dev/null
    mc alias set copy "http://${DP_DRILL_COPY_TARGET}:9000" dp-backup-drill dp-backup-drill-password >/dev/null
    mc "$@"
  ' -- "$@"
}

normalize_manifest() {
  python3 -c '
import json
import sys

entries = []
for line in sys.stdin:
    item = json.loads(line)
    entries.append(
        {
            "etag": item.get("etag", ""),
            "isDeleteMarker": item.get("isDeleteMarker", False),
            "key": item["key"],
            "size": item.get("size", 0),
            "versionId": item["versionId"],
        }
    )
for item in sorted(
    entries,
    key=lambda item: (
        item["key"],
        item["versionId"],
        item["isDeleteMarker"],
        item["size"],
        item["etag"],
    ),
):
    print(json.dumps(item, sort_keys=True, separators=(",", ":")))
'
}

for alias in source replica copy; do
  ready=false
  for _ in $(seq 1 30); do
    if mc ls "$alias" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done
  if [ "$ready" != true ]; then
    echo "MinIO service $alias did not become ready" >&2
    exit 1
  fi
done

mc mb source/production
mc mb replica/backup
mc version enable source/production
mc version enable replica/backup

# Replication must exist before the protected writes. The control below proves
# why a later copy cannot replace it.
mc replicate add source/production \
  --remote-bucket "http://dp-backup-drill:dp-backup-drill-password@${REPLICA}:9000/backup" \
  --replicate 'existing-objects,delete,delete-marker' --priority 1 --sync

printf 'first-generation\n' >"$WORK_DIR/first.txt"
printf 'second-generation\n' >"$WORK_DIR/second.txt"
printf 'deleted-generation\n' >"$WORK_DIR/deleted.txt"
docker cp "$WORK_DIR/first.txt" "$CLIENT:/tmp/first.txt"
docker cp "$WORK_DIR/second.txt" "$CLIENT:/tmp/second.txt"
docker cp "$WORK_DIR/deleted.txt" "$CLIENT:/tmp/deleted.txt"
mc cp /tmp/first.txt source/production/history/object.txt
mc cp /tmp/second.txt source/production/history/object.txt
mc cp /tmp/deleted.txt source/production/tombstone/object.txt
mc rm source/production/tombstone/object.txt

mc ls --json --versions --recursive source/production \
  | normalize_manifest >"$WORK_DIR/source-versions.jsonl"
mc ls --json --versions --recursive replica/backup \
  | normalize_manifest >"$WORK_DIR/replica-versions.jsonl"
cmp "$WORK_DIR/source-versions.jsonl" "$WORK_DIR/replica-versions.jsonl"
test "$(wc -l <"$WORK_DIR/source-versions.jsonl" | tr -d '[:space:]')" = 4

historical_version="$(python3 - "$WORK_DIR/source-versions.jsonl" <<'PY'
import json
import sys

entries = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
versions = [
    item["versionId"]
    for item in entries
    if item["key"] == "history/object.txt"
    and item["size"] == len(b"first-generation\n")
    and not item["isDeleteMarker"]
]
if len(versions) != 1:
    raise SystemExit(f"expected one first historical generation, found {versions!r}")
if sum(item["isDeleteMarker"] for item in entries) != 1:
    raise SystemExit("expected exactly one delete marker")
print(versions[0])
PY
)"
mc cp --version-id "$historical_version" replica/backup/history/object.txt /tmp/recovered-historical.txt
docker cp "$CLIENT:/tmp/recovered-historical.txt" "$WORK_DIR/recovered-historical.txt"
cmp "$WORK_DIR/first.txt" "$WORK_DIR/recovered-historical.txt"

# Negative control: the former runbook command copies only the current object.
mc cp --recursive source/production/ /tmp/cp-backup/
mc mb copy/restore
mc version enable copy/restore
mc cp --recursive /tmp/cp-backup/ copy/restore/
mc ls --json --versions --recursive copy/restore \
  | normalize_manifest >"$WORK_DIR/cp-versions.jsonl"
python3 - "$WORK_DIR/source-versions.jsonl" "$WORK_DIR/cp-versions.jsonl" "$historical_version" <<'PY'
import json
import sys

source = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
copied = [json.loads(line) for line in open(sys.argv[2], encoding="utf-8")]
historical_version = sys.argv[3]
if len(copied) >= len(source):
    raise SystemExit("mc cp control did not lose version or delete-marker history")
if any(item["versionId"] == historical_version for item in copied):
    raise SystemExit("mc cp unexpectedly retained a historical version ID")
if any(item["isDeleteMarker"] for item in copied):
    raise SystemExit("mc cp unexpectedly retained a delete marker")
PY

echo "VERSIONED_OBJECT_BACKUP_EVIDENCE source=source/production replica=replica/backup historical_version=$historical_version"
echo "VERSIONED_OBJECT_BACKUP_CP_CONTROL=lost_history"
mc --version
