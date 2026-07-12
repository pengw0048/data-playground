"""Validated compatibility boundary for the bundled ``dp_ray`` backend.

The backend currently patches one private Ray Data hash-shuffle function. Keep the supported runtime
exact until that private ABI is removed or a wider range is exercised by the multi-node differential.
This module is installed on both the driver and workers so version/ABI failures happen before data runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SUPPORTED_RAY_VERSION = "2.56.0"


class RayCompatibilityError(RuntimeError):
    """The configured Ray runtime cannot satisfy dp_ray's validated execution contract."""


def _version_error(observed: Mapping[str, str]) -> RayCompatibilityError:
    versions = sorted(set(observed.values()))
    detail = ", ".join(f"{where}={version}" for where, version in observed.items())
    if len(versions) > 1:
        reason = f"mixed Ray versions are unsupported ({detail})"
    else:
        reason = f"unsupported Ray version ({detail})"
    return RayCompatibilityError(
        f"{reason}; dp_ray supports exactly Ray {SUPPORTED_RAY_VERSION} because its hash-shuffle "
        "compatibility shim depends on that private ABI. Install `data-playground[ray]` on every node "
        "or use the repository's docker/ray image."
    )


def validate_ray_versions(driver_version: str, worker_versions: Mapping[str, str]) -> None:
    """Reject unsupported or mixed driver/worker versions with one actionable error."""
    observed = {"driver": str(driver_version), **{str(k): str(v) for k, v in worker_versions.items()}}
    if not worker_versions:
        raise RayCompatibilityError("Ray version handshake found no alive cluster nodes")
    if any(version != SUPPORTED_RAY_VERSION for version in observed.values()):
        raise _version_error(observed)


def _worker_runtime_version() -> str:
    import ray
    return str(ray.__version__)


def _node_affinity(node_id: str):
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
    return NodeAffinitySchedulingStrategy(node_id=node_id, soft=False)


def validate_ray_cluster(ray_module: Any) -> dict[str, str]:
    """Handshake with every alive Ray node before any Dataset source or operator is built.

    A node-affinity task prevents a healthy node from answering on behalf of a mismatched one. Returning
    the report is useful for startup logs and for a deterministic fake-cluster regression test.
    """
    nodes = [node for node in ray_module.nodes() if node.get("Alive", node.get("alive", False))]
    if not nodes:
        raise RayCompatibilityError("Ray version handshake found no alive cluster nodes")
    probe = ray_module.remote(num_cpus=0)(_worker_runtime_version)
    refs = []
    labels = []
    try:
        for node in nodes:
            node_id = str(node.get("NodeID") or "")
            if not node_id:
                raise RayCompatibilityError("Ray version handshake found an alive node without a NodeID")
            label = str(node.get("NodeManagerAddress") or node_id)
            labels.append(f"node[{label}:{node_id[:8]}]")
            refs.append(probe.options(scheduling_strategy=_node_affinity(node_id)).remote())
        versions = ray_module.get(refs, timeout=30)
    except RayCompatibilityError:
        raise
    except Exception as exc:  # noqa: BLE001 — turn scheduler/runtime details into an operator action
        raise RayCompatibilityError(
            "Ray worker version handshake failed before data execution; verify every cluster node uses "
            f"the Ray {SUPPORTED_RAY_VERSION} data-playground image ({type(exc).__name__}: {exc})"
        ) from exc
    report = dict(zip(labels, (str(version) for version in versions), strict=True))
    validate_ray_versions(str(ray_module.__version__), report)
    return report


def _require_hash_shuffle_abi(transform_module: Any):
    """Return Ray 2.56's private target only after checking every attribute we dereference."""
    target = getattr(transform_module, "_hash_partition", None)
    if not callable(target):
        raise RayCompatibilityError(
            f"Ray {SUPPORTED_RAY_VERSION} does not expose the expected callable "
            "ray.data._internal.arrow_ops.transform_pyarrow._hash_partition; the private ABI changed. "
            "Refusing distributed execution until dp_ray is revalidated."
        )
    if not callable(getattr(transform_module, "_has_unhashable_pandas_types", None)):
        raise RayCompatibilityError(
            f"Ray {SUPPORTED_RAY_VERSION} is missing the expected hash-shuffle schema helper; "
            "the private ABI changed and dp_ray must be revalidated."
        )
    return target


def patch_hash_shuffle() -> None:
    """Install the validated Ray-2.56 writable-hash fix on a driver or worker.

    The function is intentionally fail-loud: the worker setup hook must never continue with an unknown
    Ray version or silently skip a private-ABI mismatch, because that would make shuffle correctness
    depend on which worker happened to execute a block.
    """
    try:
        import numpy as np
        import ray
        from ray.data._internal.arrow_ops import transform_pyarrow as T
    except Exception as exc:  # noqa: BLE001 — worker startup needs an actionable compatibility failure
        raise RayCompatibilityError(
            f"dp_ray could not import its Ray {SUPPORTED_RAY_VERSION} compatibility dependencies "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    if str(ray.__version__) != SUPPORTED_RAY_VERSION:
        raise _version_error({"runtime": str(ray.__version__)})
    original = _require_hash_shuffle_abi(T)
    if getattr(original, "_dp_patched", False):
        return

    def _hash_partition(table, num_partitions):  # faithful Ray 2.56 logic + writable hash values
        if T._has_unhashable_pandas_types(table.schema):
            partitions = np.zeros((table.num_rows,), dtype=np.int64)
            for i, _tuple in enumerate(zip(*table.columns)):
                partitions[i] = hash(_tuple) % num_partitions
            return partitions
        import pandas as pd
        hashes = np.array(pd.util.hash_pandas_object(
            table.to_pandas(types_mapper=pd.ArrowDtype), index=False).values, copy=True)
        np.mod(hashes, num_partitions, out=hashes)
        return hashes

    _hash_partition._dp_patched = True
    T._hash_partition = _hash_partition
