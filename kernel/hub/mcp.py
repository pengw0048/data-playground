"""MCP server — drive the Data Playground from your own Claude Code (or any MCP client).

The in-process agent (`hub.agent`) is one way to build a canvas; this is the other. Instead of the
kernel calling an LLM, the LLM calls the kernel: a user runs `dataplay mcp`, adds it to their Claude
Code, and now their model can explore the catalog, spin up a canvas, wire typed nodes, WRITE the
transform Python for them, preview each step against real rows, and run the pipeline — all as MCP
tool calls, with the built canvas showing up in the browser (both processes share the workspace DB).

WHY HAND-ROLLED (no `mcp`/`fastmcp` dependency). The protocol surface a tools+resources server
needs is small and stable — JSON-RPC 2.0 over stdio: `initialize`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`, `ping`. Rolling it keeps `dataplay mcp` a ZERO-install command
(stdlib only — matches the project's offline-first ethos) and makes the whole thing unit-testable
without a client library: `MCPServer.handle(dict) -> dict|None` is a pure function over messages.

TWO LAYERS:
  * `Playground` — the tool implementations. Thin adapters over the SAME building blocks the HTTP
    API and the agent use (the catalog, `graph_ops`, `preview_node`, the local runner) and over the
    SAME authenticated canvas CRUD as `PUT/GET /api/canvas` (reused directly), so behavior can't
    drift from the web app. A tool raises `ToolError` for an expected, user-facing failure.
  * `MCPServer` — the JSON-RPC dispatch + a tool registry (name → description + JSON Schema +
    handler). Tool-execution failures come back as an MCP `isError` result (the model can read the
    message and retry); only malformed protocol calls become JSON-RPC errors.

A canvas an MCP client builds is persisted like any other, so it appears in the browser's file list;
reload an already-open canvas to pick up changes (live collab is a per-web-process in-memory room,
which an out-of-process MCP client isn't part of). Runs go through the LOCAL out-of-core runner
in-process (deterministic, no kernel spawn); their outputs + history land in the shared stores, so
the UI sees them too.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable

from hub import graph_ops
from hub.models import CatalogQuery

SERVER_NAME = "data-playground"
SERVER_VERSION = "0.1.0"
# Protocol versions we understand. We answer `initialize` with the client's version when we know it,
# else our latest — the spec's negotiation (the client may then disconnect if it can't live with ours).
_SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
_LATEST_PROTOCOL = _SUPPORTED_PROTOCOLS[-1]

_RUN_POLL_TIMEOUT_S = 120.0  # a `run_canvas` tool call polls the in-process run to completion up to here

_INSTRUCTIONS = (
    "Build and run data pipelines on the Data Playground canvas. A pipeline is a graph of typed "
    "nodes: a `source` reads a dataset, then relational nodes (filter/select/join/aggregate/sql/…) "
    "or a `transform` (arbitrary Python over Arrow batches) shape it, and a `write` materializes it. "
    "Typical flow: list_datasets → create_canvas → add a `source` (its `uri` is a dataset's uri or "
    "catalog name) → add + connect nodes → preview_node to check real rows at each step → "
    "validate_canvas → give the user the canvas url (or run_canvas). Prefer relational nodes over "
    "Python when they suffice (they push down and run out-of-core); reach for set_transform when the "
    "logic needs code — then preview_node to confirm it works before moving on. Call join_hints "
    "before a join (don't guess the key or miss a row-multiplying fan-out)."
)


class ToolError(Exception):
    """An expected, user-facing tool failure (bad canvas id, unknown node, unreadable dataset). The
    dispatcher turns it into an MCP `isError` tool result so the model sees the message and can adapt,
    rather than a hard protocol error."""


class JsonRpcError(Exception):
    """A protocol-level fault (unknown method / bad params) → a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _jsonable(obj: Any) -> Any:
    """Coerce a result to plain JSON (stringifying Decimals/datetimes from real data rows) so both
    the text content and the structuredContent are always serializable."""
    return json.loads(json.dumps(obj, default=str))


