"""Explicit environment profiles for processes that execute caller-controlled code.

Worker processes must not inherit the hub's whole environment: it contains session/bootstrap secrets,
LLM/provider credentials, deployment tokens, and arbitrary operator configuration. Keep this list
small and intentional. Data-engine credentials are an explicit execution capability until
attempt-scoped identities exist; metadata DB access is opt-in only for the long-lived kernel, which
currently owns its lease, heartbeat, and run-state writes.
"""

from __future__ import annotations

import os
import json
import re
from collections.abc import Mapping
from typing import Any

from hub.models import Graph

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

EPHEMERAL_OBJECT_STORE_CRED_ID = "ephemeral-workload-object-store"
_PROMOTED_SIDECAR_KEY = "_promotedTransformDefinitions"
_MAX_PROMOTED_SIDECAR_DEFINITIONS = 512
_MAX_PROMOTED_SIDECAR_BYTES = 8 * 1024 * 1024


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
    """Translate the allowlisted S3 environment into canonical object-store Cred fields.

    One-shot workers intentionally cannot read the hub settings DB. The object-store adapters still use
    normal Cred resolver contract, so copy only this already-allowlisted execution capability into the
    worker's private DB — as *secret references* (``env:DP_S3_KEY`` / ``env:DP_S3_SECRET`` /
    ``env:AWS_SESSION_TOKEN``), never material values. The allowlisted env vars remain the only
    sanctioned channel for resolved credentials to reach the worker. No catalog, identity, auth, or
    other control-plane setting crosses the boundary.
    """
    src = os.environ if source is None else source
    key, secret = src.get("DP_S3_KEY"), src.get("DP_S3_SECRET")
    if bool(key) != bool(secret):
        raise RuntimeError("DP_S3_KEY and DP_S3_SECRET must be set together")
    endpoint = str(src.get("DP_S3_ENDPOINT") or "").strip()
    cfg: dict[str, Any] = {}
    if key and secret:
        # Store references so the worker's private metadata DB never contains the credential bytes.
        cfg.update(accessKeyId="env:DP_S3_KEY", secretAccessKey="env:DP_S3_SECRET")
        if src.get("AWS_SESSION_TOKEN"):
            cfg["sessionToken"] = "env:AWS_SESSION_TOKEN"
    if endpoint:
        cfg["endpoint"] = endpoint
    region = src.get("AWS_REGION") or src.get("AWS_DEFAULT_REGION")
    if region or endpoint:
        cfg["region"] = str(region or "us-east-1")
    return cfg


