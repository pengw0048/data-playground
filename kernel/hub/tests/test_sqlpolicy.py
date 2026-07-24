from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pytest

from hub import db
from hub.executors.engine import BuildEngine, _dedupe_names, join_projection
from hub.models import Graph
from hub.plan_key import CACHE_SCHEMA_VERSION
from hub.sdk import ctx
from hub.sqlpolicy import (
    MAX_SQL_BYTES,
    FragmentKind,
    SQLPolicyError,
    SUPPORTED_DUCKDB_VERSION,
    _parse_select,
    _require_supported_version,
    bind_input_ctes,
    identifier,
    identifier_list,
    join_equality_columns,
    quote_identifier,
    validate_fragment,
    validate_query,
)


def _node(node_id: str, kind: str, config: dict) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "position": {"x": 0, "y": 0},
        "data": {"title": node_id, "config": config},
    }


def _edge(source: str, target: str) -> dict:
    return {
        "id": f"{source}-{target}",
        "source": source,
        "target": target,
        "data": {"wire": "dataset"},
    }


def _bound_engine(kind: str, config: dict, rel) -> BuildEngine:
    graph = Graph(**{
        "id": f"policy-{kind}",
        "version": 1,
        "nodes": [_node("target", kind, config)],
        "edges": [],
    })
    return BuildEngine(
        graph,
        lambda _uri: None,
        SimpleNamespace(),
        full=True,
        bound_inputs={"target": rel},
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM input; SELECT * FROM input",
        "CREATE TABLE escaped(x INTEGER)",
        "COPY input TO '/tmp/escaped.parquet'",
        "SET threads = 1",
        "PRAGMA version",
        "ATTACH ':memory:' AS escaped",
        "UPDATE input SET x = 1",
        "DELETE FROM input",
        "INSERT INTO input VALUES (1)",
        "INSTALL httpfs",
        "LOAD httpfs",
    ],
)
def test_query_policy_requires_exactly_one_select_statement(sql):
    with pytest.raises(SQLPolicyError):
        validate_query(sql, 1)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secret",
        "SELECT * FROM main.input",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM query('SELECT * FROM input')",
        "SELECT * FROM query_table('input')",
        "SELECT * FROM json_execute_serialized_sql('{}')",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM system.main.duckdb_tables()",
        "SELECT * FROM pragma_storage_info('input')",
        "SELECT which_secret('object', 's3') FROM input",
        "SHOW TABLES",
        "SHOW input",
        "DESCRIBE input",
        "SUMMARIZE input",
    ],
)
def test_query_policy_denies_external_catalog_and_table_reference_shapes(sql):
    with pytest.raises(SQLPolicyError):
        validate_query(sql, 1)


def test_query_policy_enforces_input_arity_and_allows_read_only_ctes():
    for query in (
        "SELECT * FROM input0",
        "SELECT * FROM input1",
        "SELECT * FROM input01",
        "SELECT * FROM input2",
        "SELECT * FROM input3",
    ):
        with pytest.raises(SQLPolicyError):
            validate_query(query, 1 if not query.endswith("input3") else 2)
    assert validate_query("SELECT * FROM input UNION ALL SELECT * FROM input2", 2)
    assert validate_query("TABLE input", 1)
    assert validate_query("VALUES (1), (2)", 1)
    assert validate_query(
        "WITH a AS (SELECT * FROM input), b AS (SELECT * FROM a) SELECT * FROM b", 1
    )
    assert validate_query(
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 2) "
        "SELECT * FROM n",
        1,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM input) SELECT * FROM a",
        "WITH a AS (SELECT * FROM a) SELECT * FROM a",
        "WITH a AS (SELECT * FROM input) "
        "SELECT * FROM (WITH a AS (SELECT * FROM external) SELECT * FROM a) nested",
        "WITH input AS (SELECT 1) SELECT * FROM input",
    ],
)
def test_query_policy_validates_ctes_lexically_not_as_a_global_name_set(sql):
    with pytest.raises(SQLPolicyError):
        validate_query(sql, 1)


def test_query_policy_caps_bytes_and_query_nesting():
    with pytest.raises(SQLPolicyError, match="too large"):
        validate_query("SELECT '" + ("x" * MAX_SQL_BYTES) + "' FROM input", 1)
    with pytest.raises(SQLPolicyError, match="too large"):
        validate_fragment(FragmentKind.PREDICATE, "x=" + ("1" * MAX_SQL_BYTES))

    query = "SELECT * FROM input"
    for i in range(70):
        query = f"SELECT * FROM ({query}) AS q{i}"
    with pytest.raises(SQLPolicyError, match="nesting limit"):
        validate_query(query, 1)

    wide = "SELECT " + ",".join("0" for _ in range(6000)) + " FROM input"
    assert len(wide.encode()) < MAX_SQL_BYTES
    with pytest.raises(SQLPolicyError, match="AST exceeds"):
        validate_query(wide, 1)


