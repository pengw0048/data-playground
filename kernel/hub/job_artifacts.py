"""Small JSON artifacts shared between a control plane and remote execution jobs.

The object-store client is configured exclusively from workload/data-plane environment variables. It
never reads the hub metadata DB, so this module is safe inside a one-shot Ray/Kubernetes job that must not
receive the hub's database identity.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from urllib.parse import urlparse


_OBJECT_SCHEMES = ("s3://", "r2://", "gs://", "gcs://")
JSON_ARTIFACT_MAX_BYTES = 64 * 1024**2
# Keep the recoverable SQL copy comfortably below the object/request cap. The repository's maximum
# 5,000-node/10,000-edge topology with ordinary transforms is about 1.4 MiB; 32 individually max-sized
# code nodes are about 6.4 MiB. Larger valid canvases must be split before durable Jobs submission rather
# than placing a tens-of-megabytes value in one metadata row.
JOB_SQL_ENVELOPE_MAX_BYTES = 8 * 1024**2

RAY_JOB_CONTRACT_VERSION = 3
RAY_JOB_CANONICAL_FIELDS = (
    "contract_version", "run_id", "graph", "target", "source_attempts", "sink_targets",
    "sink_contracts", "materialize_uri", "requires", "code_ref", "cluster_ref",
    "artifact_prefix", "workspace", "data_dir", "entrypoint", "module", "semantic_env",
    "semantic_env_sha256",
)
_RAY_JOB_BINDING_FIELDS = (
    "backend", "submission_id", "attempt_id", "job_uri", "result_uri", "durable", "envelope_sha256",
)
RAY_JOB_ENVELOPE_FIELDS = RAY_JOB_CANONICAL_FIELDS + _RAY_JOB_BINDING_FIELDS
RAY_JOB_RESULT_FIELDS = (
    "contract_version", "attempt_id", "submission_id", "envelope_sha256", "status", "rows", "error",
    "output_uri", "output_table", "outputs",
)


def ray_job_canonical_fields(contract_version: int) -> tuple[str, ...]:
    """Return the exact hash-bound field set for the only supported durable Jobs contract."""
    if contract_version == RAY_JOB_CONTRACT_VERSION:
        return RAY_JOB_CANONICAL_FIELDS
    raise ValueError(f"unsupported Ray job artifact contract_version {contract_version}")


def ray_job_envelope_fields(contract_version: int) -> tuple[str, ...]:
    """Return the exact envelope field set for the only supported durable Jobs contract."""
    if contract_version == RAY_JOB_CONTRACT_VERSION:
        return RAY_JOB_ENVELOPE_FIELDS
    raise ValueError(f"unsupported Ray job artifact contract_version {contract_version}")


class ArtifactNotFound(FileNotFoundError):
    """The storage service authoritatively reports that an artifact key does not exist."""


class ArtifactCorrupt(ValueError):
    """An artifact was readable, but was not a valid JSON object."""


def canonical_json(value: object) -> bytes:
    """Stable JSON bytes used by the Ray Jobs content-binding contract."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def json_artifact_payload(value: object, *, label: str = "JSON artifact") -> bytes:
    """Return canonical bytes within the same bound used by object and SQL artifact copies."""
    payload = canonical_json(value)
    if len(payload) > JSON_ARTIFACT_MAX_BYTES:
        raise ValueError(
            f"{label} exceeds the {JSON_ARTIFACT_MAX_BYTES}-byte limit"
        )
    return payload


def require_exact_object(value: object, fields: Iterable[str], *, label: str) -> dict:
    """Return a JSON object only when its field set is exactly the declared contract."""
    if not isinstance(value, dict):
        raise ArtifactCorrupt(f"{label} must be a JSON object")
    expected = set(fields)
    actual = set(value)
    missing, unknown = sorted(expected - actual), sorted(actual - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ArtifactCorrupt(f"{label} has an invalid field set ({'; '.join(details)})")
    return value


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
    payload = json_artifact_payload(value)
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


def write_json_artifact_once(uri: str, value: dict) -> None:
    """Create a contract artifact once, or accept an idempotent byte-equivalent retry.

    This application-level check is not an object-store conditional write. Production IAM must still
    deny workload overwrite/delete access to job envelopes and deny unrelated principals access to the
    attempt prefix; the independent envelope hash is the execution fence if a key changes after hub read.
    """
    try:
        existing = read_json_artifact(uri)
    except ArtifactNotFound:
        write_json_artifact(uri, value)
        existing = read_json_artifact(uri)
    if canonical_json(existing) != canonical_json(value):
        raise ArtifactCorrupt(f"artifact {uri!r} already contains different contract content")


def read_json_artifact(uri: str) -> dict:
    try:
        if _is_object_uri(uri):
            import pyarrow.fs as pafs

            fs, path = _object_fs(uri)
            info = fs.get_file_info(path)
            if info.type == pafs.FileType.NotFound:
                raise ArtifactNotFound(uri)
            with fs.open_input_file(path) as stream:
                payload = stream.read(JSON_ARTIFACT_MAX_BYTES + 1)
        else:
            with open(_local_path(uri), "rb") as stream:
                payload = stream.read(JSON_ARTIFACT_MAX_BYTES + 1)
    except FileNotFoundError as e:
        raise ArtifactNotFound(uri) from e
    if len(payload) > JSON_ARTIFACT_MAX_BYTES:
        raise ArtifactCorrupt(
            f"artifact {uri!r} exceeds the {JSON_ARTIFACT_MAX_BYTES}-byte limit"
        )
    try:
        value = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ArtifactCorrupt(f"artifact {uri!r} is not valid JSON") from e
    if not isinstance(value, dict):
        raise ArtifactCorrupt(f"artifact {uri!r} must contain a JSON object")
    return value


class JsonArtifactStore:
    def write(self, uri: str, value: dict) -> None:
        write_json_artifact(uri, value)

    def read(self, uri: str) -> dict:
        return read_json_artifact(uri)
