from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pytest

from hub import db, relationships
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter
from hub.sqlpolicy import SQLPolicyError


def test_duckdb_adapter_projection_quotes_and_checks_schema(tmp_path):
    source = str(tmp_path / "quoted.parquet")
    duckdb.connect().execute(
        f'''COPY (SELECT 7 AS "a""b", 9 AS "region,code") TO '{source}' (FORMAT PARQUET)'''
    )

    with db.run_scope():
        adapter = DuckDBAdapter()
        rel = adapter.scan(source, columns=['a"b', "region,code"])
        assert rel.columns == ['a"b', "region,code"]
        assert rel.fetchall() == [(7, 9)]
        with pytest.raises(SQLPolicyError, match="not present"):
            adapter.scan(source, columns=['a"b FROM read_text'])


def test_partition_by_parses_identifiers_and_quotes_real_column(tmp_path):
    target = str(tmp_path / "partitioned.parquet")
    with db.run_scope():
        con = db.conn()
        rel = con.sql('''SELECT * FROM (VALUES ('east', 1), ('west', 2)) t("a""b", value)''')
        adapter = DuckDBAdapter()
        uri = adapter.write(target, rel, partition_by='"a""b"')["uri"]
        result = adapter.scan(uri).order("value").fetchall()
        # Hive partition columns are reconstructed from the path and therefore appear after file columns.
        assert result == [(1, "east"), (2, "west")]
        with pytest.raises(SQLPolicyError, match="not present"):
            adapter.write(str(tmp_path / "bad.parquet"), rel, partition_by="value, missing")


def test_relationship_measurement_quotes_embedded_identifier(tmp_path):
    source = str(tmp_path / "relationships.parquet")
    duckdb.connect().execute(
        f'''COPY (SELECT * FROM (VALUES (1), (2)) t("a""b")) TO '{source}' (FORMAT PARQUET)'''
    )
    measured = relationships.measure_unique(source, ['a"b'], lambda _uri: DuckDBAdapter())
    assert measured == (True, 2)


def test_example_plugin_canonicalizes_and_quotes_configured_column():
    plugin_path = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_example" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_example_identifier_test", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured = {}

    class CaptureCtx:
        def sql(self, rel, query):
            captured["rel"] = rel
            captured["query"] = query
            return query

    module.ctx = CaptureCtx()
    rel = SimpleNamespace(columns=['na"me'])
    node = SimpleNamespace(data={"config": {"column": 'NA"ME', "keep": 1}})
    module.build(None, node, [rel])
    assert captured["rel"] is rel
    assert 'CAST("na""me" AS VARCHAR)' in captured["query"]
    assert 'AS "na""me"' in captured["query"]

    node.data["config"]["column"] = 'na"me) FROM secret --'
    with pytest.raises(SQLPolicyError, match="not present"):
        module.build(None, node, [rel])


def test_lance_projection_checks_schema_in_native_and_fallback_paths(tmp_path):
    lance = pytest.importorskip("lance")
    pa = pytest.importorskip("pyarrow")
    source = str(tmp_path / "quoted.lance")
    lance.write_dataset(pa.table({'a"b': [1, 2, 3], "other": [3, 2, 1]}), source)

    with db.run_scope():
        adapter = LanceAdapter()
        assert adapter.scan(source, columns=['a"b']).fetchall() == [(1,), (2,), (3,)]
        fallback = adapter.scan(source, columns=['a"b'], predicate='"a""b" > 1')
        assert fallback.fetchall() == [(2,), (3,)]
        with pytest.raises(SQLPolicyError, match="not present"):
            adapter.scan(source, columns=["missing"])


@pytest.mark.parametrize(
    ("plugin", "adapter_name", "loader_name", "uri"),
    [
        ("dp_iceberg", "IcebergAdapter", "_to_arrow", "iceberg://mock/db.table"),
        ("dp_hf_datasets", "HfDatasetsAdapter", "_load_arrow", "hf://mock/data"),
    ],
)
def test_reference_adapters_quote_projection_without_optional_dependencies(
    monkeypatch, plugin, adapter_name, loader_name, uri
):
    plugin_path = Path(__file__).resolve().parents[3] / "examples" / "plugins" / plugin / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"{plugin}_identifier_test", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        loader_name,
        lambda _uri: pa.table({'a"b': [1, 2], "other": [3, 4]}),
    )

    with db.run_scope():
        adapter = getattr(module, adapter_name)()
        projected = adapter.scan(uri, columns=['A"B'])
        assert projected.columns == ['a"b']
        assert projected.fetchall() == [(1,), (2,)]
        with pytest.raises(SQLPolicyError, match="not present"):
            adapter.scan(uri, columns=['a"b FROM secret'])