# --------------------------------------------------------------------------- #
# The tools — adapters over deps + the shared canvas CRUD.
# --------------------------------------------------------------------------- #
class Playground:
    """Every MCP tool, as a plain method taking one `args` dict and returning a JSON-able dict.

    Bound to a resolved user + the kernel `deps` singleton, so canvas access is authorized exactly
    like the HTTP API (open mode → the local user owns what it creates; auth mode → pass --user)."""

    def __init__(self, deps, user_id: str, base_url: str):
        self.deps = deps
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        # canvas ids this session mutated — the in-process HTTP transport drains this after each call to
        # nudge any open browser tab to pick up the edit live (empty/ignored on the stdio transport).
        self.changed_canvases: set[str] = set()

    # -- helpers ----------------------------------------------------------- #
    def _canvas_url(self, canvas_id: str) -> str:
        return f"{self.base_url}/#/canvas/{canvas_id}"

    @staticmethod
    def _req(args: dict, key: str) -> Any:
        v = args.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ToolError(f"missing required argument '{key}'")
        return v

    @staticmethod
    def _limit(args: dict, default: int) -> int:
        """A row-limit argument, honoring an explicit 0 (falsy-zero-safe) and never negative."""
        v = args.get("limit")
        return default if v is None else max(0, int(v))

    def _get_doc(self, canvas_id: str) -> dict:
        """Load a canvas doc through the SAME authorized read the HTTP API uses (404 → ToolError),
        guaranteeing the nodes/edges lists exist so the graph ops can append to them."""
        from fastapi import HTTPException

        from hub.routers import workspace as ws
        try:
            doc = ws.get_canvas(canvas_id, uid=self.user_id)
        except HTTPException as e:
            raise ToolError(f"canvas '{canvas_id}': {e.detail}")
        doc.setdefault("nodes", [])
        doc.setdefault("edges", [])
        return doc

    def _put_doc(self, canvas_id: str, doc: dict) -> None:
        """Persist a mutated doc through the SAME authorized write the HTTP API uses (403 → ToolError),
        bumping the version so the snapshot history records the edit."""
        from fastapi import HTTPException

        from hub.routers import workspace as ws
        doc["version"] = (doc.get("version") or 1) + 1
        try:
            ws.put_canvas(canvas_id, doc, uid=self.user_id)
        except HTTPException as e:
            raise ToolError(f"canvas '{canvas_id}': {e.detail}")
        self.changed_canvases.add(canvas_id)  # so the HTTP transport can nudge an open browser tab

    def _mutate(self, canvas_id: str, op: Callable[[dict], dict]) -> dict:
        """Load → apply one graph op → persist. A structural change (a node/edge added or removed)
        re-tidies positions; a pure config edit changes no structure, so positions are left exactly
        as they were (a hand-moved node isn't snapped back)."""
        doc = self._get_doc(canvas_id)
        before = {n["id"] for n in doc["nodes"]}
        sig = (len(doc["nodes"]), len(doc["edges"]))
        result = op(doc)
        if (len(doc["nodes"]), len(doc["edges"])) != sig:
            self._relayout(doc, before)
        self._put_doc(canvas_id, doc)
        return result

    @staticmethod
    def _relayout(doc: dict, before: set[str]) -> None:
        """Re-tidy after a structural change by positioning ONLY the newly-added nodes (placed below
        any pre-existing content), leaving every existing node exactly where it was. A full topological
        relayout would flow a fresh chain more prettily, but it rewrites ABSOLUTE positions for every
        node — clobbering a layout a human arranged in the browser (the whole point of MCP is that the
        canvas shows up there) and flinging a `section`'s parent-RELATIVE children out of their frame.
        Preserving positions matches the in-process agent, which lays out only its new nodes."""
        graph_ops.layout_new(doc, before)

    def _resolve_uri(self, ref: str) -> str:
        return self.deps.catalog.resolve_ref(ref)

    def _preview_doc(self, doc: dict, node_id: str, limit: int) -> dict:
        """Preview a node over a bounded sample of the CURRENT doc, in-process. Resolves source refs on
        a throwaway model copy so a `source` may name a catalog table; never mutates the stored doc."""
        from hub import graph as gmod
        from hub.executors.preview import preview_node
        from hub.models import Graph
        d = self.deps
        graph = Graph.model_validate(doc)
        gmod.resolve_source_refs(graph, d.catalog.resolve_ref)
        res = preview_node(
            graph, node_id, limit, d.resolve_adapter, d.registry, d.node_builders, d.node_specs,
            storage=d.storage)
        if res.not_previewable:
            return {"notPreviewable": True, "reason": res.reason}
        if res.error:
            return {"error": True, "reason": res.reason}
        return {"columns": [{"name": c.name, "type": c.type} for c in res.columns],
                "rows": res.rows, "rowCount": res.row_count, "hasMore": res.has_more}

    # -- catalog / discovery ---------------------------------------------- #
    def list_datasets(self, args: dict) -> dict:
        return {"datasets": graph_ops.catalog_tables(self.deps)}

    def sample_dataset(self, args: dict) -> dict:
        from fastapi import HTTPException

        from hub.models import SampleRequest
        from hub.routers.catalog import data_sample
        uri = self._resolve_uri(self._req(args, "dataset"))
        limit = self._limit(args, 20)
        try:
            res = data_sample(SampleRequest(uri=uri, k=limit, columns=args.get("columns")))
        except HTTPException as e:
            raise ToolError(str(e.detail))
        return {"uri": uri, "columns": [{"name": c.name, "type": c.type} for c in res.columns],
                "rows": res.rows, "rowCount": res.row_count, "truncated": res.truncated}

    def join_hints(self, args: dict) -> dict:
        try:
            return graph_ops.join_hints(self.deps, self._req(args, "left"), self._req(args, "right"))
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — an unreadable dataset etc. is a user-facing tool error
            raise ToolError(f"{type(e).__name__}: {e}")

    def list_node_kinds(self, args: dict) -> dict:
        return {"kinds": graph_ops.node_kinds(self.deps)}

    # -- canvases ---------------------------------------------------------- #
    def list_canvases(self, args: dict) -> dict:
        from hub.routers import workspace as ws
        rows = ws.list_canvases(uid=self.user_id)
        for r in rows:
            r["url"] = self._canvas_url(r["id"])
        return {"canvases": rows}

    def create_canvas(self, args: dict) -> dict:
        import uuid

        from hub.routers import workspace as ws
        name = (args.get("name") or "untitled").strip() or "untitled"
        cid = "canvas_" + uuid.uuid4().hex[:12]
        created = ws.create_canvas(
            {"id": cid, "name": name, "version": 1, "nodes": [], "edges": []},
            uid=self.user_id,
        )
        if not created["created"]:
            raise ToolError("generated canvas ID already exists; retry create_canvas")
        return {"canvasId": cid, "name": name, "url": self._canvas_url(cid)}

    def get_canvas(self, args: dict) -> dict:
        canvas_id = self._req(args, "canvasId")
        doc = self._get_doc(canvas_id)
        nodes = [{"id": n["id"], "type": n.get("type"),
                  "title": (n.get("data") or {}).get("title"),
                  "config": (n.get("data") or {}).get("config", {})} for n in doc["nodes"]]
        edges = [{"id": e.get("id"), "source": e.get("source"), "target": e.get("target"),
                  "targetHandle": e.get("targetHandle"), "wire": (e.get("data") or {}).get("wire")}
                 for e in doc["edges"]]
        return {"canvasId": canvas_id, "name": doc.get("name"), "url": self._canvas_url(canvas_id),
                "nodes": nodes, "edges": edges}

    # -- graph building ---------------------------------------------------- #
    def add_node(self, args: dict) -> dict:
        canvas_id, kind = self._req(args, "canvasId"), self._req(args, "kind")
        title, config = args.get("title"), args.get("config")

        def op(doc):
            try:
                r = graph_ops.add_node(doc, self.deps.node_specs, graph_ops.fresh_id(doc, kind),
                                       kind, title, config)
            except graph_ops.GraphOpError as e:
                raise ToolError(f"{e} — call list_node_kinds for the available kinds")
            return {"nodeId": r["node_id"], "inputs": r["inputs"], "outputs": r["outputs"]}
        return self._mutate(canvas_id, op)

    def connect(self, args: dict) -> dict:
        canvas_id = self._req(args, "canvasId")
        source_id, target_id = self._req(args, "sourceId"), self._req(args, "targetId")
        target_handle = args.get("targetHandle")

        def op(doc):
            try:
                r = graph_ops.connect(doc, self.deps.node_specs, graph_ops.fresh_id(doc, "e"),
                                      source_id, target_id, target_handle)
            except graph_ops.GraphOpError as e:
                raise ToolError(str(e))
            return {"ok": True, "edgeId": r["edge_id"], "wire": r["wire"]}
        return self._mutate(canvas_id, op)

    def set_node_config(self, args: dict) -> dict:
        canvas_id, node_id = self._req(args, "canvasId"), self._req(args, "nodeId")
        config = self._req(args, "config")

        def op(doc):
            try:
                return graph_ops.set_config(doc, node_id, config)
            except graph_ops.GraphOpError as e:
                raise ToolError(str(e))
        return self._mutate(canvas_id, op)

    def remove_node(self, args: dict) -> dict:
        canvas_id, node_id = self._req(args, "canvasId"), self._req(args, "nodeId")

        def op(doc):
            try:
                r = graph_ops.remove_node(doc, node_id)
            except graph_ops.GraphOpError as e:
                raise ToolError(str(e))
            return {"ok": True, "removedEdges": r["removed_edges"]}
        return self._mutate(canvas_id, op)

    def set_transform(self, args: dict) -> dict:
        """Author a `transform` node's Python and immediately preview it — the write-code-then-verify
        loop. Creates a new transform (optionally wired to `upstreamNodeId`) or updates `nodeId`'s
        code. `code` is a Python cell: for mode 'map' define `def fn(row): ...` returning the row."""
        canvas_id, code = self._req(args, "canvasId"), self._req(args, "code")
        mode = args.get("mode") or "map"
        node_id, upstream = args.get("nodeId"), args.get("upstreamNodeId")
        config = {"source": "adhoc", "scope": "dataset", "mode": mode, "code": code}
        if mode == "map_batches" and args.get("batchFormat"):
            config["batchFormat"] = args["batchFormat"]

        def op(doc):
            nonlocal node_id
            if node_id:
                try:
                    graph_ops.set_config(doc, node_id, config)
                except graph_ops.GraphOpError as e:
                    raise ToolError(str(e))
                return {"nodeId": node_id, "created": False}
            node_id = graph_ops.fresh_id(doc, "transform")
            graph_ops.add_node(doc, self.deps.node_specs, node_id, "transform", args.get("title"), config)
            if upstream:
                try:
                    graph_ops.connect(doc, self.deps.node_specs, graph_ops.fresh_id(doc, "e"), upstream, node_id)
                except graph_ops.GraphOpError as e:
                    raise ToolError(str(e))
            return {"nodeId": node_id, "created": True}

        result = self._mutate(canvas_id, op)
        # preview the freshly-saved doc so the client sees whether its code actually runs + its columns
        doc = self._get_doc(canvas_id)
        result["preview"] = self._preview_doc(doc, result["nodeId"], self._limit(args, 8))
        return result

    # -- preview / validate / run ----------------------------------------- #
    def preview_node(self, args: dict) -> dict:
        canvas_id, node_id = self._req(args, "canvasId"), self._req(args, "nodeId")
        doc = self._get_doc(canvas_id)
        if not graph_ops.find_node(doc, node_id):
            raise ToolError(f"node '{node_id}' not found on canvas '{canvas_id}'")
        return self._preview_doc(doc, node_id, self._limit(args, 10))

    def validate_canvas(self, args: dict) -> dict:
        canvas_id = self._req(args, "canvasId")
        doc = self._get_doc(canvas_id)
        try:
            return graph_ops.validate_graph(self.deps, doc)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"{type(e).__name__}: {e}")

    def run_canvas(self, args: dict) -> dict:
        """Execute the pipeline up to a sink node and wait for it to finish. Runs through the SAME
        path as the web `POST /run` (`runs.start_run`): the same confirm gate on real size, the same
        cost-based placement / capability routing, the same run ownership — so an agent-launched run
        behaves identically to a browser-launched one (and, when served in-process, is visible to the
        UI). A large/unknown run returns needsConfirm:true unless confirm:true; a run still going after
        the poll timeout returns timedOut:true with its runId — poll run_status / cancel_run."""
        from fastapi import HTTPException as HTTPExc

        from hub.models import Graph
        from hub.routers.runs import RunNeedsConfirm, start_run
        canvas_id = self._req(args, "canvasId")
        doc = self._get_doc(canvas_id)
        graph = Graph.model_validate(doc)
        target = args.get("nodeId") or self._sole_sink(doc)
        if not graph_ops.find_node(doc, target):
            raise ToolError(f"node '{target}' not found on canvas '{canvas_id}'")
        try:
            status, _owner = start_run(self.deps, graph, target, self.user_id, bool(args.get("confirm")))
        except RunNeedsConfirm as e:
            est = e.estimate
            return {"needsConfirm": True, "targetNodeId": target, "estRows": est.rows,
                    "reason": est.breakdown or "large or unknown size — a full pass; pass confirm:true to run"}
        except HTTPExc as e:  # invalid/cyclic graph from start_run's server-side checks
            raise ToolError(str(e.detail))
        status = self._await_run(status.run_id)
        return self._run_envelope(status, status.target_node_id or target)

    def run_status(self, args: dict) -> dict:
        """Poll a run started by run_canvas (by its runId) — the way to follow a run that returned
        timedOut:true to completion. Resolved like the web GET /run/{id}: an unknown/evicted id comes
        back as a terminal 'failed' status (with a reason), not a hard error."""
        from hub.routers.runs import _run_read_access, _status_or_lost
        run_id = self._req(args, "runId")
        if not _run_read_access(run_id, self.user_id):  # creator or any collaborator may observe
            raise ToolError(f"unknown runId '{run_id}'")
        st = _status_or_lost(run_id)
        return self._run_envelope(st, st.target_node_id)

    def cancel_run(self, args: dict) -> dict:
        """Cancel an in-flight run (by its runId), routed like the web POST /run/{id}/cancel. A finished
        run is returned unchanged; a truly unknown id is a tool error."""
        from fastapi import HTTPException as HTTPExc

        from hub import metadb
        from hub.models import RunStatus
        from hub.routers.runs import (
            _pruned_terminal_status,
            _require_run_mutate_access,
            _runner_for,
        )
        run_id = self._req(args, "runId")
        try:
            _require_run_mutate_access(run_id, self.user_id)
        except HTTPExc as e:
            raise ToolError(str(e.detail))
        owner = _runner_for(run_id, fallback=False)
        if owner is not None:
            return self._run_envelope(owner.cancel(run_id), None)
        # A durable plugin may be absent after restart. Mirror the HTTP cancel route: persist the
        # authorized intent before falling back to a kernel backend, so recovery can stop the remote
        # job instead of silently losing the request.
        binding = metadb.backend_job(run_id)
        if binding is not None:
            persisted = metadb.get_run_state(run_id)
            if persisted is not None and persisted.get("status") in ("queued", "running"):
                metadb.request_backend_cancel(run_id)
                # Terminal publication racing the request remains authoritative over cancellation.
                current = metadb.get_run_state(run_id)
                if current is not None:
                    return self._run_envelope(RunStatus(**current), None)
                terminal = _pruned_terminal_status(run_id)
                if terminal is not None:
                    return self._run_envelope(terminal, None)
                return self._run_envelope(RunStatus(**persisted), None)
        # A finished run needs no cancel: return its full persisted detail, or the compact fence only
        # when that detail was genuinely pruned — never let the fence shadow a live terminal RunState.
        persisted = metadb.get_run_state(run_id)
        if persisted is not None and persisted.get("status") in ("done", "failed", "cancelled"):
            return self._run_envelope(RunStatus(**persisted), None)
        if persisted is None:
            terminal = _pruned_terminal_status(run_id)
            if terminal is not None:
                return self._run_envelope(terminal, None)
        kb = self.deps.kernel_backend()
        if kb is not None:
            return self._run_envelope(kb.cancel(run_id), None)
        if persisted is not None:
            return self._run_envelope(RunStatus(**persisted), None)
        raise ToolError(f"unknown runId '{run_id}' (not started this session, or evicted)")

    def sample_result(self, args: dict) -> dict:
        """Sample the OUTPUT dataset a run materialized (by its runId) — closes the author→run→inspect
        loop so an agent can read the rows it produced without hand-reconstructing the output uri."""
        from hub.routers.runs import _run_read_access, _status_or_lost
        run_id = self._req(args, "runId")
        if not _run_read_access(run_id, self.user_id):  # don't read an unrelated user's output rows
            raise ToolError(f"unknown runId '{run_id}'")
        st = _status_or_lost(run_id)
        if not st.output_uri:
            raise ToolError(f"run '{st.run_id}' ({st.status}) produced no materialized output to sample")
        return self.sample_dataset({"dataset": st.output_uri, "limit": args.get("limit"),
                                    "columns": args.get("columns")})

    @staticmethod
    def _run_envelope(status, target: str | None) -> dict:
        """The wire shape for a run result. `timedOut` is set when the poll gave up before the run
        reached a terminal state — the run is still executing; follow it with run_status(runId)."""
        terminal = status.status in ("done", "failed", "cancelled")
        env = {"runId": status.run_id, "status": status.status,
               "targetNodeId": target if target is not None else status.target_node_id,
               "rows": status.total_rows, "ms": status.ms, "outputTable": status.output_table,
               "outputUri": status.output_uri, "error": status.error}
        if not terminal:
            env["timedOut"] = True
            env["hint"] = "run still in progress — poll run_status with this runId, or cancel_run to stop it"
        return env

    @staticmethod
    def _sole_sink(doc: dict) -> str:
        sources = {e.get("source") for e in doc["edges"]}
        sinks = [n["id"] for n in doc["nodes"] if n["id"] not in sources]
        if len(sinks) == 1:
            return sinks[0]
        raise ToolError("specify nodeId — the canvas has "
                        + (f"{len(sinks)} sink nodes: {', '.join(sinks)}" if sinks else "no runnable sink"))

    @staticmethod
    def _await_run(run_id: str):
        """Poll the run (resolved to its owning runner via _status_or_lost, DB-backed so it survives an
        eviction) to a terminal state, bounded by the poll timeout so a very long run doesn't block the
        caller forever (it keeps running; run_status resumes following it)."""
        from hub.routers.runs import _status_or_lost
        deadline = time.monotonic() + _RUN_POLL_TIMEOUT_S
        while True:
            st = _status_or_lost(run_id)
            if st.status in ("done", "failed", "cancelled") or time.monotonic() > deadline:
                return st
            time.sleep(0.1)

    # -- resources (read-only context the client can pull) ---------------- #
    def list_resources(self) -> list[dict]:
        res: list[dict] = []
        for t in self.deps.catalog.list_page(CatalogQuery(limit=5000)).items:
            res.append({"uri": f"dataplay://dataset/{t.id}", "name": t.name,
                        "description": f"dataset ({t.row_count if t.row_count is not None else '?'} rows) — {t.uri}",
                        "mimeType": "application/json"})
        from hub.routers import workspace as ws
        for c in ws.list_canvases(uid=self.user_id):
            res.append({"uri": f"dataplay://canvas/{c['id']}", "name": c.get("name") or c["id"],
                        "description": "a canvas / pipeline", "mimeType": "application/json"})
        return res

    def read_resource(self, uri: str) -> dict:
        scheme = "dataplay://"
        if not uri.startswith(scheme):
            raise ToolError(f"unknown resource uri '{uri}'")
        kind, _, ident = uri[len(scheme):].partition("/")
        if kind == "dataset":
            from hub.routers.catalog import get_table
            from fastapi import HTTPException
            try:
                t = get_table(ident)
            except HTTPException as e:
                raise ToolError(str(e.detail))
            payload = {"name": t.name, "uri": t.uri, "rowCount": t.row_count,
                       "columns": [{"name": c.name, "type": c.type} for c in t.columns],
                       "keys": [k.columns for k in t.keys]}
        elif kind == "canvas":
            payload = self.get_canvas({"canvasId": ident})
        else:
            raise ToolError(f"unknown resource kind '{kind}'")
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(_jsonable(payload), indent=2)}


