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

# How long to wait for a freshly-spawned kernel to mark its lease ready. A local process is up in a
# second or two, but a POD cold-start (schedule + image + heavy imports: duckdb/polars/pyarrow) can take
# far longer — so it's configurable (DP_KERNEL_READY_TIMEOUT_S). The pod deployment sets it higher.
KERNEL_START_TIMEOUT_S = float(os.environ.get("DP_KERNEL_READY_TIMEOUT_S", "30"))


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _kernel_child_env() -> dict:
    """Allowlisted long-lived-kernel environment.

    The kernel still needs the metadata DB until lease/heartbeat/run-state writes move behind a scoped
    control-plane API. It receives no signing/bootstrap/provider secrets merely because the hub has them.
    """
    from hub.workload_env import build_workload_env
    return build_workload_env(include_metadata_db=True)


def _get(endpoint: str, path: str, token: str, timeout: float = 5.0, connect_retries: int = 0) -> dict:
    # Read-only kernel query (e.g. /status); fast-fail by default so a dead kernel can't stall a hot read path.
    req = urllib.request.Request(
        f"http://{endpoint}{path}", headers={"X-DP-Kernel-Token": token}, method="GET")
    for attempt in range(connect_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"kernel {path} → {e.code}: {e.read().decode(errors='replace')[:400]}") from e
        except (urllib.error.URLError, OSError):
            if attempt >= connect_retries:
                raise
            time.sleep(0.5)


def _post(endpoint: str, path: str, token: str, body: dict, timeout: float = 60.0, connect_retries: int = 20) -> dict:
    req = urllib.request.Request(
        f"http://{endpoint}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-DP-Kernel-Token": token}, method="POST")
    for attempt in range(connect_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:  # a real HTTP response from the kernel — surface it, don't retry
            raise RuntimeError(f"kernel {path} → {e.code}: {e.read().decode(errors='replace')[:400]}") from e
        except (urllib.error.URLError, OSError):
            # connection-level failure. On a POD substrate the kernel marks its lease ready as soon as it's
            # serving, but k8s only routes the Service to the pod once its readiness probe passes — so the
            # first POST can be refused for a beat. Retry briefly; raise only if it never becomes reachable.
            if attempt >= connect_retries:
                raise
            time.sleep(0.5)


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
            env=_kernel_child_env(),  # keep the forgeable signing secret out of the child (P0-SEC-01)
            # Detach stdio: a DETACHED kernel that inherits the hub's stdout/stderr keeps those handles
            # open after the hub exits, so anything reading the hub's stream to EOF (a supervisor, or
            # Playwright's `webServer` teardown) blocks forever on the orphan. The kernel's real output
            # is the shared DB (run_states) + its HTTP port, never stdout — so point stdio at /dev/null.
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)  # own process group → a hub SIGTERM/exit doesn't take it down

    def kill(self, canvas_id: str, kernel_id: str) -> None:
        # no-op: a local kernel self-exits when fenced out or idle (it heartbeats the lease). Cross-host
        # force-kill is the pod substrate's job (delete the pod); a local process on another host is
        # unreachable to SIGKILL anyway.
        pass


