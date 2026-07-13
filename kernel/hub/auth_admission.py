"""Process-local admission controls for password KDF work.

Scrypt is intentionally expensive in both CPU and memory.  These controls bound the amount of that
work one hub process will accept at once and bound repeated credential guesses per client/user pair.
They are process-local by design: the KDF gate protects this process's resources, while deployments
with several hub replicas get the same per-replica protection without a new shared-service dependency.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Hashable

PASSWORD_WORK_CONCURRENCY = 4
LOGIN_PEER_ATTEMPT_CAPACITY = 100
PASSWORD_ATTEMPT_CAPACITY = 5
PASSWORD_ATTEMPT_REFILL_SECONDS = 60.0
PASSWORD_ATTEMPT_ENTRY_TTL_SECONDS = 10 * 60.0
PASSWORD_ATTEMPT_MAX_ENTRIES = 4096


def _attempt_key(namespace: bytes, *values: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(namespace)
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def login_peer_attempt_key(client_host: str) -> bytes:
    """A fixed-size key for aggregate login admission from one network peer."""
    return _attempt_key(b"dp-login-peer-v1", client_host)


def password_attempt_key(client_host: str, user_id: str) -> bytes:
    """A fixed-size, collision-resistant representation of one peer/user pair."""
    return _attempt_key(b"dp-password-pair-v1", client_host, user_id)


class PasswordWorkGate:
    """A non-blocking, process-wide cap for complete password operations."""

    def __init__(self, capacity: int = PASSWORD_WORK_CONCURRENCY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("password work capacity must be a positive integer")
        self.capacity = capacity
        self._semaphore = threading.BoundedSemaphore(capacity)

    def try_acquire(self) -> bool:
        """Acquire immediately or return False; callers must never queue a worker waiting here."""
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


@dataclass(frozen=True)
class AttemptDecision:
    allowed: bool
    retry_after: int = 0


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    last_seen: float


class AttemptLimiter:
    """Bounded, thread-safe token buckets keyed by a client/user identity pair.

    A bucket allows ``capacity`` attempts in a burst and replenishes that whole capacity over
    ``refill_seconds``. Idle entries expire and least-recently-used entries are evicted at the hard
    entry cap. Production callers use fixed-size digest keys, so both entry count and key size remain
    bounded. Login also has a per-peer bucket ahead of pair buckets, making pair eviction costly
    without turning pair-table cardinality into a global lockout.
    """

    def __init__(
        self,
        *,
        capacity: int = PASSWORD_ATTEMPT_CAPACITY,
        refill_seconds: float = PASSWORD_ATTEMPT_REFILL_SECONDS,
        entry_ttl_seconds: float = PASSWORD_ATTEMPT_ENTRY_TTL_SECONDS,
        max_entries: int = PASSWORD_ATTEMPT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("attempt capacity must be a positive integer")
        if refill_seconds <= 0 or entry_ttl_seconds <= 0:
            raise ValueError("attempt limiter durations must be positive")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("attempt limiter max_entries must be a positive integer")
        self.capacity = capacity
        self._refill_rate = capacity / float(refill_seconds)
        self._entry_ttl_seconds = float(entry_ttl_seconds)
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[Hashable, _Bucket] = OrderedDict()

    def consume(self, key: Hashable) -> AttemptDecision:
        """Atomically consume one attempt, returning the wait time when none is available."""
        now = self._clock()
        with self._lock:
            self._expire_idle(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_entries:
                    self._buckets.popitem(last=False)
                bucket = _Bucket(tokens=float(self.capacity), updated_at=now, last_seen=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(float(self.capacity), bucket.tokens + elapsed * self._refill_rate)
                bucket.updated_at = max(bucket.updated_at, now)
                bucket.last_seen = max(bucket.last_seen, now)
                self._buckets.move_to_end(key)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return AttemptDecision(allowed=True)
            retry_after = max(1, math.ceil((1.0 - bucket.tokens) / self._refill_rate))
            return AttemptDecision(allowed=False, retry_after=retry_after)

    def reset(self, key: Hashable) -> None:
        """Forget a bucket after a credential was proven successfully."""
        with self._lock:
            self._buckets.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _expire_idle(self, now: float) -> None:
        # OrderedDict order is last access, so expiration stops at the first live entry.
        while self._buckets:
            first_key = next(iter(self._buckets))
            if now - self._buckets[first_key].last_seen < self._entry_ttl_seconds:
                break
            self._buckets.popitem(last=False)


password_work_gate = PasswordWorkGate()
password_work_executor = ThreadPoolExecutor(
    max_workers=PASSWORD_WORK_CONCURRENCY,
    thread_name_prefix="dp-password",
)
login_peer_attempts = AttemptLimiter(capacity=LOGIN_PEER_ATTEMPT_CAPACITY)
login_attempts = AttemptLimiter()
password_change_attempts = AttemptLimiter()
