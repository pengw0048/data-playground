#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
COMPOSE_FILE="$ROOT/docker-compose.ray-jobs.yml"
DIAGNOSTICS=${DP_RAY_JOBS_DIAGNOSTICS_DIR:-"$ROOT/ray-jobs-diagnostics"}
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-"dp-ray-jobs-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"}

mkdir -p "$DIAGNOSTICS"

capture() {
  docker compose -f "$COMPOSE_FILE" ps -a >"$DIAGNOSTICS/final-ps.txt" 2>&1 || true
  docker compose -f "$COMPOSE_FILE" logs --no-color >"$DIAGNOSTICS/compose.log" 2>&1 || true
  docker info >"$DIAGNOSTICS/docker-info.txt" 2>&1 || true
  df -h >"$DIAGNOSTICS/disk.txt" 2>&1 || true
}

cleanup() {
  rc=$?
  capture
  if [[ ${DP_RAY_JOBS_KEEP_CLUSTER:-0} != 1 ]]; then
    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >>"$DIAGNOSTICS/teardown.log" 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

cd "$ROOT"
docker compose -f "$COMPOSE_FILE" config -q
docker compose -f "$COMPOSE_FILE" build ray-head
export DP_RAY_JOBS_CODE_REF
DP_RAY_JOBS_CODE_REF=$(docker image inspect dp-ray:local --format '{{.Id}}')
if [[ ! $DP_RAY_JOBS_CODE_REF =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Built image did not expose an immutable sha256 image ID: $DP_RAY_JOBS_CODE_REF" >&2
  exit 1
fi
echo "$DP_RAY_JOBS_CODE_REF" >"$DIAGNOSTICS/code-ref.txt"

docker compose -f "$COMPOSE_FILE" up -d --wait --wait-timeout 120 --no-build postgres minio
docker compose -f "$COMPOSE_FILE" run --rm --no-deps storage-init \
  2>&1 | tee "$DIAGNOSTICS/storage-init.log"
docker compose -f "$COMPOSE_FILE" run --rm --no-deps migrate \
  2>&1 | tee "$DIAGNOSTICS/migrate.log"
docker compose -f "$COMPOSE_FILE" up -d --no-build ray-head ray-worker

registered=0
for _ in $(seq 1 90); do
  registered=$(docker compose -f "$COMPOSE_FILE" exec -T ray-head python -c \
    'import ray; ray.init(address="auto", configure_logging=False, log_to_driver=False); print(sum(1 for n in ray.nodes() if n.get("Alive") and n.get("Resources", {}).get("CPU", 0) > 0))' \
    2>/dev/null | tail -1) || true
  if [[ $registered =~ ^[0-9]+$ ]] && (( registered >= 2 )); then
    break
  fi
  sleep 2
done
if [[ ! $registered =~ ^[0-9]+$ ]] || (( registered < 2 )); then
  echo "Expected a Ray head and worker; observed $registered CPU-bearing nodes." >&2
  exit 1
fi
echo "Ray head and worker registered." | tee "$DIAGNOSTICS/ray-ready.log"

for phase in submit-restart recover-restart cancel missing corrupt; do
  docker compose -f "$COMPOSE_FILE" run --rm --no-deps jobs-check \
    python -m hub.ray_jobs_acceptance "$phase" 2>&1 | tee "$DIAGNOSTICS/$phase.log"
done

echo "Ray Jobs restart/cancel/result acceptance: PASS"
