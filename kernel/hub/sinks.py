"""Shared write-sink contract used by execution backends.

A write node describes a logical destination. Backends must not reinterpret that config independently:
the filename determines the physical format, ``destId``/``destPath`` select a configured place, and
``partitionBy`` is forwarded only when explicitly requested. ``SinkSpec`` normalizes that contract once;
``commit_sink`` is the common adapter commit used by the local and reference Ray runners.
"""

from __future__ import annotations

import inspect
import os
import unicodedata
from dataclasses import dataclass


_KNOWN_EXTENSIONS = (
    ".parquet", ".pq", ".csv", ".tsv", ".arrow", ".feather", ".ipc", ".json", ".lance",
)
_FORMAT_EXTENSIONS = {"parquet": ".parquet", "csv": ".csv", "lance": ".lance"}


class ManagedDatasetNameError(ValueError):
    """A logical managed-dataset name that cannot be interpreted without rewriting it."""

    field = "filename"

    def __init__(self, detail: str, *, reason: str) -> None:
        super().__init__(detail)
        self.reason = reason


def managed_dataset_name(
        value: object, *, format_name: object = "parquet") -> tuple[str, str, str]:
    """Return the exact logical name, physical filename, and extension for one Write name.

    A logical dataset name is one basename, never a path or URI. Accepted characters are preserved
    byte-for-byte; this boundary rejects inputs that would otherwise require silent sanitization.
    """

    if not isinstance(value, str):
        raise ManagedDatasetNameError(
            "managed dataset name must be a string", reason="invalid_type")
    if not value.strip():
        raise ManagedDatasetNameError(
            "managed dataset name must not be blank", reason="blank")
    if value != value.strip():
        raise ManagedDatasetNameError(
            "managed dataset name must not contain surrounding whitespace",
            reason="surrounding_whitespace",
        )
    if (
        value.strip(".") == ""
        or any(character in value for character in ("/", "\\", "\x00", ":"))
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ManagedDatasetNameError(
            "managed dataset name must be one name, not a path or URI",
            reason="path_syntax",
        )

    name, extension = os.path.splitext(value)
    filename = value
    if extension.lower() not in _KNOWN_EXTENSIONS:
        extension = _FORMAT_EXTENSIONS.get(str(format_name or "parquet").lower(), ".parquet")
        name = value
        filename = f"{value}{extension}"
    return name, filename, extension


@dataclass(frozen=True)
class SinkSpec:
    """Normalized, provider-neutral write-node configuration."""

    name: str
    filename: str
    extension: str
    mode: str
    destination_id: str | None
    destination_path: str
    partition_by: str

    def __post_init__(self) -> None:
        # Keep direct construction from bypassing the same invariant enforced by ``from_config``.
        name, filename, extension = managed_dataset_name(self.filename)
        if (self.name, self.filename, self.extension) != (name, filename, extension):
            raise ValueError("sink name, filename, and extension must describe one exact dataset name")

    @classmethod
    def from_config(cls, config: dict | None, title: str | None = None) -> "SinkSpec":
        cfg = config or {}
        if cfg.get("filename") is not None:
            raw = cfg["filename"]
        elif cfg.get("name") is not None:
            raw = cfg["name"]
        elif title:
            raw = title
        else:
            raw = "output"
        name, filename, extension = managed_dataset_name(
            raw, format_name=cfg.get("format") or "parquet")

        mode = cfg.get("writeMode") or "overwrite"
        if mode not in ("overwrite", "append"):
            raise ValueError(f"write mode '{mode}' is not supported — use overwrite or append")

        partition_by = str(cfg.get("partitionBy") or "").strip()
        if partition_by and mode != "overwrite":
            raise ValueError("partitioned write does not support append — use overwrite")
        if partition_by and extension.lower() not in (".parquet", ".pq"):
            raise ValueError("partitionBy is parquet-only (a Hive-partitioned directory)")

        destination_id = str(cfg.get("destId") or "").strip() or None
        return cls(
            name=name,
            filename=filename,
            extension=extension,
            mode=mode,
            destination_id=destination_id,
            destination_path=str(cfg.get("destPath") or ""),
            partition_by=partition_by,
        )

    def target_uri(self, workspace: str, storage) -> str:
        if self.destination_id:
            from hub import destinations

            return destinations.target_uri(
                workspace, self.destination_id, self.destination_path, self.filename
            )
        return storage.output_uri(self.name, self.extension)


def api_sink_spec(config: dict | None, title: str | None = None) -> SinkSpec:
    """Build the shared sink contract and preserve name diagnostics in the HTTP error envelope."""

    try:
        return SinkSpec.from_config(config, title)
    except ManagedDatasetNameError as exc:
        from hub.api_errors import APIError, APIErrorCode

        raise APIError(
            422,
            str(exc),
            code=APIErrorCode.INVALID_MANAGED_DATASET_NAME,
            retryable=False,
            field=exc.field,
            reason=exc.reason,
        ) from exc


@dataclass(frozen=True)
class SinkCommit:
    name: str
    uri: str
    rows: int


def preflight_sink(spec: SinkSpec, workspace: str, storage, resolve_adapter,
                   target_uri: str | None = None) -> str:
    """Resolve a sink and reject an adapter that cannot accept the requested partition contract.

    This is metadata-only. A distributed runner can call it before launching remote work, while
    ``commit_sink`` calls it again at the commit boundary. Unknown destinations and incompatible plugin
    adapters therefore fail or fall back instead of silently dropping sink options.
    """

    # A hub may pass a URI it already resolved from the control-plane destination settings. In that
    # case an isolated driver validates only the adapter contract and never re-reads those settings.
    uri = target_uri if target_uri is not None else spec.target_uri(workspace, storage)
    guard = getattr(storage, "ensure_output_allowed", None)
    if callable(guard):
        guard(uri)
    if not spec.partition_by:
        return uri
    write = resolve_adapter(uri).write
    try:
        params = inspect.signature(write).parameters.values()
    except (TypeError, ValueError):
        return uri  # an opaque callable is checked authoritatively when invoked
    if not any(p.name == "partition_by" or p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
        raise NotImplementedError("the selected adapter does not support partitionBy")
    return uri


def expected_sink_uri(spec: SinkSpec, target_uri: str, adapter) -> str:
    """Published URI implied by the built-in file adapter's shared sink semantics."""
    core_file_adapter = adapter is None or adapter.__class__.__module__ == "hub.plugins.adapters"
    # Only the parquet/csv/json directory-append and Hive partitioning fold into an extension-stripped
    # directory; a Lance append commits in place and keeps its `.lance` URI.
    directory_rewriting = spec.extension.lower() != ".lance"
    if core_file_adapter and directory_rewriting and (spec.mode == "append" or spec.partition_by):
        return os.path.splitext(target_uri)[0]
    return target_uri


def is_core_managed_local_file_sink(spec: SinkSpec, uri: str, adapter, storage) -> bool:
    """Whether core can certify this sink through its immutable local revision ledger."""
    from hub.plugins.adapters import is_object_uri

    return (
        not is_object_uri(uri) and spec.mode == "overwrite"
        and not spec.partition_by
        and spec.extension.lower() in (".parquet", ".pq")
        and adapter.__class__.__module__ == "hub.plugins.adapters"
        and callable(getattr(storage, "begin_result", None))
        and callable(getattr(storage, "commit_result", None))
    )


def is_core_managed_local_lance_append_sink(spec: SinkSpec, uri: str, adapter) -> bool:
    """Whether this sink has the narrow local Lance append shape certified by core."""
    from hub.paths import checked_local_path

    return (
        spec.mode == "append"
        and not spec.partition_by
        and spec.extension.lower() == ".lance"
        and checked_local_path(uri) is not None
        and adapter.__class__.__module__ == "hub.plugins.adapters"
        and adapter.__class__.__name__ == "LanceAdapter"
    )


def commit_sink(spec: SinkSpec, relation, workspace: str, storage, resolve_adapter,
                target_uri: str | None = None, write_adapter=None) -> SinkCommit:
    """Write a relation through the selected adapter using the normalized sink contract."""

    uri = preflight_sink(spec, workspace, storage, resolve_adapter, target_uri=target_uri)
    adapter = resolve_adapter(uri)
    kwargs = {"partition_by": spec.partition_by} if spec.partition_by else {}
    writer = write_adapter or (lambda selected, target, rel, mode, **opts:
                               selected.write(target, rel, mode, **opts))
    result = writer(adapter, uri, relation, spec.mode, **kwargs)
    return SinkCommit(
        name=spec.name,
        uri=result.get("uri", uri),
        rows=int(result.get("rows") or 0),
    )
