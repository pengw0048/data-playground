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

import hashlib
import json
import logging
import os
import threading
import uuid

from hub import metadb
from hub.models import (
    CatalogBrowse,
    CatalogPage,
    CatalogPublicationReceipt,
    CatalogQuery,
    CatalogTable,
    Facets,
    FacetValue,
    FolderNode,
    KeyInfo,
    LineageEdge,
    LineageFact,
    LineageFactsPage,
    LineageNode,
    LineagePublication,
    LineageResult,
    Relationship,
)

log = logging.getLogger("hub")

# Bounded defaults for lineage traversal — a large, densely-connected component is capped so the graph
# a client renders (and the payload) stays sane; `truncated` tells the UI there was more.
DEFAULT_LINEAGE_DEPTH = 6
DEFAULT_LINEAGE_MAX_NODES = 500


def canonical_lineage_parent_tokens(parents: list[str] | None) -> list[str]:
    """Expose the core lineage wire canonicalizer without giving runners metadata-table access."""
    return metadb.catalog_lineage_parent_tokens(parents)


def lineage_for_output(graph, backend_run_id: str, step_id: str, *,
                       attempt_id: str | None = None,
                       idempotency_key: str | None = None) -> LineagePublication:
    """Resolve the catalog-only identity for one execution sink.

    A placed final region keeps its own backend run for status/cancellation, while the graph's private
    publication context preserves the researcher-visible outer run and original canvas producer.
    """
    logical_run = getattr(graph, "_publication_run_id", None) or str(backend_run_id)
    producer = getattr(graph, "_publication_producer_id", None) or str(graph.id)
    producer_version = getattr(graph, "_publication_producer_version", None)
    if producer_version is None:
        producer_version = int(graph.version)
    execution_attempt = attempt_id or getattr(graph, "_publication_attempt_id", None)
    if execution_attempt is None and logical_run != str(backend_run_id):
        execution_attempt = str(backend_run_id)
    if idempotency_key is None:
        identity = json.dumps({
            "run": logical_run,
            "attempt": execution_attempt,
            "producer": producer,
            "producerVersion": producer_version,
            "step": str(step_id),
        }, sort_keys=True, separators=(",", ":"))
        idempotency_key = "lineage-run:v1:sha256:" + hashlib.sha256(identity.encode()).hexdigest()
    return LineagePublication(
        idempotency_key=idempotency_key,
        run_id=logical_run,
        attempt_id=execution_attempt,
        producer=producer,
        producer_version=producer_version,
        step_id=str(step_id),
        provenance="run",
    )


def _manual_lineage(pipeline: str | None) -> LineagePublication:
    # A manual register call has no caller-owned retry key. Treat each call as a distinct observation;
    # durable/imported producers must supply their own stable LineagePublication identity.
    return LineagePublication(
        idempotency_key=f"lineage-manual:v1:{uuid.uuid4().hex}",
        producer=pipeline,
        provenance="manual",
    )


