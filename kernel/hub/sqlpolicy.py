"""Fail-closed policy for every user-authored DuckDB SQL/expression boundary.

DuckDB's relation helpers accept SQL strings, not inert expression objects: a filter can contain a
subquery, a projection can replace its FROM clause when concatenated unsafely, and ``Connection.sql``
accepts multiple statements.  This module is therefore the single gate used before any graph fragment is
handed to DuckDB (locally, in a subprocess, or on a Ray worker).

The policy is intentionally narrower than DuckDB SQL.  A SQL node is one SELECT over its wired
``input``/``inputN`` relations and query-local CTEs.  Expression fragments are parsed inside a fixed SELECT
wrapper and cannot introduce a subquery/table.  Table functions, catalog access, side-effecting functions,
and user macros/UDFs are rejected.  Values are still DuckDB expressions; identifiers are handled separately
through ``identifier``/``identifier_list`` so callers never interpolate an unquoted user string.

This is the untrusted graph/SDK boundary, not an OS sandbox.  Section Python receives a DuckDB relation,
and installed plugin code can import ``hub.db`` directly; both are trusted-code escape hatches and are
deliberately outside this policy.  The run transaction fence prevents untrusted SQL from racing its own
lazy bind, but trusted code that can mutate the database or process remains trusted by definition.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Iterable

import duckdb


MAX_SQL_BYTES = 64 * 1024
MAX_AST_NODES = 10_000
MAX_QUERY_DEPTH = 64
SUPPORTED_DUCKDB_VERSION = "1.5.4"


def _require_supported_version(version: str | None = None) -> None:
    current = duckdb.__version__ if version is None else str(version)
    if current == SUPPORTED_DUCKDB_VERSION:
        return
    raise RuntimeError(
        "SQL policy requires DuckDB "
        f"{SUPPORTED_DUCKDB_VERSION}; found {current}. Review the AST policy before upgrading."
    )


_require_supported_version()


class SQLPolicyError(ValueError):
    """A user-authored SQL/query fragment is outside the executable policy."""


class FragmentKind(str, Enum):
    PREDICATE = "predicate"
    JOIN_ON = "join_on"
    PROJECTION = "projection"
    ORDER_BY = "order_by"
    GROUP_BY = "group_by"
    AGGREGATES = "aggregates"
    WINDOW_EXPR = "window_expr"
    LITERAL = "literal"


@dataclass(frozen=True)
class FunctionRef:
    name: str
    catalog: str = ""
    schema: str = ""


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str
    functions: tuple[FunctionRef, ...]


_BARE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_WRAPPER_TABLE = "__dp_policy_input"
_WRAPPER_TABLE_2 = "__dp_policy_input2"
_TAIL_ALIAS = "__dp_policy_tail"
_DENIED_CORE_FUNCTIONS = {
    "current_localtime", "current_localtimestamp", "current_setting", "getvariable", "which_secret",
}
_DENIED_SPECIAL_NAMES = {
    "current_catalog", "current_date", "current_role", "current_schema", "current_time",
    "current_timestamp", "current_user", "localtime", "localtimestamp", "session_user",
}


def _ascii_fold(value: str) -> str:
    """DuckDB identifier comparison folds ASCII case only, not arbitrary Unicode case."""
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in value)


def _bounded(text: object, label: str) -> str:
    value = str(text or "")
    size = len(value.encode("utf-8"))
    if size > MAX_SQL_BYTES:
        raise SQLPolicyError(
            f"{label} is too large ({size} bytes; maximum {MAX_SQL_BYTES} bytes)"
        )
    return value


def _walk(obj):
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _parse_select(sql: str) -> tuple[dict, str]:
    """Pure parse on an isolated connection; never binds names or touches external state."""
    con = duckdb.connect()
    try:
        statements = con.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise SQLPolicyError("SQL must contain exactly one SELECT statement")
        # Use DuckDB's own serialized parse tree.  Parse the normalized Statement query so PRAGMA-style
        # SELECT rewrites become visible as table functions and are rejected below.
        normalized = statements[0].query or sql
        row = con.execute("SELECT json_serialize_sql(?)", [normalized]).fetchone()
        doc = json.loads(row[0]) if row and row[0] else None
        if not doc or doc.get("error") or len(doc.get("statements") or []) != 1:
            raise SQLPolicyError("SQL could not be represented as one SELECT query")
        node = doc["statements"][0].get("node")
        if not isinstance(node, dict) or node.get("type") not in (
            "SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"
        ):
            raise SQLPolicyError("SQL must be a read-only SELECT query")
        # Deserialize DuckDB's own AST back to canonical SQL.  Unlike Statement.query this removes a
        # trailing semicolon/comments, which lets callers inject input CTEs without changing ORDER BY or
        # nesting the query under an outer SELECT.
        canonical_row = con.execute("SELECT json_deserialize_sql(?)", [row[0]]).fetchone()
        canonical = str(canonical_row[0] or "").strip() if canonical_row else ""
        if not canonical:
            raise SQLPolicyError("SQL could not be normalized for execution")
        return node, canonical
    except SQLPolicyError:
        raise
    except Exception as exc:
        raise SQLPolicyError(f"SQL does not parse as one SELECT: {exc}") from exc
    finally:
        con.close()


def _cte_names(ast: dict) -> set[str]:
    names: set[str] = set()
    for node in _walk(ast):
        cte_map = node.get("cte_map")
        if not isinstance(cte_map, dict):
            continue
        for entry in cte_map.get("map") or []:
            if isinstance(entry, dict) and entry.get("key"):
                names.add(_ascii_fold(str(entry["key"])))
    return names


def _function_refs(ast: dict) -> tuple[FunctionRef, ...]:
    refs: list[FunctionRef] = []
    seen: set[FunctionRef] = set()
    for node in _walk(ast):
        if node.get("type") == "TABLE_FUNCTION":
            raise SQLPolicyError("table functions are not allowed in user SQL")
        if node.get("class") not in ("FUNCTION", "WINDOW"):
            continue
        ref = FunctionRef(
            name=_ascii_fold(str(node.get("function_name") or "")),
            catalog=_ascii_fold(str(node.get("catalog") or "")),
            schema=_ascii_fold(str(node.get("schema") or "")),
        )
        if not ref.name:
            raise SQLPolicyError("SQL contains an unresolved function call")
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return tuple(refs)


def _validate_special_expressions(ast: dict) -> None:
    # DuckDB's unbound AST represents SQL-standard current-date/session keywords as unqualified column
    # references.  They bind to non-deterministic/session state when no input column shadows the spelling,
    # so reject them conservatively before binding (including a real column with the reserved spelling).
    for node in _walk(ast):
        if node.get("class") != "COLUMN_REF":
            continue
        names = node.get("column_names") or []
        if len(names) == 1 and _ascii_fold(str(names[0])) in _DENIED_SPECIAL_NAMES:
            raise SQLPolicyError(
                f"non-deterministic/session expression '{names[0]}' is not allowed in cached plans"
            )


def _function_fingerprint(row) -> tuple:
    """Stable overload identity used to distinguish pristine core functions from loaded extensions."""
    return (
        _ascii_fold(str(row[2])),
        tuple(str(x) for x in (row[5] or [])),
        str(row[6] or ""),
        str(row[7] or ""),
        str(row[8] or ""),
    )


@lru_cache(maxsize=1)
def _pristine_core_functions() -> dict[str, frozenset[tuple]]:
    con = duckdb.connect()
    try:
        con.execute("SET autoinstall_known_extensions = false")
        con.execute("SET autoload_known_extensions = false")
        con.execute("SET python_enable_replacements = false")
        rows = con.execute(
            "SELECT function_name, schema_name, function_type, internal, has_side_effects, "
            "parameter_types, varargs, return_type, stability "
            "FROM system.main.duckdb_functions()"
        ).fetchall()
        out: dict[str, set[tuple]] = {}
        for row in rows:
            if bool(row[3]):
                out.setdefault(_ascii_fold(str(row[0])), set()).add(_function_fingerprint(row))
        return {name: frozenset(overloads) for name, overloads in out.items()}
    finally:
        con.close()


def _validate_functions(refs: Iterable[FunctionRef], con=None) -> None:
    refs = tuple(refs)
    if not refs:
        return
    owned = con is None
    con = con or duckdb.connect()
    try:
        for ref in refs:
            if ref.name in _DENIED_CORE_FUNCTIONS:
                raise SQLPolicyError(f"session function '{ref.name}' is not allowed")
            if ref.catalog and ref.catalog != "system":
                raise SQLPolicyError(
                    f"function '{ref.catalog}.{ref.schema}.{ref.name}' is not a system function"
                )
            if ref.schema and ref.schema not in ("main", "pg_catalog"):
                raise SQLPolicyError(
                    f"function '{ref.schema}.{ref.name}' is not in a system schema"
                )
            rows = con.execute(
                "SELECT database_name, schema_name, function_type, internal, has_side_effects, "
                "parameter_types, varargs, return_type, stability "
                "FROM system.main.duckdb_functions() "
                "WHERE system.main.lower(function_name) = ?",
                [ref.name],
            ).fetchall()
            if not rows:
                raise SQLPolicyError(f"function '{ref.name}' is not available")

            # An explicitly system-qualified call cannot bind to a temp/user shadow.  Any other spelling
            # is rejected if a non-internal candidate exists, even when an internal overload also exists.
            explicitly_system = ref.catalog == "system"
            visible = [
                r for r in rows
                if explicitly_system or _ascii_fold(str(r[1])) == (ref.schema or "main")
            ]
            if not visible:
                visible = rows
            if not explicitly_system and any(not bool(r[3]) for r in visible):
                raise SQLPolicyError(
                    f"function '{ref.name}' is shadowed by a user macro or UDF"
                )
            internal = [r for r in visible if bool(r[3])]
            if not internal:
                raise SQLPolicyError(f"function '{ref.name}' is not an internal DuckDB function")
            # Do not recursively trust a macro body.  DuckDB ships internal scalar macros that expand to
            # sleeping, PRAGMA, or catalog-reading functions while the macro row itself reports no side
            # effect metadata.  Reject every macro/table/pragma overload by name, conservatively including
            # names that also have an ordinary scalar overload (for example range).
            if any(
                _ascii_fold(str(r[2])) in ("macro", "table", "table_macro", "pragma")
                for r in rows
            ):
                raise SQLPolicyError(f"macro/table function '{ref.name}' is not allowed")
            if any(r[4] is not False for r in internal):
                raise SQLPolicyError(f"side-effecting function '{ref.name}' is not allowed")
            if any(str(r[8] or "").upper() != "CONSISTENT" for r in internal):
                raise SQLPolicyError(
                    f"non-deterministic function '{ref.name}' is not allowed in cached plans"
                )
            pristine = _pristine_core_functions().get(ref.name, frozenset())
            if not pristine or any(_function_fingerprint(r) not in pristine for r in internal):
                raise SQLPolicyError(
                    f"function '{ref.name}' is not part of the reviewed DuckDB core function set"
                )
    except SQLPolicyError:
        raise
    except Exception as exc:
        raise SQLPolicyError(f"could not verify SQL function provenance: {exc}") from exc
    finally:
        if owned:
            con.close()


def _base_tables(ast: dict) -> list[dict]:
    return [n for n in _walk(ast) if n.get("type") == "BASE_TABLE"]


_QUERY_NODE_TYPES = {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"}


def _check_query_tables(ast: dict, input_count: int) -> None:
    """Validate base tables with real lexical CTE visibility, never a query-global name set.

    A non-recursive CTE definition may see outer CTEs and earlier declarations only.  DuckDB represents
    an actual recursive declaration as RECURSIVE_CTE_NODE; only that declaration receives its own name.
    This prevents a forward/nested CTE name from laundering an external table with the same spelling.
    """
    allowed_inputs = {"input"} | {f"input{i}" for i in range(2, input_count + 1)}
    if sum(1 for node in _walk(ast) if isinstance(node, dict)) > MAX_AST_NODES:
        raise SQLPolicyError(f"SQL AST exceeds the {MAX_AST_NODES}-node policy limit")

    def visit_query(node: dict, inherited: set[str], depth: int = 0) -> None:
        if depth > MAX_QUERY_DEPTH:
            raise SQLPolicyError(f"SQL exceeds the {MAX_QUERY_DEPTH}-level query nesting limit")
        local = set(inherited)
        entries = (node.get("cte_map") or {}).get("map") or []
        for entry in entries:
            if not isinstance(entry, dict):
                raise SQLPolicyError("SQL contains an invalid CTE declaration")
            name = _ascii_fold(str(entry.get("key") or ""))
            child = (((entry.get("value") or {}).get("query") or {}).get("node"))
            if not name or not isinstance(child, dict):
                raise SQLPolicyError("SQL contains an invalid CTE declaration")
            if name in allowed_inputs:
                raise SQLPolicyError(
                    f"CTE '{entry.get('key')}' cannot shadow a wired input relation"
                )
            recursive = (
                child.get("type") == "RECURSIVE_CTE_NODE"
                and _ascii_fold(str(child.get("cte_name") or "")) == name
            )
            visit_query(child, local | ({name} if recursive else set()), depth + 1)
            local.add(name)

        stack = [node]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
                continue
            if not isinstance(value, dict):
                continue
            node_type = value.get("type")
            if value is not node and isinstance(node_type, str) and node_type in _QUERY_NODE_TYPES:
                visit_query(value, local, depth + 1)
                continue
            # DuckDB classifies SHOW/DESCRIBE/SUMMARIZE as SELECT statements, but their SHOW_REF table
            # reference reads catalog metadata rather than a wired relation.
            if node_type == "SHOW_REF":
                raise SQLPolicyError("SHOW, DESCRIBE, and SUMMARIZE are not allowed in user SQL")
            if node_type == "BASE_TABLE":
                catalog = str(value.get("catalog_name") or "")
                schema = str(value.get("schema_name") or "")
                name = str(value.get("table_name") or "")
                folded = _ascii_fold(name)
                if catalog or schema or (folded not in allowed_inputs and folded not in local):
                    raise SQLPolicyError(
                        "SQL may read only its wired input relation(s); "
                        f"table '{name or '?'}' is not allowed"
                    )
            for key, child in value.items():
                if key != "cte_map":
                    stack.append(child)

    visit_query(ast, set())


def validate_query(sql: object, input_count: int, *, con=None) -> ValidatedSQL:
    text = _bounded(sql, "SQL query").strip()
    if not text:
        raise SQLPolicyError("SQL query is empty")
    if input_count < 1:
        raise SQLPolicyError("SQL query has no wired input relation")
    ast, normalized = _parse_select(text)
    _check_query_tables(ast, input_count)
    _validate_special_expressions(ast)
    refs = _function_refs(ast)
    _validate_functions(refs, con)
    return ValidatedSQL(normalized, refs)


def bind_input_ctes(validated: ValidatedSQL, view_names: Iterable[object]) -> str:
    """Bind generated views to input/inputN without placeholder replacement or an outer SELECT."""
    views = [str(name) for name in view_names]
    if not views:
        raise SQLPolicyError("SQL query has no input views to bind")
    aliases = ["input"] + [f"input{i}" for i in range(2, len(views) + 1)]
    definitions = ", ".join(
        f"{quote_identifier(alias)} AS (SELECT * FROM {quote_identifier(view)})"
        for alias, view in zip(aliases, views)
    )
    query = validated.sql
    upper = query.upper()
    if upper.startswith("WITH RECURSIVE "):
        return f"WITH RECURSIVE {definitions}, {query[len('WITH RECURSIVE '):]}"
    if upper.startswith("WITH "):
        return f"WITH {definitions}, {query[len('WITH '):]}"
    return f"WITH {definitions} {query}"


def _wrapper(kind: FragmentKind, fragment: str) -> str:
    qtable = quote_identifier(_WRAPPER_TABLE)
    qtable2 = quote_identifier(_WRAPPER_TABLE_2)
    qtail = quote_identifier(_TAIL_ALIAS)
    if kind == FragmentKind.PREDICATE:
        return f"SELECT 1 AS {qtail} FROM {qtable} WHERE (({fragment}) AND TRUE)"
    if kind == FragmentKind.JOIN_ON:
        return (
            f"SELECT 1 AS {qtail} FROM {qtable} AS a JOIN {qtable2} AS b "
            f"ON (({fragment}) AND TRUE)"
        )
    if kind in (FragmentKind.PROJECTION, FragmentKind.AGGREGATES):
        return f"SELECT {fragment}, 0 AS {qtail} FROM {qtable}"
    if kind == FragmentKind.ORDER_BY:
        return f"SELECT 1 AS {qtail} FROM {qtable} ORDER BY {fragment}"
    if kind == FragmentKind.GROUP_BY:
        return f"SELECT count(*) AS {qtail} FROM {qtable} GROUP BY {fragment}"
    if kind == FragmentKind.WINDOW_EXPR:
        return f"SELECT {fragment} OVER () AS {qtail} FROM {qtable}"
    if kind == FragmentKind.LITERAL:
        return f"SELECT ({fragment}) AS {qtail} FROM {qtable} LIMIT 0"
    raise SQLPolicyError(f"unsupported SQL fragment kind '{kind}'")


def _top_select(ast: dict) -> dict:
    if ast.get("type") != "SELECT_NODE":
        raise SQLPolicyError("SQL fragment changed the wrapper query shape")
    return ast


def _check_fragment_shape(ast: dict, kind: FragmentKind) -> None:
    top = _top_select(ast)
    tables = _base_tables(ast)
    expected_tables = (
        {_WRAPPER_TABLE, _WRAPPER_TABLE_2}
        if kind == FragmentKind.JOIN_ON else {_WRAPPER_TABLE}
    )
    actual_tables = {str(t.get("table_name") or "") for t in tables}
    if len(tables) != len(expected_tables) or actual_tables != expected_tables:
        raise SQLPolicyError("SQL fragments cannot read tables or subqueries")
    if any(n.get("class") == "SUBQUERY" or n.get("type") == "SUBQUERY" for n in _walk(ast)):
        raise SQLPolicyError("subqueries are not allowed in SQL fragments")
    if _cte_names(ast):
        raise SQLPolicyError("CTEs are not allowed in SQL fragments")

    select_list = top.get("select_list") or []
    if not select_list or str(select_list[-1].get("alias") or "") != _TAIL_ALIAS:
        raise SQLPolicyError("SQL fragment escaped its wrapper position")
    modifiers = top.get("modifiers") or []
    modifier_types = [str(m.get("type") or "") for m in modifiers if isinstance(m, dict)]
    if kind == FragmentKind.ORDER_BY:
        if modifier_types != ["ORDER_MODIFIER"]:
            raise SQLPolicyError("ORDER BY fragment changed the wrapper query shape")
    elif kind == FragmentKind.LITERAL:
        if modifier_types != ["LIMIT_MODIFIER"]:
            raise SQLPolicyError("literal fragment changed the wrapper query shape")
    elif modifier_types:
        raise SQLPolicyError("SQL fragment cannot add query modifiers")

    where = top.get("where_clause")
    if kind == FragmentKind.PREDICATE:
        if where is None:
            raise SQLPolicyError("predicate fragment changed the wrapper query shape")
    elif where is not None:
        raise SQLPolicyError("SQL fragment cannot add a WHERE clause")
    if kind == FragmentKind.JOIN_ON:
        from_table = top.get("from_table") or {}
        if from_table.get("type") != "JOIN" or from_table.get("condition") is None:
            raise SQLPolicyError("join condition changed the wrapper query shape")

    if kind == FragmentKind.GROUP_BY:
        group_expressions = top.get("group_expressions") or []
        if not group_expressions:
            raise SQLPolicyError("GROUP BY fragment is empty")
        if top.get("group_sets") != [list(range(len(group_expressions)))]:
            raise SQLPolicyError("GROUPING SETS, ROLLUP, and CUBE are not allowed in this fragment")
        if top.get("having") is not None:
            raise SQLPolicyError("GROUP BY fragment cannot add HAVING")
    elif top.get("group_expressions") or top.get("group_sets") or top.get("having") is not None:
        raise SQLPolicyError("SQL fragment cannot add grouping or HAVING")
    if top.get("qualify") is not None:
        raise SQLPolicyError("SQL fragment cannot add QUALIFY")
    if kind == FragmentKind.WINDOW_EXPR:
        roots = select_list[:-1] if len(select_list) > 1 else select_list
        if not roots or not all(r.get("class") == "WINDOW" for r in roots):
            raise SQLPolicyError("window expression must be a window-capable function call")

    if kind == FragmentKind.LITERAL:
        if len(select_list) != 1:
            raise SQLPolicyError("literal fragment must contain exactly one value")
        # Literal fill values are deliberately data-independent.  Function calls, columns, parameters,
        # and subqueries would turn a value field into another arbitrary expression surface.
        forbidden = {"COLUMN_REF", "FUNCTION", "WINDOW", "SUBQUERY", "PARAMETER"}
        if any(str(n.get("class") or "") in forbidden for n in _walk(select_list[0])):
            raise SQLPolicyError("fill value must be a data-independent SQL literal")


def validate_fragment(kind: FragmentKind, fragment: object, *, con=None) -> ValidatedSQL:
    text = _bounded(fragment, f"{kind.value} fragment").strip()
    if not text:
        raise SQLPolicyError(f"{kind.value} fragment is empty")
    ast, _normalized = _parse_select(_wrapper(kind, text))
    _check_fragment_shape(ast, kind)
    _validate_special_expressions(ast)
    refs = _function_refs(ast)
    # The wrapper itself adds count() for GROUP_BY; it is an internal, side-effect-free aggregate.
    _validate_functions(refs, con)
    return ValidatedSQL(text, refs)


def join_equality_columns(fragment: object, *, con=None) -> tuple[list[str], list[str]] | None:
    """Extract a pure ``a.field = b.field [AND ...]`` JOIN_ON key sequence.

    The validated DuckDB AST is authoritative for identifier parsing, including quoted and escaped
    names.  Any other boolean or value expression is deliberately not interpreted as a key.
    """
    validated = validate_fragment(FragmentKind.JOIN_ON, fragment, con=con)
    ast, _normalized = _parse_select(_wrapper(FragmentKind.JOIN_ON, validated.sql))
    condition = (_top_select(ast).get("from_table") or {}).get("condition")
    if not isinstance(condition, dict) or condition.get("type") != "CONJUNCTION_AND":
        return None
    children = condition.get("children")
    if not isinstance(children, list) or len(children) < 2:
        return None

    def is_true(item: object) -> bool:
        child = item.get("child") if isinstance(item, dict) else None
        value = child.get("value") if isinstance(child, dict) else None
        return bool(
            isinstance(item, dict)
            and item.get("class") == "CAST"
            and (item.get("cast_type") or {}).get("id") == "BOOLEAN"
            and isinstance(child, dict)
            and child.get("class") == "CONSTANT"
            and isinstance(value, dict)
            and value.get("value") == "t"
        )

    # The wrapper contributes one TRUE fence; user-authored TRUE conjuncts are semantically identical
    # and cannot be allowed to hide an otherwise explicit cross-side equality.
    if not is_true(children[-1]):
        return None

    left_fields: list[str] = []
    right_fields: list[str] = []
    for item in children:
        if is_true(item):
            continue
        if not isinstance(item, dict) or item.get("type") != "COMPARE_EQUAL":
            continue
        left, right = item.get("left"), item.get("right")
        if not (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.get("class") == right.get("class") == "COLUMN_REF"
        ):
            continue
        left_names, right_names = left.get("column_names"), right.get("column_names")
        aliases = (
            {_ascii_fold(left_names[0]), _ascii_fold(right_names[0])}
            if (isinstance(left_names, list) and isinstance(right_names, list)
                and left_names and right_names
                and isinstance(left_names[0], str) and isinstance(right_names[0], str))
            else set()
        )
        if not (
            isinstance(left_names, list)
            and isinstance(right_names, list)
            and len(left_names) == len(right_names) == 2
            and all(isinstance(value, str) for value in [*left_names, *right_names])
            and aliases == {"a", "b"}
        ):
            continue
        if _ascii_fold(left_names[0]) == "a":
            left_fields.append(left_names[1])
            right_fields.append(right_names[1])
        else:
            left_fields.append(right_names[1])
            right_fields.append(left_names[1])
    return (left_fields, right_fields) if left_fields else None


def quote_identifier(name: object) -> str:
    value = str(name)
    if "\x00" in value:
        raise SQLPolicyError("SQL identifiers cannot contain NUL")
    return '"' + value.replace('"', '""') + '"'


def identifier_key(name: object) -> str:
    """DuckDB's comparison key for an unquoted identifier."""
    return _ascii_fold(str(name))