def test_untrusted_parser_does_not_retain_unique_wide_ast_objects():
    # Parsed dict/list trees are much larger than the SQL text. Keeping 1024 attacker-selected ASTs
    # turned the byte limit into a process-memory retention budget, including queries later rejected.
    assert not hasattr(_parse_select, "cache_info")
    projection = ",".join(f"{index} AS c{index}" for index in range(300))
    for unique in range(40):
        with pytest.raises(SQLPolicyError, match="not allowed"):
            validate_query(
                f"SELECT {projection}, {unique} AS unique_value FROM forbidden_{unique}", 1
            )


def test_function_policy_allows_reviewed_core_and_denies_unsafe_or_unstable_calls():
    assert validate_query(
        "SELECT abs(x), lower(CAST(x AS VARCHAR)), sum(x) OVER (), row_number() OVER () FROM input",
        1,
    )
    for sql in (
        "SELECT pg_sleep(1) FROM input",
        "SELECT pg_get_viewdef(1) FROM input",
        "SELECT get_block_size(1) FROM input",
        "SELECT range(1) FROM input",
        "SELECT setseed(0.5) FROM input",
        "SELECT current_setting('threads') FROM input",
        "SELECT getvariable('x') FROM input",
        "SELECT current_localtime() FROM input",
        "SELECT current_localtimestamp() FROM input",
        "SELECT now() FROM input",
        "SELECT today() FROM input",
        "SELECT current_date FROM input",
    ):
        with pytest.raises(SQLPolicyError):
            validate_query(sql, 1)


def test_function_policy_rejects_existing_mixed_case_shadow_without_mutating_catalog():
    con = duckdb.connect()
    try:
        con.execute('CREATE MACRO "ABS"(x) AS x + 1000')
        before = con.execute(
            "SELECT count(*) FROM system.main.duckdb_functions() "
            "WHERE system.main.lower(function_name) = 'abs' AND NOT internal"
        ).fetchone()[0]
        with pytest.raises(SQLPolicyError, match="shadowed"):
            validate_query("SELECT abs(-2) FROM input", 1, con=con)
        after = con.execute(
            "SELECT count(*) FROM system.main.duckdb_functions() "
            "WHERE system.main.lower(function_name) = 'abs' AND NOT internal"
        ).fetchone()[0]
        assert before == after == 1
    finally:
        con.close()


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        (FragmentKind.PREDICATE, "x > 0 AND y IS NOT NULL"),
        (FragmentKind.JOIN_ON, "a.id = b.id"),
        (FragmentKind.PROJECTION, "x, abs(y) AS z"),
        (FragmentKind.ORDER_BY, "x DESC NULLS LAST"),
        (FragmentKind.GROUP_BY, "x, y"),
        (FragmentKind.AGGREGATES, "sum(x) AS total, count(*) AS n"),
        (FragmentKind.WINDOW_EXPR, "row_number()"),
        (FragmentKind.LITERAL, "'redacted'::VARCHAR"),
    ],
)
def test_fragment_policy_allows_expected_shapes(kind, fragment):
    assert validate_fragment(kind, fragment).sql == fragment


def test_join_key_extraction_uses_validated_ast_for_quoted_identifiers():
    assert join_equality_columns(
        'A."customer id" = B."id" AND b."tenant-id" = a."tenant""key"'
    ) == (["customer id", 'tenant"key'], ["id", "tenant-id"])
    assert join_equality_columns(
        "a.id = b.id AND TRUE AND lower(a.label) = lower(b.label)"
    ) == (["id"], ["id"])