class InMemoryCatalog:
    """The default CatalogProvider. Named for history — it's now DB-backed (the DB is authoritative and
    cross-instance); there is no in-memory table map to go stale."""

    name = "in-memory"
    # CONTRACT: folder create/rename/delete write the local metadb, which is authoritative only because
    # THIS provider's browse() reads it. A provider that owns an EXTERNAL namespace (e.g. subclasses to
    # override browse()) MUST set `folders_mutable = False` (or override the folder methods to hit its
    # own store) — otherwise the routes would report local-only writes as success. The routes refuse
    # (501) when this is False, before touching local state.
    folders_mutable = True

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
             description: str | None = None, parents: list[str] | None = None,
             pipeline: str | None = None, lineage: LineagePublication | None = None, *,
             strict_probe: bool = False,
             strict_persist: bool = False, _persist_table: bool = True,
             _embed_table: bool = True,
             _publication_event_key: str | None = None,
             _publication_requested_version: str | None = None) -> CatalogTable:
        import hashlib as _h
        # schema/count probe (may touch disk/network) OUTSIDE the lock so a slow adapter doesn't block
        # concurrent catalog reads; only the version/collision compute + upsert below is serialized.
        try:
            adapter = self.resolve(uri)
            columns = adapter.schema(uri)
            count = adapter.count(uri)
        except Exception as exc:  # noqa: BLE001 — unmanaged registrations may describe offline data
            if strict_probe:
                raise RuntimeError("output schema/count probe failed") from exc
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
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        applied = True
        with self._lock:
            prior = metadb.catalog_get(uri)  # by uri (PK)
            if prior is None:
                prior = metadb.object_attempt_catalog_prior(uri)
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
            folder = metadb.catalog_folder_normalize(folder or "")
            self._warn_schema_drift(prior, name, columns, version)
            table = CatalogTable(
                id=tid, name=name, uri=uri, row_count=count, version=version,
                columns=columns, keys=keys, meta=meta, folder=folder, tags=tags,
                owner=owner, description=description,
            )
            if _persist_table:
                applied = self._persist(
                    table, parents=parents, pipeline=pipeline, lineage=lineage,
                    strict=strict_persist,
                    publication_event_key=_publication_event_key,
                    publication_requested_version=_publication_requested_version)
        if _embed_table and applied:
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

    def _persist(self, table: CatalogTable, *, parents: list[str] | None = None,
                 pipeline: str | None = None, lineage: LineagePublication | None = None,
                 strict: bool = False,
                 publication_event_key: str | None = None,
                 publication_requested_version: str | None = None) -> bool:
        parent_uris = metadb.catalog_lineage_parent_tokens(parents)
        try:
            from hub.handoff import prepare_attempt_commit
            prepare_attempt_commit(table.uri)
            publication = lineage or (
                _manual_lineage(pipeline) if parent_uris else None)
            publication_doc = publication.model_dump() if publication is not None else None
            if publication_event_key is not None:
                return metadb.catalog_upsert_output_idempotent(
                    publication_event_key,
                    table.uri, table.name, table.model_dump(by_alias=True),
                    requested_version=publication_requested_version,
                    parents=parent_uris, pipeline=pipeline,
                    lineage=publication_doc,
                )
            return metadb.catalog_upsert_entry(
                table.uri, table.name, table.model_dump(by_alias=True),
                parents=parent_uris, pipeline=pipeline,
                lineage=publication_doc)
        except Exception as e:  # noqa: BLE001
            from hub.handoff import is_attempt_uri
            if (strict or parent_uris or lineage is not None
                    or metadb.object_attempt_is_managed(table.uri)
                    or is_attempt_uri(table.uri)):
                raise
            log.warning("catalog persist failed for %s (%s: %s)", table.name, type(e).__name__, e)
            return True

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
        root_key = metadb.catalog_lineage_root_key(uri)
        seen: set[str] = {root_key}
        frontier = [root_key]
        edges: dict[tuple[str, str], LineageEdge] = {}
        truncated = False
        hops = 0
        # cap one frontier expansion too — a hub node with 100k children must not load them all
        edge_cap = max(2000, max_nodes * 4)
        while frontier and hops < max(1, depth):
            hops += 1
            batch, pair_truncated = metadb.catalog_lineage_key_pairs_touching(
                frontier, limit=edge_cap)
            truncated = truncated or pair_truncated
            nxt: list[str] = []
            for e in batch:
                key = (e["parent"], e["child"])
                if key not in edges:
                    edges[key] = LineageEdge(parent=e["parent"], child=e["child"],
                                             fact_count=e["fact_count"])
                for end in (e["parent"], e["child"]):
                    if end in seen:
                        continue
                    if len(seen) >= max_nodes:
                        truncated = True
                        continue
                    seen.add(end)
                    nxt.append(end)
            frontier = nxt
        if frontier and not truncated:
            # Reaching the depth boundary is not itself truncation: a complete root->leaf graph at
            # depth=1 fits. Probe one bounded frontier to include edges between already-visible nodes
            # and learn whether an unseen endpoint exists.
            probe, probe_truncated = metadb.catalog_lineage_key_pairs_touching(
                frontier, limit=edge_cap)
            truncated = probe_truncated
            for item in probe:
                key = (item["parent"], item["child"])
                if item["parent"] in seen and item["child"] in seen:
                    if key not in edges:
                        edges[key] = LineageEdge(
                            parent=item["parent"], child=item["child"],
                            fact_count=item["fact_count"])
                else:
                    truncated = True
        projection, names = metadb.catalog_lineage_project_keys(list(seen))
        root_uri = projection.get(root_key, root_key)
        # Keep only edges whose endpoints survived the cap, then apply one current projection to the
        # root, nodes, and edges. Distinct stable pairs can converge on one visible pair, so aggregate
        # once more after projection.
        projected_counts: dict[tuple[str, str], int] = {}
        for edge in edges.values():
            if edge.parent not in seen or edge.child not in seen:
                continue
            pair = (
                projection.get(edge.parent, edge.parent),
                projection.get(edge.child, edge.child),
            )
            projected_counts[pair] = projected_counts.get(pair, 0) + edge.fact_count
        kept = [
            LineageEdge(parent=parent, child=child, fact_count=count)
            for (parent, child), count in sorted(projected_counts.items())
        ]
        visible_uris = sorted({projection.get(key, key) for key in seen})
        nodes = [LineageNode(id=(names.get(u, {}).get("id") or u),
                             name=(names.get(u, {}).get("name") or u.split("/")[-1]), uri=u)
                 for u in visible_uris]
        return LineageResult(
            root_uri=root_uri, nodes=nodes, edges=kept, truncated=truncated)

    def lineage_facts_page(self, *, limit: int, after_id: int) -> LineageFactsPage:
        """Export one bounded page from the same authoritative store as this provider's graph."""
        rows, next_after_id, has_more = metadb.catalog_lineage_facts_page(
            limit=limit, after_id=after_id)
        return LineageFactsPage(
            items=[LineageFact.model_validate({**row, "id": str(row["id"])}) for row in rows],
            next_after_id=str(next_after_id) if next_after_id is not None else None,
            has_more=has_more,
        )

    # -- CatalogProvider: write-back -------------------------------------- #
    def register(self, table: CatalogTable, parents: list[str] | None = None,
                 pipeline: str | None = None) -> None:
        with self._lock:
            self._persist(table, parents=parents, pipeline=pipeline)
        self._embed_one(table)

    def register_output(self, name: str, uri: str, version: str | None = None,
                        parents: list[str] | None = None, pipeline: str | None = None,
                        lineage: LineagePublication | None = None,
                        folder: str = "", tags: list[str] | None = None, owner: str | None = None,
                        description: str | None = None,
                        _bump_usage: bool = True, _require_durable: bool = False) -> CatalogTable:
        table = self._add(name=name, uri=uri, version=version, meta=pipeline, folder=folder,
                          tags=tags, owner=owner, description=description,
                          parents=parents, pipeline=pipeline, lineage=lineage,
                          strict_probe=_require_durable,
                          strict_persist=_require_durable)
        parent_uris = metadb.catalog_lineage_parent_tokens(parents)
        if _bump_usage:
            # Local/legacy calls are one completed run each and retain their per-call popularity bump.
            for parent in parent_uris:
                metadb.catalog_bump_usage(parent)
        return table

    def record_lineage(self, *, name: str, uri: str, version: str | None,
                       parents: list[str], lineage: LineagePublication) -> int:
        """Record facts for a previously published, exact catalog output without rewriting it."""
        observed = self.get_table(uri)
        if (observed.uri != str(uri).rstrip("/") or observed.name != name
                or observed.version != version):
            raise RuntimeError("lineage destination is not the exact current catalog output")
        return metadb.catalog_record_lineage(
            destination_uri=observed.uri,
            destination_version=observed.version,
            parents=metadb.catalog_lineage_parent_tokens(parents),
            lineage=lineage.model_dump(),
        )

    def publish_output_strict(self, name: str, uri: str, version: str | None = None,
                              parents: list[str] | None = None,
                              pipeline: str | None = None,
                              lineage: LineagePublication | None = None) -> CatalogTable:
        """Durably publish a just-written unmanaged output or propagate the metadata transaction failure."""
        table = self._add(
            name=name, uri=uri, version=version, meta=pipeline,
            parents=parents, pipeline=pipeline, lineage=lineage, strict_probe=True,
            strict_persist=True)
        for parent in parents or []:
            try:
                metadb.catalog_bump_usage(parent)
            except Exception:  # noqa: BLE001 — popularity is optional after durable publication
                log.warning("catalog usage bump failed after strict publication", exc_info=True)
        return table

    def prepare_managed_output_publication(
            self, *, run_id: str, step_id: str, idempotency_key: str, name: str, uri: str,
            version: str | None = None, parents: list[str] | None = None,
            pipeline: str | None = None,
            lineage: LineagePublication | None = None) -> dict:
        """Commit exact inventory and freeze schema/catalog metadata before effects can win."""
        if not run_id or not step_id or not idempotency_key:
            raise ValueError("managed publication run, step_id, and idempotency_key are required")
        if parents and lineage is None:
            raise ValueError("managed publication with sources requires lineage identity")
        if lineage is not None and lineage.idempotency_key != idempotency_key:
            raise ValueError("managed publication lineage identity does not match its effect")
        if (lineage is not None and lineage.provenance == "run"
                and lineage.step_id != step_id):
            raise ValueError("managed publication lineage step does not match its effect")
        parent_uris = metadb.catalog_lineage_parent_tokens(parents)
        from hub.handoff import managed_read_lease, prepare_attempt_commit

        prepare_attempt_commit(uri)
        try:
            deadline = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
        except ValueError:
            deadline = 3600.0
        ttl = max(300.0, deadline + 300.0)
        with managed_read_lease(
                uri, owner=f"catalog-plan:{name}", ttl_seconds=ttl,
                allow_committed=True) as guard:
            guard.check()
            table = self._add(
                name=name, uri=uri, version=version, meta=pipeline,
                parents=parents, pipeline=pipeline, lineage=lineage, strict_probe=True,
                _persist_table=False, _embed_table=False,
            )
        identity = metadb.managed_catalog_publication_identity(uri, run_id)
        plan = {
            "contract_version": 2,
            "run_id": str(run_id),
            "step_id": str(step_id),
            "ref_key": f"{run_id}:{step_id}",
            "generation": identity["generation"],
            "event_key": str(idempotency_key),
            "name": str(name),
            "uri": str(uri).rstrip("/"),
            "version": table.version,
            "parents": parent_uris,
            "pipeline": pipeline,
            "lineage": lineage.model_dump() if lineage is not None else None,
            "table_doc": table.model_dump(by_alias=True),
        }
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
        plan["fingerprint"] = "managed-output:v2:sha256:" + hashlib.sha256(
            canonical.encode()
        ).hexdigest()
        return plan

    def publish_managed_output(self, name: str, uri: str, version: str | None = None,
                               parents: list[str] | None = None,
                               pipeline: str | None = None,
                               lineage: LineagePublication | None = None, *,
                               idempotency_key: str | None = None,
                               prepared_plan: dict | None = None) -> dict:
        """Core single-sink publication: inventory proof, catalog pointer, ref, and state commit."""
        if prepared_plan is not None:
            parent_uris = metadb.catalog_lineage_parent_tokens(parents)
            if not idempotency_key or (
                    prepared_plan.get("event_key") != idempotency_key
                    or prepared_plan.get("name") != name
                    or prepared_plan.get("uri") != str(uri).rstrip("/")
                    or prepared_plan.get("version") != version
                    or prepared_plan.get("parents") != parent_uris
                    or prepared_plan.get("pipeline") != pipeline
                    or prepared_plan.get("lineage") != (
                        lineage.model_dump() if lineage is not None else None)):
                raise RuntimeError("managed publication arguments changed after effects staging")
            return metadb.catalog_apply_managed_publication(prepared_plan)
        existing = metadb.catalog_managed_publication_receipt(uri)
        if existing is not None:
            return {**existing, "table": self.get_table(uri)}
        from hub.handoff import prepare_attempt_commit
        prepare_attempt_commit(uri)
        try:
            from hub.handoff import managed_read_lease
            try:
                deadline = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
            except ValueError:
                deadline = 3600.0
            ttl = max(300.0, deadline + 300.0)
            with managed_read_lease(
                    uri, owner=f"catalog-publish:{name}", ttl_seconds=ttl,
                    allow_committed=True) as guard:
                guard.check()
                table = self._add(
                    name=name, uri=uri, version=version, parents=parents, pipeline=pipeline,
                    lineage=lineage,
                    strict_probe=True)
        except Exception:
            receipt = metadb.catalog_managed_publication_receipt(uri)
            if receipt is not None:
                return {**receipt, "table": self.get_table(uri)}
            metadb.abandon_committed_object_attempt(uri)
            raise
        receipt = metadb.catalog_managed_publication_receipt(uri)
        if receipt is None:
            raise RuntimeError("core managed publication did not return a durable receipt")
        return {**receipt, "table": table}

    def register_output_idempotent(
        self, idempotency_key: str, **kwargs
    ) -> CatalogPublicationReceipt:
        """Durable-executor write projection keyed by one logical output effect.

        The catalog entry remains the current URI projection; lineage is idempotent per publication and
        source, so separate runs between the same datasets remain separate facts. The Jobs publisher
        records one aggregate usage event after every output is registered, so a multi-sink run does not
        overcount parents.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        parents = metadb.catalog_lineage_parent_tokens(kwargs.get("parents"))
        lineage = kwargs.get("lineage")
        if parents and lineage is None:
            raise ValueError("durable catalog publication with sources requires lineage identity")
        if lineage is not None and lineage.idempotency_key != idempotency_key:
            raise ValueError("durable catalog publication lineage identity does not match its effect")
        requested_version = kwargs.get("version")
        lineage_doc = lineage.model_dump() if lineage is not None else None
        prior = metadb.catalog_unmanaged_output_publication_receipt(
            idempotency_key,
            kwargs.get("uri"), kwargs.get("name"), requested_version,
            parents=parents, pipeline=kwargs.get("pipeline"), lineage=lineage_doc,
        )
        if prior is not None:
            return CatalogPublicationReceipt(
                idempotency_key=idempotency_key,
                uri=prior["uri"], version=prior["version"],
            )
        self._add(
            meta=kwargs.get("pipeline"),
            _publication_event_key=idempotency_key,
            _publication_requested_version=requested_version,
            strict_probe=True,
            strict_persist=True,
            **kwargs,
        )
        persisted = metadb.catalog_unmanaged_output_publication_receipt(
            idempotency_key,
            kwargs.get("uri"), kwargs.get("name"), requested_version,
            parents=parents, pipeline=kwargs.get("pipeline"), lineage=lineage_doc,
        )
        if persisted is None:  # pragma: no cover - transaction contract guard
            raise RuntimeError("catalog publication did not return a durable receipt")
        return CatalogPublicationReceipt(
            idempotency_key=idempotency_key,
            uri=persisted["uri"], version=persisted["version"],
        )

    def record_usage_idempotent(self, idempotency_key: str, parents: list[str]) -> bool:
        """Count each distinct parent once for one durable run, independently of output cardinality."""
        return metadb.catalog_bump_usage_once(idempotency_key, parents)

    def set_metadata(self, uri: str, *, folder: str | None = None, tags: list[str] | None = None,
                     owner: str | None = None, description: str | None = None,
                     name: str | None = None) -> CatalogTable:
        """Update a dataset's organization (folder/tags/owner/description). None → field unchanged;
        for owner/description an empty string CLEARS the field (there's no meaningful empty owner),
        so a client can unset without a sentinel. folder='' files at the root; tags=[] clears tags.
        A non-blank `name` renames the dataset's friendly name (blank/None keeps the current one).
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
            name=(name.strip() or None) if name is not None else None,
        )
        t = self.get_table(uri)
        self._embed_one(t)  # description/tags changed → refresh the semantic vector
        return t

    # -- folder namespace mutation ---------------------------------------------------------------- #
    # This provider's browse() reads the same metadb these write, so folder create/rename/delete are
    # authoritative here. An external provider that owns its own namespace must override these (or set
    # folders_mutable=False) — the routes call the provider, not metadb, so local state is never a
    # silent side effect for a provider that doesn't support folder mutation.
    def list_folders(self) -> list[dict]:
        return metadb.catalog_folders_list()

    def create_folder(self, path: str) -> str:
        return metadb.catalog_folder_create(path)

    def rename_folder(self, old: str, new: str) -> None:
        metadb.catalog_folder_rename(old, new)

    def delete_folder(self, path: str) -> None:
        metadb.catalog_folder_delete(path)

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
            token = (id_or_name if metadb.object_attempt_is_managed(id_or_name)
                     else doc["uri"])
            metadb.catalog_delete_entry(token)
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

    def semantic_search(self, q: str, limit: int = 50,
                        query: CatalogQuery | None = None) -> list[CatalogTable]:
        """Rank datasets by cosine similarity, constrained by the query's structured filters."""
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
            allowed = metadb.catalog_filter_uris(
                folder=query.folder if query else None,
                tags=query.tags if query else None,
                owner=query.owner if query else None,
                has_columns=query.has_columns if query else None,
                uris=query.uris if query else None,
            )
            if allowed is None:
                order = np.argsort(-scores)[:limit]
            else:
                candidates = np.fromiter(
                    (i for i, uri in enumerate(uris) if uri in allowed), dtype=np.int64,
                )
                order = (candidates[np.argsort(-scores[candidates])[:limit]]
                         if len(candidates) else candidates)
            ranked = [uris[i] for i in order]
        except Exception:  # noqa: BLE001
            log.debug("semantic search failed", exc_info=True)
            return []
        docs = metadb.catalog_get_many(ranked)
        dmap = self._declared_keys(ranked)
        return [self._overlay(self._to_table(docs[u]), dmap) for u in ranked if u in docs]

    def search(self, q: str, mode: str = "hybrid", limit: int = 50,
               *, query: CatalogQuery | None = None) -> list[CatalogTable]:
        """Search the catalog. `mode`: 'lexical' (name/folder/tag/column substring + facets),
        'semantic' (embedding similarity — needs an embedder), or 'hybrid' (both, fused by reciprocal
        rank). With no embedder, semantic/hybrid gracefully fall back to lexical, so search always works
        offline."""
        effective = (query or CatalogQuery()).model_copy(update={
            "q": q, "limit": limit, "offset": 0,
        })
        lexical = self.list_page(effective).items
        if mode == "lexical" or self._embedder is None:
            return lexical
        semantic = self.semantic_search(q, limit=limit, query=effective)
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


