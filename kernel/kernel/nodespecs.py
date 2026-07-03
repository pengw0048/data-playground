"""Backend node specs — the source of truth served at /api/nodes (PRD §4.2, §8.1).

The frontend renders + validates ANY node (built-in or plugin) generically from these schemas,
so a plugin that registers a node needs no frontend code. Typed from day one (P7): ports and
params are structured, never stringly-typed tuples.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

WireType = Literal["dataset", "sample", "selection", "sql-view", "metric", "value"]


class _M(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PortSpec(_M):
    id: str
    label: str | None = None
    wire: WireType = "dataset"
    accepts: list[WireType] | None = None


class ParamSpec(_M):
    name: str
    type: Literal["string", "text", "code", "int", "float", "bool", "select", "columns"]
    default: Any = None
    options: list[str] | None = None
    label: str | None = None
    lang: str | None = None  # for code params: 'python' | 'sql'


class NodeSpec(_M):
    kind: str
    title: str
    category: Literal["io", "shape", "compute", "query", "control", "inspect"]
    tag: str | None = None
    inputs: list[PortSpec] = []
    outputs: list[PortSpec] = []
    params: list[ParamSpec] = []
    can_bypass: bool = False
    previewable: bool = True
    blurb: str = ""


def _in(accepts=("dataset", "sample"), wire="dataset", id="in", label=None):
    return PortSpec(id=id, label=label, wire=wire, accepts=list(accepts))


def _out(wire="dataset", id="out", label=None):
    return PortSpec(id=id, wire=wire, label=label)


BUILTIN_NODE_SPECS: list[NodeSpec] = [
    NodeSpec(kind="source", title="source", category="io", tag="dataset", inputs=[], outputs=[_out()],
             params=[ParamSpec(name="uri", type="string", label="dataset uri")],
             blurb="read a registered dataset (Parquet/CSV/JSON/Arrow/Lance)"),
    NodeSpec(kind="sample", title="sample", category="shape", tag="sample",
             inputs=[_in(("dataset",))], outputs=[_out("sample")], can_bypass=True,
             params=[ParamSpec(name="n", type="int", default=1000), ParamSpec(name="seed", type="int", default=42)],
             blurb="take K rows (reservoir sample)"),
    NodeSpec(kind="filter", title="filter", category="shape", tag="filter",
             inputs=[_in()], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="predicate", type="string", label="predicate (SQL)")],
             blurb="row predicate (pushed down)"),
    NodeSpec(kind="select", title="select", category="shape", tag="select",
             inputs=[_in()], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="select", type="string", label="columns / expressions")],
             blurb="project / rename / derive columns"),
    NodeSpec(kind="transform", title="transform", category="compute", tag="transform",
             inputs=[_in(("dataset", "sample", "selection"))], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="source", type="select", options=["adhoc", "library"], default="adhoc"),
                     ParamSpec(name="mode", type="select", options=["map", "map_batches", "filter", "flat_map"], default="map"),
                     ParamSpec(name="code", type="code", lang="python")],
             blurb="Python over Arrow batches — library preset or ad-hoc cell"),
    NodeSpec(kind="sql", title="sql", category="query", tag="sql",
             inputs=[_in()], outputs=[_out("dataset")],  # a SQL view is a queryable relation → chains like any dataset
             params=[ParamSpec(name="sql", type="code", lang="sql", default="SELECT * FROM input LIMIT 100")],
             blurb="DuckDB SQL over inputs (references `input`)"),
    NodeSpec(kind="join", title="join", category="compute", tag="join",
             inputs=[_in(("dataset", "sample"), id="a", label="left"), _in(("dataset", "sample"), id="b", label="right")],
             outputs=[_out()],
             params=[ParamSpec(name="on", type="string", label="key(s)"),
                     ParamSpec(name="how", type="select", options=["inner", "left", "right", "outer"], default="inner")],
             blurb="out-of-core hash join on a key"),
    NodeSpec(kind="aggregate", title="aggregate", category="compute", tag="aggregate",
             inputs=[_in(("dataset",))], outputs=[_out()], previewable=False,
             params=[ParamSpec(name="groupBy", type="string", label="group by"),
                     ParamSpec(name="aggs", type="string", default="count(*) AS n", label="aggregations")],
             blurb="group-by aggregation (out-of-core, needs full pass)"),
    NodeSpec(kind="sort", title="sort", category="shape", tag="sort",
             inputs=[_in()], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="by", type="string", label="order by")],
             blurb="streaming sort (spills)"),
    NodeSpec(kind="dedup", title="dedup", category="shape", tag="dedup",
             inputs=[_in()], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="on", type="string", label="on columns (blank = all)")],
             blurb="distinct rows (hash-based, spillable)"),
    NodeSpec(kind="write", title="write", category="io", tag="write",
             inputs=[_in(("dataset", "sample", "selection"))], outputs=[_out()], previewable=False,
             params=[ParamSpec(name="name", type="string", label="output name"),
                     ParamSpec(name="format", type="select", options=["parquet", "csv", "lance"], default="parquet"),
                     ParamSpec(name="writeMode", type="select", options=["overwrite", "append"], default="overwrite")],
             blurb="materialize to Parquet/CSV/Lance (streaming sink)"),
    NodeSpec(kind="metric", title="metric", category="inspect", tag="metric",
             inputs=[_in()], outputs=[_out("metric", label="value")],
             params=[ParamSpec(name="agg", type="select", options=["count", "mean", "sum", "min", "max"], default="count"),
                     ParamSpec(name="column", type="string")],
             blurb="reduce to a scalar"),
    NodeSpec(kind="vector-search", title="vector-search", category="query", tag="vector",
             inputs=[_in(("dataset",))], outputs=[_out()],
             params=[ParamSpec(name="column", type="string", default="embedding"), ParamSpec(name="k", type="int", default=10)],
             blurb="top-K nearest by cosine similarity (Lance ANN / brute-force)"),
    NodeSpec(kind="notebook", title="notebook", category="inspect", tag="notebook",
             inputs=[_in(("sample", "dataset"), wire="sample")], outputs=[_out("sample")], can_bypass=True,
             params=[ParamSpec(name="code", type="code", lang="python"), ParamSpec(name="mode", type="select", options=["map", "map_batches", "filter", "flat_map"], default="map")],
             blurb="embedded cell over a sample"),
    NodeSpec(kind="branch", title="branch", category="control", tag="branch",
             inputs=[_in(("dataset", "sample", "metric"))],
             outputs=[_out(id="true", label="true"), _out(id="false", label="false")],
             params=[ParamSpec(name="predicate", type="string")],
             blurb="route by predicate / metric — no cycle"),
    NodeSpec(kind="loop", title="loop", category="control", tag="loop",
             inputs=[_in()], outputs=[_out()], previewable=False,
             params=[ParamSpec(name="maxIters", type="int", default=5), ParamSpec(name="budgetUsd", type="float", default=20)],
             blurb="bounded iterate over a subgraph body"),
    NodeSpec(kind="variable", title="variable", category="control", tag="variable",
             inputs=[_in(("metric", "sample", "dataset"), wire="metric")], outputs=[_out("value")],
             params=[ParamSpec(name="column", type="string", label="drives param")],
             blurb="a node output drives another node's param"),
    NodeSpec(kind="opaque", title="opaque", category="control", tag="opaque",
             inputs=[_in()], outputs=[_out()], previewable=False,
             params=[], blurb="an op that can't be sampled — needs full pass"),
]
