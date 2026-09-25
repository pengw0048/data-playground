"""Retained editor evidence describes actual Python inputs, including failed code."""

from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pytest

from hub import db, sandbox
from hub.executors.preview import preview_node
from hub.models import Graph


def _graph(*, code="def fn(row):\n    return row", mode="map", fmt="rows"):
    return Graph.model_validate({
        "id": "editor-input-sample", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": "test://retained-input"}}},
            {"id": "transform", "type": "transform", "position": {"x": 200, "y": 0},
             "data": {"config": {
                 "source": "adhoc", "code": code, "mode": mode,
                 "batchFormat": fmt, "onError": "raise",
             }}},
        ],
        "edges": [{"id": "input", "source": "source", "target": "transform",
                   "sourceHandle": "out", "targetHandle": "in", "data": {"wire": "dataset"}}],
    })


def _preview(table, *, capture=True, one_shot=False, **config):
    class Adapter:
        scans = 0

        def fingerprint(self, _uri):
            return "retained"

        def preview_scan(self, _uri, *, limit, **_kwargs):
            self.scans += 1
            bounded = table.slice(0, limit)
            source = (pa.RecordBatchReader.from_batches(bounded.schema, bounded.to_batches())
                      if one_shot else bounded)
            return db.conn().from_arrow(source)

        def scan(self, *_args, **_kwargs):
            raise AssertionError("editor evidence must not introduce a full scan")

    adapter = Adapter()
    result = preview_node(
        _graph(**config), "transform", 2, lambda _uri: adapter, object(),
        capture_editor_input=capture,
    )
    assert adapter.scans == 1
    return result


def test_rows_preserve_decimal_none_and_input_before_user_mutation(monkeypatch):
    exact = Decimal("12345678901234567890.123456789012345678")
    table = pa.table({"value": pa.array([exact, None], type=pa.decimal128(38, 18))})
    calls = []

    def fn(row):
        calls.append(row["value"])
        row["value"] = "changed by code"
        return row

    monkeypatch.setattr(sandbox, "compile_operator", lambda _code, _mode: fn)
    result = _preview(table)
    assert not result.error, result.reason
    assert calls == [exact, None], "capturing evidence must not call user code again"
    sample = result.editor_input_sample
    assert sample.container_type == "builtins.dict"
    assert sample.rows[0]["value"].python_type == "decimal.Decimal"
    assert sample.rows[0]["value"].representation == repr(exact)
    assert not sample.rows[0]["value"].truncated
    assert sample.rows[1]["value"].python_type == "builtins.NoneType"
    assert sample.rows[1]["value"].representation == "None"
    assert result.rows == [{"value": "changed by code"}] * 2


@pytest.mark.parametrize(("code", "category"), [
    ("def fn(row)\n    return row", "syntax_error"),
    ("def fn(row):\n    return row['missing']", "user_code_exception"),
])
def test_input_survives_syntax_and_execution_errors(code, category):
    result = _preview(pa.table({"value": [1, 2, 3]}), code=code)
    assert result.error and result.failure_category == category
    assert result.editor_input_sample.rows[0]["value"].representation == "1"
    assert len(result.editor_input_sample.rows) == 3


def test_empty_input_has_columns_but_no_invented_value_types():
    table = pa.table({"value": pa.array([], type=pa.decimal128(38, 18))})
    result = _preview(table, code="def fn(row)\n    return row")
    assert result.failure_category == "syntax_error"
    assert result.editor_input_sample.columns == ["value"]
    assert result.editor_input_sample.rows == []


def test_samples_bound_rows_columns_and_each_representation():
    table = pa.table({f"field_{i}": ["x" * 1000] * 9 for i in range(23)})
    result = _preview(table)
    assert not result.error, result.reason
    sample = result.editor_input_sample
    assert sample.row_limit == len(sample.rows) == 5
    assert sample.column_count == 23 and len(sample.columns) == 20
    assert all(len(row) == 20 for row in sample.rows)
    assert all(cell.truncated and len(cell.representation) <= 300
               for row in sample.rows for cell in row.values())


def test_large_arrow_and_numpy_values_never_build_an_unbounded_repr(monkeypatch):
    np = pytest.importorskip("numpy")
    from hub.executors import editor_input

    ordinary_repr = repr

    def bounded_repr(value):
        assert not isinstance(value, (pa.Scalar, np.ndarray)), "must not stringify the full container"
        if isinstance(value, (str, bytes)):
            assert len(value) <= 300, "string/binary payload must be bounded before repr"
        return ordinary_repr(value)

    monkeypatch.setattr(editor_input, "repr", bounded_repr, raising=False)
    values = [
        pa.scalar("hé🙂" * 100_000), pa.scalar(b"x" * 100_000),
        pa.scalar(list(range(2000))), pa.scalar({"values": list(range(2000))}),
        np.arange(2000), np.array([1, 2, 3]),
        np.str_("x" * 100_000), np.bytes_(b"x" * 100_000),
    ]
    for value in values:
        cell = editor_input._cell(value)
        assert cell.truncated
        assert len(cell.representation) <= 300
        assert "�" not in cell.representation
    assert "values omitted" in editor_input._cell(np.array([1, 2, 3])).representation


