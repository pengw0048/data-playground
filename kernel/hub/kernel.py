"""A per-canvas execution KERNEL: a long-lived, detached process that runs one canvas's runs on a
warm in-process engine and writes run status to the shared DB — so the hub (web tier) can restart
without killing an in-flight run. Launched by hub.kernel_backend.LocalProcessSpawner as
`python -m hub.kernel --canvas <id> --kernel-id <id> --token <tok> --workspace ... --port <p>`.

Phase 1 is a COLD kernel: the DuckDB connection + the DB-backed result cache are warm across runs for
free, but the per-kernel relation cache and preview-on-kernel come in Phase 2. The command channel carries
runs, profiles, cancellation, and shutdown over token-authenticated HTTP. The kernel OWNS run_states writes
(stamped with its kernel_id) — the single writer; the hub's KernelBackend only reads them.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from pydantic import BaseModel, Field

_STARTED_AT = time.monotonic()  # process start, for uptimeSeconds in /status


class RunBody(BaseModel):
    # module-level (not inside main): with `from __future__ import annotations`, FastAPI must resolve
    # the body annotation from module globals — a function-local model resolves to nothing → 422.
    run_id: str
    graph: dict
    target: str | None = None
    placement: str = "local"
    request_id: str | None = None  # OPS-01: HTTP/WebSocket id that started this run
    attempt_id: str | None = None  # OPS-01: optional managed-object attempt correlation
    input_manifest: list[dict[str, str]]


class PreviewBody(BaseModel):
    graph: dict
    node_id: str
    port_id: str | None = Field(default=None, min_length=1, max_length=128)
    k: int = 50
    offset: int = 0
    full: bool = False   # profile only: whole-dataset stats (full pass) instead of the sample


class ProfileJobBody(BaseModel):
    run_id: str
    admission_token: str = Field(min_length=20, max_length=200)
    graph: dict
    node_id: str
    plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_id: str | None = None


def _liveness_busy(inflight_count: int, runs) -> bool:
    """The idle-TTL watchdog treats the kernel as BUSY while an in-process preview/profile is in flight
    (`inflight_count`) OR any offloaded run is queued/running. In-process preview/profile don't appear in
    `runs` (only offloaded /run does), so without the inflight term a full-dataset profile longer than the
    idle-ttl would be recycled out from under itself. Module-level + pure so it's unit-testable."""
    return inflight_count > 0 or any(
        getattr(s, "status", None) in ("queued", "running") for s in runs.values())


def _persist_kernel_run_state(metadata, graph, status, *, kernel_id: str) -> None:
    """Persist one kernel-owned status with terminal region publication authority."""
    metadata.save_run_state(
        status.run_id, status.model_dump(), canvas_id=getattr(graph, "id", None),
        kernel_id=kernel_id, publish_region=status.status in ("done", "failed"))


def _rss_bytes() -> int | None:
    """Current resident set size in bytes, or None if unavailable (never faked)."""
    try:
        with open("/proc/self/statm") as f:  # Linux (prod pods): current resident pages
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # peak; bytes on macOS, KiB on Linux
        return rss if sys.platform == "darwin" else rss * 1024
    except (ImportError, ValueError, OSError, AttributeError):
        return None


def _runs_snapshot(runs, lock) -> list:
    """Snapshot the runner's run-status values under its lock (mirror _liveness_busy's read) so a
    concurrent start/finish can't raise 'dictionary changed size during iteration'."""
    if lock is None:
        return list(runs.values())
    with lock:
        return list(runs.values())


def _status_payload(relation_cache, memory_limit, inflight: int, runs, lock, started_at: float,
                    profile_runs=None, profile_lock=None) -> dict:
    """Assemble the kernel /status body. Module-level + pure so it's unit-testable without a live kernel."""
    snapshot = _runs_snapshot(runs, lock)
    if profile_runs is not None:
        snapshot.extend(_runs_snapshot(profile_runs, profile_lock))
    out = {
        "relationCache": relation_cache.stats(),
        "memoryLimit": memory_limit,
        "uptimeSeconds": max(0.0, time.monotonic() - started_at),
        "inflight": inflight,
        "activeRuns": sum(1 for s in snapshot if getattr(s, "status", None) in ("queued", "running")),
    }
    rss = _rss_bytes()
    if rss is not None:
        out["memoryRssBytes"] = rss
    return out


