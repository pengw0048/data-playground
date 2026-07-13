"""Bounded per-kernel Arrow cache for preview intermediate relations.

Each preview/run uses a rollback-only DuckDB transaction.  Catalog tables created inside one scope cannot
back a cross-scope warm cache, so cached values live as immutable Arrow tables in Python memory instead.
Entries are bounded by rows, per-entry bytes, total bytes, and LRU count; a miss is always safe.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import pyarrow as pa

from hub import db

CAP_ROWS = 50_000
CAP_BYTES = 64 << 20
MAX_ENTRIES = 64
MAX_BYTES = 256 << 20


class RelationCache:
    def __init__(self, cap_rows: int = CAP_ROWS, max_entries: int = MAX_ENTRIES,
                 cap_bytes: int = CAP_BYTES, max_bytes: int = MAX_BYTES):
        self.cap, self.max = cap_rows, max_entries
        self.cap_bytes, self.max_bytes = cap_bytes, max_bytes
        self._lru: OrderedDict[str, pa.Table] = OrderedDict()
        self._bytes = 0
        # Remember a bounded number of over-cap keys so repeated previews do not rematerialize them.
        self._toobig: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def _relation(self, table: pa.Table):
        # DuckDB retains the Arrow dependency for the relation lifetime; an LRU eviction can drop the
        # cache's reference without invalidating a relation already handed to a caller.
        return db.conn().from_arrow(table)

    def _mark_too_big(self, key: str) -> None:
        self._toobig[key] = None
        self._toobig.move_to_end(key)
        while len(self._toobig) > self.max:
            self._toobig.popitem(last=False)

    def get(self, key: str):
        """Return a relation over a cached Arrow table, or None on a safe miss."""
        with self._lock:
            table = self._lru.get(key)
            if table is None:
                return None
            self._lru.move_to_end(key)
        return self._relation(table)

    def put(self, key: str, view_name: str):
        """Materialize a bounded view into Arrow and cache it without catalog DDL."""
        with self._lock:
            if key in self._toobig:
                self._toobig.move_to_end(key)
                return None
            table = self._lru.get(key)
            if table is not None:
                self._lru.move_to_end(key)
            else:
                try:
                    table = db.conn().table(view_name).limit(self.cap + 1).to_arrow_table()
                except Exception:  # noqa: BLE001 — cache materialization failure is a safe miss
                    return None
                if table.num_rows > self.cap or table.nbytes > min(self.cap_bytes, self.max_bytes):
                    self._mark_too_big(key)
                    return None
                self._lru[key] = table
                self._bytes += table.nbytes
                while len(self._lru) > self.max or self._bytes > self.max_bytes:
                    _, victim = self._lru.popitem(last=False)
                    self._bytes -= victim.nbytes
        return self._relation(table)