@pytest.mark.parametrize(
    "condition",
    [
        "a.id = b.id OR a.tenant_id = b.tenant_id",
        "lower(a.id) = b.id",
        "a.id + 1 = b.id",
        "a.id <> b.id",
    ],
)
def test_join_key_extraction_declines_non_simple_conditions(condition):
    assert join_equality_columns(condition) is None


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        (FragmentKind.PREDICATE, "x IN (SELECT x FROM secret)"),
        (FragmentKind.JOIN_ON, "a.id IN (SELECT id FROM secret)"),
        (FragmentKind.PROJECTION, "x FROM secret"),
        (FragmentKind.PROJECTION, "x UNION SELECT x FROM secret"),
        (FragmentKind.PROJECTION, "* FROM range(1)"),
        (FragmentKind.PROJECTION, "* FROM (WITH x AS (SELECT 1) SELECT * FROM x) q"),
        (FragmentKind.PROJECTION, "x QUALIFY row_number() OVER () = 1"),
        (FragmentKind.ORDER_BY, "x LIMIT 1"),
        (FragmentKind.GROUP_BY, "x HAVING count(*) > 0"),
        (FragmentKind.AGGREGATES, "sum(x) FROM secret"),
        (FragmentKind.WINDOW_EXPR, "row_number() OVER ()"),
        (FragmentKind.LITERAL, "x"),
        (FragmentKind.LITERAL, "1, x"),
        (FragmentKind.LITERAL, "read_text('/etc/passwd')"),
    ],
)
def test_fragment_policy_rejects_subqueries_shape_escape_and_non_literals(kind, fragment):
    with pytest.raises(SQLPolicyError):
        validate_fragment(kind, fragment)


def test_identifier_policy_quotes_decodes_and_checks_ascii_membership():
    assert quote_identifier('a"b') == '"a""b"'
    assert identifier("NAME", ["name"]) == "name"
    assert identifier_list('"a""b", NAME', ['a"b', "name"]) == ['a"b', "name"]
    with pytest.raises(SQLPolicyError, match="not present"):
        identifier("missing", ["name"])
    with pytest.raises(SQLPolicyError, match="ambiguous"):
        identifier("name", ["name", "NAME"])
    with pytest.raises(SQLPolicyError, match="not present"):
        identifier("Ä", ["ä"])
    with pytest.raises(SQLPolicyError, match="unterminated"):
        identifier_list('"unfinished', ["unfinished"])
    with pytest.raises(SQLPolicyError, match="NUL"):
        quote_identifier("bad\x00name")


def test_identifier_allocator_avoids_existing_suffixes_and_ascii_fold_collisions():
    assert _dedupe_names(["name", "name", "name_2", "NAME"]) == [
        "name", "name_2", "name_2_2", "NAME_3",
    ]
    assert join_projection(["id", "name"], ["id", "name", "name_2"], ["id"]) == (
        '"id", a."name", b."name" AS "name_2", b."name_2" AS "name_2_2"'
    )
    with pytest.raises(SQLPolicyError, match="left join input schema is ambiguous"):
        join_projection(["Name", "name"], ["id"])
    with pytest.raises(SQLPolicyError, match="right join input schema is ambiguous"):
        join_projection(["id"], ["Value", "value"])


def test_chained_joins_increment_output_aliases_without_overwriting_name_2():
    with db.run_scope():
        con = db.conn()
        relations = {
            "mock://left": con.from_arrow(pa.table({"id": [1], "name": ["left"]})),
            "mock://right": con.from_arrow(pa.table({
                "id": [1], "name": ["right"], "name_2": ["right-existing"],
            })),
            "mock://third": con.from_arrow(pa.table({"id": [1], "name": ["third"]})),
        }

        class Adapter:
            def __init__(self, relation):
                self.relation = relation

            def scan(self, *_args, **_kwargs):
                return self.relation

        graph = Graph(**{
            "id": "chained-join-names",
            "version": 1,
            "nodes": [
                _node("left", "source", {"uri": "mock://left"}),
                _node("right", "source", {"uri": "mock://right"}),
                _node("third", "source", {"uri": "mock://third"}),
                _node("join1", "join", {"on": "id", "how": "inner"}),
                _node("join2", "join", {"on": "id", "how": "inner"}),
            ],
            "edges": [
                _edge("left", "join1"), _edge("right", "join1"),
                _edge("join1", "join2"), _edge("third", "join2"),
            ],
        })
        adapters = {uri: Adapter(relation) for uri, relation in relations.items()}
        joined = BuildEngine(
            graph, lambda uri: adapters[uri], SimpleNamespace(), full=True
        ).relation("join2")

        assert joined.columns == ["id", "name", "name_2", "name_2_2", "name_3"]
        assert joined.fetchall() == [(1, "left", "right", "right-existing", "third")]


