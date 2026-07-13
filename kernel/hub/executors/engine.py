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
# The faithful-preview SQL gates parse with DuckDB's OWN parser (hub.sqlanalyze) rather than regex, so
# detection matches execution exactly — handling quoting / string literals / a column named `input2` /
# nested subqueries a regex cannot. Re-exported so callers + tests keep the `engine.sql_*` names.
from hub.sqlanalyze import needs_full_input as sql_needs_full_input
from hub.sqlanalyze import reduces_rows as sql_reduces_rows
from hub.sqlpolicy import (
    FragmentKind,
    SQLPolicyError,
    bind_input_ctes,
    identifier,
    identifier_key,
    identifier_list,
    quote_identifier,
    unique_identifier_names,
    validate_fragment,
    validate_identifier_alias,
    validate_identifier_schema,
    validate_query,
)

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

# --- schema-contract type model -------------------------------------------------------------------- #
# A column type is parsed to a canonical tuple so a declared contract can be compared to an actual
# DuckDB type FAITHFULLY — preserving what matters (decimal precision/scale, timestamp unit/tz, and the
# element types of list/struct/map) rather than collapsing everything to a coarse bucket. canonical_type
# accepts BOTH the user dialect (`list<int>`, `struct<a:int>`, `decimal(38,9)`, `timestamp[ns]`) and the
# DuckDB dialect (`INTEGER[]`, `STRUCT(a INTEGER)`, `DECIMAL(38,9)`, `TIMESTAMP_NS`).
_INT_NAMES = {"int", "integer", "bigint", "long", "smallint", "tinyint", "hugeint", "ubigint",
              "uinteger", "usmallint", "utinyint", "uint", "int2", "int4", "int8", "int16", "int32",
              "int64", "uint8", "uint16", "uint32", "uint64"}
_FLOAT_NAMES = {"float", "double", "real", "number", "float4", "float8", "double precision"}
_STR_NAMES = {"string", "str", "text", "varchar", "char", "bpchar", "utf8", "uuid"}
_BYTES_NAMES = {"blob", "bytes", "binary", "varbinary", "bytea"}


