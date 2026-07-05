"""An execution backend that runs each job in a SEPARATE OS PROCESS.

Isolation, for real: the kernel stays responsive while a job runs, a runaway / segfaulting /
OOM-killed job can't take the kernel down (the parent just sees the child exit), and cancel is a
hard kill. Same plan, same engine — the child (kernel/subrun.py) rebuilds Deps for the workspace and
runs the in-process LocalRunner, writing status JSON to a file the parent polls. A dedicated child
entrypoint (not multiprocessing 'spawn') keeps this robust however the kernel was launched. (pod /
Ray backends would be plugins over this same ExecutionBackend protocol.)
"""

from __future__ import annotations

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
from kernel.plugins.runner import _CONFIRM_ROWS, _OP_SECONDS_PER_1K


class SubprocessRunner:
    name = "local-subprocess"

    def __init__(self, workspace: str, data_dir: str):
        self.workspace = workspace
        self.data_dir = data_dir
        self.runs: dict[str, RunStatus] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def can_run(self, plan: CompilePlan) -> bool:
        return plan.acyclic

    def estimate(self, plan: CompilePlan, rows: int) -> RunEstimate:
        seconds = max(0.15, sum(_OP_SECONDS_PER_1K.get(s.kind, 0.02) * (rows / 1000.0) for s in plan.steps))
        return RunEstimate(rows=rows, seconds=round(seconds, 2), placement="local",
                           needs_confirm=rows >= _CONFIRM_ROWS,
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
        threading.Thread(target=self._watch, args=(run_id, proc, status_file, job_dir), daemon=True).start()
        return status

    def _read(self, run_id: str, status_file: str) -> bool:
        """Merge the child's latest status; return True once it's terminal."""
        try:
            with open(status_file) as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return False
        self.runs[run_id] = RunStatus(**{**payload, "run_id": run_id})  # the child had its own run id
        return self.runs[run_id].status in ("done", "failed", "cancelled")

    def _watch(self, run_id: str, proc: subprocess.Popen, status_file: str, job_dir: str) -> None:
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
        if st and st.status in ("queued", "running"):  # exited without a terminal status (crash/OOM/kill)
            st.status = "failed"
            st.error = st.error or f"execution process exited (code {proc.returncode})"
        shutil.rmtree(job_dir, ignore_errors=True)
        with self._lock:
            self._procs.pop(run_id, None)

    def status(self, run_id: str) -> RunStatus:
        return self.runs[run_id]

    def cancel(self, run_id: str) -> RunStatus:
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()  # hard kill — the isolation payoff
        st = self.runs[run_id]
        if st.status in ("queued", "running"):
            st.status = "cancelled"
        return st
