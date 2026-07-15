from __future__ import annotations

import signal
import subprocess

from hub.process_scope import OwnedProcessScope


class _DirectProcess:
    def __init__(self, *, running: bool = True):
        self.returncode = None if running else 0
        self.calls: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("TERM")

    def kill(self):
        self.calls.append("KILL")
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        self.calls.append(f"wait:{timeout}")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("child", timeout)
        return self.returncode


def test_scope_uses_shared_term_then_kill_and_releases_exact_process():
    proc = _DirectProcess()
    scope = OwnedProcessScope(proc, owns_process_group=False)

    assert scope.fence(term_grace_s=0, kill_wait_s=0.1)
    assert proc.calls == ["TERM", "KILL", "wait:0.1"]
    assert not scope.owned
    assert scope.request_stop(force=True) is False
    assert proc.calls == ["TERM", "KILL", "wait:0.1"]


def test_released_posix_scope_never_signals_a_reused_group(monkeypatch):
    proc = _DirectProcess(running=False)
    proc.pid = 424242
    signals = []

    def killpg(pgid, sig):
        signals.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr("hub.process_scope.os.killpg", killpg)
    scope = OwnedProcessScope(proc, owns_process_group=True)

    assert scope.fence(term_grace_s=0, kill_wait_s=0)
    released_signals = list(signals)
    assert scope.request_stop() is False
    assert scope.request_stop(force=True) is False
    assert signals == released_signals