def test_input_cte_binding_preserves_comments_semicolon_recursive_with_and_order():
    con = duckdb.connect()
    try:
        con.execute("CREATE VIEW source_view AS SELECT * FROM (VALUES (2), (1)) t(x)")
        ordered = validate_query("/* leading */ SELECT * FROM input ORDER BY x DESC;", 1, con=con)
        assert con.execute(bind_input_ctes(ordered, ["source_view"])).fetchall() == [(2,), (1,)]

        recursive = validate_query(
            "-- leading\nWITH RECURSIVE n(x) AS "
            "(SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 3) "
            "SELECT x FROM n ORDER BY x DESC;",
            1,
            con=con,
        )
        bound = bind_input_ctes(recursive, ["source_view"])
        assert bound.startswith("WITH RECURSIVE ")
        assert con.execute(bound).fetchall() == [(3,), (2,), (1,)]
    finally:
        con.close()


def test_build_engine_rejects_multistatement_before_file_or_catalog_mutation(tmp_path):
    target = tmp_path / "must-not-exist.parquet"
    with db.run_scope():
        con = db.conn()
        rel = con.from_arrow(pa.table({"x": [1]}))
        engine = _bound_engine(
            "sql",
            {"sql": f"SELECT * FROM input; COPY input TO '{target}'"},
            rel,
        )
        with pytest.raises(SQLPolicyError):
            engine.relation("target")
        assert not target.exists()

        engine = _bound_engine(
            "sql",
            {"sql": 'SELECT * FROM input; CREATE MACRO "DP_ESCAPE"(x) AS 999'},
            rel,
        )
        with pytest.raises(SQLPolicyError):
            engine.relation("target")
        assert con.execute(
            "SELECT count(*) FROM system.main.duckdb_functions() "
            "WHERE system.main.lower(function_name) = 'dp_escape'"
        ).fetchone()[0] == 0


def test_source_pushdown_validates_predicate_before_adapter_scan():
    graph = Graph(**{
        "id": "pushdown-policy",
        "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "mock://data"}),
            _node("filter", "filter", {"predicate": "x IN (SELECT x FROM secret)"}),
        ],
        "edges": [_edge("source", "filter")],
    })
    calls = []

    class Adapter:
        def scan(self, *_args, **_kwargs):
            calls.append(True)
            raise AssertionError("adapter.scan must not run before pushdown policy validation")

    with db.run_scope(), pytest.raises(SQLPolicyError):
        BuildEngine(
            graph,
            lambda _uri: Adapter(),
            SimpleNamespace(),
            full=True,
            pushdown=True,
            output_node="filter",
        ).relation("source")
    assert calls == []


def test_sdk_sql_uses_bare_input_without_rewriting_literals_or_comments():
    with db.run_scope():
        rel = db.conn().from_arrow(pa.table({"x": [1]}))
        result = ctx.sql(
            rel,
            "SELECT x, '{input}' AS literal FROM input -- {input} remains a comment token\n",
        )
        assert result.fetchall() == [(1, "{input}")]
        with pytest.raises(SQLPolicyError):
            ctx.sql(rel, "SELECT * FROM read_text('/etc/passwd')")


def test_run_scope_snapshot_fences_policy_check_through_lazy_bind():
    base = db._base_conn()
    cleanup = base.cursor()
    try:
        existing = cleanup.execute(
            "SELECT count(*) FROM system.main.duckdb_functions() "
            "WHERE system.main.lower(function_name) = 'abs' AND NOT internal"
        ).fetchone()[0]
        if existing:
            cleanup.execute('DROP MACRO "ABS"')

        with db.run_scope():
            con = db.conn()
            validate_query("SELECT abs(-2) FROM input", 1, con=con)
            lazy = con.sql("SELECT abs(-2)")
            mutator = base.cursor()
            try:
                mutator.execute('CREATE MACRO "ABS"(x) AS x + 1000')
                assert lazy.fetchall() == [(2,)]
            finally:
                mutator.close()
    finally:
        try:
            cleanup.execute('DROP MACRO "ABS"')
        except Exception:
            pass
        cleanup.close()


def test_run_scope_disables_python_replacements_and_fixes_search_path():
    with db.run_scope():
        con = db.conn()
        assert con.execute("SELECT current_setting('python_enable_replacements')").fetchone() == (False,)
        assert con.execute("SELECT current_setting('search_path')").fetchone() == ("main",)