def _split_top(s: str) -> list[str]:
    """Split `s` on top-level commas, respecting <>, (), [] nesting (so a struct/map's inner commas
    don't split the field list)."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _split_field(f: str) -> tuple[str, str]:
    """A struct field spec → (name, type_str). Accepts `name:type` (user dialect) and `name type`
    (DuckDB), including a double-quoted name that may contain spaces."""
    f = f.strip()
    if f.startswith('"'):
        end = f.find('"', 1)
        if end > 0:
            rest = f[end + 1:].strip()
            return f[1:end], (rest[1:].strip() if rest.startswith(":") else rest)
    if ":" in f and " " not in f.split(":", 1)[0].strip():
        name, _, ftype = f.partition(":")
        return name.strip(), ftype.strip()
    parts = f.split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (f, "")


def canonical_type(t: object) -> tuple:
    """Parse a column type string (either dialect) into a canonical, comparable tuple. Unspecified detail
    is None (a bare `list`/`struct`/`map`/`timestamp` from a coarse/inferred contract stays lenient). A
    genuinely EMPTY type gets the dedicated `("any",)` sentinel (a name-only contract → wildcard) — kept
    distinct from `("other","")`, which an unrecognized but NON-empty type like `<i8` produces and which
    must stay STRICT (a precise-but-unparsed contract, not a wildcard)."""
    s = str(t or "").strip()
    if not s:
        return ("any",)
    if s.endswith("[]"):
        return ("list", canonical_type(s[:-2]))
    low = s.lower()
    idxs = [i for i in (low.find("<"), low.find("(")) if i >= 0]
    lb = min(idxs) if idxs else -1
    head = (low[:lb] if lb >= 0 else low).strip()
    inner = s[lb + 1:-1].strip() if (lb >= 0 and s[-1:] in ">)") else ""
    if head in ("list", "array"):
        return ("list", canonical_type(inner) if inner else None)
    if head == "map":
        parts = _split_top(inner) if inner else []
        return ("map", canonical_type(parts[0]), canonical_type(parts[1])) if len(parts) == 2 else ("map", None, None)
    if head in ("struct", "row"):
        if not inner:
            return ("struct", None)
        return ("struct", tuple((n, canonical_type(ft)) for n, ft in (_split_field(f) for f in _split_top(inner))))
    if head in ("decimal", "numeric"):
        args = _split_top(inner) if inner else []
        if args and args[0]:
            try:
                return ("decimal", int(args[0]), int(args[1]) if len(args) > 1 else 0)
            except ValueError:
                pass
        return ("float",)  # bare decimal → numeric coarse (matches double/decimal), no precision asserted
    if head.startswith("timestamp") or head == "datetime":
        m = re.search(r"[\[_( ]\s*(ns|us|ms|s)\b", low)
        unit = m.group(1) if m else None
        if unit == "us":
            unit = None  # microsecond is DuckDB's unmarked default → coarse, so `TIMESTAMP` == `timestamp[us]`
        tz = True if ("time zone" in low or low.endswith("tz")) else None
        return ("timestamp", unit, tz)
    if head in _INT_NAMES:
        return ("int",)
    if head in _FLOAT_NAMES:
        return ("float",)
    if head in _STR_NAMES:
        return ("string",)
    if head in _BYTES_NAMES:
        return ("bytes",)
    if head in ("bool", "boolean"):
        return ("bool",)
    if head == "date":
        return ("date",)
    if head == "time":
        return ("time",)
    if head == "json":
        return ("json",)
    return ("other", head)


def type_satisfies(want: tuple, actual: tuple) -> bool:
    """Does `actual` satisfy the `want` contract type? The contract's SPECIFICITY sets the strictness: a
    coarse want (bare `list`, `float`, `timestamp`) accepts any refinement, so a display-coarse/inferred
    contract stays lenient; a precise want (`decimal(38,9)`, `list<int>`, `timestamp[ns]`) is enforced
    exactly. This is what makes a hand-written precise contract faithful without breaking inferred ones."""
    if want == actual:
        return True
    if want == ("any",):  # a TYPELESS contract column (no type given) asserts NAME-ONLY presence — the
        return True       # coarsest want, accepts any actual. (An unrecognized non-empty type is ("other",
                          # head) and stays STRICT, so `<i8` never silently becomes a wildcard.)
    wk = want[0]
    if wk == "float":  # numeric coarse: a float contract also accepts a decimal actual
        return actual[0] in ("float", "decimal")
    if wk == "decimal":
        return (actual[0] == "decimal" and (want[1] is None or want[1] == actual[1])
                and (want[2] is None or want[2] == actual[2]))
    if wk == "timestamp":
        return (actual[0] == "timestamp" and (want[1] is None or want[1] == actual[1])
                and (want[2] is None or want[2] == actual[2]))
    if wk == "list":
        return actual[0] == "list" and (want[1] is None
                                         or (actual[1] is not None and type_satisfies(want[1], actual[1])))
    if wk == "map":
        if actual[0] != "map" or want[1] is None:
            return actual[0] == "map"
        return (actual[1] is not None and type_satisfies(want[1], actual[1])
                and type_satisfies(want[2], actual[2]))
    if wk == "struct":
        if actual[0] != "struct":
            return False
        if want[1] is None:
            return True
        if actual[1] is None:
            return False
        af = dict(actual[1])
        return all(nm in af and type_satisfies(wt, af[nm]) for nm, wt in want[1])
    return want == actual


def _canon_to_duck(c: tuple) -> str:
    """A canonical type → a DuckDB type string for the schema-only stand-in relation."""
    k = c[0]
    simple = {"int": "BIGINT", "float": "DOUBLE", "string": "VARCHAR", "bool": "BOOLEAN",
              "date": "DATE", "time": "TIME", "bytes": "BLOB", "json": "JSON"}
    if k in simple:
        return simple[k]
    if k == "decimal":
        return f"DECIMAL({c[1] if c[1] is not None else 18},{c[2] if c[2] is not None else 3})"
    if k == "timestamp":
        base = {"ns": "TIMESTAMP_NS", "ms": "TIMESTAMP_MS", "s": "TIMESTAMP_S"}.get(c[1], "TIMESTAMP")
        return base + (" WITH TIME ZONE" if c[2] else "")
    if k == "list":
        return f"{_canon_to_duck(c[1])}[]" if c[1] is not None else "VARCHAR[]"
    if k == "map":
        return "VARCHAR" if c[1] is None else f"MAP({_canon_to_duck(c[1])}, {_canon_to_duck(c[2])})"
    if k == "struct":
        if c[1] is None:
            return "VARCHAR"
        return "STRUCT(" + ", ".join(f'"{n}" {_canon_to_duck(t)}' for n, t in c[1]) + ")"
    return _DUCK_TYPE.get(c[1] if len(c) > 1 else "", "VARCHAR")


def declared_schema(node: GraphNode) -> list | None:
    """A user-declared output-schema contract (config.outputSchema) on a code op, or None. Lets a
    transform/plugin node carry a typed port + propagate types downstream without ever being run.
    Either an inline column list, OR {"ref": name[, "version": v]} referencing a named workspace contract
    (so many pipelines share ONE contract) — the ref resolves to that contract's columns."""
    sch = _cfg(node).get("outputSchema")
    if isinstance(sch, dict) and sch.get("ref"):
        from hub import metadb
        c = metadb.get_schema_contract(str(sch["ref"]), sch.get("version"))
        return c["columns"] if c and c.get("columns") else None
    return sch if isinstance(sch, list) and sch else None