# --------------------------------------------------------------------------- #
# Tool registry — name → (description, JSON Schema, handler). One place so tools/list and tools/call
# stay in lockstep. Schemas are hand-written (small, explicit) rather than reflected off signatures.
# --------------------------------------------------------------------------- #
def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_OBJ = {"type": "object"}


def _tool_specs(pg: Playground) -> list[dict]:
    canvas = {"canvasId": {**_STR, "description": "the canvas id (from create_canvas / list_canvases)"}}
    return [
        {"name": "list_datasets", "handler": pg.list_datasets,
         "description": "List the catalog's datasets: name, uri, columns (name+type), row count, and "
                        "primary-key candidate column(s) — everything to pick a source and join keys.",
         "inputSchema": _schema({})},
        {"name": "sample_dataset", "handler": pg.sample_dataset,
         "description": "Return a small sample of real rows from a dataset (by catalog name/id or uri) "
                        "so you can see its actual shape before building on it.",
         "inputSchema": _schema({"dataset": {**_STR, "description": "catalog name/id or a dataset uri"},
                                 "limit": {**_INT, "description": "max rows (default 20)"},
                                 "columns": {"type": "array", "items": _STR}}, ["dataset"])},
        {"name": "join_hints", "handler": pg.join_hints,
         "description": "How two datasets can join: ranked key-column pairs with cardinality MEASURED on "
                        "the data (1:1/1:N/N:1/N:M) + any declared relationship. Call before a join.",
         "inputSchema": _schema({"left": {**_STR, "description": "left dataset (name/id/uri)"},
                                 "right": {**_STR, "description": "right dataset (name/id/uri)"}},
                                ["left", "right"])},
        {"name": "list_node_kinds", "handler": pg.list_node_kinds,
         "description": "List every node kind (built-in + plugin) with its params and input/output "
                        "ports — the menu you build from and the exact param names to configure.",
         "inputSchema": _schema({})},
        {"name": "list_canvases", "handler": pg.list_canvases,
         "description": "List canvases (pipelines) you can access, each with a browser url.",
         "inputSchema": _schema({})},
        {"name": "create_canvas", "handler": pg.create_canvas,
         "description": "Create a new, empty canvas and return its id + browser url. Start every "
                        "pipeline here, then add a `source` node.",
         "inputSchema": _schema({"name": {**_STR, "description": "canvas name (default 'untitled')"}})},
        {"name": "get_canvas", "handler": pg.get_canvas,
         "description": "Read a canvas: its nodes (id/type/title/config), edges, and browser url.",
         "inputSchema": _schema(dict(canvas), ["canvasId"])},
        {"name": "add_node", "handler": pg.add_node,
         "description": "Add a node to a canvas. Returns its nodeId + port handles. `config` maps param "
                        "name→value (see list_node_kinds). A `source` node needs config.uri.",
         "inputSchema": _schema({**canvas, "kind": {**_STR, "description": "node kind, e.g. source/filter/join/transform"},
                                 "title": _STR, "config": _OBJ}, ["canvasId", "kind"])},
        {"name": "connect", "handler": pg.connect,
         "description": "Wire one node's output into another's input. Use targetHandle for a multi-input "
                        "node (join 'a'/'b'). The typed-wire rules reject an incompatible connection.",
         "inputSchema": _schema({**canvas, "sourceId": _STR, "targetId": _STR,
                                 "targetHandle": {**_STR, "description": "input handle for a multi-input node"}},
                                ["canvasId", "sourceId", "targetId"])},
        {"name": "set_node_config", "handler": pg.set_node_config,
         "description": "Merge config values (param name→value) into an existing node.",
         "inputSchema": _schema({**canvas, "nodeId": _STR, "config": _OBJ}, ["canvasId", "nodeId", "config"])},
        {"name": "remove_node", "handler": pg.remove_node,
         "description": "Delete a node and every edge touching it.",
         "inputSchema": _schema({**canvas, "nodeId": _STR}, ["canvasId", "nodeId"])},
        {"name": "set_transform", "handler": pg.set_transform,
         "description": "Write (or update) a `transform` node's Python and immediately preview it — the "
                        "author-then-verify loop. For mode 'map' write `def fn(row): ...` returning the "
                        "row; other modes: map_batches/filter/flat_map. Pass upstreamNodeId to wire it to "
                        "its input on create, or nodeId to update an existing transform's code.",
         "inputSchema": _schema({**canvas, "code": {**_STR, "description": "Python cell (e.g. def fn(row): ...)"},
                                 "mode": {"type": "string", "enum": ["map", "map_batches", "filter", "flat_map"]},
                                 "batchFormat": {"type": "string", "enum": ["rows", "pandas", "arrow"]},
                                 "upstreamNodeId": {**_STR, "description": "node to feed this transform (on create)"},
                                 "nodeId": {**_STR, "description": "update this existing transform instead of creating one"},
                                 "title": _STR, "limit": _INT}, ["canvasId", "code"])},
        {"name": "preview_node", "handler": pg.preview_node,
         "description": "Preview a node's output over a bounded real sample: columns + rows. The way to "
                        "verify each step (including a transform's code) works before continuing.",
         "inputSchema": _schema({**canvas, "nodeId": _STR, "limit": {**_INT, "description": "max rows (default 10)"}},
                                ["canvasId", "nodeId"])},
        {"name": "validate_canvas", "handler": pg.validate_canvas,
         "description": "Static checks without running: typed-wire errors + per-join measured "
                        "cardinality and fan-out warnings.",
         "inputSchema": _schema(dict(canvas), ["canvasId"])},
        {"name": "run_canvas", "handler": pg.run_canvas,
         "description": "Run the pipeline up to a sink node (out-of-core, on the workspace's configured "
                        "execution backend — the same one the web UI uses) and wait for the result. Omit "
                        "nodeId if the canvas has a single sink. A large/unknown run returns "
                        "needsConfirm:true unless you pass confirm:true. A run still going after the poll "
                        "timeout returns timedOut:true + runId — then poll run_status / cancel_run.",
         "inputSchema": _schema({**canvas, "nodeId": {**_STR, "description": "the node to run to (a sink)"},
                                 "confirm": {"type": "boolean"}}, ["canvasId"])},
        {"name": "run_status", "handler": pg.run_status,
         "description": "Poll a run (by the runId from run_canvas) — follow a run that returned "
                        "timedOut:true to completion. Returns the same shape as run_canvas.",
         "inputSchema": _schema({"runId": _STR}, ["runId"])},
        {"name": "cancel_run", "handler": pg.cancel_run,
         "description": "Cancel an in-flight run by its runId. A finished run is returned unchanged.",
         "inputSchema": _schema({"runId": _STR}, ["runId"])},
        {"name": "sample_result", "handler": pg.sample_result,
         "description": "Sample the OUTPUT dataset a run materialized (by its runId): columns + real "
                        "rows — read what the pipeline actually produced without rebuilding its uri.",
         "inputSchema": _schema({"runId": _STR, "limit": {**_INT, "description": "max rows (default 20)"},
                                 "columns": {"type": "array", "items": _STR}}, ["runId"])},
    ]


