from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
import types
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from hub import handoff, metadb
from hub.models import Graph, RunStatus
from hub.process_scope import OwnedProcessScope
from hub.storage import (
    MAX_MANAGED_EXECUTION_SOURCES,
    ManagedSourceAccessDenied,
    ManagedSourceLimitExceeded,
    ManagedSourceReadError,
    ManagedSourceUnavailable,
    source_read_scope,
)
from hub.subrun import _parent_attested_source_uris


@pytest.fixture(autouse=True)
def _isolated_metadata(tmp_path):
    """Keep adversarial lifecycle rows isolated from the rest of the kernel suite."""
    from hub.settings import settings

    original_engine, original_session = metadb._engine, metadb._Session
    original_url = settings.database_url
    if metadb._engine is not None:
        metadb._engine.dispose()
    settings.database_url = (os.environ.get("DP_TEST_DATABASE_URL")
                             or f"sqlite:///{tmp_path / 'managed-source-lifecycle.db'}")
    metadb._engine = metadb._Session = None
    metadb.init_db()
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _source_graph(uri: str) -> Graph:
    return Graph.model_validate({
        "id": "managed-source-contract",
        "version": 1,
        "nodes": [{
            "id": "source",
            "type": "source",
            "position": {"x": 0, "y": 0},
            "data": {"config": {"uri": uri}},
        }],
        "edges": [],
    })


def _joined_source_graph(left_uri: str, right_uri: str) -> Graph:
    return Graph.model_validate({
        "id": "managed-source-join",
        "version": 1,
        "nodes": [
            {"id": "left", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": left_uri}}},
            {"id": "right", "type": "source", "position": {"x": 0, "y": 100},
             "data": {"config": {"uri": right_uri}}},
            {"id": "join", "type": "join", "position": {"x": 200, "y": 50},
             "data": {"config": {"on": "value"}}},
        ],
        "edges": [
            {"id": "left-join", "source": "left", "target": "join",
             "targetHandle": "left"},
            {"id": "right-join", "source": "right", "target": "join",
             "targetHandle": "right"},
        ],
    })


def _identity(logical_uri: str, namespace: str, generation: int,
              attempt_id: str, *, kind: str = "region") -> tuple[str, dict]:
    uri = handoff.physical_attempt_uri(
        logical_uri, namespace, generation, attempt_id)
    return uri, {
        "attemptId": attempt_id,
        "generation": generation,
        "storageNamespace": namespace,
        "logicalUri": logical_uri,
        "kind": kind,
    }


def _inventory(uri: str) -> list[dict]:
    parsed = urlsplit(uri)
    root = f"{parsed.netloc}/{parsed.path.lstrip('/').rstrip('/')}"
    members = []
    for key, is_commit in (
            (f"{root}/part-00000.parquet", False),
            (handoff._object_manifest_path(root), True)):
        members.append({
            "member_id": handoff._member_id("unversioned_object", key, "null"),
            "key": key,
            "member_type": "unversioned_object",
            "size": 10,
            "etag": "managed-source-test",
            "version_id": None,
            "upload_id": None,
            "is_latest": True,
            "is_commit": is_commit,
        })
    return members


def _committed_region(logical_uri: str) -> dict:
    token = uuid.uuid4().hex
    handle = metadb.allocate_object_attempt(
        logical_uri=logical_uri,
        kind="region",
        run_id=f"managed-source-{token}",
        allocation_key=f"managed-source:{token}",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            logical_uri, namespace, generation, attempt_id),
        write_lease_seconds=30,
    )
    metadb.record_object_attempt_commit(handle["uri"], _inventory(handle["uri"]))
    return handle


def _attempt_state(uri: str) -> str:
    with metadb.session() as session:
        return session.get(metadb.ObjectAttempt, uri).state


def _published_region(logical_uri: str) -> dict:
    handle = _committed_region(logical_uri)
    metadb.put_result(f"interactive-source-{uuid.uuid4().hex}", {"uri": handle["uri"]})
    assert _attempt_state(handle["uri"]) == "published"
    return handle


def _read_leases(uri: str) -> list:
    with metadb.session() as session:
        return list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == uri,
            metadb.ObjectAttemptLease.lease_type == "read",
        )))