def _duck_type(t: object) -> str:
    """Map a declared/display column type to a DuckDB type for a schema-only stand-in relation, PRESERVING
    the detail that matters (decimal precision/scale, timestamp unit/tz, list/struct/map element types)
    so a declared contract propagates faithfully to downstream ports. Unknown → VARCHAR."""
    return _canon_to_duck(canonical_type(t))


def normalize_how(how: str) -> str:
    """A join's `how` → the canonical DuckDB keyword (outer→full; unknown→inner)."""
    h = (how or "inner").lower()
    h = h if h in ("inner", "left", "right", "full", "outer", "cross") else "inner"
    return "full" if h == "outer" else h


def join_projection(lcols: list, rcols: list, using_keys=()) -> str:
    """Build a deterministic, unambiguous SELECT list for a join."""
    lcols = validate_identifier_schema(lcols, label="left join input schema")
    rcols = validate_identifier_schema(rcols, label="right join input schema")
    keys = [str(k) for k in using_keys]
    key_names = [identifier(key, lcols, label="join key") for key in keys]
    for key in keys:
        identifier(key, rcols, label="join key")
    key_folds = {identifier_key(key) for key in key_names}

    parts = [
        quote_identifier(column)
        if identifier_key(column) in key_folds else f"a.{quote_identifier(column)}"
        for column in lcols
    ]
    right_columns = [column for column in rcols if identifier_key(column) not in key_folds]
    right_aliases = unique_identifier_names(right_columns, used=lcols)
    for column, alias in zip(right_columns, right_aliases, strict=True):
        expression = f"b.{quote_identifier(column)}"
        if alias != column:
            expression += f" AS {quote_identifier(alias)}"
        parts.append(expression)
    return ", ".join(parts)


