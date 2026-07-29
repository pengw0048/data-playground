"""Regression tests for the repository's fast-PR / heavy-release workflow boundary."""

import re
from collections import Counter
from pathlib import Path

import yaml

from hub import metadb


_ROOT = Path(__file__).resolve().parents[3]

_KERNEL_SHARD_ENV = {
    "kernel": "KERNEL_FILES",
    "lifecycle": "LIFECYCLE_FILES",
    "remainder-0": "GROUP_0_FILES",
    "remainder-1": "GROUP_1_FILES",
}

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
    "kernel/hub/run_controller.py",
    "kernel/hub/planner.py",
    "kernel/hub/estimate.py",
    "kernel/hub/executors/schema.py",
    "kernel/hub/cli.py",
    "kernel/hub/migrations/**",
}

_POSTGRES_DURABLE_TASK_SELECTORS = {
    "managed_local_write": ("hub/tests/test_durable_local_write_tasks.py -k postgres",),
    "external_wait": ("hub/tests/test_durable_external_wait_tasks.py",),
    "linear_checkpoint_write": (
        "hub/tests/test_linear_checkpoint_admission.py",
        "hub/tests/test_linear_checkpoint_commit.py",
        "hub/tests/test_linear_checkpoint_lifecycle.py",
    ),
    "bounded_fanout_write": ("hub/tests/test_bounded_fanout_write_tasks.py",),
    "merge_columns_write": (
        "hub/tests/test_merge_columns.py -k postgres",
        "hub/tests/test_merge_columns_api.py -k postgres",
    ),
    "restore_revision_write": (
        "hub/tests/test_restore_revision.py",
        "restore_old_revision_publishes_new_head",
        "restart_recovery_publishes_a_pending_task",
    ),
    "keyed_upsert_write": (
        "hub/tests/test_keyed_upsert_api.py",
        "postgres_submission_serializes_on_the_owner_row",
        "postgres_runtime_claim_recovers_and_fences_expired_owner",
    ),
    "distribution_report": ("hub/tests/test_distribution_reports.py",),
}


def _workflow(name: str) -> dict:
    parsed = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )
    # PyYAML applies YAML 1.1 and reads GitHub's `on` key as boolean True. Normalize only that key.
    if True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def _kernel_shard_step() -> dict:
    return next(
        step
        for step in _workflow("ci.yml")["jobs"]["kernel-shards"]["steps"]
        if step.get("name") == "Run shard ${{ matrix.shard }}"
    )


def _pull_request_paths(name: str) -> set[str]:
    return set(_workflow(name)["on"]["pull_request"]["paths"])


def _is_owned(path: str, patterns: set[str]) -> bool:
    def _matches(pattern: str) -> bool:
        # GitHub path globs treat `*` as one path segment and `**` as recursive. Python's fnmatch
        # lets `*` cross `/`, so using it here would make the policy test broader than Actions.
        parts: list[str] = []
        index = 0
        while index < len(pattern):
            if pattern.startswith("**", index):
                parts.append(".*")
                index += 2
            elif pattern[index] == "*":
                parts.append("[^/]*")
                index += 1
            elif pattern[index] == "?":
                parts.append("[^/]")
                index += 1
            else:
                parts.append(re.escape(pattern[index]))
                index += 1
        return re.fullmatch("".join(parts), path) is not None

    return any(_matches(pattern) for pattern in patterns)


def test_lean_validation_runs_on_pull_requests_and_main() -> None:
    for name in ("ci.yml", "codeql.yml", "secret-scan.yml"):
        events = _workflow(name)["on"]
        assert "pull_request" in events
        assert events["push"]["branches"] == ["main"]


