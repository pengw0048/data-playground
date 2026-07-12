#!/usr/bin/env bash
# Re-runnable KubeRay differential on an existing disposable kind cluster.
# A unique image tag plus delete/recreate avoids stale IfNotPresent images and immutable Job templates.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KIND_CLUSTER="${KIND_CLUSTER:-kind}"
IMAGE="${DP_RAY_VALIDATION_IMAGE:-dp-ray:kuberay-$(date +%Y%m%d%H%M%S)-$$}"
TIMEOUT_SECONDS="${DP_RAY_VALIDATION_TIMEOUT_SECONDS:-600}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

case "${TIMEOUT_SECONDS}" in
  ''|*[!0-9]*|0) echo "DP_RAY_VALIDATION_TIMEOUT_SECONDS must be a positive integer" >&2; exit 2 ;;
esac

# Bind kubectl to the exact cluster that `kind load` targets. A user's same-named kubeconfig context may
# have been redirected; using it while deleting fixed validation resource names would be unsafe.
KIND_KUBECONFIG="${TMP}/kind-kubeconfig"
kind get kubeconfig --name "${KIND_CLUSTER}" > "${KIND_KUBECONFIG}"
KUBECTL=(kubectl --kubeconfig "${KIND_KUBECONFIG}")

say() { printf '\n== %s\n' "$*"; }

wait_for_no_pods() {
  local selector="$1" label="$2" deadline=$((SECONDS + 180)) remaining
  while (( SECONDS < deadline )); do
    remaining="$("${KUBECTL[@]}" get pods -l "${selector}" -o name 2>/dev/null || true)"
    [ -z "${remaining}" ] && return 0
    sleep 2
  done
  "${KUBECTL[@]}" get pods -l "${selector}" -o wide || true
  echo "timed out waiting for old ${label} pods to disappear" >&2
  return 1
}

job_diagnostics() {
  "${KUBECTL[@]}" logs job/dp-ray-multinode-check --tail=500 || true
  "${KUBECTL[@]}" describe job dp-ray-multinode-check || true
}

wait_for_differential() {
  local deadline=$((SECONDS + TIMEOUT_SECONDS)) succeeded failed
  while (( SECONDS < deadline )); do
    succeeded="$("${KUBECTL[@]}" get job dp-ray-multinode-check \
      -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
    failed="$("${KUBECTL[@]}" get job dp-ray-multinode-check \
      -o jsonpath='{.status.failed}' 2>/dev/null || true)"
    if (( ${succeeded:-0} >= 1 )); then
      return 0
    fi
    if (( ${failed:-0} >= 1 )); then
      echo "multi-node differential failed" >&2
      job_diagnostics
      return 1
    fi
    sleep 2
  done
  echo "timed out waiting for the multi-node differential" >&2
  job_diagnostics
  return 1
}

say "Build and load fresh Ray image ${IMAGE}"
docker build --provenance=false -f "${ROOT}/docker/ray/Dockerfile" -t "${IMAGE}" "${ROOT}"
kind load docker-image "${IMAGE}" --name "${KIND_CLUSTER}"

say "Delete prior immutable validation resources"
"${KUBECTL[@]}" delete job dp-ray-multinode-check dp-ray-createbucket \
  --ignore-not-found --cascade=foreground --wait=true --timeout=180s
"${KUBECTL[@]}" delete raycluster dp-ray \
  --ignore-not-found --cascade=foreground --wait=true --timeout=180s
# Foreground deletion should reap dependents, but make stale execution impossible even if an operator or
# garbage collector violates that expectation. KubeRay selects same-named pods by label, not owner UID.
wait_for_no_pods "ray.io/cluster=dp-ray" "RayCluster"
wait_for_no_pods "batch.kubernetes.io/job-name=dp-ray-multinode-check" "differential Job"
wait_for_no_pods "batch.kubernetes.io/job-name=dp-ray-createbucket" "bucket Job"

# Render the checked-in local placeholder to this invocation's immutable-in-practice tag. Re-applying
# dp-ray:local with IfNotPresent can otherwise run an old image even after `kind load`, and an existing
# Job cannot have its pod template updated in place.
sed "s|image: dp-ray:local|image: ${IMAGE}|g" \
  "${ROOT}/deploy/kuberay/raycluster.yaml" > "${TMP}/raycluster.yaml"
sed "s|image: dp-ray:local|image: ${IMAGE}|g" \
  "${ROOT}/deploy/kuberay/differential-job.yaml" > "${TMP}/differential-job.yaml"

say "Start MinIO and create the validation bucket"
"${KUBECTL[@]}" apply -f "${ROOT}/deploy/kuberay/minio.yaml"
"${KUBECTL[@]}" rollout status deployment/minio --timeout=180s
"${KUBECTL[@]}" wait --for=condition=complete --timeout=180s job/dp-ray-createbucket

say "Start a fresh three-pod RayCluster"
"${KUBECTL[@]}" apply -f "${TMP}/raycluster.yaml"
for _ in $(seq 1 90); do
  count="$("${KUBECTL[@]}" get pods -l ray.io/cluster=dp-ray --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  [ "${count}" = "3" ] && break
  sleep 2
done
if [ "${count:-0}" != "3" ]; then
  "${KUBECTL[@]}" get pods -l ray.io/cluster=dp-ray -o wide
  echo "expected one head and two worker pods, found ${count:-0}" >&2
  exit 1
fi
"${KUBECTL[@]}" wait --for=condition=Ready --timeout=300s pod -l ray.io/cluster=dp-ray
"${KUBECTL[@]}" get pods -l ray.io/cluster=dp-ray -o wide

say "Run the multi-node differential"
"${KUBECTL[@]}" apply -f "${TMP}/differential-job.yaml"
if ! wait_for_differential; then
  exit 1
fi
"${KUBECTL[@]}" logs job/dp-ray-multinode-check
