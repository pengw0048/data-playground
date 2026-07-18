"""Immutable local-run input-manifest validation and exact Source binding."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import uuid

from hub import db, graph as graph_mod, metadb
from hub.backends import DatasetRevisionAdapter
from hub.models import dataset_ref_identity
from hub.plugins.adapters import (
    DuckDBAdapter,
    RevisionPermissionLost,
    RevisionProviderOffline,
    revision_adapter_for_uri,
)

_MANIFEST_FIELDS = {"node_id", "dataset_id", "revision_id", "provider", "resolved_at"}
LOCAL_FILE_INPUT_PROVIDER = "local-file-snapshot"
_LOCAL_FILE_INPUT_EXTENSIONS = (
    ".parquet", ".pq", ".csv", ".tsv", ".json", ".ndjson",
    ".arrow", ".feather", ".ipc",
)


class LocalRunInputError(RuntimeError):
    """The admitted local-run input contract is malformed, stale, or unavailable."""


def supports_local_file_snapshot(uri: str, adapter) -> bool:
    """Whether automatic exact admission can snapshot this one ordinary local Source."""
    from hub import paths

    path = paths.checked_local_path(uri)
    return bool(
        isinstance(adapter, DuckDBAdapter)
        and path is not None
        and os.path.isfile(path)
        and path.lower().endswith(_LOCAL_FILE_INPUT_EXTENSIONS)
    )


def _source_options(config: dict) -> dict[str, str]:
    options = {
        key: (str(config.get(key, "")).strip().lower()
              if key == "header" else str(config.get(key, "")).strip())
        for key in ("delimiter", "header") if str(config.get(key, "")).strip()
    }
    return {
        key: value for key, value in options.items()
        if key != "header" or value in ("yes", "no")
    }


@contextlib.contextmanager
def _stable_local_file_copy(storage, uri: str, artifact_uri: str):
    """Copy one regular source while proving its path and inode did not change mid-read."""
    from hub import paths

    path = paths.checked_local_path(uri)
    result_root = getattr(storage, "result_root", None)
    namespace_identity = getattr(storage, "result_namespace_identity", None)
    if path is None or not isinstance(result_root, str) or not callable(namespace_identity):
        raise LocalRunInputError(
            "ordinary local input exact admission requires the built-in local result storage")
    if os.path.dirname(artifact_uri) != result_root:
        raise LocalRunInputError("ordinary local input candidate is outside managed result storage")
    temp_path = f"{artifact_uri}.tmp-{uuid.uuid4().hex[:8]}"
    source_fd = temp_fd = None
    try:
        source_fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(source_fd)
        visible_before = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (
                    visible_before.st_dev, visible_before.st_ino)):
            raise LocalRunInputError(
                "ordinary local exact admission supports one regular file per Source")
        root_before = tuple(namespace_identity())
        temp_fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("short write while snapshotting local run input")
                view = view[written:]
        os.fsync(temp_fd)
        after = os.fstat(source_fd)
        visible_after = os.stat(path, follow_symlinks=False)
        identity = lambda value: (  # noqa: E731 - one exact stat tuple is easier to audit inline
            value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )
        if (identity(before) != identity(after)
                or identity(after) != identity(visible_after)
                or copied != before.st_size
                or tuple(namespace_identity()) != root_before):
            raise LocalRunInputError(
                "ordinary local input changed while its exact binding was created")
        os.close(temp_fd)
        temp_fd = None
        yield temp_path, digest.hexdigest()
    except LocalRunInputError:
        raise
    except (OSError, ValueError) as exc:
        raise LocalRunInputError(
            "ordinary local input could not be bound to an immutable exact snapshot") from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if source_fd is not None:
            os.close(source_fd)
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)


def _candidate_writer_id(dataset_id: str) -> str:
    identity = hashlib.sha256(dataset_id.encode()).hexdigest()[:32]
    return f"local-file-input:{identity}"


def snapshot_local_file_input(
        *, uri: str, config: dict, dataset_id: str, adapter, storage,
        ) -> tuple[str, dict[str, str] | None]:
    """Create or reuse one canonical immutable Parquet binding for an ordinary local file."""
    if not isinstance(adapter, DuckDBAdapter):
        raise LocalRunInputError(
            "source adapter cannot create an immutable exact local-file binding")
    options = _source_options(config)
    format_name = os.path.splitext(uri.split("?", 1)[0])[1].lower()
    writer_id = _candidate_writer_id(dataset_id)
    begin = getattr(storage, "begin_result", None)
    commit = getattr(storage, "commit_result", None)
    abort = getattr(storage, "abort_result", None)
    if not callable(begin) or not callable(commit) or not callable(abort):
        raise LocalRunInputError(
            "ordinary local input exact admission requires managed local-result ownership")
    artifact_uri = begin(f"local-input:{dataset_id}", writer_id)
    try:
        with _stable_local_file_copy(
                storage, uri, artifact_uri) as (snapshot_path, content_sha256):
            semantic = json.dumps({
                # Identity describes the user's stable bytes plus the declared interpretation.
                # Canonical Parquet is the retained transport, not the identity: retries/upgrades
                # must keep reopening the first admitted rows instead of silently re-parsing them.
                "contract": "local-file-input-v1",
                "content_sha256": content_sha256,
                "format": format_name,
                "options": options,
            }, sort_keys=True, separators=(",", ":"))
            revision_id = hashlib.sha256(semantic.encode()).hexdigest()
            if metadb.local_file_input_revision_artifact(dataset_id, revision_id) is not None:
                abort(artifact_uri, writer_id)
                return revision_id, None
            with db.base_guard():
                relation = adapter.scan_local_snapshot(snapshot_path, uri, options=options)
                adapter.write(artifact_uri, relation, mode="overwrite")
            commit(artifact_uri, writer_id)
    except LocalRunInputError:
        abort(artifact_uri, writer_id)
        raise
    except Exception as exc:
        abort(artifact_uri, writer_id)
        raise LocalRunInputError(
            "ordinary local input could not be parsed into an immutable exact binding") from exc
    except BaseException:
        abort(artifact_uri, writer_id)
        raise
    return revision_id, {
        "dataset_id": dataset_id,
        "revision_id": revision_id,
        "artifact_uri": artifact_uri,
    }


def _finalize_local_file_candidates(
        storage, candidates: list[dict[str, str]], admitted: list[dict[str, str]] | None,
        ) -> None:
    """Release winning snapshot writers or abort candidates excluded by one admission."""
    admitted_ids = {
        (item["dataset_id"], item["revision_id"])
        for item in admitted or [] if item.get("provider") == LOCAL_FILE_INPUT_PROVIDER}
    for candidate in candidates:
        identity = (candidate["dataset_id"], candidate["revision_id"])
        writer_id = _candidate_writer_id(candidate["dataset_id"])
        try:
            winner = metadb.local_file_input_revision_artifact(*identity)
        except Exception:
            continue
        if identity in admitted_ids and winner == candidate["artifact_uri"]:
            if not storage.release_result(candidate["artifact_uri"], writer_id):
                raise RuntimeError(
                    "local file input admission is missing its durable artifact owner")
        else:
            storage.abort_result(candidate["artifact_uri"], writer_id)


def finalize_local_file_candidates(
        storage, candidates: list[dict[str, str]], run_id: str) -> None:
    """Finalize candidates against one ordinary local-run admission."""
    if not candidates:
        return
    try:
        admitted = metadb.local_run_input_manifest(run_id)
    except Exception:
        # Unknown DB outcome: retain each exact writer fence for retry/dead-writer reconciliation.
        return
    _finalize_local_file_candidates(storage, candidates, admitted)


def finalize_durable_task_local_file_candidates(
        storage, candidates: list[dict[str, str]], task_id: str) -> None:
    """Finalize candidates against the persisted Task after a known or uncertain commit."""
    if not candidates:
        return
    try:
        task = metadb.durable_task(task_id)
        admitted = (
            task["input_manifest"]
            if task is not None and task.get("task_kind") == "managed_local_write" else None)
    except Exception:
        # Unknown DB outcome: retain each exact writer fence for retry/dead-writer reconciliation.
        return
    _finalize_local_file_candidates(storage, candidates, admitted)


def validate_manifest(value: object) -> list[dict[str, str]]:
    """Return a copied, ordered, secret-free manifest or fail closed."""
    if not isinstance(value, list) or any(
            not isinstance(item, dict) or set(item) != _MANIFEST_FIELDS
            or any(not isinstance(part, str) or not part for part in item.values())
            for item in value):
        raise LocalRunInputError("local run input manifest is malformed")
    return [{field: item[field] for field in (
        "node_id", "dataset_id", "revision_id", "provider", "resolved_at")}
        for item in value]


def source_nodes(graph, target_node_id: str | None):
    """The ordered Source cone whose identity is attested by a manifest."""
    cone = graph_mod.upstream_chain(graph, target_node_id) if target_node_id else graph.nodes
    return [node for node in cone if node.type == "source"]


def validate_manifest_graph(graph, target_node_id: str | None, manifest: object, *,
                            require_bound_revisions: bool) -> list[dict[str, str]]:
    """Ensure one manifest exactly covers the target's ordered Source cone."""
    admitted = validate_manifest(manifest)
    sources = source_nodes(graph, target_node_id)
    if [str(node.id) for node in sources] != [item["node_id"] for item in admitted]:
        raise LocalRunInputError("local run input manifest does not match the graph")
    if require_bound_revisions:
        for node, item in zip(sources, admitted, strict=True):
            data = node.data if isinstance(node.data, dict) else {}
            config = data.get("config") if isinstance(data, dict) else None
            if (not isinstance(config, dict)
                    or config.get("_input_dataset_id") != item["dataset_id"]
                    or config.get("_input_provider") != item["provider"]
                    or config.get("_input_revision_id") != item["revision_id"]):
                raise LocalRunInputError("local run input manifest does not match bound source identity")
    return admitted


