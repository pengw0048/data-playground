"""Explicit environment profiles for processes that execute caller-controlled code.

Worker processes must not inherit the hub's whole environment: it contains session/bootstrap secrets,
LLM/provider credentials, deployment tokens, and arbitrary operator configuration.  Keep this list
small and intentional.  Data-engine credentials are still an explicit compatibility capability until
attempt-scoped identities exist; metadata DB access is opt-in only for the long-lived kernel, which
currently owns its lease, heartbeat, and run-state writes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

# Process/runtime plumbing required to execute the same interpreter and native data libraries. These
# are paths and behavior flags, not application/provider credentials.
_HOST_RUNTIME_ENV = frozenset({
    "PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
    "HOME", "USER", "LOGNAME", "SHELL",
    "TMPDIR", "TEMP", "TMP", "TZ",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "PYTHONHASHSEED", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
    "SYSTEMROOT", "WINDIR",
})

# Settings that affect execution semantics or locate already-authorized data/runtime resources. Hub
# control-plane settings (auth, agent/model providers, spawners, public URLs, uploads) are absent.
_WORKLOAD_RUNTIME_ENV = frozenset({
    "DP_AUTH_MODE",
    "DP_WORKSPACE", "DP_DATA_DIR", "DP_DATASET_ROOTS",
    "DP_STORAGE", "DP_STORAGE_URL",
    "DP_MEMORY_LIMIT", "DP_MIN_MEM_PER_THREAD_MB", "DP_SPILL_DIR",
    "DP_PREVIEW_K", "DP_RUN_DEADLINE_S", "DP_REGION_CONCURRENCY",
    "DP_APPEND_COMPACT_PARTS", "DP_PREFLIGHT_FRAGMENTS",
    "DP_CANVAS_PIP_DEPS", "DP_PLUGINS",
    "DP_KERNEL_IDLE_TTL", "DP_KERNEL_ISOLATE_RUNS", "DP_KERNEL_PROBE_TIMEOUT",
    "DP_LOG_LEVEL",
    "DP_RAY_GPUS", "DP_RAY_GPU_TYPE", "DP_RAY_MEM", "DP_RAY_NUM_CPUS", "DP_RAY_LABELS",
    "DP_RAY_REMOTE", "DP_RAY_SHUFFLE_PARTITIONS", "DP_RAY_DRIVER_FALLBACK_MAX_BYTES",
    "DP_RAY_GPU_BATCH_ROWS",
    "RAY_ADDRESS", "RAY_DATA_DEFAULT_SHUFFLE_STRATEGY",
})

# Current compatibility bridge for the data plane. These identities remain broad; replacing them with
# attempt/dataset-scoped SecretRefs is separate architecture work. Listing them explicitly prevents an
# unrelated provider/control credential from hitchhiking merely because it also lives in os.environ.
_DATA_CONNECTION_ENV = frozenset({
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3",
    "GOOGLE_CLOUD_PROJECT",
    "DP_S3_ENDPOINT", "DP_S3_BUCKET",
    "DP_GCS_ENDPOINT",
})

# Rotatable identities and credential-file selectors are deliberately separate from execution
# semantics. Durable jobs snapshot the semantic environment into their hash-bound envelope, while a
# replay receives the operator's current credential values so normal key rotation does not change the
# logical attempt.
_DATA_CREDENTIAL_ENV = frozenset({
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "DP_S3_KEY", "DP_S3_SECRET",
})


def _derived_auth_mode(src: Mapping[str, str], env: dict[str, str]) -> None:
    if str(src.get("DP_AUTH_SECRET") or "").strip() or src.get("DP_AUTH_MODE") == "1":
        env["DP_AUTH_MODE"] = "1"


def build_workload_semantic_env(*, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return non-secret execution settings suitable for a durable, hash-bound snapshot."""
    src = os.environ if source is None else source
    keys = set(_WORKLOAD_RUNTIME_ENV) | set(_DATA_CONNECTION_ENV)
    env = {key: str(src[key]) for key in keys if src.get(key) not in (None, "")}
    _derived_auth_mode(src, env)
    return env