def test_interactive_scope_holds_one_deduped_lease_while_reader_blocks():
    published = _published_region(
        f"s3://interactive-read-block/{uuid.uuid4().hex}/input.parquet")
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def blocked_adapter_read() -> None:
        try:
            with source_read_scope(
                    None, [published["uri"] + "/", published["uri"]],
                    owner="blocked-adapter") as guards:
                assert len(guards) == 1
                entered.set()
                assert release.wait(timeout=5)
        except BaseException as exc:  # noqa: BLE001 - asserted on the parent thread
            errors.append(exc)

    thread = threading.Thread(target=blocked_adapter_read)
    thread.start()
    assert entered.wait(timeout=5)
    assert len(_read_leases(published["uri"])) == 1
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
    assert _read_leases(published["uri"]) == []


def test_data_sample_holds_lease_through_blocked_adapter_and_response(monkeypatch):
    from hub import db
    from hub.models import SampleRequest
    from hub.routers import catalog as catalog_routes

    published = _published_region(
        f"s3://interactive-sample-block/{uuid.uuid4().hex}/input.parquet")
    entered = threading.Event()
    release = threading.Event()
    results = []
    errors: list[BaseException] = []

    class Adapter:
        def preview_scan(self, uri, *_args, **_kwargs):
            assert uri == published["uri"] and _read_leases(uri)
            entered.set()
            assert release.wait(timeout=5)
            return db.conn().sql("SELECT 1 AS value")

        @staticmethod
        def metadata_count(_uri):
            return 1

    monkeypatch.setattr(catalog_routes, "get_deps", lambda: types.SimpleNamespace(
        storage=None, resolve_adapter=lambda _uri: Adapter()))

    def sample() -> None:
        try:
            results.append(catalog_routes.data_sample(SampleRequest(
                uri=published["uri"], k=1)))
        except BaseException as exc:  # noqa: BLE001 - asserted on the parent thread
            errors.append(exc)

    thread = threading.Thread(target=sample)
    thread.start()
    assert entered.wait(timeout=5)
    assert len(_read_leases(published["uri"])) == 1
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
    assert results[0].rows == [{"value": 1}]
    assert _read_leases(published["uri"]) == []


@pytest.mark.parametrize("error", [
    RuntimeError("adapter failed"),
    TimeoutError("adapter timed out"),
    asyncio.CancelledError("adapter cancelled"),
])
def test_interactive_scope_releases_after_error_timeout_or_cancel(error):
    published = _published_region(
        f"s3://interactive-read-error/{uuid.uuid4().hex}/input.parquet")
    with pytest.raises(type(error), match=str(error)):
        with source_read_scope(None, [published["uri"]], owner="failing-reader"):
            assert len(_read_leases(published["uri"])) == 1
            raise error
    assert _read_leases(published["uri"]) == []


def test_interactive_scope_acquisition_cancel_rolls_back_prior_guard():
    closed: list[str] = []

    class Guard:
        def __init__(self, uri):
            self.uri = uri

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(self.uri)
            return False

    class Storage:
        @staticmethod
        def requires_result_read(_uri):
            return True

        @staticmethod
        def acquire_result_read(uri, _owner):
            if uri == "/tmp/second-managed-source.parquet":
                raise asyncio.CancelledError("cancel during acquisition")
            return Guard(uri)

    with pytest.raises(asyncio.CancelledError, match="cancel during acquisition"):
        with source_read_scope(
                Storage(),
                ["/tmp/first-managed-source.parquet", "/tmp/second-managed-source.parquet"],
                owner="cancelled-acquisition"):
            pytest.fail("the scope must not yield after a cancelled acquisition")

    assert closed == ["/tmp/first-managed-source.parquet"]


def test_interactive_scope_timeout_unwinds_before_returning():
    from hub.sandbox import SandboxError, run_with_timeout

    published = _published_region(
        f"s3://interactive-read-timeout/{uuid.uuid4().hex}/input.parquet")
    stop = threading.Event()

    def work() -> None:
        with source_read_scope(None, [published["uri"]], owner="timed-reader"):
            stop.wait(timeout=5)

    with pytest.raises(SandboxError, match="time budget"):
        run_with_timeout(work, 0.01, on_timeout=stop.set)
    assert _read_leases(published["uri"]) == []


def test_interactive_scope_lost_lease_rejects_success_and_releases():
    published = _published_region(
        f"s3://interactive-read-lost/{uuid.uuid4().hex}/input.parquet")
    with pytest.raises(ManagedSourceUnavailable, match="unavailable or expired"):
        with source_read_scope(None, [published["uri"]], owner="lost-reader") as guards:
            assert len(guards) == 1
            guards[0]._lost.set()
    assert _read_leases(published["uri"]) == []


