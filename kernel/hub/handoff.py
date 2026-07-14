"""Commit contract for immutable region handoffs.

A distributed writer owns one unique ``.attempt-*`` prefix. Data shards are written first and the
manifest is written last. The controller publishes an attempt URI only after validating that manifest;
failed attempts remain unreferenced and can be expired without touching a committed sibling.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import uuid
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from hub.plugins.adapters import is_object_uri, object_fs, path_of

ATTEMPT_MARKER = ".attempt-"
MANIFEST_NAME = "_DP_SUCCESS.json"
MANIFEST_FORMAT = "data-playground-ray-handoff-v2"
_MAX_SHARDS = 200_000
_DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_DELETE_GRACE_SECONDS = 24 * 60 * 60


def is_attempt_uri(uri: str) -> bool:
    """Whether ``uri`` names an immutable region-attempt prefix (not an arbitrary parent path)."""
    return ATTEMPT_MARKER in uri.rstrip("/").rsplit("/", 1)[-1]


def has_attempt_path_component(uri: str) -> bool:
    """Whether any object-key component is inside the reserved managed-attempt namespace."""
    raw = str(uri)
    if not is_object_uri(raw):
        return False
    try:
        return any(ATTEMPT_MARKER in part for part in urlsplit(raw).path.split("/") if part)
    except ValueError:
        return False


def _object_manifest_path(path: str) -> str:
    """Commit records use a separate prefix so storage lifecycle can expire them before data."""
    parent, name = path.rstrip("/").rsplit("/", 1)
    return f"{parent}/_dp_commits/{name}/{MANIFEST_NAME}"


def _object_commit_dir(path: str) -> str:
    return _object_manifest_path(path).rsplit("/", 1)[0]


def allocate_attempt(*, logical_uri: str, kind: str, run_id: str, allocation_key: str,
                     uri_factory, write_lease_seconds: float = 3600,
                     catalog_key_base: str | None = None,
                     require_live_preallocation: bool = False) -> dict:
    """Control-plane allocation for a namespaced, generation-fenced physical object URI."""
    if not is_object_uri(logical_uri):
        return {"uri": uri_factory("local", 1, uuid.uuid4().hex), "generation": 1}
    from hub import metadb
    logical_uri = metadb.validate_managed_object_uri(logical_uri)
    namespace = metadb.object_storage_namespace()
    # Namespace ownership is installation-scoped and must precede local attempt ownership. The optional
    # run gate remains inside the allocation transaction; a clone conflict therefore cannot leave a DB
    # attempt whose external prefix belongs to another installation.
    ensure_storage_namespace_claim(logical_uri, namespace)
    return metadb.allocate_object_attempt(
        logical_uri=logical_uri, kind=kind, run_id=run_id, allocation_key=allocation_key,
        uri_factory=uri_factory, write_lease_seconds=write_lease_seconds,
        expected_namespace=namespace, catalog_key_base=catalog_key_base,
        require_live_preallocation=require_live_preallocation,
    )


def lookup_attempt(*, logical_uri: str, kind: str, run_id: str,
                   allocation_key: str) -> dict | None:
    """Read a prior allocation generation without reopening writer authority."""
    if not is_object_uri(logical_uri):
        return None
    from hub import metadb
    handle = metadb.lookup_object_attempt(
        logical_uri=logical_uri, kind=kind, run_id=run_id, allocation_key=allocation_key)
    if handle is not None:
        ensure_storage_namespace_claim(handle["uri"], handle["namespace"])
    return handle


def physical_attempt_uri(logical_uri: str, namespace: str, generation: int,
                         attempt_id: str) -> str:
    """Provider-neutral bounded physical prefix containing namespace, generation, and random ID."""
    parsed = urlsplit(str(logical_uri))
    if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.netloc or not parsed.path.strip("/")):
        raise ValueError("managed logical object URI contains an unsupported authority or suffix")
    path = parsed.path.rstrip("/")
    low = path.lower()
    extension = next((ext for ext in (".parquet", ".pq") if low.endswith(ext)), "")
    base = path[:-len(extension)] if extension else path
    parent, separator, name = base.rpartition("/")
    safe_namespace = re.sub(r"[^A-Za-z0-9_-]+", "_", namespace).strip("_") or "installation"
    suffix = f".attempt-{safe_namespace}-g{int(generation)}-{attempt_id}"
    budget = max(1, 240 - len(suffix.encode()))
    encoded = (name if separator else base).encode()
    stem = encoded[:budget].decode(errors="ignore").rstrip(".") or "output"
    component = stem + suffix
    physical_path = f"{parent}/{component}" if separator else component
    if len(physical_path.lstrip("/").encode()) > 896:
        raise RuntimeError("logical object key leaves insufficient room for a managed attempt namespace")
    return urlunsplit((parsed.scheme, parsed.netloc, physical_path, parsed.query, parsed.fragment))


class ManagedObjectProvider(Protocol):
    """Provider-neutral proof and exact-member lifecycle SPI."""

    complete_inventory: bool
    conditional_namespace_claims: bool

    def inventory(self, uri: str) -> list[dict]: ...
    def delete_exact(self, uri: str, member: dict) -> None: ...
    def read_namespace_claim(self, uri: str, namespace: str) -> dict | None: ...
    def write_namespace_claim(self, uri: str, namespace: str, body: bytes,
                              expected_etag: str | None) -> str: ...


class ManagedProviderCapabilityError(RuntimeError):
    pass


class NamespaceClaimConflict(RuntimeError):
    pass


def _member_id(member_type: str, key: str, identity: str) -> str:
    raw = json.dumps([member_type, key, identity], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class PyArrowManagedObjectProvider:
    """Read helper only; it cannot prove hidden versions, delete markers, or multipart uploads."""

    complete_inventory = False
    conditional_namespace_claims = False

    def inventory(self, uri: str) -> list[dict]:
        raise ManagedProviderCapabilityError(
            "managed object provider cannot prove all versions, delete markers, and multipart uploads")

    def delete_exact(self, uri: str, member: dict) -> None:
        raise ManagedProviderCapabilityError("managed object provider cannot delete exact members")

    def read_namespace_claim(self, uri: str, namespace: str) -> dict | None:
        raise ManagedProviderCapabilityError("managed object provider lacks conditional namespace claims")

    def write_namespace_claim(self, uri: str, namespace: str, body: bytes,
                              expected_etag: str | None) -> str:
        raise ManagedProviderCapabilityError("managed object provider lacks conditional namespace claims")


class Boto3ManagedObjectProvider:
    """S3 provider enumerating every version, marker, and multipart upload identity."""

    complete_inventory = True
    conditional_namespace_claims = True

    def __init__(self, uri: str):
        import boto3
        from hub import metadb
        from hub.secrets import resolve_object_store
        cfg = resolve_object_store(metadb.get_setting("objectStore", "global", default={}) or {})
        kwargs = {
            "region_name": cfg.get("region") or os.environ.get("AWS_REGION") or "us-east-1",
        }
        endpoint = cfg.get("endpoint") or os.environ.get("AWS_ENDPOINT_URL")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        access = cfg.get("accessKeyId") or os.environ.get("AWS_ACCESS_KEY_ID")
        secret = cfg.get("secretAccessKey") or os.environ.get("AWS_SECRET_ACCESS_KEY")
        token = cfg.get("sessionToken") or os.environ.get("AWS_SESSION_TOKEN")
        if access:
            kwargs["aws_access_key_id"] = access
        if secret:
            kwargs["aws_secret_access_key"] = secret
        if token:
            kwargs["aws_session_token"] = token
        self.client = boto3.client("s3", **kwargs)

    @staticmethod
    def _location(uri: str) -> tuple[str, str, str]:
        parsed = urlsplit(uri)
        bucket, key = parsed.netloc, parsed.path.lstrip("/").rstrip("/")
        if not bucket or not key:
            raise RuntimeError("managed S3 attempt requires bucket and key")
        return bucket, key, f"{bucket}/{key}"

    @staticmethod
    def _member(member_type: str, bucket: str, item: dict, *, is_commit: bool) -> dict:
        key = f"{bucket}/{item['Key']}"
        version = item.get("VersionId")
        upload = item.get("UploadId")
        identity = str(version if version not in (None, "null") else upload or "null")
        return {
            "member_id": _member_id(member_type, key, identity),
            "key": key,
            "member_type": member_type,
            "size": int(item.get("Size") or 0),
            "etag": str(item.get("ETag") or "").strip('"') or None,
            "version_id": (None if version is None or (
                version == "null" and member_type == "unversioned_object") else str(version)),
            "upload_id": str(upload) if upload else None,
            "is_latest": bool(item.get("IsLatest")),
            "is_commit": is_commit,
        }

    def _all_versions(self, bucket: str, prefix: str, *, exact: str | None = None,
                      is_commit: bool = False, versioned: bool = False) -> list[dict]:
        paginator = self.client.get_paginator("list_object_versions")
        members: list[dict] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Versions", []):
                if exact is not None and item["Key"] != exact:
                    continue
                kind = ("object_version" if versioned or item.get("VersionId") not in (None, "null")
                        else "unversioned_object")
                members.append(self._member(kind, bucket, item, is_commit=is_commit))
            for item in page.get("DeleteMarkers", []):
                if exact is not None and item["Key"] != exact:
                    continue
                members.append(self._member(
                    "delete_marker", bucket, item, is_commit=is_commit))
        return members

    def _all_uploads(self, bucket: str, prefix: str, *, exact: str | None = None,
                     is_commit: bool = False) -> list[dict]:
        members: list[dict] = []
        paginator = self.client.get_paginator("list_multipart_uploads")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Uploads", []):
                if exact is not None and item["Key"] != exact:
                    continue
                members.append(self._member(
                    "multipart_upload", bucket, item, is_commit=is_commit))
        return members

    def inventory(self, uri: str) -> list[dict]:
        bucket, key, provider_root = self._location(uri)
        versioned = self.client.get_bucket_versioning(Bucket=bucket).get("Status") \
            in ("Enabled", "Suspended")
        members = self._all_versions(bucket, key + "/", versioned=versioned)
        members.extend(self._all_uploads(bucket, key + "/"))
        commit_provider_key = _object_manifest_path(provider_root)
        commit_key = commit_provider_key[len(bucket) + 1:]
        members.extend(self._all_versions(
            bucket, commit_key, exact=commit_key, is_commit=True, versioned=versioned))
        members.extend(self._all_uploads(
            bucket, commit_key, exact=commit_key, is_commit=True))
        return sorted(members, key=lambda item: item["member_id"])

    def delete_exact(self, uri: str, member: dict) -> None:
        bucket, _key, provider_root = self._location(uri)
        key = str(member["key"])
        if not (key.startswith(provider_root + "/") or key == _object_manifest_path(provider_root)):
            raise RuntimeError("exact S3 delete key escaped the object-attempt root")
        object_key = key[len(bucket) + 1:]
        if member.get("member_type") == "multipart_upload":
            self.client.abort_multipart_upload(
                Bucket=bucket, Key=object_key, UploadId=member["upload_id"])
            return
        kwargs = {"Bucket": bucket, "Key": object_key}
        if member.get("member_type") in ("object_version", "delete_marker"):
            kwargs["VersionId"] = member["version_id"]
        self.client.delete_object(**kwargs)

    @staticmethod
    def _claim_key(namespace: str) -> str:
        return f"_dp_control/namespaces/{quote(str(namespace), safe='')}.json"

    def read_namespace_claim(self, uri: str, namespace: str) -> dict | None:
        bucket, _key, _root = self._location(uri)
        key = self._claim_key(namespace)
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 — normalize provider-specific not-found codes
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(code) in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        try:
            doc = json.loads(response["Body"].read())
        except (TypeError, ValueError) as exc:
            raise NamespaceClaimConflict("storage namespace claim marker is invalid") from exc
        return {"doc": doc, "etag": str(response.get("ETag") or "")}

    def write_namespace_claim(self, uri: str, namespace: str, body: bytes,
                              expected_etag: str | None) -> str:
        bucket, _key, _root = self._location(uri)
        kwargs = {
            "Bucket": bucket, "Key": self._claim_key(namespace), "Body": body,
            "ContentType": "application/json",
        }
        kwargs["IfMatch" if expected_etag else "IfNoneMatch"] = expected_etag or "*"
        try:
            response = self.client.put_object(**kwargs)
        except Exception as exc:  # noqa: BLE001 — one stable conflict type drives fail-closed CAS
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(code) in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
                raise NamespaceClaimConflict("storage namespace ownership changed concurrently") from exc
            raise
        return str(response.get("ETag") or "")


_provider_factory = None
_runtime_provider_factory = None
_namespace_claim_lock = threading.Lock()
_namespace_activations: set[tuple[int, str, str]] = set()


class ManagedInventoryContainmentError(RuntimeError):
    pass


def set_managed_object_provider(factory) -> None:
    """Install a provider factory for tests; deployments may use the dotted environment seam."""
    global _provider_factory
    _provider_factory = factory
    with _namespace_claim_lock:
        _namespace_activations.clear()


def set_runtime_managed_object_provider(factory) -> None:
    """Plugin registry seam for a production provider; tests use set_managed_object_provider."""
    global _runtime_provider_factory
    _runtime_provider_factory = factory
    with _namespace_claim_lock:
        _namespace_activations.clear()


def _managed_provider(uri: str) -> ManagedObjectProvider:
    if _provider_factory is not None:
        return _provider_factory(uri) if callable(_provider_factory) else _provider_factory
    if _runtime_provider_factory is not None:
        return (_runtime_provider_factory(uri) if callable(_runtime_provider_factory)
                else _runtime_provider_factory)
    dotted = os.environ.get("DP_MANAGED_OBJECT_PROVIDER", "").strip()
    if dotted:
        from hub.settings import import_dotted
        provider = import_dotted(dotted)
        return provider(uri) if isinstance(provider, type) else provider
    if urlsplit(uri).scheme.lower() == "s3":
        return Boto3ManagedObjectProvider(uri)
    return PyArrowManagedObjectProvider()


def _provider_capabilities(provider: ManagedObjectProvider) -> None:
    if not getattr(provider, "complete_inventory", False):
        raise ManagedProviderCapabilityError(
            "managed object provider cannot prove complete version and multipart inventory")
    if not getattr(provider, "conditional_namespace_claims", False):
        raise ManagedProviderCapabilityError(
            "managed object provider cannot conditionally claim a storage namespace")


def ensure_storage_namespace_claim(uri: str, namespace: str) -> None:
    """Activate once per process, then verify the external CAS marker before every managed I/O."""
    from hub import metadb

    provider = _managed_provider(uri)
    _provider_capabilities(provider)
    bucket = urlsplit(uri).netloc
    scope = hashlib.sha256(
        f"{urlsplit(uri).scheme.lower()}://{bucket}".encode()).hexdigest()
    key = (id(metadb.engine()), scope, str(namespace))
    with _namespace_claim_lock:
        if key not in _namespace_activations:
            activation_id = uuid.uuid4().hex

            def write(owner_token, storage_namespace, new_token, prior_token, prior_etag):
                current = provider.read_namespace_claim(uri, storage_namespace)
                if prior_etag:
                    if (current is None or current.get("etag") != prior_etag
                            or (current.get("doc") or {}).get("ownerToken") != owner_token
                            or (current.get("doc") or {}).get("claimToken") != prior_token):
                        raise NamespaceClaimConflict(
                            "storage namespace claim no longer matches the metadata database")
                body = json.dumps({
                    "format": "data-playground-storage-claim-v1",
                    "namespace": storage_namespace,
                    "ownerToken": owner_token,
                    "claimToken": new_token,
                }, sort_keys=True, separators=(",", ":")).encode()
                try:
                    return provider.write_namespace_claim(
                        uri, storage_namespace, body, expected_etag=prior_etag)
                except Exception as write_error:
                    # The conditional write may have committed while its response was lost. Converge
                    # only when a read proves this exact call's owner/token won; any other marker or
                    # read failure preserves the original fail-closed outcome.
                    try:
                        committed = provider.read_namespace_claim(uri, storage_namespace)
                    except Exception:
                        raise write_error
                    committed_doc = (committed or {}).get("doc") or {}
                    committed_etag = str((committed or {}).get("etag") or "")
                    if (committed_etag
                            and committed_doc.get("ownerToken") == owner_token
                            and committed_doc.get("claimToken") == new_token
                            and committed_doc.get("namespace") == storage_namespace):
                        return committed_etag
                    raise

            metadb.activate_object_storage_claim(namespace, scope, activation_id, write)
            _namespace_activations.add(key)
    claim = metadb.object_storage_claim(namespace, scope)
    current = provider.read_namespace_claim(uri, namespace)
    if (claim is None or current is None or current.get("etag") != claim["marker_etag"]
            or (current.get("doc") or {}).get("ownerToken") != claim["owner_token"]
            or (current.get("doc") or {}).get("claimToken") != claim["claim_token"]
            or (current.get("doc") or {}).get("namespace") != namespace):
        raise NamespaceClaimConflict("storage namespace ownership verification failed")


def _contained_inventory(uri: str, inventory: list[dict]) -> list[dict]:
    """Reject a provider response that escapes this generation's data/commit roots."""
    parsed = urlsplit(uri)
    path = f"{parsed.netloc}/{parsed.path.lstrip('/')}"
    data_prefix = path.rstrip("/") + "/"
    commit_key = _object_manifest_path(path)
    member_ids: set[str] = set()
    for member in inventory:
        key = str(member.get("key") or "")
        member_id = str(member.get("member_id") or "")
        member_type = str(member.get("member_type") or "")
        if (not member_id or member_id in member_ids or member_type not in (
                "object_version", "delete_marker", "multipart_upload", "unversioned_object")):
            raise ManagedInventoryContainmentError(
                "provider inventory contains an invalid or duplicate member identity")
        member_ids.add(member_id)
        if not key or not (key.startswith(data_prefix) or key == commit_key):
            raise ManagedInventoryContainmentError(
                "provider inventory escaped the object-attempt root")
    return inventory