class KernelBackend:
    """ExecutionBackend that runs on a per-canvas kernel process (name 'kernel')."""
    name = "kernel"
    cancel_acknowledges_stop = True  # persisted cancelled follows local runner/child acknowledgement

    def __init__(self, base, spawner: LocalProcessSpawner):
        self.base = base          # the in-process LocalRunner — hub-side estimate/can_run only
        self.spawner = spawner
        self.on_status = None     # unused: the kernel writes run_states directly (single writer)
        self.on_complete = None
        self.runs: dict = {}      # no in-memory runs here (status is DB-backed via the kernel); kept for interface parity

    def can_run(self, plan: CompilePlan) -> bool:
        return True

    def estimate(self, plan: CompilePlan, rows, byts=None) -> RunEstimate:
        return self.base.estimate(plan, rows, byts)

    @staticmethod
    def supports_admitted_input_manifests() -> bool:
        return True  # the persisted admission is matched before the kernel is ensured or dispatched

    def supports_selected_destination_credentials(self) -> bool:
        # The default kernel offloads full runs to a metadata-isolated subprocess. Opting out keeps the
        # run in this long-lived process, where the authoritative Cred resolver is available.
        isolate = os.environ.get("DP_KERNEL_ISOLATE_RUNS", "1").strip().lower()
        return isolate in ("0", "false", "no", "off")

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
    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None, placement,
            run_id: str | None = None, request_id: str | None = None,
            attempt_id: str | None = None,
            input_manifest: list[dict[str, str]] | None = None) -> RunStatus:
        from hub.backends import require_destination_credential_support
        from hub.local_run_inputs import (
            LocalRunInputError, validate_manifest, validate_manifest_graph,
        )
        from hub.run_outputs import preflight_run_output_target, require_single_run_output
        execution_target = target_node_id
        output_target = preflight_run_output_target(plan, execution_target)
        # This seam is callable without the HTTP router. Fail before credential resolution, kernel
        # lease claims, process spawn, or remote submission so unsupported multi-output work leaves no
        # control-plane or storage side effects.
        if output_target is not None:
            require_single_run_output(graph, output_target, self.base.node_specs)
        require_destination_credential_support(
            self, plan, graph, getattr(self.base, "workspace", ""))
        canvas_id = getattr(graph, "id", None) or "canvas"
        if not run_id:
            raise RuntimeError("kernel runs require a persisted local input admission")
        admission = metadb.local_run_input_admission(run_id)
        if (admission is None or admission["canvas_id"] != canvas_id
                or admission["target_node_id"] != target_node_id):
            raise RuntimeError("kernel run does not match its persisted local input admission")
        try:
            manifest = validate_manifest(input_manifest)
            persisted = validate_manifest(admission["manifest"])
            if manifest != persisted:
                raise LocalRunInputError(
                    "kernel transport manifest does not match its persisted admission")
            manifest = validate_manifest_graph(
                graph, target_node_id, manifest, require_bound_revisions=True)
        except LocalRunInputError as exc:
            raise RuntimeError("kernel run has an invalid admitted input manifest") from exc
        endpoint, token = self._ensure_kernel(canvas_id)
        # ``None`` is semantically meaningful: execute the complete topological graph. Never replace it
        # with the sole public output target, which would drop an independent branch.
        body = {"run_id": run_id, "graph": graph.model_dump(), "target": execution_target,
                "placement": placement, "request_id": request_id, "attempt_id": attempt_id,
                "input_manifest": manifest}
        status = RunStatus(**_post(endpoint, "/run", token, body))
        if request_id and not status.request_id:
            status.request_id = request_id
        return status

    def preview(
            self, graph: Graph, node_id: str, k: int, offset: int,
            port_id: str | None = None) -> dict:
        """Run a sample preview on the canvas's warm kernel (so it shares the kernel's engine + cache)."""
        from hub.graph import require_output_port
        selected = require_output_port(graph, node_id, self.base.node_specs, port_id)
        endpoint, token = self._ensure_kernel(getattr(graph, "id", None) or "canvas")
        return _post(endpoint, "/preview", token,
                     {"graph": graph.model_dump(), "node_id": node_id, "port_id": selected.id,
                      "k": k, "offset": offset})

    def profile(
            self, graph: Graph, node_id: str, full: bool = False,
            port_id: str | None = None) -> dict:
        from hub.graph import require_output_port
        selected = require_output_port(graph, node_id, self.base.node_specs, port_id)
        endpoint, token = self._ensure_kernel(getattr(graph, "id", None) or "canvas")
        return _post(endpoint, "/profile", token,
                     {"graph": graph.model_dump(), "node_id": node_id,
                      "port_id": selected.id, "full": full})

    def profile_job(self, graph: Graph, node_id: str, port_id: str, plan_digest: str,
                    run_id: str, admission_token: str,
                    request_id: str | None = None,
                    input_manifest: list[dict[str, str]] | None = None) -> RunStatus:
        """Queue a whole-dataset profile on the canvas's durable execution owner.

        The kernel stamps its fenced ``kernel_id`` onto the shared RunState. Any stateless hub can
        therefore route cancellation back to the exact process and DuckDB scope that owns the scan.
        """
        from hub.graph import require_output_port
        selected_port = require_output_port(
            graph, node_id, self.base.node_specs, port_id).id
        canvas_id = getattr(graph, "id", None) or "canvas"
        endpoint, token = self._ensure_kernel(canvas_id)
        return RunStatus(**_post(endpoint, "/profile-job", token, {
            "run_id": run_id,
            "admission_token": admission_token,
            "graph": graph.model_dump(),
            "node_id": node_id,
            "port_id": selected_port,
            "plan_digest": plan_digest,
            "request_id": request_id,
            "input_manifest": input_manifest,
        }))

    def status(self, run_id: str) -> RunStatus:
        d = metadb.get_run_state(run_id)  # the kernel is the writer; the DB is the source of truth
        if d is None:
            return RunStatus(run_id=run_id, status="failed", placement="local", per_node=[],
                             error="run not found (no kernel state)")
        return RunStatus(**d)

    def kill(self, canvas_id: str, kernel_id: str) -> None:
        """Force-remove a canvas's kernel via the substrate (delete the pod). No-op for local."""
        try:
            self.spawner.kill(canvas_id, kernel_id)
        except Exception:  # noqa: BLE001
            pass

    def cancel(self, run_id: str) -> RunStatus:
        k = metadb.kernel_for_run(run_id)
        if k and k["endpoint"]:
            try:
                return RunStatus(**_post(k["endpoint"], "/cancel", k["token"], {"run_id": run_id}, timeout=15.0, connect_retries=0))
            except (urllib.error.URLError, OSError, RuntimeError):
                pass  # kernel unreachable, OR it doesn't own this run (_post raises RuntimeError on an
                      # HTTP error after a cross-kernel handoff) → fall through to the last-known DB status
        return self.status(run_id)
