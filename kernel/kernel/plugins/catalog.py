"""Default catalog provider — in-memory, seeded from a local data directory (PRD §5.9).

Tables discovered on disk become catalog entries; `write` nodes register their outputs
as new children, so lineage grows as the canvas runs.
"""

from __future__ import annotations

import os
import threading

import json

from kernel import metadb
from kernel.models import (
    CatalogTable,
    KeyInfo,
    LineageEdge,
    LineageNode,
    LineageResult,
    Relationship,
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
        from kernel.relationships import key_candidates
        keys = key_candidates(columns)  # inferred candidates; the declared key is OVERLAID on read (_overlay)
        with self._lock:
            tid = f"tbl_{name}" if uri not in self._by_uri else self._by_uri[uri]
            if any(t.id == f"tbl_{name}" and t.uri != uri for t in self.tables.values()):
                tid = f"tbl_{name}_{_h.sha1(uri.encode()).hexdigest()[:6]}"
            table = CatalogTable(
                id=tid, name=name, uri=uri, row_count=count, version=version,
                columns=columns, keys=keys, meta=meta,
            )
            self.tables[tid] = table
            self._by_uri[uri] = tid
            self._persist(table)  # write-through to the shared DB (cross-instance / restart-durable)
            return table

    def _persist(self, table: CatalogTable) -> None:
        """Mirror an entry to the shared catalog table. Best-effort: a DB hiccup must not break the
        in-memory catalog, which still serves this instance."""
        try:
            metadb.catalog_upsert_entry(table.uri, table.name, table.model_dump(by_alias=True))
        except Exception:  # noqa: BLE001
            pass

    def _load_from_db(self) -> None:
        """Merge catalog entries/edges registered by OTHER instances (or before a restart) into this
        instance's in-memory view. Called at the start of each read so a dataset another stateless web
        instance registered becomes visible here. Best-effort — a DB error just serves the local cache."""
        try:
            for d in metadb.catalog_entries():
                uri = d.get("uri")
                if uri and uri not in self._by_uri:
                    t = CatalogTable.model_validate(d)
                    self.tables[t.id] = t
                    self._by_uri[uri] = t.id
            have = {(e.parent, e.child) for e in self.edges}
            for e in metadb.catalog_edges():
                if (e["parent"], e["child"]) not in have:
                    self.edges.append(LineageEdge(parent=e["parent"], child=e["child"], pipeline=e.get("pipeline")))
        except Exception:  # noqa: BLE001
            pass

    def _overlay(self, t: CatalogTable, dmap: dict[str, list[str]] | None = None) -> CatalogTable:
        """Apply the owner-declared key (from Settings — the authoritative, cross-instance store) on
        top of a table's inferred keys, freshly recomputed from its columns. Overlaying on READ (not
        baking declared into the stored doc) is what makes a declared key: (a) visible on a peer that
        already cached the dataset, and (b) cleanly reversible — clearing it restores the inferred key.
        `dmap` lets list_tables read Settings once for the whole list."""
        from kernel.plugins.adapters import is_object_uri, path_of
        from kernel.relationships import key_candidates
        declared = (dmap if dmap is not None else self._declared_keys()).get(t.uri)
        inferred = [k for k in key_candidates(t.columns) if list(k.columns) != list(declared or [])]
        keys = ([KeyInfo(columns=list(declared), confidence="declared")] if declared else []) + inferred
        # flag a LOCAL-path dataset whose file no longer exists (e.g. `make clean` / a deleted temp),
        # so the UI can grey it out + offer removal instead of surfacing a raw IOException on click.
        # Skip object-store (s3/gs) and mem:// datasets — neither is an on-disk path.
        local = not is_object_uri(t.uri) and not t.uri.startswith("mem://")
        missing = local and not os.path.exists(path_of(t.uri))
        if keys == t.keys and missing == t.missing:
            return t
        return t.model_copy(update={"keys": keys, "missing": missing})

    # -- CatalogProvider --------------------------------------------------- #
    def list_tables(self, q: str | None) -> list[CatalogTable]:
        with self._lock:
            self._load_from_db()  # pick up entries registered by other instances / before a restart
            items = list(self.tables.values())  # snapshot under the lock; safe to filter after
        dmap = self._declared_keys()  # one read for the whole list
        items = [self._overlay(t, dmap) for t in items]
        if q:
            ql = q.lower()
            items = [t for t in items if ql in t.name.lower() or ql in t.uri.lower()]
        return items

    def get_table(self, id_or_name: str) -> CatalogTable:
        with self._lock:
            self._load_from_db()  # another instance may have registered it
            if id_or_name in self.tables:
                return self._overlay(self.tables[id_or_name])
            if id_or_name in self._by_uri:
                return self._overlay(self.tables[self._by_uri[id_or_name]])
            for t in self.tables.values():
                if t.name == id_or_name:
                    return self._overlay(t)
        raise KeyError(id_or_name)

    def lineage(self, uri: str) -> LineageResult:
        # collect the connected component around `uri` (over a snapshot so a concurrent register can't
        # mutate tables/edges mid-traversal)
        with self._lock:
            self._load_from_db()  # include lineage recorded by other instances
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
        try:
            metadb.catalog_add_edge(parent, child, pipeline)  # write-through (best-effort)
        except Exception:  # noqa: BLE001
            pass

    def register(self, table: CatalogTable, parents: list[str] | None = None,
                 pipeline: str | None = None) -> None:
        with self._lock:
            self.tables[table.id] = table
            self._by_uri[table.uri] = table.id
            self._persist(table)
        for parent in parents or []:
            self._add_edge(parent, table.uri, pipeline)

    def register_output(self, name: str, uri: str, version: str, parents: list[str],
                        pipeline: str | None = None) -> CatalogTable:
        table = self._add(name=name, uri=uri, version=version, meta=pipeline)
        for parent in parents:
            self._add_edge(parent, uri, pipeline)
        return table

    def resolve_ref(self, ref: str) -> str:
        """Resolve a source reference to a dataset URI: a real path or scheme'd uri passes through
        unchanged; a bare catalog table NAME or ID resolves to its uri — so an API/agent client can
        point a `source` node at 'events' or 'tbl_events' instead of the full path (F50)."""
        if not ref or "://" in ref or "/" in ref or "\\" in ref:
            return ref  # already a path / object-store uri
        try:
            return self.get_table(ref).uri
        except KeyError:
            return ref  # unknown token → leave it (the normal "cannot read" error will surface)

    def unregister(self, id_or_name: str) -> bool:
        """Remove a dataset from the catalog (in-memory + the shared per-row store) — for pruning a
        dead entry whose backing file is gone. Returns False if not found. Declared keys/relationships
        keyed by the uri are left as-is (harmless dangling references)."""
        with self._lock:
            self._load_from_db()
            tid = id_or_name if id_or_name in self.tables else self._by_uri.get(id_or_name) \
                or next((t.id for t in self.tables.values() if t.name == id_or_name), None)
            t = self.tables.get(tid) if tid else None
            if t is None:
                return False
            self.tables.pop(tid, None)
            self._by_uri.pop(t.uri, None)
            # delete the DB row INSIDE the lock: doing it after releasing let a concurrent
            # _load_from_db re-add the just-removed row (the delete wouldn't stick).
            try:
                metadb.catalog_delete_entry(t.uri)
            except Exception:  # noqa: BLE001
                pass
        return True

    # -- declared keys & relationships (owner-asserted; per-ROW in the shared DB, cross-instance) --- #
    # Stored one-row-each (metadb.catalog_declared_keys / catalog_relationships), NOT a single JSON
    # blob, so two instances declaring different keys/relationships can't clobber each other.
    def _declared_keys(self) -> dict[str, list[str]]:
        try:
            return metadb.catalog_declared_keys()
        except Exception:  # noqa: BLE001
            return {}

    def set_declared_key(self, uri: str, columns: list[str] | None) -> None:
        """Set (or clear, columns=None/[]) the owner-declared primary key of a dataset — one DB row,
        OVERLAID on read (_overlay), so it works cross-instance and a clear cleanly restores the
        inferred key. The escape hatch for a dataset an opaque transform produced or whose key the
        name heuristic missed."""
        metadb.catalog_set_declared_key(uri, list(columns or []))

    def relationships(self, uri: str | None = None) -> list[Relationship]:
        """Owner-declared join edges; filtered to those touching `uri` when given. A malformed stored
        row (a manual edit / version skew) is skipped, never fatal — otherwise one bad row would 500
        the whole feature, including the delete path needed to remove it."""
        try:
            raw = metadb.catalog_relationships()
        except Exception:  # noqa: BLE001
            raw = []
        rels: list[Relationship] = []
        for r in raw:
            try:
                rels.append(Relationship.model_validate(r))
            except Exception:  # noqa: BLE001 — skip a bad row, don't take down relationships()
                pass
        if uri is not None:
            rels = [r for r in rels if uri in (r.left_uri, r.right_uri)]
        return rels

    @staticmethod
    def _rel_key(r: Relationship) -> str:
        # orientation-insensitive canonical key: A→B and B→A on swapped columns are ONE logical
        # relationship (sorted endpoints), so re-declaring in reverse replaces its row, not adds one.
        ends = sorted([[r.left_uri, list(r.left_columns)], [r.right_uri, list(r.right_columns)]])
        return json.dumps(ends)

    def add_relationship(self, rel: Relationship) -> None:
        metadb.catalog_upsert_relationship(self._rel_key(rel), rel.model_dump(by_alias=True))

    def remove_relationship(self, rel: Relationship) -> None:
        metadb.catalog_delete_relationship(self._rel_key(rel))
