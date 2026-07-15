"""Attempt-scoped destination credential identity for metadata-isolated workloads.

The control plane freezes a non-secret binding before dispatch. Local child processes receive the
resolved material through an inherited anonymous pipe; durable remote jobs receive only SecretRefs and
resolve them in their own trusted workload environment. Raw material never belongs in a graph, job
envelope, metadata database, log, status, or telemetry document.

This module is deliberately provider-neutral. A future scoped workload-identity provider can replace the
capability transport while keeping the binding and reauthorization contract unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any


DESTINATION_CREDENTIAL_ERROR_CODE = "destination_credential_unavailable"
DESTINATION_CREDENTIAL_ERROR_REASON = "The authorized destination credential is unavailable."
DESTINATION_CREDENTIAL_REFERENCE_ENV = "DP_DESTINATION_CREDENTIAL_REFS"

_BINDING_VERSION = 1
_CAPABILITY_VERSION = 1
# Keep the complete payload below the minimum practical anonymous-pipe capacity.  The trusted parent
# writes before spawning the child, so a larger payload could otherwise block waiting for a reader that
# does not exist yet. Object-store configs are deliberately tiny; exceeding this is a malformed contract.
_CAPABILITY_MAX_BYTES = 8 * 1024
_OBJECT_STORE_FIELDS = frozenset({
    "accessKeyId", "secretAccessKey", "sessionToken", "region", "endpoint", "useSsl",
})
_SECRET_FIELDS = ("accessKeyId", "secretAccessKey", "sessionToken")


class DestinationCredentialError(RuntimeError):
    """Stable, non-secret failure at the destination workload-identity boundary."""

    code = DESTINATION_CREDENTIAL_ERROR_CODE

    def __init__(self) -> None:
        super().__init__(DESTINATION_CREDENTIAL_ERROR_REASON)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_references(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(key not in _OBJECT_STORE_FIELDS for key in value):
        raise DestinationCredentialError()
    return dict(value)


def _validate_binding(value: object) -> dict:
    if not isinstance(value, dict):
        raise DestinationCredentialError()
    common = {"version", "scope", "mode", "destination_id"}
    mode = value.get("mode")
    expected = (
        common | {"credential_id"} if mode == "cred"
        else common | {"reference_sha256"} if mode == "legacy"
        else common if mode == "ambient"
        else set()
    )
    if (set(value) != expected or value.get("version") != _BINDING_VERSION
            or value.get("scope") != "destination-write"
            or value.get("destination_id") is not None
            and not isinstance(value.get("destination_id"), str)):
        raise DestinationCredentialError()
    if mode == "cred" and (
            not isinstance(value.get("credential_id"), str) or not value["credential_id"]):
        raise DestinationCredentialError()
    if mode == "legacy" and (
            not isinstance(value.get("reference_sha256"), str)
            or len(value["reference_sha256"]) != 64):
        raise DestinationCredentialError()
    return dict(value)


def _current_references(binding: dict) -> dict[str, Any]:
    from hub import metadb

    mode = binding["mode"]
    if mode == "ambient":
        return {}
    if mode == "cred":
        record = metadb.cred_get(binding["credential_id"])
        if not record or record.get("kind") != "object_store":
            raise DestinationCredentialError()
        return _safe_references(record.get("fields"))
    refs = _safe_references(
        metadb.get_setting("objectStore", "global", default={}) or {})
    if not refs or _digest(refs) != binding["reference_sha256"]:
        # Legacy settings have no stable entity ID. A reference change is a new identity and therefore
        # requires a new run; rotating the material behind the same refs remains valid.
        raise DestinationCredentialError()
    return refs


def _resolve_references(references: object, *, require_explicit_identity: bool) -> dict[str, Any]:
    from hub.secrets import resolve_secret_value

    refs = _safe_references(references)
    try:
        resolved = dict(refs)
        for key in _SECRET_FIELDS:
            if key in resolved and resolved[key] not in (None, ""):
                resolved[key] = resolve_secret_value(resolved[key], allow_plaintext=False)
        has_access = bool(resolved.get("accessKeyId"))
        has_secret = bool(resolved.get("secretAccessKey"))
        if has_access != has_secret or (require_explicit_identity and not (has_access and has_secret)):
            raise ValueError("incomplete object-store credential")
        if resolved.get("sessionToken") and not (has_access and has_secret):
            raise ValueError("session token has no key pair")
        return resolved
    except Exception:  # noqa: BLE001 — resolver/provider text may contain sensitive material
        raise DestinationCredentialError() from None


def authorize_destination(
        workspace: str, destination_id: str | None, target_uri: str) -> tuple[dict, dict] | None:
    """Freeze one object-store write identity and resolve its current material in the trusted parent."""
    from hub import destinations, metadb
    from hub.plugins.adapters import is_object_uri

    if not is_object_uri(target_uri):
        return None
    try:
        selected_id = ""
        if destination_id:
            destination = destinations.get_destination(workspace, destination_id)
            if destination is None:
                raise DestinationCredentialError()
            selected_id = str(
                destination.get("credId") or destination.get("cred_id") or "").strip()
        if not selected_id:
            selected_id = str(
                metadb.get_setting("defaultObjectStoreCredId", "global") or "").strip()

        base = {
            "version": _BINDING_VERSION,
            "scope": "destination-write",
            "destination_id": destination_id,
        }
        if selected_id:
            binding = {**base, "mode": "cred", "credential_id": selected_id}
        else:
            legacy = _safe_references(
                metadb.get_setting("objectStore", "global", default={}) or {})
            binding = (
                {**base, "mode": "legacy", "reference_sha256": _digest(legacy)}
                if legacy else {**base, "mode": "ambient"}
            )
        references = _current_references(binding)
        material = _resolve_references(
            references, require_explicit_identity=binding["mode"] == "cred")
        return binding, material
    except DestinationCredentialError:
        raise
    except Exception:  # provider/metadata failures cross the same non-secret public boundary
        raise DestinationCredentialError() from None


def reauthorize_binding(binding: object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the current material for the exact previously frozen identity.

    Updating fields on the same Cred ID is rotation and is accepted. Deleting it, changing kind, changing
    a legacy reference set, or selecting a different destination Cred cannot silently replace it.
    """
    try:
        validated = _validate_binding(binding)
        references = _current_references(validated)
        return references, _resolve_references(
            references, require_explicit_identity=validated["mode"] == "cred")
    except DestinationCredentialError:
        raise
    except Exception:  # provider/metadata failures cross the same non-secret public boundary
        raise DestinationCredentialError() from None


