"""Catalog, data preview, pipelines, processors — the data/discovery routes.

Split out of main.py. All routes are authed: main includes this router with
`dependencies=[Depends(current_user)]`, so the whole surface is gated by default.
"""

from __future__ import annotations

import contextlib
import glob
import os
import re
import tempfile
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hub import db, graph as g, metadb
from hub.deps import get_deps
from hub.executors.engine import _table_to_rows
from hub.plugins.adapters import is_object_uri, path_of, relation_columns
from hub.plugins.importer import ImporterNotConfigured
from hub.settings import settings
from hub.storage import ManagedSourceReadError, source_read_scope
from hub.models import (
    CatalogBrowse,
    CatalogMetadata,
    CatalogQuery,
    CatalogTable,
    ColumnSchema,
    Facets,
    ImportRequest,
    JoinSuggestion,
    KernelInfo,
    LineageEdge,
    LineageResult,
    PipelineImport,
    ProcessorDescriptor,
    Relationship,
    SampleRequest,
    SampleResult,
)


def _csv(v: str | None) -> list[str]:
    """A comma-separated query param → a clean list (['a','b'] from 'a, b, ')."""
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _catalog_query(q, folder, tags, owner, has_columns, sort, order, limit, offset, uris=None) -> CatalogQuery:
    # list params are capped like `limit` is — an unbounded ?uris=/tags= list would otherwise become
    # an arbitrarily large IN clause / EXISTS chain
    return CatalogQuery(
        q=q or None, folder=folder or None, tags=_csv(tags)[:50], owner=owner or None,
        uris=list(uris or [])[:500], has_columns=_csv(has_columns)[:50],
        sort=sort if sort in ("name", "rows", "updated", "usage", "folder") else "name",
        order="desc" if order == "desc" else "asc",
        limit=max(1, min(int(limit), 500)), offset=max(0, int(offset)))

router = APIRouter()

@router.get("/kernel", response_model=KernelInfo)
def kernel_info() -> KernelInfo:
    return get_deps().info()


@router.get("/nodes")
def list_nodes() -> list[dict]:
    """Schema of every registered node (built-in + plugin) — powers generic rendering (§4.2)."""
    return [s.model_dump(by_alias=True) for s in get_deps().node_specs.values()]


@router.get("/plugins")
def list_plugins() -> list[dict]:
    # Enrich each pack that declares a [[config]] schema with its CURRENT values (from settings), so the
    # Settings UI can render + pre-fill a form. Secret fields store references (env:/file:), not material
    # values — the reference string is safe to echo; presence is also listed in config_set.
    from hub.secrets import redact_secret_for_display
    out: list[dict] = []
    for p in get_deps().plugins:
        entry = dict(p)
        schema = entry.get("config")
        if schema:
            values: dict = {}
            is_set: list[str] = []
            for f in schema:
                stored = metadb.get_setting(f"plugin.{p['name']}.{f['key']}", "global", default=None)
                if stored not in (None, ""):
                    is_set.append(f["key"])
                # References for secrets are safe to echo; mask any residual legacy plaintext.
                values[f["key"]] = (redact_secret_for_display(stored) if f.get("secret") else stored)
            entry["config_values"] = values
            entry["config_set"] = is_set
        out.append(entry)
    return out


class SaveSchemaRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    name: str
    columns: list[ColumnSchema]


@router.get("/schemas")
def list_schemas() -> list[dict]:
    """Every named schema contract's latest version — the registry view + the reference picker."""
    return metadb.list_schema_contracts()


@router.post("/schemas")
def save_schema(req: SaveSchemaRequest) -> dict:
    """Save a named contract as a NEW version (drift is a diff between versions, never an overwrite)."""
    if not req.name.strip():
        raise HTTPException(400, "a schema contract needs a name")
    cols = [{"name": c.name, "type": c.type} for c in req.columns]
    version = metadb.save_schema_contract(req.name.strip(), cols)
    return {"name": req.name.strip(), "version": version, "columns": cols}


