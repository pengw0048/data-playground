from __future__ import annotations

import importlib.util
from pathlib import Path

import pyarrow as pa
import pytest

from hub import db
from hub.backends import DatasetAdapter, DatasetPreviewAdapter
from hub.estimate import estimate_sizes
from hub.executors.engine import BuildEngine, NotPreviewable
from hub.models import Graph, SampleRequest
from hub.plugins.adapters import DuckDBAdapter, LanceAdapter


def _node(node_id: str, kind: str, config: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "position": {"x": 0, "y": 0},
        "data": {"config": config or {}},
    }


def _edge(source: str, target: str, target_handle: str | None = None) -> dict:
    edge = {
        "id": f"{source}-{target}-{target_handle or 'in'}",
        "source": source,
        "target": target,
        "data": {"wire": "dataset"},
    }
    if target_handle is not None:
        edge["targetHandle"] = target_handle
    return edge


class _BoundedScanSpy:
    def __init__(self) -> None:
        self.scan_limits: list[int | None] = []
        self.nearest_calls = 0

    def scan(self, _uri: str, *, limit: int | None = None, **_kwargs):
        self.scan_limits.append(limit)
        return db.conn().from_arrow(pa.table({
            "id": list(range(20)),
            "value": [float(i) if i % 3 else None for i in range(20)],
            "embedding": [[float(i), 1.0] for i in range(20)],
        }))

    def preview_scan(self, uri: str, *, limit: int = 2000, **kwargs):
        return self.scan(uri, limit=limit, **kwargs)

    def nearest(self, *_args, **_kwargs):
        self.nearest_calls += 1
        raise AssertionError("interactive preview must not dispatch an unproven ANN/flat scan")


def _engine(graph: Graph, adapter: _BoundedScanSpy) -> BuildEngine:
    return BuildEngine(
        graph, lambda _uri: adapter, object(), sample_k=7, full=False,
    )


@pytest.mark.parametrize(
    ("kind", "config", "two_inputs"),
    [
        ("sample", {"n": 3, "seed": 1}, False),
        ("sort", {"by": "id DESC"}, False),
        ("window", {"expr": "row_number()", "orderBy": "id", "as": "rank"}, False),
        ("fill", {"columns": "value", "method": "mean"}, False),
        ("sql", {"sql": "SELECT * FROM input ORDER BY id DESC"}, False),
        ("sql", {"sql": "SELECT a.id FROM input a JOIN input2 b USING (id)"}, True),
        ("join", {"on": "id", "how": "inner"}, True),
        ("metric", {"agg": "count"}, False),
        ("chart", {"x": "id", "agg": "count"}, False),
        ("vector-search", {"column": "embedding", "queryVector": [1.0, 1.0], "k": 3}, False),
    ],
)
def test_preview_full_pass_operators_never_issue_an_unbounded_source_scan(
    kind: str, config: dict, two_inputs: bool,
) -> None:
    adapter = _BoundedScanSpy()
    nodes = [_node("source-a", "source", {"uri": "spy://a"})]
    edges = [_edge("source-a", "target", "a" if kind == "join" else None)]
    if two_inputs:
        nodes.append(_node("source-b", "source", {"uri": "spy://b"}))
        edges.append(_edge("source-b", "target", "b" if kind == "join" else None))
    nodes.append(_node("target", kind, config))
    graph = Graph(id=f"preview-boundary-{kind}", version=1, nodes=nodes, edges=edges)

    with db.run_scope(), pytest.raises(NotPreviewable, match="full pass"):
        _engine(graph, adapter).relation("target")

    assert adapter.scan_limits
    assert set(adapter.scan_limits) == {7}, "preview source scans must carry the adapter limit"
    assert adapter.nearest_calls == 0


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        ("source", {"uri": "spy://source"}),
        ("filter", {"predicate": "id >= 0"}),
        ("select", {"expr": "id, value"}),
        ("fill", {"columns": "value", "method": "zero"}),
        ("chart", {"x": "id", "y": "value", "agg": "none"}),
    ],
)
def test_bounded_preview_paths_push_the_budget_into_the_adapter(
    kind: str, config: dict,
) -> None:
    adapter = _BoundedScanSpy()
    if kind == "source":
        nodes = [_node("target", "source", config)]
        edges = []
    else:
        nodes = [
            _node("source", "source", {"uri": "spy://source"}),
            _node("target", kind, config),
        ]
        edges = [_edge("source", "target")]
    graph = Graph(id=f"bounded-preview-{kind}", version=1, nodes=nodes, edges=edges)

    with db.run_scope():
        table = _engine(graph, adapter).relation("target").limit(7).to_arrow_table()

    assert table.num_rows <= 7
    assert adapter.scan_limits == [7]


def test_durable_write_consumes_a_one_shot_arrow_relation_only_once(tmp_path) -> None:
    table = pa.table({"id": [1, 2, 3]})
    reader = pa.RecordBatchReader.from_batches(table.schema, table.to_batches())
    output = str(tmp_path / "one-shot.parquet")

    with db.run_scope():
        result = DuckDBAdapter().write(output, db.conn().from_arrow(reader))
        written = DuckDBAdapter().scan(output).to_arrow_table()

    assert result["rows"] == 3
    assert written.to_pylist() == table.to_pylist()