def validate_bindings(bindings: object) -> dict[str, dict]:
    if not isinstance(bindings, dict) or any(
            not isinstance(step_id, str) or not step_id for step_id in bindings):
        raise DestinationCredentialError()
    return {step_id: _validate_binding(binding) for step_id, binding in bindings.items()}


def reference_capability(bindings: object) -> str:
    """Build a safe SecretRef-only capability for a remote workload submission.

    Reauthorization and a local resolution check happen before submission, but only the references cross
    the boundary. The remote workload resolves them against its own mounted/provider environment.
    """
    validated = validate_bindings(bindings)
    entries = {}
    for step_id, binding in validated.items():
        references, _material = reauthorize_binding(binding)
        entries[step_id] = {"binding": binding, "references": references}
    payload = _canonical({"version": _CAPABILITY_VERSION, "entries": entries})
    if len(payload) > _CAPABILITY_MAX_BYTES:
        raise DestinationCredentialError()
    return payload.decode()


def resolve_reference_capability(raw: object, bindings: object) -> dict[str, dict[str, Any]]:
    """Validate and resolve a remote SecretRef capability without any ambient/default fallback."""
    validated = validate_bindings(bindings)
    if not validated:
        if raw not in (None, ""):
            raise DestinationCredentialError()
        return {}
    if not isinstance(raw, str) or not raw or len(raw.encode()) > _CAPABILITY_MAX_BYTES:
        raise DestinationCredentialError()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise DestinationCredentialError() from None
    if (not isinstance(payload, dict) or set(payload) != {"version", "entries"}
            or payload.get("version") != _CAPABILITY_VERSION
            or not isinstance(payload.get("entries"), dict)
            or set(payload["entries"]) != set(validated)):
        raise DestinationCredentialError()
    material = {}
    for step_id, binding in validated.items():
        entry = payload["entries"].get(step_id)
        if (not isinstance(entry, dict) or set(entry) != {"binding", "references"}
                or entry.get("binding") != binding):
            raise DestinationCredentialError()
        material[step_id] = _resolve_references(
            entry.get("references"), require_explicit_identity=binding["mode"] == "cred")
    return material


