"""User-confirmed promoted input columns are durable, name-only execution requirements."""

from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import pyarrow as pa
import pytest

from hub import db, metadb
from hub.executors.engine import BuildEngine
from hub.executors.preview import preview_node
from hub.main import app
from hub.models import ColumnSchema, Graph
from hub.plugins.processors import ProcessorRegistry, RegisteredProcessor


def _graph(*, on_error="raise"):
    return Graph.model_validate({
        "id": "promoted-input-contract", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": "test://input"}}},
            {"id": "transform", "type": "transform", "position": {"x": 200, "y": 0},
             "data": {"config": {"source": "library", "processor": "test.input",
                                 "version": "v1", "onError": on_error}}},
        ],
        "edges": [{"id": "input", "source": "source", "target": "transform",
                   "sourceHandle": "out", "targetHandle": "in", "data": {"wire": "dataset"}}],
    })


def _processor(*, columns=("required",), mode="map", provenance="promoted", factory=None):
    return RegisteredProcessor(
        id="test.input", title="Confirmed inputs", mode=mode, provenance=provenance,
        input_columns=list(columns),
        input_schema=[ColumnSchema(name=name, type="integer", nullable=False) for name in columns],
        fn_factory=factory or (lambda _params: lambda row: row),
    )


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("mode", ["map", "map_batches"])
@pytest.mark.parametrize("on_error", ["raise", "skip"])
def test_missing_columns_fail_before_building_user_code(full, mode, on_error):
    proc = _processor(mode=mode, factory=lambda _params: pytest.fail("must reject before user code"))
    graph = _graph(on_error=on_error)
    registry = SimpleNamespace(get=lambda _pid, _version: proc)
    engine = BuildEngine(graph, object(), registry, full=full)
    with db.run_scope():
        # No rows does not erase the actual input schema or make a missing column acceptable.
        parent = db.conn().from_arrow(pa.table({"other": pa.array([], type=pa.string())}))
        with pytest.raises(ValueError, match="missing required input columns: required") as error:
            engine._transform(graph.nodes[1], parent)
    assert "Choose an input" in str(error.value)
    assert "required-column declaration" in str(error.value)


def _preview(proc, table, *, on_error="raise"):
    class Adapter:
        def fingerprint(self, _uri):
            return "input-contract"

        def preview_scan(self, _uri, *, limit, **_kwargs):
            return db.conn().from_arrow(table.slice(0, limit))

    registry = SimpleNamespace(get=lambda _pid, _version: proc)
    return preview_node(_graph(on_error=on_error), "transform", 5, lambda _uri: Adapter(), registry)


def test_preview_reports_missing_contract_as_error_not_a_full_run_suggestion():
    result = _preview(_processor(), pa.table({"other": [1]}), on_error="skip")
    assert result.error and not result.not_previewable
    assert "missing required input columns: required" in result.reason
    assert not result.rows


def test_present_columns_do_not_coerce_values_or_reject_extras_and_empty_inputs():
    table = pa.table({"required": ["text", None], "extra": [1, 2]})
    result = _preview(_processor(), table)
    assert not result.error and not result.not_previewable, result.reason
    assert result.rows == table.to_pylist()
    empty = _preview(_processor(), table.slice(0, 0))
    assert not empty.error and not empty.not_previewable, empty.reason
    assert empty.rows == []


def test_undeclared_columns_and_installed_processor_contracts_keep_existing_behavior():
    table = pa.table({"other": [1]})
    for proc in (_processor(columns=()), _processor(provenance="plugin")):
        result = _preview(proc, table)
        assert not result.error and not result.not_previewable, result.reason
        assert result.rows == [{"other": 1}]


def test_name_only_input_contract_is_durable_idempotent_and_versioned():
    uid = f"required-input-{uuid.uuid4().hex}"
    with metadb.session() as session:
        session.add(metadb.User(id=uid, name="Input contract owner"))
    client = TestClient(app)
    body = {
        "id": f"confirmed-input-{uuid.uuid4().hex}", "title": "Confirmed inputs", "mode": "map",
        "code": "def fn(row):\n    return row", "inputColumns": ["event"],
        "outputSchema": [], "requirements": [],
    }
    first = client.post("/api/processors/promote", headers={"X-DP-User": uid}, json=body)
    assert first.status_code == 200, first.text
    original = first.json()
    assert original["inputColumns"] == ["event"]
    assert [(item["name"], item["type"]) for item in original["inputSchema"]] == [("event", "unknown")]
    replay = client.post("/api/processors/promote", headers={"X-DP-User": uid}, json=body)
    assert replay.json() == original
    changed = client.post("/api/processors/promote", headers={"X-DP-User": uid}, json={
        **body, "inputColumns": ["event", "amount"],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["id"] == original["id"]
    assert changed.json()["version"] != original["version"]
    reopened = ProcessorRegistry().get(original["id"], original["version"])
    assert reopened.input_columns == ["event"]
    assert reopened.input_schema[0].type == "unknown"