def _exact_manifest_inventory(uri: str, manifest: dict,
                              provider: ManagedObjectProvider) -> list[dict]:
    """Capture and validate all data members plus the separately-keyed commit object."""
    _fs, path = object_fs(uri)
    base = path.rstrip("/")
    expected = {f"{base}/{item['path']}": int(item["size"]) for item in manifest["shards"]}
    expected[_object_manifest_path(path)] = None
    _provider_capabilities(provider)
    actual = _contained_inventory(uri, provider.inventory(uri))
    visible = [item for item in actual if item["member_type"] in (
        "object_version", "unversioned_object") and item.get("is_latest")]
    by_key = {str(item["key"]): item for item in visible}
    # Ray's S3 filesystem creates an empty object for the exact output directory before its workers
    # write shards. It is owned by this immutable attempt and remains part of the persisted exact
    # inventory (and therefore exact GC); allow only that precise zero-byte root marker. Any nested,
    # non-empty, or otherwise unexpected object still fails the set-equality barrier below.
    root_marker = base.rstrip("/") + "/"
    if (marker := by_key.get(root_marker)) is not None and marker.get("size") == 0:
        expected[root_marker] = 0
    if set(by_key) != set(expected):
        raise RuntimeError("manifest inventory does not match the latest visible object versions")
    for key, size in expected.items():
        if size is not None and int(by_key[key].get("size") or 0) != size:
            raise RuntimeError("manifest inventory size changed")
    return actual