@router.get("/schemas/diff")
def diff_schemas(name: str = Query(...), a: int = Query(...), b: int = Query(...)) -> dict:
    """Structural diff of two versions of a named contract (which fields were added / removed / retyped)."""
    ca, cb = metadb.get_schema_contract(name, a), metadb.get_schema_contract(name, b)
    if ca is None or cb is None:
        raise HTTPException(404, "unknown contract name or version")
    return metadb.diff_columns(ca["columns"], cb["columns"])


@router.get("/schemas/{name}")
def get_schema(name: str) -> dict:
    """A contract's latest columns + all its version numbers."""
    c = metadb.get_schema_contract(name)
    if c is None:
        raise HTTPException(404, f"no schema contract named '{name}'")
    return {**c, "versions": metadb.schema_contract_versions(name)}


@router.get("/catalog/tables", response_model=list[CatalogTable])
def list_tables(
    response: Response,
    q: str | None = None,
    folder: str | None = None,
    tags: str | None = None,          # comma-separated; ALL must match
    owner: str | None = None,
    uris: list[str] | None = Query(None),  # repeated ?uris=…&uris=… — batch "get these exact uris" (no 404 on a miss)
    has_columns: str | None = Query(None, alias="hasColumns"),  # comma-separated
    sort: str = "name",               # name | rows | updated | usage | folder
    order: str = "asc",               # asc | desc
    limit: int = 50,
    offset: int = 0,
) -> list[CatalogTable]:
    """A filtered, sorted, paginated page of the catalog. Backward-compatible on the wire: the body is
    still a bare `list[CatalogTable]` (so existing callers keep working), while the TOTAL match count +
    whether more follow ride along in `X-Total-Count` / `X-Has-More` headers for a paginating UI. This
    is what lets the Tables view browse thousands of datasets without ever loading them all."""
    query = _catalog_query(q, folder, tags, owner, has_columns, sort, order, limit, offset, uris=uris)
    page = get_deps().catalog.list_page(query)
    response.headers["X-Total-Count"] = str(page.total)
    response.headers["X-Has-More"] = "1" if page.has_more else "0"
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count, X-Has-More"
    return page.items


@router.get("/catalog/facets", response_model=Facets)
def catalog_facets(
    q: str | None = None,
    folder: str | None = None,
    tags: str | None = None,
    owner: str | None = None,
    has_columns: str | None = Query(None, alias="hasColumns"),
) -> Facets:
    """Distinct folder / tag / owner values + counts over the active filter set — powers the facet rail
    (each value is a one-click, counted filter). Computed with the SAME filters as the list, so the
    counts always describe what a click would show. `semanticAvailable` rides along so the UI knows
    whether a search-by-meaning mode exists (an embedder plugin is installed)."""
    query = _catalog_query(q, folder, tags, owner, has_columns, "name", "asc", 1, 0)
    cat = get_deps().catalog
    out = cat.facets(query)
    modes = getattr(cat, "search_modes", None)
    out.semantic_available = "semantic" in modes() if callable(modes) else False
    return out


@router.get("/catalog/tree", response_model=CatalogBrowse)
def catalog_tree(prefix: str = "") -> CatalogBrowse:
    """One level of the folder/browse tree at `prefix`: immediate child folders (with subtree counts) +
    the tables filed directly here. Lets a UI lazily expand a folder tree of any size."""
    return get_deps().catalog.browse(prefix)


@router.get("/catalog/search", response_model=list[CatalogTable])
def catalog_search(
    q: str = Query(...),
    mode: str = "hybrid",
    limit: int = 50,
    folder: str | None = None,
    tags: str | None = None,
    owner: str | None = None,
    uris: list[str] | None = Query(None),
    has_columns: str | None = Query(None, alias="hasColumns"),
) -> list[CatalogTable]:
    """Search the catalog. `mode`: 'lexical' (name/folder/tag/column substring), 'semantic' (embedding
    similarity — active only when a plugin registered an embedder), or 'hybrid' (both, rank-fused).
    Structured filters use the same CatalogQuery contract in every mode. Falls back to lexical when no
    embedder is installed, so search always works offline."""
    bounded = max(1, min(int(limit), 200))
    query = _catalog_query(q, folder, tags, owner, has_columns, "name", "asc", bounded, 0, uris=uris)
    from hub.plugins.catalog import search_with_query
    return search_with_query(get_deps().catalog, query, mode, bounded)


