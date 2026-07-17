"""Fail-closed transport admission for exact-revision run inputs (#383)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hub.backends import backend_supports_admitted_input_manifests
from hub.kernel_backend import KernelBackend
from hub.models import Graph, GraphNode, RunEstimate
from hub.plugins.runner import LocalRunner
from hub.routers import runs
from hub.run_controller import RunController
from hub.subprocess_runner import SubprocessRunner


MANIFEST = [{
    "node_id": "source",
    "dataset_id": "dataset",
    "revision_id": "revision",
    "provider": "lance",
    "resolved_at": "now",
}]


def _graph() -> Graph:
    return Graph(
        id="transport-admission",
        nodes=[GraphNode(
            id="source", type="source",
            data={"config": {"uri": "lance://transport-admission"}},
        )],
    )


def _unsupported_backend(mode: str, events: list[str], *, name: str = "optional"):
    backend = SimpleNamespace(name=name)
    if mode == "false":
        backend.supports_admitted_input_manifests = lambda: False
    elif mode == "broken":
        def broken_probe():
            raise RuntimeError("capability control plane unavailable")
        backend.supports_admitted_input_manifests = broken_probe
    backend.estimate = lambda *_args: events.append("estimate") or RunEstimate(
        rows=1, bytes=1, placement="distributed", needs_confirm=False)
    backend.preallocate_run_id = lambda: events.append("preallocate") or "forbidden"
    backend.run = lambda *_args, **_kwargs: events.append("run")
    return backend


def _prepare_start(monkeypatch, deps, selected_runner) -> None:
    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs.graph_mod, "resolve_source_refs", lambda *_args: None)
    monkeypatch.setattr(runs, "_reject_invalid", lambda *_args: None)
    monkeypatch.setattr(
        runs.compiler, "compile_plan", lambda *_args: SimpleNamespace(acyclic=True))
    monkeypatch.setattr(runs, "_require_satisfiable_hard_requirements", lambda *_args: None)
    monkeypatch.setattr(runs, "_run_output_preflight", lambda *_args: None)
    monkeypatch.setattr(runs, "_route_by_capability", lambda *_args: selected_runner)
    monkeypatch.setattr(runs, "_require_destination_credential_preflight", lambda *_args: None)
    monkeypatch.setattr(runs, "_cone_size", lambda *_args: (1, 1, {}))
    monkeypatch.setattr(runs, "_bind_local_run_manifest", lambda graph, *_args: graph)


@pytest.mark.parametrize("mode", ["missing", "false", "broken"])
def test_optional_runner_manifest_probe_fails_closed_before_preallocation(mode, monkeypatch):
    events: list[str] = []
    runner = _unsupported_backend(mode, events)
    controller = SimpleNamespace(
        name="run-controller",
        plan_for_run=lambda *_args, **_kwargs: events.append("plan") or [],
        run=lambda *_args, **_kwargs: events.append("controller-run"),
    )
    deps = SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda ref: ref),
        registry={}, node_specs={}, node_ir={}, runner=SimpleNamespace(),
        controller=controller, pick_runner=lambda *_args: runner,
    )
    _prepare_start(monkeypatch, deps, runner)

    with pytest.raises(HTTPException, match="does not support admitted exact-revision") as caught:
        runs.start_run(
            deps, _graph(), "source", "user", confirmed=True,
            input_manifest=MANIFEST,
        )

    assert caught.value.status_code == 400
    assert events == ["estimate", "plan"]


@pytest.mark.parametrize("mode", ["missing", "false", "broken"])
def test_placed_runner_manifest_probe_fails_closed_before_controller_allocation(
        mode, monkeypatch):
    events: list[str] = []
    placed = _unsupported_backend(mode, events, name="placed")
    selected = SimpleNamespace(
        name="local-out-of-core",
        supports_admitted_input_manifests=lambda: True,
        estimate=lambda *_args: events.append("estimate") or RunEstimate(
            rows=1, bytes=1, placement="local", needs_confirm=False),
    )
    regions = [
        SimpleNamespace(backend="placed"),
        SimpleNamespace(backend="default"),
    ]
    deps = SimpleNamespace(
        catalog=SimpleNamespace(resolve_ref=lambda ref: ref),
        registry={}, node_specs={}, node_ir={}, runner=selected,
        runners=[placed], chosen_backend=lambda _uid: "local-out-of-core",
        pick_runner=lambda *_args: selected,
    )
    controller = RunController(deps, selected, lambda *_args: None)
    controller.plan_for_run = lambda *_args, **_kwargs: events.append("plan") or regions
    controller.run = lambda *_args, **_kwargs: events.append("controller-run")
    deps.controller = controller
    _prepare_start(monkeypatch, deps, selected)

    with pytest.raises(HTTPException, match="does not support admitted exact-revision") as caught:
        runs.start_run(
            deps, _graph(), "source", "user", confirmed=True,
            input_manifest=MANIFEST,
        )

    assert caught.value.status_code == 400
    assert events == ["estimate", "plan"]


def test_built_in_local_transports_explicitly_advertise_manifest_support():
    assert backend_supports_admitted_input_manifests(object.__new__(LocalRunner))
    assert backend_supports_admitted_input_manifests(object.__new__(SubprocessRunner))
    assert backend_supports_admitted_input_manifests(object.__new__(KernelBackend))