def test_pandas_nested_array_summary_is_honestly_truncated_without_mutating_input(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    values = list(range(2000))
    table = pa.table({"values": [values]})
    calls = []

    def fn(batch):
        actual = batch.iat[0, 0]
        assert isinstance(actual, np.ndarray)
        assert actual.tolist() == values
        calls.append(batch)
        return batch

    monkeypatch.setattr(sandbox, "compile_operator", lambda _code, _mode: fn)
    result = _preview(table, mode="map_batches", fmt="pandas")
    assert not result.error, result.reason
    assert len(calls) == 1 and result.rows == [{"values": values}]
    cell = result.editor_input_sample.rows[0]["values"]
    assert cell.python_type == "numpy.ndarray" and cell.truncated
    assert "shape=(2000,)" in cell.representation and "values omitted" in cell.representation


@pytest.mark.parametrize("values", [[], [1, 2, 3]])
def test_empty_code_reuses_prepared_one_shot_input_and_schema(values):
    table = pa.table({"value": pa.array(values, type=pa.int64())})
    result = _preview(table, code="", one_shot=True)
    assert not result.error, result.reason
    assert result.rows == [{"value": value} for value in values[:2]]
    assert [column.name for column in result.columns] == ["value"]
    assert result.editor_input_sample.columns == ["value"]
    assert len(result.editor_input_sample.rows) == len(values)


@pytest.mark.parametrize("fmt", ["rows", "pandas", "arrow"])
def test_batch_samples_use_the_exact_complete_container_passed_to_code(monkeypatch, fmt):
    if fmt == "pandas":
        pytest.importorskip("pandas")
    exact = Decimal("12345678901234567890.123456789012345678")
    # The sixth row's null makes the ACTUAL pandas integer column float64. Converting only
    # the five displayed rows would wrongly report numpy.int64 for its first value.
    table = pa.table({
        "nullable": pa.array([1, 2, 3, 4, 5, None], type=pa.int64()),
        "precise": pa.array([exact] * 6, type=pa.decimal128(38, 18)),
    })
    calls = []

    def fn(batch):
        calls.append(batch)
        return batch

    monkeypatch.setattr(sandbox, "compile_operator", lambda _code, _mode: fn)
    result = _preview(table, mode="map_batches", fmt=fmt)
    assert not result.error, result.reason
    assert len(calls) == 1
    sample = result.editor_input_sample
    assert sample.format == fmt and len(sample.rows) == 5
    if fmt == "rows":
        value = calls[0][0]["precise"]
        assert sample.container_type == "builtins.list"
    elif fmt == "pandas":
        value = calls[0].iat[0, 1]
        assert sample.container_type == (
            f"{type(calls[0]).__module__}.{type(calls[0]).__qualname__}")
        assert sample.rows[0]["nullable"].python_type == "numpy.float64"
    else:
        value = calls[0].column(1)[0]
        assert sample.container_type == "pyarrow.lib.Table"
    assert sample.rows[0]["precise"].python_type == (
        f"{type(value).__module__}.{type(value).__qualname__}")
    assert sample.rows[0]["precise"].representation == repr(value)


def test_ordinary_preview_does_not_capture_editor_input():
    result = _preview(pa.table({"value": [1]}), capture=False)
    assert not result.error and result.editor_input_sample is None


def test_kernel_transport_forwards_capture_only_when_requested(monkeypatch):
    from hub import kernel_backend
    from hub.nodespecs import BUILTIN_NODE_SPECS
    from hub.kernel import PreviewBody

    backend = kernel_backend.KernelBackend(
        SimpleNamespace(node_specs={spec.kind: spec for spec in BUILTIN_NODE_SPECS}), object())
    monkeypatch.setattr(backend, "_ensure_kernel", lambda _canvas: ("localhost:1234", "token"))
    bodies = []

    def post(_endpoint, _path, _token, body):
        bodies.append(PreviewBody.model_validate(body))
        return {}

    monkeypatch.setattr(kernel_backend, "_post", post)
    backend.preview(_graph(), "transform", 2, 0, "out", capture_editor_input=True)
    backend.preview(_graph(), "transform", 2, 0, "out")
    assert [body.capture_editor_input for body in bodies] == [True, False]
