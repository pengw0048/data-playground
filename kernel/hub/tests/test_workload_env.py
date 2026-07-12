"""Child-environment security boundaries for every process execution backend."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from hub.workload_env import (build_workload_credential_env, build_workload_env,
                              build_workload_semantic_env, data_plane_object_store_config,
                              initialize_ephemeral_metadata, prepare_workload_graph)


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
        "DP_RAY_LABELS": "pool=a100",
        "DP_RAY_DRIVER_FALLBACK_MAX_BYTES": "33554432",
        "DP_RAY_GPU_BATCH_ROWS": "8192",
        "DP_DATABASE_URL": "postgresql+psycopg://worker:secret@db/dataplay",
        "DP_STORAGE_URL": "s3://data/output",
        "AWS_ACCESS_KEY_ID": "data-key",
        "AWS_SECRET_ACCESS_KEY": "data-secret",
        "DP_GCS_ENDPOINT": "http://gcs-emulator:4443",
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
    assert env["DP_RAY_LABELS"] == "pool=a100"
    assert env["DP_RAY_GPU_BATCH_ROWS"] == "8192"
    assert env["AWS_SECRET_ACCESS_KEY"] == "data-secret"
    assert env["DP_GCS_ENDPOINT"] == "http://gcs-emulator:4443"
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


def test_blank_auth_secret_does_not_put_open_local_workloads_in_auth_mode():
    source = _source()
    source["DP_AUTH_SECRET"] = "   "

    env = build_workload_env(include_metadata_db=False, source=source)
    semantic = build_workload_semantic_env(source=source)

    assert "DP_AUTH_SECRET" not in env
    assert "DP_AUTH_MODE" not in env
    assert "DP_AUTH_MODE" not in semantic


def test_durable_workload_environment_separates_semantics_from_rotatable_credentials():
    semantic = build_workload_semantic_env(source=_source())
    credentials = build_workload_credential_env(source=_source())
    assert semantic["DP_MEMORY_LIMIT"] == "2GB"
    assert semantic["DP_STORAGE_URL"] == "s3://data/output"
    assert semantic["DP_GCS_ENDPOINT"] == "http://gcs-emulator:4443"
    assert semantic["DP_RAY_GPU_BATCH_ROWS"] == "8192"
    assert "AWS_SECRET_ACCESS_KEY" not in semantic
    assert credentials == {
        "AWS_ACCESS_KEY_ID": "data-key", "AWS_SECRET_ACCESS_KEY": "data-secret",
    }


def test_readable_malformed_job_artifact_is_corruption_not_missing(tmp_path):
    from hub.job_artifacts import ArtifactCorrupt, ArtifactNotFound, read_json_artifact

    malformed = tmp_path / "bad.dpjob"
    malformed.write_text('{"run_id":')
    with pytest.raises(ArtifactCorrupt):
        read_json_artifact(str(malformed))
    with pytest.raises(ArtifactNotFound):
        read_json_artifact(str(tmp_path / "missing.dpjob"))


def test_job_artifact_size_bound_applies_to_writes_and_reads(tmp_path, monkeypatch):
    from hub import job_artifacts

    monkeypatch.setattr(job_artifacts, "JSON_ARTIFACT_MAX_BYTES", 16)
    oversized = tmp_path / "oversized.dpjob"
    with pytest.raises(ValueError, match="16-byte limit"):
        job_artifacts.write_json_artifact(str(oversized), {"payload": "x" * 32})
    oversized.write_bytes(b'{' + b'"x":1,' * 8 + b'}')
    with pytest.raises(job_artifacts.ArtifactCorrupt, match="16-byte limit"):
        job_artifacts.read_json_artifact(str(oversized))


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
    assert env["DP_RAY_DRIVER_FALLBACK_MAX_BYTES"] == "33554432"
    assert env["PATH"].startswith(str(Path(module.sys.executable).parent))


def test_only_global_control_plane_marks_unhandled_backend_jobs(tmp_path, monkeypatch):
    from hub import deps as deps_module
    from hub import metadb

    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    calls: list[set[str]] = []
    monkeypatch.setattr(metadb, "note_unhandled_backend_jobs", lambda backends: calls.append(backends) or 0)
    monkeypatch.setattr(deps_module.settings, "workspace", str(workspace))
    monkeypatch.setattr(deps_module.settings, "data_dir", str(data))

    deps_module.Deps(str(workspace), str(data))
    deps_module.set_workspace(str(workspace), str(data))
    assert calls == []

    monkeypatch.setattr(deps_module, "_deps", None)
    deps_module.get_deps()
    assert len(calls) == 1


def test_ephemeral_worker_seeds_only_allowlisted_object_store_execution_config(tmp_path, monkeypatch):
    from hub import metadb

    monkeypatch.setenv("DP_DATABASE_URL", "sqlite:///original-test.db")
    monkeypatch.setenv("DP_S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("DP_S3_KEY", "data-key")
    monkeypatch.setenv("DP_S3_SECRET", "data-secret")
    monkeypatch.setenv("DP_AUTH_SECRET", "must-not-cross")
    seeded: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(metadb, "init_db", lambda: None)
    monkeypatch.setattr(metadb, "set_setting", lambda key, value, scope: seeded.append((key, value, scope)))

    url = initialize_ephemeral_metadata(str(tmp_path))

    assert url.endswith("/workload-metadata.db")
    assert seeded == [("objectStore", {
        "accessKeyId": "data-key",
        "secretAccessKey": "data-secret",
        "endpoint": "http://minio:9000",
        "useSsl": False,
        "region": "us-east-1",
    }, "global")]
    assert "must-not-cross" not in repr(seeded)


def test_job_artifact_module_does_not_freeze_metadata_settings_before_worker_bootstrap():
    code = (
        "import sys; import hub.job_artifacts; "
        "assert 'hub.settings' not in sys.modules; assert 'hub.metadb' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_data_plane_object_store_config_uses_only_allowlisted_worker_identity():
    cfg = data_plane_object_store_config({
        "DP_S3_KEY": "job-key",
        "DP_S3_SECRET": "job-secret",
        "AWS_SESSION_TOKEN": "job-session",
        "DP_S3_ENDPOINT": "http://minio:9000",
        "AWS_REGION": "us-east-1",
        "DP_DATABASE_URL": "postgresql://control-plane",
        "DP_AUTH_SECRET": "control-secret",
    })

    assert cfg == {
        "accessKeyId": "job-key",
        "secretAccessKey": "job-secret",
        "sessionToken": "job-session",
        "endpoint": "http://minio:9000",
        "useSsl": False,
        "region": "us-east-1",
    }
    mixed = {
        "AWS_ACCESS_KEY_ID": "aws-only",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "DP_GCS_ENDPOINT": "http://gcs:4443",
    }
    assert data_plane_object_store_config(mixed, scheme="s3") == {
        "accessKeyId": "aws-only", "secretAccessKey": "aws-secret"
    }
    assert data_plane_object_store_config(mixed, scheme="gcs") == {
        "endpoint": "http://gcs:4443", "useSsl": False
    }


def test_workload_graph_inlines_named_schema_contract_without_mutating_source():
    from hub import metadb
    from hub.models import Graph

    # This module does not import the ASGI app, whose startup normally initializes metadata.
    # Initialize explicitly so the workload-boundary tests are valid in isolation as well as in the
    # full suite.
    metadb.init_db()
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
    from hub import metadb
    from hub.models import Graph

    metadb.init_db()
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