def join_sql(lcols: list, rcols: list, a: str, b: str, on: str, condition: str, how: str,
             *, con=None) -> str:
    """The DuckDB join SQL over two views `a`,`b` — the SINGLE source of truth for join semantics + output
    naming, so the single-node engine and the distributed backend (dp_ray) never diverge. `on` = a
    comma-separated USING key list; `condition` = a raw ON expression (a.x = b.y); else a CROSS join."""
    on, cond, how = (on or "").strip(), (condition or "").strip(), normalize_how(how)
    qa, qb = quote_identifier(a), quote_identifier(b)
    if how == "cross" or (not on and not cond):
        return f"SELECT {join_projection(lcols, rcols)} FROM {qa} AS a CROSS JOIN {qb} AS b"
    if cond:
        validate_fragment(FragmentKind.JOIN_ON, cond, con=con)
        return f"SELECT {join_projection(lcols, rcols)} FROM {qa} AS a {how.upper()} JOIN {qb} AS b ON ({cond})"
    keylist = identifier_list(on, lcols, label="join key")
    for key in keylist:
        identifier(key, rcols, label="join key")
    cols = ", ".join(quote_identifier(c) for c in keylist)
    return (f"SELECT {join_projection(lcols, rcols, using_keys=keylist)} "
            f"FROM {qa} AS a {how.upper()} JOIN {qb} AS b USING ({cols})")


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
            predicate = (ccfg.get("predicate") or "").strip()
            if predicate:
                validate_fragment(FragmentKind.PREDICATE, predicate, con=db.conn())
            return None, (predicate or None)
        if consumer.type == "select":
            projection = (ccfg.get("select") or ccfg.get("expr") or "").strip()
            if projection:
                validate_fragment(FragmentKind.PROJECTION, projection, con=db.conn())
            return _plain_columns(projection), None
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
            # a reservoir sample must draw from the FULL input — over the source-capped 2000-row preview
            # prefix it would just be a sample of the first 2000 rows, not a real sample of the dataset.
            # Build the input unsampled (like join/sort/window); the reservoir size bounds the work.
            src = parent if self.full else self._faithful_inputs(node)[0]
            v = self._view(src, "s")
            return db.conn().sql(f"SELECT * FROM {v} USING SAMPLE {n} ROWS (reservoir, {seed})")

        if t == "filter":
            pred = (cfg.get("predicate") or "").strip()
            if pred:
                validate_fragment(FragmentKind.PREDICATE, pred, con=db.conn())
            return parent.filter(pred) if pred else parent

        if t == "assert":
            # a data-quality gate with TWO outputs (P0-DATA-01): 'pass' forwards EVERY input row so the
            # assert sits INLINE without corrupting the data (wire it to the next node / write), while the
            # default 'out' port is the VIOLATING rows (so preview / "view data" shows exactly what failed).
            # `IS NOT TRUE` catches both false AND null, so `x > 0` flags a null x too. The runner fails the
            # run on error-severity violations (plugins/runner.py) BEFORE any downstream write commits.
            pred = (cfg.get("predicate") or "").strip()
            v = self._view(parent, "as")
            # no predicate → ZERO violations (`WHERE false`, not `return parent`: this port is the VIOLATING
            # rows, so passing the input through would count every row as a violation). WHERE keeps the schema.
            if pred:
                validate_fragment(FragmentKind.PREDICATE, pred, con=db.conn())
            violations = db.conn().sql(f"SELECT * FROM {v} WHERE {f'({pred}) IS NOT TRUE' if pred else 'false'}")
            return {"out": violations, "pass": parent}

        if t == "select":
            expr = (cfg.get("expr") or "").strip()  # resolver canonicalizes select/expr → 'expr'
            if expr:
                validate_fragment(FragmentKind.PROJECTION, expr, con=db.conn())
            return parent.project(expr) if expr else parent

        if t == "sort":
            by = (cfg.get("by") or "").strip()
            if not by:
                return parent
            validate_fragment(FragmentKind.ORDER_BY, by, con=db.conn())
            # the true top-N is over ALL rows, not a 2000-row prefix — sort the full input in preview
            # too (the preview limit turns it into an efficient top-N)
            src = parent if self.full else self._faithful_inputs(node)[0]
            return src.order(by)

        if t == "dedup":
            on = (cfg.get("on") or "").strip()
            if on:
                v = self._view(parent, "d")
                cols = ", ".join(
                    quote_identifier(c) for c in identifier_list(on, parent.columns, label="dedup column")
                )
                return db.conn().sql(f"SELECT DISTINCT ON ({cols}) * FROM {quote_identifier(v)}")
            return parent.distinct()

        if t == "window":
            expr = (cfg.get("expr") or "").strip()
            if not expr:
                return parent
            part = (cfg.get("partitionBy") or "").strip()
            order = (cfg.get("orderBy") or "").strip()
            validate_fragment(FragmentKind.WINDOW_EXPR, expr, con=db.conn())
            if part:
                validate_fragment(FragmentKind.GROUP_BY, part, con=db.conn())
            if order:
                validate_fragment(FragmentKind.ORDER_BY, order, con=db.conn())
            over = " ".join(x for x in [f"PARTITION BY {part}" if part else "",
                                        f"ORDER BY {order}" if order else ""] if x)
            col = validate_identifier_alias(
                (cfg.get("as") or "").strip() or "window", label="window output column"
            )
            # a window fn ranks/aggregates ACROSS rows, so a sample would lie (rank within the sample, a
            # partial SUM) — compute over the full input in preview too, like sort (the preview LIMIT then
            # just truncates the display).
            src = parent if self.full else self._faithful_inputs(node)[0]
            v = self._view(src, "w")
            return db.conn().sql(
                f"SELECT *, {expr} OVER ({over}) AS {quote_identifier(col)} FROM {quote_identifier(v)}"
            )

        if t == "fill":
            cols = identifier_list(cfg.get("columns") or "", parent.columns, label="fill column")
            if not cols:
                return parent
            method = (cfg.get("method") or "constant").strip()
            value = (cfg.get("value") or "").strip()
            if method == "constant" and value:
                validate_fragment(FragmentKind.LITERAL, value, con=db.conn())

            def _fill(c: str) -> str:
                q = quote_identifier(c)
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
            repl = ", ".join(f"{_fill(c)} AS {quote_identifier(c)}" for c in cols)
            return db.conn().sql(f"SELECT * REPLACE ({repl}) FROM {quote_identifier(v)}")

        if t == "unnest":
            col = (cfg.get("column") or "").strip()
            if not col:
                return parent
            parsed = identifier_list(col, parent.columns, label="unnest column")
            if len(parsed) != 1:
                raise SQLPolicyError("unnest column must name exactly one input column")
            col = parsed[0]
            v = self._view(parent, "un")  # explode a list column → one row per element, others repeated
            cq = quote_identifier(col)
            return db.conn().sql(
                f"SELECT * EXCLUDE ({cq}), system.main.unnest({cq}) AS {cq} FROM {quote_identifier(v)}"
            )

        if t == "unpivot":
            cols = identifier_list(cfg.get("columns") or "", parent.columns, label="unpivot column")
            if not cols:
                return parent  # nothing chosen to fold → pass through
            name_col = validate_identifier_alias(
                (cfg.get("nameColumn") or "name").strip() or "name", label="unpivot name column"
            )
            value_col = validate_identifier_alias(
                (cfg.get("valueColumn") or "value").strip() or "value", label="unpivot value column"
            )
            # keep NULL cells by DEFAULT so wide→long loses NO rows — DuckDB's bare UNPIVOT drops them
            # (a row whose folded columns are all NULL would silently vanish). Opt out with includeNulls=false.
            kn = cfg.get("includeNulls", True)
            keep_nulls = kn if isinstance(kn, bool) else str(kn).strip().lower() not in ("false", "0", "no", "off", "")
            nulls = "INCLUDE NULLS" if keep_nulls else "EXCLUDE NULLS"
            v = self._view(parent, "up")  # wide → long: each chosen column becomes (name, value) rows
            on = ", ".join(quote_identifier(c) for c in cols)  # the SQL-standard FROM-form takes the NULLS mode
            return db.conn().sql(
                f"SELECT * FROM {quote_identifier(v)} UNPIVOT {nulls} "
                f"({quote_identifier(value_col)} FOR {quote_identifier(name_col)} IN ({on}))"
            )

        if t == "pivot":
            on_col = (cfg.get("pivotOn") or cfg.get("on") or "").strip()
            using = (cfg.get("using") or "").strip()
            if not on_col:
                raise NotPreviewable(node, "pivot needs a column to pivot on")
            if not self.full:  # PIVOT's output columns are the DISTINCT values of on_col → a sample would
                raise NotPreviewable(node, "pivot reshapes rows into data-dependent columns — needs a full pass")
            on_col = identifier(on_col, parent.columns, label="pivot column")
            group = identifier_list(cfg.get("groupBy") or "", parent.columns, label="pivot group column")
            using = using or "count(*)"
            validate_fragment(FragmentKind.AGGREGATES, using, con=db.conn())
            v = self._view(parent, "pv")  # long → wide: distinct values of on_col become columns
            sql = f"PIVOT {quote_identifier(v)} ON {quote_identifier(on_col)} USING {using}"
            if group:
                sql += " GROUP BY " + ", ".join(quote_identifier(c) for c in group)
            return db.conn().sql(sql)

        if t == "aggregate":
            if not self.full:
                grouped = (cfg.get("groupBy") or cfg.get("group") or "").strip()
                raise NotPreviewable(node, f"{'grouped' if grouped else 'global'} aggregate — needs a full pass (a sample would lie)")
            aggs = (cfg.get("aggs") or "count(*) AS n").strip()
            group = (cfg.get("groupBy") or "").strip()  # resolver canonicalizes groupBy/group → 'groupBy'
            validate_fragment(FragmentKind.AGGREGATES, aggs, con=db.conn())
            if group:
                validate_fragment(FragmentKind.GROUP_BY, group, con=db.conn())
            # include the group key(s) in the projection, else the aggregated rows are unlabeled
            return parent.aggregate(f"{group}, {aggs}", group) if group else parent.aggregate(aggs)

        if t == "sql":
            q = (cfg.get("sql") or "").strip()
            if not q:
                return parent
            # `input` / `inputN` are real query-scope CTE names.  Do not text-rewrite placeholder-looking
            # strings: a global replacement can alter literals/comments and create a second SQL grammar.
            validated = validate_query(q, len(inputs), con=db.conn())
            q = validated.sql
            # a GROUP BY / global aggregate over the 2000-row sample would present a PARTIAL result as
            # complete (the aggregate node already refuses a sample for exactly this reason) — refuse it.
            if not self.full and sql_reduces_rows(q):
                raise NotPreviewable(node, "this SQL aggregates/reduces rows — a sample would mislead; run a full pass")
            # a JOIN / window (OVER) / QUALIFY over two truncated 2000-row prefixes lies just like the
            # dedicated join/window nodes do — build the CTE inputs UNSAMPLED (full) so the query is
            # faithful; the preview LIMIT at the target then truncates the DISPLAY. Refuses honestly via
            # _faithful_inputs if a Python transform is upstream.
            sql_inputs = inputs
            if not self.full and sql_needs_full_input(q):
                sql_inputs = self._faithful_inputs(node)
            # Expose inputs as query-scoped CTEs named input/input2/... backed by UNIQUE views,
            # so two sql nodes in one graph never clobber a shared literal 'input' view.
            wrapped = bind_input_ctes(validated, [self._view(rel) for rel in sql_inputs])
            return db.conn().sql(wrapped)

        if t == "join":
            if len(inputs) < 2:
                return parent
            # joining two independently-truncated prefixes finds few/no real matches — join the FULL
            # inputs even in preview (bounded by the preview limit + budget). The join SQL (projection +
            # clause + naming) is the shared join_sql used by the distributed backend too, so they agree.
            ins = inputs if self.full else self._faithful_inputs(node)
            a, b = self._view(ins[0], "ja"), self._view(ins[1], "jb")
            return db.conn().sql(join_sql(list(ins[0].columns), list(ins[1].columns), a, b,
                                          cfg.get("on"), cfg.get("condition"), cfg.get("how"),
                                          con=db.conn()))

        if t == "union":
            # stack every incoming input row-wise. BY NAME aligns columns by name (filling missing ones
            # with NULL) — the safe default for same-shape datasets in a different column order; position
            # mode requires matching column counts. UNION dedups, UNION ALL keeps every row. Unlike join,
            # stacking truncated preview prefixes is still faithful (no cross-input matching), so this
            # uses the ordinary (possibly-sampled) inputs — no expensive full pass.
            if not inputs:
                return parent
            if len(inputs) == 1:
                return inputs[0]  # a lone input just passes through
            distinct = (cfg.get("mode") or "all").lower() == "distinct"
            by_name = (cfg.get("align") or "name").lower() != "position"
            op = ("UNION" if distinct else "UNION ALL") + (" BY NAME" if by_name else "")
            views = [self._view(r, f"u{i}") for i, r in enumerate(inputs)]
            return db.conn().sql(f" {op} ".join(f"SELECT * FROM {v}" for v in views))

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
            if col:
                col = identifier(col, base.columns, label="metric column")
            expr = "count(*)" if agg == "count" or not col else f"{_agg_name(agg)}({quote_identifier(col)})"
            v = self._view(base, "m")
            title = (node.data.get("title") if isinstance(node.data, dict) else None) or "metric"
            return db.conn().sql(
                f"SELECT '{_sql_str(title)}' AS metric, ({expr})::DOUBLE AS value "
                f"FROM {quote_identifier(v)}"
            )

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
            x = identifier(x, base.columns, label="chart X column")
            if y:
                y = identifier(y, base.columns, label="chart Y column")
            v, xq = self._view(base, "ch"), quote_identifier(x)
            if agg == "none":  # raw points (scatter/line) — the chart series is x,y as-is
                return db.conn().sql(
                    f"SELECT {xq} AS x, {quote_identifier(y)} AS y FROM {quote_identifier(v)}"
                )
            yexpr = "count(*)" if agg == "count" or not y else f"{_agg_name(agg)}({quote_identifier(y)})"
            # grouped series (bar/line): one point per distinct x, capped so a huge-cardinality x can't
            # blow up the chart. TRY_CAST (not ::DOUBLE) so a non-numeric/temporal min/max degrades to
            # NULL (dropped by the renderer) instead of a raw ConversionException.
            return db.conn().sql(
                f"SELECT {xq} AS x, TRY_CAST(({yexpr}) AS DOUBLE) AS y "
                f"FROM {quote_identifier(v)} GROUP BY {xq} ORDER BY {xq} LIMIT 2000"
            )

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

        if t == "variable":  # a named passthrough handle — carries its single input through unchanged
            return parent if parent is not None else _empty()
        # any other kind reaching here is unhandled — a missing plugin or a typo. Fail closed (P0-DATA-02):
        # never silently pass the input through, which would omit the intended work yet report success.
        if t not in self.node_specs and t not in self.node_builders:
            raise NotPreviewable(node, f"unknown node kind '{t}' — is its plugin installed and compatible?")
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
                for b in parent.to_arrow_reader(batch_size=_XF_BATCH):
                    t = _apply_batch(fn, pa.Table.from_batches([b]), fmt, on_error, node)
                    if t is None:  # on_error='skip' dropped this batch
                        continue
                    if tables:  # conform each later batch to the first (safe cast; loud on lossy drift)
                        t = _conform(t, tables[0].schema, node)
                    tables.append(t)
                table = pa.concat_tables(tables) if tables else parent.limit(0).to_arrow_table()
                return db.conn().from_arrow(table)
            out: list[dict] = []
            for batch in parent.to_arrow_reader(batch_size=_XF_BATCH):
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
        nbytes = 0
        FLUSH_BYTES = _SPILL_FLUSH_BYTES
        FLUSH_ROWS = _SPILL_FLUSH_ROWS

        def write_tbl(tbl: "pa.Table") -> None:
            nonlocal writer
            if tbl.num_rows == 0 and writer is not None:
                return
            if writer is None:
                writer = pq.ParquetWriter(path, tbl.schema)
            else:
                tbl = _conform(tbl, writer.schema, node)  # safe cast; loud on lossy drift (no silent corruption)
            writer.write_table(tbl)

        def flush():
            nonlocal buf, nbytes
            if buf:
                write_tbl(pa.Table.from_pylist(buf))
                buf = []
                nbytes = 0

        try:
            if fmt in ("pandas", "arrow"):  # arrow-native: type-preserving, one table per input batch
                for batch in parent.to_arrow_reader(batch_size=_XF_BATCH):
                    t = _apply_batch(fn, pa.Table.from_batches([batch]), fmt, on_error, node)
                    if t is not None:  # on_error='skip' dropped this batch
                        write_tbl(t)
            else:
                # stream output rows and flush on a BYTE budget (with a hard row cap as a backstop), so a
                # flat_map with a huge per-row fan-out never balloons the buffer — memory stays bounded.
                for batch in parent.to_arrow_reader(batch_size=_XF_BATCH):
                    for r in _iter_fn(fn, batch, mode, on_error, node):
                        buf.append(r)
                        nbytes += _est_row_bytes(r)
                        if nbytes >= FLUSH_BYTES or len(buf) >= FLUSH_ROWS:
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
        col = identifier(col, src.columns, label="vector column")
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
                q = con.sql(
                    f"SELECT {quote_identifier(col)} AS q FROM {quote_identifier(base)} "
                    f"OFFSET {qrow} LIMIT 1"
                ).fetchone()
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
            f"SELECT *, system.main.list_cosine_similarity({quote_identifier(col)}, {qlit}) AS _score "
            f"FROM {quote_identifier(base)} ORDER BY _score DESC LIMIT {k}"
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


