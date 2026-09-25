"""Live S3 capability check; run with ``python -m hub.s3_storage_check``.

Requires DP_S3_BUCKET and an already versioned bucket. Uses the credentials and
endpoint accepted by hub.s3_validation. Only this invocation's unique prefix and
namespace claim are cleaned up; bucket configuration is never changed.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from botocore.exceptions import ClientError

from hub.s3_validation import client_from_env


def require(condition: object, message: str) -> None:
    """Checks must remain active when Python is invoked with -O."""
    if not condition:
        raise RuntimeError(message)


def passed(message: str) -> None:
    print(f"PASS {message}", flush=True)


def read_body(response: dict, expected_size: int) -> bytes:
    body = response["Body"]
    try:
        result = body.read(expected_size + 1)
        require(len(result) == expected_size, "object body has unexpected length")
        return result
    finally:
        body.close()


def version_entries(client, bucket: str, prefix: str, *, page_size: int = 1,
                    allow_null: bool = False) -> list[dict]:
    entries = []
    deadline = time.monotonic() + 30
    pages = client.get_paginator("list_object_versions").paginate(
        Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": page_size})
    for number, page in enumerate(pages, 1):
        require(number <= 128 and time.monotonic() < deadline,
                "version pagination exceeded its bounded budget")
        for field, kind in (("Versions", "object_version"), ("DeleteMarkers", "delete_marker")):
            for item in page.get(field, []):
                require(item["Key"].startswith(prefix), "version listing escaped requested prefix")
                require(item.get("VersionId") not in ((None, "") if allow_null else (None, "", "null")),
                        "versioned object has no immutable version ID")
                entries.append({**item, "kind": kind})
    identities = {(item["Key"], item["VersionId"]) for item in entries}
    require(len(identities) == len(entries), "pagination repeated an object version")
    return entries


def expect_missing(call, message: str) -> None:
    try:
        response = call()
    except ClientError as exc:
        require(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404,
                f"{message}: expected 404, got {exc.response.get('Error', {}).get('Code')}")
        return
    if response.get("Body") is not None:
        response["Body"].close()
    raise RuntimeError(message)


def concurrent_conditional_put(client, bucket: str, key: str, condition: dict) -> str:
    barrier = threading.Barrier(8, timeout=10)

    def write(index: int):
        payload = f"{next(iter(condition))}-writer-{index}".encode()
        barrier.wait()
        try:
            response = client.put_object(Bucket=bucket, Key=key, Body=payload, **condition)
            return (payload, response["ETag"])
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status not in (409, 412):
                raise
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write, range(8)))
    winners = [result for result in results if result is not None]
    require(len(winners) == 1, f"{next(iter(condition))}: expected 1 winner, got {len(winners)}")
    payload, etag = winners[0]
    response = client.get_object(Bucket=bucket, Key=key)
    require(response["ETag"] == etag and read_body(response, len(payload)) == payload,
            "conditional winner does not match the stored object")
    passed(f"8 concurrent {next(iter(condition))} writes: 1 winner, 7 conflicts (409/412)")
    return etag


def configure_private_metadata() -> None:
    from hub import metadb

    metadb.init_db()
    fields = {
        "endpoint": os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ["DP_S3_ENDPOINT"],
        "accessKeyId": ("env:AWS_ACCESS_KEY_ID" if os.environ.get("AWS_ACCESS_KEY_ID")
                        else "env:DP_S3_KEY"),
        "secretAccessKey": ("env:AWS_SECRET_ACCESS_KEY" if os.environ.get("AWS_SECRET_ACCESS_KEY")
                            else "env:DP_S3_SECRET"),
        "region": os.environ.get("AWS_REGION") or "us-east-1",
    }
    if os.environ.get("AWS_SESSION_TOKEN"):
        fields["sessionToken"] = "env:AWS_SESSION_TOKEN"
    cred = metadb.cred_upsert("s3-capability-check", "S3 capability check", "object_store", fields)
    metadb.set_setting("defaultObjectStoreCredId", cred["id"], "global")


def check_storage(bucket: str, prefix: str, namespace: str) -> None:
    from hub.handoff import Boto3ManagedObjectProvider, NamespaceClaimConflict

    client = client_from_env()
    require(client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled",
            "bucket must already have versioning Enabled; this check does not change bucket mode")
    passed("bucket versioning is already Enabled")
    configure_private_metadata()
    uri = f"s3://{bucket}/{prefix}attempt"
    provider = Boto3ManagedObjectProvider(uri)
    # Use the same real S3 endpoint with bounded SDK timeouts for provider operations.
    provider.client.close()
    provider.client = client
    key = prefix + "attempt/versions.bin"
    old, latest = b"original-data", b"replacement-data-longer"
    first = client.put_object(Bucket=bucket, Key=key, Body=old)
    second = client.put_object(Bucket=bucket, Key=key, Body=latest)
    ids = [first.get("VersionId"), second.get("VersionId")]
    require(all(value not in (None, "", "null") for value in ids) and ids[0] != ids[1],
            "two writes did not produce distinct immutable versions")
    for result, data in ((first, old), (second, latest)):
        head = client.head_object(Bucket=bucket, Key=key, VersionId=result["VersionId"])
        require(head["ContentLength"] == len(data) and head["ETag"] == result["ETag"],
                "HEAD did not preserve version size/ETag")
        require(read_body(client.get_object(Bucket=bucket, Key=key,
                                           VersionId=result["VersionId"]), len(data)) == data,
                "historical version returned the wrong bytes")
    ranged = client.get_object(Bucket=bucket, Key=key, Range="bytes=2-7")
    require(ranged["ResponseMetadata"]["HTTPStatusCode"] == 206
            and ranged.get("ContentRange") == f"bytes 2-7/{len(latest)}"
            and read_body(ranged, 6) == latest[2:8], "range GET returned incorrect bytes or headers")
    copied_key = prefix + "copied.bin"
    client.copy_object(Bucket=bucket, Key=copied_key,
                       CopySource={"Bucket": bucket, "Key": key, "VersionId": first["VersionId"]})
    require(read_body(client.get_object(Bucket=bucket, Key=copied_key), len(old)) == old,
            "copying the historical version returned different bytes")
    client.delete_object(Bucket=bucket, Key=copied_key)
    expect_missing(lambda: client.head_object(Bucket=bucket, Key=copied_key),
                   "copy remained visible after delete")
    passed("version-specific HEAD/GET, range GET, historical copy and delete")

    marker = client.delete_object(Bucket=bucket, Key=key)
    require(marker.get("DeleteMarker") is True, "DELETE did not create a delete marker")
    entries = version_entries(client, bucket, key)
    expected_ids = {*ids, marker.get("VersionId")}
    require(len(entries) == 3 and {item["VersionId"] for item in entries} == expected_ids,
            "PageSize=1 omitted or added versions/delete markers")
    for item in entries:
        require(item["Key"] == key, "version pagination included an unexpected key")
        require(item.get("IsLatest") is (item["VersionId"] == marker["VersionId"]),
                "version listing reported incorrect IsLatest")
        if item["kind"] == "object_version":
            result, data = (first, old) if item["VersionId"] == first["VersionId"] else (second, latest)
            require(item.get("ETag") == result["ETag"] and item.get("Size") == len(data),
                    "version listing lost ETag/Size")
    expect_missing(lambda: client.get_object(Bucket=bucket, Key=key),
                   "delete marker did not hide the latest object")
    require(read_body(client.get_object(Bucket=bucket, Key=key, VersionId=first["VersionId"]),
                      len(old)) == old, "delete marker destroyed historical data")
    passed("two historical versions + tombstone: PageSize=1, IDs, IsLatest, ETag, Size")

    pending_key = prefix + "attempt/pending.bin"
    upload = client.create_multipart_upload(Bucket=bucket, Key=pending_key)["UploadId"]
    client.upload_part(Bucket=bucket, Key=pending_key, UploadId=upload, PartNumber=1, Body=b"pending")
    inventory = provider.inventory(uri)
    require({item["version_id"] for item in inventory if item["member_type"] != "multipart_upload"}
            == expected_ids, "managed provider inventory lost versions or markers")
    pending = [item for item in inventory if item["upload_id"] == upload]
    require(len(pending) == 1, "managed provider inventory did not list the pending upload")
    provider.delete_exact(uri, pending[0])
    require(not any(item["upload_id"] == upload for item in provider.inventory(uri)),
            "managed provider did not abort the exact multipart upload")
    for version_id in (marker["VersionId"], first["VersionId"]):
        member = next(item for item in inventory if item["version_id"] == version_id)
        provider.delete_exact(uri, member)
        require(read_body(client.get_object(Bucket=bucket, Key=key), len(latest)) == latest,
                "exact version deletion damaged the latest object")
    remaining = provider.inventory(uri)
    require(len(remaining) == 1 and remaining[0]["version_id"] == second["VersionId"]
            and remaining[0]["is_latest"], "exact delete did not preserve only the latest version")
    passed("managed provider inventory, exact marker/version deletion, multipart list + abort")

    root_key = prefix + "attempt/"
    root_marker = client.put_object(Bucket=bucket, Key=root_key, Body=b"")
    # Some S3 implementations retain directory markers as the explicit null
    # version even in versioned buckets. Core must inventory/delete that member;
    # directory-marker history is not required, unlike the data versions above.
    root_versions = version_entries(client, bucket, root_key, page_size=1000, allow_null=True)
    root_versions = [item for item in root_versions if item["Key"] == root_key]
    require(len(root_versions) == 1, "zero-byte root marker is absent from the version inventory")
    root_version_id = root_versions[0]["VersionId"]
    require(root_marker.get("VersionId") in (None, root_version_id),
            "root marker PUT and version listing disagree")
    root_members = [item for item in provider.inventory(uri) if item["key"] == f"{bucket}/{root_key}"]
    require(len(root_members) == 1 and root_members[0]["version_id"] == root_version_id
            and root_members[0]["member_type"] == "object_version"
            and root_members[0]["size"] == 0 and root_members[0]["is_latest"],
            "managed provider inventory omitted the versioned zero-byte root marker")
    provider.delete_exact(uri, root_members[0])
    require(not any(item["key"] == f"{bucket}/{root_key}" for item in provider.inventory(uri)),
            "exact deletion left the root marker in the managed inventory")
    require(read_body(client.get_object(Bucket=bucket, Key=key), len(latest)) == latest,
            "root marker deletion damaged the current dataset object")
    passed(f"zero-byte root marker is inventoried and deleted exactly (VersionId={root_version_id})")

    race_key = prefix + "conditional.bin"
    etag = concurrent_conditional_put(client, bucket, race_key, {"IfNoneMatch": "*"})
    concurrent_conditional_put(client, bucket, race_key, {"IfMatch": etag})
    claim = json.dumps({"namespace": namespace, "generation": 1}).encode()
    first_etag = provider.write_namespace_claim(uri, namespace, claim, None)
    next_claim = json.dumps({"namespace": namespace, "generation": 2}).encode()
    provider.write_namespace_claim(uri, namespace, next_claim, first_etag)
    for expected in (None, first_etag):
        try:
            provider.write_namespace_claim(uri, namespace, claim, expected)
        except NamespaceClaimConflict:
            continue
        raise RuntimeError("managed provider accepted a stale namespace claim")
    observed = provider.read_namespace_claim(uri, namespace)
    require(observed is not None and observed["doc"] == json.loads(next_claim),
            "namespace CAS did not preserve its winner")
    passed("managed provider namespace create/update CAS rejects stale owners")

    multipart_key = prefix + "multipart.bin"
    upload = client.create_multipart_upload(Bucket=bucket, Key=multipart_key)["UploadId"]
    chunks = [b"a" * (5 * 1024 * 1024), b"final-part"]
    parts = []
    for number, chunk in enumerate(chunks, 1):
        result = client.upload_part(Bucket=bucket, Key=multipart_key, UploadId=upload,
                                    PartNumber=number, Body=chunk)
        parts.append({"PartNumber": number, "ETag": result["ETag"]})
    completed = client.complete_multipart_upload(
        Bucket=bucket, Key=multipart_key, UploadId=upload, MultipartUpload={"Parts": parts})
    require(completed.get("VersionId") not in (None, "", "null"),
            "completed multipart upload has no version ID")
    payload = b"".join(chunks)
    require(read_body(client.get_object(Bucket=bucket, Key=multipart_key,
                                       VersionId=completed["VersionId"]), len(payload)) == payload,
            "multipart completion changed or truncated the payload")
    require(not client.list_multipart_uploads(Bucket=bucket, Prefix=multipart_key).get("Uploads"),
            "completed multipart upload remained pending")
    passed("two-part multipart upload (5 MiB + tail), complete and exact version readback")

    from hub import db
    from hub.plugins.adapters import DuckDBAdapter

    adapter = DuckDBAdapter()
    target = f"s3://{bucket}/{prefix}adapter.parquet"
    adapter.write(target, db.conn().sql("SELECT * FROM (VALUES (1), (2)) t(value)"), "overwrite")
    require(adapter.scan(target).order("value").fetchall() == [(1,), (2,)],
            "file adapter overwrite was not readable")
    appended = adapter.write(target, db.conn().sql("SELECT * FROM (VALUES (3), (4)) t(value)"), "append")
    require(adapter.scan(appended["uri"]).order("value").fetchall() == [(1,), (2,), (3,), (4,)],
            "file adapter overwrite -> append lost prior or appended rows (check HEAD/copy migration)")
    passed("DuckDB/PyArrow file adapter overwrite -> append preserves all four rows")


def cleanup(client, bucket: str, prefix: str, namespace: str) -> None:
    claim_key = f"_dp_control/namespaces/{namespace}.json"
    deadline = time.monotonic() + 45
    for scope in (prefix, claim_key):
        for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket, Prefix=scope):
            require(time.monotonic() < deadline, "cleanup upload listing exceeded its deadline")
            for item in page.get("Uploads", []):
                require(item["Key"].startswith(prefix) or item["Key"] == claim_key,
                        "refusing cleanup outside this validation prefix/claim")
                client.abort_multipart_upload(Bucket=bucket, Key=item["Key"], UploadId=item["UploadId"])
        entries = version_entries(client, bucket, scope, page_size=1000, allow_null=True)
        for item in entries:
            require(time.monotonic() < deadline, "cleanup exceeded its deadline")
            require(item["Key"].startswith(prefix) or item["Key"] == claim_key,
                    "refusing version deletion outside this validation prefix/claim")
            client.delete_object(Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])
        require(not version_entries(client, bucket, scope, page_size=1000, allow_null=True),
                "cleanup left object versions or delete markers")
        uploads = client.list_multipart_uploads(Bucket=bucket, Prefix=scope)
        require(not uploads.get("Uploads") and not uploads.get("IsTruncated"),
                "cleanup left pending multipart uploads")
    passed("cleanup removed only this validation prefix and namespace claim")


def main() -> int:
    bucket = os.environ.get("DP_S3_BUCKET", "").strip()
    require(bucket, "DP_S3_BUCKET must name an existing versioned bucket")
    require(os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("DP_S3_ENDPOINT"),
            "DP_S3_ENDPOINT or AWS_ENDPOINT_URL_S3 is required")
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        check_storage(bucket, sys.argv[2], sys.argv[3])
        return 0
    require(len(sys.argv) == 1, "usage: python -m hub.s3_storage_check")
    identity = uuid.uuid4().hex
    prefix, namespace = f"_dp_validation/{identity}/", f"validation-{identity}"
    client = client_from_env()
    require(client.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled",
            "bucket must already have versioning Enabled; bucket mode was not modified")
    print(f"Checking s3://{bucket}/{prefix} (temporary objects; bucket mode unchanged)", flush=True)
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix="dp-s3-capability-") as directory:
            env = dict(os.environ, DP_WORKSPACE=directory, DP_DATA_DIR=directory,
                       DP_DATABASE_URL=f"sqlite:///{Path(directory) / 'metadata.db'}",
                       DP_PLUGINS="")
            env.pop("DP_AUTH_SECRET", None)
            env.pop("DP_AUTH_MODE", None)
            result = subprocess.run(
                [sys.executable, *(["-O"] if sys.flags.optimize else []), "-m", __spec__.name,
                 "--worker", prefix, namespace], env=env, timeout=180, check=False)
            require(result.returncode == 0, f"storage capability worker failed (exit {result.returncode})")
        success = True
    finally:
        cleanup(client, bucket, prefix, namespace)
        client.close()
    if success:
        passed("all S3 storage capabilities required by this check")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL S3 storage capability check: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
