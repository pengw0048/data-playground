"""Kernel /status assembly: shape (incl. uptime + relationCache) and a lock-safe runs snapshot.

The status endpoint itself is a thin wrapper over hub.kernel._status_payload; wiring a full kernel
process is heavy, so the payload assembly + RelationCache.stats() are exercised directly here.
"""

from __future__ import annotations

import threading
import time

from hub import kernel
from hub.relation_cache import RelationCache


class _St:
    def __init__(self, status: str) -> None:
        self.status = status


def test_relation_cache_stats_shape():
    c = RelationCache()
    s = c.stats()
    assert s == {"entries": 0, "bytes": 0, "maxEntries": c.max, "maxBytes": c.max_bytes, "tooBig": 0}


def test_status_payload_shape_and_uptime():
    cache = RelationCache()
    lock = threading.Lock()
    runs = {"r1": _St("running"), "r2": _St("done"), "r3": _St("queued")}
    out = kernel._status_payload(cache, "4GB", 2, runs, lock, time.monotonic() - 5.0)

    assert out["relationCache"] == cache.stats()
    assert out["memoryLimit"] == "4GB"
    assert out["inflight"] == 2
    assert out["activeRuns"] == 2  # running + queued, not done
    assert out["uptimeSeconds"] >= 5.0
    assert set(out) >= {"relationCache", "memoryLimit", "uptimeSeconds", "inflight", "activeRuns"}
    if "memoryRssBytes" in out:  # present where RSS is cheaply available; never faked
        assert isinstance(out["memoryRssBytes"], int) and out["memoryRssBytes"] > 0


def test_status_payload_memory_limit_may_be_none():
    out = kernel._status_payload(RelationCache(), None, 0, {}, threading.Lock(), time.monotonic())
    assert out["memoryLimit"] is None
    assert out["activeRuns"] == 0 and out["inflight"] == 0


def test_runs_snapshot_is_lock_safe_under_concurrent_mutation():
    lock = threading.Lock()
    runs = {f"r{i}": _St("running") for i in range(200)}
    stop = threading.Event()

    def churn() -> None:
        i = 1000
        while not stop.is_set():
            with lock:
                runs[f"x{i}"] = _St("queued")
                runs.pop(f"x{i - 50}", None)
            i += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(500):
            # consume the snapshot the way _status_payload does — must never raise mid-iteration
            snap = kernel._runs_snapshot(runs, lock)
            assert sum(1 for s in snap if getattr(s, "status", None) in ("queued", "running")) >= 0
    finally:
        stop.set()
        t.join(timeout=2)