def prepare_attempt_commit(uri: str) -> None:
    """Persist exact terminal inventory for a managed attempt before pointer publication."""
    if not is_object_uri(uri) or not is_attempt_uri(uri):
        return
    from hub import metadb
    namespace = metadb.object_attempt_namespace(uri)
    try:
        ensure_storage_namespace_claim(uri, namespace)
    except Exception as exc:
        metadb.quarantine_object_attempt(uri, "storage namespace ownership could not be proven")
        raise RuntimeError("managed storage namespace ownership could not be proven") from exc
    manifest = read_manifest(uri)
    if manifest is None or not validate_shards(uri, manifest):
        metadb.quarantine_object_attempt(uri, "commit manifest is absent, invalid, or inconsistent")
        raise RuntimeError("managed object attempt has no valid exact commit inventory")
    provider = _managed_provider(uri)
    try:
        inventory = _exact_manifest_inventory(uri, manifest, provider)
    except Exception as exc:
        metadb.quarantine_object_attempt(uri, "exact provider inventory could not be proven")
        raise RuntimeError("managed object attempt inventory could not be proven") from exc
    uploads = [item for item in inventory if item["member_type"] == "multipart_upload"]
    if uploads:
        cleanup_inventory = inventory
        try:
            for upload in uploads:
                provider.delete_exact(uri, upload)
            refreshed = _exact_manifest_inventory(uri, manifest, provider)
            cleanup_inventory = refreshed
            if any(item["member_type"] == "multipart_upload" for item in refreshed):
                raise RuntimeError("incomplete multipart upload remained after exact abort")
            inventory = refreshed
        except Exception as cleanup_error:
            # Re-inventory first so a committed abort with a lost response converges to success. If
            # uploads remain (or the refresh cannot be proven), preserve the complete exact superset;
            # GC treats already-missing identities as an idempotent restart.
            try:
                try:
                    cleanup_inventory = _exact_manifest_inventory(uri, manifest, provider)
                except Exception:
                    pass
                if not any(item["member_type"] == "multipart_upload"
                           for item in cleanup_inventory):
                    inventory = cleanup_inventory
                else:
                    metadb.record_object_attempt_commit(uri, cleanup_inventory)
                    metadb.abandon_committed_object_attempt(uri)
            except Exception as persist_error:
                metadb.quarantine_object_attempt(
                    uri, "multipart cleanup inventory could not be persisted")
                raise RuntimeError(
                    "managed multipart cleanup inventory could not be persisted"
                ) from persist_error
            if any(item["member_type"] == "multipart_upload" for item in cleanup_inventory):
                raise RuntimeError(
                    "managed object attempt multipart cleanup failed; exact cleanup was scheduled"
                ) from cleanup_error
    try:
        metadb.record_object_attempt_commit(uri, inventory)
    except Exception as exc:
        metadb.quarantine_object_attempt(uri, "committed inventory conflicted with lifecycle state")
        raise RuntimeError("managed object attempt commit conflicted with lifecycle state") from exc