def test_kernel_pytest_shards_form_an_exact_complement_partition() -> None:
    job = _workflow("ci.yml")["jobs"]["kernel-shards"]
    expected_shards = [*_KERNEL_SHARD_ENV, "remainder-2"]
    assert job["strategy"]["matrix"]["shard"] == expected_shards

    step = _kernel_shard_step()
    explicit_groups = {
        shard: tuple(step["env"][variable].split())
        for shard, variable in _KERNEL_SHARD_ENV.items()
    }
    explicit_names = [
        name for group in explicit_groups.values() for name in group
    ]

    tests_dir = _ROOT / "kernel" / "hub" / "tests"
    test_paths = sorted(tests_dir.rglob("test_*.py"))
    relative_paths = [path.relative_to(tests_dir).as_posix() for path in test_paths]
    nested_paths = [path for path in relative_paths if "/" in path]
    assert not nested_paths, (
        "kernel shard selection uses basenames and cannot address nested tests: "
        f"{nested_paths}"
    )

    test_names = [path.name for path in test_paths]
    basename_counts = Counter(test_names)
    basename_collisions = {
        name: count for name, count in basename_counts.items() if count != 1
    }
    assert not basename_collisions

    all_tests = set(test_names)
    missing_explicit = set(explicit_names) - all_tests
    assert not missing_explicit, f"kernel shards name missing tests: {missing_explicit}"

    explicit_counts = Counter(explicit_names)
    duplicate_explicit = {
        name: count for name, count in explicit_counts.items() if count != 1
    }
    assert not duplicate_explicit, (
        f"kernel explicit shard groups overlap: {duplicate_explicit}"
    )

    complement = all_tests - set(explicit_names)
    assert complement, "remainder-2 must remain a non-empty catch-all"

    selected_counts = Counter(explicit_names)
    selected_counts.update(complement)
    assert selected_counts == basename_counts

    command = step["run"]
    normalized_command = " ".join(command.split())
    assert "set -euo pipefail" in normalized_command
    assert (
        'explicit="$KERNEL_FILES $LIFECYCLE_FILES $GROUP_0_FILES $GROUP_1_FILES"'
        in normalized_command
    )
    for shard, variable in _KERNEL_SHARD_ENV.items():
        assert f'{shard}) names="${variable}" ;;' in normalized_command
    assert "remainder-2) names=$(comm -23" in normalized_command
    assert (
        "find hub/tests -name 'test_*.py' -exec basename {} \\; | sort"
        in normalized_command
    )
    assert "<(printf '%s\\n' $explicit | sort)" in normalized_command
    assert 'test -n "$names"' in normalized_command


def test_every_admitted_durable_task_kind_has_a_postgres_ci_selector() -> None:
    kind_constraint = next(
        constraint
        for constraint in metadb.DurableTask.__table__.constraints
        if constraint.name == "ck_durable_task_kind"
    )
    admitted = set(re.findall(r"'([^']+)'", str(kind_constraint.sqltext)))
    assert admitted == set(_POSTGRES_DURABLE_TASK_SELECTORS)

    job = _workflow("ci.yml")["jobs"]["postgres-migration"]
    commands = " ".join(
        step.get("run", "") for step in job["steps"] if step.get("run")
    )
    commands = " ".join(commands.split())
    for kind, selectors in _POSTGRES_DURABLE_TASK_SELECTORS.items():
        for selector in selectors:
            assert selector in commands, f"{kind} is missing PostgreSQL CI selector {selector!r}"


def test_release_reuses_core_checks_at_the_candidate_revision() -> None:
    expected = {
        "core-ci": "./.github/workflows/ci.yml",
        "codeql": "./.github/workflows/codeql.yml",
        "secret-scan": "./.github/workflows/secret-scan.yml",
    }
    for name in ("ci.yml", "codeql.yml", "secret-scan.yml"):
        workflow_call = _workflow(name)["on"]["workflow_call"]
        expected_sha = workflow_call["inputs"]["expected_sha"]
        assert expected_sha["required"] is True
        assert expected_sha["type"] == "string"

    jobs = _workflow("release.yml")["jobs"]
    assert jobs["release-identity"]["outputs"] == {
        "sha": "${{ steps.commit.outputs.sha }}"
    }
    identity_checkout = jobs["release-identity"]["steps"][0]
    assert identity_checkout["with"]["ref"] == "${{ github.sha }}"
    publish_checkout = jobs["publish"]["steps"][0]
    assert publish_checkout["with"]["ref"] == "${{ needs.release-identity.outputs.sha }}"
    for job, called_workflow in expected.items():
        assert jobs[job]["uses"] == called_workflow
        assert jobs[job]["needs"] == "release-identity"
        assert jobs[job]["with"] == {
            "expected_sha": "${{ needs.release-identity.outputs.sha }}"
        }

    for name in ("ci.yml", "codeql.yml", "secret-scan.yml"):
        workflow_jobs = _workflow(name)["jobs"].values()
        for job in workflow_jobs:
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["ref"] == "${{ inputs.expected_sha || github.sha }}"


def test_ci_cancels_only_superseded_branch_event_runs() -> None:
    concurrency = _workflow("ci.yml")["concurrency"]
    assert concurrency == {
        "group": (
            "ci-${{ inputs.expected_sha && github.run_id || "
            "github.event_name == 'pull_request' && github.event.pull_request.number || "
            "github.event_name == 'push' && github.ref || github.run_id }}"
        ),
        "cancel-in-progress": (
            "${{ !inputs.expected_sha && "
            "(github.event_name == 'pull_request' || github.event_name == 'push') }}"
        ),
    }

    # `workflow_call` inherits the release caller's tag-push event context. The immutable input must
    # therefore win before event_name is considered, while direct PR/main events remain cancellable.
    cases = [
        # event_name, expected_sha, expected group identity, cancel superseded
        ("pull_request", "", "pull_request_number", True),
        ("push", "", "ref", True),
        ("workflow_dispatch", "", "run_id", False),
        ("push", "immutable-release-sha", "run_id", False),
    ]
    for event_name, expected_sha, expected_group, expected_cancel in cases:
        group_identity = (
            "run_id" if expected_sha
            else "pull_request_number" if event_name == "pull_request"
            else "ref" if event_name == "push"
            else "run_id"
        )
        cancel = not expected_sha and event_name in {"pull_request", "push"}
        assert (group_identity, cancel) == (expected_group, expected_cancel)


