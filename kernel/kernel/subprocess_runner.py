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

from kernel.models import CompilePlan, Graph, PerNodeStatus, Placement, RunEstimate, RunStatus
from kernel.plugins.runner import _CONFIRM_ROWS, _MAX_RUNS


class SubprocessRunner:
    name = "local-subprocess"

    def __init__(self, workspace: str, data_dir: str, catalog=None):
        self.workspace = workspace
        self.data_dir = data_dir
        self.catalog = catalog  # register outputs written by children into the parent's live catalog
        self.on_complete = None  # optional (graph, target, status) hook — Deps wires it to run-history
        self.runs: dict[str, RunStatus] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        atexit.register(self._terminate_all)  # don't orphan running children when the kernel exits

    def _terminate_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def can_run(self, plan: CompilePlan) -> bool:
        return plan.acyclic

    def estimate(self, plan: CompilePlan, rows: int | None) -> RunEstimate:
        if rows is None:  # unknown size (uncountable → unreadable → fails fast) — no fabricated ETA, no gate
            return RunEstimate(rows=None, placement="local", needs_confirm=False,
                               breakdown=f"size unknown · {len(plan.steps)} steps · isolated process")
        return RunEstimate(rows=rows, placement="local", needs_confirm=rows >= _CONFIRM_ROWS,
                           breakdown=f"{rows:,} rows · {len(plan.steps)} steps · isolated process")

    def run(self, plan: CompilePlan, graph: Graph, target_node_id: str | None,
            placement: Placement) -> RunStatus:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        per = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        status = RunStatus(run_id=run_id, status="queued", placement="local", per_node=per)
        job_dir = tempfile.mkdtemp(prefix="dp-run-")
        status_file = os.path.join(job_dir, "status.json")
        job_file = os.path.join(job_dir, "job.json")
        with open(job_file, "w") as f:
            json.dump({"workspace": self.workspace, "dataDir": self.data_dir,
                       "graph": graph.model_dump(), "target": target_node_id,
                       "statusFile": status_file}, f)
        proc = subprocess.Popen([sys.executable, "-m", "kernel.subrun", job_file])
        with self._lock:
            self.runs[run_id] = status
            self._procs[run_id] = proc
            self._evict()
        threading.Thread(target=self._watch, args=(run_id, proc, status_file, job_dir, graph, target_node_id), daemon=True).start()
        return status

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

    def _read(self, run_id: str, status_file: str) -> bool:
        """Merge the child's latest status; return True once it's terminal."""
        if run_id in self._cancelled:
            return True  # cancel already set the terminal state; don't let a stale 'running' file overwrite it
        try:
            with open(status_file) as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return False
        self.runs[run_id] = RunStatus(**{**payload, "run_id": run_id})  # the child had its own run id
        return self.runs[run_id].status in ("done", "failed", "cancelled")

    def _watch(self, run_id: str, proc: subprocess.Popen, status_file: str, job_dir: str,
               graph: Graph, target: str | None) -> None:
        while True:
            if self._read(run_id, status_file):
                break
            if proc.poll() is not None:      # child exited — do a final read then stop
                time.sleep(0.1)
                self._read(run_id, status_file)
                break
            time.sleep(0.15)
        proc.wait()
        st = self.runs.get(run_id)
        forced = bool(st and st.status in ("queued", "running"))  # exited without a terminal status
        if forced:
            if run_id in self._cancelled:
                st.status = "cancelled"                 # a hard-killed cancel, not a failure
            else:
                st.status = "failed"                    # crash / OOM / unexpected exit
                st.error = st.error or f"execution process exited (code {proc.returncode})"
        # a subprocess run wrote its output in the CHILD's catalog (discarded) — register it here so
        # it shows up in the parent's live catalog, just like an in-process run.
        if st and st.status == "done" and st.output_uri and st.output_table and self.catalog is not None:
            try:
                self.catalog.register_output(name=st.output_table, uri=st.output_uri, version="v1",
                                             parents=[], pipeline="canvas")
            except Exception:  # noqa: BLE001
                pass
        # Persist run history here (the child disables its own on_complete to avoid a daemon-thread
        # race). We read the terminal status from the child's atomically-written status file, or the
        # status we forced above on a crash/cancel — recording every terminal run, like the in-process
        # backend, with no double-write.
        if st is not None and self.on_complete and st.status in ("done", "failed", "cancelled"):
            try:
                self.on_complete(graph, target, st)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(job_dir, ignore_errors=True)
        with self._lock:
            self._procs.pop(run_id, None)

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel(self, run_id: str) -> RunStatus:
        self._cancelled.add(run_id)  # so _watch reports 'cancelled', not 'failed', for the hard-killed child
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()  # hard kill — the isolation payoff
        st = self.runs[run_id]
        if st.status in ("queued", "running"):
            st.status = "cancelled"
        return st
