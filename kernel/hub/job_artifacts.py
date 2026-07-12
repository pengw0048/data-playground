"""Small JSON artifacts shared between a control plane and remote execution jobs.

The object-store client is configured exclusively from workload/data-plane environment variables. It
never reads the hub metadata DB, so this module is safe inside a one-shot Ray/Kubernetes job that must not
receive the hub's database identity.
"""

from __future__ import annotations

import json
import os
import tempfile
from urllib.parse import urlparse


_OBJECT_SCHEMES = ("s3://", "r2://", "gs://", "gcs://")


class ArtifactNotFound(FileNotFoundError):
    """The storage service authoritatively reports that an artifact key does not exist."""


def _is_object_uri(uri: str) -> bool:
    # Keep this module import-safe before a one-shot worker initializes its private metadata DB. Importing
    # hub.plugins.adapters here would import settings/metadb too early and freeze the wrong database URL.
    return uri.startswith(_OBJECT_SCHEMES)


def _local_path(uri: str) -> str:
    parsed = urlparse(uri)
    return parsed.path if parsed.scheme in ("file", "") else uri


def _object_fs(uri: str):
    import pyarrow.fs as pafs

    scheme, _, path = uri.partition("://")
    scheme = scheme.lower()
    if scheme in ("s3", "r2"):
        endpoint = (os.environ.get("DP_S3_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL_S3")
                    or os.environ.get("AWS_ENDPOINT_URL") or "").strip()
        key = os.environ.get("DP_S3_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
        secret = os.environ.get("DP_S3_SECRET") or os.environ.get("AWS_SECRET_ACCESS_KEY")
        kwargs: dict = {}
        if key and secret:
            kwargs["access_key"], kwargs["secret_key"] = key, secret
        if os.environ.get("AWS_SESSION_TOKEN"):
            kwargs["session_token"] = os.environ["AWS_SESSION_TOKEN"]
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region:
            kwargs["region"] = region
        if endpoint:
            parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
            kwargs["endpoint_override"] = parsed.netloc or parsed.path
            kwargs["scheme"] = parsed.scheme or "https"
        return pafs.S3FileSystem(**kwargs), path
    if scheme in ("gs", "gcs"):
        endpoint = os.environ.get("DP_GCS_ENDPOINT", "").strip()
        kwargs = {}
        if endpoint:
            parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
            kwargs["endpoint_override"] = parsed.netloc or parsed.path
            kwargs["scheme"] = parsed.scheme or "https"
        return pafs.GcsFileSystem(**kwargs), path
    raise ValueError(f"job artifacts require s3://, r2://, gs://, or gcs:// storage, got {uri!r}")


def write_json_artifact(uri: str, value: dict) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    if _is_object_uri(uri):
        fs, path = _object_fs(uri)
        # Object stores have a flat key namespace. Opening bucket/prefix/key is sufficient and avoids
        # requiring control-plane CreateBucket/CreateDirectory-style permissions for a data-plane write.
        with fs.open_output_stream(path) as stream:
            stream.write(payload)
        return
    target = _local_path(uri)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".job-artifact-", dir=os.path.dirname(target) or ".")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json_artifact(uri: str) -> dict:
    if _is_object_uri(uri):
        import pyarrow.fs as pafs

        fs, path = _object_fs(uri)
        info = fs.get_file_info(path)
        if info.type == pafs.FileType.NotFound:
            raise ArtifactNotFound(uri)
        with fs.open_input_file(path) as stream:
            return json.loads(stream.read().decode())
    try:
        with open(_local_path(uri), "rb") as stream:
            return json.loads(stream.read().decode())
    except FileNotFoundError as e:
        raise ArtifactNotFound(uri) from e


class JsonArtifactStore:
    def write(self, uri: str, value: dict) -> None:
        write_json_artifact(uri, value)

    def read(self, uri: str) -> dict:
        return read_json_artifact(uri)
