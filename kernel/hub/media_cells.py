"""One bounded, certified logical-row media-cell read for managed-local Parquet."""

from __future__ import annotations

import base64
import binascii
import glob
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import duckdb

from hub import db, metadb, paths
from hub.models import MediaCellRequest
from hub.plugins.adapters import is_object_uri, object_fs
from hub.plugins.adapters import RevisionPermissionLost, RevisionProviderOffline, RevisionUnavailable
from hub.plugins.capabilities import (
    media_content_type_from_bytes,
    media_kind_from_value,
)
from hub.sqlpolicy import quote_identifier
from hub.storage import ManagedSourceReadError, source_read_scope
from hub.workspace_providers import ProviderDatasetGone, ProviderDatasetOffline, ProviderDatasetUnavailable

MEDIA_CELL_MAX_BYTES = 16 * 1024 * 1024
_INTEGER_RANGES = {
    "int8": (-2**7, 2**7 - 1),
    "int16": (-2**15, 2**15 - 1),
    "int32": (-2**31, 2**31 - 1),
    "int64": (-2**63, 2**63 - 1),
    "uint8": (0, 2**8 - 1),
    "uint16": (0, 2**16 - 1),
    "uint32": (0, 2**32 - 1),
    "uint64": (0, 2**64 - 1),
}
_INTEGER_DIGITS = {
    "int8": 3,
    "int16": 5,
    "int32": 10,
    "int64": 19,
    "uint8": 3,
    "uint16": 5,
    "uint32": 10,
    "uint64": 20,
}
_SIGNED_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_UNSIGNED_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_MAX_MEDIA_REFERENCE_BYTES = 8192
_MAX_DATA_URI_HEADER_BYTES = 256


class MediaCellError(RuntimeError):
    """Sanitized base error for the public exact-cell boundary."""


class MediaCellIdentityUnavailable(MediaCellError):
    pass


class MediaCellIdentityInvalid(MediaCellError):
    pass


class MediaCellRowNotFound(MediaCellError):
    pass


class MediaCellRowAmbiguous(MediaCellError):
    pass


class MediaCellUnsupported(MediaCellError):
    pass


class MediaCellTooLarge(MediaCellError):
    pass


class MediaCellSourceDenied(MediaCellError):
    pass


class MediaCellUnavailable(MediaCellError):
    pass


class MediaCellOffline(MediaCellError):
    pass


@dataclass(frozen=True)
class ExactMediaCellRead:
    """Core-owned input passed to an opted-in exact media-cell adapter.

    The URI stays in the adapter dispatch argument.  This value deliberately contains no storage
    location, provider configuration, credential, native row id, or positional row address.
    """

    dataset_id: str
    revision_id: str
    identity: tuple[object, ...]
    column: str
    max_bytes: int
    expected_kind: Literal["image", "video"] | None
    source_policy: Callable[[object], bytes]


@dataclass(frozen=True)
class ExactMediaCellResult:
    """One claimed media payload; core revalidates both bytes and claimed type."""

    content: bytes
    content_type: str


def _identity_values(request: MediaCellRequest, certificate) -> list[object]:
    expected = list(certificate.spec.fields)
    supplied = request.identity
    if (len(supplied) != len(expected)
            or any(item.name != field.name or item.arrow_type != field.arrow_type
                   for item, field in zip(supplied, expected, strict=True))):
        raise MediaCellIdentityInvalid("media cell identity is invalid")
    values: list[object] = []
    for item in supplied:
        if item.arrow_type == "string":
            values.append(item.value)
            continue
        pattern = (
            _UNSIGNED_INTEGER if item.arrow_type.startswith("uint") else _SIGNED_INTEGER)
        digits = item.value.removeprefix("-")
        if (len(digits) > _INTEGER_DIGITS[item.arrow_type]
                or not pattern.fullmatch(item.value)):
            raise MediaCellIdentityInvalid("media cell identity is invalid")
        try:
            value = int(item.value)
        except (TypeError, ValueError):
            raise MediaCellIdentityInvalid("media cell identity is invalid") from None
        low, high = _INTEGER_RANGES[item.arrow_type]
        if value < low or value > high:
            raise MediaCellIdentityInvalid("media cell identity is invalid")
        values.append(value)
    return values


def _media_column(columns, name: str):
    matches = [column for column in columns if column.name == name]
    if len(matches) != 1:
        raise MediaCellUnsupported("media cell column is unsupported")
    return matches[0]


