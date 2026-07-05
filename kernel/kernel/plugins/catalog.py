"""Default catalog provider — in-memory, seeded from a local data directory (PRD §5.9).

Tables discovered on disk become catalog entries; `write` nodes register their outputs
as new children, so lineage grows as the canvas runs.
"""

from __future__ import annotations

import os
import threading

from kernel.models import (
    CatalogTable,
    LineageEdge,
    LineageNode,
    LineageResult,
)


class InMemoryCatalog:
    name = "in-memory"

    def __init__(self, data_dir: str, resolve_adapter):
        self.data_dir = data_dir
        self.resolve = resolve_adapter
        self.tables: dict[str, CatalogTable] = {}
        self._by_uri: dict[str, str] = {}
        self.edges: list[LineageEdge] = []
        # this ONE catalog is shared across the process: register/register_output run on runner daemon
        # threads + subprocess _watch threads + startup, while request threads iterate in list/get/lineage.
        # An RLock serializes those (reentrant so register→_add_edge doesn't self-deadlock).
        self._lock = threading.RLock()
        self._seed()

    # -- discovery --------------------------------------------------------- #
    _EXTS = (".parquet", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc")

    def _seed(self) -> None:
        if not os.path.isdir(self.data_dir):
            return
        for fn in sorted(os.listdir(self.data_dir)):
            path = os.path.join(self.data_dir, fn)
            is_lance = os.path.isdir(path) and fn.endswith(".lance")
            if not (fn.endswith(self._EXTS) or is_lance):
                continue
            name = fn[:-6] if is_lance else os.path.splitext(fn)[0]
            self._add(name=name, uri=path, version="v1")

    def _add(self, name: str, uri: str, version: str, meta: str | None = None) -> CatalogTable:
        # id from the uri (names collide across different files); fall back to name if uri is reused
        import hashlib as _h
        # schema/count probe (may touch disk/network) OUTSIDE the lock so a slow adapter doesn't block
        # concurrent catalog reads; only the dict mutation below is serialized.
        try:
            adapter = self.resolve(uri)
            columns = adapter.schema(uri)
            count = adapter.count(uri)
        except Exception:
            columns, count = [], None
        with self._lock:
            tid = f"tbl_{name}" if uri not in self._by_uri else self._by_uri[uri]
            if any(t.id == f"tbl_{name}" and t.uri != uri for t in self.tables.values()):
                tid = f"tbl_{name}_{_h.sha1(uri.encode()).hexdigest()[:6]}"
            table = CatalogTable(
                id=tid, name=name, uri=uri, row_count=count, version=version,
                columns=columns, meta=meta,
            )
            self.tables[tid] = table
            self._by_uri[uri] = tid
            return table

    # -- CatalogProvider --------------------------------------------------- #
    def list_tables(self, q: str | None) -> list[CatalogTable]:
        with self._lock:
            items = list(self.tables.values())  # snapshot under the lock; safe to filter after
        if q:
            ql = q.lower()
            items = [t for t in items if ql in t.name.lower() or ql in t.uri.lower()]
        return items

    def get_table(self, id_or_name: str) -> CatalogTable:
        with self._lock:
            if id_or_name in self.tables:
                return self.tables[id_or_name]
            if id_or_name in self._by_uri:
                return self.tables[self._by_uri[id_or_name]]
            for t in self.tables.values():
                if t.name == id_or_name:
                    return t
        raise KeyError(id_or_name)

    def lineage(self, uri: str) -> LineageResult:
        # collect the connected component around `uri` (over a snapshot so a concurrent register can't
        # mutate tables/edges mid-traversal)
        with self._lock:
            tables = dict(self.tables)
            by_uri = dict(self._by_uri)
            all_edges = list(self.edges)
        seen: set[str] = set()
        frontier = [uri]
        nodes: list[LineageNode] = []
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            t = tables.get(by_uri.get(cur, ""))
            nodes.append(LineageNode(
                id=t.id if t else cur, name=t.name if t else cur, uri=cur,
            ))
            for e in all_edges:
                if e.parent == cur and e.child not in seen:
                    frontier.append(e.child)
                if e.child == cur and e.parent not in seen:
                    frontier.append(e.parent)
        edges = [e for e in all_edges if e.parent in seen and e.child in seen]
        return LineageResult(nodes=nodes, edges=edges)

    def _add_edge(self, parent: str, child: str, pipeline: str | None) -> None:
        if parent == child:
            return
        with self._lock:
            if any(e.parent == parent and e.child == child for e in self.edges):
                return  # dedupe: one edge per (parent, child)
            self.edges.append(LineageEdge(parent=parent, child=child, pipeline=pipeline))

    def register(self, table: CatalogTable, parents: list[str] | None = None,
                 pipeline: str | None = None) -> None:
        with self._lock:
            self.tables[table.id] = table
            self._by_uri[table.uri] = table.id
        for parent in parents or []:
            self._add_edge(parent, table.uri, pipeline)

    def register_output(self, name: str, uri: str, version: str, parents: list[str],
                        pipeline: str | None = None) -> CatalogTable:
        table = self._add(name=name, uri=uri, version=version, meta=pipeline)
        for parent in parents:
            self._add_edge(parent, uri, pipeline)
        return table
