from __future__ import annotations

import importlib.util
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from hub import db
from hub.backends import DatasetAdapter, DatasetPreviewAdapter
from hub.estimate import estimate_sizes
from hub.executors.engine import BuildEngine, NotPreviewable
from hub.executors.preview import _reservoir_preview_allowed
from hub.models import ColumnProfile, Graph, ProfileResult, SampleRequest, SampleResult
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


def test_reservoir_preview_does_not_turn_a_remote_duckdb_source_into_a_full_scan() -> None:
    graph = Graph.model_validate({
        "id": "remote-reservoir-preview", "version": 1,
        "nodes": [
            _node("source", "source", {"uri": "s3://bucket/data.parquet"}),
            _node("sample", "sample", {"n": 100, "seed": 42}),
        ],
        "edges": [_edge("source", "sample")],
    })
    adapter = type("RemoteDuckDBAdapter", (), {"name": "duckdb"})()

    assert not _reservoir_preview_allowed(graph, "sample", lambda _uri: adapter)


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


def test_exact_source_without_revision_preview_capability_fails_before_full_open(monkeypatch) -> None:
    import hub.executors.engine as engine_mod

    opened: list[str] = []

    class ExactFullOnlyAdapter:
        name = "exact-full-only"

        def open_revision(self, _uri, revision_id):
            opened.append(revision_id)
            raise AssertionError("exact preview must not open the unbounded full-run relation")

    graph = Graph(
        id="exact-full-only-preview", version=1,
        nodes=[_node("source", "source", {
            "uri": "exact-full-only://dataset",
            "datasetRef": {"kind": "exact", "datasetId": "dataset", "revisionId": "v1"},
        })],
        edges=[],
    )
    monkeypatch.setattr(
        "hub.metadb.catalog_revision_binding_for_uri", lambda _uri: {"dataset_id": "dataset"})
    monkeypatch.setattr(
        "hub.workspace_providers.provider_dataset_identity", lambda _uri: None)
    monkeypatch.setattr(engine_mod, "revision_adapter_for_uri", lambda *_args: ExactFullOnlyAdapter())

    with db.run_scope(), pytest.raises(
            NotPreviewable, match="does not guarantee a bounded exact-revision preview"):
        BuildEngine(graph, lambda _uri: ExactFullOnlyAdapter(), object(), sample_k=7, full=False).relation(
            "source")

    assert opened == []


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
    assert result.completeness == "page"

    short_page = catalog_routes.data_sample(SampleRequest(uri="sample-spy://data", k=10))
    assert calls == [("preview", 3), ("preview", 11)]
    assert short_page.rows == [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}]
    assert short_page.row_count is None
    assert short_page.has_more is None
    assert short_page.completeness == "unknown"
    assert short_page.truncated is True


def test_data_sample_declares_complete_only_with_exact_bounded_metadata(monkeypatch) -> None:
    from hub.routers import catalog as catalog_routes

    class Adapter:
        name = "metadata-spy"

        @staticmethod
        def preview_scan(_uri: str, _columns=None, *, limit: int = 2000):
            return db.conn().from_arrow(pa.table({"id": [0, 1, 2, 3]})).limit(limit)

        @staticmethod
        def metadata_count(_uri: str) -> int:
            return 4

    monkeypatch.setattr(
        catalog_routes, "get_deps",
        lambda: type("Deps", (), {"storage": None, "resolve_adapter": lambda _self, _uri: Adapter()})(),
    )

    result = catalog_routes.data_sample(SampleRequest(uri="metadata-spy://data", k=10))

    assert result.rows == [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}]
    assert result.row_count == 4
    assert result.has_more is False and result.truncated is False
    assert result.completeness == "complete"

    final_page = catalog_routes.data_sample(SampleRequest(
        uri="metadata-spy://data", k=10, offset=2,
    ))
    assert final_page.rows == [{"id": 2}, {"id": 3}]
    assert final_page.row_count == 4 and final_page.has_more is False
    assert final_page.completeness == "page" and final_page.truncated is True


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
    assert final_page.completeness == "capped"
    assert final_page.row_limit == budget
    assert final_page.limit_reason == "interactive-row-budget"
    assert final_page.limit_scope == "result-window"

    with pytest.raises(HTTPException) as exc_info:
        catalog_routes.data_sample(SampleRequest(
            uri="budget-spy://data", offset=budget, k=50,
        ))
    assert exc_info.value.status_code == 400
    assert "interactive window" in str(exc_info.value.detail)
    assert calls == [budget - 49, budget], "out-of-window requests must be rejected before scanning"


