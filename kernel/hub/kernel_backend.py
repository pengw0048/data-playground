"""The per-canvas kernel execution backend (Phase 1) + the spawner that launches one.

Selected opt-in via DP_EXECUTION=kernel. A run for a canvas goes to a long-lived, DETACHED kernel
process (one per canvas, keyed by graph.id) that outlives the hub, so the web tier can be restarted
without killing an in-flight run. The backend delegates estimate/can_run to the base local runner (a
pure hub-side calc, so the confirm gate stays hub-side), mints the authoritative run_id, ensures a
kernel via the atomic lease (spawn if we win, else reuse), and POSTs the job over a token-authed
loopback HTTP command channel. The kernel writes run_states itself (stamped with its kernel_id) — the
single writer — so status() just reads the shared DB and a hub restart never orphans a live run.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from hub import metadb
from hub.models import CompilePlan, Graph, RunEstimate, RunStatus

KERNEL_START_TIMEOUT_S = 30.0


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _post(endpoint: str, path: str, token: str, body: dict, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(
        f"http://{endpoint}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-DP-Kernel-Token": token}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # surface the kernel's error body, don't swallow it as a bare code
        raise RuntimeError(f"kernel {path} → {e.code}: {e.read().decode(errors='replace')[:400]}") from e


class LocalProcessSpawner:
    """Launch a kernel as a DETACHED local process (start_new_session=True, no atexit-kill) so it
    outlives the hub. The spawner picks a free port and passes it; the kernel binds it and marks the
    lease ready once it's actually serving."""
    name = "local-process"

    def __init__(self, workspace: str, data_dir: str):
        self.workspace = workspace
        self.data_dir = data_dir

    def spawn(self, canvas_id: str, kernel_id: str, token: str) -> None:
        port = _free_port()
        subprocess.Popen(
            [sys.executable, "-m", "hub.kernel",
             "--canvas", canvas_id, "--kernel-id", kernel_id, "--token", token,
             "--workspace", self.workspace, "--data-dir", self.data_dir, "--port", str(port)],
            start_new_session=True)  # own process group → a hub SIGTERM/exit doesn't take it down


class KernelBackend:
    """ExecutionBackend that runs on a per-canvas kernel process (name 'kernel')."""
    name = "kernel"

    def __init__(self, base, spawner: LocalProcessSpawner):
        self.base = base          # the in-process LocalRunner — hub-side estimate/can_run only
        self.spawner = spawner
        self.on_status = None     # unused: the kernel writes run_states directly (single writer)
        self.on_complete = None

    def can_run(self, plan: CompilePlan) -> bool:
        return True

    def estimate(self, plan: CompilePlan, rows) -> RunEstimate:
        return self.base.estimate(plan, rows)

    # -- kernel lifecycle -------------------------------------------------- #
    def _await_ready(self, canvas_id: str) -> dict:
        deadline = time.monotonic() + KERNEL_START_TIMEOUT_S
        while time.monotonic() < deadline:
            k = metadb.get_kernel(canvas_id)
            if k and k["state"] == "ready" and k["endpoint"]:
                return k
            time.sleep(0.1)
        raise RuntimeError(f"kernel for canvas '{canvas_id}' did not become ready in {KERNEL_START_TIMEOUT_S}s")

    def _ensure_kernel(self, canvas_id: str) -> tuple[str, str]:
        """(endpoint, token) for the canvas's kernel — spawn one iff we win the atomic lease."""
        kernel_id, token = f"k_{os.urandom(6).hex()}", os.urandom(16).hex()
        claim = metadb.claim_kernel(canvas_id, kernel_id, token)
        if claim["won"]:
            self.spawner.spawn(canvas_id, kernel_id, token)
        k = self._await_ready(canvas_id)
        return k["endpoint"], k["token"]

    # -- ExecutionBackend -------------------------------------------------- #
    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None, placement) -> RunStatus:
        canvas_id = getattr(graph, "id", None) or "canvas"
        run_id = f"run_{os.urandom(5).hex()}"  # hub-authoritative id, threaded into the kernel
        endpoint, token = self._ensure_kernel(canvas_id)
        body = {"run_id": run_id, "graph": graph.model_dump(), "target": target_node_id, "placement": placement}
        return RunStatus(**_post(endpoint, "/run", token, body))

    def status(self, run_id: str) -> RunStatus:
        d = metadb.get_run_state(run_id)  # the kernel is the writer; the DB is the source of truth
        if d is None:
            return RunStatus(run_id=run_id, status="failed", placement="local", per_node=[],
                             error="run not found (no kernel state)")
        return RunStatus(**d)

    def cancel(self, run_id: str) -> RunStatus:
        k = metadb.kernel_for_run(run_id)
        if k and k["endpoint"]:
            try:
                return RunStatus(**_post(k["endpoint"], "/cancel", k["token"], {"run_id": run_id}, timeout=15.0))
            except (urllib.error.URLError, OSError):
                pass  # kernel unreachable → fall through to the last-known DB status
        return self.status(run_id)
