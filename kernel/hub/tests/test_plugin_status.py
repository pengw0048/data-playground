from __future__ import annotations

from pathlib import Path

from hub.deps import Deps


def _write_plugin(workspace: Path, name: str, body: str) -> None:
    pack = workspace / "plugins" / name
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text(body)


def _node_registration(kind: str) -> str:
    return (
        "from hub.sdk import NodeSpec, PortSpec\n"
        "def register(reg):\n"
        f"    reg.add_node(NodeSpec(kind='{kind}', title='{kind}', category='compute', "
        "inputs=[], outputs=[PortSpec(id='out', wire='dataset')], params=[]))\n"
    )


def test_plugin_status_reports_only_effective_runtime_capabilities(tmp_path):
    workspace = tmp_path / "workspace"
    _write_plugin(workspace, "status_active_pack", _node_registration("status-active-node"))
    _write_plugin(workspace, "status_inactive_pack", "def register(reg):\n    return\n")

    deps = Deps(str(workspace), str(tmp_path / "data"))
    active = next(p for p in deps.plugins if p["name"] == "status_active_pack")
    inactive = next(p for p in deps.plugins if p["name"] == "status_inactive_pack")

    assert active["state"] == "active"
    assert active["effective_capabilities"] == ["node:status-active-node"]
    assert active["process_placement"] == ["execution"]
    assert "status-active-node" in deps.node_specs
    assert inactive["state"] == "inactive"
    assert inactive["effective_capabilities"] == []
    assert inactive["process_placement"] == []


def test_plugin_status_transfers_replaced_processor_capability(tmp_path):
    workspace = tmp_path / "workspace"
    processor = (
        "from hub.plugins.processors import RegisteredProcessor\n"
        "def register(reg):\n"
        "    reg.add_processor(RegisteredProcessor(id='shared', title=TITLE, mode='map'))\n"
    )
    _write_plugin(workspace, "status_processor_first", "TITLE = 'first'\n" + processor)
    _write_plugin(workspace, "status_processor_second", "TITLE = 'second'\n" + processor)

    deps = Deps(str(workspace), str(tmp_path / "data"))
    first = next(p for p in deps.plugins if p["name"] == "status_processor_first")
    second = next(p for p in deps.plugins if p["name"] == "status_processor_second")

    assert deps.registry.get("shared").title == "second"
    assert first["state"] == "inactive"
    assert first["effective_capabilities"] == []
    assert second["state"] == "active"
    assert second["effective_capabilities"] == ["processor:shared"]


def test_plugin_status_distinguishes_partial_conflict_and_sanitized_failure(tmp_path):
    workspace = tmp_path / "workspace"
    # Add a runner factory after the effective node so the plugin remains partially usable.
    _write_plugin(
        workspace,
        "status_degraded_pack",
        "from hub.sdk import NodeSpec, PortSpec\n"
        "def _runner(_deps):\n"
        "    raise RuntimeError('token=TOP-SECRET /private/operator/path')\n"
        "def register(reg):\n"
        "    reg.add_node(NodeSpec(kind='status-usable-node', title='usable', category='compute', "
        "inputs=[], outputs=[PortSpec(id='out', wire='dataset')], params=[]))\n"
        "    reg.add_runner_factory(_runner)\n"
    )
    _write_plugin(
        workspace,
        "status_conflict_pack",
        "from hub.sdk import NodeSpec, PortSpec\n"
        "def register(reg):\n"
        "    reg.add_node(NodeSpec(kind='source', title='collision', category='compute', "
        "inputs=[], outputs=[PortSpec(id='out', wire='dataset')], params=[]))\n",
    )
    _write_plugin(
        workspace,
        "status_failed_pack",
        "def register(reg):\n"
        "    raise ValueError('password=TOP-SECRET /private/operator/path')\n",
    )

    deps = Deps(str(workspace), str(tmp_path / "data"))
    by_name = {p["name"]: p for p in deps.plugins}

    partial = by_name["status_degraded_pack"]
    assert partial["state"] == "degraded"
    assert partial["effective_capabilities"] == ["node:status-usable-node"]
    assert "Runner activation failed (RuntimeError)" in partial["failure_summary"]
    assert "TOP-SECRET" not in partial["failure_summary"]
    assert "/private/operator/path" not in partial["failure_summary"]

    conflict = by_name["status_conflict_pack"]
    assert conflict["state"] == "conflict"
    assert conflict["effective_capabilities"] == []
    assert "built-in node" in conflict["failure_summary"]

    failed = by_name["status_failed_pack"]
    assert failed["state"] == "failed"
    assert failed["effective_capabilities"] == []
    assert "ValueError" in failed["failure_summary"]
    assert "TOP-SECRET" not in failed["failure_summary"]
    assert "/private/operator/path" not in failed["failure_summary"]
    assert failed["failure_impact"] == "optional-degradation"


def test_plugin_status_is_isolated_per_application_instance(tmp_path):
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    second_workspace.mkdir()
    _write_plugin(first_workspace, "status_first_only_pack", _node_registration("status-first-only"))

    first = Deps(str(first_workspace), str(tmp_path / "data-first"))
    second = Deps(str(second_workspace), str(tmp_path / "data-second"))

    assert any(p["name"] == "status_first_only_pack" for p in first.plugins)
    assert all(p["name"] != "status_first_only_pack" for p in second.plugins)
    assert "status-first-only" in first.node_specs
    assert "status-first-only" not in second.node_specs
    required = next(p for p in second.plugins if p["name"] == "default-catalog")
    assert required["required"] is True
    assert required["failure_impact"] == "startup-blocking"


def test_plugins_api_removes_internal_status_bookkeeping(tmp_path, monkeypatch):
    from hub.routers import catalog as catalog_router

    workspace = tmp_path / "workspace"
    _write_plugin(workspace, "status_api_pack", _node_registration("status-api-node"))
    deps = Deps(str(workspace), str(tmp_path / "data"))
    monkeypatch.setattr(catalog_router, "get_deps", lambda: deps)

    response = catalog_router.list_plugins()
    entry = next(p for p in response if p.name == "status_api_pack")
    assert entry.effective_capabilities == ["node:status-api-node"]
    assert entry.state == "active"
    assert not any(key.startswith("_") for key in entry.model_dump())
