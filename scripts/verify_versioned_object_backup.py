"""Prove native filer replication retains S3 history, including an offline-source read."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


IMAGE = (
    "chrislusf/seaweedfs:4.47@sha256:"
    "ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882"
)
KEY = "dp-backup-drill"
SECRET = "dp-backup-drill-not-for-shared-use"
BUCKET = "production"
CONTROL_BUCKET = "control"
MARKER_KEY = "_dp_control/namespaces/drill.json"
MARKER = b'{"namespace":"drill","ownerToken":"drill-owner","claimToken":"drill-claim"}'
# Exercise actual volume chunks, not only small values that a filer may inline.
FIRST = b"first-generation\n" * 131072
SECOND = b"second-generation\n"
DELETED = b"deleted-generation\n"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def manifest(client, bucket: str) -> list[dict]:
    entries = []
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for group, marker in (("Versions", False), ("DeleteMarkers", True)):
            for item in page.get(group, []):
                entries.append({
                    "key": item["Key"], "versionId": item["VersionId"],
                    "isDeleteMarker": marker, "isLatest": item["IsLatest"],
                    "size": item.get("Size", 0), "etag": item.get("ETag", ""),
                })
    return sorted(entries, key=lambda item: (item["key"], item["versionId"]))


def read(client, bucket: str, key: str, version: str | None = None) -> bytes:
    kwargs = {"Bucket": bucket, "Key": key}
    if version is not None:
        kwargs["VersionId"] = version
    response = client.get_object(**kwargs)
    with response["Body"] as body:
        return body.read()


def wait_for(label: str, check, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as exc:  # A starting service/replica is not ready yet.
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {label}; last error: {last}")


def main() -> None:
    context = os.environ.get("DOCKER_CONTEXT")
    docker_command = ["docker"] + (["--context", context] if context else [])
    evidence = Path(os.environ.get("DP_OBJECT_BACKUP_EVIDENCE_DIR") or
                    tempfile.mkdtemp(prefix="dp-object-backup-evidence-"))
    evidence.mkdir(parents=True, exist_ok=True)
    run = f"dp-object-backup-{uuid.uuid4().hex[:10]}"
    network = f"{run}-network"
    containers: list[str] = []
    volumes: list[str] = []

    def docker(*args: str, check: bool = True) -> str:
        result = subprocess.run([*docker_command, *args],
                                text=True, capture_output=True, check=False)
        if check and result.returncode:
            raise RuntimeError(f"docker {' '.join(args[:3])}: {result.stderr.strip()}")
        return ((result.stdout + result.stderr) if args[0] == "logs" else result.stdout).strip()

    def client_for(name: str):
        endpoint = "http://" + docker("port", name, "8333/tcp")
        return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=KEY,
                            aws_secret_access_key=SECRET, region_name="us-east-1",
                            config=Config(connect_timeout=3, read_timeout=5,
                                          retries={"max_attempts": 1},
                                          s3={"addressing_style": "path"}))

    def start(role: str):
        name = f"{run}-{role}"
        volume = f"{name}-data"
        containers.append(name)
        volumes.append(volume)
        docker("create", "--name", name, "--network", network,
               "-p", "127.0.0.1::8333", "--mount", f"type=volume,src={volume},dst=/data",
               IMAGE, "mini", "-dir=/data", f"-ip={name}", "-ip.bind=0.0.0.0",
               "-s3.config=/tmp/s3.json", "-admin.ui=false", "-webdav=false",
               "-s3.port.iceberg=0", "-s3.port.lance=0", "-master.telemetry=false")
        docker("cp", str(evidence / "s3.json"), f"{name}:/tmp/s3.json")
        docker("start", name)
        client = client_for(name)
        wait_for(f"{role} S3", lambda: client.list_buckets() is not None)
        return name, client

    def save(name: str, value) -> None:
        (evidence / name).write_text(json.dumps(value, indent=2) + "\n")

    save("s3.json", {"identities": [{"name": "backup-drill", "credentials": [{
        "accessKey": KEY, "secretKey": SECRET}], "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]})
    try:
        docker("network", "create", network)
        source_name, source = start("source")
        replica_name, replica = start("replica")
        sync = f"{run}-sync"
        containers.append(sync)
        docker("run", "-d", "--name", sync, "--network", network, IMAGE,
               "filer.sync", f"-a={source_name}:8888", f"-b={replica_name}:8888",
               "-a.path=/buckets", "-b.path=/buckets", "-isActivePassive",
               "-concurrency=1", "-chunkConcurrency=2")
        # Start replication before the bucket metadata and protected object writes.
        for bucket in (BUCKET, CONTROL_BUCKET):
            source.create_bucket(Bucket=bucket)
            source.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        first_id = source.put_object(Bucket=BUCKET, Key="history/object.txt", Body=FIRST)["VersionId"]
        source.put_object(Bucket=BUCKET, Key="history/object.txt", Body=SECOND)
        deleted_id = source.put_object(Bucket=BUCKET, Key="tombstone/object.txt", Body=DELETED)["VersionId"]
        source.delete_object(Bucket=BUCKET, Key="tombstone/object.txt")
        source.put_object(Bucket=CONTROL_BUCKET, Key=MARKER_KEY, Body=MARKER, IfNoneMatch="*")
        expected = {bucket: manifest(source, bucket) for bucket in (BUCKET, CONTROL_BUCKET)}
        require(len(expected[BUCKET]) == 4, "source must retain exactly four history entries")
        require(sum(item["isDeleteMarker"] for item in expected[BUCKET]) == 1,
                "source must retain exactly one delete marker")
        save("source-versions.json", expected)

        def replica_matches() -> bool:
            return all(replica.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
                       and manifest(replica, bucket) == expected[bucket] for bucket in expected)

        wait_for("exact version and namespace-marker replication", replica_matches)
        save("replica-versions.json", {bucket: manifest(replica, bucket) for bucket in expected})
        # Recovery proof: neither the source nor its sync process can serve any read.
        docker("stop", "--time", "15", sync, source_name)
        docker("restart", "--time", "15", replica_name)
        replica = client_for(replica_name)  # Docker may allocate a different ephemeral host port.
        wait_for("restarted independent replica", replica_matches)
        require(read(replica, BUCKET, "history/object.txt", first_id) == FIRST,
                "independent replica lost historical bytes")
        require(read(replica, BUCKET, "history/object.txt") == SECOND, "replica current bytes changed")
        require(read(replica, BUCKET, "tombstone/object.txt", deleted_id) == DELETED,
                "replica lost bytes behind the delete marker")
        require(read(replica, CONTROL_BUCKET, MARKER_KEY) == MARKER, "namespace marker changed")
        try:
            read(replica, BUCKET, "tombstone/object.txt")
        except ClientError as exc:
            require(exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404,
                    "deleted current object must return 404")
        else:
            raise AssertionError("replica lost the current delete marker")
        require(all(json.loads(docker("inspect", "--format", "{{json .State.Running}}", name)) is False
                    for name in (sync, source_name)), "source and sync must remain stopped")
        save("recovered-versions.json", {bucket: manifest(replica, bucket) for bucket in expected})

        # Negative control: a GET/current-object + PUT copy cannot preserve native history.
        _copy_name, copied = start("copy")
        copied.create_bucket(Bucket=BUCKET)
        copied.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        for page in replica.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
            for item in page.get("Contents", []):
                copied.put_object(Bucket=BUCKET, Key=item["Key"], Body=read(replica, BUCKET, item["Key"]))
        copied_versions = manifest(copied, BUCKET)
        require(len(copied_versions) == 1, "ordinary copy must lose non-current history")
        require(all(item["versionId"] != first_id and not item["isDeleteMarker"] for item in copied_versions),
                "ordinary copy unexpectedly retained historical identities")
        require(read(copied, BUCKET, "history/object.txt") == SECOND, "ordinary copy lost current bytes")
        save("copy-versions.json", copied_versions)
        print(f"VERSIONED_OBJECT_BACKUP_EVIDENCE method=filer.sync source_down=true replica_restarted=true entries=4 historical_version={first_id}")
        print("VERSIONED_OBJECT_BACKUP_CP_CONTROL=lost_history")
        print(f"Evidence: {evidence}")
    finally:
        for container in containers:
            (evidence / f"{container}.log").write_text(docker("logs", container, check=False))
            docker("rm", "-f", container, check=False)
        for volume in volumes:
            docker("volume", "rm", volume, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    main()
