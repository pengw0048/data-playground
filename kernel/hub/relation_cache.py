"""Per-kernel warm cache of INTERMEDIATE node relations, materialized as DuckDB tables on the kernel's
warm connection. Used only in the PREVIEW engine (a bounded sample → intermediates fit), keyed by the
shared plan_hash (so it invalidates automatically on any edit) and row-CAPPED so an unbounded relation
is never materialized (over-cap → dropped + recomputed). This is what makes preview-on-kernel pay off:
re-previewing reuses unchanged upstream nodes instead of rebuilding their subgraphs.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

from hub import db

CAP_ROWS = 50_000    # never materialize more than this (bounds memory); over-cap → don't cache
MAX_ENTRIES = 64     # LRU bound on cached tables per kernel


class RelationCache:
    def __init__(self, cap_rows: int = CAP_ROWS, max_entries: int = MAX_ENTRIES):
        self.cap, self.max = cap_rows, max_entries
        self._lru: OrderedDict[str, str] = OrderedDict()  # cache_key -> materialized table name
        self._toobig: set[str] = set()                    # keys known to exceed the cap (don't retry)
        self._lock = threading.Lock()

    def _table(self, key: str) -> str:
        return "_rc_" + hashlib.sha1(key.encode()).hexdigest()[:16]

    def get(self, key: str):
        """A relation over the cached table for `key`, or None (a miss is always safe → recompute)."""
        with self._lock:
            tbl = self._lru.get(key)
            if tbl is None:
                return None
            self._lru.move_to_end(key)
        try:
            return db.conn().table(tbl)
        except Exception:  # noqa: BLE001 — the table vanished → treat as a miss
            with self._lock:
                self._lru.pop(key, None)
            return None

    def put(self, key: str, view_name: str):
        """Materialize a built relation (registered as the view `view_name`) into a capped table and
        cache it. Returns a relation over the cached table, or None if it exceeds the cap (not cached).
        A failure never breaks the build — it just means no cache."""
        with self._lock:
            if key in self._toobig or key in self._lru:
                return self.get(key) if key in self._lru else None
            tbl = self._table(key)
            con = db.conn()
            try:
                con.execute(f'CREATE OR REPLACE TABLE "{tbl}" AS SELECT * FROM {view_name} LIMIT {self.cap + 1}')
                n = con.execute(f'SELECT count(*) FROM "{tbl}"').fetchone()[0]
            except Exception:  # noqa: BLE001
                self._drop(con, tbl)
                return None
            if n > self.cap:  # unbounded / too big → don't cache (recompute next time)
                self._drop(con, tbl)
                self._toobig.add(key)
                return None
            self._lru[key] = tbl
            while len(self._lru) > self.max:  # LRU-evict the oldest, dropping its table
                _, victim = self._lru.popitem(last=False)
                self._drop(con, victim)
            return con.table(tbl)

    @staticmethod
    def _drop(con, tbl: str) -> None:
        try:
            con.execute(f'DROP TABLE IF EXISTS "{tbl}"')
        except Exception:  # noqa: BLE001
            pass
