"""Small, in-memory ownership contract for one workload process scope.

On POSIX, callers start the direct child in a new session and retain its PID as the
owned process-group ID.  Unsupported non-POSIX platforms fall back to terminating
and reaping only the direct child; descendants cannot be fenced there.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any

PROCESS_TERM_GRACE_S = 2.0
PROCESS_KILL_WAIT_S = 5.0
_POLL_S = 0.02


def owned_process_popen_kwargs(kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return Popen kwargs that create an owned process group where supported."""
    result = dict(kwargs or {})
    if os.name == "posix":
        result["start_new_session"] = True
    return result


def _proc_starttime(pid: int) -> int | None:
    """Linux process-start fingerprint used to refuse killpg after PID reuse.

    ``Popen.poll()`` reaps the direct child. Once that PID is free, a later
    ``killpg(pid, ...)`` can hit an unrelated new session leader. The kernel
    starttime field distinguishes "our former leader" from a recycled PID.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    # stat: pid (comm) state ... starttime is field 22. ``comm`` may contain spaces
    # and parentheses, so split only after the final ')'.
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    fields = data[rparen + 1:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


class OwnedProcessScope:
    """Exact ownership of one Popen and, on POSIX, the session it leads."""

    def __init__(self, process: subprocess.Popen, *, owns_process_group: bool):
        self.process = process
        pid = getattr(process, "pid", None)
        # Popen always has a PID.  The no-PID case keeps lightweight test doubles on
        # the documented direct-child fallback without inventing a numeric PGID.
        self._pgid = pid if owns_process_group and isinstance(pid, int) and pid > 0 else None
        self._starttime = _proc_starttime(self._pgid) if self._pgid is not None else None
        self._owned = True
        self._term_sent_at: float | None = None
        self._lock = threading.Lock()
        self._fence_lock = threading.Lock()

    @property
    def owned(self) -> bool:
        with self._lock:
            return self._owned

    def _scope_exists_locked(self) -> bool:
        if not self._owned:
            return False
        if self._pgid is None:
            return self.process.poll() is None
        if not self._group_signal_target_locked():
            return False
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _group_signal_target_locked(self) -> bool:
        """True when killpg on ``_pgid`` still refers to this scope's process group."""
        if self._pgid is None:
            return False
        if self._starttime is None:
            return True
        current = _proc_starttime(self._pgid)
        if current is not None and current != self._starttime:
            # The leader PID was recycled into an unrelated process/session.
            return False
        return True

    def _signal_locked(self, *, force: bool) -> None:
        if self._pgid is not None:
            if not self._group_signal_target_locked():
                return
            try:
                os.killpg(self._pgid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass  # ESRCH means the owned group is already empty.
            return
        if self.process.poll() is not None:
            return
        if force:
            self.process.kill()
        else:
            self.process.terminate()

    def request_stop(self, *, force: bool = False) -> bool:
        """Signal only while this exact Popen scope remains owned."""
        with self._lock:
            if not self._owned:
                return False
            if not force and self._term_sent_at is not None:
                return True
            self._signal_locked(force=force)
            if not force:
                self._term_sent_at = time.monotonic()
            return True

    def fence(self, *, term_grace_s: float = PROCESS_TERM_GRACE_S,
              kill_wait_s: float = PROCESS_KILL_WAIT_S) -> bool:
        """TERM, then bounded KILL, reap the direct child, and release ownership."""
        with self._fence_lock:
            return self._fence(term_grace_s=term_grace_s, kill_wait_s=kill_wait_s)

    def _fence(self, *, term_grace_s: float, kill_wait_s: float) -> bool:
        # Reap an already-exited leader before probing its group.  Otherwise its
        # zombie alone would make killpg(..., 0) consume the full TERM grace.
        self.process.poll()
        self.request_stop()
        with self._lock:
            if not self._owned:
                return True
            term_sent_at = self._term_sent_at or time.monotonic()
        deadline = term_sent_at + max(0.0, term_grace_s)
        while True:
            # poll() both observes and reaps a direct child that exits during grace.
            self.process.poll()
            with self._lock:
                exists = self._scope_exists_locked()
            if not exists:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.request_stop(force=True)
                break
            time.sleep(min(_POLL_S, remaining))
        try:
            self.process.wait(timeout=max(0.0, kill_wait_s))
        except subprocess.TimeoutExpired:
            return False
        with self._lock:
            self._owned = False
            self._pgid = None
            self._starttime = None
        return True
