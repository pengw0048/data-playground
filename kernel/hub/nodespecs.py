"""Backend node specs — the source of truth served at /api/nodes.

The frontend renders + validates ANY node (built-in or plugin) generically from these schemas,
so a plugin that registers a node needs no frontend code. Typed from day one (P7): ports and
params are structured, never stringly-typed tuples.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hub.models import ResourceSpec  # noqa: F401 — used in the NodeSpec.requires annotation

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
    required: bool = False   # empty → the node is invalid and can't run (frontend gates Run + reason)


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
    requires: "ResourceSpec | None" = None  # plugin-declared default compute need (e.g. gpu>=8); per-
    #                                          instance override lives in node config.requires (Phase B+)


def _in(accepts=("dataset", "sample"), wire="dataset", id="in", label=None):
    return PortSpec(id=id, label=label, wire=wire, accepts=list(accepts))


def _out(wire="dataset", id="out", label=None):
    return PortSpec(id=id, wire=wire, label=label)


BUILTIN_NODE_SPECS: list[NodeSpec] = [
    NodeSpec(kind="source", title="source", category="io", tag="dataset", inputs=[], outputs=[_out()],
             params=[ParamSpec(name="uri", type="string", label="dataset uri"),
                     ParamSpec(name="delimiter", type="string", label="CSV delimiter (blank=auto, 'tab'=TSV)"),
                     ParamSpec(name="header", type="select", options=["auto", "yes", "no"], default="auto", label="CSV header row")],
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
    # The single Python-code compute node (the old `notebook` kind folded in here; both ran the
    # SAME per-batch operator). `scope` labels whether it's exploring a sample or producing a
    # dataset — execution is identical, the tag just guides the mental model.
    NodeSpec(kind="transform", title="transform", category="compute", tag="code",
             inputs=[_in(("dataset", "sample", "selection"))], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="source", type="select", options=["adhoc", "library"], default="adhoc"),
                     ParamSpec(name="scope", type="select", options=["dataset", "sample"], default="dataset", label="runs over"),
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
             params=[ParamSpec(name="on", type="string", label="shared key(s)"),
                     ParamSpec(name="condition", type="string", label="or ON expression (a.x = b.y)"),
                     ParamSpec(name="how", type="select", options=["inner", "left", "right", "outer"], default="inner")],
             blurb="out-of-core hash join — shared keys, or an ON expression across differing keys"),
    NodeSpec(kind="aggregate", title="aggregate", category="compute", tag="aggregate",
             inputs=[_in(("dataset",))], outputs=[_out()], previewable=False,
             params=[ParamSpec(name="groupBy", type="string", label="group by"),
                     ParamSpec(name="aggs", type="string", default="count(*) AS n", label="aggregations")],
             blurb="group-by aggregation (out-of-core, needs full pass)"),
    NodeSpec(kind="sort", title="sort", category="shape", tag="sort",
             inputs=[_in()], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="by", type="string", label="order by", required=True)],
             blurb="streaming sort (spills)"),
    NodeSpec(kind="dedup", title="dedup", category="shape", tag="dedup",
             inputs=[_in()], outputs=[_out()], can_bypass=True,
             params=[ParamSpec(name="on", type="string", label="on columns (blank = all)")],
             blurb="distinct rows (hash-based, spillable)"),
    NodeSpec(kind="write", title="write", category="io", tag="write",
             inputs=[_in(("dataset", "sample", "selection"))], outputs=[_out()], previewable=False,
             # filename (its extension picks the format) + destination are edited on the card / panel
             params=[ParamSpec(name="writeMode", type="select", options=["overwrite", "append"], default="overwrite")],
             blurb="materialize to Parquet/CSV/Lance (streaming sink)"),
    NodeSpec(kind="metric", title="metric", category="inspect", tag="metric",
             inputs=[_in()], outputs=[_out("metric", label="value")],
             params=[ParamSpec(name="agg", type="select", options=["count", "mean", "sum", "min", "max"], default="count"),
                     ParamSpec(name="column", type="string")],
             blurb="reduce to a scalar"),
    NodeSpec(kind="chart", title="chart", category="inspect", tag="chart",
             inputs=[_in()], outputs=[_out()],  # emits the (x, y) series → chains like any dataset
             params=[ParamSpec(name="chartType", type="select", options=["bar", "line", "scatter", "area"], default="bar"),
                     ParamSpec(name="x", type="string", label="X column"),
                     ParamSpec(name="y", type="string", label="Y column"),
                     ParamSpec(name="agg", type="select", options=["none", "count", "sum", "mean", "min", "max"], default="count", label="aggregate Y by X")],
             blurb="visualize a column pair — grouped bar/line, or raw scatter"),
    NodeSpec(kind="vector-search", title="vector-search", category="query", tag="vector",
             inputs=[_in(("dataset",))], outputs=[_out()],
             params=[ParamSpec(name="column", type="string", default="embedding"),
                     ParamSpec(name="queryRow", type="int", default=0, label="query = row #"),
                     ParamSpec(name="queryVector", type="string", label="or query vector (JSON [..])"),
                     ParamSpec(name="k", type="int", default=10)],
             blurb="top-K nearest by cosine similarity to a chosen row (brute-force)"),
    # Meta-programming primitive (see docs/meta-programming.zh.md): a composite node whose
    # implementation is a driver script (Python) over contained nodes, with real control flow
    # (for/while/if), bounded. Not sample-previewable. The nested-frame UI to manage its contained
    # nodes is a later phase; the execution core is in kernel/section.py.
    NodeSpec(kind="section", title="section", category="compute", tag="section",
             inputs=[_in(("dataset", "sample"))], outputs=[_out()], previewable=False,
             params=[ParamSpec(name="script", type="code", lang="python",
                               default="# driver script — call contained nodes by alias\nemit(inputs['in'])")],
             blurb="composite node: a driver script over contained nodes (loops / branches)"),
    # NOTE: the old control-flow nodes (branch/loop/variable/opaque) were removed; branch was
    # redundant with two filters, and real control flow now lives in `section` above.
]