# --------------------------------------------------------------------------- #
# JSON-RPC 2.0 dispatch.
# --------------------------------------------------------------------------- #
class MCPServer:
    def __init__(self, playground: Playground):
        self.pg = playground
        self._tools = {t["name"]: t for t in _tool_specs(playground)}

    # -- public: pure message → message (or None for a notification) ------- #
    def handle(self, msg: Any) -> Any:
        if isinstance(msg, list):  # JSON-RPC batch (pre-2025-06-18 clients) — reply to each in kind
            if not msg:  # an empty batch is itself an Invalid Request (JSON-RPC 2.0 §6)
                return _err_response(None, -32600, "invalid request — empty batch")
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict):
            return _err_response(None, -32600, "invalid request")
        is_notification = "id" not in msg
        mid = msg.get("id")
        params = msg.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            # JSON-RPC permits positional (array) params, but every MCP method takes a by-name object;
            # classify a non-object as Invalid params rather than letting a handler raise → -32603.
            return None if is_notification else _err_response(mid, -32602, "params must be an object")
        try:
            result = self._dispatch(msg.get("method"), params)
        except JsonRpcError as e:
            return None if is_notification else _err_response(mid, e.code, e.message, e.data)
        except Exception as e:  # noqa: BLE001 — never crash the loop; report as an internal error
            return None if is_notification else _err_response(mid, -32603, f"internal error: {type(e).__name__}: {e}")
        return None if is_notification else {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str | None, params: dict) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method and method.startswith("notifications/"):
            return None  # initialized / cancelled / progress — nothing to do; no response either way
        if method == "tools/list":
            return {"tools": [{"name": t["name"], "description": t["description"],
                               "inputSchema": t["inputSchema"]} for t in self._tools.values()]}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {"resources": self.pg.list_resources()}
        if method == "resources/read":
            uri = params.get("uri")
            if not uri:
                raise JsonRpcError(-32602, "resources/read requires 'uri'")
            try:
                return {"contents": [self.pg.read_resource(uri)]}
            except ToolError as e:
                raise JsonRpcError(-32002, str(e))  # MCP 'Resource not found' (distinct from bad params)
        raise JsonRpcError(-32601, f"method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in _SUPPORTED_PROTOCOLS else _LATEST_PROTOCOL
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": _INSTRUCTIONS}

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        tool = self._tools.get(name)
        if tool is None:
            raise JsonRpcError(-32602, f"unknown tool '{name}'")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _tool_result("arguments must be an object", is_error=True)
        try:
            result = tool["handler"](args)
        except ToolError as e:
            return _tool_result(str(e), is_error=True)
        except Exception as e:  # noqa: BLE001 — a tool bug is reported to the model, not crashed on
            return _tool_result(f"{type(e).__name__}: {e}", is_error=True)
        result = _jsonable(result)
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "structuredContent": result, "isError": False}