def build_workload_credential_env(*, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return only rotatable data-plane credentials/references for the next workload launch."""
    src = os.environ if source is None else source
    return {key: str(src[key]) for key in _DATA_CREDENTIAL_ENV if src.get(key) not in (None, "")}


def build_workload_env(*, include_metadata_db: bool = False, include_host_runtime: bool = True,
                       source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an allowlisted child environment.

    ``include_metadata_db`` is reserved for the long-lived kernel/pod while it directly owns lease,
    heartbeat, and run-state persistence. One-shot subruns and Ray drivers must leave it false.
    ``source`` is injectable so pod-manifest and regression tests do not mutate global state.
    """
    src = os.environ if source is None else source
    keys = set(_WORKLOAD_RUNTIME_ENV) | set(_DATA_CONNECTION_ENV) | set(_DATA_CREDENTIAL_ENV)
    if include_host_runtime:
        keys.update(_HOST_RUNTIME_ENV)
    if include_metadata_db:
        keys.add("DP_DATABASE_URL")
    env = {key: str(src[key]) for key in keys if src.get(key) not in (None, "")}

    # Auth mode controls filesystem/path confinement, but children never receive material that can sign
    # sessions or bootstrap an administrator. This derived boolean is the only auth value they need.
    _derived_auth_mode(src, env)
    return env


def _object_store_execution_config(source: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Translate the allowlisted legacy S3 environment into the data-plane setting adapters consume.

    One-shot workers intentionally cannot read the hub settings DB. The object-store adapters still use
    the ``objectStore`` setting as their common DuckDB/Arrow configuration contract, so copy only this
    already-allowlisted execution capability into the worker's private DB — as *secret references*
    (``env:DP_S3_KEY`` / ``env:DP_S3_SECRET``), never the material values. The allowlisted env vars
    remain the only sanctioned channel for the resolved credential to reach the worker; adapters resolve
    the references in-process. No catalog, identity, auth, or other control-plane setting crosses the
    boundary.
    """
    src = os.environ if source is None else source
    key, secret = src.get("DP_S3_KEY"), src.get("DP_S3_SECRET")
    if bool(key) != bool(secret):
        raise RuntimeError("DP_S3_KEY and DP_S3_SECRET must be set together")
    endpoint = str(src.get("DP_S3_ENDPOINT") or "").strip()
    if not (endpoint or key):
        return {}
    cfg: dict[str, Any] = {}
    if key and secret:
        # Store references so the worker's private metadata DB never contains the credential bytes.
        cfg.update(accessKeyId="env:DP_S3_KEY", secretAccessKey="env:DP_S3_SECRET")
    if endpoint:
        cfg["endpoint"] = endpoint
        cfg["useSsl"] = not endpoint.lower().startswith("http://")
    region = src.get("AWS_REGION") or src.get("AWS_DEFAULT_REGION")
    if region or endpoint:
        cfg["region"] = str(region or "us-east-1")
    return cfg


def initialize_ephemeral_metadata(directory: str) -> str:
    """Give a one-shot worker a private, disposable metadata DB instead of the hub identity.

    Deps constructs the default catalog through metadata tables even when the graph already carries
    physical source URIs. Initializing those tables locally preserves normal engine/plugin composition
    without granting access to users, catalog policy, run state, or credentials in the hub DB. The only
    setting seeded is object-store execution config reconstructed from the explicit workload environment
    as secret references (``env:DP_S3_KEY`` / ``env:DP_S3_SECRET``); this keeps DuckDB and Arrow adapters
    aligned without restoring the hub database identity or writing credential bytes into the worker DB.
    Prefer calling before importing ``hub.settings``/``hub.deps`` in the child; when those are already
    imported (tests), this also rebinds ``settings.database_url`` and resets the metadb engine.
    """
    os.makedirs(directory, exist_ok=True)
    url = "sqlite:///" + os.path.join(os.path.abspath(directory), "workload-metadata.db")
    os.environ["DP_DATABASE_URL"] = url
    # Marks this process as a one-shot workload with no hub settings DB, so object-store adapters may
    # reconstruct their config from the allowlisted data-plane environment. The hub never sets this.
    os.environ["DP_WORKLOAD_EPHEMERAL"] = "1"
    from hub import metadb
    from hub.settings import settings
    settings.database_url = url
    if metadb._engine is not None:
        metadb._engine.dispose()
    metadb._engine = metadb._Session = None
    metadb.init_db()
    object_store = _object_store_execution_config()
    if object_store:
        metadb.set_setting("objectStore", object_store, "global")
    return url


def is_ephemeral_workload(source: Mapping[str, str] | None = None) -> bool:
    """True inside a one-shot workload process (subrun / Ray driver) with no hub settings DB."""
    src = os.environ if source is None else source
    return src.get("DP_WORKLOAD_EPHEMERAL") == "1"


def data_plane_object_store_config(source: Mapping[str, str] | None = None,
                                   scheme: str | None = None) -> dict[str, Any]:
    """Translate only allowlisted data-plane environment into the private worker metadata shape."""
    src = os.environ if source is None else source
    if (scheme or "").lower() in ("gs", "gcs"):
        endpoint = src.get("DP_GCS_ENDPOINT")
        return ({"endpoint": endpoint, "useSsl": not str(endpoint).lower().startswith("http://")}
                if endpoint else {})
    key = src.get("DP_S3_KEY") or src.get("AWS_ACCESS_KEY_ID")
    secret = src.get("DP_S3_SECRET") or src.get("AWS_SECRET_ACCESS_KEY")
    endpoint = (src.get("DP_S3_ENDPOINT") or src.get("AWS_ENDPOINT_URL_S3")
                or src.get("AWS_ENDPOINT_URL"))
    cfg: dict[str, Any] = {}
    if key and secret:
        cfg["accessKeyId"], cfg["secretAccessKey"] = key, secret
    if src.get("AWS_SESSION_TOKEN"):
        cfg["sessionToken"] = src["AWS_SESSION_TOKEN"]
    if endpoint:
        cfg["endpoint"] = endpoint
        cfg["useSsl"] = not str(endpoint).lower().startswith("http://")
    region = src.get("AWS_REGION") or src.get("AWS_DEFAULT_REGION")
    if region:
        cfg["region"] = region
    return cfg


def prepare_workload_graph(graph: Any) -> dict:
    """Serialize a graph for a worker that cannot read hub metadata.

    Named schema contracts are control-plane references. Resolve them in the hub before dispatch and
    carry only their column value into the job, so schema enforcement keeps working without granting the
    worker the metadata identity. Missing references stay unresolved and therefore still fail closed in
    the worker instead of silently disabling enforcement.
    """
    payload = graph.model_dump()
    from hub import metadb

    for node in payload.get("nodes", []):
        data = node.get("data") if isinstance(node, dict) else None
        config = data.get("config") if isinstance(data, dict) else None
        schema = config.get("outputSchema") if isinstance(config, dict) else None
        if not (isinstance(schema, dict) and schema.get("ref")):
            continue
        contract = metadb.get_schema_contract(str(schema["ref"]), schema.get("version"))
        if contract and contract.get("columns"):
            config["outputSchema"] = contract["columns"]
    return payload
