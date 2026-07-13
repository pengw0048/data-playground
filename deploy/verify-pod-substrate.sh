#!/usr/bin/env bash
# End-to-end verification of the per-canvas kernel POD substrate on a LOCAL, disposable kind cluster.
#
# It builds the app image (+ a seed-baked verify image), creates a kind cluster, deploys the hub +
# Postgres + RBAC (deploy/k8s/pod-substrate.yaml), then drives a real run through the HTTP API and
# asserts that PodSpawner spawned a per-canvas kernel Pod, the run completed on it, and "restart kernel"
# tore the Pod down. Everything is namespaced to a throwaway cluster; it never touches your other kube
# contexts. Re-runnable. Set KEEP=1 to leave the cluster up for poking around.
set -euo pipefail

CLUSTER=dp-podverify
CTX="kind-${CLUSTER}"
K="kubectl --context ${CTX} -n dp"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PF_PID=""

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
wait_for_no_pods() {
  local selector="$1" deadline=$((SECONDS + 120))
  while [ -n "$($K get pods -l "$selector" -o name)" ]; do
    if (( SECONDS >= deadline )); then
      echo "timed out waiting for pods matching ${selector} to stop" >&2
      $K get pods -l "$selector" -o wide >&2
      return 1
    fi
    sleep 1
  done
}
cleanup() {
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1 → leaving cluster '${CLUSTER}' up (kind delete cluster --name ${CLUSTER} to remove)"; else
    say "Teardown"; kind delete cluster --name "${CLUSTER}" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

say "1/6 Build images (app + seed-baked verify)"
# --provenance=false: BuildKit (colima's builder) otherwise attaches attestation/provenance manifests
# that `kind load docker-image` can't import ("content digest … not found"). A plain single-platform
# image loads cleanly.
docker build --provenance=false -t dataplay:local "${ROOT}" >/dev/null
docker build --provenance=false -f "${ROOT}/deploy/k8s/Dockerfile.podverify" -t dataplay:podverify "${ROOT}" >/dev/null

say "2/6 Create kind cluster '${CLUSTER}' + load the app image"
# Only load OUR image (single-platform, --provenance=false → importable). postgres:16-alpine is a
# multi-arch index that `kind load` can't import (--all-platforms → "content digest not found"), so we
# let the kind node pull it from Docker Hub instead (the node has network).
kind get clusters 2>/dev/null | grep -qx "${CLUSTER}" || kind create cluster --name "${CLUSTER}" >/dev/null
kind load docker-image dataplay:podverify --name "${CLUSTER}" >/dev/null

say "3/6 Apply manifests, run the one-shot migration, then start the hub"
# The manifest sets the Hub Deployment to zero replicas atomically, so applying a new image cannot
# launch it before migration. Wait for old hub Pods to exit, then stop detached per-canvas kernels;
# those remain independent metadata writers after the Hub itself is gone.
kubectl --context "${CTX}" apply -f "${ROOT}/deploy/k8s/pod-substrate.yaml" >/dev/null
$K rollout status deploy/postgres --timeout=120s
wait_for_no_pods app=dp-hub
$K delete pod -l app=dp-kernel --ignore-not-found --wait=false >/dev/null
wait_for_no_pods app=dp-kernel
$K delete service -l app=dp-kernel --ignore-not-found >/dev/null
$K delete job dp-migrate --ignore-not-found >/dev/null
$K apply -f "${ROOT}/deploy/k8s/migrate-job.yaml" >/dev/null
$K wait --for=condition=complete job/dp-migrate --timeout=120s
$K scale deploy/dp-hub --replicas=1 >/dev/null
$K rollout status deploy/dp-hub --timeout=120s

say "4/6 Port-forward the hub + wait for /api/readyz"
kubectl --context "${CTX}" -n dp port-forward svc/dp-hub 18471:8471 >/dev/null 2>&1 &
PF_PID=$!
for i in $(seq 1 30); do curl -fsS localhost:18471/api/readyz >/dev/null 2>&1 && break; sleep 1; done
curl -fsS localhost:18471/api/readyz && echo " ← hub ready"

H=(-H 'Content-Type: application/json' -H 'X-DP-User: local')
CANVAS='{"id":"cv-podverify","name":"podverify","version":1,
  "nodes":[{"id":"src","type":"source","position":{"x":0,"y":0},"data":{"title":"events","config":{"uri":"events"}}},
           {"id":"flt","type":"filter","position":{"x":300,"y":0},"data":{"title":"filter","config":{"predicate":"amount > 1"}}}],
  "edges":[{"id":"e1","source":"src","target":"flt","sourceHandle":null,"targetHandle":null,"data":{"wire":"dataset"}}]}'
GRAPH='{"graph":{"id":"cv-podverify","version":1,
  "nodes":[{"id":"src","type":"source","position":{"x":0,"y":0},"data":{"title":"events","config":{"uri":"events"}}},
           {"id":"flt","type":"filter","position":{"x":300,"y":0},"data":{"title":"filter","config":{"predicate":"amount > 1"}}}],
  "edges":[{"id":"e1","source":"src","target":"flt","sourceHandle":null,"targetHandle":null,"data":{"wire":"dataset"}}]},
  "targetNodeId":"flt","confirmed":true}'

say "5/6 Run a node → PodSpawner should spawn a kernel Pod, the run complete on it"
curl -fsS "${H[@]}" -X POST localhost:18471/api/canvas -d "$CANVAS" >/dev/null
RID=$(curl -fsS "${H[@]}" -X POST localhost:18471/api/run -d "$GRAPH" | python3 -c 'import sys,json;print(json.load(sys.stdin)["runId"])')
echo "run id: $RID (the POST blocks until the kernel pod is ready)"
echo "--- kernel pods (spawned by PodSpawner) ---"; $K get pods -l app=dp-kernel -o wide
test "$($K get pods -l app=dp-kernel --no-headers 2>/dev/null | wc -l | tr -d ' ')" -ge 1 \
  && echo "✓ a per-canvas kernel Pod exists" || { echo "✗ no kernel pod spawned"; exit 1; }
for i in $(seq 1 60); do
  ST=$(curl -fsS "${H[@]}" localhost:18471/api/run/$RID | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$ST" = done ] || [ "$ST" = failed ] && break; sleep 1
done
echo "run status: $ST"; [ "$ST" = done ] && echo "✓ run completed on the kernel pod" || { echo "✗ run did not complete"; $K logs -l app=dp-kernel --tail=30; exit 1; }

say "6/6 Restart the kernel → the Pod + Service should be deleted"
curl -fsS "${H[@]}" -X POST localhost:18471/api/canvas/cv-podverify/kernel/restart >/dev/null
for i in $(seq 1 30); do [ "$($K get pods -l app=dp-kernel --no-headers 2>/dev/null | wc -l | tr -d ' ')" = 0 ] && break; sleep 1; done
$K get pods,svc -l app=dp-kernel --no-headers 2>/dev/null
test "$($K get pods -l app=dp-kernel --no-headers 2>/dev/null | wc -l | tr -d ' ')" = 0 \
  && echo "✓ kernel Pod torn down on restart" || { echo "✗ kernel pod still present after restart"; exit 1; }

say "ALL CHECKS PASSED — the pod substrate spawns, runs, and tears down on a real cluster"