@router.get("/catalog/tables/{table_id}", response_model=CatalogTable)
def get_table(table_id: str) -> CatalogTable:
    try:
        return get_deps().catalog.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")


@router.put("/catalog/tables/{table_id}/metadata", response_model=CatalogTable)
def set_table_metadata(table_id: str, req: CatalogMetadata) -> CatalogTable:
    """Curate a dataset's organization — file it into a `folder`, set `tags`, an `owner`, a
    `description`. Only the fields PRESENT in the body change (absent → untouched); an explicit
    null (or "") on owner/description CLEARS it. The probed schema/rows are untouched."""
    cat = get_deps().catalog
    try:
        table = cat.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")
    sent = req.model_fields_set
    # absent field → None (provider preserves); explicit null → "" (provider clears). name is
    # rename-only: a blank name is ignored (a dataset always keeps a name), never cleared.
    return cat.set_metadata(
        table.uri,
        folder=(req.folder or "") if "folder" in sent else None,
        tags=req.tags if "tags" in sent else None,
        owner=(req.owner or "") if "owner" in sent else None,
        description=(req.description or "") if "description" in sent else None,
        name=req.name if "name" in sent else None)


@router.get("/catalog/lineage", response_model=LineageResult)
def lineage(uri: str = Query(...), depth: int = 6, max_nodes: int = Query(500, alias="maxNodes")) -> LineageResult:
    """The lineage component around `uri`, expanded breadth-first from the store and CAPPED by `depth`
    + `max_nodes` so a large graph can't blow up the payload (`truncated` flags that the cap hit)."""
    return get_deps().catalog.lineage(uri, depth=max(1, min(int(depth), 20)),
                                      max_nodes=max(1, min(int(max_nodes), 5000)))


@router.get("/catalog/edges", response_model=list[LineageEdge])
def lineage_edges(response: Response, limit: int = 500, offset: int = 0) -> list[LineageEdge]:
    """A page of the WHOLE lineage edge set (`X-Total-Count` rides in a header) — the bulk-export
    surface an external lineage store syncs from. Edges are URI-keyed `{parent, child, column,
    pipeline}`, so a bridge plugin can map them onto OpenLineage-style datasets 1:1."""
    rows, total = metadb.catalog_edges_page(limit=max(1, min(int(limit), 2000)), offset=max(0, int(offset)))
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return [LineageEdge(**r) for r in rows]


@router.delete("/catalog/tables/{table_id}")
def unregister_table(table_id: str) -> dict:
    """Remove a dataset from the catalog (e.g. a dead entry whose file was deleted)."""
    if not get_deps().catalog.unregister(table_id):
        raise HTTPException(404, f"table '{table_id}' not found")
    return {"ok": True}


class UnregisterManyRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    ids: list[str]


@router.post("/catalog/tables/delete")
def unregister_tables(req: UnregisterManyRequest) -> dict:
    """Batch-remove datasets from the catalog (the Tables view's multi-select delete). Returns which
    ids were removed and which were already gone, so a partial result is reported honestly."""
    cat = get_deps().catalog
    deleted: list[str] = []
    missing: list[str] = []
    for tid in req.ids:
        (deleted if cat.unregister(tid) else missing).append(tid)
    return {"deleted": deleted, "missing": missing}


class JoinSuggestRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    left_uri: str
    right_uri: str


class DeclareKeyRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    columns: list[str] = []  # empty → clear the declared key


@router.put("/catalog/tables/{table_id}/key", response_model=CatalogTable)
def declare_key(table_id: str, req: DeclareKeyRequest) -> CatalogTable:
    """Set (or clear, with []) a dataset's owner-declared primary key — leads its keys and wins in
    grain. The escape hatch when the name heuristic misses or an opaque transform produced the data."""
    cat = get_deps().catalog
    try:
        table = cat.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")
    have = {c.name for c in table.columns}
    missing = [c for c in req.columns if c not in have]
    if missing:
        raise HTTPException(400, f"columns not in '{table.name}': {', '.join(missing)}")
    cat.set_declared_key(table.uri, req.columns)
    return cat.get_table(table.id)


