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
    assert deps.node_specs["status-active-node"].source == "plugin:status_active_pack"
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


def _workload_field(key: str, target: str) -> dict:
    return {
        "key": key,
        "type": "password",
        "env": target,
        "secret": True,
        "workload_env": True,
    }


def _workload_status_deps() -> Deps:
    deps = object.__new__(Deps)
    deps.plugins = []
    deps._manifests = {}
    deps._plugin_workload_fields = ()
    return deps


def _workload_plugin(
    deps: Deps, name: str, target: str, *, source: str = "entry_point", active: bool = False,
) -> tuple[dict, dict]:
    field = _workload_field(f"{name.replace('-', '_')}_token", target)
    entry = deps._new_plugin_status(name, source, config=[field])
    deps._manifests[name] = {"config": [field]}
    if active:
        entry["effective_capabilities"] = [f"telemetry:{name}"]
        entry["_placements"][f"telemetry:{name}"] = "hub"
        deps._refresh_plugin_status(entry)
    return entry, field


def test_only_successfully_active_installed_plugins_may_forward_workload_config(monkeypatch):
    import json

    from hub.deps import Registry
    from hub.workload_env import WORKLOAD_CHILD_MARKER

    deps = _workload_status_deps()
    active, active_field = _workload_plugin(
        deps, "active-pack", "DP_ACTIVE_PLUGIN_TOKEN", active=True)
    failed, _ = _workload_plugin(deps, "failed-pack", "DP_ACTIVE_PLUGIN_TOKEN")
    deps._record_plugin_problem(
        failed,
        "Plugin import or registration failed (RuntimeError); check server logs.",
    )
    inactive, _ = _workload_plugin(deps, "inactive-pack", "DP_ACTIVE_PLUGIN_TOKEN")
    drop_in, _ = _workload_plugin(
        deps, "drop-in-pack", "DP_ACTIVE_PLUGIN_TOKEN", source="drop-in", active=True)
    deps._finalize_plugin_workload_env()

    assert [entry["state"] for entry in (active, failed, inactive, drop_in)] == [
        "active", "failed", "inactive", "active",
    ]
    assert deps._plugin_workload_fields == (("active-pack", active_field),)
    inherited = {"DP_ACTIVE_PLUGIN_TOKEN": "active"}
    assert deps.plugin_workload_env(inherited=inherited) == inherited
    monkeypatch.setenv(WORKLOAD_CHILD_MARKER, json.dumps(deps.plugin_workload_names()))
    for target, value in inherited.items():
        monkeypatch.setenv(target, value)
    registry = Registry(deps)
    for pack, expected in (
        ("active-pack", "active"),
        ("failed-pack", None),
        ("inactive-pack", None),
        ("drop-in-pack", None),
    ):
        registry._pack = pack
        registry._entry = next(entry for entry in deps.plugins if entry["name"] == pack)
        assert registry.config(deps._manifests[pack]["config"][0]["key"]) == expected


def test_cross_plugin_workload_target_conflicts_fail_all_owners_deterministically():
    deps = _workload_status_deps()
    shared_target = "DP_SHARED_PLUGIN_TOKEN"
    beta, _ = _workload_plugin(deps, "beta-pack", shared_target, active=True)
    alpha, _ = _workload_plugin(deps, "alpha-pack", shared_target, active=True)
    safe, safe_field = _workload_plugin(
        deps, "safe-pack", "DP_SAFE_PLUGIN_TOKEN", active=True)
    deps._finalize_plugin_workload_env()

    expected = (
        f"Workload environment target '{shared_target}' conflicts with another active plugin."
    )
    assert alpha["state"] == beta["state"] == "conflict"
    assert alpha["failure_summary"] == beta["failure_summary"] == expected
    assert safe["state"] == "active"
    assert deps._plugin_workload_fields == (("safe-pack", safe_field),)
    inherited = {shared_target: "conflicted", "DP_SAFE_PLUGIN_TOKEN": "safe"}
    assert deps.plugin_workload_env(inherited=inherited) == {"DP_SAFE_PLUGIN_TOKEN": "safe"}


def test_duplicate_installed_entry_point_names_fail_before_loading(tmp_path, monkeypatch):
    import importlib.metadata

    loaded: list[str] = []

    class DuplicateEntryPoint:
        name = "duplicate-installed-pack"
        dist = type("Dist", (), {"name": "duplicate-dist", "version": "1.0.0"})()

        def load(self):
            loaded.append(self.name)
            raise AssertionError("duplicate entry points must not load")

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: [DuplicateEntryPoint(), DuplicateEntryPoint()],
    )

    deps = Deps(str(tmp_path / "workspace"), str(tmp_path / "data"))
    duplicates = [
        entry for entry in deps.plugins
        if entry["name"] == "duplicate-installed-pack"
    ]

    assert loaded == []
    assert len(duplicates) == 2
    assert all(entry["state"] == "conflict" for entry in duplicates)
    assert {entry["failure_summary"] for entry in duplicates} == {
        "Installed plugin entry point name 'duplicate-installed-pack' is declared more than once."
    }


def test_manifestless_entry_point_does_not_inherit_same_named_drop_in(tmp_path, monkeypatch):
    import importlib.metadata

    from hub import metadb

    workspace = tmp_path / "workspace"
    name = "same_name_pack"
    _write_plugin(
        workspace,
        name,
        "from pathlib import Path\n"
        "from hub.sdk import NodeSpec, PortSpec\n"
        "def register(reg):\n"
        "    Path(reg.deps.workspace, 'drop-in-value').write_text(reg.config('token'))\n"
        "    reg.add_node(NodeSpec(kind='same-name-drop-in', title='drop-in', "
        "category='compute', inputs=[], outputs=[PortSpec(id='out')], params=[]))\n",
    )
    workload_config = (
        "[[config]]\n"
        'key = "token"\n'
        'type = "password"\n'
        'env = "{target}"\n'
        "secret = true\n"
        "workload_env = true\n"
    )
    (workspace / "plugins" / name / "dataplay.toml").write_text(
        f'name = "{name}"\nversion = "1.0.0"\n'
        + workload_config.format(target="DP_DROP_IN_TARGET")
    )

    manifestless_observed: list[object] = []

    def register_manifestless(reg):
        manifestless_observed.append(reg.config("token"))
        reg.add_telemetry_sink(lambda _record: None)

    class EntryPoint:
        def __init__(self, entry_name, register):
            self.name = entry_name
            self._register = register
            self.dist = type("Dist", (), {"name": entry_name, "version": "1.0.0"})()

        def load(self):
            return self._register

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: [EntryPoint(name, register_manifestless)],
    )
    monkeypatch.setenv("DP_DROP_IN_SOURCE", "drop-in-material")
    monkeypatch.setenv("DP_DROP_IN_TARGET", "wrong-drop-in-fallback")
    configured = {
        f"plugin.{name}.token": "env:DP_DROP_IN_SOURCE",
    }
    monkeypatch.setattr(
        metadb,
        "get_setting",
        lambda key, _scope, _uid=None, default=None: configured.get(key, default),
    )

    deps = Deps(str(workspace), str(tmp_path / "data"))
    same_name = [entry for entry in deps.plugins if entry["name"] == name]

    assert (workspace / "drop-in-value").read_text() == "drop-in-material"
    assert [entry["source"] for entry in same_name] == ["drop-in", "entry_point"]
    assert same_name[0]["config"][0]["env"] == "DP_DROP_IN_TARGET"
    assert same_name[1]["config"] is None
    assert manifestless_observed == [None]
    assert deps.plugin_workload_env() == {}
