"""Where run outputs are persisted — pluggable so the default local directory can be swapped for
object storage (S3/GCS) with no change to the engine or write nodes: a write node/DuckDB just writes
to the uri that Storage hands back (a local path, or an ``s3://…`` uri — both are real, written via
the same DuckDB path the adapters use). Selected by DP_STORAGE_URL.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import re
import shutil
import stat
import threading
import uuid
from typing import Protocol

from hub.plugins.adapters import is_object_uri

try:  # Windows has no inherited flock proof; automatic GC is disabled there.
    import fcntl
except ImportError:  # pragma: no cover - exercised through a capability override
    fcntl = None

# temp siblings an interrupted local append/compaction/partition overwrite can leave next to a base dir
# (see adapters.py):
# `<base>.parttmp-<hex10>` (an in-flight append part), `<base>.old-<hex8>` (pre-compaction originals, briefly
# holding the data while the two-rename swap is in flight), `<base>.compact-<hex8>` (the compaction output),
# and `<base>.partition-{old,new}-<hex8>` (the same recoverable protocol for partition overwrite).
# The hex lengths are matched EXACTLY (what the adapter emits) so a real output can't accidentally collide
# with the suffix and be renamed/deleted at startup.
_TEMP_SUFFIX = re.compile(
    r"\.(?:(?P<parttmp>parttmp)-[0-9a-f]{10}|"
    r"(?P<kind>old|compact|partition-old|partition-new)-[0-9a-f]{8})$")
_RESULT_TEMP_SUFFIX = re.compile(
    r"(?P<artifact>__result_[A-Za-z0-9_-]+_[0-9a-f]{32}\.parquet)\.tmp-[0-9a-f]{8}$")
_MAINTENANCE_SCAN_BUDGET = 256

_EXTS = (".parquet", ".csv", ".tsv", ".json", ".arrow", ".feather", ".ipc")
# ephemeral full-pass run results (runner._materialize_result) share the outputs dir but are NOT
# user-published datasets — exclude them from list_outputs so a restart doesn't re-catalog them into
# the Tables view (P0-UX-01). Keyed by this basename prefix.
RESULT_PREFIX = "__result_"
RESULT_DIR = ".dp-results"
RESULT_LOCK_DIR = ".locks"
RESULT_NAMESPACE_FILE = ".namespace-id"
MAX_MANAGED_EXECUTION_SOURCES = 128


class ManagedSourceReadError(Exception):
    """A typed interactive-source policy/lifecycle failure that callers must not treat as unknown."""


class ManagedSourceUnavailable(FileNotFoundError, ManagedSourceReadError):
    """Public, provider-neutral failure for an interactive managed-source read."""

    def __init__(self):
        super().__init__("managed source is unavailable or expired")


class ManagedSourceLimitExceeded(RuntimeError, ManagedSourceReadError):
    """The complete source set exceeded the bounded acquisition contract."""


class ManagedSourceAccessDenied(PermissionError, ManagedSourceReadError):
    """A local source failed the shared-deployment path-confinement contract."""


class Storage(Protocol):
    def output_uri(self, name: str, ext: str) -> str: ...
    def list_outputs(self) -> list[str]: ...
    def recover_orphans(self) -> None: ...


class LocalResultReadGuard:
    """One exact local-result reader, protected by both the registry and a process lock."""

    def __init__(self, storage: "LocalStorage", uri: str, reader_id: str,
                 lock_fd: int | None, artifact_fd: int):
        self.storage = storage
        self.uri = uri
        self.reader_id = reader_id
        self._lock_fd = lock_fd
        self._artifact_fd = artifact_fd
        self._closed = False
        self._release_pending = False
        self._close_lock = threading.Lock()

    def __enter__(self) -> "LocalResultReadGuard":
        return self

    def check(self) -> None:
        if self._closed:
            raise RuntimeError("managed local-result read guard is closed")
        # The shared descriptor is the final deletion fence. Registry mutations are serialized and no
        # valid path removes this guard's unique ref before close(), so a DB transaction per plan step
        # would add O(nodes) metadata traffic without strengthening the proof.
        if self._lock_fd is not None:
            os.fstat(self._lock_fd)
        self.storage._check_result_read_identity(self.uri, self._artifact_fd)

    def fileno(self) -> int | None:
        return self._lock_fd

    def artifact_fileno(self) -> int:
        return self._artifact_fd

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            from hub import metadb

            # Mark before the transaction: if commit outcome is unknown, the storage-owned strong
            # reference and both exact FDs survive for bounded maintenance retry.
            self._release_pending = True
            self.storage._mark_result_reader_release(self)
            metadb.release_local_result_read(
                self.uri, self.storage.namespace_id, self.reader_id)
            lock_fd, artifact_fd = self._lock_fd, self._artifact_fd
            self._lock_fd = None
            self._artifact_fd = -1
            self._release_pending = False
            self._closed = True
            self.storage._forget_result_reader(self.reader_id)
            if artifact_fd >= 0:
                os.close(artifact_fd)
            if lock_fd is not None:
                os.close(lock_fd)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            try:
                self.check()
            except BaseException:
                # Integrity/read failure is primary. A release failure remains in the storage-owned
                # pending queue for maintenance, but must not replace the evidence that the read broke.
                try:
                    self.close()
                except Exception:
                    import logging
                    logging.getLogger("hub").warning(
                        "managed local-result cleanup failed after read integrity failure",
                        exc_info=True)
                raise
            else:
                try:
                    self.close()
                except Exception:
                    # The body and final identity check both succeeded. close() retained the exact
                    # reader in the pending queue before the uncertain DB release, so a cleanup outage
                    # must not turn a correct read into a false query failure.
                    import logging
                    logging.getLogger("hub").warning(
                        "managed local-result cleanup is pending after a successful read",
                        exc_info=True)
        else:
            try:
                self.close()
            except Exception:
                # Preserve the active query/execution exception. close() marked the exact guard pending
                # before touching metadata, so bounded maintenance retains both FDs and can retry.
                import logging
                logging.getLogger("hub").warning(
                    "managed local-result cleanup failed after execution error", exc_info=True)
        return False


def preflight_managed_execution_sources(
        storage, uris, *, include_object_attempts: bool = True) -> list[str]:
    """Deduplicate and cap lifecycle-managed sources before acquiring any one of them."""
    from hub.handoff import has_attempt_path_component

    classify_local = (getattr(storage, "requires_result_read", None)
                      or getattr(storage, "is_managed_result_uri", None))
    ordered: list[str] = []
    seen: set[str] = set()
    managed_keys: set[str] = set()
    for raw_uri in uris:
        from hub.paths import local_path

        raw = str(raw_uri)
        try:
            path = local_path(raw)
        except ValueError:
            path = raw
        normalized = ((path.rstrip(os.sep) or os.sep) if path is not None
                      else raw.rstrip("/"))
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
        local = bool(callable(classify_local) and classify_local(normalized))
        if local:
            managed_keys.add(f"local:{normalized}")
        elif include_object_attempts and has_attempt_path_component(normalized):
            managed_keys.add(f"object:{normalized}")
        if len(managed_keys) > MAX_MANAGED_EXECUTION_SOURCES:
            raise ManagedSourceLimitExceeded(
                f"an execution may use at most {MAX_MANAGED_EXECUTION_SOURCES} managed sources; "
                "split the graph or reduce its managed inputs")
    return ordered


def _interactive_source_targets(storage, uris) -> list[tuple[str, str]]:
    """Normalize and classify a complete interactive read set without acquiring anything."""
    from hub import metadb

    classify_local = (getattr(storage, "requires_result_read", None)
                      or getattr(storage, "is_managed_result_uri", None))
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_uri in uris:
        raw = str(raw_uri)
        from hub import paths

        try:
            paths.ensure_local_uri_allowed(raw)
        except PermissionError as exc:
            logging.getLogger("hub").warning("interactive source rejected by local-path policy")
            raise ManagedSourceAccessDenied(str(exc)) from None
        try:
            local = paths.local_path(raw)
        except ValueError:
            logging.getLogger("hub").warning(
                "interactive source rejected a non-canonical file URI")
            raise ManagedSourceUnavailable() from None
        normalized = ((local.rstrip(os.sep) or os.sep) if local is not None
                      else raw.rstrip("/"))

        try:
            local = bool(callable(classify_local) and classify_local(normalized))
        except Exception:
            logging.getLogger("hub").exception(
                "interactive source lifecycle classification failed")
            raise ManagedSourceUnavailable() from None

        if local:
            canonical = (normalized[len("file://"):]
                         if normalized.startswith("file://") else normalized)
            key = f"local:{canonical}"
            target = (canonical, "local")
        elif metadb.object_attempt_namespace_path(normalized):
            if not metadb.object_attempt_uri_shape(normalized):
                logging.getLogger("hub").warning(
                    "interactive source rejected a managed-attempt descendant")
                raise ManagedSourceUnavailable() from None
            key = f"object:{normalized}"
            target = (normalized, "object")
        else:
            key = f"unmanaged:{normalized}"
            target = (normalized, "unmanaged")
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)

    managed_count = sum(kind != "unmanaged" for _uri, kind in targets)
    if managed_count > MAX_MANAGED_EXECUTION_SOURCES:
        raise ManagedSourceLimitExceeded(
            f"an interactive read may use at most {MAX_MANAGED_EXECUTION_SOURCES} managed sources; "
            "split the graph or reduce its managed inputs")
    return targets


@contextlib.contextmanager
def source_read_scope(storage, uris, *, owner: str, ttl_seconds: float = 300):
    """Pin every managed source through one complete interactive read.

    Ordinary sources are a no-op. The complete set is classified, normalized, deduplicated, and
    capped before the first claim. Acquisition/check failures are logged with their internal cause and
    exposed as one stable provider-neutral error; an exception raised by the read body itself is kept.
    """
    targets = _interactive_source_targets(storage, uris)
    stack = contextlib.ExitStack()
    guards = []
    try:
        acquire_local = getattr(storage, "acquire_result_read", None)
        from hub.handoff import managed_read_lease

        for uri, kind in targets:
            if kind == "local":
                if not callable(acquire_local):
                    raise RuntimeError("managed local-source reader is unavailable")
                guards.append(stack.enter_context(acquire_local(uri, owner)))
            elif kind == "object":
                guards.append(stack.enter_context(managed_read_lease(
                    uri, owner=owner, ttl_seconds=ttl_seconds)))
    except BaseException as acquisition_error:
        try:
            stack.close()
        except BaseException:  # noqa: BLE001 - acquisition failure remains primary
            logging.getLogger("hub").exception(
                "interactive source lifecycle rollback failed")
        if not isinstance(acquisition_error, Exception):
            # Cancellation and process-control exceptions must retain their identity after every
            # already-acquired guard has been released.
            raise
        logging.getLogger("hub").exception(
            "interactive source lifecycle acquisition failed")
        raise ManagedSourceUnavailable() from None

    try:
        yield guards
    except BaseException:
        # Pass the active exception through ExitStack so guard cleanup never replaces the read failure.
        import sys
        exc_info = sys.exc_info()
        try:
            stack.__exit__(*exc_info)
        except Exception:  # noqa: BLE001 - preserve the active read/timeout/cancellation failure
            logging.getLogger("hub").exception(
                "interactive source lifecycle cleanup failed after read error")
        raise
    else:
        try:
            # Each guard performs its final identity/lease check before it releases its claim.
            stack.close()
        except Exception:
            logging.getLogger("hub").exception(
                "interactive source lifecycle final check failed")
            raise ManagedSourceUnavailable() from None


@contextlib.contextmanager
def local_result_read_scope(storage, uris, *, owner: str):
    """Hold exact local-result readers for the complete lazy-read scope; ignore ordinary sources."""
    guards = []
    with contextlib.ExitStack() as stack:
        acquire = getattr(storage, "acquire_result_read", None)
        managed = (getattr(storage, "requires_result_read", None)
                   or getattr(storage, "is_managed_result_uri", None))
        if callable(acquire) and callable(managed):
            # Classify the complete set before opening a lock/artifact FD, so an oversized or aliased
            # request fails without a partially-acquired prefix.
            for uri in preflight_managed_execution_sources(
                    storage, uris, include_object_attempts=False):
                if not managed(uri):
                    continue
                guards.append(stack.enter_context(acquire(uri, owner)))
        yield guards


class LocalStorage:
    """Outputs live as files under ``root`` (default ``<workspace>/outputs``)."""

    def __init__(self, root: str):
        self.root = root
        self._result_tokens: dict[str, tuple[str, str]] = {}
        self._writer_lock_fds: dict[str, int | None] = {}
        self._pending_writer_releases: dict[str, tuple[str, str]] = {}
        self._pending_aborts: dict[str, tuple[str, str]] = {}
        self._reader_guards: dict[str, LocalResultReadGuard] = {}
        self._pending_reader_releases: dict[str, LocalResultReadGuard] = {}
        self._result_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._result_temp_scan = None
        self._orphan_lock_scan = None
        self._reclaim_fresh_turn = False
        self.lock_supported = fcntl is not None and os.name == "posix"
        os.makedirs(self.root, exist_ok=True)
        self._root_real = os.path.realpath(self.root)
        self.result_root = os.path.join(self._root_real, RESULT_DIR)
        self._result_lock_root = os.path.join(self.result_root, RESULT_LOCK_DIR)
        self._result_dir_fd: int | None = None
        self._result_lock_dir_fd: int | None = None
        self._orphan_lock_cursor: str | None = None
        if os.name == "posix":
            flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
            root_fd = os.open(self._root_real, flags)
            try:
                try:
                    os.mkdir(RESULT_DIR, 0o700, dir_fd=root_fd)
                    self._fsync_dir(root_fd)
                except FileExistsError:
                    pass
                self._result_dir_fd = os.open(RESULT_DIR, flags, dir_fd=root_fd)
                if not stat.S_ISDIR(os.fstat(self._result_dir_fd).st_mode):
                    raise RuntimeError("managed local-result namespace must be a real directory")
                os.fchmod(self._result_dir_fd, 0o700)
                self._fsync_dir(self._result_dir_fd)
                try:
                    os.mkdir(RESULT_LOCK_DIR, 0o700, dir_fd=self._result_dir_fd)
                    self._fsync_dir(self._result_dir_fd)
                except FileExistsError:
                    pass
                self._result_lock_dir_fd = os.open(
                    RESULT_LOCK_DIR, flags, dir_fd=self._result_dir_fd)
                if not stat.S_ISDIR(os.fstat(self._result_lock_dir_fd).st_mode):
                    raise RuntimeError("managed local-result lock namespace must be a real directory")
                os.fchmod(self._result_lock_dir_fd, 0o700)
                self._fsync_dir(self._result_lock_dir_fd)
            finally:
                os.close(root_fd)
        else:  # Windows has no dir_fd/O_DIRECTORY durability contract; automatic GC stays disabled.
            os.makedirs(self._result_lock_root, mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(os.lstat(self.result_root).st_mode):
                raise RuntimeError("managed local-result namespace must be a real directory")
            if not stat.S_ISDIR(os.lstat(self._result_lock_root).st_mode):
                raise RuntimeError("managed local-result lock namespace must be a real directory")
        self.namespace_id = self._load_namespace_id()
        if self._result_dir_fd is not None:
            info = os.fstat(self._result_dir_fd)
        else:
            info = os.stat(self.result_root)
        self._result_namespace_identity = (int(info.st_dev), int(info.st_ino))

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while persisting managed local-result metadata")
            view = view[written:]

    @staticmethod
    def _fsync_dir(fd: int | None) -> None:
        if fd is not None:
            os.fsync(fd)

    def _load_namespace_id(self) -> str:
        """Create/read a stable marker shared only by processes mounting this exact filesystem."""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        marker = RESULT_NAMESPACE_FILE if self._result_dir_fd is not None else os.path.join(
            self.result_root, RESULT_NAMESPACE_FILE)
        open_kwargs = ({"dir_fd": self._result_dir_fd}
                       if self._result_dir_fd is not None else {})
        try:
            fd = os.open(marker, os.O_RDONLY | nofollow, **open_kwargs)
        except FileNotFoundError:
            result_scan_fd = (os.dup(self._result_dir_fd)
                              if self._result_dir_fd is not None else None)
            try:
                with os.scandir(result_scan_fd if result_scan_fd is not None
                                else self.result_root) as entries:
                    unexpected = any(
                        entry.name not in (RESULT_LOCK_DIR, RESULT_NAMESPACE_FILE)
                        and not entry.name.startswith(RESULT_NAMESPACE_FILE + ".tmp-")
                        for entry in entries)
            finally:
                if result_scan_fd is not None:
                    os.close(result_scan_fd)
            lock_scan_fd = (os.dup(self._result_lock_dir_fd)
                            if self._result_lock_dir_fd is not None else None)
            try:
                with os.scandir(lock_scan_fd if lock_scan_fd is not None
                                else self._result_lock_root) as lock_entries:
                    has_lock_entry = next(lock_entries, None) is not None
            finally:
                if lock_scan_fd is not None:
                    os.close(lock_scan_fd)
            if unexpected or has_lock_entry:
                raise RuntimeError(
                    "managed local-result namespace marker is missing from a non-empty namespace")
            value = uuid.uuid4().hex
            tmp_name = f"{RESULT_NAMESPACE_FILE}.tmp-{uuid.uuid4().hex}"
            tmp = (tmp_name if self._result_dir_fd is not None
                   else os.path.join(self.result_root, tmp_name))
            try:
                tmp_fd = os.open(
                    tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600,
                    **open_kwargs)
                try:
                    self._write_all(tmp_fd, (value + "\n").encode("ascii"))
                    os.fsync(tmp_fd)
                finally:
                    os.close(tmp_fd)
                try:
                    if self._result_dir_fd is not None:
                        os.link(
                            tmp_name, RESULT_NAMESPACE_FILE,
                            src_dir_fd=self._result_dir_fd, dst_dir_fd=self._result_dir_fd,
                            follow_symlinks=False)
                    else:
                        os.link(tmp, os.path.join(
                            self.result_root, RESULT_NAMESPACE_FILE), follow_symlinks=False)
                except FileExistsError:
                    pass  # another process atomically published its fully-written marker
                self._fsync_dir(self._result_dir_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp, **open_kwargs)
                self._fsync_dir(self._result_dir_fd)
            fd = os.open(marker, os.O_RDONLY | nofollow, **open_kwargs)
        try:
            marker_info = os.fstat(fd)
            if not stat.S_ISREG(marker_info.st_mode):
                raise RuntimeError("managed local-result namespace marker is not a regular file")
            if os.name == "posix" and marker_info.st_mode & 0o077:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                self._fsync_dir(self._result_dir_fd)
            raw = os.read(fd, 128).decode("ascii").strip()
        finally:
            os.close(fd)
        try:
            canonical = uuid.UUID(raw).hex
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("managed local-result namespace marker is invalid") from exc
        if canonical != raw:
            raise RuntimeError("managed local-result namespace marker is not canonical")
        return canonical

    def output_uri(self, name: str, ext: str) -> str:
        os.makedirs(self.root, exist_ok=True)
        candidate = os.path.join(self.root, f"{name}{ext}")
        self.ensure_output_allowed(candidate)
        return candidate

    def ensure_output_allowed(self, uri: str) -> None:
        """Reject ordinary sinks targeting the private immutable full-result namespace."""
        if "://" in uri and not uri.startswith("file://"):
            return
        candidate = uri[len("file://"):] if uri.startswith("file://") else uri
        if os.path.basename(candidate).casefold().startswith(RESULT_PREFIX.casefold()):
            raise ValueError(f"output names starting with {RESULT_PREFIX!r} are reserved")
        candidate_real = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath(
                (candidate_real.casefold(), self.result_root.casefold())
            ) == self.result_root.casefold()
        except ValueError:
            inside = False
        # Resolve aliases by inode too. The destination normally does not exist, so walk to its nearest
        # existing parent. A hard link to any managed artifact necessarily makes both names multi-link;
        # reject every existing multi-link regular sink in O(1) rather than scanning retained results.
        parent = os.path.dirname(candidate_real)
        while parent and not os.path.exists(parent):
            nxt = os.path.dirname(parent)
            if nxt == parent:
                break
            parent = nxt
        if parent and os.path.exists(parent):
            with contextlib.suppress(OSError):
                inside = inside or os.path.samefile(parent, self.result_root)
        if not inside and os.path.exists(candidate):
            with contextlib.suppress(OSError):
                candidate_info = os.stat(candidate, follow_symlinks=True)
                inside = stat.S_ISREG(candidate_info.st_mode) and candidate_info.st_nlink != 1
        if inside:
            raise ValueError("the managed full-result directory is reserved")

    def result_namespace_identity(self) -> tuple[int, int]:
        return self._result_namespace_identity

    def is_managed_result_uri(self, uri: str | None) -> bool:
        """Return True only for the exact canonical managed URI (trusted lifecycle call sites)."""
        if not uri:
            return False
        try:
            self._result_names(str(uri))
            return True
        except RuntimeError:
            return False

    def requires_result_read(self, uri: str | None) -> bool:
        """Classify source reads and reject aliases that could bypass the lifecycle guard.

        Exact managed URIs return True. A relative/``..``/case/symlink alias of the private namespace
        raises instead of being treated as an ordinary file. Other local and object sources return False.
        """
        if not uri:
            return False
        raw = str(uri)
        from hub.paths import local_path

        try:
            path = local_path(raw)
        except ValueError as exc:
            raise RuntimeError("managed local-result URI is not canonical") from exc
        if path is None:
            return False
        try:
            self._result_names(path)
            return True
        except RuntimeError:
            pass
        parts = [part.casefold() for part in os.path.normpath(path).split(os.sep) if part]
        reserved_shape = (
            RESULT_DIR.casefold() in parts
            and os.path.basename(path).casefold().startswith(RESULT_PREFIX.casefold())
            and os.path.basename(path).casefold().endswith(".parquet"))
        real = os.path.realpath(path)
        try:
            resolves_inside = os.path.commonpath((real, self.result_root)) == self.result_root
        except ValueError:
            resolves_inside = False
        if reserved_shape or resolves_inside:
            raise RuntimeError(
                "managed local results must be read through their exact canonical source URI")
        return False

    def validate_result_uri(
            self, uri: str, expected_identity: tuple[int, int] | None = None) -> None:
        """Fail before adapter I/O if a parent-reserved namespace was replaced."""
        self._result_names(uri)
        current = os.stat(self.result_root)
        identity = (int(current.st_dev), int(current.st_ino))
        expected = (tuple(expected_identity) if expected_identity is not None
                    else self._result_namespace_identity)
        if identity != expected or self._result_namespace_identity != expected:
            raise RuntimeError("managed local-result directory identity changed before write")

    def _check_result_read_identity(self, uri: str, artifact_fd: int) -> None:
        """Revalidate the pathname DuckDB will lazily open against the held artifact inode."""
        artifact_name, _lock_name = self._result_names(uri)
        namespace = os.stat(self.result_root)
        namespace_identity = (int(namespace.st_dev), int(namespace.st_ino))
        if namespace_identity != self._result_namespace_identity:
            raise RuntimeError("managed local-result directory identity changed during read")
        if self._result_dir_fd is not None:
            anchored = os.fstat(self._result_dir_fd)
            if (int(anchored.st_dev), int(anchored.st_ino)) != self._result_namespace_identity:
                raise RuntimeError("managed local-result directory descriptor changed during read")
            current = os.stat(
                artifact_name, dir_fd=self._result_dir_fd, follow_symlinks=False)
        else:
            current = os.lstat(os.path.join(self.result_root, artifact_name))
        held = os.fstat(artifact_fd)
        for info in (held, current):
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (os.name == "posix" and info.st_mode & 0o077)):
                raise RuntimeError(
                    "managed local result must remain an exact private single-link file")
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise RuntimeError("managed local-result artifact identity changed during read")

    def list_outputs(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        out: list[str] = []
        for fn in sorted(os.listdir(self.root)):
            p = os.path.join(self.root, fn)
            if fn.startswith(RESULT_PREFIX):
                continue  # an ephemeral run result, not a published output — never re-catalog it
            if fn.endswith(_EXTS) or (os.path.isdir(p) and fn.endswith(".lance")):
                out.append(p)
        return out

    def recover_orphans(self) -> None:
        """Recover/clean temp siblings an interrupted local write left under ``root`` — run once at startup
        (before re-cataloging outputs). Three hazards it closes: (1) a SIGKILL/OOM/power-loss
        mid append-part write leaves a ``<base>.parttmp-*`` that must NOT surface as a dataset; (2) a crash
        between compaction's two renames leaves ``<base>`` momentarily absent with the data in ``<base>.old-*``
        — restore it so the dataset stays readable; (3) partition overwrite uses the same old/new protocol,
        with ``partition-old`` as the rollback version and ``partition-new`` as unpublished staging.
        Best-effort; never raises.

        Pass 1 restores parked prior versions: if ``<base>`` is gone, the swap was cut between renames and
        the prior version is renamed back; if ``<base>`` exists, publication completed and the old sibling is
        stale. Pass 2 drops append, compaction, and partition staging, which is never the committed copy."""
        if not os.path.isdir(self.root):
            return
        try:
            entries = os.listdir(self.root)
        except OSError:
            return
        def _kind(m: re.Match) -> str:
            return m.group("parttmp") or m.group("kind")

        def _remove(path: str) -> None:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)

        for fn in entries:  # pass 1: restore a swap cut between its two renames
            m = _TEMP_SUFFIX.search(fn)
            if not m or _kind(m) not in ("old", "partition-old"):
                continue
            old, base = os.path.join(self.root, fn), os.path.join(self.root, fn[: m.start()])
            try:
                if os.path.lexists(base):
                    _remove(old)                           # base intact → superseded prior version
                else:
                    os.replace(old, base)                  # base gone mid-swap → restore prior version
            except OSError:
                pass
        for fn in entries:  # pass 2: drop partial/superseded in-flight staging
            m = _TEMP_SUFFIX.search(fn)
            if not m or _kind(m) not in ("parttmp", "compact", "partition-new"):
                continue
            p = os.path.join(self.root, fn)
            try:
                _remove(p)
            except OSError:
                pass

    def begin_result(self, content_key: str, run_id: str) -> str:
        """Reserve one unique exact path and retain its writer process fence."""
        from hub import metadb

        safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", str(content_key))[:64] or "run"
        artifact_name = f"{RESULT_PREFIX}{safe_key}_{uuid.uuid4().hex}.parquet"
        uri = os.path.join(self.result_root, artifact_name)
        lock_name = f"{artifact_name[:-len('.parquet')]}.lock"
        token = uuid.uuid4().hex
        lock_token = uuid.uuid4().hex if self.lock_supported else None
        fd = self._open_shared_lock(lock_name, create=True, lock_token=lock_token)
        try:
            metadb.begin_local_result(
                uri, self.namespace_id, self.result_root, lock_name, self.lock_supported,
                str(run_id), token, lock_token)
        except Exception:
            # The reservation may have committed before the connection failed. Retain its exact owner
            # in the same retry queue as an ordinary pre-publication abort; this is especially important
            # without POSIX locks, where global dead-writer discovery is intentionally disabled.
            owner = (str(run_id), token)
            with self._result_lock:
                self._result_tokens[uri] = owner
                self._writer_lock_fds[uri] = fd
                self._pending_aborts[uri] = owner
            try:
                self._continue_pending_abort(uri, owner)
            except Exception:
                # Preserve the begin error. The exact token and FD remain storage-owned for bounded
                # retry_result_fences(); abandon_local_result serializes behind the uncertain begin.
                import logging
                logging.getLogger("hub").warning(
                    "managed local-result reservation cleanup is pending", exc_info=True)
            raise
        with self._result_lock:
            self._result_tokens[uri] = (str(run_id), token)
            self._writer_lock_fds[uri] = fd
        return uri

    def commit_result(self, uri: str, run_id: str) -> None:
        from hub import metadb

        token = self._writer_token(uri, run_id)
        self.validate_result_uri(uri)
        writer_fd = self.result_lock_fd(uri, run_id)
        lock_token = None
        if writer_fd is not None:
            self._require_regular_lock(writer_fd)
            lock_token = self._read_lock_token(writer_fd)
            _artifact_name, lock_name = self._result_names(uri)
            current = os.stat(
                lock_name, dir_fd=self._result_lock_dir_fd, follow_symlinks=False)
            opened = os.fstat(writer_fd)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError("managed local-result writer lock identity changed")
            fcntl.flock(writer_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            artifact_fd = self._open_result_artifact(uri, allow_public_mode=True)
        except FileNotFoundError as exc:
            raise RuntimeError("managed local-result writer returned without its exact file") from exc
        try:
            info = os.fstat(artifact_fd)
            if info.st_size <= 0:
                raise RuntimeError("managed local result must be non-empty")
            if os.name == "posix":
                os.fchmod(artifact_fd, 0o600)
            os.fsync(artifact_fd)
            self._fsync_dir(self._result_dir_fd)
        finally:
            os.close(artifact_fd)
        metadb.commit_local_result(
            uri, self.namespace_id, str(run_id), token, lock_token)

    def release_result(self, uri: str, run_id: str) -> bool:
        from hub import metadb

        token = self._writer_token(uri, run_id)
        owner = (str(run_id), token)
        with self._result_lock:
            if uri in self._pending_aborts:
                raise RuntimeError("cannot release a local result while abort is unresolved")
            self._pending_writer_releases[uri] = owner
        try:
            released = metadb.release_local_result_writer(
                uri, self.namespace_id, str(run_id), token)
        except Exception:
            # Commit may have succeeded.  Retain token+SH FD and let bounded maintenance retry the
            # idempotent release instead of making the artifact look abortable.
            raise
        if not released:
            # A terminal caller should already have created the durable owner.  Treat a missing ref as
            # a retryable invariant failure; dropping the pending marker would leak the SH FD forever.
            return False
        with self._result_lock:
            if self._pending_writer_releases.get(uri) == owner:
                self._pending_writer_releases.pop(uri, None)
            self._result_tokens.pop(uri, None)
            fd = self._writer_lock_fds.pop(uri, None)
        if fd is not None:
            os.close(fd)
        return True

    def result_publication_receipt(
            self, uri: str, run_id: str, expected_doc: dict) -> bool:
        """Bind a terminal RunState read-back receipt to this exact filesystem namespace."""
        from hub import metadb

        canonical_uri = uri[len("file://"):] if uri.startswith("file://") else uri
        self._result_names(canonical_uri)
        raw_expected = next((expected_doc.get(key) for key in (
            "uri", "outputUri", "output_uri") if expected_doc.get(key)), None)
        expected_uri = str(raw_expected).rstrip("/") if raw_expected else None
        if expected_uri and expected_uri.startswith("file://"):
            expected_uri = expected_uri[len("file://"):]
        if expected_uri != canonical_uri:
            return False
        return metadb.local_result_run_state_receipt(
            str(run_id), self.namespace_id, expected_doc)

    def abort_result(self, uri: str, run_id: str) -> None:
        """Abort only after the caller has proved its writer stopped."""
        token = self._writer_token(uri, run_id)
        owner = (str(run_id), token)
        with self._result_lock:
            if uri in self._pending_writer_releases:
                raise RuntimeError(
                    "cannot abort while successful publication release is unresolved")
            current = self._pending_aborts.get(uri)
            if current is not None and current != owner:
                raise RuntimeError("local result abort belongs to a different writer")
            self._pending_aborts[uri] = owner
        self._continue_pending_abort(uri, owner)

    def _continue_pending_abort(self, uri: str, owner: tuple[str, str]) -> None:
        """Idempotently finish one exact abort after an unknown metadata commit outcome."""
        from hub import metadb

        run_id, token = owner
        delete_token = metadb.abandon_local_result(
            uri, self.namespace_id, run_id, token)
        with self._result_lock:
            # A concurrent original/retry may already have taken responsibility for close+delete.
            if (self._pending_aborts.get(uri) != owner
                    or self._result_tokens.get(uri) != owner):
                return
            self._pending_aborts.pop(uri, None)
            self._result_tokens.pop(uri, None)
            fd = self._writer_lock_fds.pop(uri, None)
        lock_token = None
        if fd is not None:
            try:
                lock_token = self._read_lock_token(fd)
            finally:
                os.close(fd)
        if delete_token:
            self._delete_claimed_result(
                uri, delete_token, lock_token=lock_token, explicit=True)

    def _writer_token(self, uri: str, run_id: str) -> str:
        with self._result_lock:
            owner = self._result_tokens.get(uri)
        if owner is None or owner[0] != str(run_id):
            raise RuntimeError("local result is not owned by this writer")
        return owner[1]

    def _result_names(self, uri: str) -> tuple[str, str]:
        path = uri[len("file://"):] if uri.startswith("file://") else uri
        if (os.path.dirname(path) != self.result_root
                or not os.path.basename(path).startswith(RESULT_PREFIX)
                or not path.endswith(".parquet")):
            raise RuntimeError("path is outside the managed local-result namespace")
        artifact_name = os.path.basename(path)
        return artifact_name, f"{artifact_name[:-len('.parquet')]}.lock"

    def _open_result_artifact(self, uri: str, *, allow_public_mode: bool = False) -> int:
        artifact_name, _lock_name = self._result_names(uri)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = (os.open(artifact_name, flags, dir_fd=self._result_dir_fd)
              if self._result_dir_fd is not None else os.open(uri, flags))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (not allow_public_mode and os.name == "posix" and info.st_mode & 0o077)):
                raise RuntimeError(
                    "managed local result must be an exact single-link regular file")
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _validate_lock_name(lock_name: str) -> None:
        if (os.path.basename(lock_name) != lock_name
                or not lock_name.startswith(RESULT_PREFIX)
                or not lock_name.endswith(".lock")):
            raise RuntimeError("invalid managed local-result lock name")

    def _open_shared_lock(
            self, lock_name: str, *, create: bool = False,
            lock_token: str | None = None) -> int | None:
        self._validate_lock_name(lock_name)
        if not self.lock_supported:
            return None
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(lock_name, flags, 0o600, dir_fd=self._result_lock_dir_fd)
        try:
            self._require_regular_lock(fd)
            if create:
                if not lock_token:
                    raise RuntimeError("managed local-result lock token is missing")
                # Fence the inode immediately after O_EXCL. Otherwise the orphan sweep could observe a
                # valid token before this creator takes SH, unlink it under EX, and leave the eventual
                # DB row bound to an unreachable open descriptor.
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self._write_all(fd, (lock_token + "\n").encode("ascii"))
                os.fsync(fd)
                self._fsync_dir(self._result_lock_dir_fd)
            else:
                if not self._read_lock_token(fd):
                    raise RuntimeError("managed local-result lock token is invalid")
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _require_regular_lock(fd: int) -> None:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or (os.name == "posix" and info.st_mode & 0o077)):
            raise RuntimeError("managed local-result lock is not a regular file")

    @staticmethod
    def _read_lock_token(fd: int) -> str:
        raw = os.pread(fd, 128, 0).decode("ascii").strip()
        try:
            canonical = uuid.UUID(raw).hex
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("managed local-result lock token is invalid") from exc
        if canonical != raw:
            raise RuntimeError("managed local-result lock token is not canonical")
        return canonical

    def result_lock_fd(self, uri: str, run_id: str) -> int | None:
        """Return the writer lock descriptor that an isolated child must inherit at Popen."""
        self._writer_token(uri, run_id)
        with self._result_lock:
            fd = self._writer_lock_fds.get(uri)
        if self.lock_supported and fd is None:
            raise RuntimeError("local result writer lock is unavailable")
        return fd

    def acquire_result_read(self, uri: str, owner: str) -> LocalResultReadGuard:
        """Acquire an ephemeral read guard before any existence check or lazy adapter scan."""
        from hub import metadb
        from hub.paths import local_path

        canonical_uri = local_path(uri)
        if canonical_uri is None:
            raise ValueError("managed local-result reader requires a local URI")
        self.validate_result_uri(canonical_uri)
        _artifact_name, lock_name = self._result_names(canonical_uri)
        reader_id = f"{owner}:{uuid.uuid4().hex}"
        try:
            fd = self._open_shared_lock(lock_name)
        except Exception as exc:
            raise FileNotFoundError("managed local result is being reclaimed") from exc
        guard = LocalResultReadGuard(
            self, canonical_uri, reader_id, fd, -1)
        # Register cleanup ownership before the first DB mutation. acquire_local_result_read() may
        # commit and then raise, and a later artifact open/check may fail after a successful commit.
        with self._result_lock:
            self._reader_guards[reader_id] = guard
        try:
            lock_token = self._read_lock_token(fd) if fd is not None else None
            if not metadb.acquire_local_result_read(
                    canonical_uri, self.namespace_id, lock_name, reader_id, lock_token):
                raise FileNotFoundError("managed local result is missing or being reclaimed")
            guard._artifact_fd = self._open_result_artifact(canonical_uri)
            guard.check()
        except Exception:
            try:
                guard.close()
            except Exception:
                # The acquisition/integrity error is primary. close() marked the exact reader pending
                # before release, retaining its lock/artifact FDs for bounded commit-unknown retry.
                import logging
                logging.getLogger("hub").warning(
                    "managed local-result failed-acquire cleanup is pending", exc_info=True)
            raise
        return guard

    def _forget_result_reader(self, reader_id: str) -> None:
        with self._result_lock:
            self._reader_guards.pop(reader_id, None)
            self._pending_reader_releases.pop(reader_id, None)

    def _mark_result_reader_release(self, guard: LocalResultReadGuard) -> None:
        with self._result_lock:
            if self._reader_guards.get(guard.reader_id) is guard:
                self._pending_reader_releases[guard.reader_id] = guard

    def _retry_pending_reader_releases(self, limit: int) -> None:
        """Bounded retry only for guards whose caller already requested close()."""
        if limit <= 0:
            return
        with self._result_lock:
            pending = list(itertools.islice(
                self._pending_reader_releases.values(), limit))
        for guard in pending:
            with contextlib.suppress(Exception):
                guard.close()

    def _retry_pending_writer_releases(self, limit: int) -> None:
        """Retry only post-publication writer releases; never reinterpret them as aborts."""
        from hub import metadb

        if limit <= 0:
            return
        with self._result_lock:
            pending = list(itertools.islice(
                self._pending_writer_releases.items(), limit))
        for uri, owner in pending:
            run_id, token = owner
            try:
                released = metadb.release_local_result_writer(
                    uri, self.namespace_id, run_id, token)
            except Exception:
                continue
            if not released:
                continue
            with self._result_lock:
                if self._pending_writer_releases.get(uri) != owner:
                    continue
                self._pending_writer_releases.pop(uri, None)
                self._result_tokens.pop(uri, None)
                fd = self._writer_lock_fds.pop(uri, None)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _retry_pending_aborts(self, limit: int) -> None:
        """Boundedly replay aborts whose metadata commit result was unknown."""
        if limit <= 0:
            return
        with self._result_lock:
            pending = list(itertools.islice(self._pending_aborts.items(), limit))
        for uri, owner in pending:
            with contextlib.suppress(Exception):
                self._continue_pending_abort(uri, owner)

    def retry_result_fences(self, limit: int = 50) -> None:
        """Retry only process-owned pending fences; never perform global storage maintenance."""
        limit = max(0, min(int(limit), 500))
        with self._maintenance_lock:
            self._retry_pending_writer_releases(limit)
            self._retry_pending_reader_releases(limit)
            self._retry_pending_aborts(limit)

    def _reconcile_dead_locks(self, limit: int) -> None:
        from hub import metadb

        if not self.lock_supported:
            return
        for uri, lock_name, lock_token in metadb.local_result_lock_candidates(
                self.namespace_id, limit=limit):
            try:
                self._validate_lock_name(lock_name)
                flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(
                    lock_name, flags, 0o600, dir_fd=self._result_lock_dir_fd)
                try:
                    self._require_regular_lock(fd)
                    if self._read_lock_token(fd) != lock_token:
                        continue
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    metadb.reconcile_dead_local_result(
                        uri, self.namespace_id, lock_name)
                finally:
                    os.close(fd)
            except (OSError, BlockingIOError, RuntimeError):
                continue

    def _delete_claimed_result(
            self, uri: str, delete_token: str, *, lock_token: str | None,
            explicit: bool = False) -> None:
        from hub import metadb

        artifact_name, lock_name = self._result_names(uri)
        if not self.lock_supported and not explicit:
            raise RuntimeError("automatic local-result GC requires inherited POSIX file locks")
        fd = None
        if self.lock_supported:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_name, flags, 0o600, dir_fd=self._result_lock_dir_fd)
        try:
            if fd is not None:
                self._require_regular_lock(fd)
                if lock_token is None or self._read_lock_token(fd) != lock_token:
                    raise RuntimeError("managed local-result lock token changed")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlink_exact_data() -> None:
                if self._result_dir_fd is not None:
                    try:
                        info = os.stat(
                            artifact_name, dir_fd=self._result_dir_fd, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise RuntimeError("managed local result is not an exact file")
                        os.unlink(artifact_name, dir_fd=self._result_dir_fd)
                    except FileNotFoundError:
                        pass
                    self._fsync_dir(self._result_dir_fd)
                    return
                current = os.stat(self.result_root)
                identity = (int(current.st_dev), int(current.st_ino))
                if identity != self._result_namespace_identity:
                    raise RuntimeError(
                        "managed local-result directory identity changed before deletion")
                path = os.path.join(self.result_root, artifact_name)
                try:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise RuntimeError("managed local result is not an exact file")
                    os.unlink(path)
                except FileNotFoundError:
                    pass

            deleted = metadb.delete_local_result(
                uri, self.namespace_id, delete_token, unlink_exact_data)
            if fd is not None:
                # The DB row is now durably gone. Removing the lock before that commit would leave a
                # deleting row permanently unretryable after a crash/rollback.
                if not deleted and self._result_path_exists(artifact_name):
                    raise RuntimeError("managed local-result row disappeared before data deletion")
                self._unlink_exact_lock(lock_name, fd)
        finally:
            if fd is not None:
                os.close(fd)

    def _result_path_exists(self, artifact_name: str) -> bool:
        try:
            if self._result_dir_fd is not None:
                os.stat(artifact_name, dir_fd=self._result_dir_fd, follow_symlinks=False)
            else:
                os.lstat(os.path.join(self.result_root, artifact_name))
            return True
        except FileNotFoundError:
            return False

    def _scan_batch(
            self, attr: str, dir_fd: int, budget: int
            ) -> list[tuple[str, os.stat_result | None]]:
        """Advance one persistent anchored directory cursor by at most ``budget`` entries."""
        state = getattr(self, attr)
        if state is None:
            scan_fd = os.dup(dir_fd)
            try:
                iterator = os.scandir(scan_fd)
            except Exception:
                os.close(scan_fd)
                raise
            state = (iterator, scan_fd)
            setattr(self, attr, state)
        iterator, scan_fd = state
        out = []
        try:
            for _ in range(max(0, budget)):
                try:
                    entry = next(iterator)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    out.append((entry.name, info))
                except StopIteration:
                    iterator.close()
                    os.close(scan_fd)
                    setattr(self, attr, None)
                    break
        except Exception:
            iterator.close()
            os.close(scan_fd)
            setattr(self, attr, None)
            raise
        return out

    def _reconcile_orphan_result_temps(self, limit: int) -> None:
        """Boundedly remove adapter temps whose unique base URI has no file or registry row."""
        from hub import metadb

        if self._result_dir_fd is None or limit <= 0:
            return
        removed = False
        cleaned = 0
        for name, info in self._scan_batch(
                "_result_temp_scan", self._result_dir_fd, _MAINTENANCE_SCAN_BUDGET):
            match = _RESULT_TEMP_SUFFIX.fullmatch(name)
            if match is None or cleaned >= limit:
                continue
            artifact_name = match.group("artifact")
            uri = os.path.join(self.result_root, artifact_name)
            try:
                if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                if self._result_path_exists(artifact_name):
                    continue
                if not metadb.local_result_uri_absent(uri, self.namespace_id):
                    continue
                current = os.stat(
                    name, dir_fd=self._result_dir_fd, follow_symlinks=False)
                if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                    continue
                os.unlink(name, dir_fd=self._result_dir_fd)
                removed = True
                cleaned += 1
            except (FileNotFoundError, OSError, RuntimeError):
                continue
        if removed:
            self._fsync_dir(self._result_dir_fd)

    def _unlink_exact_lock(self, lock_name: str, fd: int) -> None:
        actual = os.fstat(fd)
        try:
            current = os.stat(
                lock_name, dir_fd=self._result_lock_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if ((current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino)
                or not stat.S_ISREG(current.st_mode)):
            raise RuntimeError("managed local-result lock path changed before cleanup")
        os.unlink(lock_name, dir_fd=self._result_lock_dir_fd)
        self._fsync_dir(self._result_lock_dir_fd)

    def _reconcile_orphan_locks(self, limit: int) -> None:
        """Bounded rotating cleanup for a crash after DB row commit but before lock unlink."""
        from hub import metadb

        if not self.lock_supported or limit <= 0 or self._result_lock_dir_fd is None:
            return
        cleaned = 0
        for lock_name, _info in self._scan_batch(
                "_orphan_lock_scan", self._result_lock_dir_fd, _MAINTENANCE_SCAN_BUDGET):
            if cleaned >= limit:
                continue
            if not (lock_name.startswith(RESULT_PREFIX) and lock_name.endswith(".lock")):
                continue
            fd = None
            try:
                self._validate_lock_name(lock_name)
                fd = os.open(
                    lock_name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._result_lock_dir_fd)
                self._require_regular_lock(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                artifact_name = lock_name[:-len(".lock")] + ".parquet"
                uri = os.path.join(self.result_root, artifact_name)
                if self._result_path_exists(artifact_name):
                    continue
                try:
                    token = self._read_lock_token(fd)
                except RuntimeError:
                    if not metadb.local_result_lock_row_absent(
                            uri, self.namespace_id, lock_name):
                        continue
                else:
                    if not metadb.local_result_artifact_absent(
                            uri, self.namespace_id, lock_name, token):
                        continue
                self._unlink_exact_lock(lock_name, fd)
                cleaned += 1
            except (OSError, BlockingIOError, RuntimeError):
                continue
            finally:
                if fd is not None:
                    os.close(fd)

    def prune_results(self, limit: int = 50) -> None:
        """Run one bounded exact, reference-aware retention pass."""
        from hub import metadb

        limit = max(0, min(int(limit), 500))
        with self._maintenance_lock:
            self._retry_pending_writer_releases(limit)
            self._retry_pending_reader_releases(limit)
            self._retry_pending_aborts(limit)
            if not self.lock_supported:
                for uri, delete_token, lock_token in metadb.claim_deleting_local_results(
                        self.namespace_id, limit=limit):
                    try:
                        self._delete_claimed_result(
                            uri, delete_token, lock_token=lock_token, explicit=True)
                    except Exception:
                        continue
                return
            self._reconcile_dead_locks(limit)
            prefer_fresh = limit == 1 and self._reclaim_fresh_turn
            if limit == 1:
                self._reclaim_fresh_turn = not self._reclaim_fresh_turn
            for uri, delete_token, lock_token in metadb.claim_local_result_reclaims(
                    self.namespace_id, limit=limit, prefer_fresh=prefer_fresh):
                try:
                    self._delete_claimed_result(
                        uri, delete_token, lock_token=lock_token)
                except Exception:
                    continue
            self._reconcile_orphan_result_temps(limit)
            self._reconcile_orphan_locks(limit)

    def close(self) -> None:
        with self._maintenance_lock:
            for attr in ("_result_temp_scan", "_orphan_lock_scan"):
                state = getattr(self, attr, None)
                if state is not None:
                    iterator, scan_fd = state
                    with contextlib.suppress(Exception):
                        iterator.close()
                    with contextlib.suppress(OSError):
                        os.close(scan_fd)
                    setattr(self, attr, None)
            with self._result_lock:
                writer_fds = list(self._writer_lock_fds.values())
                readers = list(self._reader_guards.values())
                self._writer_lock_fds.clear()
                self._result_tokens.clear()
                self._pending_writer_releases.clear()
                self._pending_aborts.clear()
            for guard in readers:
                with contextlib.suppress(Exception):
                    guard.close()
            for fd in writer_fds:
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            with self._result_lock:
                pending_readers = any(
                    not guard._closed for guard in self._reader_guards.values())
            if pending_readers:
                return
            for attr in ("_result_dir_fd", "_result_lock_dir_fd"):
                fd = getattr(self, attr, None)
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                    setattr(self, attr, None)

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()


class ObjectStorage:
    """Outputs live under an object-store prefix (``s3://…`` / ``gs://…``). Reads and writes go
    through the same DuckDB httpfs path the adapters use — the write node just writes to the uri
    handed back."""

    def __init__(self, root: str):
        self.root = root.rstrip("/")
        from hub import metadb
        metadb.bind_object_storage_root(self.root)

    def output_uri(self, name: str, ext: str) -> str:
        return f"{self.root}/{name}{ext}"

    def recover_orphans(self) -> None:
        return  # object append writes unique part names directly (no temp-sibling staging, no compaction)

    def list_outputs(self) -> list[str]:
        from hub import db
        try:  # missing creds / unreachable bucket at boot must not crash startup — just show nothing
            db.ensure_object_store()
            with db.lock():
                rows = db.conn().execute(f"SELECT file FROM glob('{self.root}/*')").fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [f for (f,) in rows
                if f.lower().endswith(_EXTS) and not os.path.basename(f.rstrip("/")).startswith(RESULT_PREFIX)]


def make_storage(workspace: str) -> Storage:
    """DP_STORAGE_URL selects the backend. Default (unset) = ``<workspace>/outputs`` locally; a
    ``file://`` or absolute path overrides the dir; an ``s3://…`` / ``gs://…`` uri persists outputs
    to that object-store prefix (real, via httpfs). For a CUSTOM sink, set ``DP_STORAGE`` to a dotted
    path to a Storage class (``pkg.mod:Cls``), instantiated as ``Cls(workspace)`` — a plugin sink with
    no core patch. The built-in Local/Object storages are just the two default paths here."""
    cls = os.environ.get("DP_STORAGE", "").strip()
    if cls:
        from hub.settings import import_dotted
        return import_dotted(cls)(workspace)
    url = os.environ.get("DP_STORAGE_URL", "").strip()
    if is_object_uri(url):
        return ObjectStorage(url)
    root = url[len("file://"):] if url.startswith("file://") else (url or os.path.join(workspace, "outputs"))
    return LocalStorage(root)
