"""Default catalog provider — DB-backed, built to browse thousands of tables.

Every read (browse / search / facet / get / lineage) PUSHES DOWN to the shared metadata DB
(`hub.metadb`, SQLite locally / Postgres in a deployment) as an indexed, bounded query — the catalog
never loads all entries into memory to filter in Python (the earlier model that fell over past a few
hundred tables). Writes (`register` / `register_output`) write-through to the same DB, so a dataset
registered on one stateless web instance is visible to every other + survives a restart.

Organization is generic and owner-asserted: a `folder` path (the browse-hierarchy namespace), free-form
`tags`, an `owner`, and a `description`. None of it is tied to any particular external system — but it
maps 1:1 onto the namespace/tag/owner model mature catalogs expose, so an external `CatalogProvider`
(the `reg.set_catalog` seam) can round-trip it. Semantic search is opt-in: when a plugin registers an
embedder (`reg.add_embedder`), entries are embedded and `search(mode="semantic"|"hybrid")` lights up;
with no embedder the catalog still does lexical + faceted search offline, zero extra dependencies.
"""

from __future__ import annotations

import logging
import os
import threading

from hub import metadb
from hub.models import (
    CatalogBrowse,
    CatalogPage,
    CatalogQuery,
    CatalogTable,
    Facets,
    FacetValue,
    FolderNode,
    KeyInfo,
    LineageEdge,
    LineageNode,
    LineageResult,
    Relationship,
)

log = logging.getLogger("hub")

# Bounded defaults for lineage traversal — a large, densely-connected component is capped so the graph
# a client renders (and the payload) stays sane; `truncated` tells the UI there was more.
DEFAULT_LINEAGE_DEPTH = 6
DEFAULT_LINEAGE_MAX_NODES = 500
# `list_tables(None)` (the back-compat convenience the agent/MCP use) returns at most this many, so a
# huge catalog can't produce an unbounded list; real browsing uses list_page (paginated) / search.
LIST_TABLES_CAP = 5000


