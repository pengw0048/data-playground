"""Ray version and private-ABI gates that do not require a live Ray dependency."""

from __future__ import annotations

import os
import subprocess
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
        f"ray[data,default]=={ray_compat.SUPPORTED_RAY_VERSION}"
    ]
    lock = (_ROOT / "kernel/uv.lock").read_text()
    assert f'specifier = "=={ray_compat.SUPPORTED_RAY_VERSION}"' in lock
    dockerfile = (_ROOT / "docker/ray/Dockerfile").read_text()
    assert "uv sync --locked --no-dev --extra ray" in dockerfile
    assert "uv pip install 'ray" not in dockerfile
    # Digest-pinned base + exact uv installer: a mutable tag or unpinned pip install would reopen OPS-02.
    assert "FROM python:3.12-slim@sha256:" in dockerfile
    assert "pip install --no-cache-dir uv==" in dockerfile
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
    assert "ln -s /app/kernel/.venv/bin/ray /usr/local/bin/ray" in dockerfile

    cluster = yaml.safe_load((_ROOT / "deploy/kuberay/raycluster.yaml").read_text())
    _assert_restricted_pod(cluster["spec"]["headGroupSpec"]["template"]["spec"], "ray-head")
    for worker in cluster["spec"]["workerGroupSpecs"]:
        _assert_restricted_pod(worker["template"]["spec"], "ray-worker")

    job = yaml.safe_load((_ROOT / "deploy/kuberay/differential-job.yaml").read_text())
    _assert_restricted_pod(job["spec"]["template"]["spec"], "driver")


def test_kuberay_validation_profile_fits_a_four_cpu_kind_node_and_recreates_workloads():
    cluster = yaml.safe_load((_ROOT / "deploy/kuberay/raycluster.yaml").read_text())
    head = cluster["spec"]["headGroupSpec"]
    workers = cluster["spec"]["workerGroupSpecs"][0]
    job = yaml.safe_load((_ROOT / "deploy/kuberay/differential-job.yaml").read_text())
    head_pod = head["template"]["spec"]
    worker_pod = workers["template"]["spec"]
    job_pod = job["spec"]["template"]["spec"]
    head_container = head_pod["containers"][0]
    worker_container = worker_pod["containers"][0]
    driver = job["spec"]["template"]["spec"]["containers"][0]

    assert head["rayStartParams"]["num-cpus"] == "2"
    assert workers["rayStartParams"]["num-cpus"] == "2"
    assert workers["replicas"] == workers["minReplicas"] == workers["maxReplicas"] == 2
    assert "--num-cpus=2" in driver["command"][2]
    assert "--object-store-memory=536870912" in driver["command"][2]

    cpu_requests = [
        head_container["resources"]["requests"]["cpu"],
        worker_container["resources"]["requests"]["cpu"],
        driver["resources"]["requests"]["cpu"],
    ]
    assert cpu_requests == ["600m", "600m", "500m"]
    requested_millicpu = (
        int(cpu_requests[0][:-1])
        + (2 * int(cpu_requests[1][:-1]))
        + int(cpu_requests[2][:-1])
    )
    assert requested_millicpu == 2300  # leaves 1700m for kind/KubeRay/MinIO on a 4-CPU node
    assert [
        head_container["resources"]["requests"]["memory"],
        worker_container["resources"]["requests"]["memory"],
        driver["resources"]["requests"]["memory"],
    ] == ["1500Mi", "1500Mi", "1Gi"]
    assert [
        head_container["resources"]["limits"],
        worker_container["resources"]["limits"],
        driver["resources"]["limits"],
    ] == [
        {"cpu": "2", "memory": "2Gi"},
        {"cpu": "2", "memory": "2Gi"},
        {"cpu": "2", "memory": "2Gi"},
    ]
    assert [
        head_pod["volumes"][0]["emptyDir"]["sizeLimit"],
        worker_pod["volumes"][0]["emptyDir"]["sizeLimit"],
        job_pod["volumes"][0]["emptyDir"]["sizeLimit"],
    ] == ["1Gi", "1Gi", "1Gi"]

    script = (_ROOT / "deploy/kuberay/validate.sh").read_text()
    assert "dp-ray:kuberay-$(date +%Y%m%d%H%M%S)-$$" in script
    assert 'kind get kubeconfig --name "${KIND_CLUSTER}"' in script
    assert 'kubectl --kubeconfig "${KIND_KUBECONFIG}"' in script
    assert "delete job dp-ray-multinode-check dp-ray-createbucket" in script
    assert "delete raycluster dp-ray" in script
    assert script.count("--cascade=foreground") == 2
    assert 'wait_for_no_pods "ray.io/cluster=dp-ray"' in script
    assert 'wait_for_no_pods "batch.kubernetes.io/job-name=dp-ray-multinode-check"' in script
    assert 'if (( ${failed:-0} >= 1 ))' in script
    assert 'sed "s|image: dp-ray:local|image: ${IMAGE}|g"' in script


def test_kuberay_validation_fails_closed_when_old_pod_listing_fails(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "kind": "#!/bin/sh\necho 'apiVersion: v1'\n",
        "docker": "#!/bin/sh\nexit 0\n",
        "kubectl": (
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" get pods -l ray.io/cluster=dp-ray \"*) exit 42 ;;\n"
            "esac\n"
            "exit 0\n"
        ),
    }.items():
        executable = fake_bin / name
        executable.write_text(body)
        executable.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        "KIND_CLUSTER": "fail-closed-test",
        "DP_RAY_VALIDATION_IMAGE": "dp-ray:fail-closed-test",
        "DP_RAY_VALIDATION_TIMEOUT_SECONDS": "08",
    })
    result = subprocess.run(
        [_ROOT / "deploy/kuberay/validate.sh"], env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "could not list old RayCluster pods; refusing to recreate" in result.stderr
