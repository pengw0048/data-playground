"""Reference cross-host kernel substrate: one k8s Pod + Service per canvas (DP_KERNEL_SPAWNER=pod).

This is the Phase-3 substrate — the SAME KernelSpawner protocol as the local process, so nothing else
changes: the pod runs `python -m hub.kernel` (binding 0.0.0.0, advertising its Service DNS), writes
run_states + heartbeats the lease exactly as a local kernel does, and the hub reaches it at the Service
DNS. Because any hub resolves canvas_id → endpoint from the DB and can delete a pod by name, this fixes
the local substrate's single-host limit (a hub can't SIGKILL a process on another host).

REFERENCE, not turnkey: it needs the `kubernetes` client (`kernel[pod]` extra), in-cluster RBAC to
create/delete Pods+Services, a `DP_KERNEL_IMAGE` that can run `hub.kernel`, a shared `DP_DATABASE_URL`
(Postgres) reachable from the pods, and the DATA reachable from the pod (object storage via
`DP_STORAGE_URL`, or a mounted PVC — mounting is left to the operator). Verify on your own cluster.
"""

from __future__ import annotations

import hashlib
import os

# DP_* env the kernel pod needs to share the hub's DB / storage / auth / dataset roots. Object-store
# creds ride along too so the pod's DuckDB can read s3://gs:// data.
_FORWARD_ENV = ("DP_DATABASE_URL", "DP_DATASET_ROOTS", "DP_AUTH_SECRET", "DP_STORAGE_URL",
                "DP_MEMORY_LIMIT", "DP_SPILL_DIR", "DP_KERNEL_IDLE_TTL",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
                "GOOGLE_APPLICATION_CREDENTIALS")
_PORT = 8500  # fixed inside the pod; the Service exposes it


class PodSpawner:
    name = "pod"

    def __init__(self, workspace: str, data_dir: str, client=None):
        self.workspace, self.data_dir = workspace, data_dir
        self.ns = os.environ.get("DP_KERNEL_NAMESPACE", "default")
        self.image = os.environ.get("DP_KERNEL_IMAGE", "dataplay:latest")
        self._client = client  # injectable (tests); else a lazily-created CoreV1Api

    def _api(self):
        if self._client is None:
            from kubernetes import client, config  # optional dep (kernel[pod])
            try:
                config.load_incluster_config()      # running inside the cluster (the normal case)
            except Exception:  # noqa: BLE001
                config.load_kube_config()            # dev / out-of-cluster
            self._client = client.CoreV1Api()
        return self._client

    @staticmethod
    def _name(canvas_id: str) -> str:
        # a DNS-1123 name derived from the canvas id (which may contain anything)
        return "dp-kernel-" + hashlib.sha1(canvas_id.encode()).hexdigest()[:16]

    def _pod_body(self, name: str, cmd: list[str]) -> dict:
        return {
            "metadata": {"name": name, "labels": {"app": "dp-kernel", "dp-canvas": name}},
            "spec": {
                "restartPolicy": "Never",  # a kernel exits on idle/fence; k8s must not respawn it
                "containers": [{
                    "name": "kernel", "image": self.image, "command": cmd,
                    "ports": [{"containerPort": _PORT}],
                    "env": [{"name": k, "value": os.environ[k]} for k in _FORWARD_ENV if os.environ.get(k)],
                    # liveness reinforces the lease heartbeat: a wedged kernel is restarted... no —
                    # restartPolicy Never means it's just killed; the lease goes stale → reaped.
                    "readinessProbe": {"tcpSocket": {"port": _PORT}, "initialDelaySeconds": 1},
                }],
            },
        }

    def _svc_body(self, name: str) -> dict:
        return {"metadata": {"name": name},
                "spec": {"selector": {"dp-canvas": name},
                         "ports": [{"port": _PORT, "targetPort": _PORT}]}}

    def spawn(self, canvas_id: str, kernel_id: str, token: str) -> None:
        name = self._name(canvas_id)
        dns = f"{name}.{self.ns}.svc.cluster.local"
        cmd = ["python", "-m", "hub.kernel",
               "--canvas", canvas_id, "--kernel-id", kernel_id, "--token", token,
               "--workspace", self.workspace, "--data-dir", self.data_dir,
               "--port", str(_PORT), "--host", "0.0.0.0", "--advertise-host", dns]
        api = self._api()
        api.create_namespaced_service(self.ns, self._svc_body(name))
        api.create_namespaced_pod(self.ns, self._pod_body(name, cmd))
        # the pod's kernel marks the lease ready with `dns:_PORT` once serving; ensure_kernel polls it.

    def kill(self, canvas_id: str, kernel_id: str) -> None:
        name = self._name(canvas_id)
        api = self._api()
        for fn in (lambda: api.delete_namespaced_pod(name, self.ns),
                   lambda: api.delete_namespaced_service(name, self.ns)):
            try:
                fn()
            except Exception:  # noqa: BLE001 — already gone / not found is fine
                pass