def core_managed_publisher(catalog):
    """Return the core lifecycle publisher only; a lookalike custom method cannot claim this authority."""
    return catalog.publish_managed_output if type(catalog) is InMemoryCatalog else None


def core_managed_publication_planner(catalog):
    """Return the core pre-effects planner; custom lookalikes cannot mint lifecycle authority."""
    return catalog.prepare_managed_output_publication if type(catalog) is InMemoryCatalog else None


def core_unmanaged_publisher(catalog):
    """Return the inherited core strict writer; external catalogs use register + read-back."""
    if not isinstance(catalog, InMemoryCatalog):
        return None
    from functools import partial
    return partial(InMemoryCatalog.publish_output_strict, catalog)


def unmanaged_publication_supported(catalog) -> bool:
    """Whether a catalog can durably publish and attest a just-written unmanaged output."""
    return bool(
        core_unmanaged_publisher(catalog) is not None
        or (callable(getattr(catalog, "register_output", None))
            and callable(getattr(catalog, "get_table", None)))
    )


def publish_unmanaged_output_attested(catalog, *, name: str, uri: str,
                                      version: str | None = None,
                                      parents: list[str] | None = None,
                                      pipeline: str | None = None,
                                      lineage: LineagePublication | None = None):
    """Publish an unmanaged output and require an exact uri/name/version receipt."""
    publish = core_unmanaged_publisher(catalog)
    kwargs = {
        "name": name, "uri": uri, "version": version,
        "parents": parents, "pipeline": pipeline, "lineage": lineage,
    }
    if publish is not None:
        receipt = observed = publish(**kwargs)
    else:
        if not unmanaged_publication_supported(catalog):
            raise RuntimeError(
                "unmanaged output publication requires catalog registration with read-back")
        receipt = catalog.register_output(**kwargs)
        observed = catalog.get_table(uri)

    missing = object()

    def field(doc, key):
        if isinstance(doc, dict):
            return doc[key] if key in doc else missing
        return getattr(doc, key, missing)

    expected = (field(receipt, "uri"), field(receipt, "name"), field(receipt, "version"))
    actual = (field(observed, "uri"), field(observed, "name"), field(observed, "version"))
    if expected[0] != uri or expected[1] != name or missing in expected or actual != expected:
        raise RuntimeError("catalog publication read-back did not match its receipt")
    return observed