class ManagedReadLeaseGuard:
    """Renew a read lease and expose a fail-closed checkpoint to long-running readers."""

    def __init__(self, lease_id: str | None, ttl_seconds: float,
                 attestation: dict | None = None):
        self.lease_id = lease_id
        self.attestation = dict(attestation) if attestation is not None else None
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None
        if lease_id:
            try:
                self._thread = threading.Thread(
                    target=self._renew_loop, daemon=True, name="dp-object-read-lease")
                self._thread.start()
            except Exception:
                try:
                    self._release()
                except Exception:  # noqa: BLE001 — preserve the thread-start failure
                    logging.getLogger("hub").exception(
                        "managed artifact read lease rollback failed")
                self._thread = None
                raise

    def _renew_loop(self) -> None:
        from hub import metadb
        while not self._stop.wait(max(0.1, self.ttl_seconds / 3.0)):
            try:
                if not metadb.renew_object_attempt_lease(self.lease_id, self.ttl_seconds):
                    self._lost.set()
                    return
            except Exception:  # noqa: BLE001 — a reader cannot assume its deletion fence survived
                self._lost.set()
                return

    def check(self) -> None:
        if self._lost.is_set():
            raise FileNotFoundError("managed artifact read lease could not be renewed")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.lease_id:
            self._release()

    def _release(self) -> None:
        from hub import metadb
        metadb.release_object_attempt_lease(self.lease_id)


