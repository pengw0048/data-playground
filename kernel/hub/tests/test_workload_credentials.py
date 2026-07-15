"""Destination-write identity contracts across isolated workload boundaries."""

from __future__ import annotations

import contextlib
import json

import pytest

from hub import metadb
from hub.settings import settings
from hub.workload_credentials import (
    DESTINATION_CREDENTIAL_ERROR_REASON,
    DestinationCredentialError,
    authorize_destination,
    create_fd_capability,
    read_fd_capability,
    reauthorize_binding,
    reference_capability,
    resolve_reference_capability,
)


@contextlib.contextmanager
def _isolated_metadata(url: str):
    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = url
    metadb._engine = metadb._Session = None
    try:
        metadb.init_db()
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _install_destination(
        *, cred_id: str | None, fields: dict, destination_id: str = "archive") -> str:
    cred = metadb.cred_upsert(
        cred_id, "archive writer", "object_store", fields)
    metadb.set_setting("destinations", [{
        "id": destination_id,
        "name": "Archive",
        "backend": "s3",
        "root": "s3://archive-bucket/results",
        "credId": cred["id"],
    }], "global")
    return cred["id"]


def test_frozen_cred_identity_allows_same_id_rotation_but_not_rebinding_or_deletion(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DP_ARCHIVE_KEY_V1", "key-v1")
    monkeypatch.setenv("DP_ARCHIVE_SECRET_V1", "secret-v1")
    monkeypatch.setenv("DP_ARCHIVE_KEY_V2", "key-v2")
    monkeypatch.setenv("DP_ARCHIVE_SECRET_V2", "secret-v2")
    monkeypatch.setenv("DP_OTHER_KEY", "other-key")
    monkeypatch.setenv("DP_OTHER_SECRET", "other-secret")

    with _isolated_metadata(f"sqlite:///{tmp_path / 'identity.db'}"):
        selected_id = _install_destination(cred_id=None, fields={
            "accessKeyId": "env:DP_ARCHIVE_KEY_V1",
            "secretAccessKey": "env:DP_ARCHIVE_SECRET_V1",
            "region": "us-east-1",
        })
        binding, material = authorize_destination(
            str(tmp_path), "archive", "s3://archive-bucket/results/out.parquet")
        assert binding["credential_id"] == selected_id
        assert material["secretAccessKey"] == "secret-v1"

        # Rebinding the destination affects only new runs. The frozen run remains authorized as the
        # original Cred entity instead of silently switching accounts.
        other_id = metadb.cred_upsert(None, "other", "object_store", {
            "accessKeyId": "env:DP_OTHER_KEY",
            "secretAccessKey": "env:DP_OTHER_SECRET",
        })["id"]
        metadb.set_setting("destinations", [{
            "id": "archive", "name": "Archive", "backend": "s3",
            "root": "s3://archive-bucket/results", "credId": other_id,
        }], "global")
        assert reauthorize_binding(binding)[1]["secretAccessKey"] == "secret-v1"

        # Updating SecretRefs on the same stable Cred ID is an intentional rotation and is picked up by
        # a durable retry/replay without changing the authorization identity.
        metadb.cred_upsert(selected_id, "archive writer", "object_store", {
            "accessKeyId": "env:DP_ARCHIVE_KEY_V2",
            "secretAccessKey": "env:DP_ARCHIVE_SECRET_V2",
            "region": "us-east-2",
        })
        assert reauthorize_binding(binding)[1] == {
            "accessKeyId": "key-v2", "secretAccessKey": "secret-v2",
            "region": "us-east-2",
        }

        metadb.cred_delete(selected_id)
        with pytest.raises(DestinationCredentialError) as caught:
            reauthorize_binding(binding)
        assert str(caught.value) == DESTINATION_CREDENTIAL_ERROR_REASON
        assert "secret-v2" not in str(caught.value)


def test_local_fd_capability_keeps_material_out_of_serialized_contract():
    binding = {
        "version": 1,
        "scope": "destination-write",
        "mode": "cred",
        "destination_id": "archive",
        "credential_id": "cred-archive",
    }
    material = {
        "accessKeyId": "attempt-access-key",
        "secretAccessKey": "attempt-secret-value",
        "region": "us-east-1",
    }
    fd, descriptor = create_fd_capability(
        "run-exact", {"write": binding}, {"write": material})
    assert fd is not None and descriptor is not None
    serialized_job = json.dumps({
        "sinkCredentialBindings": {"write": binding},
        "sinkCredentialCapability": descriptor,
    })
    assert "attempt-access-key" not in serialized_job
    assert "attempt-secret-value" not in serialized_job
    assert read_fd_capability(
        descriptor, "run-exact", {"write": binding}) == {"write": material}


def test_remote_reference_capability_is_secret_free_and_reauthorizes_each_submission(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DP_REMOTE_KEY", "remote-key-v1")
    monkeypatch.setenv("DP_REMOTE_SECRET", "remote-secret-v1")

    with _isolated_metadata(f"sqlite:///{tmp_path / 'remote.db'}"):
        cred_id = _install_destination(cred_id=None, fields={
            "accessKeyId": "env:DP_REMOTE_KEY",
            "secretAccessKey": "env:DP_REMOTE_SECRET",
            "region": "us-west-2",
        })
        binding, _material = authorize_destination(
            str(tmp_path), "archive", "s3://archive-bucket/results/out.parquet")
        capability = reference_capability({"write": binding})
        assert "env:DP_REMOTE_SECRET" in capability
        assert "remote-secret-v1" not in capability
        assert json.loads(capability)["entries"]["write"]["binding"]["credential_id"] == cred_id

        # Cluster-side resolution occurs only after the hash-bound job is verified. Rotating material
        # behind an unchanged SecretRef is visible without mutating the durable job envelope.
        monkeypatch.setenv("DP_REMOTE_SECRET", "remote-secret-v2")
        assert resolve_reference_capability(
            capability, {"write": binding})["write"]["secretAccessKey"] == "remote-secret-v2"

        metadb.set_setting("destinations", [], "global")
        metadb.cred_delete(cred_id)
        with pytest.raises(DestinationCredentialError):
            reference_capability({"write": binding})


def test_missing_selected_secret_fails_with_stable_non_secret_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DP_MISSING_DESTINATION_SECRET", raising=False)
    monkeypatch.setenv("DP_PRESENT_DESTINATION_KEY", "unique-access-value")
    with _isolated_metadata(f"sqlite:///{tmp_path / 'missing.db'}"):
        _install_destination(cred_id=None, fields={
            "accessKeyId": "env:DP_PRESENT_DESTINATION_KEY",
            "secretAccessKey": "env:DP_MISSING_DESTINATION_SECRET",
        })
        with pytest.raises(DestinationCredentialError) as caught:
            authorize_destination(
                str(tmp_path), "archive", "s3://archive-bucket/results/out.parquet")
        assert str(caught.value) == DESTINATION_CREDENTIAL_ERROR_REASON
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True
        assert "DP_MISSING_DESTINATION_SECRET" not in str(caught.value)
        assert "unique-access-value" not in str(caught.value)