# transform read batch size — IDENTICAL in preview and full run so on_error='skip' drops the same
# rows in both (a batch-size-dependent skip would make the preview disagree with the run).
_XF_BATCH = 8192
# spill buffer bounds for the full-run transform: flush the row buffer to Parquet when it reaches this
# many BYTES (primary — one big row shouldn't need 50k siblings to trigger a flush) or this many rows
# (a hard backstop so tiny rows still flush).
_SPILL_FLUSH_BYTES = 128 * 1024 * 1024
_SPILL_FLUSH_ROWS = 500_000


def _iter_fn(fn, batch: "pa.RecordBatch", mode: str, on_error: str, node):
    """Yield the transform's output rows for one input batch. flat_map/flat_map_generator are STREAMED
    (yield from — no per-row list()), so a large per-row fan-out never materializes here; the spill
    caller flushes on a BYTE budget, keeping memory bounded regardless of fan-out."""
    rows = batch.to_pylist()
    if mode == "map_batches":
        try:
            yield from fn(rows)
            return
        except Exception as e:  # noqa: BLE001
            if on_error == "skip":
                # isolate the bad rows by re-running the UDF one row at a time and keeping the
                # successes, so 'skip' drops only the rows that actually fail — NOT the whole batch
                # (dropping the batch would make the result depend on batch size; size-invariant → preview == run).
                for r in rows:
                    try:
                        yield from fn([dict(r)])
                    except Exception:  # noqa: BLE001 — this row genuinely fails; drop just it
                        continue
                return
            raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e
    for r in rows:
        try:
            if mode == "map":
                yield fn(dict(r))
            elif mode == "filter":
                if fn(dict(r)):
                    yield r
            elif mode in ("flat_map", "flat_map_generator"):
                yield from fn(dict(r))
        except Exception as e:  # noqa: BLE001
            if on_error == "skip":
                continue
            raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e


