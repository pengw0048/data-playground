"""Fail-closed startup checks for the real S3 validation environments."""

from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from hub import s3_validation


def _error(status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "ListBuckets",
    )


def test_client_keeps_normal_artifact_timeout_separate_from_readiness(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(boto3, "client", factory)
    monkeypatch.setenv("DP_S3_ENDPOINT", "http://object-store:8333")
    monkeypatch.setenv("DP_S3_KEY", "validation-key")
    monkeypatch.setenv("DP_S3_SECRET", "validation-secret")
    for name in ("AWS_ENDPOINT_URL_S3", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)

    s3_validation.client_from_env()
    config = factory.call_args.kwargs["config"]
    assert config.read_timeout == 30
    assert config.retries == {"total_max_attempts": 1}
    assert factory.call_args.kwargs["endpoint_url"] == "http://object-store:8333"
    assert factory.call_args.kwargs["aws_access_key_id"] == "validation-key"

    s3_validation.wait_for_s3(bucket="dpray")
    assert factory.call_args.kwargs["config"].read_timeout == 3
    factory.return_value.head_bucket.assert_called_once_with(Bucket="dpray")


def test_client_preserves_temporary_aws_credentials_without_network(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-sts-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-sts-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fake-sts-token")
    client = s3_validation.client_from_env()
    try:
        credentials = client._request_signer._credentials.get_frozen_credentials()
        assert credentials.access_key == "fake-sts-access"
        assert credentials.secret_key == "fake-sts-secret"
        assert credentials.token == "fake-sts-token"
    finally:
        client.close()


def test_readiness_retries_connection_and_server_startup_failures(monkeypatch):
    client = Mock()
    client.list_buckets.side_effect = [
        EndpointConnectionError(endpoint_url="http://object-store:8333"),
        _error(503),
        {"Buckets": []},
    ]
    monkeypatch.setattr(s3_validation, "client_from_env", lambda **_: client)
    monkeypatch.setattr(s3_validation.time, "sleep", lambda _: None)
    assert s3_validation.wait_for_s3() is client
    assert client.list_buckets.call_count == 3


@pytest.mark.parametrize("status", [403, 404])
def test_readiness_does_not_hide_credentials_or_bucket_errors(monkeypatch, status):
    client = Mock()
    failure = _error(status)
    client.list_buckets.side_effect = failure
    monkeypatch.setattr(s3_validation, "client_from_env", lambda **_: client)
    with pytest.raises(ClientError) as caught:
        s3_validation.wait_for_s3()
    assert caught.value is failure
    assert client.list_buckets.call_count == 1


def test_readiness_has_a_finite_deadline(monkeypatch):
    client = Mock()
    failure = EndpointConnectionError(endpoint_url="http://object-store:8333")
    client.list_buckets.side_effect = failure
    monkeypatch.setattr(s3_validation, "client_from_env", lambda **_: client)
    now = [0.0]
    monkeypatch.setattr(s3_validation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(s3_validation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(TimeoutError) as caught:
        s3_validation.wait_for_s3(timeout=2)
    assert caught.value.__cause__ is failure
    assert client.list_buckets.call_count == 2


@pytest.mark.parametrize("existing", [False, True])
def test_bootstrap_requires_readback_of_enabled_versioning(monkeypatch, existing):
    client = Mock()
    client.list_buckets.return_value = {"Buckets": [{"Name": "dpray"}] if existing else []}
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    monkeypatch.setattr(s3_validation, "wait_for_s3", Mock())
    monkeypatch.setattr(s3_validation, "client_from_env", lambda: client)
    monkeypatch.setenv("DP_S3_BUCKET", "dpray")
    s3_validation.bootstrap_storage()
    assert client.create_bucket.call_count == (0 if existing else 1)
    client.put_bucket_versioning.assert_called_once_with(
        Bucket="dpray", VersioningConfiguration={"Status": "Enabled"},
    )
    client.get_bucket_versioning.return_value = {}
    with pytest.raises(RuntimeError, match="did not enable versioning"):
        s3_validation.bootstrap_storage()
