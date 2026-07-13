"""A per-canvas execution KERNEL: a long-lived, detached process that runs one canvas's runs on a
warm in-process engine and writes run status to the shared DB — so the hub (web tier) can restart
without killing an in-flight run. Launched by hub.kernel_backend.LocalProcessSpawner as
`python -m hub.kernel --canvas <id> --kernel-id <id> --token <tok> --workspace ... --port <p>`.

Phase 1 is a COLD kernel: the DuckDB connection + the DB-backed result cache are warm across runs for
free, but the per-kernel relation cache and preview-on-kernel come in Phase 2. Command channel here is
just /run, /cancel, /shutdown over token-authed loopback HTTP. The kernel OWNS run_states writes
(stamped with its kernel_id) — the single writer; the hub's KernelBackend only reads them.
"""

from __future__ import annotations

import argparse
import os

from pydantic import BaseModel


class RunBody(BaseModel):
    # module-level (not inside main): with `from __future__ import annotations`, FastAPI must resolve
    # the body annotation from module globals — a function-local model resolves to nothing → 422.
    run_id: str
    graph: dict
    target: str | None = None
    placement: str = "local"


class PreviewBody(BaseModel):
    graph: dict
    node_id: str
    k: int = 50
    offset: int = 0
    full: bool = False   # profile only: whole-dataset stats (full pass) instead of the sample