def _apply_fn(fn, batch: "pa.RecordBatch", mode: str, on_error: str, node) -> list[dict]:
    return list(_iter_fn(fn, batch, mode, on_error, node))


def _est_row_bytes(v, _depth: int = 0) -> int:
    """Cheap approximate in-memory size of a row/value — used to bound the spill buffer by BYTES rather
    than a fixed row count: one row can be a big blob or embedding, so 50k such rows is nothing like 50k
    small dicts. Recursion is depth-capped so a pathological nesting can't make this expensive."""
    if v is None:
        return 8
    if isinstance(v, (bytes, bytearray, str)):
        return len(v) + 16
    if isinstance(v, bool):  # before int — bool is a subclass of int
        return 8
    if isinstance(v, (int, float)):
        return 8
    if _depth >= 5:
        return 64
    if isinstance(v, dict):
        return 24 + sum(_est_row_bytes(x, _depth + 1) for x in v.values())
    if isinstance(v, (list, tuple)):
        return 24 + sum(_est_row_bytes(x, _depth + 1) for x in v)
    return 32


def _apply_batch(fn, table: "pa.Table", fmt: str, on_error: str, node) -> "pa.Table | None":
    """Run a `map_batches` UDF over a whole batch in the chosen representation — `pandas` (a DataFrame) or
    `arrow` (a pyarrow.Table) — arrow-NATIVE so column types survive (no dict round-trip). Returns the
    output Table, or None when on_error='skip' swallowed a failure (the caller DROPS it — we can't emit a
    correct-schema empty here, and the input schema would clash with the output schema of good batches).
    (The default `rows` format goes through _apply_fn.) pandas must be declared in the canvas requirements;
    pyarrow is always present."""
    try:
        return _run_batch(fn, table, fmt)
    except NotPreviewable:
        raise
    except Exception as e:  # noqa: BLE001
        if on_error == "skip":
            # isolate the bad rows: re-run the UDF on 1-row slices and keep the ones that succeed,
            # so 'skip' drops only the failing rows regardless of batch size (preview == run). None
            # only if EVERY row failed (nothing to emit — a good row defines the output schema).
            parts = []
            for i in range(table.num_rows):
                try:
                    parts.append(_run_batch(fn, table.slice(i, 1), fmt))
                except Exception:  # noqa: BLE001
                    continue
            return pa.concat_tables(parts) if parts else None
        raise NotPreviewable(node, f"cell error: {type(e).__name__}: {e}") from e


