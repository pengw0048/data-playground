"""Request-scoped preview cancellation; completion means the execution owner has returned."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from hub.python_preview import PreviewControl

PreviewStopStatus = Literal["stopped", "finished", "stopping"]
_TERMINAL_TTL_S = 3600.0
_MAX_REQUESTS = 2048


class PreviewRequestConflict(ValueError):
    pass


@dataclass
class PreviewSession:
    control: PreviewControl = field(default_factory=PreviewControl)
    done: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False
    cancel_requested: bool = False
    finished_at: float | None = None
    remote_cancel: Callable[[], dict] | None = None
    remote_terminal: bool = False

    def set_remote_cancel(self, callback: Callable[[], dict]) -> None:
        # Assigned before dispatch. A concurrent early cancel either sees this callback or the
        # cancellation event, which the transport checks before submitting any code.
        self.remote_cancel = callback


class PreviewRequests:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], PreviewSession] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        terminal = sorted(
            ((entry.finished_at, key) for key, entry in self._requests.items()
             if entry.finished_at is not None),
        )
        for ended, key in terminal:
            if now - ended < _TERMINAL_TTL_S:
                break
            entry = self._requests[key]
            if entry.remote_cancel is None or entry.remote_terminal:
                del self._requests[key]
        if len(self._requests) >= _MAX_REQUESTS:
            for _ended, key in terminal:
                entry = self._requests.get(key)
                if (entry is not None and entry.claimed and entry.done.is_set()
                        and (entry.remote_cancel is None or entry.remote_terminal)):
                    del self._requests[key]
                    if len(self._requests) < _MAX_REQUESTS:
                        break
        if len(self._requests) >= _MAX_REQUESTS:
            raise RuntimeError("Too many active previews; stop or wait for an existing preview.")

    @contextmanager
    def request(self, owner: str, canvas_id: str, request_id: str | None) -> Iterator[PreviewSession]:
        if request_id is None:
            # Legacy callers have no cancellation handle, so do not retain their completions.
            entry = PreviewSession(claimed=True)
            try:
                yield entry
            finally:
                entry.done.set()
            return
        key = (owner, canvas_id, str(uuid.UUID(request_id)))
        with self._lock:
            entry = self._requests.get(key)
            if entry is None:
                self._prune()
                entry = self._requests[key] = PreviewSession()
            elif entry.claimed:
                raise PreviewRequestConflict("This preview request was already submitted; use a new request ID.")
            entry.claimed = True
            entry.done.clear()
            entry.finished_at = None
        try:
            yield entry
        finally:
            with self._lock:
                entry.finished_at = time.monotonic()
                entry.done.set()

    def cancel(self, owner: str, canvas_id: str, request_id: str, *,
               fallback_cancel: Callable[[], dict] | None = None) -> PreviewStopStatus:
        key = (owner, canvas_id, str(uuid.UUID(request_id)))
        with self._lock:
            entry = self._requests.get(key)
            if entry is None:
                self._prune()
                entry = self._requests[key] = PreviewSession(cancel_requested=True)
                entry.control.cancel()
                entry.finished_at = time.monotonic()
                entry.done.set()
                # Remember a cancel that beats its preview POST. The same request ID can never
                # subsequently enter execution while this tombstone is retained.
                # A restarted hub also consults the still-live canvas kernel before acknowledging.
                entry.remote_cancel = fallback_cancel
            was_finished = entry.done.is_set() and not entry.cancel_requested
            if not was_finished:
                entry.cancel_requested = True
        if not was_finished:
            entry.control.cancel()
        if entry.remote_cancel is not None:
            # A completed HTTP request alone does not prove a remote process stopped. Retain this
            # callback after transport errors so Retry stop can ask the exact execution owner.
            remote = entry.remote_cancel()
            if remote.get("status") not in ("stopped", "finished"):
                return "stopping"
            entry.remote_terminal = True
            if remote.get("status") == "stopped":
                was_finished = False
                entry.cancel_requested = True
            else:
                # The execution owner finished before Stop arrived, even if the hub is
                # still reading its response or releasing a retained source lease.
                was_finished = True
        if not entry.done.wait(0.25):
            return "stopping"
        return "finished" if was_finished else "stopped"


preview_requests = PreviewRequests()