def _exact_cell(artifact_uri: str, certificate, column, values: list[object],
                max_bytes: int) -> object:
    fields = list(certificate.spec.fields)
    projection = quote_identifier(column.name)
    predicate = " AND ".join(
        f"{quote_identifier(field.name)} = ?" for field in fields)
    if column.type == "bytes":
        size = f"octet_length({projection})"
        bounded_value = f"CASE WHEN {size} <= ? THEN {projection} END"
        bounds = [max_bytes]
    elif column.type == "string":
        size = f"octet_length(encode({projection}))"
        encoded_limit = ((max_bytes + 2) // 3) * 4 + 4
        data_uri_limit = _MAX_DATA_URI_HEADER_BYTES + 1 + encoded_limit
        limit = (
            f"CASE WHEN starts_with(lower({projection}), 'data:') "
            "THEN ? ELSE ? END"
        )
        bounded_value = f"CASE WHEN {size} <= ({limit}) THEN {projection} END"
        bounds = [data_uri_limit, _MAX_MEDIA_REFERENCE_BYTES]
    else:
        raise MediaCellUnsupported("media cell column is unsupported")
    sql = (
        f"SELECT {bounded_value}, {size} FROM read_parquet(?) "
        f"WHERE {predicate} LIMIT 2"
    )
    rows = db.conn().execute(sql, [*bounds, artifact_uri, *values]).fetchall()
    if not rows:
        raise MediaCellRowNotFound("media cell row was not found")
    if len(rows) != 1:
        raise MediaCellRowAmbiguous("media cell row identity is ambiguous")
    value, size_value = rows[0]
    if value is None and isinstance(size_value, int):
        raise MediaCellTooLarge("media cell exceeds the response limit")
    return value


def _data_uri_bytes(value: str, max_bytes: int) -> bytes:
    try:
        header, encoded = value.split(",", 1)
    except ValueError as exc:
        raise MediaCellUnsupported("media cell value is unsupported") from exc
    if (len(header.encode("utf-8")) > _MAX_DATA_URI_HEADER_BYTES
            or not header.lower().endswith(";base64")):
        raise MediaCellUnsupported("media cell value is unsupported")
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 4:
        raise MediaCellTooLarge("media cell exceeds the response limit")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaCellUnsupported("media cell value is unsupported") from exc
    if len(content) > max_bytes:
        raise MediaCellTooLarge("media cell exceeds the response limit")
    return content


def _uri_bytes(storage, value: str, max_bytes: int) -> bytes:
    try:
        canonical = paths.canonical_data_uri(value)
        local = paths.checked_local_path(canonical)
    except (PermissionError, ValueError) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    if local is not None:
        if glob.has_magic(local) or os.path.isdir(local):
            raise MediaCellSourceDenied("media cell source is not permitted")
        source_uri = local
    elif not is_object_uri(canonical):
        raise MediaCellSourceDenied("media cell source is not permitted")
    elif glob.has_magic(canonical):
        raise MediaCellSourceDenied("media cell source is not permitted")
    else:
        source_uri = canonical
    try:
        with source_read_scope(
                storage, [source_uri],
                owner=f"media-cell-source:{uuid.uuid4().hex}") as guards:
            result = (
                _bounded_local_read(source_uri, guards, max_bytes)
                if local is not None else _bounded_object_read(source_uri, max_bytes)
            )
    except MediaCellTooLarge:
        raise
    except (ManagedSourceReadError, OSError, PermissionError, RuntimeError, ValueError) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    if len(result) > max_bytes:
        raise MediaCellTooLarge("media cell exceeds the response limit")
    return result


def _read_fd(fd: int, max_bytes: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise MediaCellSourceDenied("media cell source is not permitted")
    if info.st_size > max_bytes:
        raise MediaCellTooLarge("media cell exceeds the response limit")
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = max_bytes + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _bounded_local_read(source_uri: str, guards: list, max_bytes: int) -> bytes:
    if guards:
        if len(guards) != 1 or not hasattr(guards[0], "artifact_fileno"):
            raise MediaCellSourceDenied("media cell source is not permitted")
        fd = os.dup(guards[0].artifact_fileno())
    else:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source_uri, flags)
    try:
        return _read_fd(fd, max_bytes)
    finally:
        os.close(fd)


def _bounded_object_read(source_uri: str, max_bytes: int) -> bytes:
    filesystem, path = object_fs(source_uri)
    info = filesystem.get_file_info(path)
    if not info.is_file:
        raise MediaCellSourceDenied("media cell source is not permitted")
    if info.size > max_bytes:
        raise MediaCellTooLarge("media cell exceeds the response limit")
    with filesystem.open_input_file(path) as source:
        content = source.read(max_bytes + 1)
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise MediaCellUnsupported("media cell value is unsupported")
    return bytes(content)


def _cell_bytes(storage, value: object, max_bytes: int) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        if len(value) > max_bytes:
            raise MediaCellTooLarge("media cell exceeds the response limit")
        return bytes(value)
    if not isinstance(value, str):
        raise MediaCellUnsupported("media cell value is unsupported")
    if value.lower().startswith("data:"):
        return _data_uri_bytes(value, max_bytes)
    return _uri_bytes(storage, value, max_bytes)


def _cell_materialization_limit(
        column_type: str, max_bytes: int, *, data_uri: bool = False,
) -> int:
    """Return the largest cell value an adapter may materialize before source policy runs."""
    if column_type == "bytes":
        return max_bytes
    if column_type != "string":
        raise MediaCellUnsupported("media cell column is unsupported")
    if data_uri:
        encoded_limit = ((max_bytes + 2) // 3) * 4 + 4
        return _MAX_DATA_URI_HEADER_BYTES + 1 + encoded_limit
    return _MAX_MEDIA_REFERENCE_BYTES


def read_managed_local_media_cell(
        *, storage, dataset_uri: str, dataset_id: str, revision_id: str,
        request: MediaCellRequest, max_bytes: int = MEDIA_CELL_MAX_BYTES,
) -> tuple[bytes, str]:
    """Return one authorized cell and MIME type while the exact artifact guard remains held."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("media cell response limit must be positive")
    artifact_uri = metadb.managed_local_file_revision_artifact(dataset_id, revision_id)
    if artifact_uri is None:
        raise MediaCellUnavailable("media cell revision is unavailable")
    try:
        columns = metadb.managed_local_file_revision_schema(dataset_uri, revision_id)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise MediaCellUnavailable("media cell revision is unavailable") from exc
    column = _media_column(columns, request.column)
    try:
        with source_read_scope(
                storage, [artifact_uri],
                owner=f"media-cell-revision:{uuid.uuid4().hex}") as guards:
            if len(guards) != 1 or not hasattr(guards[0], "artifact_fileno"):
                raise MediaCellUnavailable("media cell revision is unavailable")
            info = os.fstat(guards[0].artifact_fileno())
            certificate = metadb.managed_local_row_identity_certificate_for_artifact(
                dataset_id, revision_id, artifact_uri,
                artifact_dev=int(info.st_dev), artifact_ino=int(info.st_ino),
            )
            if certificate is None:
                raise MediaCellIdentityUnavailable("media cell row identity is unavailable")
            values = _identity_values(request, certificate)
            with db.base_guard():
                value = _exact_cell(artifact_uri, certificate, column, values, max_bytes)
                expected_kind = media_kind_from_value(value) or column.media_kind
                content = _cell_bytes(storage, value, max_bytes)
    except MediaCellError:
        raise
    except (duckdb.Error, ManagedSourceReadError, OSError, RuntimeError) as exc:
        raise MediaCellUnavailable("media cell is unavailable") from exc
    detected = media_content_type_from_bytes(content)
    if detected is None:
        raise MediaCellUnsupported("media cell value is unsupported")
    kind, content_type = detected
    if expected_kind in {"image", "video"} and kind != expected_kind:
        raise MediaCellUnsupported("media cell value is unsupported")
    return content, content_type


def supports_exact_media_cell(adapter: object, dataset_uri: str, revision_id: str) -> bool:
    """Return an explicit, revision-scoped capability signal without guessing from metadata."""
    try:
        probe = getattr(adapter, "supports_media_cell", None)
        if not callable(probe):
            return False
        return probe(dataset_uri, revision_id) is True
    except (MediaCellError, PermissionError, ConnectionError, TimeoutError,
            RevisionPermissionLost, RevisionProviderOffline, RevisionUnavailable,
            ProviderDatasetGone, ProviderDatasetOffline, ProviderDatasetUnavailable):
        raise
    except Exception:  # noqa: BLE001 -- malformed optional capabilities fail closed
        return False


def read_exact_media_cell(
        *, storage, adapter: object, dataset_uri: str, dataset_id: str, revision_id: str,
        request: MediaCellRequest, max_bytes: int = MEDIA_CELL_MAX_BYTES,
) -> tuple[bytes, str]:
    """Dispatch one bounded exact media read while retaining the existing Parquet path unchanged."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("media cell response limit must be positive")

    try:
        supported = supports_exact_media_cell(adapter, dataset_uri, revision_id)
    except (PermissionError, RevisionPermissionLost) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    except (ConnectionError, TimeoutError, RevisionProviderOffline, ProviderDatasetOffline) as exc:
        raise MediaCellOffline("media cell provider is offline") from exc
    except (RevisionUnavailable, ProviderDatasetGone, ProviderDatasetUnavailable) as exc:
        raise MediaCellUnavailable("media cell revision is unavailable") from exc

    # Managed-local Parquet owns artifact guards, identity certificates, source policy, and the
    # string-reference path.  Keep that mature implementation intact behind the common dispatcher.
    from hub.plugins.adapters import ManagedLocalFileRevisionAdapter
    if isinstance(adapter, ManagedLocalFileRevisionAdapter):
        if not supported:
            raise MediaCellUnavailable("media cell revision is unavailable")
        return read_managed_local_media_cell(
            storage=storage, dataset_uri=dataset_uri, dataset_id=dataset_id,
            revision_id=revision_id, request=request, max_bytes=max_bytes)
    if not supported:
        raise MediaCellUnsupported("media cell value is unsupported")
    try:
        reader = getattr(adapter, "read_media_cell", None)
    except (PermissionError, RevisionPermissionLost) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    except (ConnectionError, TimeoutError, RevisionProviderOffline, ProviderDatasetOffline) as exc:
        raise MediaCellOffline("media cell provider is offline") from exc
    except (RevisionUnavailable, ProviderDatasetGone, ProviderDatasetUnavailable) as exc:
        raise MediaCellUnavailable("media cell revision is unavailable") from exc
    except Exception as exc:  # noqa: BLE001 -- malformed optional method discovery fails closed
        raise MediaCellUnsupported("media cell value is unsupported") from exc
    if not callable(reader):
        raise MediaCellUnsupported("media cell value is unsupported")
    # Generic adapters own their certified identity proof, but core still validates the public
    # typed representation against the exact schema-derived key descriptor supplied by the adapter.
    # The descriptor is intentionally returned separately so an adapter cannot reinterpret a client
    # value as a physical row reference.
    try:
        columns = adapter.revision_schema(dataset_uri, revision_id)
        column = _media_column(columns, request.column)
    except MediaCellError:
        raise
    except (PermissionError, RevisionPermissionLost) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    except (ConnectionError, TimeoutError, RevisionProviderOffline, ProviderDatasetOffline) as exc:
        raise MediaCellOffline("media cell provider is offline") from exc
    except (RevisionUnavailable, ProviderDatasetGone, ProviderDatasetUnavailable) as exc:
        raise MediaCellUnavailable("media cell revision is unavailable") from exc
    except Exception as exc:  # noqa: BLE001 -- provider schema details stay sanitized
        raise MediaCellUnavailable("media cell revision is unavailable") from exc
    try:
        descriptor = adapter.media_cell_identity_descriptor(dataset_uri, revision_id)
        identity = tuple(_identity_values(request, descriptor))
    except MediaCellError:
        raise
    except (PermissionError, RevisionPermissionLost) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    except (ConnectionError, TimeoutError, RevisionProviderOffline, ProviderDatasetOffline) as exc:
        raise MediaCellOffline("media cell provider is offline") from exc
    except (RevisionUnavailable, ProviderDatasetGone, ProviderDatasetUnavailable) as exc:
        raise MediaCellUnavailable("media cell revision is unavailable") from exc
    except Exception as exc:  # noqa: BLE001 -- provider identity details stay sanitized
        raise MediaCellIdentityUnavailable("media cell row identity is unavailable") from exc

    read = ExactMediaCellRead(
        dataset_id=dataset_id, revision_id=revision_id, identity=identity,
        column=column.name, max_bytes=max_bytes,
        expected_kind=column.media_kind if column.media_kind in {"image", "video"} else None,
        source_policy=lambda value: _cell_bytes(storage, value, max_bytes),
    )
    try:
        result = reader(dataset_uri, read)
    except MediaCellError:
        raise
    except (PermissionError, RevisionPermissionLost) as exc:
        raise MediaCellSourceDenied("media cell source is not permitted") from exc
    except (ConnectionError, TimeoutError, RevisionProviderOffline, ProviderDatasetOffline) as exc:
        raise MediaCellOffline("media cell provider is offline") from exc
    except (KeyError, LookupError, FileNotFoundError, RevisionUnavailable,
            ProviderDatasetGone, ProviderDatasetUnavailable) as exc:
        raise MediaCellUnavailable("media cell revision is unavailable") from exc
    except Exception as exc:  # noqa: BLE001 -- never expose provider details at the public boundary
        raise MediaCellUnavailable("media cell is unavailable") from exc
    if not isinstance(result, ExactMediaCellResult):
        raise MediaCellUnsupported("media cell value is unsupported")
    if not isinstance(result.content, bytes) or not isinstance(result.content_type, str):
        raise MediaCellUnsupported("media cell value is unsupported")
    if len(result.content) > max_bytes:
        raise MediaCellTooLarge("media cell exceeds the response limit")
    detected = media_content_type_from_bytes(result.content)
    if detected is None:
        raise MediaCellUnsupported("media cell value is unsupported")
    kind, content_type = detected
    if read.expected_kind is not None and kind != read.expected_kind:
        raise MediaCellUnsupported("media cell value is unsupported")
    return result.content, content_type
