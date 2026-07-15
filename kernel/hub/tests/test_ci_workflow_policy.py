"""Regression tests for the repository's fast-PR / heavy-release workflow boundary."""

from fnmatch import fnmatchcase
from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parents[3]

_RAY_SHARED_PATHS = {
    ".dockerignore",
    "docker/ray/**",
    "kernel/hatch_build.py",
    "kernel/pyproject.toml",
    "kernel/uv.lock",
    "examples/plugins/dp_ray/**",
    "kernel/hub/ray_compat.py",
    "kernel/hub/backends.py",
    "kernel/hub/compiler.py",
    "kernel/hub/db.py",
    "kernel/hub/deps.py",
    "kernel/hub/destinations.py",
    "kernel/hub/graph.py",
    "kernel/hub/handoff.py",
    "kernel/hub/ir.py",
    "kernel/hub/job_artifacts.py",
    "kernel/hub/metadb.py",
    "kernel/hub/models.py",
    "kernel/hub/nodespecs.py",
    "kernel/hub/paths.py",
    "kernel/hub/placement.py",
    "kernel/hub/sandbox.py",
    "kernel/hub/secrets.py",
    "kernel/hub/settings.py",
    "kernel/hub/sinks.py",
    "kernel/hub/sqlanalyze.py",
    "kernel/hub/sqlpolicy.py",
    "kernel/hub/storage.py",
    "kernel/hub/workload_*.py",
    "kernel/hub/executors/engine.py",
    "kernel/hub/plugins/adapters.py",
    "kernel/hub/plugins/catalog.py",
    "kernel/hub/plugins/capabilities.py",
    "kernel/hub/plugins/default_catalog.py",
    "kernel/hub/plugins/processors.py",
    "kernel/hub/plugins/runner.py",
}

_RAY_VALIDATION_PATHS = _RAY_SHARED_PATHS | {
    ".github/workflows/ray-validation.yml",
    "docker-compose.ray.yml",
    "kernel/hub/ray_gpu_contract_check.py",
    "kernel/hub/ray_multinode_check.py",
    "deploy/kuberay/validate.sh",
    "deploy/kuberay/*.yaml",
}

_RAY_JOBS_PATHS = _RAY_SHARED_PATHS | {
    ".github/workflows/ray-jobs-acceptance.yml",
    "docker-compose.ray-jobs.yml",
    "scripts/ray-jobs-acceptance.sh",
    "kernel/hub/ray_jobs_acceptance.py",
    "kernel/hub/ray_jobs_acceptance_entrypoint.py",
    "kernel/hub/routers/runs.py",
    "kernel/hub/estimate.py",
    "kernel/hub/executors/schema.py",
    "kernel/hub/cli.py",
    "kernel/hub/migrations/**",
}


def _workflow(name: str) -> dict:
    parsed = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )
    # PyYAML applies YAML 1.1 and reads GitHub's `on` key as boolean True. Normalize only that key.
    if True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def _pull_request_paths(name: str) -> set[str]:
    return set(_workflow(name)["on"]["pull_request"]["paths"])


def _is_owned(path: str, patterns: set[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def test_lean_validation_runs_on_pull_requests_and_main() -> None:
    for name in ("ci.yml", "codeql.yml", "secret-scan.yml"):
        events = _workflow(name)["on"]
        assert "pull_request" in events
        assert events["push"]["branches"] == ["main"]


def test_required_e2e_does_not_run_the_smoke_suite_twice() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    commands = [step.get("run", "") for step in jobs["e2e"]["steps"]]
    assert commands.count("cd web && npm run e2e") == 1
    config = (_ROOT / "web" / "playwright.config.ts").read_text(encoding="utf-8")
    assert "name: 'chromium-ux-smoke'" in config
    assert "grep: /@ux-smoke/" in config
    assert "dependencies: ['chromium-ux-smoke']" in config
    assert "grepInvert: /@ux-smoke/" in config


def test_non_subsystem_heavy_acceptance_is_not_a_pull_request_gate() -> None:
    expected_events = {
        "release-artifacts.yml": {"workflow_dispatch", "workflow_call"},
        "ux-acceptance.yml": {"schedule", "workflow_dispatch", "workflow_call"},
    }
    for name, expected in expected_events.items():
        assert set(_workflow(name)["on"]) == expected


def test_ray_acceptance_is_path_gated_on_pull_requests() -> None:
    expected_events = {"pull_request", "schedule", "workflow_dispatch", "workflow_call"}
    for name in ("ray-validation.yml", "ray-jobs-acceptance.yml"):
        events = _workflow(name)["on"]
        assert set(events) == expected_events
        assert "push" not in events

    assert _pull_request_paths("ray-validation.yml") == _RAY_VALIDATION_PATHS
    assert _pull_request_paths("ray-jobs-acceptance.yml") == _RAY_JOBS_PATHS


def test_ray_path_ownership_routes_representative_changes() -> None:
    ray = _pull_request_paths("ray-validation.yml")
    jobs = _pull_request_paths("ray-jobs-acceptance.yml")

    for shared in (
        "examples/plugins/dp_ray/__init__.py",
        "docker/ray/Dockerfile",
        "kernel/hub/storage.py",
        "kernel/hub/workload_env.py",
    ):
        assert _is_owned(shared, ray)
        assert _is_owned(shared, jobs)

    assert _is_owned("kernel/hub/ray_multinode_check.py", ray)
    assert not _is_owned("kernel/hub/ray_multinode_check.py", jobs)
    assert _is_owned("deploy/kuberay/raycluster.yaml", ray)
    assert not _is_owned("deploy/kuberay/raycluster.yaml", jobs)

    assert _is_owned("kernel/hub/ray_jobs_acceptance.py", jobs)
    assert not _is_owned("kernel/hub/ray_jobs_acceptance.py", ray)
    assert _is_owned("kernel/hub/migrations/versions/revision.py", jobs)
    assert not _is_owned("kernel/hub/migrations/versions/revision.py", ray)

    for docs_only in ("README.md", "docs/CI.md", "docs/RAY.md"):
        assert not _is_owned(docs_only, ray)
        assert not _is_owned(docs_only, jobs)


def test_release_publish_waits_for_every_heavy_acceptance_gate() -> None:
    jobs = _workflow("release.yml")["jobs"]
    expected = {
        "artifacts": "./.github/workflows/release-artifacts.yml",
        "ux-acceptance": "./.github/workflows/ux-acceptance.yml",
        "ray-validation": "./.github/workflows/ray-validation.yml",
        "ray-jobs-acceptance": "./.github/workflows/ray-jobs-acceptance.yml",
    }
    for job, called_workflow in expected.items():
        assert jobs[job]["uses"] == called_workflow
    assert set(jobs["publish"]["needs"]) == set(expected)