class ManagedResultCachePinGuard(ManagedReadLeaseGuard):
    """Renew one cache-reader lease and release its paired temporary ownership ref."""

    def _release(self) -> None:
        from hub import metadb
        metadb.release_result_cache_pin(self.lease_id)


@contextlib.contextmanager
def managed_read_lease(uri: str, *, owner: str | None = None, ttl_seconds: float = 300,
                       allow_committed: bool = False):
    """Pin a managed generation for one actual core read; raw URI readers must pin explicitly."""
    from hub import metadb
    if has_attempt_path_component(uri) and not is_attempt_uri(uri):
        raise FileNotFoundError("managed source must reference the exact attempt root")
    lease, attestation = metadb.acquire_attested_object_read(
        uri, owner or f"reader:{uuid.uuid4().hex}", ttl_seconds=ttl_seconds,
        allow_committed=allow_committed)
    guard = ManagedReadLeaseGuard(lease, ttl_seconds, attestation)
    try:
        yield guard
        guard.check()
    finally:
        guard.close()


def _seconds_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return max(0.0, value) if math.isfinite(value) else default
    except ValueError:
        return default


def reap_attempts(*, retention_seconds: float | None = None,
                  delete_grace_seconds: float | None = None, limit: int = 100) -> dict[str, list[str]]:
    """Run one bounded, DB-indexed GC batch; never list an object-store parent prefix."""
    from hub import metadb
    deadline = max(1.0, _seconds_env("DP_RUN_DEADLINE_S", 3600))
    retention = (max(deadline, _seconds_env(
        "DP_ATTEMPT_RETENTION_SECONDS", _DEFAULT_RETENTION_SECONDS)) if retention_seconds is None
                 else max(0.0, float(retention_seconds)))
    grace = (max(deadline, _seconds_env(
        "DP_ATTEMPT_DELETE_GRACE_SECONDS", _DEFAULT_DELETE_GRACE_SECONDS))
             if delete_grace_seconds is None
             else max(0.0, float(delete_grace_seconds)))
    batch = metadb.object_attempt_gc_batch(retention, grace, limit=limit)
    result = {"observed": [], "deleted": [], "quarantined": []}
    for item in batch:
        uri, action = item["uri"], item["action"]
        try:
            provider = _managed_provider(uri)
            _provider_capabilities(provider)
            ensure_storage_namespace_claim(uri, item["storage_namespace"])
            if action == "observe":
                inventory = _contained_inventory(uri, provider.inventory(uri))
                state = metadb.observe_object_attempt_inventory(
                    uri, inventory, quiet_seconds=grace)
                result["quarantined" if state == "quarantined" else "observed"].append(uri)
            elif action == "verify_empty":
                if not metadb.renew_object_attempt_delete(item):
                    continue
                if _contained_inventory(uri, provider.inventory(uri)):
                    metadb.quarantine_object_attempt(
                        uri, "provider inventory became non-empty during final deletion barrier")
                    result["quarantined"].append(uri)
                    continue
                if metadb.complete_object_attempt_delete_verification(item):
                    result["deleted"].append(uri)
            elif action == "delete":
                if not metadb.renew_object_attempt_delete(item):
                    continue
                persisted = metadb.object_attempt_inventory(uri)
                pending = {member["member_id"]: member for member in
                           metadb.object_attempt_inventory(uri, pending_only=True)}
                actual_inventory = _contained_inventory(uri, provider.inventory(uri))
                if not metadb.renew_object_attempt_delete(item):
                    continue
                actual = {member["member_id"]: member for member in actual_inventory}
                all_members = {member["member_id"] for member in persisted}
                unexpected = set(actual) - all_members
                resurrected = (set(actual) & all_members) - set(pending)
                if unexpected or resurrected:
                    metadb.quarantine_object_attempt(
                        uri, "provider inventory changed after exact deletion began")
                    result["quarantined"].append(uri)
                    continue
                uncertain = None
                identity_fields = (
                    "key", "member_type", "etag", "version_id", "upload_id", "size", "is_commit")
                for member_id, member in pending.items():
                    current = actual.get(member_id)
                    if current is not None:
                        identity = tuple(member.get(field) for field in identity_fields)
                        observed = tuple(current.get(field) for field in identity_fields)
                        if identity != observed:
                            uncertain = "provider member identity changed before deletion"
                            break
                if uncertain:
                    metadb.quarantine_object_attempt(uri, uncertain)
                    result["quarantined"].append(uri)
                    continue
                for member_id, member in sorted(
                        pending.items(), key=lambda pair: not pair[1]["is_commit"]):
                    if not metadb.renew_object_attempt_delete(item):
                        raise RuntimeError("delete lease expired before exact inventory completed")
                    current = actual.get(member_id)
                    if current is not None:
                        identity = tuple(member.get(field) for field in identity_fields)
                        observed = tuple(current.get(field) for field in identity_fields)
                        if identity != observed:
                            metadb.quarantine_object_attempt(
                                uri, "provider member identity changed before deletion")
                            result["quarantined"].append(uri)
                            break
                        provider.delete_exact(uri, member)
                        if not metadb.validate_object_attempt_delete(item):
                            raise RuntimeError("delete epoch changed during exact provider deletion")
                    # A prior delete may have succeeded before its DB acknowledgement. Missing is the
                    # idempotent restart case; acknowledge only this persisted exact member.
                    metadb.acknowledge_object_attempt_member(item, member_id)
                else:
                    if not metadb.renew_object_attempt_delete(item):
                        raise RuntimeError("delete lease expired before final provider inventory")
                    if _contained_inventory(uri, provider.inventory(uri)):
                        metadb.quarantine_object_attempt(
                            uri, "provider inventory was not empty after exact deletion")
                        result["quarantined"].append(uri)
                        continue
                    metadb.begin_object_attempt_delete_verification(item, grace)
                    result["observed"].append(uri)
        except (ManagedInventoryContainmentError, ManagedProviderCapabilityError,
                NamespaceClaimConflict) as exc:
            metadb.quarantine_object_attempt(uri, str(exc))
            result["quarantined"].append(uri)
        except Exception:  # noqa: BLE001 — one provider failure must not block the rest of the batch
            if action in ("delete", "verify_empty"):
                metadb.fail_object_attempt_delete(item, "exact provider deletion failed")
            logging.getLogger("hub").warning(
                "object attempt GC failed (continuing)", exc_info=True)
    return result