class InMemoryCatalog:
    """The default CatalogProvider. Named for history — it's now DB-backed (the DB is authoritative and
    cross-instance); there is no in-memory table map to go stale."""

    name = "in-memory"

    def __init__(self, data_dir: str, resolve_adapter):
        self.data_dir = data_dir
        self.resolve = resolve_adapter
        # a re-entrant lock serializes this instance's write-side read-modify-writes (version compute +
        # schema-drift check + upsert). Reads don't take it — they're single indexed DB queries.
        self._lock = threading.RLock()
        self._embedder = None          # callable(list[str]) -> list[list[float]] (set via reg.add_embedder)
        self._embed_model = ""
        self._emb_dirty = 0            # bumped on local embedding writes → invalidates _emb_cache
        self._emb_cache: tuple | None = None  # (dirty_stamp, monotonic_ts, (uris, matrix, norms) | None)
        self._reindex_lock = threading.Lock()  # one reindex walk at a time (set_embedder + explicit calls)
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
            self._add(name=name, uri=path)  # content-addressed version

    @staticmethod
    def _object_stat_sig(uri: str) -> str:
        """`:size:mtime` for a SINGLE object (so an object overwrite bumps the content-addressed version),
        or "" for a non-object uri / prefix / stat failure — the version then falls back to schema+rows+uri."""
        from hub.plugins.adapters import is_object_uri
        if not is_object_uri(uri):
            return ""
        try:
            import pyarrow.fs as pafs
            from hub.plugins.adapters import object_fs
            fs, p = object_fs(uri)
            info = fs.get_file_info(p)
            if info.type == pafs.FileType.File:
                return f":{info.size}:{info.mtime_ns}"
        except Exception:  # noqa: BLE001 — no stat available → fall back to the uri-only fingerprint
            pass
        return ""

    def _add(self, name: str, uri: str, version: str | None = None, meta: str | None = None,
             folder: str = "", tags: list[str] | None = None, owner: str | None = None,
             description: str | None = None) -> CatalogTable:
        import hashlib as _h
        # schema/count probe (may touch disk/network) OUTSIDE the lock so a slow adapter doesn't block
        # concurrent catalog reads; only the version/collision compute + upsert below is serialized.
        try:
            adapter = self.resolve(uri)
            columns = adapter.schema(uri)
            count = adapter.count(uri)
        except Exception:  # noqa: BLE001 — an unresolvable/unreadable uri → empty schema (still registered)
            adapter, columns, count = None, [], None
        fp = "unknown"
        try:
            fp = (adapter.fingerprint(uri) if adapter else "unknown") + self._object_stat_sig(uri)
        except Exception:  # noqa: BLE001
            pass
        from hub.relationships import key_candidates
        keys = key_candidates(columns)
        if version is None:
            sig = "|".join(f"{c.name}:{c.type}" for c in columns) + f"|rows={count}|fp={fp}"
            version = "v" + _h.sha256(sig.encode()).hexdigest()[:10]
        folder = (folder or "").strip("/")
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        with self._lock:
            prior = metadb.catalog_get(uri)  # by uri (PK)
            tid = (prior or {}).get("id") or f"tbl_{name}"
            if prior is None:
                other = metadb.catalog_get(f"tbl_{name}")  # id collision across different files
                if other and other.get("uri") != uri:
                    tid = f"tbl_{name}_{_h.sha1(uri.encode()).hexdigest()[:6]}"
            # carry forward organization set previously (register shouldn't silently wipe a table's
            # folder/tags/owner/description just because a re-run re-probed its schema)
            if prior:
                folder = folder or (prior.get("folder") or "")
                tags = tags or list(prior.get("tags") or [])
                owner = owner if owner is not None else prior.get("owner")
                description = description if description is not None else prior.get("description")
            self._warn_schema_drift(prior, name, columns, version)
            table = CatalogTable(
                id=tid, name=name, uri=uri, row_count=count, version=version,
                columns=columns, keys=keys, meta=meta, folder=folder, tags=tags,
                owner=owner, description=description,
            )
            self._persist(table)
        self._embed_one(table)  # best-effort semantic index (no-op without an embedder)
        return table

    @staticmethod
    def _warn_schema_drift(prior: dict | None, name: str, columns: list, version: str) -> None:
        """A written output whose columns drifted from the prior write is a silent contract break for
        downstream consumers — log a WARNING (overwrite is deliberate, so we don't fail; enforceSchema
        on a node is the hard gate)."""
        if not (prior and prior.get("columns") and columns):
            return
        pc = [(c.get("name"), c.get("type")) for c in prior["columns"]]
        cc = [(c.name, c.type) for c in columns]
        if pc == cc:
            return
        added, removed = [c for c in cc if c not in pc], [c for c in pc if c not in cc]
        detail = f"added={added} removed={removed}" if (added or removed) else "columns reordered"
        log.warning("catalog output %r schema changed on overwrite (version %s→%s): %s",
                    name, prior.get("version"), version, detail)

    def _persist(self, table: CatalogTable) -> None:
        try:
            metadb.catalog_upsert_entry(table.uri, table.name, table.model_dump(by_alias=True))
        except Exception as e:  # noqa: BLE001
            log.warning("catalog persist failed for %s (%s: %s)", table.name, type(e).__name__, e)

    # -- read-side overlay ------------------------------------------------- #
    def _overlay(self, t: CatalogTable, dmap: dict[str, list[str]] | None = None) -> CatalogTable:
        """Apply the owner-declared key on top of freshly-recomputed inferred keys, and flag a
        local-path dataset whose file has vanished. Overlaying on READ keeps a declared key visible
        cross-instance and cleanly reversible. `dmap` lets a page read declared keys once for the batch."""
        from hub.plugins.adapters import is_object_uri, path_of
        from hub.relationships import key_candidates
        declared = (dmap if dmap is not None else self._declared_keys()).get(t.uri)
        inferred = [k for k in key_candidates(t.columns) if list(k.columns) != list(declared or [])]
        keys = ([KeyInfo(columns=list(declared), confidence="declared")] if declared else []) + inferred
        local = not is_object_uri(t.uri) and not t.uri.startswith("mem://")
        missing = local and not os.path.exists(path_of(t.uri))
        if keys == t.keys and missing == t.missing:
            return t
        return t.model_copy(update={"keys": keys, "missing": missing})

    @staticmethod
    def _to_table(doc: dict) -> CatalogTable:
        return CatalogTable.model_validate(doc)

    # -- CatalogProvider: browse / search --------------------------------- #
    def list_page(self, query: CatalogQuery) -> CatalogPage:
        """One filtered, sorted, paginated window of the catalog — the scalable browse primitive. The
        page's items + total come from a single indexed DB query; memory/wire cost is bounded by the
        window, never by the catalog size."""
        docs, total = metadb.catalog_query(
            q=query.q, folder=query.folder, tags=query.tags, owner=query.owner,
            uris=query.uris, has_columns=query.has_columns, sort=query.sort, order=query.order,
            limit=query.limit, offset=query.offset)
        dmap = self._declared_keys([d["uri"] for d in docs])
        items = [self._overlay(self._to_table(d), dmap) for d in docs]
        return CatalogPage(items=items, total=total, offset=query.offset, limit=query.limit,
                           has_more=query.offset + len(items) < total)

    def list_tables(self, q: str | None) -> list[CatalogTable]:
        """Back-compat convenience — a bare (bounded) list, matching one page of the browse query. The
        agent/MCP call this; the UI uses list_page (paginated) + facets."""
        return self.list_page(CatalogQuery(q=q, limit=LIST_TABLES_CAP)).items

    def facets(self, query: CatalogQuery) -> Facets:
        """Distinct folder/tag/owner values + counts over the active filter set (drill-down)."""
        raw = metadb.catalog_facets(q=query.q, folder=query.folder, tags=query.tags,
                                    owner=query.owner, has_columns=query.has_columns)
        fv = lambda pairs: [FacetValue(value=v, count=c) for v, c in pairs]  # noqa: E731
        return Facets(folders=fv(raw["folders"]), tags=fv(raw["tags"]), owners=fv(raw["owners"]))

    def browse(self, prefix: str = "") -> CatalogBrowse:
        """One level of the folder tree at `prefix`: immediate child folders (subtree counts) + the
        tables filed directly here (a bounded sample; truncated/total signal more). Lets the UI
        lazily expand a tree of any size."""
        children, table_docs, direct_total = metadb.catalog_tree(prefix)
        dmap = self._declared_keys([d["uri"] for d in table_docs])
        return CatalogBrowse(
            prefix=(prefix or "").strip("/"),
            folders=[FolderNode(name=n, path=p, table_count=c) for n, p, c in children],
            tables=[self._overlay(self._to_table(d), dmap) for d in table_docs],
            total_tables=direct_total, truncated=direct_total > len(table_docs))

    def get_table(self, id_or_name: str) -> CatalogTable:
        doc = metadb.catalog_get(id_or_name)
        if doc is None:
            raise KeyError(id_or_name)
        return self._overlay(self._to_table(doc), self._declared_keys([doc["uri"]]))

    # -- CatalogProvider: lineage ----------------------------------------- #
    def lineage(self, uri: str, depth: int = DEFAULT_LINEAGE_DEPTH,
                max_nodes: int = DEFAULT_LINEAGE_MAX_NODES) -> LineageResult:
        """The connected component around `uri`, expanded breadth-first from the DB one frontier at a
        time and CAPPED by `depth` + `max_nodes` (so a huge lineage graph can't blow up the payload).
        `truncated` is set when the cap stopped the walk before the component was exhausted."""
        seen: set[str] = {uri}
        frontier = [uri]
        edges: dict[tuple[str, str], LineageEdge] = {}
        truncated = False
        hops = 0
        # cap one frontier expansion too — a hub node with 100k children must not load them all
        edge_cap = max(2000, max_nodes * 4)
        while frontier and hops < max(1, depth):
            hops += 1
            batch = metadb.catalog_edges_touching(frontier, limit=edge_cap)
            if len(batch) >= edge_cap:
                truncated = True
            nxt: list[str] = []
            for e in batch:
                key = (e["parent"], e["child"])
                if key not in edges:
                    edges[key] = LineageEdge(parent=e["parent"], child=e["child"],
                                             column=e.get("column"), pipeline=e.get("pipeline"))
                for end in (e["parent"], e["child"]):
                    if end in seen:
                        continue
                    if len(seen) >= max_nodes:
                        truncated = True
                        continue
                    seen.add(end)
                    nxt.append(end)
            frontier = nxt
        if frontier:  # depth budget ran out with more to explore
            truncated = True
        # keep only edges whose BOTH endpoints made it into the (capped) node set
        kept = [e for e in edges.values() if e.parent in seen and e.child in seen]
        names = metadb.catalog_get_many(list(seen))
        nodes = [LineageNode(id=(names.get(u, {}).get("id") or u),
                             name=(names.get(u, {}).get("name") or u.split("/")[-1]), uri=u)
                 for u in seen]
        return LineageResult(nodes=nodes, edges=kept, truncated=truncated)

    # -- CatalogProvider: write-back -------------------------------------- #
    def _add_edge(self, parent: str, child: str, pipeline: str | None, column: str | None = None) -> None:
        if parent == child:
            return
        try:
            metadb.catalog_add_edge(parent, child, pipeline, column)
        except Exception:  # noqa: BLE001
            pass

    def register(self, table: CatalogTable, parents: list[str] | None = None,
                 pipeline: str | None = None) -> None:
        with self._lock:
            self._persist(table)
        self._embed_one(table)
        for parent in parents or []:
            self._add_edge(parent, table.uri, pipeline)

    def register_output(self, name: str, uri: str, version: str | None = None,
                        parents: list[str] | None = None, pipeline: str | None = None,
                        folder: str = "", tags: list[str] | None = None, owner: str | None = None,
                        description: str | None = None) -> CatalogTable:
        table = self._add(name=name, uri=uri, version=version, meta=pipeline, folder=folder,
                          tags=tags, owner=owner, description=description)
        for parent in parents or []:
            self._add_edge(parent, uri, pipeline)
            metadb.catalog_bump_usage(parent)  # a derived output READ its parent → popularity signal
        return table

    def set_metadata(self, uri: str, *, folder: str | None = None, tags: list[str] | None = None,
                     owner: str | None = None, description: str | None = None) -> CatalogTable:
        """Update a dataset's organization (folder/tags/owner/description). None → field unchanged;
        for owner/description an empty string CLEARS the field (there's no meaningful empty owner),
        so a client can unset without a sentinel. folder='' files at the root; tags=[] clears tags.
        Raises KeyError if the uri isn't registered."""
        cur = metadb.catalog_get(uri)
        if cur is None:
            raise KeyError(uri)
        own = cur.get("owner") if owner is None else (owner.strip() or None)
        desc = cur.get("description") if description is None else (description.strip() or None)
        metadb.catalog_set_metadata(
            uri,
            folder=(folder if folder is not None else cur.get("folder") or "").strip("/"),
            owner=own, description=desc,
            tags=[str(t).strip() for t in (tags if tags is not None else cur.get("tags") or []) if str(t).strip()],
        )
        t = self.get_table(uri)
        self._embed_one(t)  # description/tags changed → refresh the semantic vector
        return t

    def resolve_ref(self, ref: str) -> str:
        """Resolve a source reference to a uri: a real path / scheme'd uri passes through; a bare
        catalog NAME or ID resolves to its uri (so an API/agent client can point a source at 'events')."""
        if not ref or "://" in ref or "/" in ref or "\\" in ref:
            return ref
        doc = metadb.catalog_get(ref)
        return doc["uri"] if doc else ref

    def unregister(self, id_or_name: str) -> bool:
        with self._lock:
            doc = metadb.catalog_get(id_or_name)
            if doc is None:
                return False
            metadb.catalog_delete_entry(doc["uri"])
            self._emb_dirty += 1  # its embedding row went with it
        return True

    # -- semantic search (opt-in via reg.add_embedder) -------------------- #
    def search_modes(self) -> list[str]:
        """Which search modes are live: lexical always; semantic/hybrid once an embedder is installed.
        Surfaced through /catalog/facets so the UI can offer a search-by-meaning toggle."""
        return ["lexical", "semantic", "hybrid"] if self._embedder is not None else ["lexical"]

    def set_embedder(self, fn, model: str = "custom") -> None:
        """Install an embedder — `fn(list[str]) -> list[list[float]]`. Kicks off a best-effort
        background reindex so already-registered datasets become semantically searchable too."""
        self._embedder = fn
        self._embed_model = model or "custom"
        threading.Thread(target=self._reindex_embeddings, daemon=True).start()

    @staticmethod
    def _embed_text(t: CatalogTable) -> str:
        cols = " ".join(c.name for c in t.columns[:64])
        return " ".join(x for x in (t.name, t.folder, t.description or "", " ".join(t.tags), cols) if x)

    def _embed_one(self, t: CatalogTable) -> None:
        if self._embedder is None:
            return
        try:
            import numpy as np
            vec = self._embedder([self._embed_text(t)])[0]
            arr = np.asarray(vec, dtype=np.float32)
            metadb.catalog_set_embedding(t.uri, self._embed_model, int(arr.shape[0]), arr.tobytes())
            self._emb_dirty += 1  # invalidate the in-process score matrix
        except Exception:  # noqa: BLE001 — semantic index is best-effort; never break a register
            log.debug("embed failed for %s", t.uri, exc_info=True)

    _REINDEX_PAGE = 500     # DB page per reindex step
    _REINDEX_CHUNK = 128    # texts per embedder call (models batch far better than 1-at-a-time)

    def _reindex_embeddings(self) -> None:
        """Walk the WHOLE catalog page by page (not just the first page) and embed every entry that
        has no vector yet, in chunks — so a 50k-table catalog converges instead of silently stopping.
        Serialized by a lock (set_embedder's background thread + explicit calls), and one failed chunk
        (a bad row, a concurrent write) skips forward instead of aborting the walk."""
        if self._embedder is None:
            return
        with self._reindex_lock:
            try:
                import numpy as np
                have = {u for u, _ in metadb.catalog_embeddings_for(self._embed_model)}
                offset = 0
                while True:
                    page = self.list_page(CatalogQuery(limit=self._REINDEX_PAGE, offset=offset))
                    todo = [t for t in page.items if t.uri not in have]
                    for i in range(0, len(todo), self._REINDEX_CHUNK):
                        chunk = todo[i:i + self._REINDEX_CHUNK]
                        try:
                            vecs = self._embedder([self._embed_text(t) for t in chunk])
                            for t, vec in zip(chunk, vecs):
                                arr = np.asarray(vec, dtype=np.float32)
                                metadb.catalog_set_embedding(t.uri, self._embed_model,
                                                             int(arr.shape[0]), arr.tobytes())
                        except Exception:  # noqa: BLE001
                            log.debug("embed chunk failed (skipping)", exc_info=True)
                    if todo:
                        self._emb_dirty += 1
                    if not page.has_more:
                        break
                    offset += len(page.items)
            except Exception:  # noqa: BLE001
                log.debug("catalog embedding reindex failed", exc_info=True)

    def _embedding_matrix(self):
        """(uris, matrix, norms) for the current model, cached in-process: reloading + re-stacking
        every vector per query is the expensive part of semantic search (megabytes at 10k tables).
        Invalidated by local writes (`_emb_dirty`) and a short TTL (cross-instance writes)."""
        import time
        import numpy as np
        cached = self._emb_cache
        if cached and cached[0] == self._emb_dirty and time.monotonic() - cached[1] < 30.0:
            return cached[2]
        rows = metadb.catalog_embeddings_for(self._embed_model)
        if not rows:
            data = None
        else:
            uris = [u for u, _ in rows]
            mat = np.stack([np.frombuffer(v, dtype=np.float32) for _, v in rows])
            norms = np.linalg.norm(mat, axis=1)
            norms[norms == 0] = 1.0
            data = (uris, mat, norms)
        self._emb_cache = (self._emb_dirty, time.monotonic(), data)
        return data

    def semantic_search(self, q: str, limit: int = 50) -> list[CatalogTable]:
        """Rank datasets by cosine similarity of their embedding to the query's. Empty if no embedder."""
        if self._embedder is None or not q.strip():
            return []
        try:
            import numpy as np
            qv = np.asarray(self._embedder([q])[0], dtype=np.float32)
            qn = float(np.linalg.norm(qv)) or 1.0
            data = self._embedding_matrix()
            if data is None:
                return []
            uris, mat, norms = data
            scores = (mat @ qv) / (norms * qn)
            order = np.argsort(-scores)[:limit]
            ranked = [uris[i] for i in order]
        except Exception:  # noqa: BLE001
            log.debug("semantic search failed", exc_info=True)
            return []
        docs = metadb.catalog_get_many(ranked)
        dmap = self._declared_keys(ranked)
        return [self._overlay(self._to_table(docs[u]), dmap) for u in ranked if u in docs]

    def search(self, q: str, mode: str = "hybrid", limit: int = 50) -> list[CatalogTable]:
        """Search the catalog. `mode`: 'lexical' (name/folder/tag/column substring + facets),
        'semantic' (embedding similarity — needs an embedder), or 'hybrid' (both, fused by reciprocal
        rank). With no embedder, semantic/hybrid gracefully fall back to lexical, so search always works
        offline."""
        lexical = self.list_page(CatalogQuery(q=q, limit=limit)).items
        if mode == "lexical" or self._embedder is None:
            return lexical
        semantic = self.semantic_search(q, limit=limit)
        if mode == "semantic":
            return semantic or lexical
        # hybrid: reciprocal-rank fusion of the two orderings
        k = 60.0
        score: dict[str, float] = {}
        keep: dict[str, CatalogTable] = {}
        for lst in (lexical, semantic):
            for rank, t in enumerate(lst):
                score[t.uri] = score.get(t.uri, 0.0) + 1.0 / (k + rank)
                keep[t.uri] = t
        return [keep[u] for u in sorted(score, key=lambda u: -score[u])][:limit]

    # -- declared keys & relationships (owner-asserted; per-ROW, cross-instance) --- #
    def _declared_keys(self, uris: list[str] | None = None) -> dict[str, list[str]]:
        """Declared keys for `uris` (the page being served) — an indexed batch lookup, so the browse
        read path stays O(page) even with many declared keys."""
        try:
            return metadb.catalog_declared_keys(uris)
        except Exception:  # noqa: BLE001
            return {}

    def set_declared_key(self, uri: str, columns: list[str] | None) -> None:
        metadb.catalog_set_declared_key(uri, list(columns or []))

    def relationships(self, uri: str | None = None) -> list[Relationship]:
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
        import json
        ends = sorted([[r.left_uri, list(r.left_columns)], [r.right_uri, list(r.right_columns)]])
        return json.dumps(ends)

    def add_relationship(self, rel: Relationship) -> None:
        metadb.catalog_upsert_relationship(self._rel_key(rel), rel.model_dump(by_alias=True))

    def remove_relationship(self, rel: Relationship) -> None:
        metadb.catalog_delete_relationship(self._rel_key(rel))