def unique_identifier_names(names: Iterable[object], *, used: Iterable[object] = ()) -> list[str]:
    """Allocate deterministic, ASCII-fold-unique aliases without colliding with prior names."""
    occupied = {identifier_key(name) for name in used}
    result: list[str] = []
    for raw in names:
        base = str(raw)
        candidate = base
        suffix = 2
        while identifier_key(candidate) in occupied:
            candidate = f"{base}_{suffix}"
            suffix += 1
        occupied.add(identifier_key(candidate))
        result.append(candidate)
    return result


def validate_identifier_schema(columns: Iterable[object], *, label: str) -> list[str]:
    """Return names only when DuckDB can address every input column unambiguously."""
    values = [str(column) for column in columns]
    seen: dict[str, str] = {}
    for value in values:
        key = identifier_key(value)
        if key in seen:
            raise SQLPolicyError(
                f"{label} is ambiguous: columns '{seen[key]}' and '{value}' compare equal"
            )
        seen[key] = value
    return values


def identifier(name: object, columns: Iterable[object], *, label: str = "column") -> str:
    raw = str(name)
    available = [str(c) for c in columns]
    folded = _ascii_fold(raw)
    matches = [c for c in available if _ascii_fold(c) == folded]
    if not matches:
        raise SQLPolicyError(f"{label} '{raw}' is not present in the input schema")
    if len(matches) != 1:
        raise SQLPolicyError(f"{label} '{raw}' is ambiguous in the input schema")
    return matches[0]