def initialize_ephemeral_metadata(directory: str) -> str:
    """Give a one-shot worker a private, disposable metadata DB instead of the hub identity.

    Deps constructs the default catalog through metadata tables even when the graph already carries
    physical source URIs. Initializing those tables locally preserves normal engine/plugin composition
    without granting access to users, catalog policy, run state, or credentials in the hub DB. A fixed,
    synthetic object-store Cred is reconstructed from the explicit workload environment and bound as the
    private database's default. It stores references and connection metadata only, keeping DuckDB and
    Arrow aligned without restoring the hub database identity or persisting credential bytes.
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
    cred = metadb.cred_upsert(
        EPHEMERAL_OBJECT_STORE_CRED_ID,
        "Ephemeral workload object store",
        "object_store",
        object_store,
    )
    metadb.set_setting("defaultObjectStoreCredId", cred["id"], "global")
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


def _reachable_promoted_transform_nodes(
        graph: Graph, target: str | None) -> dict[str, tuple[str, str]]:
    """Return node -> canonical exact promoted ref for the dispatched upstream cone."""
    from hub import graph as graph_mod
    from hub.promoted_transforms import (
        PROMOTED_TRANSFORM_ID, promoted_transform_version_number)

    roots = (graph_mod.upstream_chain(graph, target)
             if target is not None else graph_mod.topo_order(graph))
    nodes = graph_mod.execution_nodes(graph, roots)
    result: dict[str, tuple[str, str]] = {}
    for node in nodes:
        data = node.data if isinstance(node.data, dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        transform_id, version = config.get("processor"), config.get("version")
        if (node.type == "transform" and config.get("source") == "library"
                and isinstance(transform_id, str)
                and PROMOTED_TRANSFORM_ID.fullmatch(transform_id)
                and promoted_transform_version_number(version) is not None):
            result[node.id] = (transform_id, str(version))
    return result


def _promoted_definition_snapshot(proc: Any) -> dict:
    from hub.promoted_transforms import promoted_transform_definition

    if proc.provenance != "promoted" or not isinstance(proc.code, str):
        raise RuntimeError("workload promoted Transform definition is unavailable")
    digest, definition = promoted_transform_definition(
        title=proc.title, blurb=proc.blurb, category=proc.category, mode=proc.mode,
        code=proc.code, input_schema=proc.input_schema, output_schema=proc.output_schema,
        requirements=proc.requirements)
    if digest != proc.semantic_digest:
        raise RuntimeError("workload promoted Transform semantic digest is invalid")
    return {
        "id": proc.id,
        "version": proc.version,
        "semanticDigest": digest,
        **definition,
    }


def _attach_promoted_transform_definitions(
        payload: dict, graph: Graph, target: str | None, registry: Any | None) -> None:
    """Attach a bounded parent-resolved execution snapshot to this private workload copy."""
    # This top-level key is not part of Graph and cannot be accepted from the public wire model. Still
    # remove it explicitly before rebuilding the parent-owned snapshot so no caller-provided private
    # material can hitchhike if a dict-like graph is ever admitted at this seam.
    payload.pop(_PROMOTED_SIDECAR_KEY, None)
    refs = sorted(set(_reachable_promoted_transform_nodes(graph, target).values()))
    if not refs:
        return
    if registry is None:
        raise RuntimeError("promoted Transform workload dispatch requires the parent registry")
    if len(refs) > _MAX_PROMOTED_SIDECAR_DEFINITIONS:
        raise RuntimeError(
            f"a workload may reference at most {_MAX_PROMOTED_SIDECAR_DEFINITIONS} "
            "promoted Transform definitions")
    definitions: list[dict] = []
    encoded_bytes = 2  # surrounding JSON array brackets
    for transform_id, version in refs:
        definition = _promoted_definition_snapshot(registry.get(transform_id, version))
        encoded_bytes += len(json.dumps(
            definition, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("utf-8")) + bool(definitions)
        if encoded_bytes > _MAX_PROMOTED_SIDECAR_BYTES:
            raise RuntimeError(
                "promoted Transform workload definitions exceed the 8 MiB transport limit")
        definitions.append(definition)
    payload[_PROMOTED_SIDECAR_KEY] = definitions


def restore_workload_graph(
        payload: Any, target: str | None = None) -> Graph:
    """Validate a private promoted-definition sidecar and restore only the worker's graph copy.

    The returned Graph contains ordinary ad-hoc code for the one-shot execution engine, but the parent
    Graph, persisted Canvas, execution manifest, run state, and history remain exact ``id + version``
    references. There is no latest-version or inline-code fallback: every restored body is hash-checked
    against the complete immutable definition attached by the trusted parent.
    """
    from hub.promoted_transforms import (
        PROMOTED_TRANSFORM_ID, promoted_transform_definition,
        promoted_transform_version_number)

    if not isinstance(payload, dict):
        raise RuntimeError("workload graph is malformed")
    graph_doc = dict(payload)
    raw_definitions = graph_doc.pop(_PROMOTED_SIDECAR_KEY, None)
    graph = Graph.model_validate(graph_doc)
    node_refs = _reachable_promoted_transform_nodes(graph, target)
    expected_refs = set(node_refs.values())
    if not expected_refs:
        if raw_definitions not in (None, []):
            raise RuntimeError("workload promoted Transform definitions do not match the graph")
        return graph
    if not isinstance(raw_definitions, list):
        raise RuntimeError("workload promoted Transform definitions are missing")
    if len(raw_definitions) > _MAX_PROMOTED_SIDECAR_DEFINITIONS:
        raise RuntimeError("workload promoted Transform definition count exceeds the limit")
    encoded = json.dumps(
        raw_definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > _MAX_PROMOTED_SIDECAR_BYTES:
        raise RuntimeError("workload promoted Transform definitions exceed the transport limit")

    required_fields = {
        "id", "version", "semanticDigest", "title", "blurb", "category", "mode", "code",
        "inputSchema", "outputSchema", "requirements",
    }
    definitions: dict[tuple[str, str], dict] = {}
    for raw in raw_definitions:
        if not isinstance(raw, dict) or set(raw) != required_fields:
            raise RuntimeError("workload promoted Transform definition is malformed")
        transform_id, version = raw.get("id"), raw.get("version")
        if (not isinstance(transform_id, str)
                or PROMOTED_TRANSFORM_ID.fullmatch(transform_id) is None
                or promoted_transform_version_number(version) is None):
            raise RuntimeError("workload promoted Transform identity is invalid")
        ref = (transform_id, str(version))
        if ref in definitions:
            raise RuntimeError("workload promoted Transform definition is duplicated")
        try:
            digest, definition = promoted_transform_definition(
                title=raw["title"], blurb=raw["blurb"], category=raw["category"],
                mode=raw["mode"], code=raw["code"], input_schema=raw["inputSchema"],
                output_schema=raw["outputSchema"], requirements=raw["requirements"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("workload promoted Transform definition is malformed") from exc
        if (not isinstance(raw["semanticDigest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", raw["semanticDigest"]) is None
                or digest != raw["semanticDigest"]):
            raise RuntimeError("workload promoted Transform semantic digest is invalid")
        definitions[ref] = {**definition, "semanticDigest": digest}
    if set(definitions) != expected_refs:
        raise RuntimeError("workload promoted Transform definitions do not match the graph")

    declared_requirements = set(graph.requirements)
    by_id = {node.id: node for node in graph.nodes}
    for node_id, ref in node_refs.items():
        definition = definitions[ref]
        missing = sorted(set(definition["requirements"]) - declared_requirements)
        if missing:
            raise RuntimeError(
                f"promoted Transform {ref[0]}@{ref[1]} requires Canvas dependencies: "
                + ", ".join(missing))
        node = by_id[node_id]
        data = node.data if isinstance(node.data, dict) else {}
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        # Rewrite only this disposable worker copy. Removing the library markers is deliberate: the
        # ordinary code path cannot accidentally query latest metadata or fall back to stale client code.
        config.pop("source", None)
        config.pop("processor", None)
        config.pop("version", None)
        config["mode"] = definition["mode"]
        config["code"] = definition["code"]
        config["outputSchema"] = definition["outputSchema"]
        data["config"] = config
        node.data = data
    return graph


def public_workload_graph(payload: Any) -> Graph:
    """Rebuild the public exact-reference Graph from a private workload transport document."""
    if not isinstance(payload, dict):
        raise RuntimeError("workload graph is malformed")
    graph_doc = dict(payload)
    graph_doc.pop(_PROMOTED_SIDECAR_KEY, None)
    return Graph.model_validate(graph_doc)


def prepare_workload_graph(
        graph: Any, target: str | None = None, registry: Any | None = None) -> dict:
    """Serialize a graph for a worker that cannot read hub metadata.

    Named schema contracts are control-plane references. Resolve them in the hub before dispatch and
    carry only their column value into the job, so schema enforcement keeps working without granting the
    worker the metadata identity. Missing references stay unresolved and therefore still fail closed in
    the worker instead of silently disabling enforcement.
    """
    if not isinstance(graph, Graph):
        graph = Graph.model_validate(graph)
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
    _attach_promoted_transform_definitions(payload, graph, target, registry)
    return payload
