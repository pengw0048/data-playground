"""Cancellation follows the request's execution owner, including dispatch races."""
from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from hub.main import app
from hub.models import SampleResult
from hub.preview_requests import PreviewRequestConflict, PreviewRequests
from hub.python_preview import PreviewCancelled


def test_cancel_before_dispatch_prevents_code_and_duplicate_submission():
    requests = PreviewRequests()
    request_id = str(uuid.uuid4())
    assert requests.cancel("alice", "canvas", request_id.upper()) == "stopped"
    with requests.request("alice", "canvas", request_id) as session:
        with pytest.raises(PreviewCancelled):
            session.control.check()
    with pytest.raises(PreviewRequestConflict), requests.request("alice", "canvas", request_id):
        pytest.fail("request ID was replayed")


def test_cancel_waits_for_execution_owner_to_release_resources():
    requests = PreviewRequests()
    request_id = str(uuid.uuid4())
    entered, release, interrupted = threading.Event(), threading.Event(), threading.Event()

    def run():
        with requests.request("alice", "canvas", request_id) as session:
            session.control.add_interrupt(interrupted.set)
            entered.set()
            assert release.wait(5)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        assert requests.cancel("alice", "canvas", request_id) == "stopping"
        assert interrupted.is_set()
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(5)
    assert requests.cancel("alice", "canvas", request_id) == "stopped"


def test_cancel_is_scoped_to_both_canvas_and_principal():
    requests = PreviewRequests()
    request_id = str(uuid.uuid4())
    with requests.request("alice", "first", request_id) as session:
        requests.cancel("bob", "first", request_id)
        requests.cancel("alice", "second", request_id)
        session.control.check()
    assert requests.cancel("alice", "first", request_id) == "finished"


def test_transport_completion_does_not_claim_remote_execution_stopped():
    requests = PreviewRequests()
    request_id = str(uuid.uuid4())
    replies = [OSError("owner unreachable"), {"status": "stopping"}, {"status": "stopped"}]

    def remote():
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    with requests.request("alice", "canvas", request_id) as session:
        session.set_remote_cancel(remote)
        # The transport returns/errors while the execution owner may still be running.
    with pytest.raises(OSError):
        requests.cancel("alice", "canvas", request_id)
    assert requests.cancel("alice", "canvas", request_id) == "stopping"
    assert requests.cancel("alice", "canvas", request_id) == "stopped"


@pytest.mark.parametrize("category", ["cancelled", "timeout"])
def test_stopped_preview_failure_keeps_its_category(category):
    result = SampleResult(error=True, failure_category=category, reason="stopped")
    assert result.model_dump(by_alias=True)["failureCategory"] == category