@router.get("/catalog/relationships", response_model=list[Relationship])
def list_relationships(uri: str | None = None) -> list[Relationship]:
    return get_deps().catalog.relationships(uri)


@router.post("/catalog/relationships", response_model=list[Relationship])
def add_relationship(rel: Relationship) -> list[Relationship]:
    get_deps().catalog.add_relationship(rel)
    return get_deps().catalog.relationships()


@router.post("/catalog/relationships/delete", response_model=list[Relationship])
def delete_relationship(rel: Relationship) -> list[Relationship]:
    """POST (not DELETE) — the relationship identity is a body, and DELETE-with-body is unreliable."""
    get_deps().catalog.remove_relationship(rel)
    return get_deps().catalog.relationships()


@router.post("/catalog/join-suggestions", response_model=list[JoinSuggestion])
def join_suggestions(req: JoinSuggestRequest) -> list[JoinSuggestion]:
    """Ranked ways to join two catalog datasets, with cardinality MEASURED on the data (see
    hub.relationships) — the catalog-driven 'how do these join?' hint."""
    from hub import relationships as rel
    deps = get_deps()

    def resolve(uri: str):
        try:
            table = deps.catalog.get_table(uri)
            return table.uri, table.columns
        except KeyError:
            return uri, None
    try:
        left_uri, left_cols = resolve(req.left_uri)
        right_uri, right_cols = resolve(req.right_uri)
        from hub import paths
        paths.ensure_local_uri_allowed(left_uri)
        paths.ensure_local_uri_allowed(right_uri)
        with source_read_scope(
                deps.storage, [left_uri, right_uri],
                owner=f"catalog-join:{uuid.uuid4().hex}"):
            left_cols = (left_cols if left_cols is not None
                         else deps.resolve_adapter(left_uri).schema(left_uri))
            right_cols = (right_cols if right_cols is not None
                          else deps.resolve_adapter(right_uri).schema(right_uri))
            return rel.suggest_joins(left_cols, right_cols,
                                     rel.measured_unique(left_uri, deps.resolve_adapter),
                                     rel.measured_unique(right_uri, deps.resolve_adapter))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


class RegisterRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    uri: str
    name: str | None = None
    folder: str | None = None          # optional: file it into a browse folder at register time
    tags: list[str] | None = None
    owner: str | None = None
    description: str | None = None


@router.post("/catalog/register", response_model=CatalogTable)
def catalog_register(req: RegisterRequest) -> CatalogTable:
    deps = get_deps()
    has_scheme = bool(re.match(r"^[a-z][a-z0-9+.-]*://", req.uri, re.I))
    uri = req.uri if has_scheme else os.path.abspath(os.path.expanduser(req.uri))
    name = req.name or os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
    from hub import paths
    try:
        paths.ensure_local_uri_allowed(uri)  # multi-user: don't let a user register an arbitrary local file
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        with source_read_scope(
                deps.storage, [uri], owner=f"catalog-register:{uuid.uuid4().hex}"):
            deps.resolve_adapter(uri).schema(uri)  # validate readable
            # Retain the read guard through the catalog-entry transaction: its durable reference must
            # commit before the temporary reader disappears and makes the artifact reclaimable.
            return deps.catalog.register_output(
                name=name, uri=uri, parents=[], folder=(req.folder or "").strip("/"),
                tags=req.tags, owner=req.owner, description=req.description)
    except HTTPException:
        raise
    except ManagedSourceReadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"cannot read '{uri}': {e}")


# --------------------------------------------------------------------------- #
# Upload (bytes → shared storage → catalog)
# --------------------------------------------------------------------------- #
# The formats DuckDB can read directly. Lance is a directory, not a single uploaded file, so it's out.
_UPLOAD_EXTS = (".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc")