def _list_shards(uri: str) -> list[dict]:
    """Current Parquet objects under one attempt prefix."""
    shards: list[dict] = []
    if is_object_uri(uri):
        import pyarrow.fs as pafs
        fs, path = object_fs(uri)
        base = path.rstrip("/")
        prefix = base + "/"
        infos = fs.get_file_info(pafs.FileSelector(base, recursive=True, allow_not_found=True))
        for info in infos:
            if info.type == pafs.FileType.File and info.path.lower().endswith((".parquet", ".pq")):
                shards.append({"path": info.path[len(prefix):], "size": int(info.size)})
    else:
        base = path_of(uri)
        for root, _, files in os.walk(base):
            for name in files:
                if name.lower().endswith((".parquet", ".pq")):
                    path = os.path.join(root, name)
                    relative = os.path.relpath(path, base).replace(os.sep, "/")
                    shards.append({"path": relative, "size": os.path.getsize(path)})
    shards.sort(key=lambda item: item["path"])
    return shards


def _shard_inventory(uri: str) -> list[dict]:
    """Exact Parquet objects that make up an attempt, captured before the commit record is written."""
    shards = _list_shards(uri)
    if not shards:
        raise RuntimeError("region handoff produced no Parquet shard")
    if len(shards) > _MAX_SHARDS:
        raise RuntimeError(
            f"region handoff produced {len(shards):,} shards (limit {_MAX_SHARDS:,}); compact the region")
    return shards