def record_cached_output_lineage(
        catalog, *, name: str, uri: str, version: str,
        parents: list[str], lineage: LineagePublication, pre_publish=None) -> bool:
    """Validate one cached catalog pointer and atomically add this run's facts when supported.

    A provider without a lineage-only recorder returns ``False`` so the runner recomputes and follows
    its ordinary output-publication path. Storage or collision failures from a supported recorder are
    not downgraded to cache misses.
    """
    from hub.backends import CatalogLineageRecorder
    if not isinstance(catalog, CatalogLineageRecorder):
        return False
    try:
        observed = catalog.get_table(uri)
    except (KeyError, FileNotFoundError):
        return False

    def field(key: str):
        return observed.get(key) if isinstance(observed, dict) else getattr(observed, key, None)

    observed_uri = field("uri")
    observed_name = field("name")
    observed_version = field("version")
    if (observed_uri != str(uri).rstrip("/") or observed_name != name
            or observed_version != version):
        return False
    if pre_publish is not None:
        pre_publish()
    recorded = catalog.record_lineage(
        name=name, uri=observed_uri, version=observed_version,
        parents=parents, lineage=lineage,
    )
    if (isinstance(recorded, bool) or not isinstance(recorded, int)
            or recorded < 0):
        raise RuntimeError("catalog lineage recorder returned an invalid durable receipt")
    return True
