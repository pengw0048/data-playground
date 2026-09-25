"""Provider-neutral S3 setup for the disposable Ray validation environments."""

from __future__ import annotations

import json
import os
import time


def client_from_env(*, request_timeout: float = 30):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("DP_S3_ENDPOINT"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("DP_S3_KEY"),
        aws_secret_access_key=(
            os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("DP_S3_SECRET")
        ),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
        region_name=os.environ.get("AWS_REGION") or "us-east-1",
        # The outer readiness loop owns the deadline; SDK retries must not multiply it.
        config=Config(
            connect_timeout=3, read_timeout=request_timeout,
            retries={"total_max_attempts": 1},
        ),
    )


def wait_for_s3(*, bucket: str | None = None, timeout: float = 90):
    """Require a signed S3 operation, retrying startup failures but not bad credentials."""
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
    )

    client = client_from_env(request_timeout=3)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if bucket is None:
                client.list_buckets()
            else:
                client.head_bucket(Bucket=bucket)
            return client
        except (
            EndpointConnectionError, ConnectionClosedError,
            ConnectTimeoutError, ReadTimeoutError,
        ) as exc:
            last_error = exc
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status < 500:
                raise
            last_error = exc
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"S3 readiness timed out after {timeout}s: {last_error}") from last_error


def bootstrap_storage() -> None:
    wait_for_s3()
    client = client_from_env()
    bucket = os.environ.get("DP_S3_BUCKET", "dpray")
    names = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
    if bucket not in names:
        client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    if client.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise RuntimeError(f"S3 bucket {bucket!r} did not enable versioning")
    print(json.dumps({"check": "storage", "bucket": bucket, "versioning": "Enabled"}))


if __name__ == "__main__":
    bootstrap_storage()