def write_manifest(uri: str, *, run_id: str, rows: int, schema: object) -> None:
    """Write the commit marker last. A partial marker is invalid and is never published."""
    body = json.dumps({
        "format": MANIFEST_FORMAT,
        "runId": run_id,
        "rows": int(rows),
        "schema": str(getattr(schema, "base_schema", schema)),
        "shards": _shard_inventory(uri),
    }, sort_keys=True).encode()
    if is_object_uri(uri):
        fs, path = object_fs(uri)
        with fs.open_output_stream(_object_manifest_path(path)) as stream:
            stream.write(body)
        return
    directory = path_of(uri)
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(directory, MANIFEST_NAME)
    staged = final + ".tmp"
    with open(staged, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, final)


def read_manifest(uri: str) -> dict | None:
    """Return a validated commit manifest, or ``None`` on absence, corruption, or auth failure."""
    try:
        if is_object_uri(uri):
            fs, path = object_fs(uri)
            with fs.open_input_file(_object_manifest_path(path)) as stream:
                raw = stream.read()
        else:
            with open(os.path.join(path_of(uri), MANIFEST_NAME), "rb") as stream:
                raw = stream.read()
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001 — uncertainty is an uncommitted handoff, never a cache hit
        return None
    rows = doc.get("rows") if isinstance(doc, dict) else None
    shards = doc.get("shards") if isinstance(doc, dict) else None
    valid_shards = isinstance(shards, list) and 0 < len(shards) <= _MAX_SHARDS and all(
        isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]
        and not item["path"].startswith("/") and ".." not in item["path"].split("/")
        and isinstance(item.get("size"), int) and not isinstance(item["size"], bool) and item["size"] >= 0
        for item in (shards or []))
    if (not isinstance(doc, dict) or doc.get("format") != MANIFEST_FORMAT
            or not isinstance(doc.get("runId"), str) or not doc["runId"]
            or not isinstance(rows, int) or isinstance(rows, bool) or rows < 0 or not valid_shards):
        return None
    return doc