def bind_manifest(
    graph, target_node_id: str | None, manifest: object, resolve_adapter, *,
    allow_prebound_provider: bool = False,
):
    """Reopen admitted provider revisions and bind them only to a private dispatch graph."""
    admitted = validate_manifest_graph(
        graph, target_node_id, manifest, require_bound_revisions=False)
    bound = graph.model_copy(deep=True)
    sources = source_nodes(bound, target_node_id)
    for node, item in zip(sources, admitted, strict=True):
        config = node.data.get("config", {}) if isinstance(node.data, dict) else {}
        source_uri = str(config.get("uri") or "") if isinstance(config, dict) else ""
        from hub import workspace_providers
        prebound_provider_uri = (
            str(config.get("_input_provider_uri"))
            if (allow_prebound_provider and isinstance(config, dict)
                and isinstance(config.get("_input_provider_uri"), str)
                and config.get("_input_dataset_id") == item["dataset_id"]
                and config.get("_input_provider") == item["provider"]
                and config.get("_input_revision_id") == item["revision_id"])
            else ""
        )
        provider_dataset_id = (
            item["dataset_id"] if prebound_provider_uri
            else workspace_providers.provider_dataset_identity(source_uri) if source_uri else None)
        source_binding = (metadb.catalog_revision_binding_for_uri(source_uri)
                          if provider_dataset_id is None else None)
        dataset_ref = config.get("datasetRef") if isinstance(config, dict) else None
        try:
            selected_identity = (dataset_ref_identity(dataset_ref)
                                 if isinstance(dataset_ref, dict) else None)
        except ValueError as exc:
            raise LocalRunInputError("local run input manifest does not match the graph") from exc
        # Canonical ExecutionManifest graphs intentionally replace provider paths with one exact
        # DatasetRef. Reopen its current registered URI only to reach the already-admitted revision;
        # the dataset/revision/provider tuple below remains the authority and prevents rebinding.
        if not source_uri and selected_identity == (item["dataset_id"], item["revision_id"]):
            binding = metadb.catalog_revision_binding(item["dataset_id"])
            if binding is not None:
                source_uri = str(binding["uri"])
                source_binding = metadb.catalog_revision_binding_for_uri(source_uri)
        if item["provider"] == LOCAL_FILE_INPUT_PROVIDER:
            if (source_binding is None
                    or str(source_binding["dataset_id"]) != item["dataset_id"]
                    or (selected_identity is not None and selected_identity != (
                        item["dataset_id"], item["revision_id"]))):
                raise LocalRunInputError("local run input manifest does not match the graph")
            artifact_uri = metadb.local_file_input_revision_artifact(
                item["dataset_id"], item["revision_id"])
            if artifact_uri is None:
                raise LocalRunInputError("local run input revision is unavailable")
            revision_uri = artifact_uri
        elif ((provider_dataset_id is None and source_binding is None)
                or (provider_dataset_id if provider_dataset_id is not None
                    else str(source_binding["dataset_id"])) != item["dataset_id"]
                or (selected_identity is not None and selected_identity != (
                    item["dataset_id"], item["revision_id"]))):
            raise LocalRunInputError("local run input manifest does not match the graph")
        elif provider_dataset_id is not None:
            revision_uri = prebound_provider_uri or source_uri
        else:
            try:
                binding = metadb.catalog_revision_binding(item["dataset_id"])
            except Exception as exc:
                raise LocalRunInputError("local run input revision is unavailable") from exc
            if binding is None:
                raise LocalRunInputError("local run input revision is unavailable")
            uri = str(binding["uri"])
            revision_uri = uri
        try:
            adapter = revision_adapter_for_uri(revision_uri, resolve_adapter)
        except (PermissionError, RevisionPermissionLost, RevisionProviderOffline,
                ConnectionError, TimeoutError, OSError,
                workspace_providers.ProviderDatasetUnavailable):
            raise
        except Exception as exc:
            raise LocalRunInputError("local run input revision is unavailable") from exc
        exact_adapter = (isinstance(adapter, DatasetRevisionAdapter)
                         if prebound_provider_uri
                         else workspace_providers.provider_dataset_supports_exact(adapter)
                         if provider_dataset_id is not None
                         else isinstance(adapter, DatasetRevisionAdapter))
        if provider_dataset_id is not None and not exact_adapter:
            raise LocalRunInputError(
                "provider dataset is mutable-only and cannot enter an immutable run manifest")
        if (not exact_adapter
                or str(getattr(adapter, "name", "") or "") != item["provider"]):
            raise LocalRunInputError("local run input revision is unavailable")
        try:
            with db.base_guard():
                adapter.open_revision(revision_uri, item["revision_id"])
        except (PermissionError, RevisionPermissionLost, RevisionProviderOffline,
                ConnectionError, TimeoutError, OSError,
                workspace_providers.ProviderDatasetUnavailable):
            raise
        except Exception as exc:
            raise LocalRunInputError("local run input revision is unavailable") from exc
        provider_dispatch_uri = (
            prebound_provider_uri
            if prebound_provider_uri else
            workspace_providers.provider_dataset_dispatch_uri(adapter, source_uri)
            if provider_dataset_id is not None else revision_uri
        )
        config = node.data.setdefault("config", {})
        # The exact DatasetRef is the canonical persisted identity. Once it has been checked against
        # the manifest above, the private dispatch fields below are the execution binding; leaving
        # the ref in place would make Source planning consult the mutable catalog again.
        config.pop("datasetRef", None)
        # Source planning still requires a logical URI even when execution will open the retained
        # local snapshot below. This is a private dispatch copy, so restoring the catalog URI does
        # not weaken or leak into the canonical manifest identity.
        config["uri"] = (source_uri if item["provider"] == LOCAL_FILE_INPUT_PROVIDER
                         or provider_dataset_id is not None else revision_uri)
        if provider_dataset_id is not None:
            # One-shot dispatch capability: child kernels need the already-authorized physical URI,
            # never mount config. Canonical Canvas/history/manifest documents retain only the stable
            # synthetic identity and exact tuple.
            config["_input_provider_uri"] = provider_dispatch_uri
        # Keep the complete manifest identity on the private dispatch copy. Revision ids are only
        # provider-local and can restart after a dataset is unregistered/replaced at the same URI;
        # cache/profile keys must therefore include dataset and provider identity as well.
        config["_input_dataset_id"] = item["dataset_id"]
        config["_input_provider"] = item["provider"]
        config["_input_revision_id"] = item["revision_id"]
        artifact_uri = (metadb.local_file_input_revision_artifact(
            item["dataset_id"], item["revision_id"])
            if item["provider"] == LOCAL_FILE_INPUT_PROVIDER
            else metadb.managed_local_file_revision_artifact(
                item["dataset_id"], item["revision_id"]))
        if artifact_uri is not None:
            # Execution ownership must fence the selected old artifact, not the mutable catalog head.
            config["_input_artifact_uri"] = artifact_uri
            bound._input_artifact_uris[str(node.id)] = artifact_uri
    return bound