def _run_batch(fn, table: "pa.Table", fmt: str) -> "pa.Table":
    """Invoke a batch UDF once over `table` in the chosen representation and return a pyarrow.Table."""
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


def _conform(tbl: "pa.Table", schema: "pa.Schema", node) -> "pa.Table":
    """Cast a transform's output batch to the schema the FIRST batch established, with a SAFE cast — it
    raises on a lossy narrowing (e.g. a int64 value that won't fit the first batch's int32) instead of
    silently corrupting it. If the batch can't be safely reconciled, fail loudly and name the drift: a
    transform must emit ONE consistent schema, and the old safe=False down-cast corrupted values the
    preview never showed. A safe WIDENING (int32→int64) still passes."""
    if tbl.schema.equals(schema):
        return tbl
    try:
        return tbl.cast(schema)
    except Exception as e:  # noqa: BLE001
        try:
            drift = ", ".join(f"{f.name}: {tbl.schema.field(f.name).type}→{f.type}" for f in schema
                              if tbl.schema.get_field_index(f.name) >= 0
                              and not tbl.schema.field(f.name).type.equals(f.type))
            missing = [f.name for f in schema if tbl.schema.get_field_index(f.name) < 0]
            extra = [f.name for f in tbl.schema if schema.get_field_index(f.name) < 0]
            detail = "; ".join(x for x in (drift, f"missing {missing}" if missing else "",
                                           f"extra {extra}" if extra else "") if x)
        except Exception:  # noqa: BLE001 — never let message-building mask the real drift error
            detail = ""
        raise NotPreviewable(node, f"a transform batch's schema drifted from the first batch and can't be "
                             f"safely reconciled ({detail or e}); a transform must emit one schema") from e


