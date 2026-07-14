"""Reference plugin — a **Ray Data execution backend** that runs a canvas on Ray, straight from the
engine-neutral IR (`hub.ir`).

This proves that the IR is a real engine-neutral contract: a SECOND engine (Ray Data, not DuckDB)
executes the graph WITHOUT re-reading node configs or re-implementing lowering. The backend distributes
the clean map-style subset plus explicitly gated grouped aggregate, partitioned window, full-row dedup,
broadcast join, and plain-key sort shapes. Unsupported or semantically uncertain shapes fall back to the
DuckDB engine before Ray dispatch.

Parquet files and shard prefixes claimed by the exact built-in DuckDB adapter are read by Ray workers
directly only after fragment, physical-footer union, adapter-metadata schema, and typed exact-root Hive
layout proof.
Simple overwrite Parquet sinks are written by workers to an immutable attempt prefix and published only
after a success manifest lands. Exact built-in lazy formats can use a batch-streamed compatibility path
only when their driver transfer fits `DP_RAY_DRIVER_FALLBACK_MAX_BYTES` (64 MiB by default). Object IPC
and plugin adapters without an explicit Ray capability fall back before dispatch; append, partitioned,
non-Parquet, and custom sinks keep shared sink semantics under the same byte bound. Broadcast joins use
the bound for the right side. This prevents a Ray selection from becoming an unbounded driver OOM path
while preserving small compatibility workloads.

EXECUTION MODEL — an isolated subprocess driver. Running Ray inline in the kernel process deadlocks: the
source read / sink write go through a DuckDB base connection, and a materialization on the hub's
pre-existing connection wedges once `ray.init()` has run in the same process. So `run()` spawns a fresh
subprocess (`_driver.py`) whose OWN process holds its DuckDB + Ray (`ray.init` before any DuckDB). The hub
resolves logical destinations before dispatch and owns catalog registration after the driver returns;
the driver receives physical sink URIs and never reads that control-plane state. This is the same
process-isolation boundary the built-in SubprocessRunner uses. With no `DP_RAY_JOBS_ADDRESS`, development
retains that local Popen driver. When a Jobs address is configured, the same driver runs as an official
Ray Job with a deterministic submission ID, immutable input/result artifacts in shared object storage,
and a durable SQL backend binding. Replacement hubs reattach through `JobSubmissionClient`; cancellation
waits for an acknowledged stop and one publication lease owner commits catalog/history effects. The
remote driver receives data-plane credentials only, never the hub metadata DB identity. See
`docs/RAY_JOBS.md` for the production contract and tradeoffs.

The `uv` fix. If the kernel is launched via `uv run` (common), Ray's default behavior
(`RAY_ENABLE_UV_RUN_RUNTIME_ENV`) re-launches its WORKERS through `uv` too — which builds a fresh,
ray-less `.venv`, so workers can't `import ray`, the raylet dies, and the run hangs. The driver sets
`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (before `import ray`) so workers use its own interpreter (which has
ray); `_supervise` also strips uv/`VIRTUAL_ENV` markers and runs the child off the repo's pyproject.
With that, the live differential (`test_ray_backend_live_differential`) passes on macOS AND Linux — it's
opt-in only because it needs the `[ray]` extra + is slow. `DP_RAY_NUM_CPUS` optionally caps the worker
pool. The Part B mechanism (a plugin node's `ir` hook → clean op → routed here) is also covered
cluster-free by `test_plugin_node_ir_hook_runs_on_duckdb_and_ray`.

Opt-in: `uv pip install -e 'kernel[ray]'`, drop this folder in `<workspace>/plugins/`, and select it
via Settings → Execution or `DP_EXECUTION=ray-data`. It never becomes the default (the kernel is), so a
small graph won't spin up Ray unless you ask.
"""

from __future__ import annotations

import atexit
import contextlib
import glob
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import sys
import threading
import time
import uuid
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit

from hub import db, graph as g
from hub.backends import DurableCatalogPublisher
from hub.handoff import (allocate_attempt, attempt_has_commit_record, attempt_has_contents,
                         discard_attempt, has_attempt_path_component, is_attempt_uri,
                         lookup_attempt, read_manifest, validate_shards, write_manifest)
from hub.sinks import SinkSpec, commit_sink, expected_sink_uri, preflight_sink
from hub.sqlanalyze import agg_has_order_sensitive, window_needs_order  # AST (DuckDB's own parser), shared
from hub.ir import (CLEAN_OPS, CLEAN_TRANSFORM_MODES, lower_to_ir, parse_group_keys, parse_sort_keys,
                    plan_is_clean, plan_is_distributable)
from hub.job_artifacts import (RAY_JOB_CONTRACT_VERSION, RAY_JOB_RESULT_FIELDS, ArtifactCorrupt,
                               ArtifactNotFound, JsonArtifactStore, canonical_json,
                               json_artifact_payload, ray_job_canonical_fields,
                               ray_job_envelope_fields, require_exact_object)
from hub.models import (CatalogPublicationReceipt, PerNodeStatus, ResourceSpec, RunBackendRef,
                        RunStatus, WorkerInfo)
from hub.placement import node_requires, satisfies
from hub.sqlpolicy import (
    FragmentKind,
    SQLPolicyError,
    identifier,
    quote_identifier,
    validate_fragment,
    validate_identifier_alias,
)
from hub.workload_env import (build_workload_credential_env, build_workload_env,
                              build_workload_semantic_env, prepare_workload_graph)

log = logging.getLogger("hub")

# the relational ops THIS backend claims beyond the map-style clean subset (ARC3). The engine does NOT
# reimplement these on Ray operators — it lets RAY do only the SHUFFLE (hash-partition rows by the op's
# key) and lets DUCKDB do the compute on each COMPLETE partition, running the SAME SQL the single-node
# engine runs. So the result is byte-identical BY CONSTRUCTION (it's DuckDB, on partitions holding every
# row of their key-groups — nothing combined across partitions), MOST DuckDB aggregates work, the output
# carries DuckDB's exact schema, and the only thing parsed is the shuffle KEY (bare columns). `aggregate`
# = a GROUPED aggregate; a global aggregate (no key) is cheap + falls back to the single-node engine. An
# ORDER-SENSITIVE aggregate (list/string_agg/first/last/any_value/arg_max/…) depends on intra-group row
# order, which the hash-shuffle does not preserve, so it falls back — detected by name via the shared
# AST analyzer (hub.sqlanalyze.agg_has_order_sensitive), conservatively including an ORDER-BY'd form like
# `list(x ORDER BY x)` (DuckDB rewrites the ORDER BY out of the parsed AST, so we can't prove it safe).
# `window` = a PARTITION BY window (shuffle by the
# partition key → DuckDB window per complete partition); requires a non-empty ORDER BY (a no-ORDER-BY
# window like row_number is intra-partition-order-dependent → falls back), and is exact up to ORDER BY
# ties (the same inherent tie-ceiling as sort — single-node is itself unstable there). `dedup` = full-row
# DISTINCT (shuffle by ALL columns → DuckDB DISTINCT; identical rows colocate). A keyed DISTINCT ON keeps
# the first row in an arbitrary order (non-deterministic even single-node) → needs an explicit order key,
# so it falls back; and a dedup whose schema has any FLOAT/DOUBLE column falls back too, because the
# shuffle's raw-byte equality distinguishes -0.0/0.0 and NaN payloads that DuckDB DISTINCT coalesces.
# `join` = a BROADCAST join: collect the RIGHT side to the driver + broadcast it, then DuckDB-join each
# LEFT block against the full right per worker (the SAME join_sql the single-node engine uses → identical
# output). inner/left/cross are correct block-by-block; right/full (need unmatched-right rows) fall back,
# and — like Spark's broadcast hint — the right is assumed small enough to broadcast (a large-large join
# should not pin engine=ray).
# `sort` = a native Ray range-shuffle sort on plain-column keys, then repartition(1) so the ordered output
# is a SINGLE file (matching the single-node engine's single ordered writer — a sharded read wouldn't
# preserve global order). FAITHFULNESS is exact only for a TOTAL (unique) key: the sequence then equals
# DuckDB's, incl. NULL placement (both NULLS LAST on DuckDB 1.5.x). For a NON-unique key, ties are
# unstable in BOTH engines → correctly sorted but tie-order may differ from single-node (not
# byte-identical); a float/double DESC with NaN also differs (Ray puts NaN last, DuckDB treats it as
# largest). SCALE: repartition(1) gathers the whole sorted set onto ONE worker — fine for this reference
# backend, but a sort exceeding one node's memory should not pin engine=ray (a production backend would
# write ordered shards + stitch on read).
RAY_RELATIONAL = frozenset({"aggregate", "window", "dedup", "join", "sort"})
_DRIVER_FALLBACK_DEFAULT_BYTES = 64 << 20
_DRIVER_FALLBACK_BATCH_ROWS = 1024
_DRIVER_FALLBACK_MAX_BATCHES = 20_000
_PARQUET_FRAGMENT_LIMIT = 10_000
_PARQUET_EXTENSIONS = (".parquet", ".pq")
_HIVE_DEFAULT_PARTITION = "__HIVE_DEFAULT_PARTITION__"
_JOBS_BACKEND = "ray-jobs"
_JOB_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})
_RESULT_RECONCILIATION_STATES = frozenset({
    "result_fencing", "result_submitted", "result_stop_fenced",
})
_CANCEL_FENCE_ENTRYPOINT = "sleep 86400"
_CONTROL_OBSERVATION_WRITE_S = 10.0
_JOBS_CLIENT_ENV_LOCK = threading.Lock()
_GPU_BATCH_ROWS_DEFAULT = 4096
_GPU_BATCH_ROWS_MAX = 65536
_JOB_CONTRACT_VERSION = RAY_JOB_CONTRACT_VERSION
_JOB_RESULT_FIELDS = RAY_JOB_RESULT_FIELDS


class ArtifactContractError(RuntimeError):
    """A readable shared artifact does not match its immutable SQL/backend binding."""


class JobsConfigurationDrift(RuntimeError):
    """The current operator configuration points at a different durable execution namespace."""


class JobsConfigurationUnavailable(RuntimeError):
    """A replacement process lacks the local production contract needed to reattach safely."""


class TerminalResultMissing(RuntimeError):
    """Ray succeeded but no result object appeared during the authoritative-not-found grace period."""


class DurableTerminalObserved(RuntimeError):
    """A permanent terminal run fence replaced retention-pruned backend detail."""


class PublicationEffectsWon(RuntimeError):
    """A terminal publisher crossed the write-ahead barrier before remote control did."""


def _float_env(name: str, default: float, minimum: float = 0.01) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _gpu_batch_rows() -> int:
    """Validated finite GPU batch size; large operator values are capped to bound task memory."""
    raw = os.environ.get("DP_RAY_GPU_BATCH_ROWS", "").strip()
    if not raw:
        return _GPU_BATCH_ROWS_DEFAULT
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError("DP_RAY_GPU_BATCH_ROWS must be a positive integer") from e
    if value <= 0:
        raise ValueError("DP_RAY_GPU_BATCH_ROWS must be a positive integer")
    return min(value, _GPU_BATCH_ROWS_MAX)


def _canonical_accelerator_type(value: object) -> str:
    """Match Ray's canonical ``accelerator_type:<TYPE>`` resource names for operator input."""
    return str(value or "").strip().upper()


def _job_status_name(value) -> str:
    """Normalize Ray's JobStatus enum and lightweight fake-client strings."""
    raw = getattr(value, "value", value)
    return str(raw or "").rsplit(".", 1)[-1].upper()


def _job_attempt_id(job: dict) -> str:
    try:
        fields = ray_job_canonical_fields(int(job.get("contract_version") or 0))
    except (TypeError, ValueError) as e:
        raise ArtifactContractError(str(e)) from e
    missing = [key for key in fields if key not in job]
    if missing:
        raise ArtifactContractError(f"Ray job artifact is missing canonical fields: {', '.join(missing)}")
    canonical = {key: job[key] for key in fields}
    return hashlib.sha256(canonical_json(canonical)).hexdigest()[:24]


def _job_envelope_sha256(job: dict) -> str:
    try:
        fields = ray_job_envelope_fields(int(job.get("contract_version") or 0))
    except (TypeError, ValueError) as e:
        raise ArtifactContractError(str(e)) from e
    envelope = {key: job[key] for key in fields if key != "envelope_sha256"}
    return hashlib.sha256(canonical_json(envelope)).hexdigest()


def _semantic_env_sha256(env: dict[str, str]) -> str:
    return hashlib.sha256(canonical_json(env)).hexdigest()


def _jobs_submission_id(run_id: str, attempt_id: str) -> str:
    safe_run = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-")[-32:] or "run"
    return f"dp-{safe_run}-{attempt_id[:12]}"


def _secure_duckdb_connection():
    """A worker/metadata connection with the same fail-closed session and lazy-bind snapshot fence."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    con.execute("SET python_enable_replacements = false")
    con.execute("SET search_path = 'main'")
    con.execute("BEGIN TRANSACTION")
    return con


def _validate_policy_fragments(con, fragments) -> None:
    for kind, text in fragments or ():
        validate_fragment(FragmentKind(kind), text, con=con)


def _driver_fallback_limit() -> int:
    raw = os.environ.get("DP_RAY_DRIVER_FALLBACK_MAX_BYTES", str(_DRIVER_FALLBACK_DEFAULT_BYTES))
    try:
        limit = int(raw)
    except ValueError as exc:
        raise RuntimeError("DP_RAY_DRIVER_FALLBACK_MAX_BYTES must be an integer byte count") from exc
    if limit < 0:
        raise RuntimeError("DP_RAY_DRIVER_FALLBACK_MAX_BYTES must be >= 0")
    return limit


def _require_driver_fallback(size: int | None, purpose: str) -> int:
    """Authorize one compatibility collect only when its byte size is known and bounded."""
    limit = _driver_fallback_limit()
    guidance = (
        "Use Parquet on shared/object storage for worker-direct I/O, run this shape on the local backend, "
        "or raise DP_RAY_DRIVER_FALLBACK_MAX_BYTES only after sizing driver memory."
    )
    if size is None or size < 0:
        raise RuntimeError(
            f"{purpose} requires a driver compatibility collect, but its byte size is unknown. {guidance}"
        )
    if size > limit:
        raise RuntimeError(
            f"{purpose} requires a {size}-byte driver compatibility collect, above the {limit}-byte limit. "
            f"{guidance}"
        )
    return size


def _remote_ray() -> bool:
    return os.environ.get("DP_RAY_REMOTE", "").strip().lower() in ("1", "true", "yes", "on")


def _is_builtin_adapter(adapter: object) -> bool:
    """Native filesystem paths must never bypass plugin adapter semantics."""
    from hub.plugins.adapters import DuckDBAdapter

    return type(adapter) is DuckDBAdapter


def _filesystem_path(uri: str):
    import pyarrow.fs as pafs

    from hub.plugins.adapters import is_object_uri, object_fs, path_of

    return object_fs(uri) if is_object_uri(uri) else (pafs.LocalFileSystem(), path_of(uri))


def _file_infos(uri: str) -> tuple[object, str, object, list[object]]:
    """Return filesystem metadata with a hard file-count contract for driver-side processing.

    PyArrow's filesystem call can still materialize a provider listing before returning. The count cap
    bounds footer reads and all subsequent metadata retained by this process; deployments should compact
    prefixes well below it (and use provider inventory/metrics for truly enormous prefixes).
    """
    import pyarrow.fs as pafs

    fs, path = _filesystem_path(uri)
    info = fs.get_file_info(path)
    if info.type == pafs.FileType.File:
        return fs, path, info, [info]
    if info.type != pafs.FileType.Directory:
        return fs, path, info, []
    infos = fs.get_file_info(pafs.FileSelector(path.rstrip("/"), recursive=True, allow_not_found=True))
    files = [item for item in infos if item.type == pafs.FileType.File]
    if len(files) > _PARQUET_FRAGMENT_LIMIT:
        raise RuntimeError(
            f"source '{uri}' contains more than {_PARQUET_FRAGMENT_LIMIT:,} files; compact the dataset "
            "or run it on the local backend"
        )
    return fs, path.rstrip("/"), info, files


def _physical_source_bytes(uri: str) -> int | None:
    """Best-effort stored byte size before opening a source through an adapter."""
    limit = _driver_fallback_limit()
    try:
        _fs, _path, _info, files = _file_infos(uri)
        if not files:
            return None
        total = 0
        for info in files:
            total += max(0, int(info.size))
            if total > limit:
                return total
        return total
    except Exception:  # noqa: BLE001 — unknown size fails closed at the caller
        return None


def _native_parquet_fragments(uri: str, root: str, root_info, files: list[object]) -> list[str]:
    """Select exactly the files ``DuckDBAdapter`` would read for this URI shape.

    An explicit file is exact. An object prefix uses the adapter's current lowercase ``*.parquet``
    contract. A local directory first probes lowercase ``.parquet``, then ``.pq``; after that probe,
    DuckDB's glob includes hidden files/directories of the winning extension. Python's case-sensitive
    patterns match DuckDB on the supported Linux production filesystem and the empirically verified
    macOS development path.
    """
    import pyarrow.fs as pafs

    from hub.plugins.adapters import is_object_uri

    if is_object_uri(uri):
        # DuckDBAdapter identifies an exact object by the URI suffix before it ever stats the key.
        if uri.lower().endswith(_PARQUET_EXTENSIONS):
            return [root] if root_info.type == pafs.FileType.File else []
        if root_info.type != pafs.FileType.Directory:
            return []
        return sorted(info.path for info in files if info.path.endswith(".parquet"))
    if root_info.type == pafs.FileType.File:
        return [root]
    if root_info.type != pafs.FileType.Directory:
        return []
    for extension in _PARQUET_EXTENSIONS:
        pattern = os.path.join(root, "**", f"*{extension}")
        if not glob.glob(pattern, recursive=True):
            continue
        selected = set(glob.glob(pattern, recursive=True, include_hidden=True))
        return sorted(info.path for info in files if info.path in selected)
    return []


def _bounded_builtin_source_supported(uri: str) -> bool:
    """Formats whose exact built-in adapter scan is lazy enough to stream under a byte ceiling."""
    from hub.plugins.adapters import is_object_uri, path_of

    low = uri.split("?", 1)[0].rstrip("/").lower()
    if is_object_uri(uri):
        # DuckDBAdapter eagerly downloads object IPC before it can expose a reader. Never invoke it in a
        # Ray driver. CSV/JSON and Parquet are lazy in DuckDB; a suffix-less URI is its Parquet-prefix form.
        if low.endswith((".arrow", ".feather", ".ipc")):
            return False
        return not low.endswith((".lance", ".delta", ".iceberg"))
    path = path_of(uri)
    return os.path.isdir(path) or low.endswith((
        ".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson", ".arrow", ".feather", ".ipc",
    ))


def _bounded_adapter_source(uri: str, adapter: object, ray):
    """Stream a proven-small built-in source into Ray without concatenating batches on the driver."""
    import pyarrow as pa

    if not _is_builtin_adapter(adapter) or not _bounded_builtin_source_supported(uri):
        raise RuntimeError(
            f"source '{uri}' has no bounded Ray driver-streaming contract; run it on the local backend "
            "or provide a native distributed connector"
        )
    _require_driver_fallback(_physical_source_bytes(uri), f"source '{uri}'")
    refs = []
    decoded_bytes = 0
    with db.base_guard():
        relation = adapter.scan(uri)
        reader = relation.to_arrow_reader(_DRIVER_FALLBACK_BATCH_ROWS)
        schema = reader.schema
        for index, batch in enumerate(reader, start=1):
            if index > _DRIVER_FALLBACK_MAX_BATCHES:
                raise RuntimeError(
                    f"source '{uri}' produced more than {_DRIVER_FALLBACK_MAX_BATCHES:,} driver batches; "
                    "compact it or use a native distributed connector"
                )
            decoded_bytes += int(batch.nbytes)
            _require_driver_fallback(decoded_bytes, f"source '{uri}' after decoding")
            # Each object is transferred to Ray immediately. The driver retains only bounded ObjectRef
            # metadata and never constructs one all-source Arrow table.
            refs.append(ray.put(pa.Table.from_batches([batch], schema=schema)))
    if not refs:
        refs.append(ray.put(pa.Table.from_batches([], schema=schema)))
    return _remember_ray_schema(ray.data.from_arrow_refs(refs), schema)


def _native_parquet_plan(uri: str, adapter: object) -> dict | None:
    """Prove a built-in Parquet dataset's fragments, physical schema, and partition boundary.

    Footer schemas are unified explicitly because Ray 2.56 otherwise infers from one fragment. Hive
    parsing is rooted at the dataset itself so an ancestor such as ``tenant=acme`` cannot become a data
    column, while genuine immediate ``key=value`` partitions remain visible.
    """
    if not _is_builtin_adapter(adapter):
        return None
    try:
        import pyarrow as pa
        import pyarrow.fs as pafs
        import pyarrow.parquet as pq

        fs, root, root_info, files = _file_infos(uri)
        fragments = _native_parquet_fragments(uri, root, root_info, files)
        if not fragments:
            return None
        if len(fragments) > _PARQUET_FRAGMENT_LIMIT:
            raise RuntimeError(
                f"source '{uri}' contains more than {_PARQUET_FRAGMENT_LIMIT:,} Parquet fragments; "
                "compact the dataset or run it on the local backend"
            )
        is_file = root_info.type == pafs.FileType.File
        base_dir = posixpath.dirname(root) if is_file else root.rstrip("/")
        parents: list[tuple[str, ...]] = []
        for fragment in fragments:
            relative = posixpath.relpath(fragment, base_dir or ".")
            if relative == ".." or relative.startswith("../"):
                raise RuntimeError(f"source '{uri}' has a fragment outside its dataset root")
            parent = posixpath.dirname(relative)
            parents.append(tuple(p for p in parent.split("/") if p and p != "."))
        nonempty = [parts for parts in parents if parts]
        partition_keys: tuple[str, ...] = ()
        if nonempty:
            if len(nonempty) != len(parents):
                raise RuntimeError(f"source '{uri}' mixes root files and partition directories")
            key_rows = []
            for parts in parents:
                keys = []
                for component in parts:
                    key, sep, value = component.partition("=")
                    key, value = unquote(key), unquote(value)
                    if not sep or not key or not value:
                        raise RuntimeError(
                            f"source '{uri}' has a non-Hive fragment layout at '{component}'"
                        )
                    if value == _HIVE_DEFAULT_PARTITION:
                        raise RuntimeError(
                            f"source '{uri}' uses the Hive default-partition sentinel; "
                            "Ray cannot prove DuckDB NULL partition parity"
                        )
                    keys.append(key)
                if len(set(keys)) != len(keys):
                    raise RuntimeError(f"source '{uri}' repeats a Hive partition key in one path")
                key_rows.append(tuple(keys))
            partition_keys = key_rows[0]
            if any(keys != partition_keys for keys in key_rows[1:]):
                raise RuntimeError(f"source '{uri}' has inconsistent Hive partition keys")
            # DuckDB receives an absolute/globbed path and parses Hive-looking components above the
            # requested root too. Ray is deliberately rooted at `base_dir`, so a genuine partitioned
            # dataset nested below any `key=value` component would have different logical columns. Reject
            # before asking the adapter for metadata; the oracle itself would already contain the leak.
            def _hive_like(component: str) -> bool:
                key, sep, value = component.partition("=")
                return bool(sep and unquote(key) and unquote(value))

            if any(_hive_like(component) for component in root.rstrip("/").split("/") if component):
                raise RuntimeError(
                    f"source '{uri}' is a Hive dataset below a Hive-looking root/ancestor; "
                    "use bounded/local adapter semantics"
                )
        schemas = [pq.read_schema(fragment, filesystem=fs) for fragment in fragments]
        unified = pa.unify_schemas(schemas, promote_options="permissive")
        if set(partition_keys) & set(unified.names):
            raise RuntimeError(f"source '{uri}' stores a Hive partition key in both paths and file data")
        # The exact built-in adapter is the semantic oracle. DuckDB decides partition-column presence,
        # ordering, and types; Ray may run native only when its physical union plus typed partitions can
        # reproduce that metadata exactly without reading rows into the driver.
        if db.is_run_scoped():
            oracle = adapter.scan(uri, limit=0).to_arrow_table().schema
        else:
            # Remote listing/footer work can block for seconds. Execute it on a thread-confined cursor so
            # only cursor creation briefly takes the base lock; unrelated previews/runs remain available.
            with db.run_scope():
                oracle = adapter.scan(uri, limit=0).to_arrow_table().schema
        if oracle.names[:len(unified.names)] != unified.names:
            raise RuntimeError(f"source '{uri}' adapter physical-column order differs from footer union")
        for field in unified:
            if oracle.field(field.name).type != field.type:
                raise RuntimeError(
                    f"source '{uri}' adapter type for '{field.name}' differs from footer union"
                )
        partition_fields = [oracle.field(name) for name in oracle.names[len(unified.names):]]
        if tuple(field.name for field in partition_fields) != partition_keys:
            raise RuntimeError(
                f"source '{uri}' adapter Hive column order differs from its exact-root layout"
            )
        partition_types: dict[str, type] = {}
        supported = {
            pa.int64(): int,
            pa.string(): str,
        }
        for field in partition_fields:
            python_type = supported.get(field.type)
            if python_type is None:
                raise RuntimeError(
                    f"source '{uri}' Hive column '{field.name}' has unsupported Ray 2.56 partition "
                    f"type {field.type}"
                )
            partition_types[field.name] = python_type
        logical = pa.schema([*unified, *partition_fields], metadata=oracle.metadata)
        if logical.names != oracle.names or any(
                logical.field(name).type != oracle.field(name).type for name in logical.names):
            raise RuntimeError(f"source '{uri}' logical schema differs from the adapter metadata oracle")
        return {
            "paths": fragments,
            "filesystem": fs,
            "schema": logical,
            "base_dir": base_dir,
            "partition_keys": partition_keys,
            "partition_types": partition_types,
        }
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 — any listing/footer uncertainty disables native execution
        raise RuntimeError(
            f"native Parquet proof unavailable (code=parquet_proof_unavailable,type={type(exc).__name__})"
        ) from exc


def _read_native_parquet(ray, plan: dict, ray_opts: dict | None = None):
    from ray.data.datasource import Partitioning

    dataset = ray.data.read_parquet(
        plan["paths"], filesystem=plan["filesystem"], schema=plan["schema"],
        columns=plan["schema"].names,
        partitioning=Partitioning(
            "hive", base_dir=plan["base_dir"], field_types=plan["partition_types"],
            filesystem=plan["filesystem"]
        ),
        ray_remote_args=ray_opts or None,
    )
    return _remember_ray_schema(dataset, plan["schema"])


_ATTEMPT_COMPONENT_MAX_BYTES = 240
_OBJECT_ATTEMPT_KEY_MAX_BYTES = 896  # reserve 128 bytes for a shard or commit-record child path
_ATTEMPT_MIN_SLUG_BYTES = 8


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Return the longest valid UTF-8 prefix within ``max_bytes``."""
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _attempt_component(base_name: str, readable: str, digest: str, limit: int, uri: str) -> str:
    marker = ".attempt-"
    digest_suffix = f"-{digest}"
    floor = _utf8_prefix(readable, min(_ATTEMPT_MIN_SLUG_BYTES, len(readable.encode("utf-8"))))
    floor = floor.rstrip("._-") or "a"
    fixed_bytes = len((marker + digest_suffix).encode("utf-8"))
    if limit < fixed_bytes + len(floor.encode("utf-8")):
        raise RuntimeError(
            f"Ray output URI '{uri}' leaves only {max(0, limit)} bytes for an immutable attempt name; "
            "shorten its parent path"
        )
    base, slug = base_name, readable
    if len((base + marker + slug + digest_suffix).encode("utf-8")) > limit:
        slug_budget = limit - fixed_bytes - len(base.encode("utf-8"))
        slug = (
            _utf8_prefix(readable, slug_budget).rstrip("._-")
            if slug_budget >= len(floor.encode("utf-8")) else floor
        )
        slug = slug or floor
    if len((base + marker + slug + digest_suffix).encode("utf-8")) > limit:
        base_budget = limit - fixed_bytes - len(slug.encode("utf-8"))
        base = _utf8_prefix(base_name, base_budget)
    component = base + marker + slug + digest_suffix
    if len(component.encode("utf-8")) > limit:  # defensive: never return a prefix the writer cannot create
        raise RuntimeError(f"Ray output URI '{uri}' cannot fit a bounded immutable attempt name")
    return component