def test_identifier_quoting_prevents_join_and_dedup_schema_name_sql_escape(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET_SHOULD_NOT_APPEAR")
    evil = f'content" FROM read_text(\'{secret}\') AS a --'

    with db.run_scope():
        con = db.conn()
        left = con.from_arrow(pa.table({"id": [1], evil: ["safe-value"]}))
        right = con.from_arrow(pa.table({"id": [1], "right_value": ["joined"]}))

        class Adapter:
            def __init__(self, rel):
                self.rel = rel

            def scan(self, *_args, **_kwargs):
                return self.rel

        graph = Graph(**{
            "id": "quoted-join",
            "version": 1,
            "nodes": [
                _node("left", "source", {"uri": "mock://left"}),
                _node("right", "source", {"uri": "mock://right"}),
                _node("join", "join", {"on": "id", "how": "inner"}),
            ],
            "edges": [_edge("left", "join"), _edge("right", "join")],
        })
        adapters = {"mock://left": Adapter(left), "mock://right": Adapter(right)}
        joined = BuildEngine(
            graph, lambda uri: adapters[uri], SimpleNamespace(), full=True
        ).relation("join")
        assert joined.columns == ["id", evil, "right_value"]
        assert joined.fetchall() == [(1, "safe-value", "joined")]

        dedup = _bound_engine("dedup", {"on": quote_identifier(evil)}, left).relation("target")
        values = [str(value) for row in dedup.fetchall() for value in row]
        assert "safe-value" in values
        assert "TOP_SECRET_SHOULD_NOT_APPEAR" not in values


def _load_dp_ray():
    path = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_ray" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_ray_sqlpolicy_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ray_driver_and_worker_helpers_revalidate_user_fragments(monkeypatch):
    module = _load_dp_ray()
    schema = pa.schema([("k", pa.int64()), ("v", pa.int64())])
    allowed = module._duckdb_empty_result_schema(
        'SELECT "k", sum("v") AS total FROM "_blk" GROUP BY "k"',
        policy_fragments=(
            (FragmentKind.GROUP_BY.value, '"k"'),
            (FragmentKind.AGGREGATES.value, 'sum("v") AS total'),
        ),
        _blk=schema,
    )
    assert allowed.names == ["k", "total"]

    with pytest.raises(SQLPolicyError):
        module._duckdb_empty_result_schema(
            'SELECT * FROM "_blk"',
            policy_fragments=((FragmentKind.AGGREGATES.value, "pg_sleep(1)"),),
            _blk=schema,
        )

    class Data:
        def materialize(self):
            return self

        def num_blocks(self):
            return 1

        def schema(self, fetch_if_missing=True):
            return schema

        def repartition(self, *_args, **_kwargs):
            return self

        def map_batches(self, fn, **_kwargs):
            fn(pa.table({"k": [1], "v": [2]}))
            return self

    monkeypatch.setenv("DP_RAY_SHUFFLE_PARTITIONS", "1")
    runner = object.__new__(module.RayRunner)
    with pytest.raises(SQLPolicyError):
        runner._shuffle_duckdb(
            Data(),
            ["k"],
            'SELECT * FROM "_blk"',
            policy_fragments=((FragmentKind.AGGREGATES.value, "pg_sleep(1)"),),
        )


def test_ray_concurrent_preflights_use_independent_closed_policy_connections(monkeypatch):
    import concurrent.futures
    import threading

    module = _load_dp_ray()
    real_secure_connection = module._secure_duckdb_connection
    created = []
    created_lock = threading.Lock()

    def tracked_secure_connection():
        con = real_secure_connection()
        with created_lock:
            created.append(con)
        return con

    monkeypatch.setattr(module, "_secure_duckdb_connection", tracked_secure_connection)
    steps = [
        SimpleNamespace(
            id="aggregate", op="aggregate", inputs=[("source", None)],
            config={"groupBy": "k", "aggs": "sum(v) AS total"},
        ),
        SimpleNamespace(
            id="window", op="window", inputs=[("aggregate", None)],
            config={"partitionBy": "k", "orderBy": "total", "expr": "row_number()"},
        ),
        SimpleNamespace(
            id="join", op="join", inputs=[("window", None), ("right", None)],
            config={"condition": "a.k = b.k", "how": "inner"},
        ),
    ]

    class IR:
        def __init__(self):
            self.steps = steps

        def is_distributable(self, _relational):
            return True

    runner = object.__new__(module.RayRunner)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        reasons = list(pool.map(lambda _index: runner._ray_unsupported_reason(IR()), range(16)))

    assert reasons == [None] * 16
    assert len(created) == 16
    for con in created:
        with pytest.raises(duckdb.Error):
            con.execute("SELECT 1")


def test_policy_version_and_cache_namespace_are_explicit_security_boundaries():
    assert duckdb.__version__ == SUPPORTED_DUCKDB_VERSION
    _require_supported_version(SUPPORTED_DUCKDB_VERSION)
    with pytest.raises(RuntimeError, match="Review the AST policy"):
        _require_supported_version("9.9.9")
    assert CACHE_SCHEMA_VERSION == 3