def _table_to_rows(tbl: "pa.Table") -> list[dict]:
    import decimal
    rows = tbl.to_pylist()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, decimal.Decimal):
                # ship a number when float round-trips the EXACT value (prices etc. — keeps grid
                # sort/charts/numeric rendering), else an exact string, so the previewed value never
                # disagrees with the exact value the run writes to parquet (faithful preview).
                fv = float(v)
                r[k] = fv if decimal.Decimal(repr(fv)) == v else str(v)
            elif isinstance(v, (bytes, bytearray)):
                r[k] = f"<{len(v)} bytes>"
            elif hasattr(v, "isoformat"):
                r[k] = v.isoformat()
    return rows


def _dedupe_names(names: list[str]) -> list[str]:
    return unique_identifier_names(names)


_AGG_ALLOWED = {"count", "sum", "mean", "avg", "min", "max", "median", "stddev"}


def _agg_name(agg: str) -> str:
    a = (agg or "count").lower()
    if a not in _AGG_ALLOWED:
        raise ValueError(f"unsupported aggregate '{agg}' (allowed: {', '.join(sorted(_AGG_ALLOWED))})")
    return {"mean": "avg"}.get(a, a)


def _ident(col: str) -> str:
    # DOUBLE embedded quotes (SQL identifier escaping) — the caller wraps the result in "...". Stripping
    # them (the old behavior) silently addressed a DIFFERENT column when a real name contained a quote.
    return str(col).replace('"', '""')


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
