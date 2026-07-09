"""The build engine — a node builds a step in a typed logical plan.

The `dataset` wire is a lazy DuckDB relation; relational ops (filter/select/join/aggregate/
sort/dedup/sql/sample) build relation transforms that DuckDB executes out-of-core (streaming,
spilling to disk). The `transform` node is the escape hatch: arbitrary Python over Arrow
`RecordBatch`es. The SAME relation is executed on a bounded sample (preview) or in full (run),
so what you see on the sample is faithful — except nodes flagged not-previewable (P8).
"""

from __future__ import annotations

import json
import re
from typing import Any

import duckdb
import pyarrow as pa

from hub import db, graph as g, sandbox
from hub.ir import resolve_config  # single source of built-in node config resolution (shared with the IR)
from hub.models import PREVIEWABLE_MODES, ColumnSchema, Graph, GraphNode
from hub.plugins.adapters import display_type, relation_columns
from hub.plugins.capabilities import tag_columns

Relation = duckdb.DuckDBPyRelation

# Node kinds whose result cannot be faithfully computed on a truncated sample (P8).
NOT_PREVIEWABLE_KINDS = {"aggregate", "write", "opaque", "loop", "section"}
_TRANSFORM_KINDS = {"transform", "notebook"}


class NotPreviewable(Exception):
    def __init__(self, node: GraphNode, reason: str):
        self.node = node
        self.reason = reason
        super().__init__(reason)


def _cfg(node: GraphNode) -> dict:
    return node.data.get("config", {}) if isinstance(node.data, dict) else {}


def _bypassed(node: GraphNode) -> bool:
    return bool(node.data.get("bypassed")) if isinstance(node.data, dict) else False


_PLAIN_COL = re.compile(r'^\s*(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*$')


def _plain_columns(expr: str) -> list[str] | None:
    """A `select` expression → list of bare column names IFF it is a plain comma-separated identifier
    list (quoted or bare). Returns None for anything else — `*`, `a AS b`, `a+1`, `f(x)`, `t.a` — so a
    projection push-down is only ever a provable pure column subset (never drops a needed column)."""
    if not expr:
        return None
    cols: list[str] = []
    for part in expr.split(","):
        m = _PLAIN_COL.match(part)
        if not m:
            return None
        cols.append(m.group(1) or m.group(2))
    return cols or None


def _disabled(node: GraphNode) -> bool:
    return bool(node.data.get("disabled")) if isinstance(node.data, dict) else False


# code-op kinds whose OUTPUT columns can't be resolved without running them → untyped ports, UNLESS
# the node carries a user-declared schema contract (config.outputSchema). See executors/schema.
_CODE_KINDS = {"transform", "notebook", "section", "vector-search", "loop", "opaque"}

_DUCK_TYPE = {
    "int": "BIGINT", "integer": "BIGINT", "bigint": "BIGINT", "long": "BIGINT",
    "smallint": "BIGINT", "tinyint": "BIGINT", "hugeint": "HUGEINT",
    "float": "DOUBLE", "double": "DOUBLE", "real": "DOUBLE", "number": "DOUBLE", "decimal": "DOUBLE",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
    "string": "VARCHAR", "str": "VARCHAR", "text": "VARCHAR", "varchar": "VARCHAR",
    "char": "VARCHAR", "utf8": "VARCHAR",
    "date": "DATE", "timestamp": "TIMESTAMP", "datetime": "TIMESTAMP", "time": "TIME",
    "blob": "BLOB", "bytes": "BLOB", "json": "JSON",
}


def declared_schema(node: GraphNode) -> list | None:
    """A user-declared output-schema contract (config.outputSchema) on a code op, or None. Lets a
    transform/plugin node carry a typed port + propagate types downstream without ever being run."""
    sch = _cfg(node).get("outputSchema")
    return sch if isinstance(sch, list) and sch else None


def _duck_type(t: object) -> str:
    """Map a declared/display column type to a DuckDB type for a schema-only stand-in relation. Coarse
    on purpose (column-NAME propagation is what matters; exact type rarely does) — unknown → VARCHAR."""
    s = str(t or "").strip()
    is_list = s.endswith("]") or "list" in s.lower()
    base = re.sub(r"[\[(<].*$", "", s).strip().lower()
    d = _DUCK_TYPE.get(base, "VARCHAR")
    return f"{d}[]" if is_list else d