def _land_upload(deps, tmp_path: str, target_uri: str) -> str:
    """Move the just-received temp file to its final home and return the landed uri.

    LOCAL: a byte-for-byte atomic rename (tmp is created in the target dir, so os.replace is atomic and
    never crosses a filesystem). OBJECT STORE: there is no generic multi-backend raw-PUT, so round-trip
    the bytes through DuckDB's httpfs write path (parquet→parquet, csv→csv, json→json). That writer picks
    the format purely by extension, so where the exact byte format can't be preserved the target extension
    is normalized to match what actually gets written — else a reader would mis-parse: tsv→csv (write emits
    commas), ndjson→json (write emits a JSON array), arrow/feather→parquet (no object-store reader at all).
    The returned uri reflects any such extension change.
    """
    if is_object_uri(target_uri):
        base, low = os.path.splitext(target_uri)[0], target_uri.lower()
        if low.endswith((".arrow", ".feather", ".ipc")):
            target_uri = base + ".parquet"
        elif low.endswith(".tsv"):
            target_uri = base + ".csv"
        elif low.endswith(".ndjson"):
            target_uri = base + ".json"
        with db.run_scope():  # own cursor — an upload re-encode doesn't hold the base lock / block runs
            rel = deps.resolve_adapter(tmp_path).scan(tmp_path)
            deps.resolve_adapter(target_uri).write(target_uri, rel)
        return target_uri
    dest = path_of(target_uri)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    os.replace(tmp_path, dest)  # atomic (same dir as the temp); leaves nothing to clean up
    return target_uri


def _finalize_upload(deps, tmp: str, target: str, name: str) -> CatalogTable:
    """Validate → land → register. All DuckDB work, so it runs in a threadpool (not the event loop).
    Validate the temp file FIRST so an unreadable upload is rejected without leaving an orphan behind."""
    try:
        deps.resolve_adapter(tmp).schema(tmp)  # the uploaded bytes are readable (metadata only, limit=0)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"uploaded file is not readable: {e}")
    final = _land_upload(deps, tmp, target)
    with db.run_scope():  # own cursor — register's count(*) full-scan (CSV/JSON) must not hold the base lock
        return deps.catalog.register_output(name=name, uri=final, parents=[])  # content-addressed version


@router.post("/catalog/upload", response_model=CatalogTable)
async def catalog_upload(request: Request) -> CatalogTable:
    """Upload a dataset file's bytes and register it. The raw request body IS the file; its name comes in
    the X-Upload-Filename header. Bytes STREAM to a temp file, capped at DP_MAX_UPLOAD_BYTES as they arrive
    (so an oversized upload is aborted without being buffered anywhere — no reliance on Content-Length),
    then land in shared storage (a local dir, or object storage when DP_STORAGE_URL is set — visible to
    every instance) and register into the cross-instance catalog, returned as a CatalogTable."""
    deps = get_deps()
    raw = os.path.basename(unquote(request.headers.get("x-upload-filename") or "").replace("\\", "/")) or "upload"
    stem, ext = os.path.splitext(raw)
    ext = ext.lower()
    if ext not in _UPLOAD_EXTS:
        raise HTTPException(400, f"unsupported file type '{ext or raw}' — upload Parquet / CSV / TSV / JSON / Arrow")
    name = re.sub(r"[\x00-\x1f\x7f]", "", stem) or "upload"  # strip control chars (they flow into the table id + UI)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "upload"
    # short suffix so two uploads of the same filename don't silently clobber each other (the catalog
    # still shows the clean name; only the stored file path is uniquified)
    target = deps.storage.output_uri(f"{safe}-{uuid.uuid4().hex[:6]}", ext)
    obj = is_object_uri(target)
    limit = settings.max_upload_bytes
    # stream the temp into a HIDDEN subdir of the outputs dir — same filesystem as the target (so the
    # final os.replace stays atomic), but list_outputs (non-recursive, skips non-.lance dirs) won't
    # enumerate it, so a crash-orphaned temp is never re-registered as a phantom dataset on next boot.
    tmp_dir = None if obj else os.path.join(os.path.dirname(path_of(target)) or ".", ".uploads-tmp")
    if tmp_dir:
        os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=ext, dir=tmp_dir)
    try:
        written = 0
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():  # pulled from the socket as it arrives — nothing pre-buffered
                written += len(chunk)
                if written > limit:  # abort mid-stream, before the whole body lands anywhere
                    raise HTTPException(413, f"upload exceeds the {limit}-byte limit (raise DP_MAX_UPLOAD_BYTES)")
                out.write(chunk)
        if written == 0:
            raise HTTPException(400, "empty upload")
        # landing + schema probe + register are blocking DuckDB calls — run them off the event loop
        return await run_in_threadpool(_finalize_upload, deps, tmp, target, name)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)  # local: already moved (no-op); object store / any error: drop the temp


