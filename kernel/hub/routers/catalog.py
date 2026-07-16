"""Catalog, data preview, pipelines, processors — the data/discovery routes.

Split out of main.py. All routes are authed: main includes this router with
`dependencies=[Depends(current_user)]`, so the whole surface is gated by default.
"""

from __future__ import annotations

import contextlib
import datetime
import glob
import os
import re
import tempfile
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hub import db, graph as g, metadb
from hub.api_errors import APIError, APIErrorCode
from hub.backends import CatalogLineageFactExporter, DatasetRevisionAdapter
from hub.deps import get_deps
from hub.executors.engine import _table_to_rows
from hub.plugins.adapters import (
    BoundedPreviewUnsupported, RevisionUnavailable, is_object_uri, managed_local_file_revision_adapter,
    path_of, relation_columns,
)
from hub.plugins.importer import ImporterNotConfigured
from hub.sampling import provenance_for_dataset
from hub.settings import settings
from hub.storage import ManagedSourceReadError, source_read_scope
from hub.models import (
    CatalogBrowse,
    CatalogEdit,
    CatalogFolder,
    CatalogMetadata,
    CatalogPage,
    CatalogQuery,
    CatalogTable,
    ColumnSchema,
    DatasetRevision,
    DatasetRevisionPage,
    DatasetRevisionResolution,
    Facets,
    ImportRequest,
    JoinSuggestion,
    KernelInfo,
    LineageFactsPage,
    LineageResult,
    PipelineImport,
    ProcessorDescriptor,
    Relationship,
    SchemaCompatibility,
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

# `preview_scan` currently accepts a source row limit, not an offset. Pagination therefore reads a
# bounded prefix; keep that prefix under a fixed server-owned budget regardless of caller-controlled
# `offset`/`k`. A larger browse belongs in a durable run, not an interactive request.
DATA_SAMPLE_PREVIEW_ROW_BUDGET = 2_000

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
    cols = []
    for field in req.columns:
        # A manually submitted field is a declaration. Inferred preview fields keep
        # their explicit provenance when the UI saves them as a named contract.
        if "provenance" not in field.model_fields_set:
            field = field.model_copy(update={"provenance": "declared"})
        cols.append(field.model_dump(by_alias=True))
    version = metadb.save_schema_contract(req.name.strip(), cols)
    return {"name": req.name.strip(), "version": version, "columns": cols}


@router.get("/schemas/diff", response_model=SchemaCompatibility)
def diff_schemas(name: str = Query(...), a: int = Query(...), b: int = Query(...)) -> SchemaCompatibility:
    """Compatibility result with per-field reasons for two versions of a named contract."""
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


@router.get("/catalog/tables", response_model=CatalogPage)
def list_tables(
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
) -> CatalogPage:
    """A filtered, sorted, paginated catalog page with its window and total in the response body."""
    query = _catalog_query(q, folder, tags, owner, has_columns, sort, order, limit, offset, uris=uris)
    return get_deps().catalog.list_page(query)


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


class FolderCreateRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    path: str


class FolderRenameRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    old_path: str
    new_path: str


class FolderDeleteRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    path: str


def _folder_provider():
    """The active catalog provider IF it supports folder mutation, else None → the caller 501s. Folder
    listing + mutation go through the provider (not metadb directly), so a provider that owns an
    external namespace is never mutated as a silent local-only side effect."""
    cat = get_deps().catalog
    return cat if getattr(cat, "folders_mutable", False) else None


@router.get("/catalog/folders", response_model=list[CatalogFolder])
def list_folders() -> list[CatalogFolder]:
    """Every folder entity — including EMPTY ones. Powers the folder-name autocomplete (unioned with the
    entry-derived folder facets on the client). Empty when the provider owns no local folder store."""
    cat = _folder_provider()
    return [CatalogFolder(path=f["path"]) for f in (cat.list_folders() if cat else [])]


def _require_folder_provider():
    cat = _folder_provider()
    if cat is None:
        raise HTTPException(501, "this catalog provider does not support folder mutation")
    return cat


@router.post("/catalog/folders", response_model=CatalogFolder)
def create_folder(req: FolderCreateRequest) -> CatalogFolder:
    """Create an EMPTY folder (fill it later). 409 if it already exists, 400 if the path is invalid."""
    cat = _require_folder_provider()
    try:
        return CatalogFolder(path=cat.create_folder(req.path))
    except metadb.FolderExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/catalog/folders/rename")
def rename_folder(req: FolderRenameRequest) -> dict:
    """Rename a folder, cascading to every dataset and subfolder under it. Works whether the folder is a
    created entity or exists only because a dataset was registered into it. 400 on unknown/collision."""
    cat = _require_folder_provider()
    try:
        cat.rename_folder(req.old_path, req.new_path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/catalog/folders/delete")
def delete_folder(req: FolderDeleteRequest) -> dict:
    """Delete a folder, moving its datasets + subfolders up to the parent (structure preserved). 400 if unknown."""
    cat = _require_folder_provider()
    try:
        cat.delete_folder(req.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


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
    return get_deps().catalog.search(query.q or "", mode=mode, limit=bounded, query=query)


@router.get("/catalog/tables/{table_id}", response_model=CatalogTable)
def get_table(table_id: str) -> CatalogTable:
    try:
        return get_deps().catalog.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")


def _revision_adapter(uri: str) -> DatasetRevisionAdapter:
    if managed := managed_local_file_revision_adapter(uri):
        return managed
    adapter = get_deps().resolve_adapter(uri)
    if not isinstance(adapter, DatasetRevisionAdapter):
        raise APIError(501, "dataset_revision_history_unavailable",
                       code=APIErrorCode.NOT_IMPLEMENTED, retryable=False)
    return adapter


def _revision_binding_for_table(table_id: str) -> tuple[CatalogTable, dict]:
    table = get_table(table_id)
    binding = metadb.catalog_revision_binding_for_uri(table.uri)
    if binding is None:
        raise APIError(410, "dataset_revision_unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    return table, binding


def _revision(dataset_id: str, raw: dict, adapter: DatasetRevisionAdapter) -> DatasetRevision:
    return DatasetRevision(dataset_id=dataset_id, revision_id=str(raw["revision_id"]),
                           committed_at=raw.get("committed_at"),
                           retention_owner=getattr(adapter, "retention_owner", "provider"))


@router.get("/catalog/tables/{table_id}/revisions", response_model=DatasetRevisionPage)
def list_dataset_revisions(table_id: str, limit: int = Query(20, ge=1, le=100),
                           cursor: str | None = Query(None, max_length=256)) -> DatasetRevisionPage:
    """A bounded newest-first page of provider-native history for one current registration."""
    table, binding = _revision_binding_for_table(table_id)
    try:
        adapter = _revision_adapter(table.uri)
        rows, next_cursor = adapter.revision_history(table.uri, limit=limit, cursor=cursor)
    except RevisionUnavailable:
        raise APIError(410, "dataset_revision_unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    return DatasetRevisionPage(items=[
        _revision(binding["dataset_id"], row, adapter) for row in rows],
                               next_cursor=next_cursor, has_more=next_cursor is not None)


@router.get("/catalog/tables/{table_id}/revisions/resolve", response_model=DatasetRevisionResolution)
def resolve_dataset_revision(table_id: str,
                             as_of: datetime.datetime | None = Query(None, alias="asOf")) -> DatasetRevisionResolution:
    """Resolve latest or an as-of instant to immutable provider evidence without opening head later."""
    table, binding = _revision_binding_for_table(table_id)
    try:
        adapter = _revision_adapter(table.uri)
        raw = adapter.resolve_revision(table.uri, as_of=as_of)
    except RevisionUnavailable:
        raise APIError(410, "dataset_revision_unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    return DatasetRevisionResolution(dataset_id=binding["dataset_id"],
                                     revision_id=str(raw["revision_id"]),
                                     committed_at=raw.get("committed_at"),
                                     retention_owner=getattr(
                                         adapter, "retention_owner", "provider"),
                                     selector="as_of" if as_of is not None else "latest")


@router.get("/catalog/revisions/{dataset_id}/{revision_id}", response_model=DatasetRevisionResolution)
def open_dataset_revision(dataset_id: str, revision_id: str) -> DatasetRevisionResolution:
    """Verify one persisted dataset/revision binding exactly; unavailable never means current head."""
    binding = metadb.catalog_revision_binding(dataset_id)
    if binding is None:
        raise APIError(410, "dataset_revision_unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    try:
        adapter = _revision_adapter(binding["uri"])
        with db.base_guard():
            adapter.open_revision(binding["uri"], revision_id)
    except RevisionUnavailable:
        raise APIError(410, "dataset_revision_unavailable",
                       code=APIErrorCode.RESOURCE_GONE, retryable=False)
    return DatasetRevisionResolution(dataset_id=binding["dataset_id"], revision_id=revision_id,
                                     retention_owner=getattr(
                                         adapter, "retention_owner", "provider"),
                                     selector="exact")


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
    try:
        return cat.set_metadata(
            table.uri,
            folder=(req.folder or "") if "folder" in sent else None,
            tags=req.tags if "tags" in sent else None,
            owner=(req.owner or "") if "owner" in sent else None,
            description=(req.description or "") if "description" in sent else None,
            name=req.name if "name" in sent else None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/catalog/tables/{table_id}/edit", response_model=CatalogTable)
def save_table_edit(table_id: str, req: CatalogEdit) -> CatalogTable:
    """Atomically save a staged built-in table edit, guarded by its opaque CAS revision.

    This route intentionally does not extend the external CatalogProvider SPI. A provider that does
    not ship the same built-in implementation is truthful rather than accepting two separate writes.
    """
    from hub.plugins.catalog import InMemoryCatalog

    cat = get_deps().catalog
    if type(cat) is not InMemoryCatalog:
        raise HTTPException(501, "this catalog provider does not support atomic metadata and key edits")
    try:
        table = cat.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")
    try:
        return cat.save_metadata_edit(
            table.uri, expected_revision=req.expected_revision, folder=req.folder, tags=req.tags,
            owner=req.owner, description=req.description, name=req.name,
            declared_key=req.declared_key)
    except metadb.CatalogMetadataConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/catalog/lineage", response_model=LineageResult)
def lineage(
        uri: str = Query(..., max_length=8192), depth: int = 6,
        max_nodes: int = Query(500, alias="maxNodes"),
) -> LineageResult:
    """The lineage component around `uri`, expanded breadth-first from the store and CAPPED by `depth`
    + `max_nodes` so a large graph can't blow up the payload (`truncated` flags that the cap hit)."""
    try:
        root_uri = metadb.catalog_lineage_uri(uri)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return get_deps().catalog.lineage(root_uri, depth=max(1, min(int(depth), 20)),
                                      max_nodes=max(1, min(int(max_nodes), 5000)))


@router.get("/catalog/lineage/facts", response_model=LineageFactsPage)
def lineage_facts(
        limit: int = Query(100, ge=1, le=500),
        after_id: str = Query("0", alias="afterId", pattern=r"^(0|[1-9][0-9]{0,18})$"),
) -> LineageFactsPage:
    """Export immutable provenance facts with a deletion-safe, monotonic keyset cursor."""
    cursor = int(after_id)
    if cursor >= 2**63:
        raise HTTPException(422, "afterId exceeds the signed BIGINT cursor range")
    catalog = get_deps().catalog
    if not isinstance(catalog, CatalogLineageFactExporter):
        raise HTTPException(501, "catalog provider does not support lineage fact export")
    try:
        page = LineageFactsPage.model_validate(
            catalog.lineage_facts_page(limit=limit, after_id=cursor))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            502, "catalog provider returned an invalid lineage fact page") from exc
    ids = [int(item.id) for item in page.items]
    invalid_window = (
        len(page.items) > limit
        or any(fact_id <= cursor for fact_id in ids)
        or any(left >= right for left, right in zip(ids, ids[1:]))
        or (page.has_more and (
            not ids or page.next_after_id is None
            or int(page.next_after_id) != ids[-1]
        ))
    )
    if invalid_window:
        raise HTTPException(502, "catalog provider returned an invalid lineage fact page")
    return page


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
    if req.offset >= DATA_SAMPLE_PREVIEW_ROW_BUDGET:
        raise HTTPException(
            400,
            f"sample offset is outside the {DATA_SAMPLE_PREVIEW_ROW_BUDGET}-row interactive window; "
            "use a durable run for a larger range",
        )
    if req.k > DATA_SAMPLE_PREVIEW_ROW_BUDGET:
        raise HTTPException(
            400,
            f"sample page size exceeds the {DATA_SAMPLE_PREVIEW_ROW_BUDGET}-row interactive work budget",
        )
    # A final page may straddle the budget boundary. Clamp its source prefix instead of reporting
    # `has_more=true` on the preceding page and then rejecting the user's valid Next click.
    preview_rows = min(
        DATA_SAMPLE_PREVIEW_ROW_BUDGET,
        req.offset + req.k + 1,  # one look-ahead row makes `has_more` exact within the window
    )
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
                preview_scan = getattr(adapter, "preview_scan", None)
                if not callable(preview_scan):
                    return SampleResult(
                        not_previewable=True,
                        reason=(f"source adapter '{getattr(adapter, 'name', type(adapter).__name__)}' "
                                "does not guarantee a bounded preview — needs a full pass"),
                    )
                # Fetch one extra row so `has_more` remains exact without a full count. The adapter's
                # explicit preview capability, not an outer LIMIT, owns the source-level work bound.
                try:
                    rel = preview_scan(
                        req.uri, req.columns, limit=preview_rows,
                    )
                except BoundedPreviewUnsupported as exc:
                    return SampleResult(not_previewable=True, reason=str(exc))
                cols = relation_columns(rel)          # schema is metadata — no second scan needed
                page = _table_to_rows(rel.limit(req.k + 1, req.offset).to_arrow_table())
                rows = page[:req.k]
                metadata_count = getattr(adapter, "metadata_count", None)
                try:
                    total = metadata_count(req.uri) if callable(metadata_count) else None
                except Exception:  # metadata uncertainty stays unknown; never fall back to a full scan
                    total = None
            page_end = req.offset + len(rows)
            # ``preview_scan(limit=...)`` guarantees an upper work bound, not EOF semantics. A
            # third-party adapter may legitimately return fewer rows than requested while more data
            # exists (remote pagination, sparse partitions, transient batch boundaries). Only the
            # adapter's explicit bounded metadata capability can establish an exact total.
            exact_total = total
            budgeted_total = (min(exact_total, DATA_SAMPLE_PREVIEW_ROW_BUDGET)
                              if exact_total is not None else None)
            budget_capped = page_end >= DATA_SAMPLE_PREVIEW_ROW_BUDGET and (
                exact_total is None or exact_total > page_end
            )
            if len(page) > req.k:
                has_more: bool | None = True
            elif budget_capped:
                # Pagination is deliberately closed at this result-window boundary even when the
                # source's exact total is larger or unknown.
                has_more = False
            elif budgeted_total is not None:
                has_more = budgeted_total > page_end
            else:
                # A short bounded adapter batch is not an EOF signal.
                has_more = None
            if budget_capped:
                completeness = "capped"
            elif exact_total is None:
                completeness = "page" if has_more is True or req.offset > 0 else "unknown"
            elif req.offset == 0 and page_end >= exact_total:
                completeness = "complete"
            else:
                completeness = "page"
            result = SampleResult(columns=cols, rows=rows, row_count=exact_total, has_more=has_more,
                                  # Every page after offset zero omits the rows before it, including
                                  # the final page of a dataset with an exact total.
                                  truncated=(req.offset > 0 or exact_total is None
                                             or exact_total > page_end),
                                  completeness=completeness,
                                  row_limit=(DATA_SAMPLE_PREVIEW_ROW_BUDGET
                                             if budget_capped else None),
                                  limit_reason=("interactive-row-budget"
                                                if budget_capped else None),
                                  limit_scope=("result-window" if budget_capped else None),
                                  sample_provenance=provenance_for_dataset(
                                      req.uri, adapter, requested_rows=req.k,
                                      scanned_rows=None, returned_rows=len(rows), total_rows=exact_total,
                                      limitations=[
                                          ("Exact metadata proves this response contains the complete dataset."
                                           if completeness == "complete" else
                                           f"This is a bounded prefix preview (at most {preview_rows} rows read), not representative or random."),
                                      ],
                                  ))
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
        deps = get_deps()
        result = deps.importer.import_pipeline(req.config, req.params)
        if result.graph:
            invalid = g.validation_error(
                result.graph, deps.node_specs, deps.node_builders)
            if invalid:
                raise ValueError(invalid[0])
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
