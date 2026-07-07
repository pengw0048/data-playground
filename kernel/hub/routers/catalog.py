"""Catalog, data preview, pipelines, processors — the data/discovery routes.

Split out of main.py. All routes are authed: main includes this router with
`dependencies=[Depends(current_user)]`, so the whole surface is gated by default.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hub import db, metadb
from hub.deps import get_deps
from hub.executors.engine import _table_to_rows
from hub.plugins.adapters import is_object_uri, path_of, relation_columns
from hub.plugins.importer import ImporterNotConfigured
from hub.settings import settings
from hub.models import (
    CatalogTable,
    ColumnSchema,
    ImportRequest,
    JoinSuggestion,
    KernelInfo,
    LineageResult,
    PipelineImport,
    ProcessorDescriptor,
    Relationship,
    SampleRequest,
    SampleResult,
)

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
    return get_deps().plugins


@router.get("/catalog/tables", response_model=list[CatalogTable])
def list_tables(q: str | None = None) -> list[CatalogTable]:
    return get_deps().catalog.list_tables(q)


@router.get("/catalog/tables/{table_id}", response_model=CatalogTable)
def get_table(table_id: str) -> CatalogTable:
    try:
        return get_deps().catalog.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")


@router.get("/catalog/lineage", response_model=LineageResult)
def lineage(uri: str = Query(...)) -> LineageResult:
    return get_deps().catalog.lineage(uri)


@router.delete("/catalog/tables/{table_id}")
def unregister_table(table_id: str) -> dict:
    """Remove a dataset from the catalog (e.g. a dead entry whose file was deleted)."""
    if not get_deps().catalog.unregister(table_id):
        raise HTTPException(404, f"table '{table_id}' not found")
    return {"ok": True}


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

    def cols(uri: str):
        try:
            return deps.catalog.get_table(uri).columns
        except KeyError:
            return deps.resolve_adapter(uri).schema(uri)  # not registered → probe directly
    try:
        return rel.suggest_joins(cols(req.left_uri), cols(req.right_uri),
                                 rel.measured_unique(req.left_uri, deps.resolve_adapter),
                                 rel.measured_unique(req.right_uri, deps.resolve_adapter))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


class RegisterRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    uri: str
    name: str | None = None


@router.post("/catalog/register", response_model=CatalogTable)
def catalog_register(req: RegisterRequest) -> CatalogTable:
    deps = get_deps()
    has_scheme = bool(re.match(r"^[a-z][a-z0-9+.-]*://", req.uri))
    uri = req.uri if has_scheme else os.path.abspath(os.path.expanduser(req.uri))
    name = req.name or os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
    from hub import paths
    try:
        paths.ensure_local_uri_allowed(uri)  # multi-user: don't let a user register an arbitrary local file
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        deps.resolve_adapter(uri).schema(uri)  # validate readable
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"cannot read '{uri}': {e}")
    # register_output write-throughs per-row to catalog_entries (metadb), which _load_from_db restores
    # on every read + across restart — so no separate 'datasets' settings blob (its read-modify-write
    # dropped a concurrent registration; F45). The per-row store is authoritative + cross-instance.
    return deps.catalog.register_output(name=name, uri=uri, version="v1", parents=[])


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
        return deps.catalog.register_output(name=name, uri=final, version="v1", parents=[])


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
    name = stem or "upload"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "upload"
    # short suffix so two uploads of the same filename don't silently clobber each other (the catalog
    # still shows the clean name; only the stored file path is uniquified)
    target = deps.storage.output_uri(f"{safe}-{uuid.uuid4().hex[:6]}", ext)
    obj = is_object_uri(target)
    limit = settings.max_upload_bytes
    tmp_dir = None if obj else (os.path.dirname(path_of(target)) or ".")
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
    from hub import paths
    try:
        paths.ensure_local_uri_allowed(req.uri)  # multi-user: don't sample an arbitrary local file
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        adapter = deps.resolve_adapter(req.uri)
        with db.run_scope():  # own cursor — a big sample doesn't block other users' runs/previews
            rel = adapter.scan(req.uri, req.columns, limit=req.k)
            cols = relation_columns(rel)          # schema is metadata — no second scan needed
            rows = _table_to_rows(rel.to_arrow_table())
            total = adapter.count(req.uri)
        return SampleResult(columns=cols, rows=rows, row_count=total,
                            truncated=(total is None or total > len(rows)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Pipeline import (bundle extension point)
# --------------------------------------------------------------------------- #
@router.post("/pipelines/import", response_model=PipelineImport)
def import_pipeline(req: ImportRequest) -> PipelineImport:
    try:
        return get_deps().importer.import_pipeline(req.config, req.params)
    except ImporterNotConfigured as e:
        raise HTTPException(501, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


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