class BuildEngine:
    def __init__(self, graph: Graph, resolve_adapter, registry, sample_k: int | None = None,
                 full: bool = False, node_builders: dict | None = None, node_specs: dict | None = None,
                 bound_inputs: dict | None = None, spill_files: list | None = None,
                 schema_only: bool = False, warm=None, warm_scope: str = "",
                 pushdown: bool = False, output_node: str | None = None):
        self.graph = graph
        self.resolve_adapter = resolve_adapter
        self.registry = registry
        self.sample_k = sample_k
        self.full = full
        # pushdown: hand a single-consumer source→filter/select's predicate/projection to adapter.scan()
        # so an adapter that prunes at the source does. Full-run write/count paths only (preview keeps
        # the plain scan so previewing a source shows the source). output_node = the run's requested
        # target: never prune it (you asked to read that node directly). See _source_pushdown.
        self.pushdown = pushdown
        self._output_node = output_node
        # schema_only: we only want column names/types, never rows — scan sources with limit=0 so
        # even an eager adapter (Lance materializes on to_table) touches no data. See executors/schema.
        self.schema_only = schema_only
        self.node_builders = node_builders or {}
        self.node_specs = node_specs or {}
        # a node id -> Relation to inject as that node's input (used to run a section's sub-node
        # against a script-provided handle instead of a wired upstream edge)
        self.bound_inputs = bound_inputs or {}
        # temp parquet files spilled during this run (transform spill, section _materialize); the
        # runner deletes them in its finally so they don't accumulate across the kernel's lifetime.
        # Shared with sub-engines (sections) so a contained transform's spill is GC'd too.
        self.spill_files = spill_files if spill_files is not None else []
        # warm: an optional cross-build RelationCache (the kernel's, preview scope) — reuses unchanged
        # upstream node relations across previews. warm_scope isolates preview vs run keys.
        self.warm = warm
        self.warm_scope = warm_scope
        # a node builds either one Relation (single output) or a dict of named output ports
        # (multi-output — e.g. a section that emit()s several named result sets)
        self._cache: dict[str, "Relation | dict[str, Relation]"] = {}

    # -- public ------------------------------------------------------------ #
    def relation(self, node_id: str, handle: str | None = None) -> Relation:
        if node_id not in self._cache:
            self._cache[node_id] = self._warm_or_lower(node_id)
        return self._pick(node_id, self._cache[node_id], handle)

    def _warm_or_lower(self, node_id: str):
        """Build a node — reusing the warm cache when set. A hit skips the whole upstream subgraph; a
        cacheable miss is materialized (row-capped) for the next preview. Keyed by the shared plan_hash
        so any edit invalidates it; only single-output, cacheable nodes are cached."""
        node = g.node_map(self.graph)[node_id]
        if self.warm is None:
            return self._lower(node)
        from hub.plan_key import plan_cacheable, plan_hash
        if not plan_cacheable(self.graph, node_id, self.node_builders):
            return self._lower(node)
        key = f"{self.warm_scope}:{plan_hash(self.graph, node_id, self.resolve_adapter)}"
        hit = self.warm.get(key)
        if hit is not None:
            return hit
        built = self._lower(node)
        if not isinstance(built, dict):  # cache single-output relations only (multi-output is rarer)
            cached = self.warm.put(key, self._view(built))
            if cached is not None:
                return cached
        return built

    def _pick(self, node_id: str, built, handle: str | None) -> Relation:
        # single-output nodes build a bare Relation and ignore the handle; multi-output nodes
        # build {port -> Relation}, so route by the edge's source_handle (default port = "out").
        if not isinstance(built, dict):
            return built
        if handle is not None and handle in built:
            return built[handle]
        if handle is None:
            # explicit key check — a DuckDB relation is falsy when it has 0 rows (defines __len__),
            # so `built.get("out") or …` would wrongly skip an empty default port (and force a count).
            return built["out"] if "out" in built else next(iter(built.values()))
        raise NotPreviewable(g.node_map(self.graph)[node_id], f"output port '{handle}' was not produced")

    def rows(self, node_id: str, k: int, offset: int = 0) -> tuple[list[dict], list[ColumnSchema]]:
        tbl = self.relation(node_id).limit(k, offset).to_arrow_table()
        # a join over columns that share a name (e.g. both inputs have `id`) yields duplicate
        # column names; de-dup so no column is silently dropped when rows become dicts.
        names = _dedupe_names(tbl.column_names)
        if names != tbl.column_names:
            tbl = tbl.rename_columns(names)
        cols = tag_columns([ColumnSchema(name=n, type=display_type(str(t)))
                            for n, t in zip(tbl.column_names, tbl.schema.types)])
        return _table_to_rows(tbl), cols

    # -- inputs ------------------------------------------------------------ #
    def _inputs(self, node: GraphNode) -> list[Relation]:
        if node.id in self.bound_inputs:  # section sub-node: input injected by the driver script
            return [self.bound_inputs[node.id]]
        # route each incoming edge by its source port (multi-output nodes build {port -> Relation})
        return [self.relation(e.source, e.source_handle) for e in g.incoming(self.graph, node.id)]

    def _faithful_inputs(self, node: GraphNode) -> list[Relation]:
        """Build this node's inputs UNSAMPLED even during preview, so an op that must see all rows
        (join / sort / vector-search) is FAITHFUL — the op's own LIMIT then makes it an efficient
        top-N, bounded by the preview budget. A truncated-prefix version of these ops lies (a join of
        two independent 2000-row prefixes finds few real matches; a sort/vector-search shows the top
        of an arbitrary prefix, not the true top-K). Refuse honestly (P8) if a Python transform is
        upstream — a 'full' eval would spill every row inside a preview."""
        if self.full:
            return self._inputs(node)
        chain = g.upstream_chain(self.graph, node.id)
        if any(n.type in ("transform", "notebook", "opaque", "loop") for n in chain):
            raise NotPreviewable(node, f"{node.type} over a transformed input — needs a full pass")
        full = BuildEngine(self.graph, self.resolve_adapter, self.registry, sample_k=None, full=True,
                              node_builders=self.node_builders, node_specs=self.node_specs)
        return [full.relation(e.source, e.source_handle) for e in g.incoming(self.graph, node.id)]

    def _join_projection(self, left: Relation, right: Relation) -> str:
        """SELECT list for an ON-expression join: all left columns as-is, right columns renamed with a
        `_2` suffix where they clash with a left column, so the joined relation has no duplicate names."""
        lcols = list(left.columns)
        lset = set(lcols)
        parts = [f'a."{c}"' for c in lcols]
        parts += [f'b."{c}" AS "{c}_2"' if c in lset else f'b."{c}"' for c in right.columns]
        return ", ".join(parts)

    def _view(self, rel: Relation, base: str = "v") -> str:
        # process-globally-unique name so concurrent engines never clobber each other's views
        name = db.unique_view(base)
        rel.create_view(name, replace=True)
        return name

    def _stand_in(self, cols: list) -> Relation:
        """An empty relation with the declared column names/types — a typed schema_only stand-in for a
        code op, so a declared contract propagates to downstream relational ops without running code."""
        def _name(c) -> str:
            return str((c.get("name") if isinstance(c, dict) else getattr(c, "name", "")) or "col")

        def _type(c):
            return c.get("type") if isinstance(c, dict) else getattr(c, "type", None)

        def _q(n: str) -> str:  # a safely-quoted SQL identifier — double any embedded quote
            return '"' + n.replace('"', '""') + '"'

        names = [_name(c) for c in cols] or ["col"]
        try:
            parts = [f'CAST(NULL AS {_duck_type(_type(c))}) AS {_q(_name(c))}' for c in cols] or ["NULL AS col"]
            return db.conn().sql(f"SELECT {', '.join(parts)} LIMIT 0")
        except Exception:  # noqa: BLE001 — a declared type didn't parse → all-VARCHAR (names still propagate)
            parts = [f'CAST(NULL AS VARCHAR) AS {_q(n)}' for n in names]
            return db.conn().sql(f"SELECT {', '.join(parts)} LIMIT 0")

    def _source_pushdown(self, node: GraphNode) -> tuple[list[str] | None, str | None]:
        """When a source has EXACTLY ONE consumer and it's a plain (not bypassed/disabled) filter or
        select, hand that predicate / projection to adapter.scan() — so a warehouse/Iceberg/plugin
        adapter prunes rows or columns at the source (the built-in DuckDB adapter honors them too). The
        consumer node ALSO applies its own op, so the result is byte-identical whether or not the
        adapter honors the hint; a source with 0 or ≥2 consumers is left alone (can't prove safety)."""
        outs = g.outgoing(self.graph, node.id)
        if len(outs) != 1:
            return None, None
        consumer = g.node_map(self.graph).get(outs[0].target)
        if consumer is None or _disabled(consumer) or _bypassed(consumer):
            return None, None
        ccfg = _cfg(consumer)
        if consumer.type == "filter":
            return None, ((ccfg.get("predicate") or "").strip() or None)
        if consumer.type == "select":
            return _plain_columns((ccfg.get("select") or ccfg.get("expr") or "").strip()), None
        return None, None

    # -- building ---------------------------------------------------------- #
    def _lower(self, node: GraphNode) -> Relation:  # noqa: C901
        t = node.type
        cfg = resolve_config(node)  # the SAME resolver the IR uses (hub.ir) — one source of built-in config

        # a disabled node (and, since inputs pull through it, everything downstream) produces nothing
        if _disabled(node):
            raise NotPreviewable(node, "node is disabled")

        # a declared output-schema contract on a code op: in schema_only mode, stand in a typed empty
        # relation so its columns (and everything downstream) type WITHOUT running the code. A BYPASSED
        # node is skipped — it passes its input through (handled below), so the declaration doesn't apply.
        if self.schema_only and not _bypassed(node) and (t in _CODE_KINDS or t in self.node_builders):
            dsch = declared_schema(node)
            if dsch is not None:
                return self._stand_in(dsch)

        if t == "source":
            uri = cfg.get("uri")
            if not uri:
                raise NotPreviewable(node, "no dataset selected")
            from hub import paths
            paths.ensure_local_uri_allowed(uri)  # multi-user: a source can't read an arbitrary local file
            # CSV parse overrides (delimiter/header), already normalized + nested under 'options' by the
            # resolver — passed only when set, so an adapter whose scan() predates the kwarg keeps working
            extra = {"options": cfg["options"]} if cfg.get("options") else {}
            if self.schema_only:
                return self.resolve_adapter(uri).scan(uri, limit=0, **extra)  # metadata only — never materialize
            if self.pushdown and node.id != self._output_node:
                cols, pred = self._source_pushdown(node)  # prune at the source for adapters that can
                if cols:
                    extra["columns"] = cols
                if pred:
                    extra["predicate"] = pred
            rel = self.resolve_adapter(uri).scan(uri, **extra)
            if self.sample_k and not self.full:
                rel = rel.limit(self.sample_k)
            return rel

        inputs = self._inputs(node)
        # plugin-provided node kinds (§8.1) — dispatch BEFORE the no-inputs guard so a plugin
        # can define a 0-input source/generator. Honor the plugin's declared previewable.
        if t in self.node_builders:
            if not self.full and not self._spec_previewable(t):
                raise NotPreviewable(node, f"'{t}' is not sample-previewable — needs a full pass")
            return self.node_builders[t](self, node, inputs)

        if t == "section":  # composite node implemented by a driver script over contained nodes
            if not self.full:  # runs real work over its nodes — not faithful on a sample (P8)
                raise NotPreviewable(node, "a section runs real work over its nodes — needs a full pass")
            from hub.section import run_section
            return run_section(self, node, inputs)  # {port -> Relation}: routed by _pick per edge

        if not inputs and t not in ("source",):
            raise NotPreviewable(node, "not connected to a source")
        parent = inputs[0] if inputs else None

        if _bypassed(node) and parent is not None:
            return parent

        if t == "sample":
            # default ONLY when n is unset (None) — a configured n=0 means 0 rows, not the fallback
            n = cfg.get("n")
            n = max(0, int(n if n is not None else (self.sample_k or 1000)))
            seed = int(cfg.get("seed", 42))
            v = self._view(parent, "s")
            return db.conn().sql(f"SELECT * FROM {v} USING SAMPLE {n} ROWS (reservoir, {seed})")

        if t == "filter":
            pred = (cfg.get("predicate") or "").strip()
            return parent.filter(pred) if pred else parent

        if t == "assert":
            # a data-quality gate — the node's relation IS the VIOLATING rows (so "view data" shows exactly
            # what failed). `IS NOT TRUE` catches both false AND null, so `x > 0` flags a null x too. The
            # runner fails the run on error-severity violations (see plugins/runner.py); no predicate =
            # nothing violates → passthrough. Same columns as the input (SELECT *), so its port stays typed.
            pred = (cfg.get("predicate") or "").strip()
            v = self._view(parent, "as")
            # no predicate → ZERO violations (NOT `return parent`: this relation is the VIOLATING rows, so
            # passing the input through would count every row as a violation). `WHERE false` keeps the schema.
            return db.conn().sql(f"SELECT * FROM {v} WHERE {f'({pred}) IS NOT TRUE' if pred else 'false'}")

        if t == "select":
            expr = (cfg.get("expr") or "").strip()  # resolver canonicalizes select/expr → 'expr'
            return parent.project(expr) if expr else parent

        if t == "sort":
            by = (cfg.get("by") or "").strip()
            if not by:
                return parent
            # the true top-N is over ALL rows, not a 2000-row prefix — sort the full input in preview
            # too (the preview limit turns it into an efficient top-N)
            src = parent if self.full else self._faithful_inputs(node)[0]
            return src.order(by)

        if t == "dedup":
            on = (cfg.get("on") or "").strip()
            if on:
                v = self._view(parent, "d")
                cols = ", ".join(f'"{c.strip()}"' for c in on.split(","))
                return db.conn().sql(f"SELECT DISTINCT ON ({cols}) * FROM {v}")
            return parent.distinct()

        if t == "window":
            expr = (cfg.get("expr") or "").strip()
            if not expr:
                return parent
            part = (cfg.get("partitionBy") or "").strip()
            order = (cfg.get("orderBy") or "").strip()
            over = " ".join(x for x in [f"PARTITION BY {part}" if part else "",
                                        f"ORDER BY {order}" if order else ""] if x)
            col = (cfg.get("as") or "").strip() or "window"  # strip THEN default (all-spaces → "window", not "")
            # a window fn ranks/aggregates ACROSS rows, so a sample would lie (rank within the sample, a
            # partial SUM) — compute over the full input in preview too, like sort (the preview LIMIT then
            # just truncates the display).
            src = parent if self.full else self._faithful_inputs(node)[0]
            v = self._view(src, "w")
            return db.conn().sql(f'SELECT *, {expr} OVER ({over}) AS "{col}" FROM {v}')

        if t == "fill":
            cols = [c.strip() for c in (cfg.get("columns") or "").split(",") if c.strip()]
            if not cols:
                return parent
            method = (cfg.get("method") or "constant").strip()
            value = (cfg.get("value") or "").strip()

            def _fill(c: str) -> str:
                q = f'"{c}"'
                if method == "constant":
                    return f"COALESCE({q}, {value})" if value else q  # blank value → no-op replace
                if method == "zero":
                    return f"COALESCE({q}, 0)"
                agg = {"mean": "avg", "min": "min", "max": "max"}.get(method)
                return f"COALESCE({q}, {agg}({q}) OVER ())" if agg else q

            # mean/min/max impute from a WHOLE-COLUMN aggregate → a sample would compute the wrong fill
            # value; run over the full input in preview (constant/zero are per-row, so stay on `parent`).
            faithful = method in ("mean", "min", "max") and not self.full
            v = self._view(self._faithful_inputs(node)[0] if faithful else parent, "fl")
            repl = ", ".join(f'{_fill(c)} AS "{c}"' for c in cols)
            return db.conn().sql(f"SELECT * REPLACE ({repl}) FROM {v}")

        if t == "unnest":
            col = (cfg.get("column") or "").strip()
            if not col:
                return parent
            v = self._view(parent, "un")  # explode a list column → one row per element, others repeated
            return db.conn().sql(f'SELECT * EXCLUDE ("{col}"), unnest("{col}") AS "{col}" FROM {v}')

        if t == "aggregate":
            if not self.full:
                raise NotPreviewable(node, "global aggregate — needs a full pass (a sample would lie)")
            aggs = (cfg.get("aggs") or "count(*) AS n").strip()
            group = (cfg.get("groupBy") or "").strip()  # resolver canonicalizes groupBy/group → 'groupBy'
            # include the group key(s) in the projection, else the aggregated rows are unlabeled
            return parent.aggregate(f"{group}, {aggs}", group) if group else parent.aggregate(aggs)

        if t == "sql":
            q = (cfg.get("sql") or "").strip()
            if not q:
                return parent
            # Expose inputs as query-scoped CTEs named input/input2/... backed by UNIQUE views,
            # so two sql nodes in one graph never clobber a shared literal 'input' view.
            aliases = ["input"] + [f"input{i + 1}" for i in range(1, len(inputs))]
            ctes = [f"{a} AS (SELECT * FROM {self._view(rel)})" for a, rel in zip(aliases, inputs)]
            cte = "WITH " + ", ".join(ctes)
            wrapped = f"{cte}, {q[4:].lstrip()}" if q[:4].upper() == "WITH" else f"{cte} {q}"
            return db.conn().sql(wrapped)

        if t == "join":
            if len(inputs) < 2:
                return parent
            on = (cfg.get("on") or "").strip()
            cond = (cfg.get("condition") or "").strip()  # raw ON expression, aliases a.<col> / b.<col>
            how = (cfg.get("how") or "inner").lower()
            how = how if how in ("inner", "left", "right", "full", "outer", "cross") else "inner"
            how = "full" if how == "outer" else how
            # joining two independently-truncated prefixes finds few/no real matches — join the FULL
            # inputs even in preview (bounded by the preview limit + budget)
            ins = inputs if self.full else self._faithful_inputs(node)
            a, b = self._view(ins[0], "ja"), self._view(ins[1], "jb")
            if how == "cross" or (not on and not cond):
                return db.conn().sql(f"SELECT a.*, b.* FROM {a} AS a CROSS JOIN {b} AS b")
            if cond:
                # an ON expression (e.g. `a.user_id = b.uid`, or a composite/inequality condition) —
                # keep BOTH sides' columns but rename right-side name clashes, so a downstream select/sql
                # isn't ambiguous (USING coalesces keys; a bare ON does not)
                proj = self._join_projection(ins[0], ins[1])
                return db.conn().sql(f"SELECT {proj} FROM {a} AS a {how.upper()} JOIN {b} AS b ON ({cond})")
            cols = ", ".join(f'"{c.strip()}"' for c in on.split(","))
            return db.conn().sql(f"SELECT * FROM {a} {how.upper()} JOIN {b} USING ({cols})")

        if t in _TRANSFORM_KINDS:
            return self._transform(node, parent)

        if t == "metric":
            agg = cfg.get("agg", "count")
            col = cfg.get("column")
            # honest (P8): a metric reduces over ALL rows, so compute over the FULL input even in
            # preview — never a truncated sample value. (Relational upstream is cheap out-of-core.)
            base = parent
            if not self.full:
                # computing the true value means a full pass over the upstream. That is cheap for
                # relational ops (DuckDB), but a Python transform upstream would spill EVERY row
                # inside a "preview" — refuse honestly in that case (P8) rather than run away.
                chain = g.upstream_chain(self.graph, node.id)
                if any(n.type in ("transform", "notebook", "opaque", "loop") for n in chain):
                    raise NotPreviewable(node, "metric over a transformed input — needs a full pass")
                inc = g.incoming(self.graph, node.id)
                if inc:
                    full = BuildEngine(self.graph, self.resolve_adapter, self.registry,
                                          sample_k=None, full=True, node_builders=self.node_builders,
                                          node_specs=self.node_specs)
                    base = full.relation(inc[0].source, inc[0].source_handle)
            expr = "count(*)" if agg == "count" or not col else f'{_agg_name(agg)}("{_ident(col)}")'
            v = self._view(base, "m")
            title = (node.data.get("title") if isinstance(node.data, dict) else None) or "metric"
            return db.conn().sql(f"SELECT '{_sql_str(title)}' AS metric, ({expr})::DOUBLE AS value FROM {v}")

        if t == "chart":
            x, y, agg = cfg.get("x"), cfg.get("y"), cfg.get("agg", "count")  # default matches nodespec/UI
            if not x:
                raise NotPreviewable(node, "pick an X column to chart")
            if agg == "none" and not y:
                raise NotPreviewable(node, "pick a Y column (or an aggregation) to chart")
            if agg not in ("none", "count") and not y:  # sum/mean/min/max need a Y (don't silently count)
                raise NotPreviewable(node, f"pick a Y column to {agg}")
            base = parent
            if agg != "none" and not self.full:
                # a grouped chart aggregates over ALL rows — compute over the full input even in
                # preview (honest, like metric); refuse if a Python transform is upstream.
                chain = g.upstream_chain(self.graph, node.id)
                if any(n.type in ("transform", "notebook", "opaque", "loop") for n in chain):
                    raise NotPreviewable(node, "chart over a transformed input — needs a full pass")
                inc = g.incoming(self.graph, node.id)
                if inc:
                    full = BuildEngine(self.graph, self.resolve_adapter, self.registry, sample_k=None,
                                          full=True, node_builders=self.node_builders, node_specs=self.node_specs)
                    base = full.relation(inc[0].source, inc[0].source_handle)
            v, xq = self._view(base, "ch"), f'"{_ident(x)}"'
            if agg == "none":  # raw points (scatter/line) — the chart series is x,y as-is
                return db.conn().sql(f'SELECT {xq} AS x, "{_ident(y)}" AS y FROM {v}')
            yexpr = "count(*)" if agg == "count" or not y else f'{_agg_name(agg)}("{_ident(y)}")'
            # grouped series (bar/line): one point per distinct x, capped so a huge-cardinality x can't
            # blow up the chart. TRY_CAST (not ::DOUBLE) so a non-numeric/temporal min/max degrades to
            # NULL (dropped by the renderer) instead of a raw ConversionException.
            return db.conn().sql(f"SELECT {xq} AS x, TRY_CAST(({yexpr}) AS DOUBLE) AS y FROM {v} GROUP BY {xq} ORDER BY {xq} LIMIT 2000")

        if t == "vector-search":
            return self._vector_search(node, inputs)

        if t in ("write", "opaque", "loop"):
            if self.full and parent is not None:
                return parent  # runner performs the real work / commit; here we pass through
            raise NotPreviewable(node, {
                "write": "commit is all-or-nothing — needs a full pass",
                "opaque": "opaque op — needs a full pass",
                "loop": "each loop pass runs real work — needs a full pass",
            }[t])

        return parent if parent is not None else _empty()

    def _spec_previewable(self, kind: str) -> bool:
        spec = self.node_specs.get(kind)
        return bool(getattr(spec, "previewable", True)) if spec is not None else True

    # -- transform escape hatch (Python over Arrow batches) ---------------- #
    def _transform(self, node: GraphNode, parent: Relation) -> Relation:
        cfg = resolve_config(node)  # shared resolver (hub.ir): mode/code/source/processor/params/onError
        if node.type == "transform" and cfg.get("source") == "library":
            pid = cfg.get("processor")
            if pid and self.registry.has(pid):
                proc = self.registry.get(pid)
                fn, mode = proc.build(cfg.get("params", {})), proc.mode
            elif cfg.get("code"):
                # the library processor isn't registered (e.g. an in-memory promote lost on restart),
                # but the node kept its original code — run that instead of failing (no data loss).
                mode = cfg.get("mode", "map")
                fn = sandbox.compile_operator(cfg["code"], mode)
            else:
                raise NotPreviewable(node, f"processor '{pid}' is not registered")
        else:
            code = cfg.get("code")
            mode = cfg.get("mode", "map")
            if not code:
                return parent
            fn = sandbox.compile_operator(code, mode)

        if mode not in PREVIEWABLE_MODES:
            raise NotPreviewable(node, f"transform mode '{mode}' needs a full pass")

        on_error = cfg.get("onError", "raise")
        # map_batches can hand the whole batch to the cell as a pandas DataFrame or a pyarrow Table
        # (type-preserving) instead of the default row-dicts. Row modes (map/filter/flat_map) are dicts.
        fmt = cfg.get("batchFormat", "rows") if mode == "map_batches" else "rows"
        try:
            if self.full:
                return self._transform_spill(node, parent, fn, mode, on_error, fmt)
            # preview: input is bounded (source sampled), so in-memory is fine and fast
            if fmt in ("pandas", "arrow"):
                tables: list = []
                for b in parent.to_arrow_reader(batch_size=2048):
                    t = _apply_batch(fn, pa.Table.from_batches([b]), fmt, on_error, node)
                    if t is None:  # on_error='skip' dropped this batch
                        continue
                    if tables:  # tolerate per-batch dtype drift by casting to the first (matches the spill path)
                        try:
                            t = t.cast(tables[0].schema)
                        except Exception:  # noqa: BLE001
                            t = t.cast(tables[0].schema, safe=False)
                    tables.append(t)
                table = pa.concat_tables(tables) if tables else parent.limit(0).to_arrow_table()
                return db.conn().from_arrow(table)
            out: list[dict] = []
            for batch in parent.to_arrow_reader(batch_size=2048):
                out.extend(_apply_fn(fn, batch, mode, on_error, node))
            table = pa.Table.from_pylist(out) if out else parent.limit(0).to_arrow_table()
            return db.conn().from_arrow(table)
        except NotPreviewable:
            raise
        except Exception as e:  # noqa: BLE001
            raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e

    def _transform_spill(self, node, parent, fn, mode, on_error, fmt="rows") -> Relation:
        """Full-run transform: stream output batches to a temp Parquet (bounded memory, out-of-core)."""
        import os
        import pyarrow.parquet as pq
        spill_dir = os.path.join(_spill_root(), "transform")
        os.makedirs(spill_dir, exist_ok=True)
        path = os.path.join(spill_dir, f"{db.unique_view('xf')}.parquet")
        writer: "pq.ParquetWriter | None" = None
        buf: list[dict] = []
        FLUSH = 50_000

        def write_tbl(tbl: "pa.Table") -> None:
            nonlocal writer
            if tbl.num_rows == 0 and writer is not None:
                return
            if writer is None:
                writer = pq.ParquetWriter(path, tbl.schema)
            else:
                try:
                    tbl = tbl.cast(writer.schema)
                except Exception:  # noqa: BLE001 — schema drift across batches; keep going best-effort
                    tbl = tbl.cast(writer.schema, safe=False)
            writer.write_table(tbl)

        def flush():
            nonlocal buf
            if buf:
                write_tbl(pa.Table.from_pylist(buf))
                buf = []

        try:
            if fmt in ("pandas", "arrow"):  # arrow-native: type-preserving, one table per input batch
                for batch in parent.to_arrow_reader(batch_size=8192):
                    t = _apply_batch(fn, pa.Table.from_batches([batch]), fmt, on_error, node)
                    if t is not None:  # on_error='skip' dropped this batch
                        write_tbl(t)
            else:
                for batch in parent.to_arrow_reader(batch_size=8192):
                    buf.extend(_apply_fn(fn, batch, mode, on_error, node))
                    if len(buf) >= FLUSH:
                        flush()
                flush()
        except BaseException:
            # close + delete the partial spill so we never leak a handle or a truncated file
            if writer is not None:
                try:
                    writer.close()
                finally:
                    writer = None
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        if writer is None:
            return parent.limit(0)
        writer.close()
        self.spill_files.append(path)  # GC'd at end-of-run by the runner
        return db.conn().read_parquet(path)

    # -- vector search (Lance / brute-force cosine) ------------------------ #
    def _vector_search(self, node: GraphNode, inputs: list[Relation]) -> Relation:
        cfg = _cfg(node)
        col = cfg.get("column", "embedding")
        k = int(cfg.get("k", 10))
        if not inputs:
            raise NotPreviewable(node, "vector-search needs a dataset input")
        # the true nearest-K are over ALL rows (and the query row itself must come from the full set),
        # not a 2000-row prefix — score the full input in preview too
        src = inputs[0] if self.full else self._faithful_inputs(node)[0]
        base = self._view(src, "vs")
        con = db.conn()
        # query = an explicit external vector (e.g. a text embedding), else a chosen row's vector.
        # the UI/config may carry it as a JSON string "[...]" or as a real list.
        qv = cfg.get("queryVector")
        if isinstance(qv, str) and qv.strip():
            try:
                qv = json.loads(qv)
            except ValueError:
                qv = None
        if isinstance(qv, (list, tuple)) and qv:
            query = [float(x) for x in qv]
        else:
            qrow = max(0, int(cfg.get("queryRow", 0)))
            try:
                q = con.sql(f'SELECT "{col}" AS q FROM {base} OFFSET {qrow} LIMIT 1').fetchone()
            except Exception as e:  # noqa: BLE001
                raise NotPreviewable(node, f"no vector column '{col}': {e}") from e
            if not q or q[0] is None:
                raise NotPreviewable(node, f"no vector in column '{col}'")
            query = [float(x) for x in q[0]]
        # native ANN when the input is a bare Lance source (uses its vector index if present), else a
        # brute-force cosine scan out-of-core in DuckDB
        luri = self._bare_lance_source(node)
        if luri is not None:
            try:
                return self.resolve_adapter(luri).nearest(luri, col, query, k)
            except Exception:  # noqa: BLE001 — no index / older lance / unsupported → brute force
                pass
        qlit = "[" + ", ".join(str(x) for x in query) + "]::DOUBLE[]"
        return con.sql(
            f'SELECT *, list_cosine_similarity("{col}", {qlit}) AS _score '
            f'FROM {base} ORDER BY _score DESC LIMIT {k}'
        )

    def _bare_lance_source(self, node: GraphNode) -> str | None:
        """The .lance uri if this node's single input is a bare Lance source (no ops between) — so
        vector-search can use Lance's native nearest search instead of a full brute-force cosine scan."""
        inc = g.incoming(self.graph, node.id)
        if len(inc) != 1:
            return None
        src = g.node_map(self.graph).get(inc[0].source)
        if src is None or src.type != "source":
            return None
        uri = (src.data.get("config", {}) if isinstance(src.data, dict) else {}).get("uri", "")
        return uri if str(uri).lower().rstrip("/").endswith(".lance") else None