def _cancel_owned_run(run_runner, profile_runner, run_id: str, persisted: dict | None):
    """Cancel on the runner selected by the durable job type, not by the receiving hub's memory."""
    is_profile = (persisted or {}).get(
        "job_type", (persisted or {}).get("jobType")) == "profile"
    if not is_profile:
        try:
            profile_runner.status(run_id)
            is_profile = True
        except KeyError:
            pass
    if is_profile:
        try:
            return profile_runner.cancel(run_id)
        except KeyError:
            # A fenced replacement kernel cannot own the old in-memory scope. Preserve the last durable
            # status; the hub will not route here unless this kernel still owns the run's lease.
            if persisted is not None:
                from hub.models import RunStatus
                return RunStatus(**persisted)
            raise
    return run_runner.cancel(run_id)


def _cancel_with_profile_admission(
        run_runner, profile_runner, run_id: str, persisted: dict | None,
        profile_admission_lock, refresh_persisted=None):
    """Serialize a profile cancel with its consume->process-registration critical section."""
    is_profile = (persisted or {}).get(
        "job_type", (persisted or {}).get("jobType")) == "profile"
    if is_profile:
        with profile_admission_lock:
            current = refresh_persisted() if refresh_persisted is not None else persisted
            return _cancel_owned_run(run_runner, profile_runner, run_id, current)
    return _cancel_owned_run(run_runner, profile_runner, run_id, persisted)


def _start_admitted_profile(
        *, profile_runner, graph, node_id: str, plan_digest: str, run_id: str,
        request_id: str | None, profile_attempt_order: int, persist_failure) -> object:
    """Register/spawn an admitted profile or terminalize a proven pre-spawn failure.

    The caller holds the kernel's profile admission mutex. If a subprocess runner retains a possibly
    live child after a post-Popen setup error, ``status`` remains available and is returned unchanged;
    only a failure with no retained worker ownership is safe to publish as terminal.
    """
    from hub.models import PerNodeStatus, RunStatus

    try:
        return profile_runner.run(
            graph, node_id, plan_digest=plan_digest,
            profile_attempt_order=profile_attempt_order, run_id=run_id,
            request_id=request_id,
        )
    except Exception as exc:
        try:
            return profile_runner.status(run_id)
        except KeyError:
            if getattr(exc, "reaped", True) is False:
                raise RuntimeError(
                    "profile supervisor lost ownership of a possibly live child") from exc
            failed = RunStatus(
                run_id=run_id, status="failed", job_type="profile",
                target_node_id=node_id, plan_digest=plan_digest,
                profile_attempt_order=profile_attempt_order, request_id=request_id,
                per_node=[PerNodeStatus(
                    node_id=node_id, status="failed", label="Full profile",
                    error=f"{type(exc).__name__}: {exc}",
                )],
                error=f"{type(exc).__name__}: {exc}",
            )
            retain_failure = getattr(profile_runner, "retain_terminal_failure", None)
            if callable(retain_failure):
                return retain_failure(graph, failed, persist_failure)
            persist_failure(graph, failed)
            return failed


def _admitted_kernel_graph(body: RunBody, *, kernel_canvas: str, deps, metadata):
    """Validate a kernel command against its durable admission and reopen exact Source bindings."""
    from hub.local_run_inputs import LocalRunInputError, bind_manifest, validate_manifest, validate_manifest_graph
    from hub.models import Graph

    try:
        admission = metadata.local_run_input_admission(body.run_id)
    except Exception as exc:
        raise LocalRunInputError("kernel run input admission is unavailable") from exc
    if (admission is None or admission.get("run_id") != body.run_id
            or admission.get("canvas_id") != kernel_canvas
            or admission.get("target_node_id") != body.target):
        raise LocalRunInputError("kernel run does not match its persisted input admission")
    manifest = validate_manifest(body.input_manifest)
    persisted = validate_manifest(admission.get("manifest"))
    if manifest != persisted:
        raise LocalRunInputError("kernel transport manifest does not match its persisted admission")
    try:
        graph = Graph(**body.graph)
    except Exception as exc:
        raise LocalRunInputError("kernel graph payload is malformed") from exc
    if graph.id != kernel_canvas:
        raise LocalRunInputError("kernel graph does not match its canvas")
    validate_manifest_graph(graph, body.target, manifest, require_bound_revisions=False)
    graph = bind_manifest(graph, body.target, manifest, deps.resolve_adapter)
    validate_manifest_graph(graph, body.target, manifest, require_bound_revisions=True)
    return graph, manifest


