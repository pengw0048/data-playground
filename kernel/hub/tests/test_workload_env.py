"""Child-environment security boundaries for every process execution backend."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hub.workload_env import build_workload_env, prepare_workload_graph


_CONTROL_SECRETS = {
    "DP_AUTH_SECRET": "s" * 40,
    "DP_AUTH_PASSWORD": "bootstrap-password",
    "DP_AGENT_API_KEY": "agent-key",
    "OPENAI_API_KEY": "openai-key",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "LUMA_CONTROL_TOKEN": "control-token",
    "UNRELATED_PROVIDER_SECRET": "provider-secret",
}


def _source() -> dict[str, str]:
    return {
        "PATH": "/runtime/bin",
        "DP_MEMORY_LIMIT": "2GB",
        "DP_DATABASE_URL": "postgresql+psycopg://worker:secret@db/dataplay",
        "DP_STORAGE_URL": "s3://data/output",
        "AWS_ACCESS_KEY_ID": "data-key",
        "AWS_SECRET_ACCESS_KEY": "data-secret",
        **_CONTROL_SECRETS,
    }


def _assert_control_secrets_absent(env: dict[str, str]) -> None:
    assert not (_CONTROL_SECRETS.keys() & env.keys())
    assert "provider-secret" not in env.values()


def test_one_shot_workload_environment_is_allowlisted_without_metadata_identity():
    env = build_workload_env(include_metadata_db=False, source=_source())
    _assert_control_secrets_absent(env)
    assert env["PATH"] == "/runtime/bin" and env["DP_MEMORY_LIMIT"] == "2GB"
    assert env["DP_STORAGE_URL"] == "s3://data/output"
    assert env["AWS_SECRET_ACCESS_KEY"] == "data-secret"
    assert "DP_DATABASE_URL" not in env
    assert env["DP_AUTH_MODE"] == "1"  # derived confinement signal, never signing material


def test_subrun_backend_uses_the_one_shot_profile(monkeypatch):
    from hub.subprocess_runner import _subrun_child_env

    for key, value in _source().items():
        monkeypatch.setenv(key, value)
    env = _subrun_child_env()
    _assert_control_secrets_absent(env)
    assert "DP_DATABASE_URL" not in env
    assert env["AWS_SECRET_ACCESS_KEY"] == "data-secret"


def test_long_lived_kernel_profile_only_adds_the_current_metadata_bridge():
    env = build_workload_env(include_metadata_db=True, source=_source())
    _assert_control_secrets_absent(env)
    assert env["DP_DATABASE_URL"].startswith("postgresql+")
    assert env["DP_AUTH_MODE"] == "1"


def test_pod_manifest_uses_the_same_explicit_profile(monkeypatch):
    from hub.pod_spawner import PodSpawner

    for key, value in _source().items():
        monkeypatch.setenv(key, value)
    pod = PodSpawner("/workspace", "/data")._pod_body("kernel", ["python", "-m", "hub.kernel"])
    env = {entry["name"]: entry["value"] for entry in pod["spec"]["containers"][0]["env"]}
    _assert_control_secrets_absent(env)
    assert env["DP_DATABASE_URL"].startswith("postgresql+")
    assert env["AWS_SECRET_ACCESS_KEY"] == "data-secret"
    assert "PATH" not in env  # supplied by the image, not copied from the hub container


def test_ray_driver_uses_one_shot_profile(monkeypatch):
    for key, value in _source().items():
        monkeypatch.setenv(key, value)
    src = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "dp_ray" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dp_ray_workload_env_test", src)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    env = module._ray_child_env()
    _assert_control_secrets_absent(env)
    assert "DP_DATABASE_URL" not in env
    assert env["AWS_SECRET_ACCESS_KEY"] == "data-secret"
    assert env["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "0"
    assert env["PATH"].startswith(str(Path(module.sys.executable).parent))


def test_workload_graph_inlines_named_schema_contract_without_mutating_source():
    from hub import metadb
    from hub.models import Graph

    metadb.save_schema_contract("isolated_worker_contract", [{"name": "v", "type": "int"}])
    graph = Graph.model_validate({
        "id": "contract-worker",
        "version": 1,
        "nodes": [{
            "id": "transform",
            "type": "transform",
            "position": {"x": 0, "y": 0},
            "data": {"config": {
                "outputSchema": {"ref": "isolated_worker_contract"},
                "enforceSchema": True,
            }},
        }],
        "edges": [],
    })

    payload = prepare_workload_graph(graph)

    assert payload["nodes"][0]["data"]["config"]["outputSchema"] == [{"name": "v", "type": "int"}]
    assert graph.nodes[0].data["config"]["outputSchema"] == {"ref": "isolated_worker_contract"}


def test_workload_graph_keeps_missing_contract_for_fail_closed_enforcement():
    from hub.models import Graph

    graph = Graph.model_validate({
        "id": "missing-contract-worker",
        "version": 1,
        "nodes": [{
            "id": "transform",
            "type": "transform",
            "position": {"x": 0, "y": 0},
            "data": {"config": {
                "outputSchema": {"ref": "missing_isolated_worker_contract"},
                "enforceSchema": True,
            }},
        }],
        "edges": [],
    })

    payload = prepare_workload_graph(graph)

    assert payload["nodes"][0]["data"]["config"]["outputSchema"] == {
        "ref": "missing_isolated_worker_contract"
    }