def _attempt_handoff_uri_for_owner(
        uri: str, run_id: str, scope: str | None, owner: str | None, *,
        namespace: str | None = None, generation: int | None = None,
        attempt_id: str | None = None) -> str:
    """Return an immutable region-output prefix for one execution attempt.

    The controller suggests a stable, content-addressed URI. Writing a multi-object Ray result directly
    to that prefix lets a retry race a still-running/failed attempt and expose a mixture of shards. Keep
    the stable URI as the cache key, but publish a unique physical prefix only after the attempt succeeds.
    """
    low = uri.lower()
    extension = next((ext for ext in (".parquet", ".pq") if low.endswith(ext)), "")
    base = uri[:-len(extension)] if extension else uri.rstrip("/")
    raw = (f"{namespace}-g{generation}-{attempt_id}" if namespace and generation and attempt_id
           else str(run_id))
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-") or "attempt"
    readable = readable[:64].rstrip("._-") or "attempt"
    # Hash the complete, unmodified logical URI before stripping its extension. `out.parquet` and
    # `out.pq` otherwise share one physical base. A whole-graph write also scopes by step ID so fan-out
    # sinks in one run can never reattach each other. Canonical JSON prevents delimiter ambiguity.
    identity_doc = {
        "runId": raw,
        "scope": None if scope is None else str(scope),
        "uri": str(uri),
    }
    if namespace is not None:
        identity_doc.update({
            "namespace": namespace, "generation": generation, "attemptId": attempt_id,
        })
    if owner:
        identity_doc["owner"] = owner
    identity = json.dumps(identity_doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    parsed = urlsplit(base)
    if parsed.scheme.lower() in ("s3", "gs", "gcs", "r2") and parsed.netloc:
        parent_path, separator, base_name = parsed.path.rpartition("/")
        if not separator or not base_name:
            raise RuntimeError(f"Ray object output URI '{uri}' must include an object key")
        parent_key = parent_path.lstrip("/")
        parent_bytes = len(parent_key.encode("utf-8")) + (1 if parent_key else 0)
        component_limit = min(_ATTEMPT_COMPONENT_MAX_BYTES, _OBJECT_ATTEMPT_KEY_MAX_BYTES - parent_bytes)
        component = _attempt_component(base_name, readable, digest, component_limit, uri)
        path = f"{parent_path}/{component}" if parent_path else f"/{component}"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
    parent, separator, base_name = base.rpartition("/")
    component = _attempt_component(
        base_name if separator else base, readable, digest, _ATTEMPT_COMPONENT_MAX_BYTES, uri
    )
    return f"{parent}/{component}" if separator else component


def _attempt_handoff_uri(uri: str, run_id: str, scope: str | None = None, *,
                         namespace: str | None = None, generation: int | None = None,
                         attempt_id: str | None = None) -> str:
    """Return the current installation-owned or allocated immutable output prefix."""
    from hub.plugins.adapters import is_object_uri

    owner = None
    if is_object_uri(uri) and namespace is None:
        from hub import metadb
        owner = metadb.object_attempt_owner_id()
    return _attempt_handoff_uri_for_owner(
        uri, run_id, scope, owner, namespace=namespace,
        generation=generation, attempt_id=attempt_id,
    )


def _attempt_allocation_key(uri: str, run_id: str, kind: str,
                            scope: str | None = None) -> str:
    """Return the stable registry key shared by allocation and read-only recovery."""
    logical_hash = hashlib.sha256(str(uri).encode()).hexdigest()
    return json.dumps({
        "kind": kind, "runId": str(run_id), "scope": scope, "logical": logical_hash,
    }, sort_keys=True, separators=(",", ":"))


def _allocate_handoff_uri(uri: str, run_id: str, kind: str,
                          scope: str | None = None,
                          catalog_key_base: str | None = None,
                          require_live_preallocation: bool = False) -> str:
    """Use durable allocation for object writes and a process-private path for local writes."""
    from hub.plugins.adapters import is_object_uri
    if not is_object_uri(uri):
        return _attempt_handoff_uri(uri, run_id, scope=scope)
    allocation_key = _attempt_allocation_key(uri, run_id, kind, scope)
    if kind == "region":
        prior = lookup_attempt(
            logical_uri=uri, kind=kind, run_id=run_id, allocation_key=allocation_key)
        if prior is not None and prior["state"] in ("committed", "published"):
            return prior["uri"]
    handle = allocate_attempt(
        logical_uri=uri, kind=kind, run_id=run_id, allocation_key=allocation_key,
        catalog_key_base=catalog_key_base,
        require_live_preallocation=require_live_preallocation,
        uri_factory=lambda storage_namespace, allocated_generation, allocated_id:
        _attempt_handoff_uri(
            uri, run_id, scope=scope, namespace=storage_namespace,
            generation=allocated_generation, attempt_id=allocated_id),
    )
    return handle["uri"]


def _worker_direct_parquet_sink(spec: SinkSpec, uri: str, adapter: object) -> bool:
    """True only for sink shapes Ray can publish without changing adapter semantics."""
    from urllib.parse import urlparse

    from hub.plugins.adapters import is_object_uri

    scheme = urlparse(uri).scheme.lower()
    filesystem_uri = is_object_uri(uri) or scheme in ("", "file")
    return (
        _is_builtin_adapter(adapter) and filesystem_uri
        and spec.mode == "overwrite" and not spec.partition_by
        and spec.extension.lower() in (".parquet", ".pq")
    )


def _write_handoff_manifest(uri: str, *, run_id: str, rows: int, schema: object) -> None:
    """Write the commit marker last; the controller publishes ``uri`` only after this returns."""
    write_manifest(uri, run_id=run_id, rows=rows, schema=schema)


def _write_empty_parquet(uri: str, schema: object) -> None:
    """Publish one typed empty shard when Ray has no blocks to write."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from hub.plugins.adapters import is_object_uri, object_fs, path_of

    arrow_schema = getattr(schema, "base_schema", schema)
    if not isinstance(arrow_schema, pa.Schema):
        raise RuntimeError("an empty Ray result did not expose an Arrow schema")
    table = pa.Table.from_batches([], schema=arrow_schema)
    if is_object_uri(uri):
        fs, path = object_fs(uri)
        with fs.open_output_stream(path.rstrip("/") + "/part-000000.parquet") as stream:
            pq.write_table(table, stream)
        return
    local_uri = path_of(uri)
    os.makedirs(local_uri, exist_ok=True)
    # Exclusive creation is the local create-only fence. Object-store attempts rely on their unique
    # publisher identity plus the empty-prefix proof immediately above this call.
    with open(os.path.join(local_uri, "part-000000.parquet"), "xb") as stream:
        pq.write_table(table, stream)


_RAY_SCHEMA_HINT_ATTR = "_dp_known_arrow_schema"
_UNKNOWN_RAY_SCHEMA = object()
_NO_RAY_SCHEMA_HINT = object()


def _arrow_schema(schema):
    import pyarrow as pa

    arrow_schema = getattr(schema, "base_schema", schema)
    return arrow_schema if isinstance(arrow_schema, pa.Schema) else None


def _remember_ray_schema(dataset, schema):
    """Attach driver-side schema lineage; Ray Dataset transformations do not preserve custom attrs."""
    arrow_schema = _arrow_schema(schema)
    setattr(dataset, _RAY_SCHEMA_HINT_ATTR, arrow_schema if arrow_schema is not None else _UNKNOWN_RAY_SCHEMA)
    return dataset


def _known_ray_schema(dataset):
    """Return empty-result schema lineage without asking Ray to execute or sample the Dataset."""
    hint = getattr(dataset, _RAY_SCHEMA_HINT_ATTR, _NO_RAY_SCHEMA_HINT)
    if hint is _UNKNOWN_RAY_SCHEMA:
        return None
    if hint is not _NO_RAY_SCHEMA_HINT:
        return hint
    try:
        schema = dataset.schema(fetch_if_missing=False)
    except TypeError:  # lightweight unit-test stand-ins expose the older no-argument shape
        schema = dataset.schema()
    return schema


def _runtime_ray_schema(dataset):
    """Return Ray's materialized schema, ignoring empty-result lineage hints."""
    try:
        return _arrow_schema(dataset.schema(fetch_if_missing=False))
    except TypeError:  # lightweight unit-test stand-ins expose the older no-argument shape
        return _arrow_schema(dataset.schema())


def _ray_schema_explicitly_unknown(dataset) -> bool:
    return getattr(dataset, _RAY_SCHEMA_HINT_ATTR, _NO_RAY_SCHEMA_HINT) is _UNKNOWN_RAY_SCHEMA


def _declared_ray_schema(config: dict):
    """Convert a resolved outputSchema contract to Arrow metadata without executing user code."""
    columns = config.get("outputSchema")
    if not isinstance(columns, list) or not columns:
        return None
    import duckdb

    from hub.executors.engine import _duck_type

    projections = []
    for column in columns:
        if not isinstance(column, dict) or not str(column.get("name") or ""):
            raise RuntimeError("Ray transform outputSchema contains an unnamed column")
        name = validate_identifier_alias(column["name"], label="Ray outputSchema column")
        projections.append(
            f"CAST(NULL AS {_duck_type(column.get('type'))}) AS {quote_identifier(name)}"
        )
    con = _secure_duckdb_connection()
    try:
        return con.execute(f"SELECT {', '.join(projections)} WHERE FALSE").to_arrow_table().schema
    finally:
        con.close()


def _duckdb_empty_result_schema(sql: str, *, policy_fragments=(), **inputs):
    """Resolve relational output metadata from typed empty inputs on an isolated DuckDB connection."""
    import pyarrow as pa
    schemas = {name: _arrow_schema(schema) for name, schema in inputs.items()}
    if any(schema is None for schema in schemas.values()):
        return None
    con = _secure_duckdb_connection()
    try:
        for name, schema in schemas.items():
            con.register(name, pa.Table.from_batches([], schema=schema))
        _validate_policy_fragments(con, policy_fragments)
        return con.execute(sql).to_arrow_table().schema
    finally:
        con.close()


def _write_worker_direct_parquet(dataset, uri: str, *, attempt_id: str,
                                 ray_opts: dict | None = None) -> tuple[int, str]:
    """Write one immutable Parquet attempt and publish its success manifest last."""
    from hub.plugins.adapters import is_object_uri, object_fs, path_of
    low = uri.lower()
    extension = next((ext for ext in _PARQUET_EXTENSIONS if low.endswith(ext)), "")
    out_dir = uri[:-len(extension)] if extension else uri.rstrip("/")
    committed = read_manifest(out_dir)
    if (committed is not None and committed.get("runId") == attempt_id
            and validate_shards(out_dir, committed)):
        return int(committed["rows"]), out_dir
    if attempt_has_commit_record(out_dir) or attempt_has_contents(out_dir):
        raise RuntimeError(
            "Ray output attempt already exists without an exact committed inventory; refusing to "
            "overwrite an immutable or possibly live prefix"
        )
    owns_prefix = True
    try:
        declared_schema = _known_ray_schema(dataset)
        materialized = dataset.materialize()
        rows = materialized.count()
        materialized_schema = _runtime_ray_schema(materialized)
        unknown_schema = _ray_schema_explicitly_unknown(dataset)
        schema = (
            declared_schema if rows == 0 and declared_schema is not None
            else None if rows == 0 and unknown_schema
            else materialized_schema if materialized_schema is not None else declared_schema
        )
        if rows == 0:
            _write_empty_parquet(out_dir, schema)
        else:
            try:
                from ray.data import SaveMode
                create_only = SaveMode.ERROR
            except (ModuleNotFoundError, ImportError):  # unit fakes run without the optional Ray package
                create_only = "error"
            if is_object_uri(out_dir):
                fs, path = object_fs(out_dir)
                materialized.write_parquet(
                    path, filesystem=fs, mode=create_only, ray_remote_args=ray_opts or None
                )
            else:
                local_dir = path_of(out_dir)
                materialized.write_parquet(
                    local_dir, mode=create_only, ray_remote_args=ray_opts or None
                )
        _write_handoff_manifest(out_dir, run_id=attempt_id, rows=rows, schema=schema)
        owns_prefix = False
        return rows, out_dir
    finally:
        if owns_prefix and not is_object_uri(out_dir):
            # A local failed write is synchronous with this process. On an object store, Ray worker tasks
            # can outlive a failed/disconnected driver; only durable backend terminal reconciliation may
            # authorize deletion, so leave the registered writing attempt for lifecycle handling.
            discard_attempt(out_dir)


def _ray_child_env() -> dict[str, str]:
    """Allowlisted Ray-driver environment; the driver never needs the hub metadata identity."""
    child = build_workload_env(include_metadata_db=False)
    # A kernel launched through uv must not make Ray workers build a fresh, ray-less environment.
    for key in list(child):
        if key in ("VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX") or key.startswith("UV_"):
            child.pop(key, None)
    child["PATH"] = os.path.dirname(sys.executable) + os.pathsep + child.get("PATH", "")
    child["RAY_DATA_DISABLE_PROGRESS_BARS"] = "1"
    child["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
    child["DP_RAY_GPU_BATCH_ROWS"] = str(_gpu_batch_rows())
    return child


def _ray_jobs_env(job: dict) -> dict[str, str]:
    """Frozen non-secret semantics plus the operator's current rotatable data credentials."""
    child = dict(job["semantic_env"])
    child.update(build_workload_credential_env())
    return child


def _ray_opts(requires: dict | None) -> dict:
    """Map the region's resolved resource need (the planner's `requires`) to per-Ray-task placement
    options, so a Ray cluster schedules the region's map tasks onto a worker that has the resource:
    `gpu` → num_gpus (each map task needs a GPU), `gpuType` → Ray's exact accelerator_type fence; a
    non-`engine` label `k=v` → a custom resource named `v` (fractional so many tasks share one node —
    declare it on the node via `ray start --resources`).
    cpu/mem are omitted: they're per-REGION aggregates, not the per-TASK cost Ray schedules on."""
    if not requires:
        return {}
    opts: dict = {}
    gpu_type = requires.get("gpu_type") or requires.get("gpuType")
    if requires.get("gpu") or gpu_type:
        # A type-only requirement means "one GPU of this type" throughout placement/UI semantics.
        opts["num_gpus"] = float(requires.get("gpu") or 1)
    if gpu_type:
        # Ray's typed accelerator option adds the canonical ``accelerator_type:<type>`` resource fence.
        # num_gpus alone can otherwise land an A100-pinned task on any available GPU model.
        opts["accelerator_type"] = _canonical_accelerator_type(gpu_type)
    res = {str(v): 0.001 for k, v in (requires.get("labels") or {}).items() if k != "engine" and v}
    if res:
        opts["resources"] = res
    return opts


def _gpu_batch_size(ray_opts: dict | None) -> int | None:
    opts = ray_opts or {}
    return _gpu_batch_rows() if (opts.get("num_gpus") or opts.get("accelerator_type")) else None


def _advertised_ray_labels() -> dict[str, str]:
    """Operator-declared placement labels, e.g. ``DP_RAY_LABELS=pool=a100,zone=use1``.

    A non-engine label value is also the Ray custom-resource name used by ``_ray_opts``. Keeping the
    same declaration in the hub capacity and the cluster's ``ray start --resources`` configuration
    makes pre-dispatch admission agree with Ray's task scheduler.
    """
    labels = {"engine": "ray"}
    for item in os.environ.get("DP_RAY_LABELS", "").split(","):
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key and value and key != "engine":
            labels[key] = value
    return labels


def _make_mapper(config: dict):
    """A Ray Data batch UDF that reuses the DuckDB engine's EXACT operator — so a transform produces the
    same rows on Ray as locally. Captures only plain strings, so it cloudpickles to Ray workers."""
    code, mode, on_error = config.get("code"), config["mode"], config.get("onError", "raise")
    fmt = config.get("batchFormat", "rows") if mode == "map_batches" else "rows"

    def _op(table):  # a pyarrow.Table block
        import pyarrow as pa

        from hub import sandbox
        from hub.executors.engine import _apply_batch, _apply_fn

        fn = sandbox.compile_operator(code, mode)
        if fmt in ("pandas", "arrow"):  # whole-batch pandas/arrow UDF — SAME arrow-native path as local
            res = _apply_batch(fn, table, fmt, on_error, None)
            return res if res is not None else table.slice(0, 0)  # skip → empty block (Ray needs a table)
        rows: list[dict] = []
        for batch in table.to_batches():
            rows.extend(_apply_fn(fn, batch, mode, on_error, None))
        return pa.Table.from_pylist(rows) if rows else table.slice(0, 0)  # keep schema when a batch empties

    return _op


class RayRunner:
    name = "ray-data"
    durable_backend = _JOBS_BACKEND

    def __init__(self, deps, jobs_client_factory=None, artifact_store=None, recover: bool = True):
        self.deps = deps
        self.base = deps.runner            # the local out-of-core runner — estimate, fallback, lineage reuse
        self.resolve_adapter = deps.resolve_adapter
        self.catalog = deps.catalog
        self.node_specs = deps.node_specs
        # mirror the hub-wired status/history hooks so Ray runs are just as visible cross-instance
        self.on_status = getattr(self.base, "on_status", None)
        self.on_complete = getattr(self.base, "on_complete", None)
        self.runs: dict[str, RunStatus] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._cancel_ack: set[str] = set()
        # A driver whose OS process cannot yet be reaped must remain non-terminal.  Keep every
        # publication fence and the credential-bearing work directory owned until a background
        # reconciler can prove the process stopped.
        self._unreaped_drivers: dict[str, dict] = {}
        self._finalizing_drivers: set[str] = set()
        self._driver_procs: dict[str, object] = {}
        self._driver_workdirs: dict[str, str] = {}
        self._retained_workdirs: dict[str, str] = {}
        self._settled: dict[str, threading.Event] = {}
        self._supervising: set[str] = set()
        self._cancel_stop_sent: set[str] = set()
        self._backend_refs: dict[str, dict] = {}
        self._control_observed_monotonic: dict[str, float] = {}
        self._recovery_blocked: set[str] = set()
        self._lock = threading.Lock()
        self.jobs_address = os.environ.get("DP_RAY_JOBS_ADDRESS", "").strip()
        # Presence of the plugin is enough to own/recover an existing binding: its durable SQL row carries
        # the original control address. A new submission still requires ``jobs_address`` below.
        self.durable_available = True
        self._jobs_client_factory = jobs_client_factory
        self._artifacts = artifact_store or JsonArtifactStore()
        self._jobs_poll_s = _float_env("DP_RAY_JOBS_POLL_S", 1.0)
        self._jobs_cancel_timeout_s = _float_env("DP_RAY_JOBS_CANCEL_TIMEOUT_S", 30.0)
        self._jobs_result_timeout_s = _float_env("DP_RAY_JOBS_RESULT_TIMEOUT_S", 30.0)
        self._submission_lease_s = _float_env("DP_RAY_JOBS_SUBMISSION_LEASE_S", 30.0, 1.0)
        self._publication_lease_s = _float_env("DP_RAY_JOBS_PUBLICATION_LEASE_S", 60.0, 5.0)
        self._jobs_request_timeout_s = _float_env("DP_RAY_JOBS_REQUEST_TIMEOUT_S", 30.0, 1.0)
        self._max_lease_hold_s = _float_env("DP_RAY_JOBS_MAX_LEASE_HOLD_S", 300.0, 1.0)
        atexit.register(self._terminate_all)
        if recover:
            self._recover_jobs()

    def _terminate_all(self) -> None:
        """Fence local drivers at shutdown; clean only writers whose exit is proven."""
        import shutil

        with self._lock:
            drivers = list(self._driver_procs.items())
            for run_id, _proc in drivers:
                event = self._cancel.get(run_id)
                if event is not None:
                    event.set()
        for run_id, proc in drivers:
            if not self._try_reap_driver(proc):
                continue  # retain workdir and managed ownership; never guess writer termination
            with self._lock:
                state = self._unreaped_drivers.pop(run_id, None)
                if state is not None:
                    if run_id in self._finalizing_drivers:
                        state = None
                    else:
                        self._finalizing_drivers.add(run_id)
            if state is not None:
                status_file = os.path.join(state["work"], "status.json")
                if os.path.exists(status_file):
                    try:
                        with open(status_file) as stream:
                            state["result"] = json.load(stream)
                    except Exception:  # noqa: BLE001 — malformed receipt stays private
                        state["result"] = None
                state["returncode"] = proc.returncode
                try:
                    self._finish_supervision(state)
                except Exception:  # noqa: BLE001 — interpreter shutdown remains best-effort
                    logging.getLogger(__name__).exception(
                        "Ray shutdown finalization failed after driver reap")
                continue
            # A normal supervisor may still be reading its terminal receipt.  Reaping is enough for
            # shutdown safety; never race that owner by deleting its workdir here.
        with self._lock:
            retained = list(self._retained_workdirs.items())
        for run_id, work in retained:
            try:
                shutil.rmtree(work)
            except Exception:  # noqa: BLE001 — leave it for operator cleanup
                continue
            with self._lock:
                self._retained_workdirs.pop(run_id, None)

    # Gate on the declared subset from the CompilePlan alone. An unpinned whole-backend choice can use
    # DuckDB for unsupported work; run()/run_unit() separately enforce explicit engine=ray placement.
    def can_run(self, plan) -> bool:
        return plan_is_clean(plan) or plan_is_distributable(plan, RAY_RELATIONAL)

    def estimate(self, plan, rows, byts=None):
        return self.base.estimate(plan, rows, byts)  # reuse the hub-side confirm gate verbatim

    def _refresh_recovery_blocked_terminal(self, status: RunStatus) -> bool:
        """Converge a stale local recovery diagnostic behind the authoritative terminal fence."""
        from hub import metadb

        run_id = status.run_id
        terminal = metadb.terminal_run_status(run_id)
        if terminal not in ("done", "failed", "cancelled"):
            return False
        persisted = metadb.get_run_state(run_id)
        canonical = None
        if persisted is not None:
            try:
                candidate = RunStatus.model_validate(persisted)
                if candidate.run_id == run_id and candidate.status == terminal:
                    canonical = candidate
            except Exception:  # malformed retained detail falls back to the compact terminal fence
                pass
        if canonical is not None:
            self._copy_status(status, canonical)
        elif not self._converge_terminal_fence(status):
            return False
        with self._lock:
            self._recovery_blocked.discard(run_id)
            self._settled.setdefault(run_id, threading.Event()).set()
        self._prune_terminal_runs()
        return True

    def status(self, run_id: str) -> RunStatus:
        st = self.runs.get(run_id)
        if st is None:
            st = self._reattach_job(run_id)
        if st is None:
            raise KeyError(run_id)
        if run_id in self._recovery_blocked:
            self._refresh_recovery_blocked_terminal(st)
        ref = getattr(st, "backend_ref", None)
        if (st.status in ("queued", "running") and ref and ref.backend == _JOBS_BACKEND
                and run_id not in self._recovery_blocked):
            self._ensure_jobs_supervisor(run_id)
        return st

    def cancel(self, run_id: str) -> RunStatus:
        st = self.runs.get(run_id)
        if st and run_id in self._recovery_blocked:
            self._refresh_recovery_blocked_terminal(st)
        ref = getattr(st, "backend_ref", None) if st else None
        if st and run_id in self._recovery_blocked and st.status in ("queued", "running"):
            from hub import metadb

            if not metadb.request_backend_cancel(run_id):
                self._refresh_recovery_blocked_terminal(st)
                return self.runs.get(run_id, st)
            if self._refresh_recovery_blocked_terminal(st):
                return st
            if run_id in self._cancel:
                self._cancel[run_id].set()
            suffix = "cancellation recorded; repair the malformed durable binding before remote stop can resume"
            if suffix not in (st.error or ""):
                st.error = f"{st.error}; {suffix}" if st.error else suffix
                metadb.save_run_state(run_id, st.model_dump())
            self._refresh_recovery_blocked_terminal(st)
            return st
        if st and ref and ref.backend == _JOBS_BACKEND and st.status in ("queued", "running"):
            from hub import metadb
            if not metadb.request_backend_cancel(run_id):
                self._refresh_recovery_blocked_terminal(st)
                return self.runs.get(run_id, st)
            ev = self._cancel.get(run_id)
            if ev:
                ev.set()
            settled = self._settled.get(run_id)
            if settled:
                # STOPPED is an acknowledgement, not a request label. The supervisor issues stop_job and
                # keeps polling the official Jobs API; timeout leaves the run non-terminal because the
                # remote entrypoint may still publish.
                acknowledged = settled.wait(self._jobs_cancel_timeout_s + self._jobs_poll_s * 2)
                current = self.runs.get(run_id, st)
                if not acknowledged and current.status in ("queued", "running"):
                    if self._refresh_recovery_blocked_terminal(current):
                        return current
                    # Do not make the synchronous API response depend on the supervisor winning a polling
                    # boundary race. The supervisor persists the same diagnostic and keeps reattaching.
                    current.error = self._cancel_timeout_error()
                    metadb.save_run_state(current.run_id, current.model_dump())
                    self._refresh_recovery_blocked_terminal(current)
                return current
            return self.runs.get(run_id, st)
        if st and st.status in ("queued", "running"):
            ev = self._cancel.get(run_id)
            if ev:
                ev.set()  # cooperative — checked between IR steps (Ray has no cheap mid-Dataset abort)
            # Cancellation is only a request here.  A clean terminal `done` receipt may already have
            # crossed the data commit point, and an unreaped driver may still publish.  The supervisor
            # arbitrates that race after wait() proves the driver stopped.
        return st

    def cancel_acknowledged(self, run_id: str) -> bool:
        st = self.runs.get(run_id)
        ref = getattr(st, "backend_ref", None) if st else None
        if ref and ref.backend == _JOBS_BACKEND:
            settled = self._settled.get(run_id)
            return bool(st.status == "cancelled" and settled and settled.is_set())
        with self._lock:
            return run_id in self._cancel_ack

    def _acknowledge_cancel(self, run_id: str) -> None:
        with self._lock:
            self._cancel_ack.add(run_id)

    def _cancel_timeout_error(self) -> str:
        return (
            f"Ray stop was not acknowledged within {self._jobs_cancel_timeout_s:g}s; "
            "the run remains non-terminal and supervision continues"
        )

    def logs(self, run_id: str) -> str:
        """Do not expose remote raw logs until an operator-only diagnostics surface exists."""
        st = self.status(run_id)
        if not st.backend_ref or st.backend_ref.backend != _JOBS_BACKEND:
            raise KeyError(f"run '{run_id}' is not a Ray Jobs run")
        return "Ray diagnostics are available only through protected operator tooling"

    @staticmethod
    def _stable_exception(context: str, error: BaseException, code: str) -> str:
        """Safe projection for shared state/logs: never include remote exception text or identifiers."""
        return f"{context} (code={code},type={type(error).__name__})"

    @staticmethod
    def _public_remote_error(error: object) -> str:
        """Expose a stable code/type only; remote text can contain rotated credentials or user data."""
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]{0,79})\s*:", str(error or ""))
        error_type = match.group(1).rsplit(".", 1)[-1] if match else "RemoteExecutionError"
        return f"Ray execution failed ({error_type}; code=ray_execution_failed)"

    @staticmethod
    def _validate_control_address(address: str) -> str:
        parsed = urlparse(address)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError("DP_RAY_JOBS_ADDRESS must be an http(s) Ray Jobs API address")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(
                "DP_RAY_JOBS_ADDRESS must not contain credentials, query parameters, or fragments"
            )
        return address.rstrip("/")

    def _jobs_client(self, address: str | None = None):
        address = self._validate_control_address(str(address or self.jobs_address or "").strip())
        if self._jobs_client_factory is not None:
            return self._jobs_client_factory(address)
        try:
            from ray.job_submission import JobSubmissionClient
        except ImportError as e:
            raise RuntimeError(
                "Ray Jobs mode requires the optional ray[default,data] dependencies on the submitting hub"
            ) from e
        # Ray's SDK intentionally lets RAY_ADDRESS override even an explicit HTTP ``address`` argument.
        # A hub may legitimately carry RAY_ADDRESS=auto for another backend, so pin the official, higher-
        # priority API-server variable only for client construction and restore the process environment.
        marker = object()
        with _JOBS_CLIENT_ENV_LOCK:
            previous = os.environ.get("RAY_API_SERVER_ADDRESS", marker)
            os.environ["RAY_API_SERVER_ADDRESS"] = address
            try:
                request_timeout_s = self._jobs_request_timeout_s

                class BoundedJobSubmissionClient(JobSubmissionClient):
                    def _do_request(self, method, endpoint, **kwargs):
                        kwargs.setdefault("timeout", request_timeout_s)
                        return super()._do_request(method, endpoint, **kwargs)

                return BoundedJobSubmissionClient(address)
            finally:
                if previous is marker:
                    os.environ.pop("RAY_API_SERVER_ADDRESS", None)
                else:
                    os.environ["RAY_API_SERVER_ADDRESS"] = previous

    def _jobs_contract(self, *, recovery: bool = False) -> dict[str, str]:
        """Validate the production contract before creating any durable binding or remote job."""
        from hub.plugins.adapters import is_object_uri

        values = {
            "control_address": os.environ.get("DP_RAY_JOBS_ADDRESS", "").strip(),
            "entrypoint": os.environ.get("DP_RAY_JOBS_ENTRYPOINT", "").strip(),
            "module": os.environ.get(
                "DP_RAY_JOBS_MODULE", "/app/examples/plugins/dp_ray/__init__.py"
            ).strip(),
            "code_ref": os.environ.get("DP_RAY_JOBS_CODE_REF", "").strip(),
            "cluster_ref": os.environ.get("DP_RAY_JOBS_CLUSTER_REF", "").strip(),
            "workspace": os.environ.get("DP_RAY_JOBS_WORKSPACE", "").strip(),
            "data_dir": os.environ.get("DP_RAY_JOBS_DATA_DIR", "").strip(),
            "storage": os.environ.get("DP_STORAGE_URL", "").strip(),
        }
        env_names = {
            "control_address": "DP_RAY_JOBS_ADDRESS", "entrypoint": "DP_RAY_JOBS_ENTRYPOINT",
            "module": "DP_RAY_JOBS_MODULE", "code_ref": "DP_RAY_JOBS_CODE_REF",
            "cluster_ref": "DP_RAY_JOBS_CLUSTER_REF", "workspace": "DP_RAY_JOBS_WORKSPACE",
            "data_dir": "DP_RAY_JOBS_DATA_DIR",
        }
        missing = [env_names[key] for key in env_names if not values[key]]
        if not values["storage"]:
            missing.append("DP_STORAGE_URL")
        if missing:
            error = JobsConfigurationUnavailable if recovery else RuntimeError
            raise error(
                "Ray Jobs mode is incomplete; configure image-baked code and shared artifacts: "
                + ", ".join(missing)
            )
        values["control_address"] = self._validate_control_address(values["control_address"])
        prefix = os.environ.get("DP_RAY_JOBS_ARTIFACT_PREFIX", "").strip()
        values["artifact_prefix"] = prefix or f"{values['storage'].rstrip('/')}/__ray_jobs__"
        if not is_object_uri(values["storage"]) or not is_object_uri(values["artifact_prefix"]):
            raise RuntimeError(
                "Ray Jobs mode requires object storage shared by hub, driver, and workers; set "
                "DP_STORAGE_URL and DP_RAY_JOBS_ARTIFACT_PREFIX to s3://, r2://, gs://, or gcs:// URIs"
            )
        return values

    def _validate_jobs_io(self, ir, sink_targets: dict[str, str] | None = None,
                          materialize_uri: str | None = None) -> None:
        from hub.plugins.adapters import is_object_uri

        if len(sink_targets or {}) > 1:
            raise RuntimeError(
                "multiple Ray write sinks require atomic batch publication, which is not enabled"
            )
        local_reads = [s.config.get("uri") for s in ir.steps
                       if s.op == "read" and not is_object_uri(str(s.config.get("uri") or ""))]
        local_writes = [uri for uri in (sink_targets or {}).values() if not is_object_uri(uri)]
        unsupported_sinks = []
        for step in ir.steps:
            if step.op != "write":
                continue
            target_uri = (sink_targets or {}).get(step.id)
            if target_uri is None:
                raise RuntimeError(f"Ray Jobs write sink '{step.id}' has no resolved target")
            spec = SinkSpec.from_config(step.config, step.config.get("title"))
            if not _worker_direct_parquet_sink(
                    spec, target_uri, self.resolve_adapter(target_uri)):
                unsupported_sinks.append(step.id)
        append_steps = [s.id for s in ir.steps if s.op == "write"
                        and SinkSpec.from_config(s.config, s.config.get("title")).mode == "append"]
        partition_steps = [s.id for s in ir.steps if s.op == "write"
                           and SinkSpec.from_config(s.config, s.config.get("title")).partition_by]
        if materialize_uri and not is_object_uri(materialize_uri):
            local_writes.append(materialize_uri)
        if local_reads or local_writes:
            raise RuntimeError(
                "Ray Jobs runs off-host and accepts only shared object-store inputs/outputs; "
                f"non-shared inputs={local_reads or 'none'}, outputs={local_writes or 'none'}"
            )
        if append_steps:
            raise RuntimeError(
                "Ray Jobs durable retries require idempotent sinks; file-adapter append is not safe after "
                f"cluster job-metadata loss (write steps: {append_steps}). Use overwrite or a transactional sink."
            )
        if partition_steps:
            raise RuntimeError(
                "Ray Jobs object-store sinks do not yet support partitionBy through the shared adapter "
                f"contract (write steps: {partition_steps})"
            )
        if unsupported_sinks:
            raise RuntimeError(
                "Ray Jobs production mode supports only built-in, overwrite, non-partitioned "
                "Parquet sinks with hub-managed object attempts; unsupported write steps: "
                f"{unsupported_sinks}"
            )

    def _validate_jobs_catalog_publication(self, sink_targets: dict[str, str]) -> None:
        """Fail before allocation/submission unless managed write-ahead publication is available."""
        if not sink_targets:
            return
        from hub.plugins.catalog import (
            core_managed_publication_planner,
            core_managed_publisher,
        )

        if (not isinstance(self.catalog, DurableCatalogPublisher)
                or core_managed_publication_planner(self.catalog) is None
                or core_managed_publisher(self.catalog) is None):
            raise RuntimeError(
                "Ray Jobs managed sinks require the core write-ahead catalog planner and publisher"
            )

    def _jobs_source_attempts(self, graph, target) -> list[str]:
        """Freeze exact managed source roots; ordinary object URIs need no lifecycle pin."""
        from hub.storage import preflight_managed_execution_sources

        sources = preflight_managed_execution_sources(
            self.deps.storage, g.execution_source_uris(graph, target)
        )
        managed: set[str] = set()
        for source in sources:
            uri = str(source).rstrip("/")
            if not has_attempt_path_component(uri):
                continue
            if not is_attempt_uri(uri):
                raise RuntimeError(
                    "Ray Jobs managed sources must reference an exact object-attempt root"
                )
            managed.add(uri)
        return sorted(managed)

    def _make_jobs_artifacts(self, run_id: str, graph, target, *, sink_targets=None,
                             sink_contracts=None, source_attempts=None,
                             materialize_uri=None, requires=None) -> tuple[dict, dict]:
        from hub import metadb

        cfg = self._jobs_contract()
        if sink_targets and sink_contracts is None:
            raise RuntimeError(
                "Ray Jobs sink contracts must be allocated and frozen before artifact creation"
            )
        created_by, _auth_canvas_id = metadb.run_auth(run_id)
        if not created_by:
            raise RuntimeError(
                "Ray Jobs requires a durable run owner before artifact allocation"
            )
        graph_doc = prepare_workload_graph(graph)
        semantic_env = build_workload_semantic_env()
        semantic_env.update({
            "DP_RAY_JOB_MODE": "1",
            # Remote jobs execute only dependencies already present in the image. Never let a replay
            # inherit a changed hub setting and perform an unpinned network-time installation.
            "DP_CANVAS_PIP_DEPS": "0",
            "DP_WORKSPACE": cfg["workspace"],
            "DP_DATA_DIR": cfg["data_dir"],
            "RAY_DATA_DISABLE_PROGRESS_BARS": "1",
            "RAY_ENABLE_UV_RUN_RUNTIME_ENV": "0",
            "DP_RAY_GPU_BATCH_ROWS": str(_gpu_batch_rows()),
        })
        canonical = {
            "contract_version": _JOB_CONTRACT_VERSION,
            "run_id": run_id,
            "graph": graph_doc,
            "target": target,
            "source_attempts": source_attempts or [],
            "sink_targets": sink_targets,
            "sink_contracts": sink_contracts or {},
            "materialize_uri": materialize_uri,
            "requires": requires,
            "code_ref": cfg["code_ref"],
            "cluster_ref": cfg["cluster_ref"],
            "artifact_prefix": cfg["artifact_prefix"],
            "workspace": cfg["workspace"],
            "data_dir": cfg["data_dir"],
            "entrypoint": cfg["entrypoint"],
            "module": cfg["module"],
            "semantic_env": semantic_env,
            "semantic_env_sha256": _semantic_env_sha256(semantic_env),
        }
        attempt_id = _job_attempt_id(canonical)
        submission_id = _jobs_submission_id(run_id, attempt_id)
        base = f"{cfg['artifact_prefix'].rstrip('/')}/{submission_id}"
        ref = {
            "backend": _JOBS_BACKEND,
            "cluster_ref": cfg["cluster_ref"],
            "submission_id": submission_id,
            "attempt_id": attempt_id,
            "job_uri": f"{base}/job.dpjob",
            "result_uri": f"{base}/result.dpresult",
            "code_ref": cfg["code_ref"],
            "control_address": cfg["control_address"],
            "cancel_requested": False,
            "durable": True,
        }
        job = {**canonical, **ref, "result_uri": ref["result_uri"]}
        # SQL-only routing fields are not disclosed to the workload envelope.
        job.pop("control_address", None)
        job.pop("cancel_requested", None)
        job["envelope_sha256"] = _job_envelope_sha256(job)
        return ref, job

    @staticmethod
    def _ref_model(ref: dict) -> RunBackendRef:
        return RunBackendRef.model_validate(ref)

    @staticmethod
    def _validated_sink_contracts(job: dict) -> dict[str, dict[str, str]]:
        contracts = job.get("sink_contracts")
        raw_targets = job.get("sink_targets")
        targets = {} if raw_targets is None else raw_targets
        if not isinstance(contracts, dict) or not isinstance(targets, dict):
            raise ArtifactContractError("Ray job sink contracts and targets must be objects")
        if set(contracts) != set(targets):
            raise ArtifactContractError("Ray job sink contracts do not match its sink target set")
        if len(targets) > 1:
            raise ArtifactContractError(
                "Ray Jobs supports at most one write sink until atomic batch publication is available"
            )
        validated: dict[str, dict[str, str]] = {}
        fields = {"name", "logical_uri", "physical_uri", "writer"}
        for step_id, contract in contracts.items():
            if not isinstance(step_id, str) or not step_id or not isinstance(contract, dict):
                raise ArtifactContractError("Ray job contains an invalid sink contract")
            if set(contract) != fields or any(
                    not isinstance(contract.get(key), str) or not contract[key]
                    for key in fields):
                raise ArtifactContractError(f"Ray job sink contract '{step_id}' is incomplete")
            logical_uri = contract["logical_uri"]
            physical_uri = contract["physical_uri"]
            writer = contract["writer"]
            if logical_uri != targets.get(step_id):
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' changed its logical target"
                )
            if writer != "worker-direct-parquet":
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' has an unsupported writer"
                )
            if physical_uri == logical_uri or not is_attempt_uri(physical_uri):
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' has no managed physical attempt"
                )
            validated[step_id] = contract
        return validated

    @staticmethod
    def _validated_source_attempts(job: dict) -> list[str]:
        raw = job.get("source_attempts")
        if (not isinstance(raw, list)
                or any(not isinstance(uri, str) or not uri for uri in raw)):
            raise ArtifactContractError("Ray job source_attempts must be a list of exact URIs")
        canonical = sorted(set(uri.rstrip("/") for uri in raw))
        if raw != canonical:
            raise ArtifactContractError("Ray job source_attempts must be sorted and unique")
        try:
            from hub.models import Graph
            graph = Graph.model_validate(job.get("graph"))
            graph_managed: set[str] = set()
            for source in g.execution_source_uris(graph, job.get("target")):
                uri = str(source).rstrip("/")
                if not has_attempt_path_component(uri):
                    continue
                if not is_attempt_uri(uri):
                    raise ArtifactContractError(
                        "Ray job graph contains a managed-source descendant instead of its exact root"
                    )
                graph_managed.add(uri)
        except ArtifactContractError:
            raise
        except Exception as e:
            raise ArtifactContractError("Ray job graph has an invalid managed-source contract") from e
        if canonical != sorted(graph_managed):
            raise ArtifactContractError(
                "Ray job source_attempts do not match its hash-bound execution graph"
            )
        return canonical

    @staticmethod
    def _validate_jobs_source_pins(job: dict) -> list[dict]:
        """Attest SQL-owned source generations and their exact envelope URI order."""
        from hub import metadb

        expected = RayRunner._validated_source_attempts(job)
        try:
            pins = metadb.backend_source_pins(str(job.get("run_id") or ""))
        except RuntimeError as e:
            raise ArtifactContractError(
                "Ray job durable source-generation pins failed attestation"
            ) from e
        if pins is None:
            raise ArtifactContractError("Ray job has no durable backend source-pin owner")
        malformed = any(
            not isinstance(pin, dict)
            or set(pin) != {"uri", "generation"}
            or not isinstance(pin.get("uri"), str)
            or isinstance(pin.get("generation"), bool)
            or not isinstance(pin.get("generation"), int)
            or pin["generation"] < 1
            for pin in pins
        )
        if malformed or [pin["uri"] for pin in pins] != expected:
            raise ArtifactContractError(
                "Ray job source attempts do not match its durable source-generation pins"
            )
        return pins

    def _validate_job_artifact_integrity(
            self, ref: RunBackendRef, status: RunStatus, job: dict) -> None:
        """Validate immutable execution bytes without consulting this process's current config."""
        try:
            version = int(job.get("contract_version") or 0)
            if version != _JOB_CONTRACT_VERSION:
                raise ValueError(
                    f"unsupported Ray job artifact contract_version {version}"
                )
            envelope_fields = ray_job_envelope_fields(version)
        except (AttributeError, TypeError, ValueError) as e:
            raise ArtifactContractError(str(e)) from e
        try:
            require_exact_object(job, envelope_fields, label="Ray job artifact")
        except ArtifactCorrupt as e:
            raise ArtifactContractError(str(e)) from e
        attempt_id = _job_attempt_id(job)
        expected = {
            "backend": ref.backend,
            "cluster_ref": ref.cluster_ref,
            "submission_id": ref.submission_id,
            "attempt_id": ref.attempt_id,
            "job_uri": ref.job_uri,
            "result_uri": ref.result_uri,
            "code_ref": ref.code_ref,
            "durable": True,
        }
        for key, value in expected.items():
            if job.get(key) != value:
                raise ArtifactContractError(
                    f"Ray job artifact {key} does not match its durable backend binding"
                )
        if job.get("run_id") != status.run_id:
            raise ArtifactContractError("Ray job artifact run_id does not match its RunStatus")
        if attempt_id != ref.attempt_id:
            raise ArtifactContractError("Ray job artifact content hash does not match attempt_id")
        if _job_envelope_sha256(job) != job["envelope_sha256"]:
            raise ArtifactContractError("Ray job artifact envelope hash does not match its content")
        semantic_env = job.get("semantic_env")
        if not isinstance(semantic_env, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in semantic_env.items()):
            raise ArtifactContractError("Ray job artifact semantic_env must be a string map")
        if _semantic_env_sha256(semantic_env) != job.get("semantic_env_sha256"):
            raise ArtifactContractError("Ray job artifact semantic environment hash does not match")
        if _jobs_submission_id(status.run_id, attempt_id) != ref.submission_id:
            raise ArtifactContractError("Ray job artifact submission_id is not deterministic")
        self._validated_source_attempts(job)
        self._validated_sink_contracts(job)

    def _validate_job_reattach_config(self, job: dict) -> None:
        """Require current code/cluster compatibility only before a submit or replay."""
        current = self._jobs_contract(recovery=True)
        if job.get("cluster_ref") != current["cluster_ref"]:
            raise JobsConfigurationDrift(
                "DP_RAY_JOBS_CLUSTER_REF changed; restore the original cluster identity before reattaching"
            )
        if job.get("artifact_prefix") != current["artifact_prefix"]:
            raise JobsConfigurationDrift(
                "DP_RAY_JOBS_ARTIFACT_PREFIX changed; restore the original artifact namespace before reattaching"
            )
        for key in ("code_ref", "entrypoint", "module", "workspace", "data_dir"):
            if job.get(key) != current[key]:
                raise JobsConfigurationDrift(
                    f"Ray Jobs {key} changed; restore the original image contract before reattaching"
                )

    def _validate_job_artifact(self, ref: RunBackendRef, status: RunStatus, job: dict) -> None:
        """Compatibility wrapper for callers that explicitly require integrity and reattachability."""
        self._validate_job_artifact_integrity(ref, status, job)
        self._validate_job_reattach_config(job)

    def _read_or_materialize_job_artifact(
            self, ref: RunBackendRef, status: RunStatus) -> dict:
        """Read the object artifact, recreating the exact canonical bytes after a post-bind crash."""
        from hub import metadb

        durable_payload = metadb.backend_job_artifact_payload(status.run_id)
        try:
            candidate = self._artifacts.read(ref.job_uri)
        except (ArtifactNotFound, FileNotFoundError):
            if durable_payload is None:  # nullable keeps pre-upgrade/manual rows recoverable when object exists
                raise
            try:
                candidate = json.loads(durable_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ArtifactCorrupt("durable SQL job envelope is not valid JSON") from e
            if not isinstance(candidate, dict):
                raise ArtifactCorrupt("durable SQL job envelope must contain a JSON object")
            # The payload was bounded and hash-bound before the SQL commit. Rewriting that exact object
            # identity is safe for concurrent replacement supervisors; they all carry identical bytes.
            self._artifacts.write(ref.job_uri, candidate)
            candidate = self._artifacts.read(ref.job_uri)
        if durable_payload is not None and canonical_json(candidate) != durable_payload:
            raise ArtifactContractError(
                "Ray job artifact does not match its durable SQL envelope"
            )
        return candidate

    def _durable_sql_job_envelope(self, ref: RunBackendRef, status: RunStatus) -> dict:
        """Load and validate the hash-bound SQL copy without requiring object-store availability."""
        from hub import metadb

        payload = metadb.backend_job_artifact_payload(status.run_id)
        if payload is None:
            raise ArtifactNotFound("durable SQL job envelope is unavailable")
        try:
            candidate = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ArtifactCorrupt("durable SQL job envelope is not valid JSON") from e
        if not isinstance(candidate, dict):
            raise ArtifactCorrupt("durable SQL job envelope must contain a JSON object")
        if canonical_json(candidate) != payload:
            raise ArtifactCorrupt("durable SQL job envelope is not canonical JSON")
        self._validate_job_artifact_integrity(ref, status, candidate)
        self._validate_jobs_source_pins(candidate)
        self._validate_jobs_sink_attempts(candidate)
        return candidate

    def _validate_job_result(self, job: dict, result: dict) -> dict:
        try:
            require_exact_object(result, _JOB_RESULT_FIELDS, label="Ray result artifact")
        except ArtifactCorrupt as e:
            raise ArtifactContractError(str(e)) from e
        try:
            version = int(job.get("contract_version") or 0)
        except (TypeError, ValueError) as e:
            raise ArtifactContractError("unsupported Ray job artifact contract_version") from e
        if version != _JOB_CONTRACT_VERSION:
            raise ArtifactContractError(
                f"unsupported Ray job artifact contract_version {version}"
            )
        if int(result.get("contract_version") or 0) != version:
            raise ArtifactContractError("Ray result contract_version does not match its job")
        try:
            ray_job_envelope_fields(version)
        except ValueError as e:
            raise ArtifactContractError(str(e)) from e
        if result.get("attempt_id") != job["attempt_id"]:
            raise ArtifactContractError("Ray result attempt_id does not match the durable backend binding")
        if result.get("submission_id") != job["submission_id"]:
            raise ArtifactContractError("Ray result submission_id does not match the durable backend binding")
        if result.get("envelope_sha256") != job["envelope_sha256"]:
            raise ArtifactContractError("Ray result envelope hash does not match the submitted job")
        if result.get("status") not in ("done", "failed", "cancelled"):
            raise ArtifactContractError("Ray result artifact is not terminal")
        rows = result.get("rows", 0)
        if isinstance(rows, bool) or not isinstance(rows, int) or not 0 <= rows <= (1 << 63) - 1:
            raise ArtifactContractError("Ray result rows must be a non-negative signed 64-bit integer")
        outputs = result.get("outputs")
        if not isinstance(outputs, list):
            raise ArtifactContractError("Ray result outputs must be a list")
        for key in ("error", "output_uri", "output_table"):
            if result.get(key) is not None and not isinstance(result[key], str):
                raise ArtifactContractError(f"Ray result {key} must be a string or null")
        raw_sink_targets = job.get("sink_targets")
        sink_targets = {} if raw_sink_targets is None else raw_sink_targets
        if not isinstance(sink_targets, dict):
            raise ArtifactContractError("Ray job sink_targets must be an object or null")
        if job.get("materialize_uri"):
            if result["status"] != "done":
                if outputs or result.get("output_uri") or result.get("output_table"):
                    raise ArtifactContractError(
                        "failed/cancelled Ray region results cannot expose partial outputs"
                    )
                return result
            if result.get("error"):
                raise ArtifactContractError("successful Ray results cannot contain an error")
            requested = str(job["materialize_uri"])
            expected_uri = requested[:-len(".parquet")] if requested.lower().endswith(".parquet") else requested
            if result.get("output_uri") != expected_uri or outputs:
                raise ArtifactContractError("Ray region result does not match its materialization target")
            return result

        expected: dict[str, set[tuple[str, str, str]]] = {}
        for step_id, contract in self._validated_sink_contracts(job).items():
            expected[step_id] = {(
                contract["name"], contract["physical_uri"], contract["logical_uri"]
            )}
        actual: dict[str, tuple[str, str, str]] = {}
        normalized_outputs: list[dict[str, str]] = []
        for output in outputs:
            allowed_fields = {"step_id", "name", "uri", "logical_uri"}
            valid_field_sets = (allowed_fields,)
            if (not isinstance(output, dict) or set(output) not in valid_field_sets
                    or not all(output.get(key) for key in ("step_id", "name", "uri"))):
                raise ArtifactContractError("Ray result contains an incomplete catalog output")
            step_id = str(output["step_id"])
            if step_id in actual:
                raise ArtifactContractError(f"Ray result repeats write step '{step_id}'")
            logical_uri = str(output.get("logical_uri") or sink_targets.get(step_id) or "")
            if not logical_uri:
                raise ArtifactContractError("Ray result contains an incomplete logical sink identity")
            actual[step_id] = (str(output["name"]), str(output["uri"]), logical_uri)
            normalized_outputs.append({
                "step_id": step_id, "name": str(output["name"]), "uri": str(output["uri"]),
                "logical_uri": logical_uri,
            })
        if result["status"] != "done":
            # A driver can fail after one immutable sink committed. Keep exact, hash-bound sink evidence
            # private in the result artifact for later cleanup, but never project it into public status or
            # catalog. Unknown/mismatched URIs remain contract corruption.
            if result.get("output_uri") or result.get("output_table"):
                raise ArtifactContractError(
                    "failed/cancelled Ray results cannot expose a primary output"
                )
            if any(step_id not in expected or value not in expected[step_id]
                   for step_id, value in actual.items()):
                raise ArtifactContractError(
                    "Ray partial outputs do not match the hash-bound job sinks"
                )
            return result
        if result.get("error"):
            raise ArtifactContractError("successful Ray results cannot contain an error")
        if set(actual) != set(expected) or any(
                value not in expected[step_id] for step_id, value in actual.items()):
            raise ArtifactContractError("Ray result catalog outputs do not match the hash-bound job sinks")
        if expected:
            pair = (result.get("output_table"), result.get("output_uri"))
            allowed_pairs = {
                (name, uri) for options in expected.values()
                for name, uri, _logical_uri in options
            }
            if pair not in allowed_pairs:
                raise ArtifactContractError("Ray result primary output does not match a hash-bound job sink")
        elif outputs or result.get("output_uri") or result.get("output_table"):
            raise ArtifactContractError("Ray non-sink result returned an unexpected catalog output")
        if normalized_outputs != outputs:
            result = {**result, "outputs": normalized_outputs}
        return result

    def _install_jobs_status(self, status: RunStatus, binding: dict | None = None) -> None:
        run_id = status.run_id
        with self._lock:
            self.runs[run_id] = status
            self._cancel.setdefault(run_id, threading.Event())
            self._settled.setdefault(run_id, threading.Event())
            if binding:
                self._backend_refs[run_id] = dict(binding)
                if binding.get("cancel_requested"):
                    self._cancel[run_id].set()
            if status.status in ("done", "failed", "cancelled"):
                self._settled[run_id].set()
        # Status/cancel routing is normally installed by start_run. Recovery happens during plugin loading,
        # so it must restore the same owner index explicitly for this replacement process.
        if hasattr(self.deps, "run_index"):
            self.deps.run_index[run_id] = self
        if status.status in ("done", "failed", "cancelled"):
            self._prune_terminal_runs()

    def _prune_terminal_runs(self) -> None:
        from hub.plugins.runner import _MAX_RUNS

        terminal = {"done", "failed", "cancelled"}
        with self._lock:
            for run_id, status in list(self.runs.items()):
                if status.status in terminal:
                    self._cancel.pop(run_id, None)
            while len(self.runs) > _MAX_RUNS:
                victim = next((rid for rid, status in self.runs.items() if status.status in terminal), None)
                if victim is None:
                    break
                self.runs.pop(victim, None)
                self._cancel.pop(victim, None)
                self._settled.pop(victim, None)
                self._backend_refs.pop(victim, None)
                self._control_observed_monotonic.pop(victim, None)
                self._recovery_blocked.discard(victim)
                self._cancel_stop_sent.discard(victim)
                if getattr(self.deps, "run_index", {}).get(victim) is self:
                    self.deps.run_index.pop(victim, None)

    def _ensure_jobs_supervisor(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._supervising:
                return True
            self._supervising.add(run_id)
        try:
            threading.Thread(target=self._supervise_jobs, args=(run_id,), daemon=True,
                             name=f"dp-ray-job-{run_id}").start()
            return True
        except Exception as exc:  # SQL binding already owns this live run; a later status poll retries
            with self._lock:
                self._supervising.discard(run_id)
            status = self.runs.get(run_id)
            if status is not None and status.status in ("queued", "running"):
                status.error = (
                    "Ray Jobs supervisor unavailable; retrying "
                    f"(code=supervisor_start_failed,type={type(exc).__name__})"
                )
                try:
                    from hub import metadb
                    metadb.save_run_state(run_id, status.model_dump())
                except Exception:  # the next status/recovery pass still retries process-locally
                    pass
            return False

    def _recover_jobs(self) -> None:
        from hub import metadb

        for ref, doc in metadb.active_backend_jobs(_JOBS_BACKEND):
            try:
                effects_ready = (
                    ref.get("publication_state") == "effects_started"
                    and isinstance(ref.get("publication_effects"), dict)
                )
                if ref.get("recovery_blocked_reason") and not effects_ready:
                    raise RuntimeError(str(ref["recovery_blocked_reason"]))
                if ref.get("_recovery_error") and not effects_ready:
                    raise ValueError(str(ref["_recovery_error"]))
                if doc.get("_recovery_error"):
                    if not effects_ready:
                        raise ValueError(str(doc["_recovery_error"]))
                    staged_terminal = ref["publication_effects"]["terminal_status"]
                    live_status = str(doc.get("status") or "queued")
                    if live_status not in ("queued", "running"):
                        live_status = "queued"
                    status = RunStatus(
                        run_id=str(ref["run_id"]), status=live_status,
                        placement="distributed", per_node=[],
                        target_node_id=staged_terminal.get("target_node_id"),
                        error="Ray terminal publication recovering",
                    )
                else:
                    status = RunStatus.model_validate(doc)
                status.backend_ref = self._ref_model(ref)
                self._install_jobs_status(status, ref)
                self._ensure_jobs_supervisor(status.run_id)
            except Exception as exc:  # noqa: BLE001 — isolate and surface each damaged active row
                run_id = str(ref.get("run_id") or doc.get("run_id") or "")
                if not run_id:
                    log.warning(
                        "ray_jobs_recovery_blocked missing_run_id backend=%s error_type=%s",
                        _JOBS_BACKEND, type(exc).__name__,
                    )
                    continue
                reason = self._stable_exception(
                    "Ray Jobs recovery blocked", exc, "recovery_blocked"
                )
                live_status = str(doc.get("status") or "queued")
                if live_status not in ("queued", "running"):
                    live_status = "queued"
                blocked = RunStatus(
                    run_id=run_id, status=live_status, placement="distributed", per_node=[], error=reason,
                    target_node_id=(doc.get("target_node_id")
                                    if isinstance(doc.get("target_node_id"), str) else None),
                )
                try:
                    blocked.backend_ref = self._ref_model(ref)
                except Exception:  # the durable row itself may be the malformed recovery input
                    pass
                if ref.get("cancel_requested"):
                    blocked.error = (
                        f"{reason}; cancellation recorded; repair the malformed durable binding "
                        "before remote stop can resume"
                    )[:2000]
                marked = metadb.mark_backend_recovery_blocked(
                    run_id, _JOBS_BACKEND, blocked.model_dump(), reason
                )
                if not marked:  # a terminal publisher or repair won the race; do not install stale state
                    log.warning(
                        "ray_jobs_recovery_blocked_race run_id=%s backend=%s",
                        run_id, _JOBS_BACKEND,
                        extra={"dataplay_run_id": run_id, "dataplay_backend": _JOBS_BACKEND},
                    )
                    continue
                with self._lock:
                    self.runs[run_id] = blocked
                    self._backend_refs[run_id] = dict(ref)
                    self._cancel.setdefault(run_id, threading.Event())
                    if ref.get("cancel_requested"):
                        self._cancel[run_id].set()
                    self._settled.setdefault(run_id, threading.Event())
                    self._recovery_blocked.add(run_id)
                if hasattr(self.deps, "run_index"):
                    self.deps.run_index[run_id] = self
                log.warning(
                    "ray_jobs_recovery_blocked run_id=%s backend=%s error_type=%s code=%s",
                    run_id, _JOBS_BACKEND, type(exc).__name__, "recovery_blocked",
                    extra={
                        "dataplay_run_id": run_id,
                        "dataplay_backend": _JOBS_BACKEND,
                        "dataplay_error_type": type(exc).__name__,
                    },
                )

    def _reattach_job(self, run_id: str) -> RunStatus | None:
        from hub import metadb

        ref = metadb.backend_job(run_id)
        doc = metadb.get_run_state(run_id)
        if not ref or ref.get("backend") != _JOBS_BACKEND or not doc:
            return None
        status = RunStatus.model_validate(doc)
        status.backend_ref = self._ref_model(ref)
        self._install_jobs_status(status, ref)
        if status.status in ("queued", "running"):
            self._ensure_jobs_supervisor(run_id)
        return status

    # -- PlaceableBackend (region dispatch, Phase C3) ---------------------- #
    # dp_ray advertises ONE synthetic worker labelled engine=ray. place() claims a region ONLY when it
    # explicitly asks for engine=ray (config.requires.labels) — so the cost-based mem policy never
    # silently routes here; a user opts a node into Ray deliberately. reachable_tiers: local Ray shares
    # the fs and can read object storage, so both (a real remote cluster would declare object-only).
    def workers(self) -> list:
        # The hub can't query a live Ray cluster (the driver runs in an isolated subprocess — see the
        # DuckDB×Ray deadlock note), so an operator declares the cluster's shape via env: DP_RAY_GPUS /
        # DP_RAY_GPU_TYPE / DP_RAY_MEM / DP_RAY_LABELS. That advertised capacity feeds the topology view
        # + the run-plan
        # pre-flight ("needs 4×a100 — backends advertise: 8×a100"). Defaults keep the engine=ray label.
        import os
        try:
            gpu = int(os.environ.get("DP_RAY_GPUS", "0") or 0)
        except ValueError:
            gpu = 0  # a mistyped count shouldn't silently drop the whole capacity report
        try:
            cpu = float(os.environ.get("DP_RAY_NUM_CPUS", "0") or 0)
        except ValueError:
            cpu = 0
        cap = ResourceSpec(cpu=cpu or None, mem=os.environ.get("DP_RAY_MEM", "1000GB"),
                           gpu=gpu or None,
                           gpu_type=(_canonical_accelerator_type(os.environ.get("DP_RAY_GPU_TYPE"))
                                     or None) if gpu else None,
                           labels=_advertised_ray_labels())
        return [WorkerInfo(id="ray", capacity=cap, state="idle")]

    def place(self, requires) -> "str | None":
        # RunController's parent orchestration is currently in-memory. Advertising this backend for a
        # production Jobs region would make the sub-job durable while losing the logical parent and all
        # later regions on hub restart. Whole-graph RayRunner.run remains restart-reattachable.
        if self.jobs_address:
            return None
        labels = getattr(requires, "labels", None) or {}
        return "ray" if labels.get("engine") == "ray" else None

    def accepts_whole_graph(self, requires) -> bool:
        """Claim hard Ray pins in Jobs mode without claiming non-durable region orchestration."""
        return bool(self.jobs_address and self._requires_ray(requires))

    @staticmethod
    def preallocate_run_id() -> str:
        """Mint a logical ID without allocating artifacts or contacting Ray."""
        return f"run_{uuid.uuid4().hex}"

    def reachable_tiers(self):
        # A same-host reference cluster (worker-direct LOCAL reads) reaches local + object. But an
        # OFF-HOST cluster's workers can't read the hub's local disk — declaring local there would let the
        # controller route a region handoff to local and silently produce a result the remote workers
        # can't read. So when the operator marks the cluster remote (DP_RAY_REMOTE), reach is object-only,
        # and the controller correctly refuses a handoff with no shared object store.
        remote = bool(self.jobs_address) or os.environ.get("DP_RAY_REMOTE", "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        return ("object",) if remote else ("local", "object")

    def run_unit(self, graph, output_node, output_uri, requires=None, run_id=None) -> RunStatus:
        """Run ONE region's subgraph on Ray and materialize output_node → output_uri (the RunController
        handoff contract). A clean region runs distributed on Ray: reads AND writes worker-direct (each
        block written as its own parquet shard, no driver funnel — output_uri becomes a DIRECTORY of
        shards). `requires` (the planner's resolved region need) is passed to Ray so its map tasks are
        scheduled onto a matching worker. Unsupported unpinned work falls back locally; an explicit Ray
        requirement fails before dispatch."""
        if self.jobs_address:
            raise RuntimeError(
                "Ray Jobs supports durable whole-graph runs only; region orchestration is not yet durable"
            )
        run_id = run_id or f"unit_{uuid.uuid4().hex}"
        ir = lower_to_ir(graph, output_node, self.node_specs, self.deps.node_ir)
        status = RunStatus(run_id=run_id, status="queued", placement="distributed", target_node_id=output_node,
                           per_node=[PerNodeStatus(node_id=output_node, status="queued", label=output_node)])
        req = requires.model_dump() if hasattr(requires, "model_dump") else requires
        with self._lock:
            prior = self.runs.get(run_id)
            if prior is not None:
                return prior  # one in-process owner for an explicit attempt ID
            self.runs[run_id] = status
            self._cancel_ack.discard(run_id)
        reason = self._source_unsupported_reason(graph, output_node, ir)
        reason = reason or self._ray_unsupported_reason(ir)
        reason = reason or self._dedup_unsupported_reason(graph, ir)
        cone = g.upstream_chain(graph, output_node) if output_node else graph.nodes
        reason = reason or self._gpu_type_conflict_reason(graph, cone)
        reason = reason or self._resource_unsupported_reason(requires, ir)
        if _remote_ray():
            from hub.plugins.adapters import is_object_uri
            if not is_object_uri(output_uri):
                reason = reason or (
                    "a remote Ray cluster cannot materialize a region on the hub's local filesystem; "
                    "configure a shared object storage tier"
                )
        # Reject an explicitly pinned unsupported graph before allocation. No writer can start on this
        # path, so minting a durable writing generation/write lease would create a permanent false owner.
        if reason and self._requires_ray(requires, graph, output_node):
            return self._unsupported_status(graph, output_node, reason, run_id=run_id)
        try:
            attempt_uri = _allocate_handoff_uri(output_uri, run_id, "region")
        except Exception as e:  # noqa: BLE001 — a control-plane allocation must precede object writes
            status.status = "failed"
            status.error = self._stable_exception(
                "Object attempt allocation failed", e, "object_attempt_allocation_failed"
            )
            for item in status.per_node:
                item.status = "failed"
            return status
        committed = read_manifest(attempt_uri)
        if (committed is not None and committed.get("runId") == run_id
                and validate_shards(attempt_uri, committed)
                and self.base._output_exists(attempt_uri)):
            status.status, status.output_uri = "done", attempt_uri
            status.rows_processed = status.total_rows = int(committed["rows"])
            status.progress = 1.0
            for item in status.per_node:
                item.status = "done"
            return status
        if attempt_has_commit_record(attempt_uri) or attempt_has_contents(attempt_uri):
            status.status = "failed"
            status.error = (
                "Ray attempt prefix already exists without an exact committed inventory; "
                "refusing to overwrite an immutable or possibly live attempt")
            for item in status.per_node:
                item.status = "failed"
            return status
        if reason:
            return self._materialize_local(  # non-clean → local engine, same reserved status/attempt
                graph, output_node, attempt_uri, run_id, status=status)
        with self._lock:
            self._cancel[run_id] = threading.Event()
        # Never let retries/concurrent attempts overwrite the same multi-object prefix. The controller
        # caches the returned URI only after this sub-run is terminal, so this physical attempt prefix is
        # the atomic publication boundary; an interrupted attempt can leave only an unreferenced orphan.
        threading.Thread(target=self._supervise, args=(run_id, graph, output_node, status),
                         kwargs={"materialize_uri": attempt_uri, "requires": req}, daemon=True).start()
        return status

    def _materialize_local(self, graph, output_node, attempt_uri, run_id=None, status=None) -> RunStatus:
        """Non-clean region fallback under the same immutable handoff contract as distributed Ray.

        The fallback is synchronous and uses DuckDB only, but it still writes one unique attempt prefix,
        then a success manifest. Writing the stable controller suggestion directly would let concurrent
        retries truncate each other even though the distributed path is immutable.
        """
        from hub.executors.engine import BuildEngine
        from hub.plugins.adapters import is_object_uri
        run_id = run_id or f"unit_{uuid.uuid4().hex}"
        status = status or RunStatus(
            run_id=run_id, status="running", placement="local", target_node_id=output_node,
            per_node=[PerNodeStatus(node_id=output_node, status="running", label=output_node)])
        status.status, status.placement = "running", "local"
        for item in status.per_node:
            item.status = "running"
        with self._lock:
            self.runs.setdefault(run_id, status)
        owns_prefix = False
        try:
            committed = read_manifest(attempt_uri)
            if (committed is not None and committed.get("runId") == run_id
                    and validate_shards(attempt_uri, committed)
                    and self.base._output_exists(attempt_uri)):
                status.status, status.output_uri = "done", attempt_uri
                status.rows_processed = status.total_rows = int(committed["rows"])
                status.progress = 1.0
                for p in status.per_node:
                    p.status = "done"
                return status
            if attempt_has_commit_record(attempt_uri) or attempt_has_contents(attempt_uri):
                raise RuntimeError(
                    "attempt prefix already exists without an exact committed inventory; use a new run ID")
            owns_prefix = True
            from hub.storage import local_result_read_scope
            with local_result_read_scope(
                    self.deps.storage, g.all_upstream_source_uris(graph, output_node),
                    owner=f"ray-local-fallback:{run_id}"):
                with db.run_scope():
                    if is_object_uri(attempt_uri):
                        db.ensure_object_store()
                    eng = BuildEngine(graph, self.resolve_adapter, self.deps.registry, full=True,
                                      node_builders=self.deps.node_builders, node_specs=self.node_specs,
                                      output_node=output_node)
                    rel = eng.relation(output_node)
                    data_uri = attempt_uri.rstrip("/") + "/part-00000.parquet"
                    result = self.base._adapter_write(
                        self.resolve_adapter(data_uri), data_uri, rel, "overwrite", threading.Event())
                    schema = list(zip(rel.columns, (str(t) for t in rel.types)))
                    write_manifest(
                        attempt_uri, run_id=run_id, rows=int(result.get("rows") or 0), schema=schema)
            status.status, status.output_uri = "done", attempt_uri
            status.rows_processed = status.total_rows = int(result.get("rows") or 0)
            status.progress = 1.0
        except Exception as e:  # noqa: BLE001
            if owns_prefix:
                discard_attempt(attempt_uri)  # synchronous writer stopped; safe to remove only our prefix
            status.status = "failed"
            status.error = self._stable_exception(
                "Ray local materialization failed", e, "local_materialization_failed"
            )
        for p in status.per_node:
            p.status = status.status
        return status

    def _ray_unsupported_reason(self, ir) -> str | None:
        policy_con = _secure_duckdb_connection()
        try:
            return self._ray_unsupported_reason_with_connection(ir, policy_con)
        finally:
            policy_con.close()

    def _ray_unsupported_reason_with_connection(self, ir, policy_con) -> str | None:
        # (1) every step is clean OR a claimed relational op; (2) every clean transform carries inlined
        # code (a Ray worker has no access to the driver's processor registry); (3) every aggregate has a
        # GROUPED, bare-column key we can hash-shuffle on (a global aggregate — empty keys — or an
        # expression key has no shuffle key → DuckDB single-node, which is cheap for a global reduce).
        if not ir.is_distributable(RAY_RELATIONAL):
            unsupported = [f"{step.id}:{step.op}" for step in ir.steps
                           if step.op not in CLEAN_OPS and step.op not in RAY_RELATIONAL]
            return "unsupported operator(s): " + ", ".join(unsupported or ["empty graph"])
        missing_code = [s.id for s in ir.steps
                        if s.op in CLEAN_TRANSFORM_MODES and not s.config.get("code")]
        if missing_code:
            return "worker-portable transform code is missing for node(s): " + ", ".join(missing_code)
        enforced = [s.id for s in ir.steps
                    if s.op in CLEAN_TRANSFORM_MODES and s.config.get("enforceSchema") is True]
        if enforced:
            return (
                "distributed schema enforcement is not implemented for transform node(s): "
                + ", ".join(enforced)
            )
        for s in ir.steps:
            if s.op == "write":
                try:
                    SinkSpec.from_config(s.config, s.config.get("title"))
                except (TypeError, ValueError) as exc:
                    return (
                        "write node has unsupported sink semantics "
                        f"(code=sink_semantics_unsupported,type={type(exc).__name__})"
                    )
            if s.op == "aggregate":
                if not parse_group_keys(s.config.get("groupBy", "")):
                    return f"aggregate node '{s.id}' needs a non-empty bare-column GROUP BY"
                try:
                    validate_fragment(
                        FragmentKind.GROUP_BY, s.config.get("groupBy", ""), con=policy_con
                    )
                    validate_fragment(
                        FragmentKind.AGGREGATES,
                        s.config.get("aggs") or "count(*) AS n",
                        con=policy_con,
                    )
                except SQLPolicyError as exc:
                    return f"aggregate node '{s.id}' violates SQL policy: {exc}"
                # pass the EFFECTIVE aggs (default matches _build_aggregate) so a node with no aggs isn't
                # spuriously rejected by the empty-fragment conservative default.
                if agg_has_order_sensitive(s.config.get("aggs") or "count(*) AS n"):
                    return f"aggregate node '{s.id}' contains an order-sensitive aggregate"
            if s.op == "window":
                if not parse_group_keys(s.config.get("partitionBy", "")):
                    return f"window node '{s.id}' needs a bare-column PARTITION BY"
                expr = s.config.get("expr", "")
                try:
                    validate_fragment(
                        FragmentKind.GROUP_BY, s.config.get("partitionBy", ""), con=policy_con
                    )
                    validate_fragment(FragmentKind.WINDOW_EXPR, expr, con=policy_con)
                    if (s.config.get("orderBy") or "").strip():
                        validate_fragment(
                            FragmentKind.ORDER_BY, s.config.get("orderBy"), con=policy_con
                        )
                except SQLPolicyError as exc:
                    return f"window node '{s.id}' violates SQL policy: {exc}"
                if agg_has_order_sensitive(expr):
                    return f"window node '{s.id}' contains an order-sensitive aggregate"
                if window_needs_order(expr) and not (s.config.get("orderBy") or "").strip():
                    return f"window node '{s.id}' needs ORDER BY for deterministic distributed execution"
            if s.op == "dedup" and (s.config.get("on") or "").strip():
                return f"dedup node '{s.id}' uses order-dependent keyed DISTINCT"
            if s.op == "join":
                from hub.executors.engine import normalize_how
                if normalize_how(s.config.get("how", "")) not in ("inner", "left", "cross"):
                    return f"join node '{s.id}' uses an unsupported right/full broadcast join"
                condition = (s.config.get("condition") or "").strip()
                if condition:
                    try:
                        validate_fragment(FragmentKind.JOIN_ON, condition, con=policy_con)
                    except SQLPolicyError as exc:
                        return f"join node '{s.id}' violates SQL policy: {exc}"
            if s.op == "sort" and parse_sort_keys(s.config.get("by", "")) is None:
                return f"sort node '{s.id}' needs a non-empty bare-column sort key"
        return None

    def _ray_runnable(self, ir) -> bool:
        return self._ray_unsupported_reason(ir) is None

    @staticmethod
    def _requires_ray(requires, graph=None, target=None) -> bool:
        raw = requires.model_dump() if hasattr(requires, "model_dump") else (requires or {})
        if raw.get("gpu") or raw.get("gpu_type") or raw.get("gpuType") \
                or (raw.get("labels") or {}).get("engine") == "ray":
            return True
        if graph is None:
            return False
        nodes = g.upstream_chain(graph, target) if target else graph.nodes
        return any(((node.data.get("config", {}).get("requires", {}).get("labels", {})
                     if isinstance(node.data, dict) else {}).get("engine") == "ray") for node in nodes)

    def _resource_unsupported_reason(self, requires, ir=None) -> str | None:
        if requires is None:
            return None
        try:
            req = requires if isinstance(requires, ResourceSpec) else ResourceSpec(**requires)
        except Exception as exc:  # noqa: BLE001
            return (
                "invalid Ray resource requirement "
                f"(code=resource_requirement_invalid,type={type(exc).__name__})"
            )
        if req.gpu_type:
            req = req.model_copy(update={"gpu_type": _canonical_accelerator_type(req.gpu_type)})
        workers = self.workers()
        if not any(satisfies(worker.capacity, req) for worker in workers):
            wanted = req.model_dump(by_alias=True, exclude_none=True)
            offered = [worker.capacity.model_dump(by_alias=True, exclude_none=True) for worker in workers]
            return f"requested resources {wanted} exceed advertised Ray capacity {offered}"
        labels = {k: v for k, v in (req.labels or {}).items() if k != "engine"}
        gpu_pinned = bool(req.gpu or req.gpu_type)
        if gpu_pinned:
            try:
                _gpu_batch_rows()
            except ValueError as exc:
                return str(exc)
        if ir is not None and gpu_pinned:
            whole_partition = sorted({
                step.op for step in ir.steps if step.op in {"aggregate", "window", "dedup"}
            })
            if whole_partition:
                # These operators deliberately run one complete hash partition per DuckDB UDF call.
                # Giving that call a GPU claim while leaving batch_size=None is an unbounded-memory lie;
                # splitting it into finite GPU batches would break group/window/distinct correctness.
                return (
                    "GPU placement cannot safely batch whole-partition operator(s): "
                    + ", ".join(whole_partition)
                )
        if ir is not None and (req.gpu or req.gpu_type or labels) and any(
                step.op == "sort" for step in ir.steps):
            # Ray 2.56 Dataset.sort/repartition expose no ray_remote_args. Claiming the region while its
            # range-shuffle ignores the requested GPU/custom pool would be false placement.
            return "sort cannot honor GPU/custom-resource placement with the supported Ray 2.56 API"
        return None

    def _source_unsupported_reason(self, graph, target, ir) -> str | None:
        """Preflight every read before a Ray subprocess can touch data.

        Native reads are reserved for the exact built-in adapter and require bounded fragment/footer/layout
        proof. Everything else must have a known-small built-in streaming path; custom adapter semantics,
        object IPC's eager download, and unbounded/unknown inputs fall back or fail before dispatch.
        """
        from hub.plugins.adapters import is_object_uri

        classify_local = getattr(self.deps.storage, "requires_result_read", None)
        if callable(classify_local):
            for uri in g.execution_source_uris(graph, target):
                try:
                    if not classify_local(uri):
                        continue
                except Exception as exc:  # an alias must fail before any schema/dedup probe
                    return f"source '{uri}' is an invalid managed local-result alias: {exc}"
                return (
                    f"source '{uri}' is a managed local full result; Ray dispatch cannot inherit its "
                    "exact POSIX read fence, so use the local backend or shared object storage"
                )
        for step in ir.steps:
            if step.op != "read":
                continue
            uri = step.config.get("uri")
            if not uri:
                return f"read node '{step.id}' has no physical URI"
            try:
                adapter = self.resolve_adapter(uri)
            except Exception as exc:  # noqa: BLE001
                return (
                    "Ray source adapter resolution failed "
                    f"(code=source_adapter_unavailable,type={type(exc).__name__})"
                )
            if not _is_builtin_adapter(adapter):
                return (
                    f"source '{uri}' is claimed by adapter '{type(adapter).__name__}', which has no explicit "
                    "bounded/distributed Ray capability"
                )
            if not (_remote_ray() and not is_object_uri(uri)):
                try:
                    if _native_parquet_plan(uri, adapter) is not None:
                        continue
                except RuntimeError:
                    # A schema/layout proof failure may still use the exact built-in adapter, but only under
                    # the small driver-streaming ceiling checked below.
                    pass
            if not _bounded_builtin_source_supported(uri):
                return (
                    f"source '{uri}' has no bounded Ray driver-streaming contract; use shared Parquet, "
                    "a native distributed connector, or the local backend"
                )
            try:
                _require_driver_fallback(_physical_source_bytes(uri), f"source '{uri}'")
            except RuntimeError as exc:
                return str(exc)

    def _gpu_type_conflict_reason(self, graph, nodes) -> str | None:
        types = {
            _canonical_accelerator_type(requirement.gpu_type)
            for node in nodes
            for requirement in [node_requires(node, self.node_specs)]
            if requirement is not None and requirement.gpu_type
        }
        if len(types) > 1:
            return (
                "one Ray execution region cannot satisfy multiple GPU types: "
                + ", ".join(sorted(types))
            )
        return None

    def _unsupported_status(self, graph, target, reason, *, run_id=None, plan=None) -> RunStatus:
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        per_node = ([PerNodeStatus(node_id=s.node_id, status="failed", label=s.label) for s in plan.steps]
                    if plan is not None else
                    [PerNodeStatus(node_id=target, status="failed", label=target)])
        status = RunStatus(
            run_id=run_id,
            status="failed",
            placement="distributed",
            target_node_id=target,
            per_node=per_node,
            error=f"Ray execution was explicitly required but is unsupported: {reason}",
        )
        with self._lock:
            self.runs[run_id] = status
        self._emit(graph, status)
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001
                pass
        return status

    def _resolve_sink_targets(self, ir) -> dict[str, str]:
        """Resolve and validate logical sinks on the hub, before the isolated driver is dispatched."""
        from hub.plugins.adapters import is_object_uri

        targets: dict[str, str] = {}
        for step in ir.steps:
            if step.op == "write":
                spec = SinkSpec.from_config(step.config, step.config.get("title"))
                uri = preflight_sink(
                    spec, self.deps.workspace, self.base.storage, self.resolve_adapter
                )
                adapter = self.resolve_adapter(uri)
                worker_direct = _worker_direct_parquet_sink(spec, uri, adapter)
                if worker_direct and _remote_ray() and not is_object_uri(uri):
                    raise RuntimeError(
                        "a remote Ray cluster requires an object-storage destination for worker-direct "
                        "Parquet output; configure DP_STORAGE_URL/destId or run this graph locally"
                    )
                targets[step.id] = uri
        return targets

    def _claim_sink_attempts(
            self, ir, targets: dict[str, str], run_id: str, *,
            require_live_preallocation: bool = False) -> dict[str, str]:
        """Precompute and register worker-direct sink attempts in the hub control plane.

        The isolated driver intentionally has a private metadata DB, so it must receive the exact physical
        URI instead of minting/registering one after dispatch. If a later claim fails, unwind the claims
        already made; no writer has started yet.
        """
        if len(targets) > 1:
            raise RuntimeError(
                "multiple Ray write sinks require atomic batch publication, which is not enabled")

        managed_steps = []
        unmanaged_steps = []
        for step in ir.steps:
            if step.op != "write":
                continue
            target_uri = targets[step.id]
            spec = SinkSpec.from_config(step.config, step.config.get("title"))
            if _worker_direct_parquet_sink(spec, target_uri, self.resolve_adapter(target_uri)):
                managed_steps.append((step, target_uri, spec))
            else:
                unmanaged_steps.append(step)
        from hub.plugins.catalog import core_managed_publisher, unmanaged_publication_supported
        if managed_steps and core_managed_publisher(self.catalog) is None:
            raise RuntimeError(
                "managed object writes require the core transactional catalog publisher")
        if unmanaged_steps and not unmanaged_publication_supported(self.catalog):
            raise RuntimeError(
                "unmanaged Ray writes require catalog registration with read-back support")

        attempts: dict[str, str] = {}
        try:
            for step, target_uri, spec in managed_steps:
                attempt_uri = _allocate_handoff_uri(
                    target_uri, run_id, "sink", scope=step.id,
                    catalog_key_base=f"tbl_{spec.name}",
                    require_live_preallocation=require_live_preallocation)
                attempts[step.id] = attempt_uri
            return attempts
        except Exception:
            for attempt_uri in attempts.values():
                discard_attempt(attempt_uri)
            raise

    def _freeze_jobs_sink_contracts(
            self, ir, targets: dict[str, str], run_id: str,
            sink_attempts: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
        """Freeze allocated writer identities before a durable Jobs attempt is bound."""
        sink_attempts = sink_attempts or {}
        contracts: dict[str, dict[str, str]] = {}
        for step in ir.steps:
            if step.op != "write":
                continue
            logical_uri = targets[step.id]
            spec = SinkSpec.from_config(step.config, step.config.get("title"))
            direct = _worker_direct_parquet_sink(
                spec, logical_uri, self.resolve_adapter(logical_uri)
            )
            if not direct:
                raise RuntimeError(
                    f"Ray Jobs write sink '{step.id}' is not a hub-managed direct Parquet sink"
                )
            if step.id not in sink_attempts:
                raise RuntimeError(
                    f"Ray Jobs direct sink '{step.id}' has no allocated object attempt"
                )
            contracts[step.id] = {
                "name": spec.name,
                "logical_uri": logical_uri,
                "physical_uri": sink_attempts[step.id],
                "writer": "worker-direct-parquet",
            }
        if set(sink_attempts) != set(contracts):
            raise RuntimeError("Ray Jobs sink allocation set does not match its frozen writer set")
        self._validate_jobs_sink_attempts({
            "run_id": run_id, "sink_targets": targets, "sink_contracts": contracts,
        }, allowed_states={"writing"})
        return contracts

    @staticmethod
    def _validate_jobs_sink_attempts(
            job: dict, *, allowed_states: set[str] | None = None) -> dict[str, dict]:
        """Resolve frozen direct sinks through the exact allocation key without reopening writers."""
        handles: dict[str, dict] = {}
        for step_id, contract in RayRunner._validated_sink_contracts(job).items():
            if contract.get("writer") != "worker-direct-parquet":
                continue
            allocation_key = _attempt_allocation_key(
                contract["logical_uri"], job["run_id"], "sink", step_id
            )
            try:
                handle = lookup_attempt(
                    logical_uri=contract["logical_uri"], kind="sink",
                    run_id=job["run_id"], allocation_key=allocation_key,
                )
            except Exception as e:  # registry/provider identity failure is contract failure
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' could not verify its object attempt"
                ) from e
            if handle is None:
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' has no object-attempt allocation"
                )
            expected = {
                "uri": contract["physical_uri"],
                "logical_uri": contract["logical_uri"],
                "kind": "sink",
                "run_id": job["run_id"],
                "allocation_key": allocation_key,
            }
            if any(handle.get(key) != value for key, value in expected.items()):
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' changed its allocated object identity"
                )
            if allowed_states is not None and handle.get("state") not in allowed_states:
                raise ArtifactContractError(
                    f"Ray job sink contract '{step_id}' is not writable "
                    f"(state={handle.get('state')})"
                )
            handles[step_id] = handle
        return handles

    def _prepare_jobs_submission(self, job: dict) -> None:
        """Gate replay on current code and exact still-writable parent-owned attempts."""
        self._validate_job_reattach_config(job)
        self._validate_jobs_source_pins(job)
        self._validate_jobs_sink_attempts(job, allowed_states={"writing"})

    def _sink_targets_runnable(self, ir) -> bool:
        try:
            self._resolve_sink_targets(ir)
            return True
        except Exception:  # noqa: BLE001 — unknown destination/incompatible adapter ⇒ safe local fallback
            return False

    def _dedup_unsupported_reason(self, graph, ir) -> str | None:
        """Full-row dedup shuffles by ALL columns so identical rows colocate, then DuckDB DISTINCT per
        partition. But Ray's hash-shuffle equality is RAW-BYTE, which distinguishes values DuckDB DISTINCT
        coalesces: -0.0 vs 0.0, and distinct NaN bit-patterns. So two rows differing only in signed-zero /
        NaN-payload hash to DIFFERENT partitions and BOTH survive → one extra row vs single-node DuckDB.
        Fall back to the single-node engine whenever a dedup's schema carries any floating-point column,
        SCALAR OR NESTED. Inspects the RAW DuckDB column types (`rel.types`) — NOT the display type, which
        normalizes STRUCT/MAP/LIST to a bare `struct`/`map`/`list` and would hide a nested double (and maps
        DECIMAL→float, wrongly forcing an exact-decimal dedup local). Needs the schema (the IR carries
        none), so it's separate from the config-only _ray_runnable gate; only paid when a dedup is present."""
        dedups = [s for s in ir.steps if s.op == "dedup"]
        if not dedups:
            return None
        from hub.executors.engine import BuildEngine
        # a raw DuckDB type string carries nested element types (STRUCT(a DOUBLE), DOUBLE[], MAP(…, DOUBLE))
        # so this catches nested floats; DECIMAL(…) / HUGEINT don't match, so exact types still distribute.
        float_re = re.compile(r"\b(?:float|double|real)\b", re.I)
        for s in dedups:
            try:
                with db.run_scope():
                    rel = BuildEngine(graph, self.resolve_adapter, self.deps.registry, full=True,
                                      node_builders=self.deps.node_builders, node_specs=self.node_specs,
                                      output_node=s.id).relation(s.id)
                    types = [str(t) for t in rel.types]  # RAW DuckDB types (schema-only; no data scan)
            except Exception:  # noqa: BLE001 — can't prove the schema is float-free → don't distribute (safe)
                return f"could not prove dedup node '{s.id}' has Ray-safe equality semantics"
            if any(float_re.search(t) for t in types):
                return f"dedup node '{s.id}' contains floating-point values with incompatible hash equality"
        return None

    def _dedup_needs_single_node(self, graph, ir) -> bool:
        return self._dedup_unsupported_reason(graph, ir) is not None

    def run(self, plan, graph, target_node_id, placement, run_id=None) -> RunStatus:
        from hub.placement import graph_requires

        ir = lower_to_ir(graph, target_node_id, self.node_specs, self.deps.node_ir)
        reason = self._source_unsupported_reason(graph, target_node_id, ir)
        reason = reason or self._ray_unsupported_reason(ir)
        reason = reason or self._dedup_unsupported_reason(graph, ir)
        # A final placed region reaches this whole-backend seam (not run_unit). Aggregate the target cone's
        # requirements here so it gets the same fail-loud admission and Ray task options as an intermediate
        # region; otherwise final GPU/custom-resource pins silently bypass placement enforcement.
        cone = g.upstream_chain(graph, target_node_id) if target_node_id else graph.nodes
        requires = graph_requires(graph, self.node_specs, nodes=cone)
        reason = reason or self._gpu_type_conflict_reason(graph, cone)
        reason = reason or self._resource_unsupported_reason(requires, ir)
        if reason and self._requires_ray(requires, graph, target_node_id):
            return self._unsupported_status(graph, target_node_id, reason, run_id=run_id, plan=plan)
        if reason:
            return self.base.run(plan, graph, target_node_id, placement, run_id=run_id)  # safe fallback
        try:
            if self.jobs_address:
                # Production mode must fail closed. Falling back locally because a remote sink/config is invalid
                # would violate an explicit Ray placement request and hide a deployment error.
                self._jobs_contract()
                sink_targets = self._resolve_sink_targets(ir)
                self._validate_jobs_io(ir, sink_targets=sink_targets)
                self._validate_jobs_catalog_publication(sink_targets)
                source_attempts = self._jobs_source_attempts(graph, target_node_id)
            else:
                sink_targets = self._resolve_sink_targets(ir)
        except Exception as exc:  # noqa: BLE001 — resolve/adapter uncertainty ⇒ local or explicit failure
            if self.jobs_address:
                raise
            logging.getLogger(__name__).exception("Ray sink preflight failed")
            if self._requires_ray(requires, graph, target_node_id):
                return self._unsupported_status(
                    graph, target_node_id,
                    self._stable_exception(
                        "Ray sink preflight failed", exc, "sink_preflight_failed"
                    ),
                    run_id=run_id, plan=plan,
                )
            return self.base.run(plan, graph, target_node_id, placement, run_id=run_id)
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        per_node = [PerNodeStatus(node_id=s.node_id, status="queued", label=s.label) for s in plan.steps]
        status = RunStatus(run_id=run_id, status="queued", placement="distributed", per_node=per_node,
                           target_node_id=target_node_id)
        if self.jobs_address:
            sink_attempts = self._claim_sink_attempts(
                ir, sink_targets, run_id, require_live_preallocation=True
            )
            # The router's leased run preallocation owns allocation-to-bind rollback. It can atomically
            # prove that no backend row exists and terminalize every attempt for this run; doing that here
            # with a check-then-cleanup would race another hub binding the same logical run.
            sink_contracts = self._freeze_jobs_sink_contracts(
                ir, sink_targets, run_id, sink_attempts
            )
            return self._start_jobs(
                status, graph, target_node_id, sink_targets=sink_targets,
                sink_contracts=sink_contracts, source_attempts=source_attempts,
                requires=requires.model_dump(),
            )
        with self._lock:
            self.runs[run_id] = status
            self._cancel[run_id] = threading.Event()
            self._cancel_ack.discard(run_id)
        self._emit(graph, status)
        try:
            sink_attempts = self._claim_sink_attempts(ir, sink_targets, run_id)
        except Exception as exc:  # noqa: BLE001 — never dispatch an object write the hub cannot track
            logging.getLogger(__name__).exception("Ray sink control-plane setup failed")
            status.status = "failed"
            status.error = self._stable_exception(
                "Object sink attempt allocation failed", exc, "sink_attempt_allocation_failed"
            )
            for item in status.per_node:
                item.status = "failed"
            self._emit(graph, status)
            with self._lock:
                self._cancel.pop(run_id, None)
            if self.on_complete:
                try:
                    self.on_complete(graph, target_node_id, status)
                except Exception:  # noqa: BLE001
                    pass
            return status
        # PROCESS ISOLATION: run Ray in a fresh subprocess (its main thread inits Ray BEFORE any DuckDB),
        # so the app's shared DuckDB connection never coexists with Ray in one process. The parent only
        # spawns + polls a status file (no DuckDB here), so it can't deadlock. (Ray inline in-process
        # deadlocks against the shared DuckDB connection — see the module docstring.)
        threading.Thread(target=self._supervise, args=(run_id, graph, target_node_id, status),
                         kwargs={"requires": requires.model_dump(), "sink_targets": sink_targets,
                                 "sink_attempts": sink_attempts},
                         daemon=True).start()
        return status

    def _start_jobs(self, status: RunStatus, graph, target, *, sink_targets=None, sink_contracts=None,
                    source_attempts=None, materialize_uri=None, requires=None) -> RunStatus:
        """Bind the hash-bound job contract before materializing or submitting its deterministic ID."""
        from hub import metadb

        ref, job = self._make_jobs_artifacts(
            status.run_id, graph, target, sink_targets=sink_targets,
            sink_contracts=sink_contracts, source_attempts=source_attempts,
            materialize_uri=materialize_uri, requires=requires,
        )
        self._jobs_client(ref["control_address"])  # validate SDK + endpoint before SQL ownership
        job_payload = json_artifact_payload(job, label="Ray job artifact")
        status.backend_ref = self._ref_model(ref)
        stored, created = metadb.bind_backend_job(
            status.run_id, ref, status.model_dump(), canvas_id=getattr(graph, "id", None),
            job_payload=job_payload, source_uris=job["source_attempts"],
        )
        # The SQL commit is the ownership handoff. From this point onward no object-store read/write may
        # escape back to the caller: the locally installed supervisor owns materialization, retries, and
        # cancellation. A racing duplicate simply reattaches the stored canonical binding/payload.
        persisted = metadb.get_run_state(status.run_id)
        current = self.runs.get(status.run_id)
        if not created and current is not None:
            # An idempotent duplicate in this process must not replace the object the active supervisor is
            # mutating; doing so would strand callers on a stale queued copy while the old copy completes.
            status = current
        elif not created and persisted:
            status = RunStatus.model_validate(persisted)
        status.backend_ref = self._ref_model(stored)
        self._install_jobs_status(status, stored)
        if status.status in ("queued", "running"):
            # Persist backend_ref before the asynchronous submit. This is the restart handoff point.
            self._emit(graph, status)
            self._ensure_jobs_supervisor(status.run_id)
        return status

    def _emit(self, graph, status) -> None:
        if self.on_status:
            try:
                self.on_status(graph, status)
            except Exception:  # noqa: BLE001 — never let persistence break a run
                pass

    def _register_outputs(self, graph, result, job: dict | None = None, *,
                          expected_targets=None, expected_attempts=None) -> None:
        """Publish driver-written outputs through the hub-owned catalog/control plane."""
        from hub.plugins.adapters import is_object_uri
        from hub.plugins.catalog import (
            core_managed_publisher,
            publish_unmanaged_output_attested,
        )
        durable_catalog = self.catalog if job and isinstance(
            self.catalog, DurableCatalogPublisher
        ) else None
        register_once = getattr(durable_catalog, "register_output_idempotent", None)
        usage_once = getattr(durable_catalog, "record_usage_idempotent", None)
        if job and durable_catalog is None:
            raise RuntimeError(
                "Ray Jobs requires the DurableCatalogPublisher capability: "
                "register_output_idempotent(...) and record_usage_idempotent(idempotency_key, parents)"
            )
        nodes = {node.id: node for node in graph.nodes}
        outputs = result.get("outputs") or []
        if expected_targets is not None:
            returned = {str(output.get("step_id")) for output in outputs if output.get("step_id")}
            if len(returned) != len(outputs) or returned != set(expected_targets):
                raise RuntimeError("ray driver returned an incomplete or unexpected sink set")
        for output in outputs:
            step_id, name, uri = output.get("step_id"), output.get("name"), output.get("uri")
            logical_uri = output.get("logical_uri")
            if not (step_id and name and uri):
                raise RuntimeError("ray driver returned an incomplete sink result")
            if expected_targets is not None and logical_uri != expected_targets.get(step_id):
                raise RuntimeError(f"ray driver returned an unexpected logical URI for sink '{step_id}'")
            expected_uri = (expected_attempts or {}).get(step_id)
            if expected_uri is not None and uri != expected_uri:
                raise RuntimeError(f"ray driver returned an unexpected attempt URI for sink '{step_id}'")
            node = nodes.get(step_id)
            if node is None or node.type != "write":
                raise RuntimeError(f"ray driver returned an unknown sink '{step_id}'")
            spec = SinkSpec.from_config(node.data.get("config") or {}, node.data.get("title"))
            if name != spec.name:
                raise RuntimeError(f"ray driver returned an unexpected name for sink '{step_id}'")
            if expected_targets is not None and expected_uri is None:
                target_uri = expected_targets.get(step_id)
                published_uri = expected_sink_uri(
                    spec, target_uri, self.resolve_adapter(target_uri))
                if uri != published_uri:
                    raise RuntimeError(
                        f"ray driver returned an unexpected physical URI for sink '{step_id}'")

        run_parents: list[str] = []
        for output in outputs:
            step_id, name, uri = output["step_id"], output["name"], output["uri"]
            logical_uri = output.get("logical_uri")
            parents = list(dict.fromkeys(g.all_upstream_publication_uris(graph, step_id)))
            run_parents.extend(parents)
            managed_attempt = bool(logical_uri and is_object_uri(uri) and is_attempt_uri(uri))
            if managed_attempt:
                publish = core_managed_publisher(self.catalog)
                if publish is None:
                    raise RuntimeError("managed object output has no core publisher")
                try:
                    receipt = publish(
                        name=name, uri=uri, version=None, parents=parents, pipeline="canvas")
                except Exception as exc:
                    logging.getLogger(__name__).exception(
                        "Ray managed sink publication failed for step %s", step_id)
                    raise RuntimeError(
                        f"Ray could not atomically publish managed sink '{step_id}'"
                    ) from exc
                if not isinstance(receipt, dict) or receipt.get("uri") != uri:
                    raise RuntimeError(
                        f"core publisher returned an invalid receipt for sink '{step_id}'")
            elif register_once is not None:
                idempotency_key = f"{_JOBS_BACKEND}:{job['attempt_id']}:{step_id}"
                raw_receipt = register_once(
                    idempotency_key=idempotency_key,
                    name=name, uri=uri, version=None, parents=parents, pipeline="canvas",
                )
                try:
                    receipt = CatalogPublicationReceipt.model_validate(raw_receipt)
                except Exception as e:  # noqa: BLE001 — a durable receipt is the publication boundary
                    raise RuntimeError(
                        "durable catalog publisher returned no valid output receipt"
                    ) from e
                if receipt.idempotency_key != idempotency_key or receipt.uri != uri:
                    raise RuntimeError(
                        "durable catalog publisher returned a receipt for a different output effect"
                    )
            else:
                publish_unmanaged_output_attested(
                    self.catalog, name=name, uri=uri, version=None,
                    parents=parents, pipeline="canvas")
        if usage_once and (result.get("outputs") or []):
            usage_once(
                idempotency_key=f"{_JOBS_BACKEND}:{job['attempt_id']}",
                parents=list(dict.fromkeys(run_parents)),
            )

    @staticmethod
    def _listed_job_status(jobs, submission_id: str) -> str | None:
        if isinstance(jobs, dict):
            info = jobs.get(submission_id)
            if info is None:
                return None
            value = getattr(info, "status", info.get("status") if isinstance(info, dict) else info)
            return _job_status_name(value)
        for info in jobs or []:
            jid = (getattr(info, "submission_id", None) or getattr(info, "job_id", None)
                   or (info.get("submission_id") or info.get("job_id") if isinstance(info, dict) else None))
            if jid == submission_id:
                value = getattr(info, "status", info.get("status") if isinstance(info, dict) else None)
                return _job_status_name(value)
        return None

    def _find_job(self, client, submission_id: str) -> str | None:
        """Authoritatively distinguish missing from an ambiguous status request failure.

        ``get_job_status`` raises RuntimeError for both not-found and transport failures. A successful
        ``list_jobs`` is therefore the second, cluster-authoritative check; if listing also fails we do
        not submit because that could double-run an already accepted job.
        """
        try:
            return _job_status_name(client.get_job_status(submission_id))
        except Exception as status_error:  # noqa: BLE001
            try:
                return self._listed_job_status(client.list_jobs(), submission_id)
            except Exception as list_error:  # noqa: BLE001
                raise RuntimeError(
                    "Ray Jobs discovery unavailable "
                    f"(code=job_discovery_unavailable,status_type={type(status_error).__name__},"
                    f"list_type={type(list_error).__name__})"
                ) from list_error

    @staticmethod
    def _remote_job_metadata(client, submission_id: str) -> dict[str, str]:
        info = client.get_job_info(submission_id)
        metadata = getattr(info, "metadata", None)
        if metadata is None and isinstance(info, dict):
            metadata = info.get("metadata")
        if not isinstance(metadata, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in metadata.items()):
            raise RuntimeError("Ray job metadata is missing or malformed")
        return metadata

    @staticmethod
    def _note_submission_winner(run_id: str, attempt_id: str) -> None:
        """Persist a visible workload unless terminal effects already won the SQL race."""
        from hub import metadb

        if metadb.note_backend_submission_observed(run_id, attempt_id):
            return
        current = metadb.backend_job(run_id) or {}
        if current.get("publication_state") in ("effects_started", "published"):
            raise PublicationEffectsWon(run_id)
        if current.get("submission_state") in (
                "submitted", "stopping", "result_submitted"):
            return
        raise RuntimeError("Ray workload winner lost its durable SQL binding")

    def _validate_remote_workload_winner(
            self, client, run_id: str, attempt_id: str, code_ref: str) -> None:
        metadata = self._remote_job_metadata(client, _jobs_submission_id(run_id, attempt_id))
        if (metadata.get("dataplay_run_id") != run_id
                or metadata.get("dataplay_attempt_id") != attempt_id
                or metadata.get("dataplay_code_ref") != code_ref
                or metadata.get("dataplay_stop_fence") is not None):
            raise RuntimeError("Ray workload metadata does not match its durable execution binding")

    def _persist_fencing_winner(
            self, client, status: RunStatus, ref: RunBackendRef, binding: dict,
            *, reason: str) -> bool:
        """Classify the remote deterministic ID and atomically persist which submit won its race."""
        from hub import metadb

        metadata = self._remote_job_metadata(client, ref.submission_id)
        if (metadata.get("dataplay_run_id") != status.run_id
                or metadata.get("dataplay_attempt_id") != ref.attempt_id):
            raise RuntimeError("Ray job metadata does not match its durable execution binding")
        fence_reason = metadata.get("dataplay_stop_fence")
        if fence_reason is not None:
            current_binding = metadb.backend_job(status.run_id) or binding
            allowed_reasons = set()
            if current_binding.get("cancel_requested"):
                allowed_reasons.add("cancellation")
            if current_binding.get("quarantine_reason"):
                allowed_reasons.add("quarantine")
            if current_binding.get("submission_state") in (
                    *_RESULT_RECONCILIATION_STATES, "result_fenced"):
                allowed_reasons.add("result-reconciliation")
            if fence_reason not in allowed_reasons:
                raise RuntimeError("Ray stop-fence metadata does not match its durable control intent")
            if not metadb.note_backend_stop_fence_accepted(
                    status.run_id, ref.attempt_id, binding.get("submission_owner")):
                current = metadb.backend_job(status.run_id) or {}
                expected = (
                    ("result_stop_fenced", "result_fenced")
                    if fence_reason == "result-reconciliation" else ("fence_stopping",)
                )
                if current.get("publication_state") in ("effects_started", "published"):
                    raise PublicationEffectsWon(status.run_id)
                if current.get("submission_state") not in expected:
                    raise RuntimeError("Ray stop-fence winner lost its durable SQL ownership")
            return True
        if metadata.get("dataplay_code_ref") != ref.code_ref:
            raise RuntimeError("Ray workload metadata does not match its durable code binding")
        self._note_submission_winner(status.run_id, ref.attempt_id)
        return False

    def _bounded_lease_keepalive(
            self, done: threading.Event, interval: float, renew) -> None:
        """Renew one SQL owner for a bounded wall-clock interval, then permit takeover."""
        deadline = time.monotonic() + self._max_lease_hold_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or done.wait(min(interval, remaining)):
                return
            if time.monotonic() >= deadline:
                return
            try:
                if not renew():
                    return
            except Exception:  # noqa: BLE001 — the owner CAS and finite deadline stay authoritative
                pass

    def _ensure_job_submitted(self, client, job: dict) -> str:
        from hub import metadb

        submission_id = job["submission_id"]
        existing = self._find_job(client, submission_id)
        if existing:
            self._validate_remote_workload_winner(
                client, job["run_id"], job["attempt_id"], job["code_ref"])
            self._note_submission_winner(job["run_id"], job["attempt_id"])
            return existing
        owner = uuid.uuid4().hex
        claim = metadb.claim_backend_submission_after_missing(
            job["run_id"], job["attempt_id"], owner, self._submission_lease_s
        )
        if claim == "cancelled":
            return "CANCEL_REQUESTED"
        if claim in ("busy", "submitted"):
            return "SUBMITTING"
        if claim != "claimed":
            raise RuntimeError(f"Ray Jobs submission claim is {claim}")
        child_env = _ray_jobs_env(job)
        child_env.pop("DP_DATABASE_URL", None)  # defense in depth: workload profile already excludes it
        entrypoint = " ".join((
            job["entrypoint"], shlex.quote(job["job_uri"]), shlex.quote(job["attempt_id"]),
            shlex.quote(job["submission_id"]), shlex.quote(job["envelope_sha256"]),
        ))
        metadata = {
            "job_name": f"Data Playground {job['run_id']}",
            "dataplay_run_id": job["run_id"],
            "dataplay_attempt_id": job["attempt_id"],
            "dataplay_code_ref": job["code_ref"],
        }
        keepalive_done = threading.Event()

        def _keepalive() -> None:
            interval = max(0.25, self._submission_lease_s / 3)
            self._bounded_lease_keepalive(
                keepalive_done, interval,
                lambda: metadb.renew_backend_submission(
                    job["run_id"], job["attempt_id"], owner,
                    self._submission_lease_s,
                ),
            )

        threading.Thread(target=_keepalive, daemon=True,
                         name=f"dp-ray-submit-{job['run_id']}").start()
        try:
            returned = client.submit_job(
                entrypoint=entrypoint,
                submission_id=submission_id,
                runtime_env={"env_vars": child_env},
                metadata=metadata,
            )
            if returned and str(returned) != submission_id:
                raise RuntimeError(
                    f"Ray Jobs returned submission id {returned!r}, expected deterministic id {submission_id!r}"
                )
            self._note_submission_winner(job["run_id"], job["attempt_id"])
            return "PENDING"
        except Exception:  # noqa: BLE001 — an accepted submit can race the response; re-check before failing
            existing = self._find_job(client, submission_id)
            if existing:
                self._validate_remote_workload_winner(
                    client, job["run_id"], job["attempt_id"], job["code_ref"])
                self._note_submission_winner(job["run_id"], job["attempt_id"])
                return existing
            # A timeout/disconnect can return before the server accepts the still-in-flight HTTP request.
            # Preserve the linearized claim; lease-based replay or the cancel-fence path will reconcile it.
            raise
        finally:
            keepalive_done.set()

    def _job_failure(self, client, submission_id: str) -> str:
        del client, submission_id
        return "RemoteJobFailed: remote job reported failure"

    def _read_job_result(self, job: dict) -> dict:
        deadline = time.monotonic() + self._jobs_result_timeout_s
        last_error = None
        while time.monotonic() <= deadline:
            try:
                return self._validate_job_result(job, self._artifacts.read(job["result_uri"]))
            except ArtifactContractError:
                raise  # readable but untrusted content will not become valid through consistency delay
            except ArtifactCorrupt as e:
                raise ArtifactContractError(str(e)) from e
            except (ArtifactNotFound, FileNotFoundError) as e:
                # A successful entrypoint may become visible before its just-written object. Only an
                # authoritative absence gets this bounded consistency grace period.
                last_error = e
                time.sleep(min(self._jobs_poll_s, 0.25))
            except Exception:
                # Transport/auth/5xx is ambiguous, not evidence that a SUCCEEDED job omitted its result.
                # Keep the run non-terminal and let a fresh supervisor retry after storage recovers.
                raise
        raise TerminalResultMissing(
            f"Ray job succeeded without a readable terminal result artifact: {last_error}"
        )

    def _terminal_result_if_present(self, job: dict) -> dict | None:
        """Use immutable terminal evidence before replaying a job whose Ray metadata disappeared."""
        try:
            raw = self._artifacts.read(job["result_uri"])
        except (ArtifactNotFound, FileNotFoundError):  # authoritative absence permits replay/cancellation
            return None
        except ArtifactCorrupt as e:
            return {"status": "failed", "rows": 0, "outputs": [], "artifact_invalid": True,
                    "error": self._stable_exception(
                        "Ray result artifact rejected", e, "result_artifact_invalid"
                    )}
        try:
            result = self._validate_job_result(job, raw)
        except Exception as e:
            return {"status": "failed", "rows": 0, "outputs": [], "artifact_invalid": True,
                    "error": self._stable_exception(
                        "Ray result artifact rejected", e, "result_artifact_invalid"
                    )}
        return result

    def _result_contract_error_if_present(self, job: dict) -> Exception | None:
        """Detect readable corruption without treating an early valid result as terminal evidence."""
        try:
            raw = self._artifacts.read(job["result_uri"])
        except (ArtifactNotFound, FileNotFoundError):
            return None
        except ArtifactCorrupt as e:
            return e
        except Exception:  # transport/auth is ambiguous and must not quarantine a real job
            return None
        try:
            self._validate_job_result(job, raw)
        except Exception as e:  # exact, readable contract mismatch
            return e
        return None

    @staticmethod
    def _copy_status(target: RunStatus, source: RunStatus) -> None:
        for field in RunStatus.model_fields:
            if field != "status":
                setattr(target, field, getattr(source, field))
        # Pollers use terminal status as the publication barrier. Copy it last so they cannot observe a
        # new terminal label paired with stale rows/output/per-node fields from the previous live state.
        target.status = source.status

    @staticmethod
    def _converge_terminal_fence(status: RunStatus) -> bool:
        """Stop stale local supervision after retention prunes the winning backend/status detail."""
        from hub import metadb

        terminal = metadb.terminal_run_status(status.run_id)
        if terminal not in ("done", "failed", "cancelled"):
            return False
        status.status = terminal
        if terminal == "done":
            status.progress, status.error = 1.0, None
        elif terminal == "cancelled":
            status.error = None
        else:
            status.error = "Run failed (code=terminal_details_pruned)"
        if terminal != "done":
            status.output_uri = status.output_table = None
        for node in status.per_node:
            node.status = "done" if terminal == "done" else terminal
        return True

    def _apply_job_result(self, status: RunStatus, result: dict, *, remote: bool = True) -> None:
        status.status = result["status"]
        status.error = (
            (self._public_remote_error(result["error"]) if remote
             else "Ray Jobs artifact rejected (code=artifact_contract_invalid)")
            if result.get("error") else None
        )
        status.output_uri, status.output_table = result.get("output_uri"), result.get("output_table")
        status.rows_processed = status.total_rows = int(result.get("rows") or 0)
        if status.status == "done":
            status.progress = 1.0
        for node in status.per_node:
            node.status = "done" if status.status == "done" else status.status

    def _prepare_job_publication_effects(
            self, job: dict, graph, result: dict) -> tuple[list[dict], dict | None]:
        """Preflight object/schema reads and freeze every managed SQL effect before the barrier."""
        from hub import metadb
        from hub.plugins.catalog import core_managed_publication_planner

        outputs = sorted(result.get("outputs") or [], key=lambda item: item["step_id"])
        if not outputs:
            return [], None
        planner = core_managed_publication_planner(self.catalog)
        if planner is None:
            raise RuntimeError("Ray Jobs managed outputs require the core catalog planner")
        nodes = {node.id: node for node in graph.nodes}
        plans: list[dict] = []
        run_parents: list[str] = []
        for output in outputs:
            step_id = output["step_id"]
            node = nodes.get(step_id)
            if node is None or node.type != "write":
                raise RuntimeError(f"Ray driver returned an unknown sink '{step_id}'")
            spec = SinkSpec.from_config(node.data.get("config") or {}, node.data.get("title"))
            if output["name"] != spec.name:
                raise RuntimeError(f"Ray driver returned an unexpected name for sink '{step_id}'")
            parents = list(dict.fromkeys(g.all_upstream_publication_uris(graph, step_id)))
            run_parents.extend(parents)
            plans.append(planner(
                run_id=job["run_id"], step_id=step_id,
                idempotency_key=f"{_JOBS_BACKEND}:{job['attempt_id']}:{step_id}",
                name=output["name"], uri=output["uri"], version=None,
                parents=parents, pipeline="canvas",
            ))
        usage = None
        if plans:
            usage = metadb.catalog_prepare_usage_publication(
                job["run_id"], f"{_JOBS_BACKEND}:{job['attempt_id']}",
                list(dict.fromkeys(run_parents)),
            )
        return plans, usage

    def _apply_job_publication_effects(self, effects: dict) -> None:
        """Replay only frozen SQL effects; no artifact, manifest, schema, or Ray reads are allowed."""
        from hub import metadb

        for plan in effects["catalog_effects"]:
            receipt = metadb.catalog_apply_managed_publication(plan)
            if (not isinstance(receipt, dict)
                    or receipt.get("event_key") != plan["event_key"]
                    or receipt.get("uri") != plan["uri"]
                    or receipt.get("version") != plan["version"]):
                raise RuntimeError(
                    f"core publisher returned an invalid staged receipt for '{plan['step_id']}'")
        if effects["usage_effect"] is not None:
            metadb.catalog_apply_usage_publication(effects["usage_effect"])

    def _jobs_graph_from_durable_sql(
            self, ref: RunBackendRef, candidate: RunStatus):
        """Best-effort telemetry context from immutable SQL only after effects have started."""
        from hub import metadb
        from hub.models import Graph

        payload = metadb.backend_job_artifact_payload(candidate.run_id)
        if payload is None:
            return None
        job = json.loads(payload.decode("utf-8"))
        if not isinstance(job, dict) or canonical_json(job) != payload:
            return None
        self._validate_job_artifact_integrity(ref, candidate, job)
        return Graph.model_validate(job["graph"])

    def _publish_job_result(self, job: dict, graph, target, status: RunStatus, result: dict, *,
                            artifact_error: bool = False) -> None:
        """Stage terminal effects once, replay them, then atomically expose the canonical terminal."""
        from hub import metadb

        ref = status.backend_ref
        if ref is None or ref.backend != _JOBS_BACKEND:
            raise RuntimeError("Ray Jobs publication has no durable backend reference")
        owner = uuid.uuid4().hex
        while True:
            claim = metadb.claim_backend_publication(
                status.run_id, ref.attempt_id, owner, self._publication_lease_s
            )
            if claim == "published":
                canonical = metadb.backend_job(status.run_id)
                if not canonical or not canonical.get("result"):
                    if self._converge_terminal_fence(status):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                self._copy_status(status, RunStatus.model_validate(canonical["result"]))
                return
            if claim == "busy":
                time.sleep(self._jobs_poll_s)
                continue
            if claim == "submission":
                # A submit or stop-fence request linearized first. Its eventual remote outcome can
                # invalidate the terminal observation that brought us here, so return to supervision
                # and discover it again instead of waiting under a stale publication candidate.
                return
            if claim == "lost":
                if self._converge_terminal_fence(status):
                    return
                raise RuntimeError("Ray result lost the durable attempt publication fence")

            keepalive_done = threading.Event()

            def _keepalive() -> None:
                interval = max(1.0, self._publication_lease_s / 3)
                self._bounded_lease_keepalive(
                    keepalive_done, interval,
                    lambda: metadb.renew_backend_publication(
                        status.run_id, ref.attempt_id, owner,
                        self._publication_lease_s,
                    ),
                )

            threading.Thread(target=_keepalive, daemon=True,
                             name=f"dp-ray-publish-{status.run_id}").start()
            try:
                if claim == "effects":
                    effects = metadb.backend_publication_effects(
                        status.run_id, ref.attempt_id)
                    if effects is None:
                        raise RuntimeError("Ray publication lost its staged effects plan")
                else:
                    # Never expose a terminal in-memory object until the durable winner CAS succeeds.
                    candidate = status.model_copy(deep=True)
                    self._apply_job_result(
                        candidate, result,
                        remote=not artifact_error and "contract_version" in job,
                    )
                    validated_result = None
                    sink_attempts = None
                    catalog_effects: list[dict] = []
                    usage_effect = None
                    try:
                        if "sink_contracts" in job and "sink_targets" in job:
                            handles = self._validate_jobs_sink_attempts(job)
                            sink_attempts = {
                                step_id: handle["uri"] for step_id, handle in handles.items()
                            }
                        if candidate.status == "done":
                            validated_result = self._validate_job_result(job, dict(result))
                            if graph is None:
                                raise ArtifactContractError(
                                    "successful Ray Jobs publication has no hash-bound graph")
                            catalog_effects, usage_effect = \
                                self._prepare_job_publication_effects(
                                    job, graph, validated_result)
                        stage = metadb.begin_backend_publication_effects(
                            status.run_id, ref.attempt_id, owner, candidate.model_dump(),
                            validated_result, sink_attempts,
                            catalog_effects=catalog_effects, usage_effect=usage_effect,
                        )
                    except metadb.BackendPublicationConflict as conflict:
                        # Required catalog projection irreversibly lost to unregister/a newer current
                        # generation before the barrier. Publish a sanitized failure and atomically
                        # retire the exact committed/writing sinks instead of exposing false success.
                        if candidate.status != "done":
                            raise
                        candidate = status.model_copy(deep=True)
                        self._apply_job_result(candidate, {
                            "status": "failed", "rows": 0, "outputs": [],
                            "error": self._stable_exception(
                                "Ray output publication conflicted", conflict,
                                "catalog_publication_conflict",
                            ),
                        })
                        stage = metadb.begin_backend_publication_effects(
                            status.run_id, ref.attempt_id, owner, candidate.model_dump(),
                            None, sink_attempts, catalog_effects=[], usage_effect=None,
                        )
                    except metadb.BackendPublicationBusy as busy:
                        status.error = self._stable_exception(
                            "Ray result publication waiting for catalog", busy,
                            "catalog_publication_busy",
                        )
                        metadb.save_run_state(status.run_id, status.model_dump())
                        time.sleep(self._jobs_poll_s)
                        continue
                    except Exception as e:
                        # Pre-effects object/schema/control failures are retryable and cannot leak a
                        # partial catalog projection because begin has not linearized yet.
                        status.error = self._stable_exception(
                            "Ray result publication preflight unavailable", e,
                            "publication_preflight_unavailable",
                        )
                        metadb.save_run_state(status.run_id, status.model_dump())
                        time.sleep(self._jobs_poll_s)
                        continue
                    if stage == "quarantined":
                        return
                    if stage == "submission":
                        return
                    if stage in ("published", "lost"):
                        continue
                    if stage == "busy":
                        time.sleep(self._jobs_poll_s)
                        continue
                    effects = metadb.backend_publication_effects(
                        status.run_id, ref.attempt_id)
                    if effects is None:
                        raise RuntimeError("Ray publication failed to attest its staged effects plan")

                candidate = RunStatus.model_validate(effects["terminal_status"])
                try:
                    self._apply_job_publication_effects(effects)
                except Exception as e:
                    status.error = self._stable_exception(
                        "Ray result publication waiting for staged effects", e,
                        "publication_effect_unavailable",
                    )
                    metadb.save_run_state(status.run_id, status.model_dump())
                    time.sleep(self._jobs_poll_s)
                    continue
                if metadb.finish_backend_publication(
                        status.run_id, ref.attempt_id, owner, candidate.model_dump()):
                    self._copy_status(status, candidate)
                    telemetry_graph = None
                    try:
                        telemetry_graph = self._jobs_graph_from_durable_sql(ref, candidate)
                    except Exception:
                        log.warning(
                            "ray_jobs_terminal_telemetry_context_unavailable run_id=%s",
                            status.run_id, exc_info=True,
                        )
                    if telemetry_graph is not None and getattr(
                            telemetry_graph, "id", None) != "_region":
                        from hub.deps import _emit_telemetry
                        per_node = [node.model_dump() for node in (status.per_node or [])] or None
                        _emit_telemetry(
                            self.deps, telemetry_graph, candidate.target_node_id,
                            status, per_node)
                    return
                binding = metadb.backend_job(status.run_id)
                if (candidate.status != "failed" and binding
                        and binding.get("publication_state") != "published"
                        and binding.get("quarantine_reason")):
                    # Quarantine cannot cross effects_started; this only handles a pending preflight
                    # loser that observed the fence before begin linearized.
                    return
            finally:
                keepalive_done.set()
            # Ownership changed or the DB write was interrupted. Read the canonical winner on the next loop;
            # catalog upsert + run-history-by-run_id make a lease-recovery replay idempotent after a crash.
            time.sleep(self._jobs_poll_s)

    def _quarantine_invalid_job(self, status: RunStatus, ref: RunBackendRef, error: Exception) -> bool:
        """Persist the corruption fence, stop the SQL-bound attempt, then publish failure."""
        from hub import metadb

        reason = self._stable_exception(
            "Ray Jobs artifact rejected", error, "artifact_contract_invalid"
        )
        if not metadb.request_backend_quarantine(status.run_id, reason):
            # A terminal publication that won first is authoritative. A stale corruption observer
            # must converge from retained detail or the permanent terminal fence, never stop a remote
            # submission that no longer belongs to a live backend binding.
            canonical = metadb.backend_job(status.run_id)
            if (canonical
                    and canonical.get("publication_state") == "effects_started"):
                self._publish_job_result(
                    {}, None, status.target_node_id, status, {}, artifact_error=True)
                return True
            if (canonical and canonical.get("publication_state") == "published"
                    and canonical.get("result")):
                try:
                    winner = RunStatus.model_validate(canonical["result"])
                    if (winner.run_id == status.run_id
                            and winner.status in ("done", "failed", "cancelled")):
                        self._copy_status(status, winner)
                        return True
                except (TypeError, ValueError):
                    pass
            if self._converge_terminal_fence(status):
                return True
            raise RuntimeError(
                "Ray quarantine fence was rejected without an authoritative terminal result"
            )
        binding = metadb.backend_job(status.run_id) or {}
        self._backend_refs[status.run_id] = binding
        return self._resume_quarantined_job(status, ref, binding)

    def _resume_quarantined_job(self, status: RunStatus, ref: RunBackendRef, binding: dict) -> bool:
        """Return true only after the durable quarantine reaches terminal publication."""
        from hub import metadb

        reason = "Ray Jobs artifact rejected (code=artifact_contract_invalid)"
        if binding.get("submission_state") in _RESULT_RECONCILIATION_STATES:
            status.error = reason + "; waiting for durable result reconciliation"
            metadb.save_run_state(status.run_id, status.model_dump())
            return False
        try:
            settled_result_fence = binding.get("submission_state") == "result_fenced"
            state = None
            if not settled_result_fence:
                client = self._jobs_client(binding.get("control_address"))
                state = self._find_job(client, ref.submission_id)
                self._note_jobs_control_observed(status, ref)
                if state is not None:
                    self._persist_fencing_winner(
                        client, status, ref, binding, reason="quarantine"
                    )
                    binding = self._refresh_jobs_binding(status.run_id)
                if state is None:
                    if binding.get("submission_state") in ("stopping", "fence_stopping"):
                        self._settle_stop_control(status.run_id, ref.attempt_id)
                    else:
                        state, _accepted_fence = self._fence_missing_stop(
                            client, status, ref, reason="quarantine"
                        )
                    if state in ("SUBMITTING", "FENCING"):
                        return False
                    if state == "MISSING":
                        state = None
                if state and state not in _JOB_TERMINAL:
                    if status.run_id not in self._cancel_stop_sent:
                        client.stop_job(ref.submission_id)
                        self._cancel_stop_sent.add(status.run_id)
                    state = self._find_job(client, ref.submission_id)
                    self._note_jobs_control_observed(status, ref)
                    if state is not None and state not in _JOB_TERMINAL:
                        status.error = reason + "; remote job quarantined, waiting for STOPPED"
                        metadb.save_run_state(status.run_id, status.model_dump())
                        return False
                binding = self._refresh_jobs_binding(status.run_id)
                if (state is None or state in _JOB_TERMINAL) and binding.get(
                        "submission_state") in ("stopping", "fence_stopping"):
                    self._settle_stop_control(status.run_id, ref.attempt_id)
        except PublicationEffectsWon:
            raise
        except Exception as control_error:  # cannot prove the untrusted remote execution stopped
            status.error = self._stable_exception(
                "Ray quarantine control unavailable; run remains non-terminal",
                control_error, "quarantine_control_unavailable",
            )
            metadb.save_run_state(status.run_id, status.model_dump())
            return False

        try:
            trusted_job = self._durable_sql_job_envelope(ref, status)
        except Exception:
            # Corruption may include the SQL copy itself. Never act on untrusted sink URIs; quarantine
            # still converges the backend binding through its independently stored attempt ID.
            trusted_job = {"attempt_id": ref.attempt_id}
        self._publish_job_result(
            trusted_job, None, status.target_node_id, status,
            {"status": "failed", "error": reason, "rows": 0, "outputs": []},
            artifact_error=True,
        )
        return True

    def _persist_jobs_live_error(self, status: RunStatus, message: str) -> None:
        from hub import metadb

        status.error = message
        metadb.save_run_state(status.run_id, status.model_dump())

    def _note_jobs_control_observed(self, status: RunStatus, ref: RunBackendRef) -> None:
        """Advance durable liveness after a successful Jobs status/list observation, at bounded rate."""
        from hub import metadb

        now = time.monotonic()
        with self._lock:
            previous = self._control_observed_monotonic.get(status.run_id)
            if previous is not None and now - previous < _CONTROL_OBSERVATION_WRITE_S:
                return
            self._control_observed_monotonic[status.run_id] = now
        try:
            metadb.note_backend_control_observed(
                status.run_id, ref.attempt_id, _CONTROL_OBSERVATION_WRITE_S
            )
        except Exception:
            # A failed durable write must remain retryable; process-local throttling is not authoritative.
            with self._lock:
                self._control_observed_monotonic.pop(status.run_id, None)
            raise

    def _refresh_jobs_binding(self, run_id: str) -> dict:
        from hub import metadb

        binding = metadb.backend_job(run_id)
        if not binding:
            status = self.runs.get(run_id)
            if status is not None and self._converge_terminal_fence(status):
                raise DurableTerminalObserved(run_id)
            raise RuntimeError(f"durable Ray Jobs binding for '{run_id}' disappeared")
        if (binding.get("_recovery_error")
                and binding.get("publication_state") != "effects_started"):
            raise RuntimeError(str(binding["_recovery_error"]))
        self._backend_refs[run_id] = binding
        if binding.get("cancel_requested"):
            self._cancel[run_id].set()
        return binding

    def _resume_staged_binding(
            self, status: RunStatus, ref: RunBackendRef, binding: dict) -> bool:
        """Resume a write-ahead plan before consulting any mutable control or artifact state."""
        if binding.get("publication_state") != "effects_started":
            return False
        self._publish_job_result(
            {}, None, status.target_node_id, status, {}, artifact_error=True)
        return True

    def _resume_settled_result_fence(
            self, status: RunStatus, ref: RunBackendRef, binding: dict,
            job: dict | None = None, graph=None, target=None) -> bool:
        """Publish after a terminal reconciliation fence without mistaking it for a workload."""
        if binding.get("submission_state") != "result_fenced":
            return False
        if binding.get("quarantine_reason"):
            return self._resume_quarantined_job(status, ref, binding)
        try:
            self._publish_reconciled_result(job, graph, target, status)
        except (ArtifactNotFound, ArtifactCorrupt, ArtifactContractError) as error:
            return self._quarantine_invalid_job(status, ref, error)
        return True

    def _resume_publication_winner(self, status: RunStatus, ref: RunBackendRef) -> bool:
        """Yield remote control to a publisher that already crossed or completed the barrier."""
        binding = self._refresh_jobs_binding(status.run_id)
        if binding.get("publication_state") not in ("effects_started", "published"):
            return False
        self._publish_job_result(
            {}, None, status.target_node_id, status, {}, artifact_error=True)
        return True

    @staticmethod
    def _settle_result_reconciliation(run_id: str, attempt_id: str) -> None:
        from hub import metadb

        if metadb.settle_backend_result_reconciliation(run_id, attempt_id):
            return
        current = metadb.backend_job(run_id) or {}
        if current.get("publication_state") in ("effects_started", "published"):
            raise PublicationEffectsWon(run_id)
        if current.get("submission_state") in ("submitted", "result_fenced"):
            return
        raise RuntimeError("Ray result reconciliation lost its durable SQL state")

    @staticmethod
    def _settle_stop_control(run_id: str, attempt_id: str) -> None:
        from hub import metadb

        if metadb.settle_backend_stop_control(run_id, attempt_id):
            return
        current = metadb.backend_job(run_id) or {}
        if current.get("publication_state") in ("effects_started", "published"):
            raise PublicationEffectsWon(run_id)
        if current.get("submission_state") in ("submitted", "stop_fenced"):
            return
        raise RuntimeError("Ray stop control lost its durable SQL state")

    def _drive_result_reconciliation(
            self, status: RunStatus, ref: RunBackendRef,
            binding: dict) -> tuple[object, bool]:
        """Fence an uncertain replay before trusting a result observed after metadata loss.

        The caller invokes this only after a valid, hash-bound result was observed while the
        deterministic Ray ID was authoritatively missing, or while resuming the durable states created
        by that decision. ``ready`` means every possible writer is terminal/absent and SQL no longer
        carries a result-reconciliation state, so the result object may be read again and published.
        """
        from hub import metadb

        client = self._jobs_client(binding.get("control_address"))
        submission_state = binding.get("submission_state")
        state: str | None
        if submission_state == "submitting":
            # The caller already observed authoritative metadata loss. Claiming the fixed-ID fence is
            # the next linearization point; a delayed workload submit can still win the same Ray ID.
            state, _accepted_fence = self._fence_missing_stop(
                client, status, ref, reason="result-reconciliation"
            )
        elif submission_state == "result_fencing":
            state = self._find_job(client, ref.submission_id)
            self._note_jobs_control_observed(status, ref)
            if state is None:
                state, _accepted_fence = self._fence_missing_stop(
                    client, status, ref, reason="result-reconciliation"
                )
            else:
                self._persist_fencing_winner(
                    client, status, ref, binding, reason="result-reconciliation"
                )
        elif submission_state in ("result_submitted", "result_stop_fenced"):
            state = self._find_job(client, ref.submission_id)
            self._note_jobs_control_observed(status, ref)
        else:
            raise RuntimeError(
                f"Ray result reconciliation has invalid submission state {submission_state!r}"
            )

        if state in ("SUBMITTING", "FENCING", "REOBSERVE"):
            return client, False

        current = self._refresh_jobs_binding(status.run_id)
        current_state = current.get("submission_state")
        if state in (None, "MISSING"):
            if current_state in ("result_submitted", "result_stop_fenced"):
                self._settle_result_reconciliation(status.run_id, ref.attempt_id)
            elif current_state not in ("queued", "submitted", "stop_fenced"):
                return client, False
            return client, True

        if current_state not in ("result_submitted", "result_stop_fenced"):
            raise RuntimeError("Ray result reconciliation has no durable remote winner")
        if state not in _JOB_TERMINAL:
            if status.run_id not in self._cancel_stop_sent:
                client.stop_job(ref.submission_id)
                self._cancel_stop_sent.add(status.run_id)
            state = self._find_job(client, ref.submission_id)
            self._note_jobs_control_observed(status, ref)
            if state is not None and state not in _JOB_TERMINAL:
                self._persist_jobs_live_error(
                    status,
                    "Ray result reconciliation fenced a stale replay; waiting for it to stop",
                )
                return client, False

        self._settle_result_reconciliation(status.run_id, ref.attempt_id)
        return client, True

    def _publish_reconciled_result(
            self, job: dict | None, graph, target, status: RunStatus) -> None:
        """Re-read terminal evidence only after result reconciliation fenced every writer."""
        if job is None:
            ref = status.backend_ref
            if ref is None or ref.backend != _JOBS_BACKEND:
                raise RuntimeError("Ray result reconciliation has no durable backend reference")
            job = self._durable_sql_job_envelope(ref, status)
            from hub.models import Graph
            graph = Graph.model_validate(job["graph"])
            target = job.get("target")
        completed = self._terminal_result_if_present(job)
        if completed is None:
            completed = {
                "status": "failed", "rows": 0, "outputs": [],
                "error": (
                    "TerminalResultMissing: result artifact disappeared after "
                    "remote replay reconciliation"
                ),
            }
        elif completed.get("artifact_invalid"):
            ref = status.backend_ref
            if ref is None or ref.backend != _JOBS_BACKEND:
                raise RuntimeError("Ray result reconciliation has no durable backend reference")
            self._quarantine_invalid_job(
                status, ref, ArtifactContractError(completed["error"])
            )
            return
        self._publish_job_result(job, graph, target, status, completed)

    def _fence_missing_stop(self, client, status: RunStatus, ref: RunBackendRef,
                            *, reason: str) -> tuple[str, bool]:
        """Reserve an expired uncertain submission ID with a fixed stoppable job.

        A delayed original request and this fence use the same Ray submission ID, so Ray accepts at
        most one. Whichever wins becomes visible and can be stopped without local job artifacts/config.
        """
        from hub import metadb

        owner = uuid.uuid4().hex
        claim = metadb.claim_backend_stop_fence(
            status.run_id, ref.attempt_id, owner, self._submission_lease_s,
            result_reconcile=reason == "result-reconciliation",
        )
        if claim == "not_needed":
            if reason == "result-reconciliation":
                # A concurrent observer may have changed ``submitting`` to ``submitted`` because the
                # delayed workload appeared after our missing observation. Discard that stale result
                # candidate and require a fresh status/list read before publication.
                return "REOBSERVE", False
            return "MISSING", False
        if claim == "settled_missing":
            # The remote ID was previously accepted (real job or fixed stop fence). Authoritative
            # metadata loss ends execution, but a trusted result artifact may still prove completion.
            return "STOPPED", False
        if claim == "busy":
            self._persist_jobs_live_error(
                status, f"Ray {reason} is waiting for an already-linearized submit to settle"
            )
            return "SUBMITTING", False
        if claim != "claimed":
            raise RuntimeError(f"Ray cancellation submission fence is {claim}")

        try:
            returned = client.submit_job(
                entrypoint=_CANCEL_FENCE_ENTRYPOINT,
                submission_id=ref.submission_id,
                runtime_env={},
                metadata={
                    "job_name": f"Data Playground {reason} fence {status.run_id}",
                    "dataplay_run_id": status.run_id,
                    "dataplay_attempt_id": ref.attempt_id,
                    "dataplay_stop_fence": reason,
                },
            )
            if returned and str(returned) != ref.submission_id:
                raise RuntimeError(
                    f"Ray Jobs returned submission id {returned!r}, expected {ref.submission_id!r}"
                )
        except Exception as submit_error:  # the original workload may have won the same remote ID
            existing = self._find_job(client, ref.submission_id)
            if existing is not None:
                binding = metadb.backend_job(status.run_id) or {}
                try:
                    accepted_fence = self._persist_fencing_winner(
                        client, status, ref, binding, reason=reason
                    )
                except PublicationEffectsWon:
                    raise
                except Exception as metadata_error:
                    self._persist_jobs_live_error(
                        status, self._stable_exception(
                            f"Ray {reason} winner metadata is invalid; control remains non-terminal",
                            metadata_error, "stop_fence_metadata_invalid",
                        ),
                    )
                    return "FENCING", False
                return existing, accepted_fence
            self._persist_jobs_live_error(
                status, self._stable_exception(
                    f"Ray {reason} fence submission is uncertain; retrying after its DB lease",
                    submit_error, "stop_fence_submit_uncertain",
                ),
            )
            return "FENCING", False

        if not metadb.note_backend_stop_fence_accepted(
                status.run_id, ref.attempt_id, owner):
            current = metadb.backend_job(status.run_id) or {}
            if current.get("publication_state") in ("effects_started", "published"):
                raise PublicationEffectsWon(status.run_id)
            expected = (
                "result_stop_fenced"
                if reason == "result-reconciliation" else "fence_stopping"
            )
            if current.get("submission_state") != expected:
                raise RuntimeError("Ray stop-fence acceptance lost its durable SQL ownership")
        state = self._find_job(client, ref.submission_id)
        if state is None:
            self._persist_jobs_live_error(
                status, f"Ray accepted the {reason} fence; waiting for it to become stoppable"
            )
            return "FENCING", True
        return state, True

    def _cancel_control_state(self, status: RunStatus, ref: RunBackendRef,
                              binding: dict) -> tuple[bool, object | None, str | None]:
        """Drive persisted cancel intent using only the durable SQL routing handle.

        ``MISSING`` means a successful Jobs listing authoritatively proved that no remote execution
        exists. ``None`` means control was ambiguous and the run must remain non-terminal.
        """
        if not (binding.get("cancel_requested") or self._cancel[status.run_id].is_set()):
            return False, None, None
        if binding.get("submission_state") in _RESULT_RECONCILIATION_STATES:
            # Result reconciliation already owns a stronger writer fence. The main supervisor resumes
            # that durable state before user cancellation, then re-evaluates the still-persisted intent.
            return True, None, "SUBMITTING"
        try:
            client = self._jobs_client(binding.get("control_address"))
            state = self._find_job(client, ref.submission_id)
            self._note_jobs_control_observed(status, ref)
            cancel_fenced = binding.get("submission_state") in (
                "fence_stopping", "stop_fenced")
            if state is not None:
                cancel_fenced = self._persist_fencing_winner(
                    client, status, ref, binding, reason="cancellation"
                )
                binding = self._refresh_jobs_binding(status.run_id)
                cancel_fenced = binding.get("submission_state") == "fence_stopping"
            if state is None:
                if binding.get("submission_state") in ("stopping", "fence_stopping"):
                    cancel_fenced = binding.get("submission_state") == "fence_stopping"
                    self._settle_stop_control(status.run_id, ref.attempt_id)
                    return True, client, "STOPPED"
                state, accepted_fence = self._fence_missing_stop(
                    client, status, ref, reason="cancellation")
                cancel_fenced = cancel_fenced or accepted_fence
                if state in ("MISSING", "SUBMITTING", "FENCING"):
                    return True, client, state
            if state not in _JOB_TERMINAL and status.run_id not in self._cancel_stop_sent:
                client.stop_job(ref.submission_id)
                self._cancel_stop_sent.add(status.run_id)
                state = self._find_job(client, ref.submission_id)
                self._note_jobs_control_observed(status, ref)
            binding = self._refresh_jobs_binding(status.run_id)
            if (state is None or state in _JOB_TERMINAL) and binding.get(
                    "submission_state") in ("stopping", "fence_stopping"):
                cancel_fenced = binding.get("submission_state") == "fence_stopping"
                self._settle_stop_control(status.run_id, ref.attempt_id)
            if state is None or (cancel_fenced and state in _JOB_TERMINAL):
                state = "STOPPED"  # the fixed fence carries no user result to reconcile
            return True, client, state
        except PublicationEffectsWon:
            raise
        except Exception as e:  # noqa: BLE001 — ambiguity can hide a still-running remote attempt
            self._persist_jobs_live_error(
                status, self._stable_exception(
                    "Ray cancellation control unavailable; retrying", e,
                    "cancellation_control_unavailable",
                ),
            )
            return True, None, None

    def _publish_cancelled_binding(
            self, status: RunStatus, binding: dict, job: dict | None = None) -> None:
        ref = status.backend_ref
        if job is None:
            if ref is None:
                raise RuntimeError("cancelled Ray Jobs binding has no durable backend reference")
            try:
                job = self._durable_sql_job_envelope(ref, status)
            except (ArtifactNotFound, ArtifactCorrupt, ArtifactContractError):
                # Durable cancel intent plus remote stop proof authorizes begin to derive this exact
                # binding's active sinks from SQL even when its hash-bound envelope is unavailable.
                job = {"attempt_id": ref.attempt_id}
        self._publish_job_result(
            job, None, status.target_node_id, status,
            {"status": "cancelled", "rows": 0, "outputs": []},
        )

    def _supervise_jobs(self, run_id: str) -> None:
        from hub import metadb

        status = self.runs[run_id]
        ref = status.backend_ref
        assert ref is not None and ref.backend == _JOBS_BACKEND
        settled = self._settled[run_id]
        graph = target = job = client = terminal_result = None
        state: str | None = None
        try:
            # Recover the trusted execution envelope. Cancel control still uses only the durable SQL
            # handle, but no cancellation terminal may outrank an already hash-bound result artifact.
            while job is None:
                binding = self._refresh_jobs_binding(run_id)
                if self._resume_staged_binding(status, ref, binding):
                    return
                if self._resume_settled_result_fence(status, ref, binding):
                    return
                if binding.get("submission_state") in _RESULT_RECONCILIATION_STATES:
                    client, ready = self._drive_result_reconciliation(
                        status, ref, binding)
                    if ready:
                        self._publish_reconciled_result(
                            job, graph, target, status)
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                if binding.get("quarantine_reason"):
                    if self._resume_quarantined_job(status, ref, binding):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                cancelling = bool(
                    binding.get("cancel_requested") or self._cancel[status.run_id].is_set()
                )
                try:
                    candidate = self._read_or_materialize_job_artifact(ref, status)
                except (ArtifactNotFound, FileNotFoundError) as e:
                    # A cancel-requested binding that never left queued cannot race a submit: the DB
                    # submit claim observes cancel_requested. It therefore needs no artifact to stop.
                    if (cancelling and binding.get("submission_state") == "queued"):
                        self._publish_cancelled_binding(status, binding)
                        return
                    if cancelling:
                        _requested, cancel_client, cancel_state = self._cancel_control_state(
                            status, ref, binding
                        )
                        client = cancel_client or client
                        if cancel_state in ("MISSING", "STOPPED", "SUCCEEDED", "FAILED"):
                            state = cancel_state
                    self._persist_jobs_live_error(
                        status, self._stable_exception(
                            "Ray Jobs artifact missing; retrying", e, "job_artifact_missing"
                        )
                    )
                    time.sleep(self._jobs_poll_s)
                    continue
                except (ArtifactCorrupt, ArtifactContractError) as e:
                    if self._quarantine_invalid_job(status, ref, e):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                except Exception as e:  # noqa: BLE001 — transport/auth is ambiguous, not corruption
                    if cancelling:
                        _requested, cancel_client, cancel_state = self._cancel_control_state(
                            status, ref, binding
                        )
                        client = cancel_client or client
                        if cancel_state in ("MISSING", "STOPPED", "SUCCEEDED", "FAILED"):
                            state = cancel_state
                    self._persist_jobs_live_error(
                        status, self._stable_exception(
                            "Ray Jobs artifact unavailable; retrying", e,
                            "job_artifact_unavailable",
                        )
                    )
                    time.sleep(self._jobs_poll_s)
                    continue
                try:
                    self._validate_job_artifact_integrity(ref, status, candidate)
                    from hub.models import Graph
                    graph, target = Graph.model_validate(candidate["graph"]), candidate.get("target")
                except Exception as e:  # readable contract corruption must be stopped before publication
                    if self._quarantine_invalid_job(status, ref, e):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                try:
                    # Source pins are SQL-owned generation attestations; sink validation is a read-only
                    # exact allocation lookup. Neither check reopens or advances a writer.
                    self._validate_jobs_source_pins(candidate)
                    self._validate_jobs_sink_attempts(candidate)
                except ArtifactContractError as e:
                    if self._quarantine_invalid_job(status, ref, e):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                except Exception as e:  # metadata transport failure is ambiguous, not corruption
                    self._persist_jobs_live_error(
                        status, self._stable_exception(
                            "Ray Jobs source-pin attestation unavailable; retrying", e,
                            "source_pin_attestation_unavailable",
                        )
                    )
                    time.sleep(self._jobs_poll_s)
                    continue
                job = candidate

            completed = None
            if cancelling:
                try:
                    completed = self._terminal_result_if_present(job)
                except Exception:
                    # Cancellation can still stop the SQL-bound remote job during a result-store outage;
                    # publication remains non-terminal until a later supervisor can validate the object.
                    pass
            if completed is not None and completed.get("artifact_invalid"):
                self._quarantine_invalid_job(
                    status, ref, ArtifactContractError(completed["error"])
                )
                return
            if cancelling and state in ("STOPPED", "MISSING"):
                if completed is not None:
                    self._publish_job_result(job, graph, target, status, completed)
                else:
                    self._publish_cancelled_binding(status, binding, job)
                return

            # Establish backend state. A result object is never consulted while Ray explicitly reports a
            # live state; it is strong terminal evidence only after authoritative job-metadata loss.
            while state is None:
                binding = self._refresh_jobs_binding(run_id)
                if self._resume_staged_binding(status, ref, binding):
                    return
                if self._resume_settled_result_fence(
                        status, ref, binding, job, graph, target):
                    return
                if binding.get("quarantine_reason"):
                    if self._resume_quarantined_job(status, ref, binding):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                cancel_intent = bool(
                    binding.get("cancel_requested") or self._cancel[status.run_id].is_set()
                )
                if cancel_intent:
                    try:
                        completed = self._terminal_result_if_present(job)
                    except Exception:
                        completed = None  # control may stop, but terminal publication still waits for storage
                    if completed is not None and completed.get("artifact_invalid"):
                        self._quarantine_invalid_job(
                            status, ref, ArtifactContractError(completed["error"])
                        )
                        return
                cancelling, cancel_client, cancel_state = self._cancel_control_state(status, ref, binding)
                if cancelling:
                    client = cancel_client or client
                    if cancel_state in ("STOPPED", "MISSING"):
                        completed = self._terminal_result_if_present(job)
                        if completed is not None:
                            self._publish_job_result(job, graph, target, status, completed)
                        else:
                            self._publish_cancelled_binding(status, binding, job)
                        return
                    if cancel_state in ("SUCCEEDED", "FAILED"):
                        state = cancel_state
                        break
                    time.sleep(self._jobs_poll_s)
                    continue
                try:
                    client = self._jobs_client(binding.get("control_address"))
                    state = self._find_job(client, ref.submission_id)
                    self._note_jobs_control_observed(status, ref)
                    if state is not None:
                        self._validate_remote_workload_winner(
                            client, status.run_id, ref.attempt_id, ref.code_ref)
                        self._note_submission_winner(
                            status.run_id, ref.attempt_id)
                    else:
                        completed = self._terminal_result_if_present(job)
                        if completed is not None:
                            if completed.get("artifact_invalid"):
                                self._quarantine_invalid_job(
                                    status, ref, ArtifactContractError(completed["error"])
                                )
                                return
                            if binding.get("submission_state") == "submitting":
                                client, ready = self._drive_result_reconciliation(
                                    status, ref, binding)
                                if ready:
                                    self._publish_reconciled_result(
                                        job, graph, target, status)
                                    return
                                state = None
                            else:
                                terminal_result, state = completed, "SUCCEEDED"
                        else:
                            self._prepare_jobs_submission(job)
                            state = self._ensure_job_submitted(client, job)
                            if state == "CANCEL_REQUESTED":
                                self._cancel[run_id].set()
                                state = None
                    status.error = None
                except ArtifactContractError as e:
                    if self._quarantine_invalid_job(status, ref, e):
                        return
                    state = None
                    time.sleep(self._jobs_poll_s)
                except Exception as e:  # noqa: BLE001 — accepted submit/control outage is ambiguous
                    self._persist_jobs_live_error(
                        status, self._stable_exception(
                            "Ray Jobs control plane unavailable; retrying", e,
                            "control_plane_unavailable",
                        )
                    )
                    state = None
                    time.sleep(self._jobs_poll_s)

            last_visible = None
            while state not in _JOB_TERMINAL:
                binding = self._refresh_jobs_binding(run_id)
                if self._resume_staged_binding(status, ref, binding):
                    return
                if self._resume_settled_result_fence(
                        status, ref, binding, job, graph, target):
                    return
                if binding.get("submission_state") in _RESULT_RECONCILIATION_STATES:
                    client, ready = self._drive_result_reconciliation(
                        status, ref, binding)
                    if ready:
                        self._publish_reconciled_result(
                            job, graph, target, status)
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                if binding.get("quarantine_reason"):
                    if self._resume_quarantined_job(status, ref, binding):
                        return
                    time.sleep(self._jobs_poll_s)
                    continue
                cleared_live_error = False
                cancel_intent = bool(
                    binding.get("cancel_requested") or self._cancel[status.run_id].is_set()
                )
                if cancel_intent:
                    try:
                        completed = self._terminal_result_if_present(job)
                    except Exception:
                        completed = None
                    if completed is not None and completed.get("artifact_invalid"):
                        self._quarantine_invalid_job(
                            status, ref, ArtifactContractError(completed["error"])
                        )
                        return
                cancelling, cancel_client, cancel_state = self._cancel_control_state(status, ref, binding)
                if cancelling:
                    client = cancel_client or client
                    if cancel_state in ("STOPPED", "MISSING"):
                        state = "STOPPED"
                        break
                    if cancel_state in ("SUCCEEDED", "FAILED"):
                        state = cancel_state
                        break
                    # A live/ambiguous cancel never reaches replay or result publication.
                    state = cancel_state or state
                else:
                    try:
                        assert client is not None
                        observed = self._find_job(client, ref.submission_id)
                        self._note_jobs_control_observed(status, ref)
                        if observed is not None:
                            self._validate_remote_workload_winner(
                                client, status.run_id, ref.attempt_id, ref.code_ref)
                            self._note_submission_winner(
                                status.run_id, ref.attempt_id)
                            state = observed
                            if observed not in _JOB_TERMINAL:
                                invalid_result = self._result_contract_error_if_present(job)
                                if invalid_result is not None:
                                    if self._quarantine_invalid_job(status, ref, invalid_result):
                                        return
                        else:
                            # Only authoritative metadata loss opens the result/replay path. An exact,
                            # hash-bound result is terminal proof; missing allows an overwrite-safe replay.
                            completed = self._terminal_result_if_present(job)
                            if completed is not None:
                                if completed.get("artifact_invalid"):
                                    self._quarantine_invalid_job(
                                        status, ref,
                                        ArtifactContractError(completed["error"]),
                                    )
                                    return
                                if binding.get("submission_state") == "submitting":
                                    client, ready = self._drive_result_reconciliation(
                                        status, ref, binding)
                                    if ready:
                                        self._publish_reconciled_result(
                                            job, graph, target, status)
                                        return
                                    state = "METADATA_MISSING"
                                else:
                                    terminal_result, state = completed, "SUCCEEDED"
                            else:
                                self._prepare_jobs_submission(job)
                                state = self._ensure_job_submitted(client, job)
                                if state == "CANCEL_REQUESTED":
                                    self._cancel[run_id].set()
                                    state = "METADATA_MISSING"
                        cleared_live_error = status.error is not None
                        status.error = None
                    except ArtifactContractError as e:
                        if self._quarantine_invalid_job(status, ref, e):
                            return
                        time.sleep(self._jobs_poll_s)
                        continue
                    except Exception as e:  # noqa: BLE001 — no terminal claim on ambiguous control/storage
                        self._persist_jobs_live_error(
                            status, self._stable_exception(
                                "Ray status/control plane unavailable; retrying", e,
                                "status_control_unavailable",
                            )
                        )
                        state = "METADATA_MISSING"
                visible = "running" if state == "RUNNING" else "queued"
                if visible != last_visible or cleared_live_error:
                    status.status = visible
                    from hub import metadb
                    metadb.save_run_state(status.run_id, status.model_dump())
                    last_visible = visible
                time.sleep(self._jobs_poll_s)

            result = None
            if terminal_result is not None:
                result = terminal_result
            elif state == "STOPPED":
                stopped_result = self._terminal_result_if_present(job)
                if stopped_result is not None and stopped_result.get("artifact_invalid"):
                    self._quarantine_invalid_job(
                        status, ref, ArtifactContractError(stopped_result["error"])
                    )
                else:
                    result = stopped_result or {
                        "status": "cancelled", "rows": 0, "outputs": []
                    }
            elif state == "FAILED":
                result = {"status": "failed", "error": self._job_failure(client, ref.submission_id),
                          "rows": 0, "outputs": []}
            else:
                try:
                    result = self._read_job_result(job)
                except ArtifactContractError as e:
                    # Result corruption must win the same durable quarantine fence whether it is
                    # observed immediately before or after Ray reports a terminal state. A transient
                    # control failure leaves result unset so the normal supervisor tail reschedules.
                    self._quarantine_invalid_job(status, ref, e)
                except TerminalResultMissing as e:
                    # Ray is authoritatively terminal here. Missing terminal evidence becomes a failed
                    # run; transport/auth errors propagate and remain reattachable.
                    result = {"status": "failed",
                              "error": f"{type(e).__name__}: result artifact rejected",
                              "rows": 0, "outputs": []}
            if result is not None:
                self._publish_job_result(job, graph, target, status, result)
        except PublicationEffectsWon as publication_winner:
            try:
                if not self._resume_publication_winner(status, ref):
                    raise RuntimeError(
                        "Ray remote control yielded without a durable publication winner"
                    ) from publication_winner
            except Exception as resume_error:  # retain a live owner for the next supervisor
                self._persist_jobs_live_error(
                    status, self._stable_exception(
                        "Ray publication winner recovery interrupted; retrying", resume_error,
                        "publication_winner_recovery_interrupted",
                    ),
                )
        except DurableTerminalObserved:
            pass  # permanent fence already converged the local object; finally stops supervision
        except Exception as e:  # noqa: BLE001 — retain ownership; a fresh supervisor retries reattachment
            self._persist_jobs_live_error(
                status, self._stable_exception(
                    "Ray Jobs supervision interrupted; retrying", e,
                    "supervision_interrupted",
                )
            )
        finally:
            if status.status in ("done", "failed", "cancelled"):
                settled.set()
            with self._lock:
                self._supervising.discard(run_id)
            self._prune_terminal_runs()
            # Every non-terminal exit, including an early return after a transient quarantine/control
            # failure, must retain an autonomous supervisor. API status polling is not a liveness owner.
            if status.status in ("queued", "running"):
                time.sleep(self._jobs_poll_s)
                self._ensure_jobs_supervisor(run_id)

    def _supervise(self, run_id, graph, target, status, materialize_uri=None, requires=None,
                   sink_targets=None, sink_attempts=None) -> None:
        """Run one local Ray driver in an isolated temporary directory and always erase it."""
        import shutil
        import tempfile

        work = tempfile.mkdtemp(prefix="dp_ray_")
        with self._lock:
            self._driver_workdirs[run_id] = work
        result, returncode, proc = None, "not-started", None
        driver_reaped = True  # no Popen means there is no writer to fence
        supervisor_error = None
        read_leases = contextlib.ExitStack()
        read_guards = []
        try:
            from hub.handoff import managed_read_lease
            from hub.storage import preflight_managed_execution_sources
            try:
                deadline = float(os.environ.get("DP_RUN_DEADLINE_S", "3600"))
            except ValueError:
                deadline = 3600.0
            ttl = max(300.0, deadline + 300.0)
            source_uris = preflight_managed_execution_sources(
                self.deps.storage, g.execution_source_uris(graph, target))
            for uri in source_uris:
                read_guards.append(read_leases.enter_context(managed_read_lease(
                    uri, owner=f"ray:{run_id}", ttl_seconds=ttl)))
            result, returncode, proc, driver_reaped, supervisor_error = self._supervise_in_work(
                run_id, graph, target, status, work,
                materialize_uri=materialize_uri, requires=requires,
                sink_targets=sink_targets, sink_attempts=sink_attempts,
            )
        except Exception as exc:  # noqa: BLE001 — provider/process detail belongs only in server logs
            logging.getLogger(__name__).exception("Ray supervisor setup failed")
            supervisor_error = self._stable_exception(
                "Ray supervisor setup failed", exc, "supervisor_setup_failed"
            )

        state = {
            "run_id": run_id, "graph": graph, "target": target, "status": status,
            "work": work, "proc": proc, "result": result, "returncode": returncode,
            "supervisor_error": supervisor_error, "read_leases": read_leases,
            "read_guards": read_guards, "materialize_uri": materialize_uri,
            "sink_targets": sink_targets, "sink_attempts": sink_attempts,
        }
        if not driver_reaped:
            status.status = "running"
            status.stalled = True
            status.error = "Ray driver termination is still being reconciled"
            with self._lock:
                self._unreaped_drivers[run_id] = state
            self._emit(graph, status)  # non-terminal: the driver may still write
            try:
                threading.Thread(
                    target=self._reconcile_unreaped_driver,
                    args=(run_id,), daemon=True, name=f"dp-ray-reap-{run_id}",
                ).start()
            except Exception:  # ownership remains retained for operator/atexit recovery
                logging.getLogger(__name__).exception(
                    "could not start Ray driver reconciliation thread")
            return
        self._finish_supervision(state)

    def _supervise_in_work(self, run_id, graph, target, status, work, materialize_uri=None,
                           requires=None, sink_targets=None,
                           sink_attempts=None) -> tuple[dict | None, int | str, object | None, bool, str | None]:
        """Parent side: spawn the isolated Ray driver, poll its status file, mirror the result. Touches
        NO DuckDB (only subprocess + files + the DB-backed on_status/on_complete hooks) → never deadlocks.
        `materialize_uri` set = region mode (write target → that uri); else whole-graph mode (write node).
        `requires` = the region's resource need, forwarded to the driver → per-task Ray placement.
        `sink_targets` is the hub-resolved write-step-id → logical URI map; `sink_attempts` carries the
        exact parent-claimed physical URI for worker-direct sinks. Region mode omits both."""
        import json
        import subprocess

        cancel = self._cancel[run_id]
        status.status = "running"
        self._emit(graph, status)
        job_file, status_file = os.path.join(work, "job.json"), os.path.join(work, "status.json")
        job = {"workspace": self.deps.workspace, "data_dir": self.deps.data_dir, "target": target,
               "graph": prepare_workload_graph(graph), "module": os.path.abspath(__file__), "requires": requires,
               "materialize_uri": materialize_uri, "attempt_id": run_id, "status_file": status_file}
        if sink_targets is not None:  # whole-graph run only; region materialization has no write sink
            job["sink_targets"] = sink_targets
            job["sink_attempts"] = sink_attempts or {}
        with open(job_file, "w") as f:
            json.dump(job, f)
        driver = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_driver.py")
        result = None
        proc = None
        driver_log = None
        supervisor_error = None
        reaped = True
        try:
            # Redirect the child's stdio to a log file (never an inherited pipe — Ray logs copiously and
            # a full pipe would block the child mid-run; the result comes back via status_file). Own
            # session so Ray's worker signals/pgroup are decoupled from the (daemon-thread) parent.
            driver_log = open(os.path.join(work, "driver.log"), "w")
            # CRITICAL for a kernel launched via `uv run`: Ray detects the uv context and re-launches its
            # WORKERS through uv, which (with the repo pyproject + a VIRTUAL_ENV mismatch) builds a fresh
            # ray-less .venv → workers can't `import ray` → the raylet dies and the run hangs. Strip the
            # uv/venv markers and put the venv's bin on PATH so Ray runs workers with THIS interpreter
            # (which has ray); run from the work dir so uv/Ray don't pick up the repo's pyproject.
            child_env = _ray_child_env()
            proc = subprocess.Popen([sys.executable, driver, job_file], cwd=work,
                                    stdout=driver_log, stderr=driver_log,
                                    start_new_session=True, env=child_env)
            with self._lock:
                self._driver_procs[run_id] = proc
            reaped = False
            while proc.poll() is None:
                if cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    break
                # surface the driver's INTERIM progress (it rewrites the status file as it computes/writes)
                # into the parent RunStatus, so a placed region's progress advances mid-run — not just at
                # the region boundary. A partial read (mid-write) raises → skipped until the next tick.
                try:
                    if os.path.exists(status_file):
                        with open(status_file) as f:
                            interim = json.load(f)
                        if interim.get("status") == "running" and interim.get("progress") is not None:
                            status.progress = float(interim["progress"])
                            if interim.get("rows"):
                                status.rows_processed = int(interim["rows"])
                            self._emit(graph, status)
                except (ValueError, OSError):
                    pass
                time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001 — keep user-visible status non-terminal until reap proof
            logging.getLogger(__name__).exception("Ray driver supervision failed")
            supervisor_error = self._stable_exception(
                "Ray driver control failed", exc, "driver_control_failed"
            )
        finally:
            # A parent-side error must not leave the credential-bearing child running while its work
            # directory is erased. Terminate first, then close the inherited log descriptor.
            if proc is not None:
                reaped = self._try_reap_driver(proc)
            if driver_log is not None:
                driver_log.close()
        if reaped and os.path.exists(status_file):
            try:
                with open(status_file) as f:
                    result = json.load(f)
            except Exception:  # noqa: BLE001 — an invalid receipt can never authorize publication
                logging.getLogger(__name__).exception("Ray driver returned an invalid status document")
                supervisor_error = "Ray execution supervisor failed"
                result = None
        return (result, proc.returncode if proc is not None else "not-started",
                proc, reaped, supervisor_error)

    @staticmethod
    def _try_reap_driver(proc) -> bool:
        """Best-effort fence for one local driver; true only after wait/poll proves exit."""
        try:
            if proc.poll() is not None:
                proc.wait(timeout=0)
                return True
        except Exception:  # noqa: BLE001 — continue to terminate/kill proof
            logging.getLogger(__name__).exception("Ray driver poll/wait failed")
        try:
            proc.terminate()
            proc.wait(timeout=10)
            return True
        except Exception:  # noqa: BLE001 — force-reap below
            try:
                proc.kill()
                proc.wait(timeout=5)
                return True
            except Exception:  # noqa: BLE001 — alive/unknown remains non-terminal
                logging.getLogger(__name__).exception("Ray driver could not be force-reaped")
                return False

    def _reconcile_unreaped_driver(self, run_id: str) -> None:
        """Retain ownership and retry until the local driver is observably stopped."""
        while True:
            with self._lock:
                state = self._unreaped_drivers.get(run_id)
            if state is None:
                return
            if self._try_reap_driver(state["proc"]):
                break
            time.sleep(1.0)
        with self._lock:
            state = self._unreaped_drivers.pop(run_id, None)
            if state is None or run_id in self._finalizing_drivers:
                return
            self._finalizing_drivers.add(run_id)
        status_file = os.path.join(state["work"], "status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file) as f:
                    state["result"] = json.load(f)
            except Exception:  # noqa: BLE001 — malformed terminal data remains private
                logging.getLogger(__name__).exception(
                    "Ray driver returned an invalid status document after reconciliation")
                state["result"] = None
                state["supervisor_error"] = "Ray execution supervisor failed"
        state["returncode"] = state["proc"].returncode
        self._finish_supervision(state)

    def _finish_supervision(self, state: dict) -> None:
        """Publish a terminal result only after the local driver has been proven stopped."""
        import shutil

        run_id, graph, target, status = (
            state["run_id"], state["graph"], state["target"], state["status"])
        cleanup_succeeded = False
        publication_blocked = False
        try:
            for guard in state["read_guards"]:
                guard.check()
        except Exception:  # noqa: BLE001 — a lost source fence forbids output publication
            logging.getLogger(__name__).exception("Ray managed source lease was lost")
            publication_blocked = True
            status.status, status.error = "failed", "Ray managed source lease was lost"

        # job.json contains graph code/source URIs and may contain credentials.  Only a reaped driver
        # reaches this method, so deleting the directory cannot race a live writer.
        try:
            shutil.rmtree(state["work"])
            cleanup_succeeded = True
        except Exception:  # noqa: BLE001 — cleanup failure is a visible terminal contract
            logging.getLogger(__name__).exception("Ray driver workdir cleanup failed")
            status.status, status.error = "failed", "Ray driver workdir cleanup failed"

        if cleanup_succeeded and not publication_blocked:
            try:
                kwargs = {
                    "cancel_requested": bool(
                        self._cancel.get(run_id) and self._cancel[run_id].is_set()),
                }
                if state["sink_targets"] is not None or state["sink_attempts"] is not None:
                    kwargs.update(
                        expected_targets=state["sink_targets"],
                        expected_attempts=state["sink_attempts"],
                    )
                self._settle_popen_result(
                    graph, status, state["result"], state["returncode"], **kwargs)
            except Exception:  # noqa: BLE001 — continue through terminal bookkeeping
                logging.getLogger(__name__).exception("Ray result settlement failed")
                status.status, status.error = "failed", "Ray result settlement failed"

        if status.status != "done":
            from hub.plugins.adapters import is_object_uri
            local_attempts = ([state["materialize_uri"]]
                              if state["materialize_uri"] else [])
            local_attempts.extend((state["sink_attempts"] or {}).values())
            for attempt_uri in local_attempts:
                if is_object_uri(attempt_uri):
                    continue  # remote worker terminal proof is owned by the durable Ray control plane
                try:
                    discard_attempt(attempt_uri)
                except Exception:  # noqa: BLE001 — cleanup detail stays in server logs
                    logging.getLogger(__name__).exception("Ray attempt cleanup failed")

        status.stalled = False
        for item in status.per_node:
            item.status = "done" if status.status == "done" else status.status
        if status.status == "cancelled":
            self._acknowledge_cancel(run_id)
        self._emit(graph, status)
        with self._lock:
            self._cancel.pop(run_id, None)
            self._unreaped_drivers.pop(run_id, None)
            self._finalizing_drivers.discard(run_id)
            self._driver_procs.pop(run_id, None)
            work = self._driver_workdirs.pop(run_id, None)
            if cleanup_succeeded:
                self._retained_workdirs.pop(run_id, None)
            elif work:
                self._retained_workdirs[run_id] = work
        if self.on_complete:
            try:
                self.on_complete(graph, target, status)
            except Exception:  # noqa: BLE001
                pass
        try:
            state["read_leases"].close()
        except Exception:  # noqa: BLE001 — lease expiry is the safe fallback after driver stop
            logging.getLogger(__name__).exception("Ray managed source lease cleanup failed")

    def _settle_popen_result(self, graph, status, result, returncode, *,
                             expected_targets=None, expected_attempts=None,
                             cancel_requested: bool = False) -> None:
        """Apply a local driver result only after its sensitive work directory was erased."""
        # Only a TERMINAL status file is authoritative. A hard kill can leave the last interim
        # {"status":"running", ...} file behind; a dead driver must fail rather than hang forever.
        if result and result.get("status") == "done" and returncode != 0:
            status.status = "failed"
            status.error = "Ray driver exited unsuccessfully after writing a terminal receipt"
            status.output_uri = status.output_table = None
            return
        # A clean done receipt wins a late cancellation because its data commit point has already
        # crossed.  Without that receipt, a reaped driver's cancellation request wins over a crash or
        # incomplete interim document.
        if cancel_requested and not (
                result and result.get("status") == "done" and returncode == 0):
            status.status = "cancelled"
            status.error = None
            status.output_uri = status.output_table = None
            return
        if not (result and result.get("status") in ("done", "failed", "cancelled")):
            status.status = "failed"
            status.error = "Ray driver exited without a terminal status (code=driver_result_missing)"
            return
        should_publish = (
            result["status"] == "done"
            and (expected_targets is not None or bool(result.get("outputs")))
        )
        shared_error = None
        if should_publish:
            try:
                if expected_targets is None and expected_attempts is None:
                    self._register_outputs(graph, result)
                else:
                    self._register_outputs(
                        graph, result, expected_targets=expected_targets,
                        expected_attempts=expected_attempts,
                    )
            except Exception as exc:  # noqa: BLE001 — local parity: catalog commit failure fails the run
                logging.getLogger(__name__).exception("Ray output publication failed")
                shared_error = self._stable_exception(
                    "Catalog registration failed", exc, "catalog_registration_failed"
                )
                result = dict(result, status="failed", error=None)
        # Failed/cancelled outputs stay private. Remote attempts remain fenced as writing until the
        # backend can prove every worker is terminal; the ownership reaper deliberately never guesses.
        status.status = result["status"]
        status.error = (
            shared_error
            or (self._public_remote_error(result["error"]) if result.get("error") else None)
        )
        if status.status == "done":
            status.output_uri, status.output_table = result.get("output_uri"), result.get("output_table")
        else:
            status.output_uri = status.output_table = None
        status.rows_processed = status.total_rows = int(result.get("rows") or 0)
        if status.status == "done":
            status.progress = 1.0

    def _run_ir_sync(self, ir, graph, target, ray_opts=None, progress=None, sink_targets=None,
                     attempt_id: str | None = None, sink_attempts=None,
                     sink_contracts=None) -> dict:
        """Child side (in the driver subprocess, Ray already init'd): execute the clean IR synchronously
        and return a result dict for the parent. Reuses _build/_commit; the fresh-process DuckDB is safe
        because Ray was init'd before it was created. Sink targets are physical URIs resolved by the hub;
        the isolated driver never reads destination settings."""
        outputs: list[dict[str, str]] = []
        attempt_id = attempt_id or f"driver_{uuid.uuid4().hex}"
        try:
            datasets: dict[str, object] = {}
            rows, out_uri, out_table = 0, None, None
            for step in ir.steps:
                if step.op == "write":
                    frozen = (sink_contracts or {}).get(step.id)
                    target_uri = (
                        frozen.get("logical_uri") if isinstance(frozen, dict)
                        else (sink_targets or {}).get(step.id)
                    )
                    if not target_uri:
                        raise RuntimeError(f"missing hub-resolved target URI for write step '{step.id}'")
                    rows, out_uri, out_table = self._commit(
                        step, datasets, target_uri, attempt_id=attempt_id, ray_opts=ray_opts,
                        attempt_uri=(
                            frozen.get("physical_uri") if isinstance(frozen, dict)
                            else (sink_attempts or {}).get(step.id)
                        ),
                        writer=frozen.get("writer") if isinstance(frozen, dict) else None,
                    )
                    outputs.append({"step_id": step.id, "name": out_table, "uri": out_uri,
                                    "logical_uri": target_uri})
                else:
                    datasets[step.id] = self._build(step, datasets, ray_opts)
            if target and target in datasets:  # a non-sink target → force a real row count
                rows = datasets[target].count()
            return {"status": "done", "rows": rows, "output_uri": out_uri,
                    "output_table": out_table, "outputs": outputs}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(e).__name__}: {e}",
                    "rows": 0, "outputs": outputs}

    def _run_ir_materialize(self, ir, graph, target, uri, ray_opts=None, progress=None,
                            attempt_id: str | None = None) -> dict:
        """Child side, region mode: run the clean IR up to `target` on Ray and materialize it to `uri`.
        WORKER-DIRECT WRITE: `uri` becomes a DIRECTORY of parquet shards, each written in parallel by a
        Ray task — nothing funnels through the driver (the old collect→concat→single-file OOM'd on a big
        region). The RunController's ref-read / _output_exists / _move_tier all accept a parts-dir. Reports
        interim `progress` so the parent's placed-region progress advances mid-run."""
        attempt_id = attempt_id or f"driver_{uuid.uuid4().hex}"
        try:
            if progress:
                progress(0.05)
            datasets: dict[str, object] = {}
            for step in ir.steps:
                if step.op == "write":  # a region is cut BEFORE any write; ignore a stray one
                    continue
                datasets[step.id] = self._build(step, datasets, ray_opts)
            rows, out_dir = _write_worker_direct_parquet(
                datasets[target], uri, attempt_id=attempt_id, ray_opts=ray_opts
            )
            if progress:
                progress(0.9, rows)
            return {"status": "done", "rows": rows, "output_uri": out_dir, "output_table": None}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(e).__name__}: {e}", "rows": 0}

    def _build(self, step, datasets, ray_opts=None):
        import ray
        opts = ray_opts or {}

        if step.op == "read":
            uri = step.config["uri"]
            from hub.plugins.adapters import is_object_uri

            adapter = self.resolve_adapter(uri)
            if not _is_builtin_adapter(adapter):
                raise RuntimeError(
                    f"source '{uri}' is claimed by adapter '{type(adapter).__name__}' without an explicit "
                    "bounded/distributed Ray capability"
                )
            if not (_remote_ray() and not is_object_uri(uri)):
                try:
                    native = _native_parquet_plan(uri, adapter)
                except RuntimeError:
                    native = None  # bounded exact-adapter compatibility is the only permitted fallback
                if native is not None:
                    return _read_native_parquet(ray, native, opts)
            return _bounded_adapter_source(uri, adapter, ray)
        parent = datasets[step.inputs[0][0]]                   # clean transforms/passthrough are single-input
        if step.op == "passthrough":
            return parent
        if step.op in CLEAN_TRANSFORM_MODES:
            # `opts` (num_gpus / custom resources from the region's requires) makes Ray schedule each map
            # task onto a worker that has the resource — the planner's placement, honored on the cluster.
            batch_size = _gpu_batch_size(opts)
            batch_kwargs = {"batch_size": batch_size} if batch_size is not None else {}
            result = parent.map_batches(
                _make_mapper(step.config), batch_format="pyarrow", **batch_kwargs, **opts
            )
            if step.op == "filter":
                return _remember_ray_schema(result, _known_ray_schema(parent))
            # A schema-changing UDF's outputSchema is only an empty-result fallback unless enforcement is
            # requested elsewhere. Materialize once in the cluster so non-empty downstream operators use
            # the actual runtime schema; otherwise a stale/narrow declaration could change their semantics.
            result = result.materialize()
            rows = result.count()
            runtime_schema = _runtime_ray_schema(result)
            if rows > 0 and runtime_schema is None:
                raise RuntimeError("a non-empty Ray transform did not expose an Arrow schema")
            # Ray 2.56 can report the parent's schema after a schema-changing UDF emits zero rows. That
            # metadata is stale by construction, so only the portable contract may type an empty result.
            schema = runtime_schema if rows > 0 else _declared_ray_schema(step.config)
            return _remember_ray_schema(result, schema)
        if step.op == "aggregate":
            return self._build_aggregate(step, parent, opts)
        if step.op == "window":
            return self._build_window(step, parent, opts)
        if step.op == "dedup":
            # full-row DISTINCT: shuffle by ALL columns so identical rows colocate in one partition, then
            # DuckDB DISTINCT per partition. Every surviving row is identical to the dups it replaces, so
            # the result is deterministic + byte-identical (unlike keyed DISTINCT ON — gated out above).
            lineage_schema = _arrow_schema(_known_ray_schema(parent))
            parent = parent.materialize()
            rows = parent.count()
            runtime_schema = _runtime_ray_schema(parent)
            if rows > 0 and runtime_schema is None:
                raise RuntimeError("a non-empty Ray dedup input did not expose an Arrow schema")
            schema = runtime_schema if rows > 0 else lineage_schema
            if schema is None:
                raise RuntimeError("an empty Ray dedup input did not expose an Arrow schema")
            parent = _remember_ray_schema(parent, schema)
            return self._shuffle_duckdb(parent, schema.names, "SELECT DISTINCT * FROM _blk", opts)
        if step.op == "join":
            return self._build_join(step, datasets, opts)
        if step.op == "sort":
            keys = parse_sort_keys(step.config.get("by", ""))
            cols = [c for c, _d in keys]
            desc = [d for _c, d in keys]
            # Ray's native sort IS the distributed range-shuffle; repartition(1) then coalesces the ordered
            # range-partitions into ONE block → a single ordered output file (a sharded write's parts read
            # back in arbitrary order would lose the global order). Matches the single-node engine, which
            # also writes one ordered file. `descending` is per-key.
            return _remember_ray_schema(
                parent.sort(cols, descending=desc).repartition(1), _known_ray_schema(parent)
            )
        raise RuntimeError(f"ray backend reached a non-clean op '{step.op}' (should have fallen back)")

    def _build_join(self, step, datasets, ray_opts=None):
        """Distributed BROADCAST join. Collect the RIGHT (small/dimension) side to the driver and broadcast
        it into the map closure, then DuckDB-joins each LEFT block against the FULL right on its worker,
        using the SHARED join_sql — so semantics, output schema, and the `_2`-suffix / USING-coalesce
        naming are byte-identical to the single-node engine. Each left row joins independently against the
        complete right, so inner/left/cross are correct block-by-block (right/full are gated out)."""
        import pyarrow as pa
        import ray

        from hub.executors.engine import join_sql
        left = datasets[step.inputs[0][0]]                     # incoming-edge order = engine's left, right
        right_source = datasets[step.inputs[1][0]]
        declared_right_schema = _known_ray_schema(right_source)
        right_schema_unknown = _ray_schema_explicitly_unknown(right_source)
        right = right_source.materialize()
        try:
            right_bytes = right.size_bytes()
        except Exception:  # noqa: BLE001 — fail closed instead of risking an unbounded driver collect
            right_bytes = None
        _require_driver_fallback(right_bytes, "broadcast join right side")
        materialized_schema = _known_ray_schema(right)
        materialized_arrow_schema = _arrow_schema(materialized_schema)
        declared_right_arrow_schema = _arrow_schema(declared_right_schema)
        refs = ray.get(right.to_arrow_refs())                  # broadcast side: driver → workers
        if refs:
            right_tbl = pa.concat_tables(refs)
            if right_tbl.num_rows == 0:
                if declared_right_arrow_schema is None:
                    raise RuntimeError("an empty broadcast side did not expose an Arrow schema")
                right_tbl = pa.Table.from_batches([], schema=declared_right_arrow_schema)
        else:                                                  # right produced ZERO blocks — keep its TYPED
            right_arrow_schema = declared_right_arrow_schema or (
                None if right_schema_unknown else materialized_arrow_schema
            )
            if right_arrow_schema is None:
                raise RuntimeError("an empty broadcast side did not expose an Arrow schema")
            right_tbl = pa.Table.from_batches([], schema=right_arrow_schema)  # typed NULLs, not null crash
        cfg = step.config
        left_schema = _known_ray_schema(left)
        left_arrow_schema = _arrow_schema(left_schema)
        right_columns = list(right_tbl.column_names)

        def _join_block(tbl):                                  # each LEFT block ⋈ the full broadcast right
            # A declared outputSchema is empty-result lineage, not an instruction to project a non-empty
            # runtime batch. Build the worker SQL from the actual Arrow block so a stale/narrow contract
            # cannot silently drop columns that the UDF really produced.
            con = _secure_duckdb_connection()
            try:
                con.register("_l", tbl)
                con.register("_r", right_tbl)
                sql = join_sql(list(tbl.column_names), right_columns, "_l", "_r",
                               cfg.get("on"), cfg.get("condition"), cfg.get("how"), con=con)
                return con.execute(sql).fetch_arrow_table()
            finally:
                con.close()

        result = left.map_batches(
            _join_block, batch_format="pyarrow", batch_size=_gpu_batch_size(ray_opts),
            **(ray_opts or {})
        )
        empty_sql = join_sql(
            left_arrow_schema.names if left_arrow_schema is not None else [], right_columns, "_l", "_r",
            cfg.get("on"), cfg.get("condition"), cfg.get("how"),
        )
        join_fragments = (
            ((FragmentKind.JOIN_ON.value, str(cfg.get("condition") or "")),)
            if str(cfg.get("condition") or "").strip() else ()
        )
        schema = _duckdb_empty_result_schema(
            empty_sql, policy_fragments=join_fragments, _l=left_schema, _r=right_tbl.schema
        )
        return _remember_ray_schema(result, schema)

    def _shuffle_duckdb(self, parent, keys, sql, ray_opts=None, *, policy_fragments=()):
        """The shared distributed-relational mechanism: RAY hash-shuffles `parent` by `keys` so every row
        of a key-group lands in ONE partition (its default HASH_SHUFFLE), then DUCKDB runs `sql` (reading
        the partition as `_blk`) on each WHOLE partition (batch_size=None → the batch IS the partition, so
        groups are never split). Because each group is complete in its partition, the union of the
        per-partition results equals the single-node DuckDB result BYTE-FOR-BYTE — it IS DuckDB, running
        the same SQL the single-node engine runs, with DuckDB's exact schema. This one mechanism backs
        aggregate/window (and extends to join/dedup) — no operator is reimplemented on Ray."""
        if _gpu_batch_size(ray_opts) is not None:
            raise RuntimeError(
                "GPU placement cannot execute a whole hash partition without breaking batch bounds"
            )

        def _run(tbl):                                          # runs on a WORKER, one complete-groups partition
            con = _secure_duckdb_connection()
            try:
                con.register("_blk", tbl)
                _validate_policy_fragments(con, policy_fragments)
                return con.execute(sql).fetch_arrow_table()
            finally:
                con.close()

        input_schema = _known_ray_schema(parent)
        try:
            parts = int(os.environ.get("DP_RAY_SHUFFLE_PARTITIONS", "0"))
        except ValueError:
            parts = 0
        if parts <= 0:
            # Ray 2.56 requires num_blocks even for a keyed repartition. Materializing first gives us the
            # actual upstream block count through the public API; using the lazy Dataset's private plan
            # would couple the plugin to an unstable Ray implementation detail. This deliberately adds an
            # upstream barrier in auto mode; a correct keyed shuffle must materialize the full input anyway.
            parent = parent.materialize()
            parts = max(1, parent.num_blocks())
        shuffled = parent.repartition(parts, keys=keys)
        result = shuffled.map_batches(
            _run, batch_format="pyarrow", batch_size=None, **(ray_opts or {})
        )
        schema = _duckdb_empty_result_schema(
            sql, policy_fragments=policy_fragments, _blk=input_schema
        )
        return _remember_ray_schema(result, schema)

    def _build_aggregate(self, step, parent, ray_opts=None):
        """Distributed GROUP BY: hash-shuffle by the group key, DuckDB `GROUP BY` per complete partition
        (see _shuffle_duckdb). Any DuckDB aggregate works; only the shuffle key is parsed."""
        cfg = step.config
        keys = parse_group_keys(cfg.get("groupBy", "")) or []   # gating guarantees a non-empty bare-col key
        schema = _arrow_schema(_known_ray_schema(parent))
        if schema is None:
            raise RuntimeError("Ray aggregate input did not expose a schema for GROUP BY validation")
        keys = [identifier(key, schema.names, label="Ray aggregate group column") for key in keys]
        group = ", ".join(quote_identifier(key) for key in keys)
        aggs = (cfg.get("aggs") or "count(*) AS n").strip()     # DuckDB default (mirrors engine.py:649)
        fragments = (
            (FragmentKind.GROUP_BY.value, group),
            (FragmentKind.AGGREGATES.value, aggs),
        )
        return self._shuffle_duckdb(
            parent, keys, f"SELECT {group}, {aggs} FROM {quote_identifier('_blk')} GROUP BY {group}",
            ray_opts, policy_fragments=fragments,
        )

    def _build_window(self, step, parent, ray_opts=None):
        """Distributed window: hash-shuffle by PARTITION BY so each window-partition is complete in one Ray
        partition, then DuckDB runs the SAME `expr OVER (…)` per partition — exact, because the window's
        own ORDER BY (applied by DuckDB on the complete group) sets rank/lag, not the shuffle order.
        Mirrors engine.py's window SQL. Gating guarantees a bare-column PARTITION BY as the shuffle key."""
        cfg = step.config
        keys = parse_group_keys(cfg.get("partitionBy", "")) or []
        schema = _arrow_schema(_known_ray_schema(parent))
        if schema is None:
            raise RuntimeError("Ray window input did not expose a schema for PARTITION BY validation")
        keys = [identifier(key, schema.names, label="Ray window partition column") for key in keys]
        part = ", ".join(quote_identifier(key) for key in keys)
        order = (cfg.get("orderBy") or "").strip()
        expr = (cfg.get("expr") or "").strip()
        col = validate_identifier_alias(
            (cfg.get("as") or "").strip() or "window", label="Ray window output column"
        )
        over = " ".join(x for x in [f"PARTITION BY {part}" if part else "",
                                    f"ORDER BY {order}" if order else ""] if x)
        fragments = [
            (FragmentKind.GROUP_BY.value, part),
            (FragmentKind.WINDOW_EXPR.value, expr),
        ]
        if order:
            fragments.append((FragmentKind.ORDER_BY.value, order))
        return self._shuffle_duckdb(
            parent, keys,
            f"SELECT *, {expr} OVER ({over}) AS {quote_identifier(col)} "
            f"FROM {quote_identifier('_blk')}",
            ray_opts, policy_fragments=tuple(fragments),
        )

    def _commit(self, step, datasets, target_uri: str, *,
                attempt_id: str | None = None,
                ray_opts: dict | None = None,
                attempt_uri: str | None = None,
                writer: str | None = None) -> tuple[int, str, str]:
        cfg = step.config
        spec = SinkSpec.from_config(cfg, cfg.get("title"))
        ds = datasets[step.inputs[0][0]]
        attempt_id = attempt_id or f"driver_{uuid.uuid4().hex}"
        if writer is not None and writer not in ("worker-direct-parquet", "adapter-compat"):
            raise RuntimeError(f"unsupported frozen Ray sink writer {writer!r}")
        adapter = None if writer == "worker-direct-parquet" else self.resolve_adapter(target_uri)
        worker_direct = (
            writer == "worker-direct-parquet" if writer is not None
            else _worker_direct_parquet_sink(spec, target_uri, adapter)
        )
        if worker_direct:
            actual_uri = attempt_uri or _attempt_handoff_uri(target_uri, attempt_id, scope=step.id)
            from hub.plugins.adapters import is_object_uri
            if is_object_uri(target_uri) and attempt_uri is None:
                raise RuntimeError("object sink attempt was not preclaimed by the hub before dispatch")
            rows, actual_uri = _write_worker_direct_parquet(
                ds, actual_uri, attempt_id=attempt_id, ray_opts=ray_opts
            )
            return rows, actual_uri, spec.name
        tbl = _collect_arrow(ds, purpose=(
            f"{spec.mode} {spec.extension} sink"
            + (f" partitioned by {spec.partition_by}" if spec.partition_by else "")
        ))
        with db.base_guard():
            rel = db.conn().from_arrow(tbl)
            committed = commit_sink(spec, rel, self.deps.workspace, self.base.storage,
                                    self.resolve_adapter, target_uri=target_uri)
        return committed.rows, committed.uri, committed.name