def _apply_fn(fn, batch: "pa.RecordBatch", mode: str, on_error: str, node) -> list[dict]:
    rows = batch.to_pylist()
    out: list[dict] = []
    if mode == "map_batches":
        try:
            return list(fn(rows))
        except Exception as e:  # noqa: BLE001
            if on_error == "skip":
                return []  # drop the failed batch (returning the untransformed input would lie)
            raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e
    for r in rows:
        try:
            if mode == "map":
                out.append(fn(dict(r)))
            elif mode == "filter":
                if fn(dict(r)):
                    out.append(r)
            elif mode in ("flat_map", "flat_map_generator"):
                out.extend(list(fn(dict(r))))
        except Exception as e:  # noqa: BLE001
            if on_error == "skip":
                continue
            raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e
    return out


def _apply_batch(fn, table: "pa.Table", fmt: str, on_error: str, node) -> "pa.Table | None":
    """Run a `map_batches` UDF over a whole batch in the chosen representation — `pandas` (a DataFrame) or
    `arrow` (a pyarrow.Table) — arrow-NATIVE so column types survive (no dict round-trip). Returns the
    output Table, or None when on_error='skip' swallowed a failure (the caller DROPS it — we can't emit a
    correct-schema empty here, and the input schema would clash with the output schema of good batches).
    (The default `rows` format goes through _apply_fn.) pandas must be declared in the canvas requirements;
    pyarrow is always present."""
    try:
        if fmt == "arrow":
            res = fn(table)
            if isinstance(res, pa.Table):
                return res
            if isinstance(res, pa.RecordBatch):
                return pa.Table.from_batches([res])
            raise TypeError(f"an arrow batch UDF must return a pyarrow.Table, got {type(res).__name__}")
        import pandas as pd  # noqa: F401 — required only when the user picks the pandas format
        res = fn(table.to_pandas())
        if not isinstance(res, pd.DataFrame):
            raise TypeError(f"a pandas batch UDF must return a DataFrame, got {type(res).__name__}")
        return pa.Table.from_pandas(res, preserve_index=False)
    except NotPreviewable:
        raise
    except Exception as e:  # noqa: BLE001
        if on_error == "skip":
            return None  # drop the failed batch entirely — a good batch defines the output schema
        raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e