def test_data_sample_marks_an_unknown_total_at_the_budget_boundary(monkeypatch) -> None:
    from hub.routers import catalog as catalog_routes

    class Adapter:
        name = "unknown-budget-spy"

        @staticmethod
        def preview_scan(_uri: str, _columns=None, *, limit: int = 2000):
            # Returning exactly the requested prefix cannot prove whether row limit + 1 exists.
            return db.conn().sql(f"SELECT i AS id FROM range({limit}) rows(i)")

    monkeypatch.setattr(
        catalog_routes, "get_deps",
        lambda: type("Deps", (), {"storage": None, "resolve_adapter": lambda _self, _uri: Adapter()})(),
    )
    budget = catalog_routes.DATA_SAMPLE_PREVIEW_ROW_BUDGET

    result = catalog_routes.data_sample(SampleRequest(
        uri="unknown-budget-spy://data", offset=budget - 50, k=50,
    ))

    assert len(result.rows) == 50 and result.row_count is None
    assert result.has_more is False
    assert result.completeness == "capped"
    assert result.row_limit == budget
    assert result.limit_reason == "interactive-row-budget"
    assert result.limit_scope == "result-window"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"completeness": "complete", "rowCount": None}, "complete data requires rowCount"),
        ({"completeness": "complete", "rowCount": 0, "truncated": True},
         "complete data cannot be truncated"),
        ({"hasMore": True, "truncated": False}, "hasMore requires truncated"),
        ({"completeness": "page", "truncated": False},
         "successful non-complete data requires truncated=true"),
        ({"completeness": "unknown", "truncated": False},
         "successful non-complete data requires truncated=true"),
        ({"rowLimit": 2000, "limitReason": "preview-scan"},
         "rowLimit, limitReason, and limitScope"),
        ({"completeness": "capped", "truncated": True}, "capped data requires rowLimit"),
        ({"error": True}, "unavailable sample requires a non-empty reason"),
        ({"notPreviewable": True, "reason": "  "},
         "unavailable sample requires a non-empty reason"),
        ({"error": True, "reason": "failed", "completeness": "sample"},
         "unavailable sample must have unknown completeness"),
        ({"error": True, "reason": "failed", "rows": [{"id": 1}]},
         "unavailable sample cannot carry rows"),
        ({"error": True, "reason": "failed", "rowCount": 0},
         "unavailable sample cannot carry rows"),
        ({"error": True, "reason": "failed", "hasMore": False},
         "unavailable sample cannot carry rows"),
        ({"error": True, "reason": "failed", "truncated": True},
         "unavailable sample cannot be truncated"),
        ({"error": True, "reason": "failed", "rowLimit": 10,
          "limitReason": "preview-scan", "limitScope": "each-source"},
         "unavailable sample cannot carry an active row limit"),
        ({"rowLimit": 10, "limitReason": "preview-scan", "limitScope": "result-window"},
         "preview-scan limits must use limitScope=each-source"),
        ({"rowLimit": 10, "limitReason": "interactive-row-budget", "limitScope": "each-source"},
         "interactive-row-budget limits must use limitScope=result-window"),
        ({"completeness": "sample", "truncated": False, "rowLimit": 10,
          "limitReason": "preview-scan", "limitScope": "each-source"},
         "sample data requires truncated=true"),
        ({"completeness": "sample", "truncated": True},
         "sample data requires an each-source preview-scan limit"),
        ({"completeness": "capped", "truncated": True, "hasMore": False,
          "rowLimit": 10, "limitReason": "preview-scan", "limitScope": "each-source"},
         "capped data requires a result-window interactive-row-budget limit"),
    ],
)
def test_sample_result_rejects_contradictory_scope_contract(payload, message) -> None:
    with pytest.raises(ValidationError, match=message):
        SampleResult.model_validate(payload)


def test_sample_result_accepts_empty_complete_and_unproven_short_page() -> None:
    complete = SampleResult(completeness="complete", row_count=0, has_more=False)
    short_page = SampleResult(
        rows=[{"id": 1}], completeness="unknown", truncated=True,
    )

    assert complete.row_count == 0 and complete.rows == []
    assert short_page.row_count is None and short_page.completeness == "unknown"

    unavailable = SampleResult(error=True, reason="failed")
    assert unavailable.completeness == "unknown" and unavailable.rows == []