def attempt_has_commit_record(uri: str) -> bool:
    """Whether any commit object exists, valid or corrupt; uncertainty is treated as occupied."""
    try:
        if is_object_uri(uri):
            import pyarrow.fs as pafs

            fs, path = object_fs(uri)
            return fs.get_file_info(_object_manifest_path(path)).type == pafs.FileType.File
        return os.path.lexists(os.path.join(path_of(uri), MANIFEST_NAME))
    except Exception:  # noqa: BLE001 — never overwrite a prefix whose commit state is unknown
        return True


def validate_shards(uri: str, manifest: dict) -> bool:
    """Fail closed unless the current Parquet path/size set exactly matches the committed inventory."""
    try:
        return _shard_inventory(uri) == manifest["shards"]
    except Exception:  # noqa: BLE001 — missing/auth/metadata uncertainty is never a cache hit
        return False


def attempt_has_shards(uri: str) -> bool:
    """Whether an unpublished prefix already contains data; uncertainty fails closed as occupied."""
    try:
        return bool(_list_shards(uri))
    except Exception:  # noqa: BLE001 — never overwrite a prefix whose state cannot be proven empty
        return True


def attempt_has_contents(uri: str) -> bool:
    """Whether an unpublished data prefix contains any object, not only a recognized Parquet shard."""
    try:
        if is_object_uri(uri):
            import pyarrow.fs as pafs

            fs, path = object_fs(uri)
            infos = fs.get_file_info(
                pafs.FileSelector(path.rstrip("/"), recursive=True, allow_not_found=True)
            )
            return any(info.type == pafs.FileType.File for info in infos)
        path = path_of(uri)
        if os.path.isfile(path) or os.path.islink(path):
            return True
        return any(files for _root, _dirs, files in os.walk(path))
    except Exception:  # noqa: BLE001 — never overwrite a prefix whose occupancy is uncertain
        return True


def discard_attempt(uri: str) -> None:
    """Record terminal writer proof; object deletion remains quiet-period/reaper owned."""
    if not is_attempt_uri(uri):
        return
    try:
        if is_object_uri(uri):
            from hub import metadb
            quiet = _seconds_env("DP_ATTEMPT_INVENTORY_QUIET_SECONDS", 60)
            if not metadb.mark_object_attempt_terminal(uri.rstrip("/"), quiet_seconds=quiet):
                return
            metadb.observe_object_attempt_inventory(
                uri.rstrip("/"), _contained_inventory(
                    uri, _managed_provider(uri).inventory(uri)), quiet_seconds=quiet)
        else:
            path = path_of(uri)
            if os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    except Exception:  # noqa: BLE001 — cleanup is best-effort; the terminal run status stays authoritative
        pass