def _table_to_rows(tbl: "pa.Table") -> list[dict]:
    import decimal
    rows = tbl.to_pylist()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, decimal.Decimal):
                r[k] = float(v)  # schema says float; don't ship a Decimal (serializes as string)
            elif isinstance(v, (bytes, bytearray)):
                r[k] = f"<{len(v)} bytes>"
            elif hasattr(v, "isoformat"):
                r[k] = v.isoformat()
    return rows


def _dedupe_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


_AGG_ALLOWED = {"count", "sum", "mean", "avg", "min", "max", "median", "stddev"}


def _agg_name(agg: str) -> str:
    a = (agg or "count").lower()
    if a not in _AGG_ALLOWED:
        raise ValueError(f"unsupported aggregate '{agg}' (allowed: {', '.join(sorted(_AGG_ALLOWED))})")
    return {"mean": "avg"}.get(a, a)


def _ident(col: str) -> str:
    return str(col).replace('"', '')  # strip quotes; the caller wraps in "..."


def _sql_str(s: str) -> str:
    return str(s).replace("'", "''")  # escape single quotes for a SQL string literal


def _empty() -> Relation:
    return db.conn().sql("SELECT 1 WHERE 1=0")


def _spill_root() -> str:
    import os
    import tempfile
    return os.environ.get("DP_SPILL_DIR", os.path.join(tempfile.gettempdir(), "dataplay-spill"))


def node_previewable(node: GraphNode, registry=None, node_specs=None) -> bool:
    if node.type in NOT_PREVIEWABLE_KINDS:
        return False
    if node.type == "transform":
        cfg = _cfg(node)
        if cfg.get("source") == "library" and registry is not None:
            pid = cfg.get("processor")
            if pid and registry.has(pid):
                return registry.get(pid).mode in PREVIEWABLE_MODES
        return cfg.get("mode", "map") in PREVIEWABLE_MODES
    # plugin kinds: honor the declared spec.previewable (§8.1)
    if node_specs and node.type in node_specs:
        return bool(getattr(node_specs[node.type], "previewable", True))
    return True