def test_lance_write_consumes_a_one_shot_arrow_relation_only_once(tmp_path) -> None:
    pytest.importorskip("lance")
    table = pa.table({"id": [1, 2, 3], "value": ["a", "b", "c"]})
    reader = pa.RecordBatchReader.from_batches(table.schema, table.to_batches(max_chunksize=1))
    output = str(tmp_path / "one-shot.lance")
    adapter = LanceAdapter()

    with db.run_scope():
        result = adapter.write(output, db.conn().from_arrow(reader))
        written = adapter.scan(output).to_arrow_table()

    assert result["rows"] == 3
    assert written.to_pylist() == table.to_pylist()


def test_remote_ipc_source_preview_refuses_before_opening_the_object_store(monkeypatch) -> None:
    adapter = DuckDBAdapter()
    opened: list[str] = []
    monkeypatch.setattr("hub.plugins.adapters.object_fs", lambda uri: opened.append(uri))
    graph = Graph(
        id="remote-ipc-preview", version=1,
        nodes=[_node("source", "source", {"uri": "s3://bucket/data.feather"})],
        edges=[],
    )

    with db.run_scope(), pytest.raises(NotPreviewable, match="no strict bounded preview"):
        _engine(graph, adapter).relation("source")

    assert opened == []


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("s3://bucket/partitioned-prefix", "object-store prefixes"),
        ("s3://bucket/**/*.parquet", "glob sources"),
    ],
)
def test_object_namespace_preview_refuses_before_listing(monkeypatch, uri, reason) -> None:
    monkeypatch.setattr(
        "hub.db.ensure_object_store",
        lambda: pytest.fail("preview initialized or listed the object namespace"),
    )

    with pytest.raises(NotPreviewable, match=reason):
        graph = Graph(
            id="object-prefix-preview", version=1,
            nodes=[_node("source", "source", {"uri": uri})], edges=[],
        )
        with db.run_scope():
            BuildEngine(
                graph, lambda _uri: DuckDBAdapter(), object(), sample_k=7, full=False,
            ).relation("source")


