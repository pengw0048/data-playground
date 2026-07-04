"""The lowering engine (PRD §6/§7) — a node lowers to a step in a typed logical plan.

The `dataset` wire is a lazy DuckDB relation; relational ops (filter/select/join/aggregate/
sort/dedup/sql/sample) lower to relation transforms that DuckDB executes out-of-core (streaming,
spilling to disk). The `transform` node is the escape hatch: arbitrary Python over Arrow
`RecordBatch`es. The SAME relation is executed on a bounded sample (preview) or in full (run),
so what you see on the sample is faithful — except nodes flagged not-previewable (P8).
"""

from __future__ import annotations

from typing import Any

import duckdb
import pyarrow as pa

from kernel import db, graph as g, sandbox
from kernel.models import PREVIEWABLE_MODES, ColumnSchema, Graph, GraphNode
from kernel.plugins.adapters import display_type, relation_columns
from kernel.plugins.capabilities import tag_columns

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


class LoweringEngine:
    def __init__(self, graph: Graph, resolve_adapter, registry, sample_k: int | None = None,
                 full: bool = False, node_lowerings: dict | None = None, node_specs: dict | None = None,
                 bound_inputs: dict | None = None):
        self.graph = graph
        self.resolve_adapter = resolve_adapter
        self.registry = registry
        self.sample_k = sample_k
        self.full = full
        self.node_lowerings = node_lowerings or {}
        self.node_specs = node_specs or {}
        # a node id -> Relation to inject as that node's input (used to run a section's sub-node
        # against a script-provided handle instead of a wired upstream edge)
        self.bound_inputs = bound_inputs or {}
        self._cache: dict[str, Relation] = {}

    # -- public ------------------------------------------------------------ #
    def relation(self, node_id: str) -> Relation:
        if node_id in self._cache:
            return self._cache[node_id]
        node = g.node_map(self.graph)[node_id]
        rel = self._lower(node)
        self._cache[node_id] = rel
        return rel

    def rows(self, node_id: str, k: int) -> tuple[list[dict], list[ColumnSchema]]:
        tbl = self.relation(node_id).limit(k).to_arrow_table()
        # a join over columns that share a name (e.g. both inputs have `id`) yields duplicate
        # column names; de-dup so no column is silently dropped when rows become dicts.
        names = _dedupe_names(tbl.column_names)
        if names != tbl.column_names:
            tbl = tbl.rename_columns(names)
        cols = tag_columns([ColumnSchema(name=n, type=display_type(str(t)))
                            for n, t in zip(tbl.column_names, tbl.schema.types)])
        return _table_to_rows(tbl), cols

    # -- inputs (honors branch routing) ------------------------------------ #
    def _inputs(self, node: GraphNode) -> list[Relation]:
        if node.id in self.bound_inputs:  # section sub-node: input injected by the driver script
            return [self.bound_inputs[node.id]]
        out: list[Relation] = []
        for e in g.incoming(self.graph, node.id):
            rel = self.relation(e.source)
            parent = g.node_map(self.graph)[e.source]
            if parent.type == "branch":
                rel = self._route_branch(parent, rel, e.source_handle)
            out.append(rel)
        return out

    def _view(self, rel: Relation, base: str = "v") -> str:
        # process-globally-unique name so concurrent engines never clobber each other's views
        name = db.unique_view(base)
        rel.create_view(name, replace=True)
        return name

    # -- lowering ---------------------------------------------------------- #
    def _lower(self, node: GraphNode) -> Relation:  # noqa: C901
        t = node.type
        cfg = _cfg(node)

        if t == "source":
            uri = cfg.get("uri") or cfg.get("table")
            if not uri:
                raise NotPreviewable(node, "no dataset selected")
            rel = self.resolve_adapter(uri).scan(uri)
            if self.sample_k and not self.full:
                rel = rel.limit(self.sample_k)
            return rel

        inputs = self._inputs(node)
        # plugin-provided node kinds (§8.1) — dispatch BEFORE the no-inputs guard so a plugin
        # can define a 0-input source/generator. Honor the plugin's declared previewable.
        if t in self.node_lowerings:
            if not self.full and not self._spec_previewable(t):
                raise NotPreviewable(node, f"'{t}' is not sample-previewable — needs a full pass")
            return self.node_lowerings[t](self, node, inputs)

        if t == "section":  # composite node implemented by a driver script over contained nodes
            if not self.full:  # runs real work over its nodes — not faithful on a sample (P8)
                raise NotPreviewable(node, "a section runs real work over its nodes — needs a full pass")
            from kernel.section import run_section
            return run_section(self, node, inputs)

        if not inputs and t not in ("source",):
            raise NotPreviewable(node, "not connected to a source")
        parent = inputs[0] if inputs else None

        if _bypassed(node) and parent is not None:
            return parent

        if t == "sample":
            n = max(0, int(cfg.get("n", self.sample_k or 1000)))
            seed = int(cfg.get("seed", 42))
            v = self._view(parent, "s")
            return db.conn().sql(f"SELECT * FROM {v} USING SAMPLE {n} ROWS (reservoir, {seed})")

        if t == "filter":
            pred = (cfg.get("predicate") or "").strip()
            return parent.filter(pred) if pred else parent

        if t == "select":
            expr = (cfg.get("select") or cfg.get("expr") or "").strip()
            return parent.project(expr) if expr else parent

        if t == "sort":
            by = (cfg.get("by") or "").strip()
            return parent.order(by) if by else parent

        if t == "dedup":
            on = (cfg.get("on") or "").strip()
            if on:
                v = self._view(parent, "d")
                cols = ", ".join(f'"{c.strip()}"' for c in on.split(","))
                return db.conn().sql(f"SELECT DISTINCT ON ({cols}) * FROM {v}")
            return parent.distinct()

        if t == "aggregate":
            if not self.full:
                raise NotPreviewable(node, "global aggregate — needs a full pass (a sample would lie)")
            aggs = (cfg.get("aggs") or "count(*) AS n").strip()
            group = (cfg.get("groupBy") or cfg.get("group") or "").strip()
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
            how = (cfg.get("how") or "inner").lower()
            how = how if how in ("inner", "left", "right", "full", "outer", "cross") else "inner"
            how = "full" if how == "outer" else how
            a, b = self._view(inputs[0], "ja"), self._view(inputs[1], "jb")
            if how == "cross" or not on:
                return db.conn().sql(f"SELECT * FROM {a} CROSS JOIN {b}")
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
                pids = g.parents(self.graph, node.id)
                if pids:
                    full = LoweringEngine(self.graph, self.resolve_adapter, self.registry,
                                          sample_k=None, full=True, node_lowerings=self.node_lowerings,
                                          node_specs=self.node_specs)
                    base = full.relation(pids[0])
            expr = "count(*)" if agg == "count" or not col else f'{_agg_name(agg)}("{_ident(col)}")'
            v = self._view(base, "m")
            title = (node.data.get("title") if isinstance(node.data, dict) else None) or "metric"
            return db.conn().sql(f"SELECT '{_sql_str(title)}' AS metric, ({expr})::DOUBLE AS value FROM {v}")

        if t == "branch":
            return parent  # router; routing applied on outgoing edges

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

    # -- branch routing ---------------------------------------------------- #
    def _route_branch(self, node: GraphNode, rel: Relation, handle: str | None) -> Relation:
        pred = (_cfg(node).get("predicate") or "").strip()
        want_true = (handle or "true") != "false"
        if not pred:
            return rel if want_true else rel.filter("1=0")
        return rel.filter(pred if want_true else f"NOT ({pred})")

    # -- transform escape hatch (Python over Arrow batches) ---------------- #
    def _transform(self, node: GraphNode, parent: Relation) -> Relation:
        cfg = _cfg(node)
        if node.type == "transform" and cfg.get("source") == "library":
            pid = cfg.get("processor")
            if not (pid and self.registry.has(pid)):
                raise NotPreviewable(node, f"processor '{pid}' is not registered")
            proc = self.registry.get(pid)
            fn, mode = proc.build(cfg.get("params", {})), proc.mode
        else:
            code = cfg.get("code")
            mode = cfg.get("mode", "map")
            if not code:
                return parent
            fn = sandbox.compile_operator(code, mode)

        if mode not in PREVIEWABLE_MODES:
            raise NotPreviewable(node, f"transform mode '{mode}' needs a full pass")

        on_error = cfg.get("onError", "raise")
        try:
            if self.full:
                return self._transform_spill(node, parent, fn, mode, on_error)
            # preview: input is bounded (source sampled), so in-memory is fine and fast
            out: list[dict] = []
            for batch in parent.to_arrow_reader(batch_size=2048):
                out.extend(_apply_fn(fn, batch, mode, on_error, node))
            table = pa.Table.from_pylist(out) if out else parent.limit(0).to_arrow_table()
            return db.conn().from_arrow(table)
        except NotPreviewable:
            raise
        except Exception as e:  # noqa: BLE001
            raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e

    def _transform_spill(self, node, parent, fn, mode, on_error) -> Relation:
        """Full-run transform: stream output batches to a temp Parquet (bounded memory, out-of-core)."""
        import os
        import pyarrow.parquet as pq
        spill_dir = os.path.join(_spill_root(), "transform")
        os.makedirs(spill_dir, exist_ok=True)
        path = os.path.join(spill_dir, f"{db.unique_view('xf')}.parquet")
        writer: "pq.ParquetWriter | None" = None
        buf: list[dict] = []
        FLUSH = 50_000

        def flush():
            nonlocal writer, buf
            if not buf:
                return
            tbl = pa.Table.from_pylist(buf)
            buf = []
            if writer is None:
                writer = pq.ParquetWriter(path, tbl.schema)
            else:
                try:
                    tbl = tbl.cast(writer.schema)
                except Exception:  # noqa: BLE001 — schema drift across batches; keep going best-effort
                    tbl = tbl.cast(writer.schema, safe=False)
            writer.write_table(tbl)

        try:
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
        return db.conn().read_parquet(path)

    # -- vector search (Lance / brute-force cosine) ------------------------ #
    def _vector_search(self, node: GraphNode, inputs: list[Relation]) -> Relation:
        cfg = _cfg(node)
        col = cfg.get("column", "embedding")
        k = int(cfg.get("k", 10))
        if not inputs:
            raise NotPreviewable(node, "vector-search needs a dataset input")
        base = self._view(inputs[0], "vs")
        # brute-force cosine similarity to the first row's vector (query), out-of-core in DuckDB
        con = db.conn()
        try:
            q = con.sql(f'SELECT "{col}" AS q FROM {base} LIMIT 1').fetchone()
        except Exception as e:  # noqa: BLE001
            raise NotPreviewable(node, f"no vector column '{col}': {e}") from e
        if not q or q[0] is None:
            raise NotPreviewable(node, f"no vector in column '{col}'")
        qlit = "[" + ", ".join(str(float(x)) for x in q[0]) + "]::DOUBLE[]"
        return con.sql(
            f'SELECT *, list_cosine_similarity("{col}", {qlit}) AS _score '
            f'FROM {base} ORDER BY _score DESC LIMIT {k}'
        )


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