def test_interactive_scope_rejects_every_invalid_generation_before_body():
    namespace = metadb.object_storage_namespace()
    unknown = handoff.physical_attempt_uri(
        f"s3://interactive-invalid/{uuid.uuid4().hex}/unknown.parquet",
        namespace, 1, uuid.uuid4().hex)
    committed = _committed_region(
        f"s3://interactive-invalid/{uuid.uuid4().hex}/committed.parquet")

    logical = f"s3://interactive-invalid/{uuid.uuid4().hex}/superseded.parquet"
    old = _committed_region(logical)
    replacement = _committed_region(logical)
    pointer = f"interactive-replacement-{uuid.uuid4().hex}"
    metadb.put_result(pointer, {"uri": old["uri"]})
    metadb.put_result(pointer, {"uri": replacement["uri"]})
    assert _attempt_state(old["uri"]) == "superseded"

    foreign = _published_region(
        f"s3://interactive-invalid/{uuid.uuid4().hex}/foreign.parquet")
    with metadb.session() as session:
        session.get(metadb.ObjectAttempt, foreign["uri"]).storage_namespace = "foreign-installation"

    descendant_root = _published_region(
        f"s3://interactive-invalid/{uuid.uuid4().hex}/descendant.parquet")
    invalid = [
        unknown,
        unknown.replace("s3://", "S3://"),
        committed["uri"],
        old["uri"],
        foreign["uri"],
        f"{descendant_root['uri']}/part-00000.parquet",
    ]
    reads: list[str] = []
    for uri in invalid:
        with pytest.raises(ManagedSourceUnavailable, match="unavailable or expired"):
            with source_read_scope(None, [uri], owner="invalid-reader"):
                reads.append(uri)
    assert reads == []