def _liveness_busy(inflight_count: int, runs) -> bool:
    """The idle-TTL watchdog treats the kernel as BUSY while an in-process preview/profile is in flight
    (`inflight_count`) OR any offloaded run is queued/running. In-process preview/profile don't appear in
    `runs` (only offloaded /run does), so without the inflight term a full-dataset profile longer than the
    idle-ttl would be recycled out from under itself. Module-level + pure so it's unit-testable."""
    return inflight_count > 0 or any(
        getattr(s, "status", None) in ("queued", "running") for s in runs.values())


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.kernel")
    p.add_argument("--canvas", required=True)
    p.add_argument("--kernel-id", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")            # bind address (0.0.0.0 in a pod)
    p.add_argument("--advertise-host", default=None)         # address the hub reaches us at (Service DNS in a pod); defaults to --host
    p.add_argument("--idle-ttl", type=float, default=float(os.environ.get("DP_KERNEL_IDLE_TTL", "900")))
    args = p.parse_args()

    # Freeze the workspace into the env BEFORE importing hub.settings (its DB url is frozen at import).
    # The hub's env is inherited, so these are usually already set to the same values — setdefault keeps
    # the hub's, guaranteeing the kernel shares the hub's metadata DB (run_states / kernels).
    os.environ.setdefault("DP_WORKSPACE", args.workspace)
    os.environ.setdefault("DP_DATA_DIR", args.data_dir)

    import logging
    import threading
    import time
    from contextlib import asynccontextmanager, contextmanager

    import uvicorn
    from fastapi import FastAPI, Header, HTTPException

    from hub import compiler, db, metadb
    from hub.deps import set_workspace
    from hub.models import Graph

    from hub.relation_cache import RelationCache

    canvas, kid, token = args.canvas, args.kernel_id, args.token
    deps = set_workspace(args.workspace, args.data_dir)
    warm = RelationCache()  # per-kernel warm cache of preview intermediate relations (dropped on restart)
    # cell-crash-isolation: full RUNS execute in a killable, deadline-bounded child PROCESS by default, so
    # a runaway/segfaulting/OOM cell kills only that run — the warm kernel (and its live previews) survive.
    # Previews/profile stay in-process on the warm engine (fast, interactive), protected by the reaper.
    # Opt out with DP_KERNEL_ISOLATE_RUNS=0 (runs on the warm in-process engine = the old behavior).
    _isolate = os.environ.get("DP_KERNEL_ISOLATE_RUNS", "1").strip().lower() not in ("0", "false", "no", "off")
    run_runner = deps.runner
    if _isolate:
        run_runner = next((r for r in deps.runners if getattr(r, "name", "") == "local-subprocess"), deps.runner)
    # single-writer: the kernel persists run_states stamped with OUR kernel_id (so the boot-time reaper
    # spares this run while we're alive). Wire it onto the runner that OWNS runs (the isolated child
    # manager when isolating), since that's what emits queued/progress/terminal for /run.
    run_runner.on_status = lambda g, st: metadb.save_run_state(
        st.run_id, st.model_dump(), canvas_id=getattr(g, "id", None), kernel_id=kid,
        publish_region=st.status == "done")

    last_activity = [time.monotonic()]
    # in-process preview/profile run IN this kernel (not offloaded to a child), so — unlike /run — they
    # don't show up in run_runner.runs. Count them explicitly so the idle-TTL watchdog treats the kernel
    # as BUSY for the WHOLE duration of a long full-dataset profile, not just the instant it started
    # (else a profile longer than idle-ttl gets its warm kernel recycled out from under it).
    _inflight = [0]
    _inflight_lock = threading.Lock()

    @contextmanager
    def _inflight_work():
        with _inflight_lock:
            _inflight[0] += 1
        last_activity[0] = time.monotonic()
        try:
            yield
        finally:
            with _inflight_lock:
                _inflight[0] -= 1
            last_activity[0] = time.monotonic()  # restart the idle clock from completion, not from start

    def _auth(tok: str | None) -> None:
        if tok != token:
            raise HTTPException(401, "bad kernel token")

    def _ensure_deps(graph) -> None:
        # install the canvas's declared pip deps into this kernel + let the sandbox import EXACTLY them
        # (replace, not grow — so removing a requirement stops allowing it; empty requirements → allow
        # nothing). Runs on every request; ensure() is idempotent so it's cheap once installed.
        from hub import kernel_deps, sandbox
        from hub.settings import settings
        if not settings.canvas_pip_deps:  # operator disabled per-canvas deps → install nothing, allow nothing
            sandbox.set_allowed(set())
            return
        reqs = getattr(graph, "requirements", None) or []
        mods = kernel_deps.ensure(reqs, kernel_deps.deps_dir(args.workspace, canvas)) if reqs else set()
        sandbox.set_allowed(mods)

    # wedge watchdog: a healthy warm kernel completes a trivial query fast (runs are offloaded to child
    # processes, so nothing long-running blocks the kernel itself). If it CAN'T for several cycles it's
    # wedged (deadlocked engine / held lock) — drop the lease and exit so the next run respawns a fresh
    # kernel. A GIL-starved wedge is caught for free: the heartbeat thread can't run, so the lease goes
    # stale and the hub reaper recycles it. Tie liveness to real responsiveness, not a blind timer.
    try:
        _probe_timeout = float(os.environ.get("DP_KERNEL_PROBE_TIMEOUT", "5"))
    except ValueError:
        _probe_timeout = 5.0
    _probe_fails = [0]

    def _heartbeat_loop() -> None:
        while True:
            time.sleep(5.0)
            # A raise here (a transient DB error in heartbeat_kernel / db.responsive) would SILENTLY kill
            # this daemon thread → the lease goes stale → the hub reaps our runs and we zombie, with no
            # trace of why. Catch + log + retry next cycle; the os._exit paths below still exit the process
            # (os._exit doesn't raise), so fencing/idle/unresponsive recycling is unaffected.
            try:
                if not metadb.heartbeat_kernel(canvas, kid):
                    os._exit(0)  # fenced out — a newer kernel took over this canvas
                busy = _liveness_busy(_inflight[0], run_runner.runs)
                if busy:
                    last_activity[0] = time.monotonic()
                elif time.monotonic() - last_activity[0] > args.idle_ttl:
                    metadb.drop_kernel(canvas, kid)  # fenced delete — releases only if still ours
                    os._exit(0)
                if db.responsive(_probe_timeout):
                    _probe_fails[0] = 0
                else:
                    _probe_fails[0] += 1
                    if _probe_fails[0] >= 3:  # ~3 cycles of unresponsiveness (< KERNEL_STALE_S) → recycle
                        metadb.drop_kernel(canvas, kid)
                        os._exit(1)
            except Exception:  # noqa: BLE001
                logging.getLogger("hub").warning("kernel heartbeat cycle failed (continuing)", exc_info=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # mark ready only once uvicorn is actually serving, so the hub never POSTs to a dead port.
        # advertise the address the hub reaches us at (Service DNS in a pod), not the bind host.
        metadb.mark_kernel_ready(canvas, kid, f"{args.advertise_host or args.host}:{args.port}")
        threading.Thread(target=_heartbeat_loop, daemon=True).start()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.post("/run")
    def run(body: RunBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        last_activity[0] = time.monotonic()
        graph = Graph(**body.graph)
        _ensure_deps(graph)
        plan = compiler.compile_plan(graph, body.target, deps.registry, deps.node_specs, deps.node_ir)
        st = run_runner.run(plan, graph, body.target, body.placement, run_id=body.run_id)
        return st.model_dump()

    @app.post("/preview")
    def preview(body: PreviewBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        from hub.executors.preview import preview_node
        graph = Graph(**body.graph)
        _ensure_deps(graph)
        with _inflight_work():
            return preview_node(graph, body.node_id, body.k, deps.resolve_adapter,
                                deps.registry, deps.node_builders, deps.node_specs, offset=body.offset, cache=warm).model_dump()

    @app.post("/profile")
    def profile(body: PreviewBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        from hub.executors.profile import profile_node
        graph = Graph(**body.graph)
        _ensure_deps(graph)
        with _inflight_work():
            return profile_node(graph, body.node_id, deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, cache=warm, full=body.full).model_dump()

    @app.post("/cancel")
    def cancel(body: dict, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        return run_runner.cancel(body["run_id"]).model_dump()  # the runner that OWNS the run (isolated child mgr)

    @app.post("/shutdown")
    def shutdown(x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        metadb.drop_kernel(canvas, kid)
        from hub.sdk import close_resources
        close_resources()  # release warm resource handles (models/decoders/pools) before the hard exit
        threading.Timer(0.2, lambda: os._exit(0)).start()
        return {"ok": True}

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