def _dispatch_profile_job(
        *, body: ProfileJobBody, kernel_canvas: str, kernel_id: str,
        profile_runner, profile_admission_lock, metadata) -> object:
    """Validate the kernel's canvas, consume admission, then register exactly one profile child."""
    from hub.models import Graph, RunStatus

    graph = Graph(**body.graph)
    if graph.id != kernel_canvas:
        raise RuntimeError("profile graph does not match the kernel canvas")
    with profile_admission_lock:
        won, durable = metadata.consume_profile_run_preallocation(
            body.run_id, body.admission_token, canvas_id=kernel_canvas, kernel_id=kernel_id,
            target_node_id=body.node_id, plan_digest=body.plan_digest,
        )
        if not won:
            return RunStatus(**durable)
        profile_attempt_order = durable.get(
            "profile_attempt_order", durable.get("profileAttemptOrder"))
        if not isinstance(profile_attempt_order, int) or profile_attempt_order < 1:
            raise RuntimeError("profile admission is missing its attempt order")
        return _start_admitted_profile(
            profile_runner=profile_runner, graph=graph, node_id=body.node_id,
            plan_digest=body.plan_digest, run_id=body.run_id,
            request_id=body.request_id,
            profile_attempt_order=profile_attempt_order,
            persist_failure=lambda _graph, st: metadata.save_run_state(
                st.run_id, st.model_dump(), canvas_id=kernel_canvas,
                kernel_id=kernel_id),
        )


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
    # A kernel is a service process too: production metadata must already be migrated by the one-shot
    # release command. Local/disposable SQLite keeps its serialized auto-initialization behavior.
    metadb.init_db()
    deps = set_workspace(args.workspace, args.data_dir, maintain_storage=False)
    warm = RelationCache()  # per-kernel warm cache of preview intermediate relations (dropped on restart)
    # cell-crash-isolation: full RUNS execute in a killable, deadline-bounded child PROCESS by default, so
    # a runaway/segfaulting/OOM cell kills only that run — the warm kernel (and its live previews) survive.
    # Previews/sample profiles stay in-process on the warm engine (fast, interactive), protected by the
    # reaper. Whole-dataset profiles use profile_runner so their own DuckDB scope is independently cancellable.
    # Opt out with DP_KERNEL_ISOLATE_RUNS=0 (runs on the warm in-process engine = the old behavior).
    _isolate = os.environ.get("DP_KERNEL_ISOLATE_RUNS", "1").strip().lower() not in ("0", "false", "no", "off")
    run_runner = deps.runner
    if _isolate:
        run_runner = next((r for r in deps.runners if getattr(r, "name", "") == "local-subprocess"), deps.runner)
    # single-writer: the kernel persists run_states stamped with OUR kernel_id (so the boot-time reaper
    # spares this run while we're alive). Wire it onto the runner that OWNS runs (the isolated child
    # manager when isolating), since that's what emits queued/progress/terminal for /run.
    run_runner.on_status = lambda g, st: _persist_kernel_run_state(
        metadb, g, st, kernel_id=kid)
    profile_runner = deps.profile_runner
    profile_runner.on_status = lambda g, st: metadb.save_run_state(
        st.run_id, st.model_dump(), canvas_id=getattr(g, "id", None), kernel_id=kid)
    # Serialize the narrow consume->runner-registration window with profile cancellation. Once consume
    # commits, a cross-hub cancel waits until either the process is registered (and can be killed) or a
    # proven pre-spawn failure is durable; accepted cancel intent is never answered from stale queued state.
    _profile_admission_lock = threading.Lock()

    last_activity = [time.monotonic()]
    # In-process preview/sample-profile requests do not show up in either runner, so count them explicitly.
    # Whole-dataset profile jobs live in profile_runner.runs and are included in the watchdog below.
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
                retry_result_fences = getattr(deps.storage, "retry_result_fences", None)
                if callable(retry_result_fences):
                    retry_result_fences(50)  # only this process can close its inherited writer/read FDs
                active = _runs_snapshot(run_runner.runs, getattr(run_runner, "_lock", None))
                active.extend(_runs_snapshot(profile_runner.runs, getattr(profile_runner, "_lock", None)))
                busy = _liveness_busy(_inflight[0], {st.run_id: st for st in active})
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
        try:
            yield
        finally:
            from hub.observability import drain_sinks
            drain_sinks()

    app = FastAPI(lifespan=lifespan)

    @app.post("/run")
    def run(body: RunBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        last_activity[0] = time.monotonic()
        from hub.local_run_inputs import LocalRunInputError
        from hub.models import RunStatus
        try:
            graph, manifest = _admitted_kernel_graph(
                body, kernel_canvas=canvas, deps=deps, metadata=metadb)
        except LocalRunInputError as exc:
            failed = metadb.fail_claimed_local_run_dispatch(
                body.run_id, f"{type(exc).__name__}: {exc}")
            return RunStatus(**failed).model_dump()
        _ensure_deps(graph)
        plan = compiler.compile_plan(graph, body.target, deps.registry, deps.node_specs, deps.node_ir)
        run_kwargs = {"run_id": body.run_id, "request_id": body.request_id,
                      "attempt_id": body.attempt_id}
        if _isolate:
            run_kwargs["input_manifest"] = manifest
        st = run_runner.run(plan, graph, body.target, body.placement, **run_kwargs)
        return st.model_dump()

    @app.post("/preview")
    def preview(body: PreviewBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        from hub.executors.preview import preview_node
        graph = Graph(**body.graph)
        _ensure_deps(graph)
        with _inflight_work():
            return preview_node(graph, body.node_id, body.k, deps.resolve_adapter,
                                deps.registry, deps.node_builders, deps.node_specs, offset=body.offset,
                                cache=warm, storage=deps.storage,
                                port_id=body.port_id).model_dump()

    @app.post("/profile")
    def profile(body: PreviewBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        from hub.executors.profile import profile_node
        graph = Graph(**body.graph)
        _ensure_deps(graph)
        with _inflight_work():
            return profile_node(graph, body.node_id, deps.resolve_adapter, deps.registry,
                                deps.node_builders, deps.node_specs, cache=warm, full=body.full,
                                storage=deps.storage, port_id=body.port_id).model_dump()

    @app.post("/profile-job")
    def profile_job(body: ProfileJobBody, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        last_activity[0] = time.monotonic()
        status = _dispatch_profile_job(
            body=body, kernel_canvas=canvas, kernel_id=kid,
            profile_runner=profile_runner,
            profile_admission_lock=_profile_admission_lock, metadata=metadb,
        )
        return status.model_dump()

    @app.post("/cancel")
    def cancel(body: dict, x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        run_id = str(body["run_id"])
        persisted = metadb.get_run_state(run_id)
        return _cancel_with_profile_admission(
            run_runner, profile_runner, run_id, persisted,
            _profile_admission_lock, lambda: metadb.get_run_state(run_id),
        ).model_dump()

    @app.get("/status")
    def status(x_dp_kernel_token: str = Header(None)):
        _auth(x_dp_kernel_token)
        with _inflight_lock:
            inflight = _inflight[0]
        mem = os.environ.get("DP_MEMORY_LIMIT") or os.environ.get("DP_KERNEL_MEM")
        return _status_payload(
            warm, mem, inflight, run_runner.runs, getattr(run_runner, "_lock", None), _STARTED_AT,
            profile_runner.runs, getattr(profile_runner, "_lock", None),
        )

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
