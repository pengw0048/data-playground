"""Data Playground kernel — FastAPI app (PRD §9).

One kernel per open canvas session. Backend-agnostic core; the default bundle runs fully
offline (DuckDB adapter, in-memory catalog, local runner). All routes under /api, JSON,
camelCase on the wire.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from kernel import compiler
from kernel import graph as graph_mod
from kernel.deps import get_deps
from kernel.executors.preview import preview_node
from kernel.graph import upstream_chain
from kernel.models import (
    CatalogTable,
    ColumnSchema,
    CompilePlan,
    CompileRequest,
    EstimateRequest,
    ImportRequest,
    KernelInfo,
    LineageResult,
    PipelineImport,
    PreviewRequest,
    ProcessorDescriptor,
    RunEstimate,
    RunRequest,
    RunStatus,
    SampleRequest,
    SampleResult,
)
from kernel.plugins.importer import ImporterNotConfigured
from kernel.settings import settings

api = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# Kernel + catalog
# --------------------------------------------------------------------------- #
@api.get("/kernel", response_model=KernelInfo)
def kernel_info() -> KernelInfo:
    return get_deps().info()


@api.get("/nodes")
def list_nodes() -> list[dict]:
    """Schema of every registered node (built-in + plugin) — powers generic rendering (§4.2)."""
    return [s.model_dump(by_alias=True) for s in get_deps().node_specs.values()]


@api.get("/plugins")
def list_plugins() -> list[dict]:
    return get_deps().plugins


@api.get("/catalog/tables", response_model=list[CatalogTable])
def list_tables(q: str | None = None) -> list[CatalogTable]:
    return get_deps().catalog.list_tables(q)


@api.get("/catalog/tables/{table_id}", response_model=CatalogTable)
def get_table(table_id: str) -> CatalogTable:
    try:
        return get_deps().catalog.get_table(table_id)
    except KeyError:
        raise HTTPException(404, f"table '{table_id}' not found")


@api.get("/catalog/lineage", response_model=LineageResult)
def lineage(uri: str = Query(...)) -> LineageResult:
    return get_deps().catalog.lineage(uri)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    uri: str
    name: str | None = None


@api.post("/catalog/register", response_model=CatalogTable)
def catalog_register(req: RegisterRequest) -> CatalogTable:
    deps = get_deps()
    import os
    uri = os.path.abspath(os.path.expanduser(req.uri)) if not req.uri.startswith(("http", "s3", "mem")) else req.uri
    name = req.name or os.path.splitext(os.path.basename(uri.rstrip("/")))[0]
    try:
        deps.resolve_adapter(uri).schema(uri)  # validate readable
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"cannot read '{uri}': {e}")
    return deps.catalog.register_output(name=name, uri=uri, version="v1", parents=[])


# --------------------------------------------------------------------------- #
# Data preview
# --------------------------------------------------------------------------- #
@api.post("/data/sample", response_model=SampleResult)
def data_sample(req: SampleRequest) -> SampleResult:
    deps = get_deps()
    if req.k is not None and req.k < 0:
        raise HTTPException(400, "k must be >= 0")
    try:
        from kernel.executors.engine import _table_to_rows
        from kernel.plugins.adapters import relation_columns
        adapter = deps.resolve_adapter(req.uri)
        rel = adapter.scan(req.uri, req.columns, limit=req.k)
        total = adapter.count(req.uri)
        rows = _table_to_rows(rel.to_arrow_table())
        return SampleResult(columns=relation_columns(adapter.scan(req.uri, req.columns, limit=0)),
                            rows=rows, row_count=total, truncated=(total is None or total > len(rows)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Pipeline import (bundle extension point)
# --------------------------------------------------------------------------- #
@api.post("/pipelines/import", response_model=PipelineImport)
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
@api.get("/processors", response_model=list[ProcessorDescriptor])
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


@api.post("/processors/promote", response_model=ProcessorDescriptor)
def promote_processor(req: PromoteRequest) -> ProcessorDescriptor:
    p = get_deps().registry.promote(
        id=req.id, title=req.title, mode=req.mode, code=req.code,
        input_columns=req.input_columns, output_schema=req.output_schema, blurb=req.blurb,
    )
    return p.descriptor()


# --------------------------------------------------------------------------- #
# Compile / preview / estimate / run
# --------------------------------------------------------------------------- #
def _reject_invalid(graph, deps) -> None:
    """400 on a graph the type system forbids (server-side; the frontend blocks these too)."""
    errs = graph_mod.type_errors(graph, deps.node_specs)
    if errs:
        raise HTTPException(400, "incompatible connection: " + "; ".join(errs[:5]))


@api.post("/graph/compile", response_model=CompilePlan)
def compile_graph(req: CompileRequest) -> CompilePlan:
    deps = get_deps()
    errs = graph_mod.type_errors(req.graph, deps.node_specs)
    if errs:
        return CompilePlan(target_node_id=req.target_node_id, steps=[], acyclic=True,
                           error="incompatible connection: " + "; ".join(errs[:5]))
    return compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)


@api.post("/run/preview", response_model=SampleResult)
def run_preview(req: PreviewRequest) -> SampleResult:
    deps = get_deps()
    return preview_node(req.graph, req.node_id, req.k or settings.preview_k,
                        deps.resolve_adapter, deps.registry, deps.node_lowerings, deps.node_specs)


def _row_estimate(req_graph, target_node_id, deps) -> int:
    chain = upstream_chain(req_graph, target_node_id) if target_node_id else req_graph.nodes
    for n in chain:
        if n.type == "source":
            cfg = n.data.get("config", {}) if isinstance(n.data, dict) else {}
            uri = cfg.get("uri") or cfg.get("table")
            if uri:
                try:
                    c = deps.resolve_adapter(uri).count(uri)
                    if c:
                        return c
                except Exception:  # noqa: BLE001
                    pass
    return 1000


@api.post("/run/estimate", response_model=RunEstimate)
def run_estimate(req: EstimateRequest) -> RunEstimate:
    deps = get_deps()
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    return deps.pick_runner(plan).estimate(plan, rows)


@api.post("/run", response_model=RunStatus)
def run(req: RunRequest) -> RunStatus:
    deps = get_deps()
    _reject_invalid(req.graph, deps)
    plan = compiler.compile_plan(req.graph, req.target_node_id, deps.registry, deps.node_specs)
    if not plan.acyclic:
        raise HTTPException(400, plan.error or "graph has a cycle")
    runner = deps.pick_runner(plan)
    rows = _row_estimate(req.graph, req.target_node_id, deps)
    est = runner.estimate(plan, rows)
    if est.needs_confirm and not req.confirmed:
        raise HTTPException(409, "run needs confirmation (cost/placement over threshold)")
    return runner.run(plan, req.graph, req.target_node_id, est.placement)


@api.get("/run/{run_id}", response_model=RunStatus)
def run_status(run_id: str) -> RunStatus:
    try:
        return get_deps().runner.status(run_id)
    except KeyError:
        raise HTTPException(404, f"run '{run_id}' not found")


@api.post("/run/{run_id}/cancel", response_model=RunStatus)
def run_cancel(run_id: str) -> RunStatus:
    try:
        return get_deps().runner.cancel(run_id)
    except KeyError:
        raise HTTPException(404, f"run '{run_id}' not found")


# --------------------------------------------------------------------------- #
# Canvas persistence (portable JSON document, NFR-7)
# --------------------------------------------------------------------------- #
def _canvas_dir() -> str:
    d = os.path.join(settings.data_dir, "canvases")
    os.makedirs(d, exist_ok=True)
    return d


@api.get("/canvas")
def list_canvases() -> list[dict]:
    out = []
    for fn in sorted(os.listdir(_canvas_dir())):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(_canvas_dir(), fn)) as f:
                    doc = json.load(f)
                out.append({"id": doc.get("id", fn[:-5]), "name": doc.get("name", fn[:-5]),
                            "version": doc.get("version", 1)})
            except Exception:  # noqa: BLE001
                continue
    return out


@api.get("/canvas/{canvas_id}")
def get_canvas(canvas_id: str) -> dict:
    path = os.path.join(_canvas_dir(), f"{canvas_id}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"canvas '{canvas_id}' not found")
    with open(path) as f:
        return json.load(f)


@api.put("/canvas/{canvas_id}")
def put_canvas(canvas_id: str, doc: dict) -> dict:
    path = os.path.join(_canvas_dir(), f"{canvas_id}.json")
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    return {"ok": True, "id": canvas_id}


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="Data Playground kernel", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.include_router(api)


@app.websocket("/ws/run/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    deps = get_deps()
    try:
        while True:
            try:
                st = deps.runner.status(run_id)
            except KeyError:
                await ws.send_json({"error": "run not found"})
                break
            await ws.send_json(st.model_dump(by_alias=True))
            if st.status in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# Serve the built SPA (P6, single process). Prefer the bundled copy shipped in the wheel
# (kernel/_web), fall back to the dev build (web/dist).
_BUNDLED = os.path.join(os.path.dirname(__file__), "_web")
_DEV = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web", "dist"))
_DIST = _BUNDLED if os.path.isdir(_BUNDLED) else _DEV
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