def test_public_early_cancel_stops_example_rows_without_executing_code():
    client = TestClient(app)
    request_id = str(uuid.uuid4())
    canvas_id = f"early-stop-{uuid.uuid4().hex}"
    response = client.post(f"/api/run/preview/{request_id}/cancel", json={"canvasId": canvas_id})
    assert response.status_code == 200
    assert response.json() == {"status": "stopped"}
    body = {
        "previewRequestId": request_id,
        "graph": {"id": canvas_id, "name": "Stop race", "version": 1, "nodes": [{
            "id": "python", "type": "transform", "position": {"x": 0, "y": 0},
            "data": {"config": {"source": "adhoc", "mode": "map",
                "code": "def fn(row):\n    raise RuntimeError('must never execute')"}},
        }], "edges": []},
        "nodeId": "python", "exampleRowsJson": '[{"value": 1}]',
    }
    response = client.post("/api/run/editor-preview/examples", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["failureCategory"] == "cancelled"
    assert client.post("/api/run/editor-preview/examples", json=body).status_code == 409


def test_remote_completion_wins_over_hub_response_cleanup_race():
    requests = PreviewRequests()
    request_id = str(uuid.uuid4())
    entered, release = threading.Event(), threading.Event()

    def remote():
        release.set()
        return {"status": "finished"}

    def run():
        with requests.request("alice", "canvas", request_id) as session:
            session.set_remote_cancel(remote)
            entered.set()
            assert release.wait(5)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        assert requests.cancel("alice", "canvas", request_id) == "finished"
    finally:
        release.set()
        worker.join(5)


def test_capacity_retires_completed_work_without_losing_early_stop(monkeypatch):
    import hub.preview_requests as module
    monkeypatch.setattr(module, "_MAX_REQUESTS", 2)
    requests = PreviewRequests()
    early = str(uuid.uuid4())
    requests.cancel("alice", "canvas", early)
    for _ in range(5):
        with requests.request("alice", "canvas", str(uuid.uuid4())) as session:
            session.control.check()
    with requests.request("alice", "canvas", early) as session:
        with pytest.raises(PreviewCancelled):
            session.control.check()


def test_capacity_does_not_evict_uncertain_remote_execution(monkeypatch):
    import hub.preview_requests as module
    monkeypatch.setattr(module, "_MAX_REQUESTS", 1)
    requests = PreviewRequests()
    with requests.request("alice", "canvas", str(uuid.uuid4())) as session:
        session.set_remote_cancel(lambda: {"status": "stopping"})
    with pytest.raises(RuntimeError), requests.request("alice", "canvas", str(uuid.uuid4())):
        pytest.fail("lost the only handle to unresolved remote execution")


def test_kernel_transport_registers_exact_owner_before_dispatch_and_keeps_it_after_failure(monkeypatch):
    from types import SimpleNamespace
    from hub import kernel_backend
    from hub.deps import get_deps
    from hub.models import Graph, GraphNode
    from hub.python_preview import PreviewControl

    backend = kernel_backend.KernelBackend(
        SimpleNamespace(node_specs=get_deps().node_specs), SimpleNamespace())
    monkeypatch.setattr(backend, "_ensure_kernel", lambda _canvas: ("127.0.0.1:1234", "token"))
    graph = Graph(id="transport-stop", nodes=[GraphNode(id="source", type="source")])
    callbacks = []
    calls = []
    request_id = str(uuid.uuid4())

    def post(endpoint, path, token, body, **kwargs):
        calls.append(path)
        assert endpoint == "127.0.0.1:1234" and token == "token"
        assert kwargs["connect_retries"] == 0
        assert callbacks, "Stop must be routable before a possibly admitted POST"
        if path == "/preview":
            assert body["preview_request_id"] == request_id
            assert body["preview_owner"] == "alice"
            raise OSError("preview response lost")
        assert body == {"canvas_id": graph.id, "owner": "alice", "preview_request_id": request_id}
        return {"status": "stopped"}

    monkeypatch.setattr(kernel_backend, "_post", post)
    with pytest.raises(OSError):
        backend.preview(graph, "source", 10, 0, preview_request_id=request_id,
                        preview_owner="alice", control=PreviewControl(),
                        register_remote_cancel=callbacks.append)
    assert callbacks[0]() == {"status": "stopped"}
    assert calls == ["/preview", "/cancel-preview"]


def test_kernel_transport_stopped_during_spawn_never_dispatches(monkeypatch):
    from types import SimpleNamespace
    from hub import kernel_backend
    from hub.deps import get_deps
    from hub.models import Graph, GraphNode
    from hub.python_preview import PreviewControl

    control = PreviewControl()
    backend = kernel_backend.KernelBackend(
        SimpleNamespace(node_specs=get_deps().node_specs), SimpleNamespace())

    def ensure(_canvas):
        control.cancel()
        return "127.0.0.1:1234", "token"

    monkeypatch.setattr(backend, "_ensure_kernel", ensure)
    monkeypatch.setattr(kernel_backend, "_post", lambda *_args, **_kwargs: pytest.fail("dispatched cancelled preview"))
    with pytest.raises(PreviewCancelled):
        backend.preview(Graph(id="spawn-stop", nodes=[GraphNode(id="source", type="source")]),
                        "source", 10, 0, preview_request_id=str(uuid.uuid4()), control=control)


def test_restarted_hub_asks_surviving_kernel_before_acknowledging_stop():
    requests = PreviewRequests()
    request_id = str(uuid.uuid4())
    replies = iter([{"status": "stopping"}, {"status": "stopped"}])
    assert requests.cancel("alice", "canvas", request_id, fallback_cancel=lambda: next(replies)) == "stopping"
    # Retain the first execution owner even if a later caller has no fallback lookup.
    assert requests.cancel("alice", "canvas", request_id) == "stopped"