class CatalogCompat:
    """Adapter that lets a provider written against the PRE-scale CatalogProvider protocol (only
    list_tables/get_table/… — no list_page/facets/browse/search/set_metadata) keep working behind the
    new discovery routes. `reg.set_catalog` wraps such providers automatically. The fallbacks realize
    one bounded list_tables() call and filter in Python — the OLD provider's own cost model, so
    nothing regresses; a provider that wants pushdown implements the new methods and skips the shim.
    Everything the inner provider DOES have passes straight through (__getattr__)."""

    _FALLBACK_CAP = 5000  # matches LIST_TABLES_CAP — the old surface was already bounded by this

    def __init__(self, inner):
        self._inner = inner
        self.name = getattr(inner, "name", "catalog")

    def __getattr__(self, attr):
        return getattr(self._inner, attr)

    def _all(self, q: str | None) -> list[CatalogTable]:
        return list(self._inner.list_tables(q) or [])[: self._FALLBACK_CAP]

    @staticmethod
    def _matches(t: CatalogTable, query: CatalogQuery) -> bool:
        if query.folder:
            f = query.folder.strip("/")
            tf = (t.folder or "").strip("/")
            if tf != f and not tf.startswith(f + "/"):
                return False
        if query.uris and t.uri not in query.uris:
            return False
        if query.owner and t.owner != query.owner:
            return False
        have_tags = set(t.tags or [])
        if any(tag not in have_tags for tag in query.tags):
            return False
        have_cols = {c.name for c in (t.columns or [])}
        return not any(c not in have_cols for c in query.has_columns)

    def list_page(self, query: CatalogQuery) -> CatalogPage:
        if hasattr(self._inner, "list_page"):
            return self._inner.list_page(query)
        items = [t for t in self._all(query.q) if self._matches(t, query)]
        keys = {"name": lambda t: (t.name or "").lower(), "rows": lambda t: t.row_count or 0,
                "usage": lambda t: getattr(t, "usage", 0) or 0, "folder": lambda t: (t.folder or "").lower(),
                "updated": lambda t: getattr(t, "updated_at", None) or ""}
        items.sort(key=keys.get(query.sort, keys["name"]), reverse=query.order == "desc")
        window = items[query.offset:query.offset + query.limit]
        return CatalogPage(items=window, total=len(items), offset=query.offset, limit=query.limit,
                           has_more=query.offset + len(window) < len(items))

    def facets(self, query: CatalogQuery) -> Facets:
        if hasattr(self._inner, "facets"):
            return self._inner.facets(query)
        from collections import Counter
        items = [t for t in self._all(query.q) if self._matches(t, query)]
        folders = Counter((t.folder or "").strip("/") for t in items if t.folder)
        owners = Counter(t.owner for t in items if t.owner)
        tags = Counter(tag for t in items for tag in (t.tags or []))
        fv = lambda c: [FacetValue(value=v, count=n) for v, n in c.most_common(100)]  # noqa: E731
        return Facets(folders=fv(folders), tags=fv(tags), owners=fv(owners))

    def browse(self, prefix: str = "") -> CatalogBrowse:
        if hasattr(self._inner, "browse"):
            return self._inner.browse(prefix)
        p = (prefix or "").strip("/")
        depth = 0 if not p else p.count("/") + 1
        items = self._all(None)
        children: dict[str, int] = {}
        direct: list[CatalogTable] = []
        for t in items:
            f = (t.folder or "").strip("/")
            if f == p:
                direct.append(t)
                continue
            if p and not f.startswith(p + "/"):
                continue
            segs = f.split("/") if f else []
            if len(segs) > depth:
                cp = "/".join(segs[: depth + 1])
                children[cp] = children.get(cp, 0) + 1
        return CatalogBrowse(
            prefix=p,
            folders=[FolderNode(name=cp.split("/")[-1], path=cp, table_count=n)
                     for cp, n in sorted(children.items(), key=lambda kv: kv[0].lower())],
            tables=sorted(direct, key=lambda t: (t.name or "").lower())[:100],
            total_tables=len(direct), truncated=len(direct) > 100)

    def search(self, q: str, mode: str = "hybrid", limit: int = 50) -> list[CatalogTable]:
        if hasattr(self._inner, "search"):
            return self._inner.search(q, mode=mode, limit=limit)
        return self._all(q)[:limit]

    def search_modes(self) -> list[str]:
        fn = getattr(self._inner, "search_modes", None)
        return fn() if callable(fn) else ["lexical"]

    def set_metadata(self, uri: str, **kwargs) -> CatalogTable:
        fn = getattr(self._inner, "set_metadata", None)
        if callable(fn):
            return fn(uri, **kwargs)
        raise NotImplementedError(f"catalog provider '{self.name}' does not support curation")

    def lineage(self, uri: str, depth: int = DEFAULT_LINEAGE_DEPTH,
                max_nodes: int = DEFAULT_LINEAGE_MAX_NODES) -> LineageResult:
        try:
            return self._inner.lineage(uri, depth=depth, max_nodes=max_nodes)
        except TypeError:  # old signature: lineage(uri)
            return self._inner.lineage(uri)