def _split_identifiers(value: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    quoted = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == '"':
            if quoted and i + 1 < len(value) and value[i + 1] == '"':
                # Preserve the SQL spelling.  Delimiter tracking skips the escaped quote, while
                # _decode_identifier below turns the doubled pair into the real column name.
                current.extend(('"', '"'))
                i += 2
                continue
            quoted = not quoted
            current.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if quoted:
        raise SQLPolicyError("unterminated quoted identifier")
    out.append("".join(current).strip())
    return out


def _decode_identifier(token: str) -> str:
    if not token:
        raise SQLPolicyError("identifier list contains an empty item")
    if token.startswith('"'):
        if len(token) < 2 or not token.endswith('"'):
            raise SQLPolicyError(f"invalid quoted identifier '{token}'")
        inner = token[1:-1]
        # Decode doubled quotes and reject a stray unescaped quote.
        out, i = [], 0
        while i < len(inner):
            if inner[i] == '"':
                if i + 1 >= len(inner) or inner[i + 1] != '"':
                    raise SQLPolicyError(f"invalid quoted identifier '{token}'")
                out.append('"')
                i += 2
            else:
                out.append(inner[i])
                i += 1
        return "".join(out)
    if not _BARE_IDENT.fullmatch(token):
        raise SQLPolicyError(f"identifier '{token}' must be a bare or double-quoted column name")
    return token


def parse_identifier_list(value: object, *, label: str = "column") -> list[str]:
    text = _bounded(value, f"{label} list").strip()
    if not text:
        return []
    # Keep parsing deliberately small: these fields are identifiers, not SQL expressions.
    tokens = _split_identifiers(text)
    return [_decode_identifier(token) for token in tokens]


def identifier_list(value: object, columns: Iterable[object], *, label: str = "column") -> list[str]:
    return [
        identifier(name, columns, label=label)
        for name in parse_identifier_list(value, label=label)
    ]


def validate_identifier_alias(value: object, *, label: str = "alias") -> str:
    raw = _bounded(value, label).strip()
    if not raw:
        raise SQLPolicyError(f"{label} is empty")
    if "\x00" in raw:
        raise SQLPolicyError(f"{label} cannot contain NUL")
    return raw