def _collect_arrow(dataset, *, purpose: str = "Ray result"):
    """Collect a Ray Dataset only after its materialized Arrow footprint passes the driver limit."""
    import pyarrow as pa

    declared_schema = _known_ray_schema(dataset)
    unknown_schema = _ray_schema_explicitly_unknown(dataset)
    materialized = dataset.materialize()
    try:
        size = materialized.size_bytes()
    except Exception:  # noqa: BLE001 — unknown size is never authorization to collect
        size = None
    _require_driver_fallback(size, purpose)
    batches = []
    decoded_bytes = 0
    for index, batch in enumerate(materialized.iter_batches(batch_format="pyarrow"), start=1):
        if index > _DRIVER_FALLBACK_MAX_BATCHES:
            raise RuntimeError(
                f"{purpose} produced more than {_DRIVER_FALLBACK_MAX_BATCHES:,} driver batches"
            )
        decoded_bytes += int(batch.nbytes)
        _require_driver_fallback(decoded_bytes, f"{purpose} after decoding")
        batches.append(batch)
    if batches:
        return pa.concat_tables(batches)
    materialized_schema = _known_ray_schema(materialized)
    schema = declared_schema if declared_schema is not None else None if unknown_schema else materialized_schema
    arrow_schema = getattr(schema, "base_schema", schema)
    if not isinstance(arrow_schema, pa.Schema):
        raise RuntimeError("an empty Ray result did not expose an Arrow schema")
    return pa.Table.from_batches([], schema=arrow_schema)


def register(reg) -> None:
    # opt-in: added as an available backend, selected only when execution == 'ray-data' (never the default)
    reg.add_runner(RayRunner(reg.deps))