def test_local_directory_preview_refuses_before_recursive_glob(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "partitioned"
    (directory / "partition=1").mkdir(parents=True)
    monkeypatch.setattr(
        "hub.plugins.adapters.glob.glob",
        lambda *_args, **_kwargs: pytest.fail("preview recursively enumerated a local directory"),
    )
    graph = Graph(
        id="local-directory-preview", version=1,
        nodes=[_node("source", "source", {"uri": str(directory)})], edges=[],
    )

    with db.run_scope(), pytest.raises(NotPreviewable, match="directory datasets"):
        _engine(graph, DuckDBAdapter()).relation("source")


def test_local_ipc_preview_fails_closed_before_opening_a_record_batch(tmp_path, monkeypatch) -> None:
    path = tmp_path / "single-batch.arrow"
    path.touch()
    monkeypatch.setattr(
        "hub.plugins.adapters._read_ipc",
        lambda *_args, **_kwargs: pytest.fail("preview opened an unbounded IPC record batch"),
    )
    graph = Graph(
        id="local-ipc-preview", version=1,
        nodes=[_node("source", "source", {"uri": str(path)})], edges=[],
    )

    with db.run_scope(), pytest.raises(NotPreviewable, match="no strict bounded preview"):
        _engine(graph, DuckDBAdapter()).relation("source")


def test_adapter_without_explicit_preview_capability_fails_closed() -> None:
    class FullOnlyAdapter:
        name = "full-only"

        def scan(self, _uri: str, **_kwargs):
            raise AssertionError("preview must not call the full-run scan method")

    graph = Graph(
        id="full-only-adapter", version=1,
        nodes=[_node("source", "source", {"uri": "full-only://data"})],
        edges=[],
    )

    with db.run_scope(), pytest.raises(NotPreviewable, match="does not guarantee a bounded preview"):
        _engine(graph, FullOnlyAdapter()).relation("source")


@pytest.mark.parametrize(
    ("plugin", "adapter_name", "loader_name", "uri"),
    [
        ("dp_hf_datasets", "HfDatasetsAdapter", "_load_arrow", "hf://org/dataset:train"),
        ("dp_iceberg", "IcebergAdapter", "_to_arrow", "iceberg://prod/db.table"),
    ],
)
def test_full_run_reference_adapters_omit_preview_and_fingerprint_without_loading(
        monkeypatch, plugin, adapter_name, loader_name, uri) -> None:
    source = Path(__file__).resolve().parents[3] / "examples" / "plugins" / plugin / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"{plugin}_preview_contract", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = getattr(module, adapter_name)()
    monkeypatch.setattr(
        module, loader_name,
        lambda *_args, **_kwargs: pytest.fail("fingerprint loaded source data"),
    )

    assert isinstance(adapter, DatasetAdapter)
    assert not isinstance(adapter, DatasetPreviewAdapter)
    assert adapter.fingerprint(uri) == adapter.fingerprint(uri)


def test_data_sample_uses_only_preview_and_metadata_capabilities(monkeypatch) -> None:
    from hub.routers import catalog as catalog_routes

    calls: list[tuple[str, int | None]] = []

    class Adapter:
        name = "sample-spy"

        def preview_scan(self, _uri: str, _columns=None, *, limit: int = 2000):
            calls.append(("preview", limit))
            return db.conn().from_arrow(pa.table({"id": [0, 1, 2, 3]})).limit(limit)

        def scan(self, *_args, **_kwargs):
            raise AssertionError("data sample must not call full-run scan")

        def count(self, *_args, **_kwargs):
            raise AssertionError("data sample must not call potentially full-scanning count")

    monkeypatch.setattr(
        catalog_routes, "get_deps",
        lambda: type("Deps", (), {"storage": None, "resolve_adapter": lambda _self, _uri: Adapter()})(),
    )

    result = catalog_routes.data_sample(SampleRequest(uri="sample-spy://data", k=2))

    assert calls == [("preview", 3)]
    assert result.rows == [{"id": 0}, {"id": 1}]
    assert result.has_more is True
    assert result.row_count is None


def test_data_sample_enforces_a_fixed_source_work_budget(monkeypatch) -> None:
    from fastapi import HTTPException
    from hub.routers import catalog as catalog_routes

    calls: list[int] = []

    class Adapter:
        name = "budget-spy"

        def preview_scan(self, _uri: str, _columns=None, *, limit: int = 2000):
            calls.append(limit)
            return db.conn().sql(f"SELECT i AS id FROM range({limit}) rows(i)")

        @staticmethod
        def metadata_count(_uri: str) -> int:
            return 5_000

    monkeypatch.setattr(
        catalog_routes, "get_deps",
        lambda: type("Deps", (), {"storage": None, "resolve_adapter": lambda _self, _uri: Adapter()})(),
    )
    budget = catalog_routes.DATA_SAMPLE_PREVIEW_ROW_BUDGET

    penultimate = catalog_routes.data_sample(SampleRequest(
        uri="budget-spy://data", offset=budget - 100, k=50,
    ))
    assert calls == [budget - 49]
    assert penultimate.rows[0] == {"id": budget - 100}
    assert penultimate.rows[-1] == {"id": budget - 51}
    assert penultimate.has_more is True

    final_page = catalog_routes.data_sample(SampleRequest(
        uri="budget-spy://data", offset=budget - 50, k=50,
    ))
    assert calls == [budget - 49, budget]
    assert final_page.rows[0] == {"id": budget - 50}
    assert final_page.rows[-1] == {"id": budget - 1}
    assert final_page.row_count == 5_000
    assert final_page.has_more is False, "metadata outside the preview window must not enable Next"

    with pytest.raises(HTTPException) as exc_info:
        catalog_routes.data_sample(SampleRequest(
            uri="budget-spy://data", offset=budget, k=50,
        ))
    assert exc_info.value.status_code == 400
    assert "interactive window" in str(exc_info.value.detail)
    assert calls == [budget - 49, budget], "out-of-window requests must be rejected before scanning"


def test_unknown_source_estimate_never_calls_full_count() -> None:
    class Adapter:
        def fingerprint(self, _uri: str) -> str:
            return "stable"

        def count(self, _uri: str) -> int:
            raise AssertionError("estimate must not issue a full source count")

    graph = Graph(
        id="unknown-estimate", version=1,
        nodes=[_node("source", "source", {"uri": "csv-spy://data"})],
        edges=[],
    )

    estimate = estimate_sizes(graph, lambda _uri: Adapter(), target="source")

    assert estimate["source"].rows is None
    assert estimate["source"].confidence == "unknown"


def test_partitioned_directory_metadata_count_does_not_enumerate_files(
        tmp_path, monkeypatch) -> None:
    directory = tmp_path / "million-partition-dataset"
    (directory / "partition=1").mkdir(parents=True)
    monkeypatch.setattr(
        "hub.plugins.adapters.glob.glob",
        lambda *_args, **_kwargs: pytest.fail("metadata_count enumerated a directory namespace"),
    )

    assert DuckDBAdapter().metadata_count(str(directory)) is None


def test_remote_ipc_schema_only_reads_no_record_batches(tmp_path, monkeypatch) -> None:
    path = tmp_path / "remote.feather"
    table = pa.table({"id": [1, 2, 3], "value": ["a", "b", "c"]})
    with pa.OSFile(str(path), "wb") as sink:
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)

    class Filesystem:
        @staticmethod
        def open_input_file(_path: str):
            return pa.memory_map(str(path), "r")

    monkeypatch.setattr("hub.plugins.adapters.object_fs", lambda _uri: (Filesystem(), "remote.feather"))
    monkeypatch.setattr(
        "pyarrow.feather.read_table",
        lambda *_args, **_kwargs: pytest.fail("schema-only scan eagerly read remote IPC rows"),
    )

    with db.run_scope():
        relation = DuckDBAdapter().scan("s3://bucket/remote.feather", limit=0)
        assert relation.columns == ["id", "value"]
        assert relation.to_arrow_table().num_rows == 0
