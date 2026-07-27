"""The optional exact media adapter stays bounded, typed, and revision-pinned."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hub.main import app
from hub.media_cells import (
    ExactMediaCellRead,
    ExactMediaCellResult,
    MediaCellOffline,
    MediaCellSourceDenied,
    MediaCellTooLarge,
    MediaCellUnavailable,
    MediaCellUnsupported,
    read_exact_media_cell,
    supports_exact_media_cell,
)
from hub.models import ColumnSchema, MediaCellRequest
from hub.plugins.adapters import RevisionPermissionLost, RevisionProviderOffline, RevisionUnavailable
from hub.workspace_providers import _BoundProviderDatasetAdapter


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBM = b"\x1aE\xdf\xa3webm"


def _request() -> MediaCellRequest:
    return MediaCellRequest.model_validate({
        "identity": [{"name": "id", "arrowType": "int32", "value": "7"}],
        "column": "media",
    })


def _descriptor():
    return SimpleNamespace(spec=SimpleNamespace(fields=(
        SimpleNamespace(name="id", arrow_type="int32"),
    )))


class _Adapter:
    name = "fake-exact-media"

    def __init__(self, *, supported: bool = True, result=ExactMediaCellResult(PNG, "image/png")):
        self.supported = supported
        self.result = result
        self.calls: list[tuple[str, object]] = []

    def supports_media_cell(self, uri: str, revision_id: str) -> bool:
        self.calls.append(("supports", (uri, revision_id)))
        return self.supported

    @staticmethod
    def revision_schema(_uri: str, _revision_id: str):
        return [ColumnSchema(name="media", type="bytes", media_kind="image")]

    @staticmethod
    def media_cell_identity_descriptor(_uri: str, _revision_id: str):
        return _descriptor()

    def read_media_cell(self, uri: str, request):
        self.calls.append(("read", (uri, request)))
        return self.result


def _read(adapter, *, uri: str = "provider://dataset", revision_id: str = "revision-7"):
    return read_exact_media_cell(
        storage=None, adapter=adapter, dataset_uri=uri, dataset_id="dataset-stable",
        revision_id=revision_id, request=_request(), max_bytes=1024)


def test_optional_adapter_is_explicit_and_uses_only_the_requested_exact_revision():
    adapter = _Adapter()

    content, content_type = _read(adapter)

    assert (content, content_type) == (PNG, "image/png")
    assert adapter.calls[0] == ("supports", ("provider://dataset", "revision-7"))
    assert adapter.calls[1][0] == "read"
    assert adapter.calls[1][1][0] == "provider://dataset"
    read = adapter.calls[-1][1][1]
    assert read.dataset_id == "dataset-stable"
    assert read.revision_id == "revision-7"
    assert read.identity == (7,)
    assert read.column == "media" and read.max_bytes == 1024 and read.expected_kind == "image"
    assert callable(read.source_policy)

    # A changed mutable head cannot cause a read of "latest": the dispatcher passes only the
    # caller's exact revision and surfaces the adapter's unavailable result unchanged.
    def unavailable_after_head(_uri, read):
        assert read.revision_id == "older-revision"
        raise RevisionUnavailable("revision compacted")

    adapter.read_media_cell = unavailable_after_head
    with pytest.raises(MediaCellUnavailable):
        _read(adapter, revision_id="older-revision")
    assert all(call[1][1].revision_id != "latest" for call in adapter.calls if call[0] == "read")


def test_source_policy_callback_is_one_value_only_and_exposes_no_source_authority():
    class PolicyAdapter(_Adapter):
        def read_media_cell(self, uri: str, request):
            self.calls.append(("read", (uri, request)))
            with pytest.raises(TypeError):
                request.source_policy(PNG, "forbidden-extra-argument")
            return ExactMediaCellResult(request.source_policy(PNG), "image/png")

    assert {field.name for field in fields(ExactMediaCellRead)} == {
        "dataset_id", "revision_id", "identity", "column", "max_bytes",
        "expected_kind", "source_policy",
    }
    content, content_type = _read(PolicyAdapter())
    assert (content, content_type) == (PNG, "image/png")


def test_source_policy_callback_reuses_the_core_value_bound():
    class PolicyAdapter(_Adapter):
        def __init__(self, value):
            super().__init__()
            self.value = value

        def read_media_cell(self, _uri: str, request):
            return ExactMediaCellResult(request.source_policy(self.value), "image/png")

    with pytest.raises(MediaCellTooLarge):
        _read(PolicyAdapter(PNG + b"x" * 2048))
    with pytest.raises(MediaCellUnsupported):
        _read(PolicyAdapter(object()))


def test_method_absent_and_exact_opt_out_fail_closed_without_reader_invocation():
    class NoCapability:
        pass

    assert supports_exact_media_cell(NoCapability(), "provider://dataset", "revision-7") is False
    with pytest.raises(MediaCellUnsupported):
        _read(NoCapability())

    opted_out = _Adapter(supported=False)
    with pytest.raises(MediaCellUnsupported):
        _read(opted_out)
    assert opted_out.calls == [("supports", ("provider://dataset", "revision-7"))]


@pytest.mark.parametrize("result,error", [
    (object(), MediaCellUnsupported),
    (ExactMediaCellResult(b"not media", "image/png"), MediaCellUnsupported),
    (ExactMediaCellResult(PNG + b"x" * 2048, "image/png"), MediaCellTooLarge),
    (ExactMediaCellResult(WEBM, "video/webm"), MediaCellUnsupported),
])
def test_malformed_oversized_and_wrong_kind_provider_results_are_rejected(result, error):
    with pytest.raises(error):
        _read(_Adapter(result=result))


@pytest.mark.parametrize("failure,error", [
    (RevisionPermissionLost("no access"), MediaCellSourceDenied),
    (RevisionProviderOffline("offline"), MediaCellOffline),
    (RevisionUnavailable("gone"), MediaCellUnavailable),
])
def test_typed_provider_refusals_keep_the_public_media_taxonomy(failure, error):
    adapter = _Adapter()

    def fail(_uri, _request):
        raise failure

    adapter.read_media_cell = fail
    with pytest.raises(error):
        _read(adapter)


@pytest.mark.parametrize("failure,error", [
    (RevisionPermissionLost("no access"), MediaCellSourceDenied),
    (RevisionProviderOffline("offline"), MediaCellOffline),
    (RevisionUnavailable("gone"), MediaCellUnavailable),
])
def test_typed_capability_probe_failures_keep_the_public_media_taxonomy(failure, error):
    adapter = _Adapter()

    def fail(_uri, _revision_id):
        raise failure

    adapter.supports_media_cell = fail
    with pytest.raises(error):
        _read(adapter)


def test_bound_provider_delegates_only_through_the_canonical_source_binding():
    physical = _Adapter()
    bound = _BoundProviderDatasetAdapter(
        "workspace-provider://canonical", "provider://physical", physical)

    content, content_type = _read(bound, uri="workspace-provider://canonical")

    assert (content, content_type) == (PNG, "image/png")
    assert physical.calls[0] == ("supports", ("provider://physical", "revision-7"))
    assert physical.calls[-1][1][0] == "provider://physical"
    with pytest.raises(MediaCellUnsupported):
        _read(bound, uri="workspace-provider://different")


class _RouteAdapter:
    name = "route-exact-media"

    @staticmethod
    def supports_media_cell(_uri: str, _revision_id: str) -> bool:
        return True

    @staticmethod
    def revision_schema(_uri: str, _revision_id: str):
        return [ColumnSchema(name="media", type="bytes", media_kind="image")]

    @staticmethod
    def media_cell_identity_descriptor(_uri: str, _revision_id: str):
        return _descriptor()

    @staticmethod
    def read_media_cell(_uri: str, _request):
        return ExactMediaCellResult(PNG, "image/png")


class _ProbeLookupFailure:
    name = "probe-lookup-failure"

    def __getattr__(self, name: str):
        if name == "supports_media_cell":
            raise RuntimeError("super-secret probe lookup detail")
        raise AttributeError(name)


class _ReaderLookupFailure:
    name = "reader-lookup-failure"
    supports_media_cell = staticmethod(_RouteAdapter.supports_media_cell)
    revision_schema = staticmethod(_RouteAdapter.revision_schema)
    media_cell_identity_descriptor = staticmethod(
        _RouteAdapter.media_cell_identity_descriptor)

    def __getattr__(self, name: str):
        if name == "read_media_cell":
            raise RuntimeError("super-secret reader lookup detail")
        raise AttributeError(name)


class _SchemaFailure(_RouteAdapter):
    @staticmethod
    def revision_schema(_uri: str, _revision_id: str):
        raise RuntimeError("super-secret schema detail")


class _DescriptorFailure(_RouteAdapter):
    @staticmethod
    def media_cell_identity_descriptor(_uri: str, _revision_id: str):
        raise RuntimeError("super-secret descriptor detail")


@pytest.mark.parametrize(("physical", "status", "body"), [
    (
        _ProbeLookupFailure(), 415,
        {
            "detail": "media_cell_unsupported",
            "code": "media_cell_unsupported",
            "retryable": False,
        },
    ),
    (
        _ReaderLookupFailure(), 415,
        {
            "detail": "media_cell_unsupported",
            "code": "media_cell_unsupported",
            "retryable": False,
        },
    ),
    (
        _SchemaFailure(), 410,
        {
            "detail": "media_cell_unavailable",
            "code": "resource_gone",
            "retryable": False,
        },
    ),
    (
        _DescriptorFailure(), 409,
        {
            "detail": "media_cell_identity_unavailable",
            "code": "media_cell_identity_unavailable",
            "retryable": False,
        },
    ),
])
def test_route_sanitizes_dynamic_lookup_schema_and_descriptor_failures(
        physical, status, body, monkeypatch):
    from hub.plugins import adapters as plugin_adapters
    from hub.routers import catalog as catalog_routes

    logical_uri = "workspace-provider://canonical"
    bound = _BoundProviderDatasetAdapter(logical_uri, "provider://physical", physical)
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.setattr(
        catalog_routes, "_revision_binding_for_dataset_id",
        lambda _dataset_id: {"dataset_id": "dataset-stable", "uri": logical_uri},
    )
    monkeypatch.setattr(
        plugin_adapters, "managed_local_file_revision_adapter", lambda _uri: None)
    monkeypatch.setattr(catalog_routes, "_revision_adapter", lambda _uri: bound)
    monkeypatch.setattr(
        catalog_routes, "get_deps", lambda: SimpleNamespace(storage=None))

    response = TestClient(app).post(
        "/api/catalog/revisions/dataset-stable/revision-7/media-cell",
        headers={"X-DP-User": "media-adapter-contract"},
        json={
            "identity": [{"name": "id", "arrowType": "int32", "value": "7"}],
            "column": "media",
        },
    )

    assert response.status_code == status
    assert response.json() == body
    assert "super-secret" not in response.text
