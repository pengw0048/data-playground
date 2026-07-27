"""Public plugin SDK revision-access failure conformance."""

from __future__ import annotations

import errno
import importlib.util
import socket
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub.deps import Registry
from hub.plugins.adapters import LanceAdapter
from hub.sdk import (
    RevisionPermissionLost,
    RevisionProviderOffline,
    RevisionResolutionAmbiguous,
    RevisionUnavailable,
)


def _reference_plugin():
    source = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "dp_revision_access_fixture"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dp_revision_access_fixture_reference", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _http_forbidden() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://provider.example/revision", 403, "Forbidden", {}, None)


@pytest.mark.parametrize(("failure_factory", "expected"), [
    (lambda: ValueError("LanceError(IO): Permission denied (os error 13)"),
     RevisionPermissionLost),
    (lambda: TimeoutError("object-store request timed out"), RevisionProviderOffline),
    (lambda: OSError(errno.EHOSTUNREACH, "No route to host"), RevisionProviderOffline),
    (lambda: socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
     RevisionProviderOffline),
    (_http_forbidden, RevisionUnavailable),
], ids=["errno-13", "timeout", "network-unreachable", "dns", "http-not-inferred"])
@pytest.mark.parametrize("adapter_kind", ["reference", "lance"])
def test_builtin_and_public_reference_adapter_share_revision_access_policy(
        monkeypatch, failure_factory, expected, adapter_kind):
    failure = failure_factory()
    if adapter_kind == "reference":
        adapter = _reference_plugin().RevisionAccessFixtureAdapter()
        monkeypatch.setattr(
            adapter, "_open_exact",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
    else:
        adapter = LanceAdapter()
        monkeypatch.setattr(
            adapter, "_dataset",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))

    with pytest.raises(expected) as caught:
        adapter.revision_detail("revision-access-fixture://table", "1", preview_limit=1)
    assert str(caught.value) == {
        RevisionPermissionLost: "revision_permission_lost",
        RevisionProviderOffline: "revision_provider_offline",
        RevisionUnavailable: "revision_unavailable",
    }[expected]
    assert caught.value.__cause__ is failure


def test_reference_plugin_registers_using_only_the_public_exception_surface():
    module = _reference_plugin()
    deps = SimpleNamespace(adapters=[])
    module.register(Registry(deps))
    assert len(deps.adapters) == 1
    assert deps.adapters[0].name == "revision-access-fixture"


def test_public_revision_exception_types_remain_adapter_compatible():
    from hub.plugins import adapters

    assert adapters.RevisionUnavailable is RevisionUnavailable
    assert adapters.RevisionPermissionLost is RevisionPermissionLost
    assert adapters.RevisionProviderOffline is RevisionProviderOffline
    assert adapters.RevisionResolutionAmbiguous is RevisionResolutionAmbiguous
