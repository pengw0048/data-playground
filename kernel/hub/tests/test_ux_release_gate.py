from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_gate():
    path = Path(__file__).resolve().parents[3] / "scripts" / "ux_release_gate.py"
    spec = importlib.util.spec_from_file_location("ux_release_gate_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_gate_blocks_open_p0_and_p1_ux_defects_but_not_tracking_or_prs():
    gate = _load_gate()
    issues = [
        {"number": 174, "title": "tracking", "labels": [{"name": "P1"}, {"name": "ux"}]},
        {"number": 160, "title": "data loss", "labels": [{"name": "P0"}, {"name": "ux"}]},
        {"number": 164, "title": "stale preview", "labels": [{"name": "P1"}, {"name": "ux"}]},
        {"number": 173, "title": "responsive", "labels": [{"name": "P2"}, {"name": "ux"}]},
        {"number": 123, "title": "unrelated P1", "labels": [{"name": "P1"}, {"name": "api"}]},
        {"number": 999, "title": "labeled PR", "labels": [{"name": "P1"}, {"name": "ux"}],
         "pull_request": {"url": "https://api.github.test/pulls/999"}},
    ]

    assert [issue["number"] for issue in gate.blockers(issues)] == [160, 164]


def test_fixture_builder_is_deterministic_and_full_profile_has_the_catalog_matrix(tmp_path):
    path = Path(__file__).resolve().parents[3] / "scripts" / "build_ux_fixtures.py"
    spec = importlib.util.spec_from_file_location("build_ux_fixtures_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first, second = tmp_path / "first", tmp_path / "second"
    first_manifest = module.build(first, "full")
    second_manifest = module.build(second, "full")

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    assert (first / "episodes.csv").read_bytes() == (second / "episodes.csv").read_bytes()
    assert len(list(first.glob("catalog_*.csv"))) == 120
    assert len(list(first.glob("relationship_dense_*.csv"))) == 24