# --------------------------------------------------------------------------- #
# Data preview
# --------------------------------------------------------------------------- #
@router.post("/data/sample", response_model=SampleResult)
def data_sample(req: SampleRequest) -> SampleResult:
    deps = get_deps()
    if req.k is not None and req.k < 0:
        raise HTTPException(400, "k must be >= 0")
    if req.offset < 0:
        raise HTTPException(400, "offset must be >= 0")
    from hub import paths
    try:
        paths.ensure_local_uri_allowed(req.uri)  # multi-user: don't sample an arbitrary local file
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        with source_read_scope(
                deps.storage, [req.uri], owner=f"sample:{uuid.uuid4().hex}"):
            # Exact reference-aware retention keeps a managed file stable for this whole scope. For an
            # unmanaged local path, report a stable expired signal before constructing a lazy relation.
            local = paths.local_path(req.uri)
            if local is not None:
                if not os.path.exists(local) and not glob.glob(local, recursive=True):
                    raise HTTPException(410, "dataset artifact is missing or expired")
            adapter = deps.resolve_adapter(req.uri)
            with db.run_scope():  # own cursor — a big sample doesn't block other users' runs/previews
                # Keep the adapter contract unchanged: request a bounded prefix, then page that lazy
                # relation. Fetch one extra row so `has_more` is exact even when count() is unavailable.
                rel = adapter.scan(req.uri, req.columns, limit=req.offset + req.k + 1)
                cols = relation_columns(rel)          # schema is metadata — no second scan needed
                page = _table_to_rows(rel.limit(req.k + 1, req.offset).to_arrow_table())
                rows = page[:req.k]
                total = adapter.count(req.uri)
            has_more = len(page) > req.k or (total is not None and total > req.offset + len(rows))
            result = SampleResult(columns=cols, rows=rows, row_count=total, has_more=has_more,
                                  truncated=(total is None or total > req.offset + len(rows)))
        with contextlib.suppress(Exception):
            metadb.catalog_bump_usage(req.uri)  # someone looked at this data → popularity signal (best-effort)
        return result
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(410, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Pipeline import (bundle extension point)
# --------------------------------------------------------------------------- #
@router.post("/pipelines/import", response_model=PipelineImport)
def import_pipeline(req: ImportRequest) -> PipelineImport:
    try:
        result = get_deps().importer.import_pipeline(req.config, req.params)
        # auto-lay-out an imported graph the importer didn't position (so nodes don't stack at 0,0); an
        # importer that set its own positions keeps them. Inside the try so a bad graph → 400, not 500.
        if result.graph and result.graph.nodes and all(n.position.x == 0 and n.position.y == 0 for n in result.graph.nodes):
            g.layout(result.graph)
    except ImporterNotConfigured as e:
        raise HTTPException(501, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return result


# --------------------------------------------------------------------------- #
# Processors (library picker + promote)
# --------------------------------------------------------------------------- #
@router.get("/processors", response_model=list[ProcessorDescriptor])
def list_processors() -> list[ProcessorDescriptor]:
    return get_deps().registry.list()


class PromoteRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    id: str
    title: str
    mode: str = "map"
    code: str
    input_columns: list[str] = []
    output_schema: list[ColumnSchema] = []
    blurb: str = ""


@router.post("/processors/promote", response_model=ProcessorDescriptor)
def promote_processor(req: PromoteRequest) -> ProcessorDescriptor:
    p = get_deps().registry.promote(
        id=req.id, title=req.title, mode=req.mode, code=req.code,
        input_columns=req.input_columns, output_schema=req.output_schema, blurb=req.blurb,
    )
    return p.descriptor()