def _tool_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _err_response(mid: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


# --------------------------------------------------------------------------- #
# stdio transport.
# --------------------------------------------------------------------------- #
def serve_stdio(server: MCPServer, stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC messages from stdin, write responses to stdout, until EOF.

    CRITICAL: stdout is the protocol channel and must carry ONLY protocol messages — so we redirect
    the process-wide stdout to stderr for the duration and write responses to the ORIGINAL stdout.
    Any stray print()/library chatter during a tool call then lands on stderr and can't corrupt the
    stream. (The kernel's plugin loader, for one, prints to stdout.)"""
    stdin = stdin or sys.stdin
    real_out = stdout or sys.stdout
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        while True:
            line = stdin.readline()
            if not line:  # EOF — the client closed the pipe
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _write(real_out, _err_response(None, -32700, "parse error"))
                continue
            resp = server.handle(msg)
            if resp is not None:
                _write(real_out, resp)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        sys.stdout = saved


def _write(out, obj: Any) -> None:
    out.write(json.dumps(obj, default=str))
    out.write("\n")
    out.flush()


def build_server(base_url: str | None = None, user_id: str | None = None) -> MCPServer:
    """Build an MCPServer bound to the kernel's deps singleton + a resolved user. Called by the CLI
    (`dataplay mcp`) after the workspace is configured; also the entry point tests use."""
    from hub import metadb
    from hub.deps import get_deps
    from hub.settings import settings
    metadb.init_db()  # locked local init, or a strict production schema-head check (never production DDL)
    uid = metadb.resolve_user(user_id or metadb.DEFAULT_USER_ID)
    if user_id and uid != user_id:
        # resolve_user silently falls back to the default 'local' user for an unknown id — a typo'd
        # --user would then act as the bootstrap admin. Fail loudly instead.
        raise SystemExit(f"--user '{user_id}' is not a known user id")
    return MCPServer(Playground(get_deps(), uid, base_url or settings.base_url))


def build_http_server(user_id: str) -> MCPServer:
    """An MCPServer for the IN-PROCESS HTTP transport (POST /mcp on the web app), bound to the deps
    singleton the app already built and to the request's already-authenticated user. No init_db /
    resolve_user here — the app boot and the `current_user` dependency did both. Built per request
    (cheap); a mutating tool records touched canvases on `.pg.changed_canvases` for the live nudge."""
    from hub.deps import get_deps
    from hub.settings import settings
    return MCPServer(Playground(get_deps(), user_id, settings.base_url))
