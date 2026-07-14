"""Fault-injection wrapper for the real Ray Jobs acceptance topology.

The wrapper always delegates successful work to the image-baked production driver. It only adds a
deterministic acceptance barrier (so the submitting hub can replace a catalog generation and exit while
the remote job remains live), or mutates the terminal result after first proving that the production
driver emitted a valid successful receipt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


_PREFIX = "ray-jobs-acceptance-"


def _s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("DP_S3_ENDPOINT"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("DP_S3_KEY"),
        aws_secret_access_key=(
            os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("DP_S3_SECRET")
        ),
        region_name=os.environ.get("AWS_REGION") or "us-east-1",
    )


def _location(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise RuntimeError(f"acceptance result must be an S3 object, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _wait_for_marker(job_uri: str, name: str, description: str) -> None:
    marker_uri = job_uri.rsplit("/", 1)[0] + f"/{name}"
    bucket, key = _location(marker_uri)
    client = _s3_client()
    deadline = time.monotonic() + 90
    while True:
        try:
            client.head_object(Bucket=bucket, Key=key)
            return
        except Exception as exc:  # noqa: BLE001 - retry only the provider's authoritative not-found
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code") or "")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{description} was not published") from exc
            time.sleep(0.2)


def main() -> int:
    if len(sys.argv) != 5:
        raise RuntimeError(
            "usage: ray_jobs_acceptance_entrypoint.py JOB_URI ATTEMPT_ID SUBMISSION_ID ENVELOPE_SHA256"
        )

    from hub.job_artifacts import read_json_artifact

    job = read_json_artifact(sys.argv[1])
    graph_id = str((job.get("graph") or {}).get("id") or "")
    if not graph_id.startswith(_PREFIX):
        raise RuntimeError("acceptance wrapper refuses a non-acceptance graph")
    mode = graph_id[len(_PREFIX):]

    if mode == "restart":
        # Do not let the driver read until the submitting hub has replaced the catalog pointer. This
        # proves the hash-bound exact generation and its SQL pin, instead of relying on timing luck.
        _wait_for_marker(
            job["job_uri"], "acceptance-source-replaced", "catalog replacement barrier"
        )
        # A second process must first observe this exact submission as RUNNING and construct its recovery
        # runner. This handshake proves live reattachment without relying on container-start timing.
        _wait_for_marker(
            job["job_uri"], "acceptance-recovery-ready", "replacement-hub barrier"
        )
    elif mode == "cancel":
        # The production supervisor must stop this live entrypoint and observe official STOPPED. If stop
        # is broken, the delayed driver eventually runs and the acceptance phase fails as done, not cancelled.
        time.sleep(60)
    elif mode not in ("missing", "corrupt"):
        raise RuntimeError(f"unknown acceptance mode {mode!r}")

    driver = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "dp_ray" / "_driver.py"
    completed = subprocess.run([sys.executable, str(driver), *sys.argv[1:]], check=False)
    if completed.returncode != 0 or mode not in ("missing", "corrupt"):
        return completed.returncode

    # Never let a driver/runtime failure satisfy a negative-result assertion. The fault is injected only
    # after the real driver has produced a valid, hash-bound success receipt.
    result = read_json_artifact(job["result_uri"])
    if (result.get("status") != "done" or result.get("attempt_id") != job.get("attempt_id")
            or result.get("submission_id") != job.get("submission_id")
            or result.get("envelope_sha256") != job.get("envelope_sha256")):
        print("production driver did not produce the expected successful receipt", file=sys.stderr)
        print(json.dumps(result, sort_keys=True, default=str), file=sys.stderr)
        return 70

    bucket, key = _location(job["result_uri"])
    client = _s3_client()
    if mode == "missing":
        client.delete_object(Bucket=bucket, Key=key)
        print("[acceptance] deleted the proven-success result before Ray reported SUCCEEDED", flush=True)
    else:
        client.put_object(Bucket=bucket, Key=key, Body=b'{"corrupt":true}', ContentType="application/json")
        print("[acceptance] replaced the proven-success result with invalid contract bytes", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
