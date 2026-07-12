"""Ray version and private-ABI gates that do not require a live Ray dependency."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hub import ray_compat


_ROOT = Path(__file__).resolve().parents[3]


class _Probe:
    def __init__(self):
        self.node_id = None

    def options(self, *, scheduling_strategy):
        self.node_id = scheduling_strategy
        return self

    def remote(self):
        return self.node_id


class _FakeRay:
    def __init__(self, driver_version, worker_versions):
        self.__version__ = driver_version
        self.worker_versions = worker_versions

    def nodes(self):
        return [{
            "Alive": True,
            "NodeID": node_id,
            "NodeManagerAddress": f"10.0.0.{index}",
        } for index, node_id in enumerate(self.worker_versions, start=1)]

    @staticmethod
    def remote(**_options):
        return lambda _fn: _Probe()

    def get(self, refs, timeout=None):
        assert timeout == 30
        return [self.worker_versions[node_id] for node_id in refs]


def test_ray_cluster_version_handshake_accepts_the_supported_version(monkeypatch):
    monkeypatch.setattr(ray_compat, "_node_affinity", lambda node_id: node_id)
    ray = _FakeRay("2.56.0", {"node-a": "2.56.0", "node-b": "2.56.0"})

    report = ray_compat.validate_ray_cluster(ray)

    assert set(report.values()) == {"2.56.0"}
    assert len(report) == 2


def test_ray_cluster_version_handshake_rejects_mixed_workers(monkeypatch):
    monkeypatch.setattr(ray_compat, "_node_affinity", lambda node_id: node_id)
    ray = _FakeRay("2.56.0", {"node-a": "2.56.0", "node-b": "2.55.0"})

    with pytest.raises(ray_compat.RayCompatibilityError, match="mixed Ray versions") as exc:
        ray_compat.validate_ray_cluster(ray)

    assert "node-b" in str(exc.value) or "10.0.0.2" in str(exc.value)
    assert "exactly Ray 2.56.0" in str(exc.value)


def test_ray_version_contract_rejects_an_unsupported_uniform_cluster():
    with pytest.raises(ray_compat.RayCompatibilityError, match="unsupported Ray version") as exc:
        ray_compat.validate_ray_versions("2.57.0", {"node-a": "2.57.0"})

    assert "data-playground[ray]" in str(exc.value)


def test_hash_shuffle_feature_detection_checks_before_dereference():
    with pytest.raises(ray_compat.RayCompatibilityError, match="_hash_partition"):
        ray_compat._require_hash_shuffle_abi(SimpleNamespace())

    with pytest.raises(ray_compat.RayCompatibilityError, match="schema helper"):
        ray_compat._require_hash_shuffle_abi(SimpleNamespace(_hash_partition=lambda *_args: None))


def test_hash_shuffle_feature_detection_returns_the_validated_target():
    target = lambda *_args: None
    module = SimpleNamespace(
        _hash_partition=target,
        _has_unhashable_pandas_types=lambda _schema: False,
    )

    assert ray_compat._require_hash_shuffle_abi(module) is target


def test_ray_dependency_image_and_kuberay_manifest_share_one_version_contract():
    pyproject = tomllib.loads((_ROOT / "kernel/pyproject.toml").read_text())
    assert pyproject["project"]["optional-dependencies"]["ray"] == [
        f"ray[data]=={ray_compat.SUPPORTED_RAY_VERSION}"
    ]
    lock = (_ROOT / "kernel/uv.lock").read_text()
    assert f'specifier = "=={ray_compat.SUPPORTED_RAY_VERSION}"' in lock
    dockerfile = (_ROOT / "docker/ray/Dockerfile").read_text()
    assert "uv sync --extra ray" in dockerfile
    assert "uv pip install 'ray" not in dockerfile
    manifest = (_ROOT / "deploy/kuberay/raycluster.yaml").read_text()
    assert f'rayVersion: "{ray_compat.SUPPORTED_RAY_VERSION}"' in manifest


def _assert_restricted_pod(pod: dict, container_name: str) -> None:
    assert pod["automountServiceAccountToken"] is False
    pod_security = pod["securityContext"]
    assert pod_security["runAsNonRoot"] is True
    assert (pod_security["runAsUser"], pod_security["runAsGroup"], pod_security["fsGroup"]) == (
        10001, 10001, 10001,
    )
    assert pod_security["seccompProfile"] == {"type": "RuntimeDefault"}
    container = next(item for item in pod["containers"] if item["name"] == container_name)
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def test_ray_image_and_validation_pods_enforce_a_non_root_security_boundary():
    dockerfile = (_ROOT / "docker/ray/Dockerfile").read_text()
    assert "USER 10001:10001" in dockerfile
    assert 'ENV HOME="/home/dataplay"' in dockerfile

    cluster = yaml.safe_load((_ROOT / "deploy/kuberay/raycluster.yaml").read_text())
    _assert_restricted_pod(cluster["spec"]["headGroupSpec"]["template"]["spec"], "ray-head")
    for worker in cluster["spec"]["workerGroupSpecs"]:
        _assert_restricted_pod(worker["template"]["spec"], "ray-worker")

    job = yaml.safe_load((_ROOT / "deploy/kuberay/differential-job.yaml").read_text())
    _assert_restricted_pod(job["spec"]["template"]["spec"], "driver")