def test_profile_result_requires_an_explicit_success_scope() -> None:
    with pytest.raises(ValidationError, match="must declare complete or sample"):
        ProfileResult(columns=[], row_count=3, sampled=False)
    with pytest.raises(ValidationError, match="complete profile cannot be marked sampled"):
        ProfileResult(completeness="complete", sampled=True)
    with pytest.raises(ValidationError, match="unavailable profile cannot carry statistics"):
        ProfileResult(error=True, reason="failed", columns=[], row_count=1)
    with pytest.raises(ValidationError, match="unavailable profile requires a non-empty reason"):
        ProfileResult(error=True)
    with pytest.raises(ValidationError, match=r"nonNull \+ nulls must equal rowCount"):
        ProfileResult(
            completeness="sample", sampled=True, row_count=2,
            columns=[ColumnProfile(name="id", type="int64", non_null=1)],
        )
    with pytest.raises(ValidationError, match="requires a distinct value"):
        ColumnProfile(
            name="id", type="int64", distinct=None, distinct_is_approximate=True,
        )

    assert ProfileResult(completeness="sample", sampled=True).completeness == "sample"
    assert ProfileResult(completeness="complete", sampled=False).completeness == "complete"
    assert ProfileResult(error=True, reason="failed").completeness == "unknown"


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


def test_adapter_filesystem_probes_enforce_shared_mode_roots(
        tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3]}), outside)
    sibling = tmp_path / "allowed-sibling"
    sibling.mkdir()
    sibling_dataset = sibling / "rows.parquet"
    pq.write_table(pa.table({"id": [4]}), sibling_dataset)
    symlink_escape = allowed / "escape.parquet"
    symlink_escape.symlink_to(outside)
    monkeypatch.setenv("DP_AUTH_SECRET", "adapter-boundary-secret-0123456789")
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    monkeypatch.setenv("DP_DATASET_ROOTS", str(allowed))

    adapter = DuckDBAdapter()
    mixed_case_file_uri = "FiLe" + outside.as_uri()[4:]
    for uri in (
        str(outside), outside.as_uri(), mixed_case_file_uri,
        str(sibling_dataset), str(symlink_escape),
    ):
        with pytest.raises(PermissionError, match="outside the allowed roots"):
            adapter.matches(uri)
        with pytest.raises(PermissionError, match="outside the allowed roots"):
            adapter.preview_scan(uri)
        with pytest.raises(PermissionError, match="outside the allowed roots"):
            adapter.metadata_count(uri)
        with pytest.raises(PermissionError, match="outside the allowed roots"):
            adapter.fingerprint(uri)

    # Validating the literal in-root pattern is insufficient: DuckDB expands it later and would follow
    # this matched symlink outside the allowed root. Shared mode must reject local globbing before I/O.
    with pytest.raises(PermissionError, match="glob dataset paths"):
        with db.run_scope():
            adapter.scan(str(allowed / "*.parquet")).fetchall()


def test_adapter_filesystem_probes_use_checked_canonical_paths(
        tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    dataset = allowed / "rows.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3]}), dataset)
    adapter = DuckDBAdapter()

    monkeypatch.setenv("DP_AUTH_SECRET", "adapter-boundary-secret-0123456789")
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    monkeypatch.setenv("DP_DATASET_ROOTS", str(allowed))
    mixed_case_file_uri = "FiLe" + dataset.as_uri()[4:]
    assert adapter.matches(mixed_case_file_uri)
    assert adapter.metadata_count(mixed_case_file_uri) == 3
    assert adapter.fingerprint(mixed_case_file_uri) != "unknown"
    with db.run_scope():
        assert adapter.preview_scan(mixed_case_file_uri, limit=2).to_arrow_table().num_rows == 2

    alias = allowed / "alias.parquet"
    alias.symlink_to(dataset)
    observed: list[str] = []
    sentinel = object()

    def scan(uri: str, **_kwargs):
        observed.append(uri)
        return sentinel

    monkeypatch.setattr(adapter, "scan", scan)
    mixed_case_alias_uri = "FiLe" + alias.as_uri()[4:]
    assert adapter.preview_scan(mixed_case_alias_uri, limit=2) is sentinel
    assert observed == [str(dataset.resolve())]

    # Open mode is the trusted local product and intentionally retains arbitrary local-file access.
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    outside = tmp_path / "open-mode.parquet"
    pq.write_table(pa.table({"id": [4, 5]}), outside)
    open_adapter = DuckDBAdapter()
    assert open_adapter.metadata_count(str(outside)) == 2
    assert open_adapter.fingerprint(str(outside)) != "unknown"
    with db.run_scope():
        assert open_adapter.scan(str(tmp_path / "*.parquet")).aggregate(
            "count(*) AS n").fetchone()[0] == 2


def test_empty_local_uri_is_not_the_working_directory() -> None:
    from hub import paths

    adapter = DuckDBAdapter()
    assert paths.checked_local_path("") is None
    assert not adapter.matches("")
    assert adapter.metadata_count("") is None
    assert adapter.fingerprint("") == "unknown"


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