def test_release_workflow_requires_a_clean_version_identity() -> None:
    commands = [step.get("run", "") for step in _workflow("release.yml")["jobs"]["publish"]["steps"]]
    release_checks = [command for command in commands if "check_release_versions.py" in command]
    assert len(release_checks) == 2
    assert all("--release" in command for command in release_checks)


def test_release_artifact_smokes_use_the_canonical_package_version() -> None:
    jobs = _workflow("release-artifacts.yml")["jobs"]
    for job in ("wheel-smoke", "image-smoke"):
        read_version = next(
            step for step in jobs[job]["steps"] if step.get("name") == "Read package version")
        command = read_version["run"]
        assert "scripts/check_release_versions.py" in command
        assert "--pyproject kernel/pyproject.toml" in command
        assert "--print-version" in command
        assert "re.search" not in command


def test_required_e2e_does_not_run_the_smoke_suite_twice() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    commands = [step.get("run", "") for step in jobs["e2e"]["steps"]]
    playwright_runs = [
        command for command in commands if "e2e-timing.mjs phase playwright-run" in command
    ]
    assert len(playwright_runs) == 1
    assert sum(command.count("cd web && npm run e2e") for command in commands) == 1
    assert "cd web && npm run e2e" in playwright_runs[0]
    config = (_ROOT / "web" / "playwright.config.ts").read_text(encoding="utf-8")
    assert "name: 'chromium-first-run'" in config
    assert "grep: /@first-run/" in config
    assert "name: 'chromium-ux-smoke'" in config
    assert "dependencies: ['chromium-first-run']" in config
    assert "grep: /@ux-smoke/" in config
    assert "dependencies: ['chromium-ux-smoke']" in config
    assert "grepInvert: /@ux-smoke|@first-run/" in config


def test_non_subsystem_heavy_acceptance_is_not_a_pull_request_gate() -> None:
    expected_events = {
        "release-artifacts.yml": {"workflow_dispatch", "workflow_call"},
        "upgrade-drill.yml": {"schedule", "workflow_dispatch", "workflow_call"},
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
    assert _is_owned("kernel/hub/run_controller.py", jobs)
    assert _is_owned("kernel/hub/planner.py", jobs)
    assert _is_owned("kernel/hub/migrations/versions/revision.py", jobs)
    assert not _is_owned("kernel/hub/migrations/versions/revision.py", ray)
    assert not _is_owned("deploy/kuberay/overlays/prod.yaml", ray)

    for docs_only in ("README.md", "docs/CI.md", "docs/RAY.md"):
        assert not _is_owned(docs_only, ray)
        assert not _is_owned(docs_only, jobs)


def test_release_publish_waits_for_every_required_gate() -> None:
    jobs = _workflow("release.yml")["jobs"]
    expected = {
        "artifacts": "./.github/workflows/release-artifacts.yml",
        "upgrade-drill": "./.github/workflows/upgrade-drill.yml",
        "ux-acceptance": "./.github/workflows/ux-acceptance.yml",
        "ray-validation": "./.github/workflows/ray-validation.yml",
        "ray-jobs-acceptance": "./.github/workflows/ray-jobs-acceptance.yml",
    }
    for job, called_workflow in expected.items():
        assert jobs[job]["uses"] == called_workflow
    core_gates = {
        "core-ci": "./.github/workflows/ci.yml",
        "codeql": "./.github/workflows/codeql.yml",
        "secret-scan": "./.github/workflows/secret-scan.yml",
    }
    for job, called_workflow in core_gates.items():
        assert jobs[job]["uses"] == called_workflow
    assert set(jobs["publish"]["needs"]) == {"release-identity", *expected, *core_gates}


def test_upgrade_drill_uses_exact_candidate_and_both_metadata_backends() -> None:
    workflow = _workflow("upgrade-drill.yml")
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch", "workflow_call"}
    assert "pull_request" not in workflow["on"] and "push" not in workflow["on"]
    expected_sha = workflow["on"]["workflow_call"]["inputs"]["expected_sha"]
    assert expected_sha == {"description": "Immutable candidate commit to build and certify.",
                            "required": True, "type": "string"}
    steps = workflow["jobs"]["upgrade"]["steps"]
    assert steps[0]["with"]["ref"] == "${{ inputs.expected_sha || github.sha }}"
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "--backend sqlite" in commands
    assert "--backend postgres" in commands

    release_job = _workflow("release.yml")["jobs"]["upgrade-drill"]
    assert release_job["needs"] == "release-identity"
    assert release_job["uses"] == "./.github/workflows/upgrade-drill.yml"
    assert release_job["with"] == {
        "expected_sha": "${{ needs.release-identity.outputs.sha }}"
    }
