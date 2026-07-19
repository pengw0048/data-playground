"""Acceptance checks for the fixed server-owned compound fixture authority."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_shared_builder_matches_the_ux_fixture_delegate(tmp_path):
    import importlib.util
    from pathlib import Path
    from hub.compound_fixture_definition import build_compound_timeline

    script = Path(__file__).resolve().parents[3] / "scripts" / "build_ux_fixtures.py"
    spec = importlib.util.spec_from_file_location("compound_fixture_delegate", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    direct, delegated = tmp_path / "direct", tmp_path / "delegated"
    build_compound_timeline(direct)
    module._build_compound_timeline(delegated)
    assert {path.relative_to(direct): path.read_bytes() for path in direct.rglob("*") if path.is_file()} == {
        path.relative_to(delegated): path.read_bytes() for path in delegated.rglob("*") if path.is_file()}


def test_oversized_canonical_member_fails_before_csv_parsing_without_path_leak(tmp_path, monkeypatch):
    import pyarrow.csv as pacsv
    import pytest

    from hub import compound_fixture

    replacement = tmp_path / "private-oversized-member.csv"
    replacement.write_bytes(b"x" * (compound_fixture.MAX_FIXTURE_MEMBER_BYTES + 1))
    monkeypatch.setattr(compound_fixture, "_resource", lambda _member_id: replacement)
    parser_calls = 0

    def forbidden_parser(*_args, **_kwargs):
        nonlocal parser_calls
        parser_calls += 1
        raise AssertionError("CSV parser must not see an oversized canonical member")

    monkeypatch.setattr(pacsv, "read_csv", forbidden_parser)
    with pytest.raises(compound_fixture.FixtureUnavailable) as raised:
        compound_fixture.CompoundFixtureAdapter()._table(
            compound_fixture.fixture_uri("episodes"))
    assert parser_calls == 0
    assert "byte cap" in str(raised.value)
    assert str(replacement) not in str(raised.value)


def _fixture_evidence_request(root, authority):
    import json
    from hub.compound_fixture_definition import _compound_manifest_revision
    from hub.models import TemporalEvidenceRequestV1

    document = json.loads((authority.root / "manifest.json").read_text())
    exact_members = {item.id: item for item in authority.manifest.members}
    for member in document["members"]:
        exact = exact_members[member["id"]]
        member["datasetId"], member["revisionId"] = exact.dataset_id, exact.revision_id
    document["revisionId"] = _compound_manifest_revision(document)
    return TemporalEvidenceRequestV1(
        manifestJson=json.dumps(document), episodeId="episode-1",
        streamIds=["numeric-sensor", "interval-annotation", "video"],
        streamViews=[{"streamId": key, "datasetViewId": value.id}
                     for key, value in authority.views.items()],
        referenceViewId=authority.views["interval-annotation"].id,
        pair={"leftStreamId": "numeric-sensor", "rightStreamId": "video"},
        gapThresholdTicks="1", toleranceTicks="1",
    )


def test_reference_authority_ignores_selected_external_catalog_and_restarts(tmp_path):
    from hub import deps as deps_module
    from hub import metadb
    from hub.backends import CatalogProvider
    from hub.compound_fixture import current_user_fixture_reference, fixture_uri
    from hub.deps import set_workspace
    from hub.main import app
    from hub.routers.temporal_evidence import temporal_evidence

    class IndependentCatalog:
        def __init__(self):
            self.fixture_writes = 0

        def register_output(self, *args, **kwargs):
            self.fixture_writes += 1
            return None

        def _unused(self, *args, **kwargs):
            raise AssertionError("fixed reference authority consulted the selected external catalog")

        list_page = facets = browse = search = search_modes = get_table = lineage = _unused
        relationships = resolve_ref = register = set_metadata = unregister = _unused
        set_declared_key = add_relationship = remove_relationship = _unused

    previous = deps_module._deps
    root = tmp_path / "workspace"
    try:
        metadb.init_db()
        deps = set_workspace(str(root), str(root / "data"))
        external = IndependentCatalog()
        assert isinstance(external, CatalogProvider)
        deps.catalog = external

        with TestClient(app) as client:
            discovery = client.get("/api/compound-datasets/reference")
        assert discovery.status_code == 200
        assert deps.catalog is external
        assert external.fixture_writes == 0
        assert all(metadb.catalog_revision_binding_for_uri(fixture_uri(member)) is not None
                   for member in ("episodes", "sensor-observations", "interval-annotations", "video-observations"))
        first = current_user_fixture_reference("external-user")
        assert temporal_evidence(_fixture_evidence_request(root, first), "external-user").complete is True

        restarted = set_workspace(str(root), str(root / "data"))
        second_external = IndependentCatalog()
        restarted.catalog = second_external
        with TestClient(app) as client:
            replay = client.get("/api/compound-datasets/reference")
        second = current_user_fixture_reference("external-user")
        assert replay.status_code == 200
        assert restarted.catalog is second_external
        assert replay.json() == discovery.json()
        assert second.manifest.ref == first.manifest.ref
        assert {stream: view.id for stream, view in second.views.items()} == {
            stream: view.id for stream, view in first.views.items()}
        assert external.fixture_writes == second_external.fixture_writes == 0
    finally:
        deps_module._deps = previous


def test_fixture_survives_restart_and_detail_and_range_are_redacted(tmp_path, monkeypatch):
    from hub import deps as deps_module
    from hub import metadb
    from hub.compound_fixture import current_user_fixture_reference
    from hub.deps import get_deps, set_workspace
    from hub.main import app

    previous = deps_module._deps
    root = tmp_path / "workspace"
    try:
        metadb.init_db()
        set_workspace(str(root), str(root / "data"))
        with TestClient(app) as client:
            discovery = client.get("/api/compound-datasets/reference")
            assert discovery.status_code == 200
            discovered = discovery.json()
            base = f"/api/compound-datasets/{discovered['datasetId']}/revisions/{discovered['revisionId']}"
            exact = client.get(base)
            assert exact.status_code == 200
            assert exact.json() == discovered

        calls = 0
        catalog = get_deps().catalog
        original_register = catalog.register_output

        def counted_register(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_register(*args, **kwargs)

        monkeypatch.setattr(catalog, "register_output", counted_register)
        first = current_user_fixture_reference("local")
        first_views = {stream_id: view.id for stream_id, view in first.views.items()}
        assert (root / "data" / "compound-fixture-v1" / "manifest.json").is_file()
        assert all(view.dataset_ref.revision_id for view in first.views.values())
        assert {member.id for member in first.manifest.members} == {
            "episodes", "sensor-observations", "interval-annotations", "video-observations"}
        from hub.models import ExactDatasetRef
        from hub.routers.dataset_views import _open_exact
        from hub.compound_fixture import fixture_uri
        adapter = get_deps().resolve_adapter(fixture_uri("sensor-observations"))
        sensor_member = next(member for member in first.manifest.members if member.id == "sensor-observations")
        assert adapter.preview_revision(
            fixture_uri("sensor-observations"), sensor_member.revision_id, limit=1,
        ).count("*").fetchone()[0] == 1
        for member in first.manifest.members:
            with _open_exact(ExactDatasetRef(kind="exact", datasetId=member.dataset_id,
                                              revisionId=member.revision_id), operation="fixture-test") as source:
                assert source.relation.limit(1).to_arrow_table().num_rows == 1

        from hub.routers.temporal_evidence import temporal_evidence
        evidence = temporal_evidence(_fixture_evidence_request(root, first), "local")
        assert evidence.complete is True
        assert evidence.pair is not None and evidence.pair.complete is True

        set_workspace(str(root), str(root / "data"))
        second = current_user_fixture_reference("local")
        assert second.manifest.ref == first.manifest.ref
        assert {
            stream_id: view.id for stream_id, view in second.views.items()
        } == first_views

        assert base == (f"/api/compound-datasets/{second.manifest.ref.dataset_id}"
                        f"/revisions/{second.manifest.digest}")
        asset = base + "/episodes/episode-1/streams/video/assets/flower-webm"
        with TestClient(app) as client:
            detail = client.get(base)
            assert detail.status_code == 200
            assert detail.json()["episodes"][1]["streams"][2]["state"] == "absent"
            assert detail.json()["episodes"][0]["referenceClockId"] == "reference-ms"
            assert detail.json()["episodes"][0]["endTick"] == "10000"
            assert str(root) not in detail.text
            assert "manifestJson" not in detail.text

            reference = client.get("/api/compound-datasets/reference")
            assert reference.status_code == 200
            assert reference.json()["datasetId"] == second.manifest.ref.dataset_id
            assert "datasetViews" not in reference.text

            full = client.get(asset)
            assert full.status_code == 200
            assert (
                full.headers["etag"]
                == '"c6f8a348953395598a9a73b9bab1676436410797bce9f398f4be1531d6e76dda"'
            )
            assert len(full.content) == 554_058
            prefix = client.get(asset, headers={"Range": "bytes=0-3"})
            assert (
                prefix.status_code,
                prefix.headers["content-range"],
                prefix.content,
            ) == (206, "bytes 0-3/554058", full.content[:4])
            suffix = client.get(asset, headers={"Range": "bytes=-4"})
            assert suffix.content == full.content[-4:]
            opened = client.get(asset, headers={"Range": "bytes=4-"})
            assert opened.content == full.content[4:]
            head = client.head(asset)
            assert (head.status_code, head.headers["content-length"], head.content) == (
                200,
                "554058",
                b"",
            )
            assert (
                client.get(asset, headers={"Range": "bytes=0-1,2-3"}).status_code == 416
            )
            assert (
                client.get(
                    base + "/episodes/episode-2/streams/video/assets/flower-webm"
                ).status_code
                == 410
            )
        assert calls == 0
    finally:
        deps_module._deps = previous


def test_tampered_source_manifest_fails_before_member_id_patch(tmp_path):
    import json
    import pytest

    from hub import deps as deps_module
    from hub import metadb
    from hub.compound_fixture import FixtureUnavailable, current_user_fixture_reference, fixture_authority
    from hub.deps import set_workspace

    previous = deps_module._deps
    root = tmp_path / "workspace"
    try:
        metadb.init_db()
        set_workspace(str(root), str(root / "data"))
        first = current_user_fixture_reference("local")
        manifest = first.root / "manifest.json"
        document = json.loads(manifest.read_text())
        document["clockMappings"][0]["offsetTick"] += 1
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(FixtureUnavailable):
            fixture_authority()
        with pytest.raises(FixtureUnavailable):
            current_user_fixture_reference("local")
    finally:
        deps_module._deps = previous


def test_fixture_bootstrap_accepts_trusted_root_alias_and_rejects_managed_symlinks(tmp_path):
    from hub.compound_fixture import materialize_fixture

    real = tmp_path / "real"
    linked_parent = tmp_path / "linked-parent"
    real.mkdir()
    linked_parent.symlink_to(real, target_is_directory=True)
    alias_target = materialize_fixture(str(linked_parent / "data"))
    assert alias_target == (real / "data" / "compound-fixture-v1").resolve()

    target_root = tmp_path / "target-symlink" / "data"
    target_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target_root / "compound-fixture-v1").symlink_to(outside, target_is_directory=True)
    assert materialize_fixture(str(target_root)) is None

    lock_root = tmp_path / "lock-symlink" / "data"
    lock_root.mkdir(parents=True)
    (lock_root / ".compound-fixture-v1.lock").symlink_to(tmp_path / "outside.lock")
    assert materialize_fixture(str(lock_root)) is None

    root = tmp_path / "workspace" / "data"
    target = materialize_fixture(str(root))
    assert target is not None
    manifest = target / "manifest.json"
    manifest.unlink()
    manifest.symlink_to(tmp_path / "elsewhere.json")
    assert materialize_fixture(str(root)) is None


def test_asset_openapi_separates_get_and_head_bodies():
    from hub.main import app

    path = ("/api/compound-datasets/{dataset_id}/revisions/{revision_id}/episodes/{episode_id}"
            "/streams/{stream_id}/assets/{asset_id}")
    operations = app.openapi()["paths"][path]
    get, head = operations["get"]["responses"], operations["head"]["responses"]
    assert set(get["200"]["content"]) == {"video/webm"}
    assert "Content-Range" not in get["200"]["headers"]
    assert "Content-Range" in get["206"]["headers"]
    assert "content" not in head["200"] and "content" not in head["206"]
    assert set(head["416"]["headers"]) == {"Content-Range"}


def test_asset_replacement_fails_closed_without_exposing_a_local_path(tmp_path):
    from hub import deps as deps_module
    from hub import metadb
    from hub.compound_fixture import fixture_authority
    from hub.deps import set_workspace
    from hub.main import app

    previous = deps_module._deps
    root = tmp_path / "workspace"
    try:
        metadb.init_db()
        set_workspace(str(root), str(root / "data"))
        authority = fixture_authority()
        (root / "data" / "compound-fixture-v1" / "flower.webm").write_bytes(b"replacement")
        url = (
            f"/api/compound-datasets/{authority.manifest.ref.dataset_id}/revisions/{authority.manifest.digest}"
            "/episodes/episode-1/streams/video/assets/flower-webm"
        )
        with TestClient(app) as client:
            response = client.get(url)
            detail = client.get(url.rsplit("/episodes/", 1)[0])
        assert response.status_code == 410
        assert detail.status_code == 200
        assert detail.json()["assets"][0]["status"] == "unavailable"
        assert str(root) not in response.text
    finally:
        deps_module._deps = previous


def test_asset_permission_is_redacted_as_forbidden(tmp_path, monkeypatch):
    from hub import deps as deps_module
    from hub import metadb
    from hub.compound_fixture import fixture_authority
    from hub.deps import set_workspace
    from hub.main import app
    from hub.routers import compound_datasets

    previous = deps_module._deps
    root = tmp_path / "workspace"
    try:
        metadb.init_db()
        set_workspace(str(root), str(root / "data"))
        authority = fixture_authority()
        monkeypatch.setattr(compound_datasets.os, "open", lambda *_args: (_ for _ in ()).throw(PermissionError()))
        url = (
            f"/api/compound-datasets/{authority.manifest.ref.dataset_id}/revisions/{authority.manifest.digest}"
            "/episodes/episode-1/streams/video/assets/flower-webm"
        )
        with TestClient(app) as client:
            response = client.get(url)
        assert response.status_code == 403
        assert str(root) not in response.text
    finally:
        deps_module._deps = previous
