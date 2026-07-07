"""Reference plugin — a **pipeline importer** that turns a tiny JSON pipeline format into a runnable
canvas graph.

`import = parse a foreign pipeline into a canvas` is the last big extension seam. The generic core
ships no importer (there's no universal pipeline format), so this is a worked example of the
`reg.set_importer(...)` SPI: it parses a small, self-describing JSON document into a chain of built-in
nodes (`source → …steps… → write`) and returns it as `PipelineImport.graph`. The SPA drops that graph
onto a fresh canvas and it runs like anything else — proving `import → canvas → run`, plugin-only.

The format (illustrative — a real importer parses your org's format instead)::

    {
      "source": "events",                              # a catalog table name or a uri
      "steps": [
        {"filter": "amount > 0"},                      # → filter node   (config.predicate)
        {"select": "id, amount"},                      # → select node   (config.select)
        {"aggregate": {"groupBy": "id", "aggs": "sum(amount) AS total"}},   # dict → config as-is
        {"sql": "SELECT * FROM input LIMIT 100"}        # → sql node      (config.sql)
      ],
      "write": {"name": "out", "format": "parquet"}    # → write node
    }

Each step is a one-key dict `{kind: config}`; a string config is wrapped into the kind's primary param,
a dict config is used verbatim. Node positions are left at (0,0) — the core lays the graph out on
import. Drop this folder into `<workspace>/plugins/` or install it as a `dataplay.plugins` entry point.
"""

from __future__ import annotations

from hub.models import Graph, GraphEdge, GraphEdgeData, GraphNode, PipelineImport

# a string step's value goes into this param for the kind; kinds not listed take a dict config verbatim
_STRING_PARAM = {"filter": "predicate", "select": "select", "sql": "sql", "sort": "by", "dedup": "on"}
_DRIVER_KIND = {"source": "read", "write": "write"}  # else "op" — for the human-readable driver_steps


class JsonPipelineImporter:
    name = "json-pipeline"

    def import_pipeline(self, config: str, params: dict | None) -> PipelineImport:
        import json

        spec = json.loads(config)
        if not isinstance(spec, dict) or "source" not in spec:
            raise ValueError("expected a JSON object with a 'source' (and optional 'steps'/'write')")

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        def add(nid: str, kind: str, cfg: dict) -> None:
            if nodes:  # chain to the previous node with a dataset wire
                prev = nodes[-1].id
                edges.append(GraphEdge(id=f"e-{prev}-{nid}", source=prev, target=nid, data=GraphEdgeData(wire="dataset")))
            nodes.append(GraphNode(id=nid, type=kind, data={"config": cfg}))

        add("src", "source", {"uri": str(spec["source"])})
        for i, step in enumerate(spec.get("steps", []), 1):
            if not isinstance(step, dict) or len(step) != 1:
                raise ValueError(f"step {i} must be a one-key object {{kind: config}}, got {step!r}")
            kind, val = next(iter(step.items()))
            cfg = val if isinstance(val, dict) else {_STRING_PARAM.get(kind, kind): val}
            add(f"step{i}", kind, cfg)
        if "write" in spec:
            w = spec["write"]
            add("sink", "write", w if isinstance(w, dict) else {"name": str(w)})

        driver = [{"kind": _DRIVER_KIND.get(n.type, "op"), "label": n.type, "node_type": n.type} for n in nodes]
        return PipelineImport(config=config, params=params or {}, driver_steps=driver,
                              graph=Graph(id="canvas", version=1, nodes=nodes, edges=edges))


def register(reg) -> None:
    reg.set_importer(JsonPipelineImporter())  # claims POST /pipelines/import
