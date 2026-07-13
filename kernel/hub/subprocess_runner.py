"""An execution backend that runs each job in a SEPARATE OS PROCESS.

Isolation, for real: the kernel stays responsive while a job runs, a runaway / segfaulting /
OOM-killed job can't take the kernel down (the parent just sees the child exit), and cancel is a
hard kill. Same plan, same engine — the child (kernel/subrun.py) rebuilds Deps for the workspace and
runs the in-process LocalRunner, writing status JSON to a file the parent polls. A dedicated child
entrypoint (not multiprocessing 'spawn') keeps this robust however the kernel was launched. (pod /
Ray backends would be plugins over this same ExecutionBackend protocol.)
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from hub.models import CompilePlan, Graph, PerNodeStatus, Placement, RunEstimate, RunStatus
from hub.plugins.runner import _CONFIRM_ROWS, _MAX_RUNS

_CANCEL_GRACE_S = 2.0  # cooperative child cancel first; then SIGTERM/SIGKILL for runaway native/Python code


def _subrun_child_env() -> dict[str, str]:
    """A one-shot worker does not own control-plane persistence and receives no metadata identity."""
    from hub.workload_env import build_workload_env
    return build_workload_env(include_metadata_db=False)


class SubprocessRunner:
    name = "local-subprocess"

    def __init__(self, workspace: str, data_dir: str, catalog=None, deadline_s: float | None = None,
                 storage=None, resolve_adapter=None, node_builders=None):
        self.workspace = workspace
        self.data_dir = data_dir
        self.catalog = catalog  # register outputs written by children into the parent's live catalog
        if storage is None:
            from hub.storage import make_storage
            storage = make_storage(workspace)
        self.storage = storage
        self.resolve_adapter = resolve_adapter
        self.node_builders = node_builders if node_builders is not None else {}
        self.result_put = None  # optional parent DB cache publication after RunState owns the result
        self.on_complete = None  # optional (graph, target, status) hook — Deps wires it to run-history
        self.on_status = None    # optional (graph, status) hook — Deps wires it to DB-backed live status
        self.runs: dict[str, RunStatus] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancel_files: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._object_results: dict[str, dict] = {}
        self._lock = threading.Lock()
        # wall-clock deadline: a child that runs longer than this is hard-killed and the run fails, so a
        # runaway cell (`while True`, a livelocked native op) can't pin a worker forever. <=0 disables.
        try:
            self.deadline_s = deadline_s if deadline_s is not None else float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
        except ValueError:
            self.deadline_s = 3600.0
        atexit.register(self._terminate_all)  # don't orphan running children when the kernel exits

    def _terminate_all(self) -> None:
        """Fence, reap, then discard parent-owned writers during an orderly interpreter shutdown.

        SIGKILL cannot run this hook; its writing attempts deliberately remain unowned for operator
        reconciliation because lease expiry alone is not proof that the child writer stopped.
        """
        with self._lock:
            procs = list(self._procs.items())
            self._cancelled.update(run_id for run_id, _proc in procs)
        for _run_id, proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        for run_id, proc in procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception:  # noqa: BLE001
                continue
            owned = self._object_results.get(run_id)
            if owned is not None:
                from hub.handoff import discard_attempt
                discard_attempt(owned["uri"])

    def reachable_tiers(self) -> tuple:
        # every subprocess backend is a SAME-HOST child sharing the workspace filesystem, so it reaches the
        # local tier (a same-host handoff needs no object store) as well as a configured object store.
        # Declared on the base so any same-host subprocess subclass (e.g. PoolRunner) is covered — a named
        # backend otherwise defaults to object-only and the controller would refuse a valid local handoff.
        return ("local", "object")

    def can_run(self, plan: CompilePlan) -> bool:
        return plan.acyclic

    def estimate(self, plan: CompilePlan, rows: int | None, byts: int | None = None) -> RunEstimate:
        from hub.plugins.runner import _CONFIRM_BYTES, _fmt_bytes
        if rows is None and byts is None:  # uncountable → unreadable → fails fast; no fabricated ETA, no gate
            return RunEstimate(rows=None, bytes=None, placement="local", needs_confirm=False,
                               breakdown=f"size unknown · {len(plan.steps)} steps · isolated process")
        needs = (byts is not None and byts >= _CONFIRM_BYTES) or (rows is not None and rows >= _CONFIRM_ROWS)
        size = _fmt_bytes(byts) if byts is not None else "size unknown"
        rowstr = f"{rows:,} rows" if rows is not None else "unknown rows"
        return RunEstimate(rows=rows, bytes=byts, placement="local", needs_confirm=needs,
                           breakdown=f"{size} · {rowstr} · {len(plan.steps)} steps · isolated process")

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement, run_id: str | None = None) -> RunStatus:
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"  # a kernel passes the hub-minted id (authoritative)
        per = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        status = RunStatus(run_id=run_id, status="queued", placement="local", per_node=per)
        job_extra: dict = {"runId": run_id}
        target = next((node for node in graph.nodes if node.id == target_node_id), None)
        if target is not None and target.type not in ("write", "assert"):
            logical_uri = self.storage.output_uri(
                f"__result_{run_id}", ".parquet")
            from hub.plugins.adapters import is_object_uri
            if is_object_uri(logical_uri):
                if self.resolve_adapter is None:
                    raise RuntimeError(
                        "object-backed subprocess results require a parent adapter resolver")
                if self.on_status is None:
                    raise RuntimeError(
                        "object-backed subprocess results require authoritative parent run persistence")
                from hub.plan_key import plan_cacheable, plan_hash
                phash = plan_hash(graph, target_node_id, self.resolve_adapter)
                cacheable = plan_cacheable(graph, target_node_id, self.node_builders)
                logical_uri = self.storage.output_uri(f"__result_{phash}", ".parquet")
                from hub.handoff import allocate_attempt, physical_attempt_uri
                handle = allocate_attempt(
                    logical_uri=logical_uri, kind="region", run_id=run_id,
                    allocation_key=f"subprocess-full-result:{run_id}:{phash}",
                    uri_factory=lambda namespace, generation, attempt_id: physical_attempt_uri(
                        logical_uri, namespace, generation, attempt_id),
                )
                self._object_results[run_id] = {
                    "uri": handle["uri"], "cache_key": phash if cacheable else None,
                }
                job_extra["forcedResultUri"] = handle["uri"]
        try:
            return self._spawn(status, job_extra, graph, target_node_id)
        except Exception:
            owned = self._object_results.pop(run_id, None)
            if owned is not None:
                from hub.handoff import discard_attempt
                discard_attempt(owned["uri"])
            raise

    def run_unit(self, graph: Graph, output_node: str, output_uri: str, requires=None) -> RunStatus:
        """Run a placement region's sub-graph in a worker PROCESS and materialize output_node's relation
        to output_uri (no catalog registration). This is how a placed region executes on its worker —
        the seam a pod/Ray backend overrides to allocate a pod / submit a job. `requires` (the region's
        resource need) is accepted for signature parity but ignored: a subprocess is one local process,
        so there's no worker to place onto."""
        run_id = f"unit_{uuid.uuid4().hex[:10]}"
        status = RunStatus(run_id=run_id, status="queued", placement="local", per_node=[])
        return self._spawn(status, {"materializeUri": output_uri}, graph, output_node)

    def _spawn(self, status: RunStatus, job_extra: dict, graph: Graph, target: str | None) -> RunStatus:
        from hub.workload_env import prepare_workload_graph

        run_id = status.run_id
        job_dir = tempfile.mkdtemp(prefix="dp-run-")
        status_file = os.path.join(job_dir, "status.json")
        cancel_file = os.path.join(job_dir, "cancel.requested")
        job_file = os.path.join(job_dir, "job.json")
        with open(job_file, "w") as f:
            json.dump({"workspace": self.workspace, "dataDir": self.data_dir,
                       "graph": prepare_workload_graph(graph),
                       "target": target, "statusFile": status_file,
                       "cancelFile": cancel_file, **job_extra}, f)
        # A one-shot worker gets only runtime/data capabilities, never the hub metadata identity or
        # ambient signing/bootstrap/provider secrets. It creates a disposable local metadata DB itself.
        try:
            proc = subprocess.Popen([sys.executable, "-m", "hub.subrun", job_file],
                                    env=_subrun_child_env())
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        try:
            with self._lock:
                self.runs[run_id] = status
                self._procs[run_id] = proc
                self._cancel_files[run_id] = cancel_file
                self._evict()
            self._emit(graph, status)  # persist 'queued' to the DB (pollable on any instance / after restart)
            threading.Thread(
                target=self._watch,
                args=(run_id, proc, status_file, job_dir, graph, target),
                daemon=True,
            ).start()
            return status
        except Exception:
            # Once Popen succeeds the child may be writing. Reap it before the caller terminalizes the
            # parent-owned attempt; setup failure alone is not writer terminal proof.
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            finally:
                with self._lock:
                    self.runs.pop(run_id, None)
                    self._procs.pop(run_id, None)
                    self._cancel_files.pop(run_id, None)
                shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def _emit(self, graph: Graph, status: RunStatus, *, strict: bool = False) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001
                if strict:
                    raise

    def _complete(self, graph: Graph, target: str | None, status: RunStatus) -> None:
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001 — RunState already owns managed results
                pass

    def _evict(self) -> None:
        """Bound self.runs (called under self._lock) — subprocess runs accumulated forever otherwise.
        Evict only TERMINAL runs (oldest first); never drop a run whose child is still executing."""
        _terminal = {"done", "failed", "cancelled"}
        while len(self.runs) > _MAX_RUNS:
            victim = next((rid for rid, st in self.runs.items() if st.status in _terminal), None)
            if victim is None:
                break  # all retained runs are still live — exceed the cap rather than drop one
            self.runs.pop(victim, None)
            self._cancelled.discard(victim)
            self._procs.pop(victim, None)
            self._cancel_files.pop(victim, None)

    def _read(self, run_id: str, status_file: str) -> RunStatus | None:
        """Merge progress, but hold a child terminal status for parent-side finalization."""
        try:
            with open(status_file) as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None
        observed = RunStatus(**{**payload, "run_id": run_id})  # the child had its own run id
        if observed.status in ("done", "failed", "cancelled"):
            return observed
        if run_id not in self._cancelled:
            self.runs[run_id] = observed
        return None

    def _watch(self, run_id: str, proc: subprocess.Popen, status_file: str, job_dir: str,
               graph: Graph, target: str | None) -> None:
        start = time.monotonic()
        deadline_hit = False
        cancel_seen_at = None
        last = None
        terminal = None
        while True:
            terminal = self._read(run_id, status_file)
            if terminal is not None:
                break
            # mirror INTERMEDIATE progress to the DB: the kernel poll path reads run_states (not our
            # in-memory dict), so without this the row would sit at 'queued' for the whole run body.
            cur = self.runs.get(run_id)
            if cur is not None:
                dump = cur.model_dump()
                if dump != last:
                    self._emit(graph, cur)
                    last = dump
            if proc.poll() is not None:      # child exited — do a final read then stop
                time.sleep(0.1)
                terminal = self._read(run_id, status_file)
                break
            if run_id in self._cancelled:
                cancel_seen_at = cancel_seen_at or time.monotonic()
                if time.monotonic() - cancel_seen_at > _CANCEL_GRACE_S:
                    # The cooperative request reaches LocalRunner's cursor interrupt + pre-publish fence.
                    # A runaway Python/native operation may ignore it; terminate only after the grace window.
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except OSError:
                            pass  # exited between poll and signal
                    time.sleep(0.1)
                    self._read(run_id, status_file)
                    break
            if self.deadline_s and self.deadline_s > 0 and time.monotonic() - start > self.deadline_s:
                deadline_hit = True           # runaway — hard-kill the child and fail the run
                if proc.poll() is None:
                    proc.terminate()
                time.sleep(0.1)
                terminal = self._read(run_id, status_file)
                break
            time.sleep(0.15)
        try:
            proc.wait(timeout=2 if run_id in self._cancelled else 5)
        except subprocess.TimeoutExpired:
            proc.kill()  # SIGTERM ignored (e.g. a C-level DuckDB loop) → force-reap so _watch can't hang
            proc.wait()
        terminal = self._read(run_id, status_file) or terminal
        current = self.runs.get(run_id)
        st = terminal or (current.model_copy(deep=True) if current is not None else None)
        forced = bool(st and st.status in ("queued", "running"))  # exited without a terminal status
        if forced:
            if run_id in self._cancelled:
                st.status = "cancelled"                 # a hard-killed cancel, not a failure (user intent wins)
            elif deadline_hit:
                st.status = "failed"
                st.error = st.error or f"run exceeded the wall-clock deadline of {self.deadline_s:.0f}s — killed"
            else:
                st.status = "failed"                    # crash / OOM / unexpected exit
                st.error = st.error or f"execution process exited (code {proc.returncode})"
        owned_result = self._object_results.get(run_id)
        object_terminal_persisted = False
        if owned_result is not None and st is not None:
            attempt_uri = owned_result["uri"]
            cancelled = run_id in self._cancelled
            valid_child_commit = (
                not cancelled and st.status == "done" and proc.returncode == 0
                and st.output_uri == attempt_uri
            )
            if valid_child_commit:
                try:
                    from hub.handoff import prepare_attempt_commit
                    prepare_attempt_commit(attempt_uri)
                    st.output_uri = attempt_uri
                    st.output_table = None
                except Exception as exc:  # noqa: BLE001 - parent commit is the publication boundary
                    from hub import metadb
                    from hub.handoff import discard_attempt
                    if not metadb.abandon_committed_object_attempt(attempt_uri):
                        discard_attempt(attempt_uri)
                    st.status = "failed"
                    st.error = f"parent object-result commit failed: {type(exc).__name__}: {exc}"
                    st.output_uri = st.output_table = None
            else:
                from hub.handoff import discard_attempt
                discard_attempt(attempt_uri)  # child is reaped, so writer terminal proof is valid
                st.output_uri = st.output_table = None
                if cancelled:
                    st.status = "cancelled"
        # a subprocess run wrote its output in the CHILD's catalog (discarded) — register it here so
        # it shows up in the parent's live catalog, just like an in-process run.
        if st and st.status == "done" and st.output_uri and st.output_table and self.catalog is not None:
            try:
                self.catalog.register_output(name=st.output_table, uri=st.output_uri,
                                             parents=[], pipeline="canvas")  # content-addressed version
            except Exception:  # noqa: BLE001
                pass
        # Finalize before publishing the terminal status. Otherwise a caller can observe `done` and query
        # the catalog while parent-side output registration is still in flight.
        # Persist run history here (the child disables its own on_complete to avoid a daemon-thread
        # race). We read the terminal status from the child's atomically-written status file, or the
        # status we forced above on a crash/cancel — recording every terminal run, like the in-process
        # backend, with no double-write.
        if st is not None and st.status in ("done", "failed", "cancelled"):
            if owned_result is not None and st.status == "done":
                try:
                    # This strict parent RunState transaction publishes the committed region attempt
                    # and establishes its primary owner. History and cache are optional after this point.
                    self._emit(graph, st, strict=True)
                    object_terminal_persisted = True
                    self._complete(graph, target, st)
                    cache_key = owned_result.get("cache_key")
                    if cache_key and self.result_put:
                        try:
                            self.result_put(cache_key, {
                                "rows": st.total_rows or st.rows_processed or 0,
                                "uri": st.output_uri, "table": None,
                            })
                        except Exception:  # RunState already owns the exact result
                            pass
                except Exception as exc:  # primary terminal persistence did not commit
                    from hub import metadb
                    metadb.abandon_committed_object_attempt(owned_result["uri"])
                    st.status = "failed"
                    st.error = f"parent object-result publication failed: {type(exc).__name__}: {exc}"
                    st.output_uri = st.output_table = None
            if not object_terminal_persisted:
                self._complete(graph, target, st)
                self._emit(graph, st)
            with self._lock:
                self.runs[run_id] = st
        shutil.rmtree(job_dir, ignore_errors=True)
        with self._lock:
            self._procs.pop(run_id, None)
            self._cancel_files.pop(run_id, None)
            self._object_results.pop(run_id, None)

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel_acknowledged(self, run_id: str) -> bool:
        """True only once a cancelled child's process is observably gone/reaped."""
        st = self.runs.get(run_id)
        if st is None or st.status != "cancelled":
            return False
        with self._lock:
            proc = self._procs.get(run_id)
        return proc is None or proc.poll() is not None

    def cancel(self, run_id: str) -> RunStatus:
        with self._lock:
            self._cancelled.add(run_id)  # hard-kill fallback resolves as cancelled, not failed
            cancel_file = self._cancel_files.get(run_id)
        if cancel_file:
            try:
                with open(cancel_file, "x"):
                    pass
            except FileExistsError:
                pass
            except OSError:
                pass  # watcher still hard-kills after the bounded grace period
        # _watch publishes `cancelled` only after wait()/kill() has reaped the child. Until then the status
        # remains non-terminal, making terminal status a real stop acknowledgement rather than an optimistic
        # label while the process could still commit an output.
        return self.runs[run_id]