def test_interactive_scope_rolls_back_partial_acquire_and_caps_before_acquire(monkeypatch):
    published = _published_region(
        f"s3://interactive-partial/{uuid.uuid4().hex}/published.parquet")
    unknown = handoff.physical_attempt_uri(
        f"s3://interactive-partial/{uuid.uuid4().hex}/unknown.parquet",
        metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    with pytest.raises(ManagedSourceUnavailable):
        with source_read_scope(
                None, [published["uri"], unknown], owner="partial-reader"):
            pytest.fail("an unknown second generation must fail acquisition")
    assert _read_leases(published["uri"]) == []

    acquisitions: list[str] = []

    @contextlib.contextmanager
    def acquire(uri, **_kwargs):
        acquisitions.append(uri)
        yield object()

    monkeypatch.setattr(handoff, "managed_read_lease", acquire)
    uris = [
        handoff.physical_attempt_uri(
            f"s3://interactive-cap/{index}/input.parquet",
            metadb.object_storage_namespace(), 1, f"{index:032x}")
        for index in range(MAX_MANAGED_EXECUTION_SOURCES + 1)
    ]
    with pytest.raises(ManagedSourceLimitExceeded, match="at most"):
        with source_read_scope(None, uris, owner="capped-reader"):
            pass
    assert acquisitions == []


def test_mixed_case_file_uri_is_confined_before_any_adapter_read(monkeypatch):
    from fastapi import HTTPException
    from hub import paths
    from hub.plugins.adapters import path_of
    from hub.routers import catalog as catalog_routes

    monkeypatch.setenv(
        "DP_AUTH_SECRET", "interactive-path-policy-test-secret-0123456789")
    escaped = "FiLe:///etc/passwd"
    assert path_of(escaped) == "/etc/passwd"
    with pytest.raises(PermissionError):
        paths.ensure_local_uri_allowed(escaped)
    with pytest.raises(ManagedSourceAccessDenied):
        with source_read_scope(None, [escaped], owner="confined-reader"):
            pytest.fail("path policy must run before the read body")

    adapter_calls: list[str] = []

    class Catalog:
        @staticmethod
        def get_table(ref):
            raise KeyError(ref)

    def resolve(_uri):
        adapter_calls.append("resolve")
        raise AssertionError("adapter resolution must follow path confinement")

    deps = types.SimpleNamespace(storage=None, catalog=Catalog(), resolve_adapter=resolve)
    monkeypatch.setattr(catalog_routes, "get_deps", lambda: deps)
    with pytest.raises(HTTPException) as error:
        catalog_routes.join_suggestions(catalog_routes.JoinSuggestRequest(
            left_uri=escaped, right_uri="/tmp/ordinary.parquet"))
    assert error.value.status_code == 403
    assert adapter_calls == []


def test_colon_local_path_is_confined_and_remote_schemes_are_normalized(monkeypatch):
    import sys

    from hub import paths
    from hub.plugins import adapters
    from hub.plugins.adapters import path_of

    monkeypatch.setenv(
        "DP_AUTH_SECRET", "interactive-colon-policy-test-secret-0123456789")
    monkeypatch.setattr(paths, "allowed_roots", lambda: ["/definitely-allowed"])
    colon_path = "relative:../../outside.csv"
    assert paths.local_path(colon_path) == colon_path
    assert path_of(colon_path) == colon_path
    with pytest.raises(PermissionError):
        paths.ensure_local_uri_allowed(colon_path)

    class Storage:
        @staticmethod
        def requires_result_read(_uri):
            raise AssertionError("classification must follow local-path confinement")

    with pytest.raises(ManagedSourceAccessDenied):
        with source_read_scope(Storage(), [colon_path], owner="colon-confined-reader"):
            pytest.fail("the source scope must not yield for an escaped local path")

    assert path_of("S3://bucket/data.csv") == "S3://bucket/data.csv"
    assert path_of("HTTPS://example.test/data.csv") == "HTTPS://example.test/data.csv"
    assert not adapters.is_object_uri("S3://bucket/data.csv")
    reads: list[str] = []

    class Connection:
        @staticmethod
        def read_parquet(uri):
            reads.append(uri)
            return object()

    monkeypatch.setattr(adapters.db, "ensure_object_store", lambda: None)
    adapters.DuckDBAdapter()._read(Connection(), "S3://bucket/data.parquet")
    assert reads == ["s3://bucket/data.parquet"]

    lance_reads: list[str] = []
    monkeypatch.setitem(sys.modules, "lance", types.SimpleNamespace(
        dataset=lambda uri: lance_reads.append(uri) or object()))
    adapters.LanceAdapter()._dataset("S3://bucket/data.lance")
    assert lance_reads == ["s3://bucket/data.lance"]


def test_source_limit_is_typed_and_not_downgraded_by_estimate_or_plan(monkeypatch):
    from fastapi import HTTPException
    from hub import graph as graph_mod
    from hub.models import CompileRequest
    from hub.routers import runs as run_routes
    from hub.run_controller import RunController

    uris = [
        handoff.physical_attempt_uri(
            f"s3://interactive-policy-cap/{index}/input.parquet",
            metadb.object_storage_namespace(), 1, f"{index:032x}")
        for index in range(MAX_MANAGED_EXECUTION_SOURCES + 1)
    ]
    graph = _source_graph("/tmp/ordinary.parquet")
    monkeypatch.setattr(graph_mod, "execution_source_uris", lambda *_args, **_kwargs: uris)

    def no_adapter(_uri):
        raise AssertionError("the complete source cap must run before adapter resolution")

    deps = types.SimpleNamespace(
        storage=None, resolve_adapter=no_adapter, registry={}, node_builders={}, node_specs={},
        catalog=types.SimpleNamespace(resolve_ref=lambda ref: ref),
    )
    controller = RunController(deps, base=None, place_fn=None)
    deps.controller = controller
    with pytest.raises(ManagedSourceLimitExceeded):
        controller.plan_summary(graph, "source")
    with pytest.raises(HTTPException) as cone_error:
        run_routes._cone_size(graph, "source", deps)
    assert cone_error.value.status_code == 400
    assert "at most" in cone_error.value.detail

    monkeypatch.setattr(run_routes, "get_deps", lambda: deps)
    monkeypatch.setattr(run_routes, "_require_graph_read_access", lambda *_args: None)
    monkeypatch.setattr(run_routes, "_reject_invalid", lambda *_args: None)
    monkeypatch.setattr(run_routes, "_actuals_for", lambda *_args: {})
    monkeypatch.setattr(run_routes.graph_mod, "resolve_source_refs", lambda *_args: None)
    request = CompileRequest(graph=graph, target_node_id="source")
    for boundary in (run_routes.graph_schema, run_routes.graph_estimate):
        with pytest.raises(HTTPException) as route_error:
            boundary(request, uid="user")
        assert route_error.value.status_code == 400
        assert "at most" in route_error.value.detail
    plan = run_routes.graph_plan(request, uid="user")
    assert plan["regions"] == [] and "at most" in plan["error"]
    assert isinstance(ManagedSourceLimitExceeded("limit"), ManagedSourceReadError)


def test_interactive_scope_is_a_noop_for_ordinary_sources(monkeypatch):
    class UnmanagedStorage:
        @staticmethod
        def requires_result_read(_uri):
            return False

        @staticmethod
        def acquire_result_read(*_args, **_kwargs):
            raise AssertionError("ordinary sources must not acquire a local guard")

    monkeypatch.setattr(
        handoff, "managed_read_lease",
        lambda *_args, **_kwargs: pytest.fail("ordinary sources must not acquire an object lease"))
    with source_read_scope(
            UnmanagedStorage(),
            ["/tmp/ordinary.parquet", "/tmp/ordinary.parquet/", "s3://bucket/ordinary.parquet"],
            owner="ordinary-reader") as guards:
        assert guards == []


def test_interactive_graph_readers_reject_unknown_generation_before_adapter():
    from hub import relationships
    from hub.estimate import estimate_sizes
    from hub.executors.preview import preview_node
    from hub.executors.profile import profile_node
    from hub.executors.schema import schema_for_graph
    from hub.models import ColumnSchema

    unknown = handoff.physical_attempt_uri(
        f"s3://interactive-callsite/{uuid.uuid4().hex}/unknown.parquet",
        metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    graph = _source_graph(unknown)
    adapter_calls: list[str] = []

    def resolve(_uri):
        adapter_calls.append("resolve")
        raise AssertionError("adapter resolution must follow lifecycle acquisition")

    preview = preview_node(graph, "source", 1, resolve, {}, storage=None)
    assert preview.error and preview.reason == "managed source is unavailable or expired"
    sampled = profile_node(graph, "source", resolve, {}, storage=None)
    full = profile_node(graph, "source", resolve, {}, full=True, storage=None)
    assert sampled.error and full.error
    assert sampled.reason == full.reason == "managed source is unavailable or expired"
    with pytest.raises(ManagedSourceUnavailable):
        schema_for_graph(graph, resolve, {}, storage=None)
    with pytest.raises(ManagedSourceUnavailable):
        estimate_sizes(graph, resolve, storage=None)

    joined = _joined_source_graph(unknown, "/tmp/ordinary.parquet")
    columns = {
        "left": [ColumnSchema(name="value", type="BIGINT")],
        "right": [ColumnSchema(name="value", type="BIGINT")],
    }
    with pytest.raises(ManagedSourceUnavailable):
        relationships.analyze_join(
            joined, "join", columns, object(), resolve, storage=None)
    assert adapter_calls == []


def test_interactive_catalog_and_graph_ops_use_sanitized_scope(monkeypatch):
    from fastapi import HTTPException
    from hub import graph_ops
    from hub.models import SampleRequest
    from hub.routers import catalog as catalog_routes

    unknown = handoff.physical_attempt_uri(
        f"s3://interactive-route/{uuid.uuid4().hex}/unknown.parquet",
        metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    adapter_calls: list[str] = []

    class Catalog:
        @staticmethod
        def get_table(_ref):
            raise KeyError(_ref)

    def resolve(_uri):
        adapter_calls.append("resolve")
        raise AssertionError("adapter resolution must follow lifecycle acquisition")

    deps = types.SimpleNamespace(storage=None, catalog=Catalog(), resolve_adapter=resolve)
    with pytest.raises(ManagedSourceUnavailable, match="unavailable or expired"):
        graph_ops.join_hints(deps, unknown, "/tmp/ordinary.parquet")

    monkeypatch.setattr(catalog_routes, "get_deps", lambda: deps)
    with pytest.raises(HTTPException) as sample_error:
        catalog_routes.data_sample(SampleRequest(uri=unknown, k=1))
    assert sample_error.value.status_code == 410
    assert sample_error.value.detail == "managed source is unavailable or expired"

    with pytest.raises(HTTPException) as register_error:
        catalog_routes.catalog_register(catalog_routes.RegisterRequest(uri=unknown))
    assert register_error.value.status_code == 400
    assert register_error.value.detail == "managed source is unavailable or expired"

    with pytest.raises(HTTPException) as join_error:
        catalog_routes.join_suggestions(catalog_routes.JoinSuggestRequest(
            left_uri=unknown, right_uri="/tmp/ordinary.parquet"))
    assert join_error.value.status_code == 400
    assert join_error.value.detail == "managed source is unavailable or expired"
    assert adapter_calls == []


def test_child_accepts_only_the_exact_parent_attestation():
    logical = "s3://managed-source-contract/data/input.parquet"
    uri, identity = _identity(logical, "installation", 7, "a" * 32)
    job = {
        "target": "source",
        "managedSourceAttempts": {uri: identity},
        "managedLocalSources": {},
    }

    assert _parent_attested_source_uris(job, _source_graph(uri)) == frozenset({uri})


@pytest.mark.parametrize("scheme", ["file://", "FILE://", "FiLe://"])
def test_parent_child_local_attestation_uses_one_case_insensitive_canonical_path(tmp_path, scheme):
    from hub.subprocess_runner import SubprocessRunner

    path = str(tmp_path / ".dp-results" / f"__result_case_{uuid.uuid4().hex}.parquet")
    file_uri = Path(path).as_uri()
    graph_uri = scheme + file_uri[len("file://"):]

    class Guard:
        uri = path

        @staticmethod
        def fileno():
            return None

        def __enter__(self):
            return self

        @staticmethod
        def __exit__(*args):
            return False

    class Storage:
        namespace_id = "local-attestation"

        @staticmethod
        def requires_result_read(uri):
            assert uri == path
            return True

        @staticmethod
        def acquire_result_read(uri, _owner):
            assert uri == path
            return Guard()

        @staticmethod
        def result_namespace_identity():
            return (101, 202)

    graph = _source_graph(graph_uri)
    runner = SubprocessRunner(str(tmp_path), str(tmp_path), storage=Storage())
    bundle = runner._claim_source_leases(graph, "source", "case-reader")
    try:
        job = {
            "target": "source",
            "managedSourceAttempts": bundle["attempts"],
            "managedLocalSources": bundle["local_sources"],
        }
        assert list(bundle["local_sources"]) == [path]
        assert _parent_attested_source_uris(job, graph) == frozenset({path})
    finally:
        bundle["stack"].close()


@pytest.mark.parametrize("case,match", [
    ("missing", "contract is missing"),
    ("extra", "does not match the graph"),
    ("malformed", "contract is malformed"),
    ("descendant", "exact attempt root"),
])
def test_child_rejects_unattested_or_aliased_managed_sources(case, match):
    logical = "s3://managed-source-contract/data/input.parquet"
    uri, identity = _identity(logical, "installation", 3, "b" * 32)
    graph_uri = uri
    job = {"target": "source"}

    if case == "extra":
        other_uri, other_identity = _identity(
            "s3://managed-source-contract/data/other.parquet",
            "installation", 4, "c" * 32)
        job["managedSourceAttempts"] = {
            uri: identity,
            other_uri: other_identity,
        }
    elif case == "malformed":
        job["managedSourceAttempts"] = {
            uri: {"logicalUri": logical, "kind": "region"},
        }
    elif case == "descendant":
        graph_uri = f"{uri}/part-00000.parquet"
        job["managedSourceAttempts"] = {uri: identity}

    with pytest.raises(RuntimeError, match=match):
        _parent_attested_source_uris(job, _source_graph(graph_uri))


def test_parent_attestation_atomically_pins_replaced_generation_against_gc(tmp_path):
    from hub.subprocess_runner import SubprocessRunner

    logical = f"s3://managed-source-pin/{uuid.uuid4().hex}/input.parquet"
    old = _committed_region(logical)
    replacement = _committed_region(logical)
    cache_key = f"managed-source-pointer-{uuid.uuid4().hex}"
    metadb.put_result(cache_key, {"uri": old["uri"], "rows": 1})
    assert _attempt_state(old["uri"]) == "published"

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    bundle = runner._claim_source_leases(
        _source_graph(old["uri"]), "source", "attested-reader")
    try:
        attestation = bundle["attempts"][old["uri"]]
        assert attestation == {
            "attemptId": old["attempt_id"],
            "generation": old["generation"],
            "storageNamespace": old["storage_namespace"],
            "logicalUri": logical,
            "kind": "region",
        }
        with metadb.session() as session:
            leases = list(session.scalars(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == old["uri"],
                metadb.ObjectAttemptLease.lease_type == "read",
            )))
        assert len(leases) == 1

        metadb.put_result(cache_key, {"uri": replacement["uri"], "rows": 1})
        assert _attempt_state(old["uri"]) == "superseded"
        assert old["uri"] not in {
            item["uri"] for item in metadb.object_attempt_gc_batch(0, 0)
        }
    finally:
        bundle["stack"].close()

    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == old["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []
    assert old["uri"] in {
        item["uri"] for item in metadb.object_attempt_gc_batch(0, 0)
    }


@pytest.mark.parametrize("case,match", [
    ("unknown", "no lifecycle ownership row"),
    ("committed", "state=committed"),
    ("superseded", "state=superseded"),
    ("descendant", "exact attempt root"),
])
def test_parent_rejects_unattestable_managed_source_before_dispatch(
        tmp_path, case, match):
    from hub.subprocess_runner import SubprocessRunner

    logical = f"s3://managed-source-reject/{uuid.uuid4().hex}/input.parquet"
    if case == "unknown":
        uri = handoff.physical_attempt_uri(
            logical, metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    else:
        old = _committed_region(logical)
        uri = old["uri"]
        if case in ("superseded", "descendant"):
            replacement = _committed_region(logical)
            cache_key = f"managed-source-reject-{uuid.uuid4().hex}"
            metadb.put_result(cache_key, {"uri": old["uri"]})
            if case == "superseded":
                metadb.put_result(cache_key, {"uri": replacement["uri"]})
            else:
                uri = f"{old['uri']}/part-00000.parquet"

    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    with pytest.raises(FileNotFoundError, match=match):
        runner._claim_source_leases(_source_graph(uri), "source", f"reject-{case}")


def test_parent_deduplicates_sources_and_releases_partial_claim_on_failure(tmp_path):
    from hub.subprocess_runner import SubprocessRunner

    logical = f"s3://managed-source-partial/{uuid.uuid4().hex}/input.parquet"
    published = _committed_region(logical)
    cache_key = f"managed-source-partial-{uuid.uuid4().hex}"
    metadb.put_result(cache_key, {"uri": published["uri"]})
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))

    duplicate = runner._claim_source_leases(
        _joined_source_graph(published["uri"], published["uri"]), "join", "duplicate")
    try:
        assert list(duplicate["attempts"]) == [published["uri"]]
        with metadb.session() as session:
            assert session.scalar(select(metadb.ObjectAttemptLease).where(
                metadb.ObjectAttemptLease.attempt_uri == published["uri"],
                metadb.ObjectAttemptLease.lease_type == "read",
            )) is not None
    finally:
        duplicate["stack"].close()

    unknown = handoff.physical_attempt_uri(
        f"s3://managed-source-partial/{uuid.uuid4().hex}/missing.parquet",
        metadb.object_storage_namespace(), 1, uuid.uuid4().hex)
    with pytest.raises(FileNotFoundError, match="no lifecycle ownership row"):
        runner._claim_source_leases(
            _joined_source_graph(published["uri"], unknown), "join", "partial")
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == published["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []


def test_read_lease_thread_start_failure_rolls_back_atomic_claim(monkeypatch):
    logical = f"s3://managed-source-thread/{uuid.uuid4().hex}/input.parquet"
    published = _committed_region(logical)
    metadb.put_result(f"managed-source-thread-{uuid.uuid4().hex}", {
        "uri": published["uri"],
    })

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(handoff.threading, "Thread", BrokenThread)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        with handoff.managed_read_lease(published["uri"], owner="broken-thread"):
            pass
    with metadb.session() as session:
        assert list(session.scalars(select(metadb.ObjectAttemptLease).where(
            metadb.ObjectAttemptLease.attempt_uri == published["uri"],
            metadb.ObjectAttemptLease.lease_type == "read",
        ))) == []


def test_raw_managed_read_rejects_attempt_member_uri():
    logical = f"s3://managed-source-member/{uuid.uuid4().hex}/input.parquet"
    published = _committed_region(logical)
    metadb.put_result(f"managed-source-member-{uuid.uuid4().hex}", {
        "uri": published["uri"],
    })
    with pytest.raises(FileNotFoundError, match="exact attempt root"):
        with handoff.managed_read_lease(
                f"{published['uri']}/part-00000.parquet", owner="member-reader"):
            pass


@pytest.mark.parametrize("publication", ["managed-sink", "unmanaged-sink", "object-result"])
def test_source_lease_lost_after_child_done_blocks_every_publication(
        tmp_path, monkeypatch, publication):
    from hub.subprocess_runner import SubprocessRunner
    import hub.subprocess_runner as subprocess_runner

    run_id = f"lost-source-{publication}"
    events: list[str] = []
    publication_calls: list[str] = []
    output_uri = (
        "s3://managed-source-loss/results/out.attempt-parent"
        if publication != "unmanaged-sink" else str(tmp_path / "out.csv"))
    output_table = None if publication == "object-result" else "out"
    job_dir = tmp_path / publication
    job_dir.mkdir()
    status_file = job_dir / "status.json"
    status_file.write_text(json.dumps(RunStatus(
        run_id="child",
        status="done",
        placement="local",
        per_node=[],
        output_uri=output_uri,
        output_table=output_table,
    ).model_dump()))

    class Guard:
        checks = 0

        def check(self):
            self.checks += 1
            if self.checks == 2:
                raise FileNotFoundError("SECRET_SOURCE_LEASE_SENTINEL")

    guard = Guard()

    class Stack:
        closed = False

        def close(self):
            assert events == ["reaped"]
            self.closed = True
            events.append("released")

    stack = Stack()

    class FinishedProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            events.append("reaped")
            return 0

    class Catalog:
        @staticmethod
        def register_output(**_kwargs):
            publication_calls.append("register")
            return {"uri": output_uri, "name": "out", "version": "v1"}

        @staticmethod
        def get_table(_uri):
            publication_calls.append("readback")
            return {"uri": output_uri, "name": "out", "version": "v1"}

    runner = SubprocessRunner(str(tmp_path), str(tmp_path), catalog=Catalog())
    process = FinishedProcess()
    runner.runs[run_id] = RunStatus(
        run_id=run_id, status="running", placement="local", per_node=[])
    runner._procs[run_id] = process
    runner._process_scopes[run_id] = OwnedProcessScope(
        process, owns_process_group=False)
    runner._source_leases[run_id] = {
        "stack": stack,
        "guards": [guard],
        "attempts": {"source": {}},
    }

    if publication == "managed-sink":
        runner._object_sinks[run_id] = {"write": {
            "uri": output_uri,
            "logical_uri": "s3://managed-source-loss/results/out.parquet",
            "name": "out",
            "parents": [],
        }}
        monkeypatch.setattr(
            runner, "_publish_object_sinks",
            lambda *_args: publication_calls.append("managed-publish"))
        monkeypatch.setattr(
            runner, "_discard_object_sinks",
            lambda _sinks: publication_calls.append("managed-discard"))
    elif publication == "unmanaged-sink":
        runner._sink_contracts[run_id] = {"write": {
            "logical_uri": output_uri,
            "published_uri": output_uri,
            "name": "out",
            "parents": [],
        }}
    else:
        runner._object_results[run_id] = {
            "uri": output_uri,
            "cache_key": None,
            "run_state_owner": True,
        }
        monkeypatch.setattr(
            subprocess_runner, "_safe_abandon_attempt",
            lambda *_args, **_kwargs: publication_calls.append("result-abandon"))

    monkeypatch.setattr(
        handoff, "prepare_attempt_commit",
        lambda *_args, **_kwargs: publication_calls.append("prepare"))
    runner._watch(
        run_id, process, str(status_file), str(job_dir),
        Graph.model_validate({
            "id": "lost-source", "version": 1, "nodes": [], "edges": [],
        }), None)

    final = runner.status(run_id)
    assert final.status == "failed"
    assert final.error == "managed source lease was lost during execution"
    assert "SECRET_SOURCE_LEASE_SENTINEL" not in final.error
    assert final.output_uri is None and final.output_table is None
    assert guard.checks == 2
    assert stack.closed and events == ["reaped", "released"]
    assert "prepare" not in publication_calls
    assert "managed-publish" not in publication_calls
    assert "register" not in publication_calls and "readback" not in publication_calls
    if publication == "managed-sink":
        assert publication_calls == ["managed-discard"]
    elif publication == "object-result":
        assert publication_calls == ["result-abandon"]
    else:
        assert publication_calls == []


def test_unreaped_supervisor_retains_managed_source_bundle(tmp_path, monkeypatch):
    from hub.subprocess_runner import SubprocessRunner

    run_id = "unreaped-managed-source"
    job_dir = tmp_path / "unreaped-job"
    job_dir.mkdir()

    class Process:
        returncode = None

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

        @staticmethod
        def wait(timeout=None):
            raise OSError("reap proof unavailable")

    class Stack:
        closed = False

        def close(self):
            self.closed = True

    stack = Stack()
    runner = SubprocessRunner(str(tmp_path), str(tmp_path))
    process = Process()
    runner.runs[run_id] = RunStatus(
        run_id=run_id, status="running", placement="local", per_node=[])
    runner._procs[run_id] = process
    runner._cancel_files[run_id] = str(job_dir / "cancel.requested")
    runner._source_leases[run_id] = {
        "stack": stack,
        "guards": [object()],
        "attempts": {"s3://source.attempt-parent": {}},
    }
    retries = []
    monkeypatch.setattr(
        runner, "_watch_inner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("watcher bug")))
    monkeypatch.setattr(
        runner, "_schedule_watch_retry",
        lambda *_args, **_kwargs: retries.append("retry"))

    try:
        runner._watch(
            run_id, process, str(job_dir / "status.json"), str(job_dir),
            Graph.model_validate({
                "id": "unreaped", "version": 1, "nodes": [], "edges": [],
            }), None)

        assert runner.status(run_id).status == "running"
        assert runner.status(run_id).stalled is True
        assert retries == ["retry"]
        assert runner._procs[run_id] is process
        assert runner._source_leases[run_id]["stack"] is stack
        assert stack.closed is False
        assert job_dir.is_dir()
    finally:
        runner._procs.clear()
        runner._cancel_files.clear()
        runner._release_source_leases(run_id)
        runner.runs.clear()
        shutil.rmtree(job_dir, ignore_errors=True)
