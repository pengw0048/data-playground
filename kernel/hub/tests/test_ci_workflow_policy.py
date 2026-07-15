"""Regression tests for the repository's fast-PR / heavy-release workflow boundary."""

from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parents[3]


def _workflow(name: str) -> dict:
    parsed = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )
    # PyYAML applies YAML 1.1 and reads GitHub's `on` key as boolean True. Normalize only that key.
    if True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


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


def test_heavy_acceptance_is_manual_scheduled_or_release_callable() -> None:
    expected_events = {
        "release-artifacts.yml": {"workflow_dispatch", "workflow_call"},
        "ux-acceptance.yml": {"schedule", "workflow_dispatch", "workflow_call"},
        "ray-validation.yml": {"schedule", "workflow_dispatch", "workflow_call"},
        "ray-jobs-acceptance.yml": {"schedule", "workflow_dispatch", "workflow_call"},
    }
    for name, expected in expected_events.items():
        assert set(_workflow(name)["on"]) == expected


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
