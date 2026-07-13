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

from hub.workload_env import build_workload_env

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
    def _name(canvas_id: str, kernel_id: str) -> str:
        # a DNS-1123 name unique per (canvas, kernel) — so a NEW kernel never collides with a
        # still-terminating old one (no 409), and kill(canvas, kernel_id) is naturally FENCED: it can
        # only delete ITS kernel's pod/service, never a newer kernel that already took over the canvas.
        return "dp-kernel-" + hashlib.sha1(f"{canvas_id}/{kernel_id}".encode()).hexdigest()[:16]

    @staticmethod
    def _create_idempotent(create_fn, ns: str, body: dict) -> None:
        try:
            create_fn(ns, body)
        except Exception as e:  # noqa: BLE001
            # 409 AlreadyExists = a prior spawn of THIS kernel_id already created it (the name is unique
            # per kernel_id, so it's ours) → idempotent, fine. Anything else is a real error → re-raise.
            if getattr(e, "status", None) != 409:
                raise

    def _pod_body(self, name: str, cmd: list[str]) -> dict:
        # The image supplies PATH/HOME/etc.; only explicit workload config/capabilities cross the pod
        # boundary. The metadata identity remains until the kernel persistence protocol is separated.
        child = build_workload_env(include_metadata_db=True, include_host_runtime=False)
        env = [{"name": key, "value": child[key]} for key in sorted(child)]
        return {
            "metadata": {"name": name, "labels": {"app": "dp-kernel", "dp-canvas": name}},
            "spec": {
                "restartPolicy": "Never",  # a kernel exits on idle/fence; k8s must not respawn it
                "containers": [{
                    "name": "kernel", "image": self.image, "command": cmd,
                    "ports": [{"containerPort": _PORT}],
                    "env": env,
                    # liveness reinforces the lease heartbeat: a wedged kernel is restarted... no —
                    # restartPolicy Never means it's just killed; the lease goes stale → reaped.
                    "readinessProbe": {"tcpSocket": {"port": _PORT}, "initialDelaySeconds": 1},
                }],
            },
        }

    def _svc_body(self, name: str) -> dict:
        return {"metadata": {"name": name, "labels": {"app": "dp-kernel", "dp-canvas": name}},
                "spec": {"selector": {"dp-canvas": name},
                         "ports": [{"port": _PORT, "targetPort": _PORT}]}}

    def spawn(self, canvas_id: str, kernel_id: str, token: str) -> None:
        name = self._name(canvas_id, kernel_id)
        dns = f"{name}.{self.ns}.svc.cluster.local"
        cmd = ["python", "-m", "hub.kernel",
               "--canvas", canvas_id, "--kernel-id", kernel_id, "--token", token,
               "--workspace", self.workspace, "--data-dir", self.data_dir,
               "--port", str(_PORT), "--host", "0.0.0.0", "--advertise-host", dns]
        api = self._api()
        self._create_idempotent(api.create_namespaced_service, self.ns, self._svc_body(name))
        self._create_idempotent(api.create_namespaced_pod, self.ns, self._pod_body(name, cmd))
        # the pod's kernel marks the lease ready with `dns:_PORT` once serving; ensure_kernel polls it.

    def kill(self, canvas_id: str, kernel_id: str) -> None:
        name = self._name(canvas_id, kernel_id)
        api = self._api()
        # grace_period_seconds=0: the kernel was already asked to exit (or is dead), so don't wait out
        # k8s's 30s termination grace — free the name immediately (a same-name recreate never 409s anyway
        # now that the name carries kernel_id, but this also frees resources promptly).
        for fn in (lambda: api.delete_namespaced_pod(name, self.ns, grace_period_seconds=0),
                   lambda: api.delete_namespaced_service(name, self.ns)):
            try:
                fn()
            except Exception:  # noqa: BLE001 — already gone / not found is fine
                pass
