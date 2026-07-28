"""Editor-only Example rows execute through the Transform sandbox without durable state."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from hub.deps import get_deps
from hub.editor_examples import (
    EDITOR_EXAMPLE_MAX_BYTES,
    EDITOR_EXAMPLE_MAX_ROWS,
    parse_editor_example_rows,
)
from hub.main import app
from hub.models import SampleResult
from hub.routers import runs as runs_router
from hub import sandbox

client = TestClient(app)


def _graph(*, mode: str = "map", code: str | None = None) -> dict:
    default_code = {
        "map": "def fn(row):\n    return {**row, 'tested': row['value'] * 2}",
        "map_batches": (
            "def fn(rows):\n"
            "    return [{**row, 'tested': row['value'] * 2} for row in rows]"
        ),
        "filter": "def fn(row):\n    return row['value'] > 1",
        "flat_map": (
            "def fn(row):\n"
            "    return [row, {**row, 'value': row['value'] * 10}]"
        ),
    }[mode]
    return {
        "id": f"editor-examples-{uuid.uuid4().hex}",
        "name": "Example rows",
        "version": 1,
        "requirements": [],
        "nodes": [{
            "id": "transform",
            "type": "transform",
            "position": {"x": 0, "y": 0},
            "data": {
                "title": "Transform",
                "config": {
                    "source": "adhoc",
                    "mode": mode,
                    "code": code or default_code,
                    "onError": "raise",
                },
            },
        }],
        "edges": [],
    }


def _preview(graph: dict, fixture: str):
    return client.post("/api/run/editor-preview/examples", json={
        "graph": graph,
        "nodeId": "transform",
        "portId": "out",
        "k": 50,
        "offset": 0,
        "exampleRowsJson": fixture,
        "parameterBindings": [],
    })


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("map", [{"value": 1, "tested": 2}, {"value": 2, "tested": 4}]),
        ("map_batches", [{"value": 1, "tested": 2}, {"value": 2, "tested": 4}]),
        ("filter", [{"value": 2}]),
        ("flat_map", [
            {"value": 1}, {"value": 10}, {"value": 2}, {"value": 20},
        ]),
    ],
)
def test_example_rows_use_existing_transform_modes(mode, expected):
    response = _preview(_graph(mode=mode), '[{"value":1},{"value":2}]')

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == expected
    assert body["completeness"] == "complete"
    assert body["truncated"] is False
    assert body["sampleProvenance"] is None
    assert body["inputManifest"] is None
    assert body["editorTestInput"] is None


def test_example_rows_preserve_structured_transform_diagnostics():
    response = _preview(
        _graph(code="def fn(row):\n    raise ValueError('fixture boom')"),
        '[{"value":1}]',
    )

    assert response.status_code == 200, response.text
    assert response.json()["failureCategory"] == "user_code_exception"
    assert response.json()["userCodeException"] == {
        "nodeId": "transform",
        "nodeTitle": "Transform",
        "exceptionType": "ValueError",
        "message": "fixture boom",
        "rowIndex": 0,
        "availableColumns": ["value"],
        "guidance": None,
    }


def test_example_rows_preserve_syntax_location_without_sandbox_wrapper():
    response = _preview(_graph(code="def fn(row)\n    return row"), '[{"value":1}]')

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["failureCategory"] == "syntax_error"
    assert body["syntaxError"] == {
        "line": 1,
        "column": 12,
        "message": "expected ':'",
    }
    assert body["reason"] == "Line 1: expected ':'"
    assert "adhoc-cell" not in body["reason"]
    assert "SandboxError" not in body["reason"]


def test_dependency_syntax_error_during_cell_exec_remains_a_runtime_failure(monkeypatch):
    def parse_dependency_code():
        raise SyntaxError("dependency generated code failed")

    monkeypatch.setitem(
        sandbox._ALLOWED_MODULES,
        "fixture_dependency",
        SimpleNamespace(parse=parse_dependency_code),
    )
    response = _preview(_graph(code=(
        "fixture_dependency.parse()\n"
        "def fn(row):\n"
        "    return row"
    )), '[{"value":1}]')

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["failureCategory"] == "runtime_error"
    assert body["syntaxError"] is None
    assert body["userCodeException"] is None
    assert "dependency generated code failed" in body["reason"]


def test_dependency_syntax_error_while_processing_a_row_remains_user_code_failure(monkeypatch):
    def parse_dependency_code():
        raise SyntaxError("dependency generated code failed")

    monkeypatch.setitem(
        sandbox._ALLOWED_MODULES,
        "fixture_dependency",
        SimpleNamespace(parse=parse_dependency_code),
    )
    response = _preview(_graph(code=(
        "def fn(row):\n"
        "    fixture_dependency.parse()\n"
        "    return row"
    )), '[{"value":1}]')

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["failureCategory"] == "user_code_exception"
    assert body["syntaxError"] is None
    assert body["userCodeException"]["exceptionType"] == "SyntaxError"
    assert body["userCodeException"]["message"] == "dependency generated code failed"


@pytest.mark.parametrize("flag", ["bypassed", "disabled"])
def test_example_rows_test_code_despite_canvas_execution_flags(flag):
    graph = _graph()
    graph["nodes"][0]["data"][flag] = True

    response = _preview(graph, '[{"value":3}]')

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"value": 3, "tested": 6}]


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ("{", "must be valid JSON"),
        ('{"value":1}', "must be a JSON array of objects"),
        ("[1]", "Every example row must be a JSON object"),
        ('[{"value":1},{"other":2}]', "must use the same fields"),
        (json.dumps([{"value": index} for index in range(
            EDITOR_EXAMPLE_MAX_ROWS + 1)]), "may contain at most"),
        ('[{"value":"' + ("界" * (EDITOR_EXAMPLE_MAX_BYTES // 2)) + '"}]',
         "must be at most"),
        ('[{"value":1},{"value":"two"}]', "consistent Arrow-compatible"),
    ],
    ids=[
        "invalid-json",
        "not-array",
        "non-object-row",
        "inconsistent-shape",
        "row-budget",
        "byte-budget",
        "inconsistent-types",
    ],
)
def test_invalid_example_rows_are_rejected_before_execution(
        fixture, message, monkeypatch):
    def forbidden_preview(*_args, **_kwargs):
        raise AssertionError("invalid Example rows reached Transform execution")

    monkeypatch.setattr(runs_router, "preview_node", forbidden_preview)
    response = _preview(_graph(), fixture)

    assert response.status_code == 422, response.text
    assert message in response.text


def test_invalid_unicode_is_rejected_by_the_shared_backend_validator():
    with pytest.raises(ValueError, match="must use valid Unicode"):
        parse_editor_example_rows('[{"value":"\ud800"}]')


def test_example_rows_do_not_mutate_canvas_or_create_run_history():
    graph = _graph()
    created = client.post("/api/canvas", json=graph)
    assert created.status_code == 200, created.text
    try:
        before = client.get(f"/api/canvas/{graph['id']}")
        assert before.status_code == 200, before.text
        original = copy.deepcopy(before.json())

        response = _preview(graph, '[{"value":7,"fixtureOnly":"sentinel"}]')
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [{
            "value": 7, "fixtureOnly": "sentinel", "tested": 14,
        }]

        after = client.get(f"/api/canvas/{graph['id']}")
        history = client.get(f"/api/canvas/{graph['id']}/runs")
        assert after.status_code == 200, after.text
        assert after.json() == original
        assert history.status_code == 200, history.text
        assert history.json() == []
    finally:
        client.delete(f"/api/canvas/{graph['id']}")


def test_example_rows_use_the_kernel_preview_transport_when_selected(monkeypatch):
    fixture = '[{"value":3,"fixtureOnly":"kernel-sentinel"}]'
    observed: dict = {}

    class FakeKernel:
        def preview(
                self, graph, node_id, k, offset, port_id,
                *, example_rows_json=None, example_uri=None):
            observed.update({
                "graph": graph,
                "node_id": node_id,
                "k": k,
                "offset": offset,
                "port_id": port_id,
                "example_rows_json": example_rows_json,
                "example_uri": example_uri,
            })
            return SampleResult(
                columns=[],
                rows=[{"value": 3, "tested": 6}],
                has_more=False,
                completeness="sample",
                truncated=True,
                row_limit=2000,
                limit_reason="preview-scan",
                limit_scope="each-source",
            ).model_dump()

    deps = get_deps()
    monkeypatch.setattr(deps, "chosen_backend", lambda _uid: "kernel")
    monkeypatch.setattr(deps, "kernel_backend", lambda: FakeKernel())

    response = _preview(_graph(), fixture)

    assert response.status_code == 200, response.text
    assert observed["example_rows_json"] == fixture
    assert str(observed["example_uri"]).startswith("mem://editor-example-")
    assert "kernel-sentinel" not in json.dumps(
        observed["graph"].model_dump(mode="json"))
    assert response.json()["rows"] == [{"value": 3, "tested": 6}]
    assert response.json()["completeness"] == "complete"
