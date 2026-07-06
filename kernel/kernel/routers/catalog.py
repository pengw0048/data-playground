"""Catalog, data preview, pipelines, processors — the data/discovery routes.

Split out of main.py. All routes are authed: main includes this router with
`dependencies=[Depends(current_user)]`, so the whole surface is gated by default.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from kernel import db, metadb
from kernel.deps import get_deps
from kernel.executors.engine import _table_to_rows
from kernel.plugins.adapters import relation_columns
from kernel.plugins.importer import ImporterNotConfigured
from kernel.models import (
    CatalogTable,
    ColumnSchema,
    ImportRequest,
    KernelInfo,
    LineageResult,
    PipelineImport,
    ProcessorDescriptor,
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
    from kernel import paths
    try:
        paths.ensure_local_uri_allowed(uri)  # multi-user: don't let a user register an arbitrary local file
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        deps.resolve_adapter(uri).schema(uri)  # validate readable
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"cannot read '{uri}': {e}")
    t = deps.catalog.register_output(name=name, uri=uri, version="v1", parents=[])
    # persist so user-added datasets survive a kernel restart (re-registered on startup)
    try:
        ds = metadb.get_setting("datasets", "global", default=[]) or []
        if not any(d.get("uri") == uri for d in ds):
            ds.append({"uri": uri, "name": name})
            metadb.set_setting("datasets", ds, "global")
    except Exception:  # noqa: BLE001
        pass
    return t


# --------------------------------------------------------------------------- #
# Data preview
# --------------------------------------------------------------------------- #
@router.post("/data/sample", response_model=SampleResult)
def data_sample(req: SampleRequest) -> SampleResult:
    deps = get_deps()
    if req.k is not None and req.k < 0:
        raise HTTPException(400, "k must be >= 0")
    from kernel import paths
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