def create_fd_capability(
        run_id: str, bindings: object,
        material: Mapping[str, Mapping[str, Any]]) -> tuple[int | None, dict | None]:
    """Create an anonymous, one-use parent-to-child material channel.

    The returned read fd must be included in ``pass_fds`` and closed by the parent after ``Popen``.
    """
    validated = validate_bindings(bindings)
    if not validated:
        if material:
            raise DestinationCredentialError()
        return None, None
    if set(material) != set(validated):
        raise DestinationCredentialError()
    try:
        payload = _canonical({
            "version": _CAPABILITY_VERSION,
            "run_id": run_id,
            "binding_sha256": _digest(validated),
            "material": {
                step_id: _safe_references(dict(material[step_id]))
                for step_id in validated
            },
        })
    except Exception:
        raise DestinationCredentialError() from None
    if len(payload) > _CAPABILITY_MAX_BYTES:
        raise DestinationCredentialError()
    read_fd, write_fd = os.pipe()
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(write_fd, payload[offset:])
            if written <= 0:
                raise DestinationCredentialError()
            offset += written
    except Exception:
        os.close(read_fd)
        raise DestinationCredentialError() from None
    finally:
        os.close(write_fd)
    return read_fd, {
        "version": _CAPABILITY_VERSION,
        "fd": read_fd,
        "bindingSha256": _digest(validated),
    }


def read_fd_capability(
        descriptor: object, run_id: str, bindings: object) -> dict[str, dict[str, Any]]:
    """Consume an inherited anonymous capability exactly once and return in-memory material."""
    validated = validate_bindings(bindings)
    if not validated:
        if descriptor is not None:
            raise DestinationCredentialError()
        return {}
    if (not isinstance(descriptor, dict)
            or set(descriptor) != {"version", "fd", "bindingSha256"}
            or descriptor.get("version") != _CAPABILITY_VERSION
            or not isinstance(descriptor.get("fd"), int) or descriptor["fd"] < 0
            or descriptor.get("bindingSha256") != _digest(validated)):
        raise DestinationCredentialError()
    fd = descriptor["fd"]
    chunks = []
    size = 0
    try:
        while True:
            chunk = os.read(fd, min(8192, _CAPABILITY_MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _CAPABILITY_MAX_BYTES:
                raise DestinationCredentialError()
    except DestinationCredentialError:
        raise
    except OSError:
        raise DestinationCredentialError() from None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    try:
        payload = json.loads(b"".join(chunks))
    except (TypeError, ValueError):
        raise DestinationCredentialError() from None
    if (not isinstance(payload, dict)
            or set(payload) != {"version", "run_id", "binding_sha256", "material"}
            or payload.get("version") != _CAPABILITY_VERSION
            or payload.get("run_id") != run_id
            or payload.get("binding_sha256") != _digest(validated)
            or not isinstance(payload.get("material"), dict)
            or set(payload["material"]) != set(validated)):
        raise DestinationCredentialError()
    return {
        step_id: _safe_references(payload["material"][step_id])
        for step_id in validated
    }
