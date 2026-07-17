from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import queue
import shutil
import stat
import threading
import uuid
from pathlib import Path

from pydantic import ValidationError

from hub import metadb
from hub.external_wait import (
    ExternalWaitCheckpoint,
    ExternalWaitHandle,
    ExternalWaitPollOutcome,
    ExternalWaitSubmitRequest,
)

_CALL_TIMEOUT_SECONDS = 1.0
_MAX_PROVIDER_CALLS = 8
_calls: queue.Queue[tuple[dict, str, object]] = queue.Queue(maxsize=_MAX_PROVIDER_CALLS)
_capacity = threading.BoundedSemaphore(_MAX_PROVIDER_CALLS)
_state_lock = threading.Lock()
_inflight: dict[str, int] = {}
_workers_started = False


def _commit_failure(claim: dict, token: str, code: str, delay: float = 0.25) -> None:
    metadb.commit_external_wait_transition(
        claim["task_id"],
        claim["attempt_id"],
        token,
        failure_code=code,
        retry_after=delay,
    )


def _provider_call(claim: dict, deps) -> tuple[str, dict | None]:
    adapter = deps._external_wait_adapter(claim["provider_kind"])
    if adapter is None:
        return "adapter_unavailable", None
    request = ExternalWaitSubmitRequest.model_validate(claim["submit_request"])
    if claim["handle"] is None:
        handle = ExternalWaitHandle.model_validate(adapter.submit(request))
        if handle.provider_kind != request.provider_kind:
            raise ValueError("provider kind mismatch")
        return "handle", handle.model_dump(mode="json")
    handle = ExternalWaitHandle.model_validate(claim["handle"])
    checkpoint = (
        ExternalWaitCheckpoint.model_validate(claim["checkpoint"])
        if claim["checkpoint"] is not None
        else None
    )
    raw = (
        adapter.cancel(handle, checkpoint)
        if claim["cancel_requested"]
        else adapter.status(handle, checkpoint)
    )
    return "outcome", ExternalWaitPollOutcome.model_validate(raw).model_dump(
        mode="json"
    )


def _stage_paths(claim: dict, deps) -> tuple[Path, Path, Path]:
    root = Path(deps.workspace).resolve() / ".dp-external-stage"
    _managed_dir(root)
    digest = hashlib.sha256(str(claim["task_id"]).encode()).hexdigest()[:32]
    stage = root / "attempts" / f"{digest}-{claim['attempt_number']}"
    return stage, stage / "result.csv", root / ".locks" / f"{digest}.lock"


def _managed_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError("external wait staging root is invalid")


@contextlib.contextmanager
def _stage_lock(lock_path: Path):
    _managed_dir(lock_path.parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _prepare_stage(claim: dict, deps, token: str) -> tuple[Path, tuple[int, int]]:
    stage, target, _lock = _stage_paths(claim, deps)
    _managed_dir(stage.parent)
    expected = (claim.get("stage_dev"), claim.get("stage_ino"))
    if stage.exists() or stage.is_symlink():
        info = stage.lstat()
        if expected == (None, None):
            if stat.S_ISLNK(info.st_mode):
                stage.unlink()
            elif stat.S_ISDIR(info.st_mode):
                shutil.rmtree(stage)
            else:
                raise ValueError("external wait staging root is invalid")
        elif (info.st_dev, info.st_ino) != expected or not stat.S_ISDIR(info.st_mode):
            raise ValueError("external wait staging root identity changed")
    if not stage.exists():
        stage.mkdir(mode=0o700)
    info = stage.lstat()
    identity = (int(info.st_dev), int(info.st_ino))
    if not metadb.pin_external_wait_stage(
        claim["task_id"], claim["attempt_id"], token, *identity
    ):
        raise RuntimeError("external wait staging owner was fenced")
    return target, identity


def _cleanup_stage(claim: dict, deps, identity: tuple[int, int]) -> bool:
    stage, _target, _lock = _stage_paths(claim, deps)
    try:
        info = stage.lstat()
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(info.st_mode):
        stage.unlink()
        return True
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        return False
    entries = list(stage.iterdir())
    if any(entry.name != "result.csv" or entry.is_dir() for entry in entries):
        return False
    for entry in entries:
        entry.unlink()
    return True


def _remove_empty_stage(claim: dict, deps, identity: tuple[int, int]) -> None:
    stage, _target, _lock = _stage_paths(claim, deps)
    with contextlib.suppress(OSError):
        info = stage.lstat()
        if (info.st_dev, info.st_ino) == identity:
            stage.rmdir()


def _validate_download(target: Path, identity: tuple[int, int], raw) -> dict:
    from hub.external_wait import ExternalWaitDownloadEvidence

    evidence = ExternalWaitDownloadEvidence.model_validate(raw)
    root = target.parent
    root_info = root.lstat()
    info = target.lstat()
    entries = list(root.iterdir())
    if (root_info.st_dev, root_info.st_ino) != identity or not stat.S_ISDIR(
        root_info.st_mode
    ):
        raise ValueError("staging root identity changed")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or len(entries) != 1
        or entries[0].name != target.name
        or os.path.commonpath((str(target.resolve()), str(root.resolve())))
        != str(root.resolve())
        or evidence.media_type != "text/csv"
        or info.st_size != evidence.bytes_written
    ):
        raise ValueError("download evidence does not match the staged result")
    digest = hashlib.sha256()
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        expected = (info.st_dev, info.st_ino, info.st_size, info.st_nlink, info.st_mode)
        observed = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_nlink,
            opened.st_mode,
        )
        if observed != expected or not stat.S_ISREG(opened.st_mode):
            raise ValueError("download result identity changed during validation")
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after_file = os.fstat(fd)
        if (
            after_file.st_dev,
            after_file.st_ino,
            after_file.st_size,
            after_file.st_nlink,
            after_file.st_mode,
        ) != observed:
            raise ValueError("download result changed during validation")
    finally:
        os.close(fd)
    if digest.hexdigest() != evidence.sha256:
        raise ValueError("download digest does not match the staged result")
    after = root.lstat()
    if (after.st_dev, after.st_ino) != identity or [
        entry.name for entry in root.iterdir()
    ] != [target.name]:
        raise ValueError("staging root identity changed")
    return evidence.model_dump(mode="json")


def _download(claim: dict, token: str, deps) -> None:
    stage, _target, lock_path = _stage_paths(claim, deps)
    del stage
    with _stage_lock(lock_path) as locked:
        if not locked:
            metadb.fail_external_wait_finalization(
                claim["task_id"],
                claim["attempt_id"],
                token,
                "external_wait_stage_busy",
                permanent=False,
            )
            return
        try:
            if claim.get("action") == "cancel_after_success":
                identity = (claim.get("stage_dev"), claim.get("stage_ino"))
                current = metadb.heartbeat_external_wait_transition(
                    claim["task_id"], claim["attempt_id"], token
                )
                if current and (
                    None in identity or _cleanup_stage(claim, deps, identity)
                ):
                    done = metadb.cancel_external_wait_after_success(
                        claim["task_id"], claim["attempt_id"], token
                    )
                    if done:
                        _remove_empty_stage(claim, deps, identity)
                return
            target, identity = _prepare_stage(claim, deps, token)
            if claim["cancel_requested"]:
                if _cleanup_stage(claim, deps, identity):
                    metadb.cancel_external_wait_after_success(
                        claim["task_id"], claim["attempt_id"], token
                    )
                return
            adapter = deps._external_wait_adapter(claim["provider_kind"])
            if adapter is None:
                raise RuntimeError("external wait adapter is unavailable")
            handle = ExternalWaitHandle.model_validate(claim["handle"])
            evidence = _validate_download(
                target, identity, adapter.download(handle, target)
            )
            committed = metadb.commit_external_wait_download(
                claim["task_id"], claim["attempt_id"], token, evidence
            )
            if (
                committed == "cancel_requested"
                and metadb.heartbeat_external_wait_transition(
                    claim["task_id"], claim["attempt_id"], token
                )
                and _cleanup_stage(claim, deps, identity)
            ):
                done = metadb.cancel_external_wait_after_success(
                    claim["task_id"], claim["attempt_id"], token
                )
                if done:
                    _remove_empty_stage(claim, deps, identity)
        except (ValidationError, ValueError, TypeError, KeyError, OSError):
            identity = locals().get("identity")
            current = metadb.heartbeat_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token
            )
            if current and identity is not None:
                _cleanup_stage(claim, deps, identity)
                done = metadb.fail_external_wait_finalization(
                    claim["task_id"],
                    claim["attempt_id"],
                    token,
                    "external_wait_download_invalid",
                    permanent=True,
                )
                if done:
                    _remove_empty_stage(claim, deps, identity)
        except BaseException:
            metadb.fail_external_wait_finalization(
                claim["task_id"],
                claim["attempt_id"],
                token,
                "external_wait_download_failed",
                permanent=False,
            )


def _publish(claim: dict, token: str, deps) -> None:
    from hub.local_writes import write_managed_local_file
    from hub.models import WriteIntent
    from hub import metadb as catalog_db
    import pyarrow.csv as arrow_csv
    import pyarrow.parquet as parquet

    _stage, target, lock_path = _stage_paths(claim, deps)
    identity = (claim.get("stage_dev"), claim.get("stage_ino"))
    with _stage_lock(lock_path) as locked:
        if not locked or None in identity:
            metadb.fail_external_wait_finalization(
                claim["task_id"],
                claim["attempt_id"],
                token,
                "external_wait_stage_busy",
                permanent=False,
            )
            return
        try:
            intent = WriteIntent.model_validate(claim["write_intent"])

            def write_candidate(uri: str) -> None:
                _validate_download(target, identity, claim["download_evidence"])
                parquet.write_table(arrow_csv.read_csv(target), uri)

            receipt = write_managed_local_file(
                storage=deps.storage,
                catalog=deps.catalog,
                intent=intent,
                write_artifact=write_candidate,
            )
            if not metadb.heartbeat_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token
            ):
                raise RuntimeError("external wait staging cleanup failed")
            cleaned = _cleanup_stage(claim, deps, identity)
            done = metadb.finish_external_wait_publication(
                claim["task_id"],
                claim["attempt_id"],
                token,
                receipt.model_dump(by_alias=True, mode="json"),
            )
            if done and cleaned:
                _remove_empty_stage(claim, deps, identity)
        except catalog_db.ManagedLocalWriteConflict:
            if metadb.heartbeat_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token
            ):
                _cleanup_stage(claim, deps, identity)
                done = metadb.fail_external_wait_finalization(
                    claim["task_id"],
                    claim["attempt_id"],
                    token,
                    "external_wait_destination_stale",
                    permanent=True,
                )
                if done:
                    _remove_empty_stage(claim, deps, identity)
        except (ValidationError, ValueError, TypeError, KeyError, OSError):
            if metadb.heartbeat_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token
            ):
                _cleanup_stage(claim, deps, identity)
                done = metadb.fail_external_wait_finalization(
                    claim["task_id"],
                    claim["attempt_id"],
                    token,
                    "external_wait_publication_invalid",
                    permanent=True,
                )
                if done:
                    _remove_empty_stage(claim, deps, identity)
        except BaseException:
            metadb.fail_external_wait_finalization(
                claim["task_id"],
                claim["attempt_id"],
                token,
                "external_wait_publication_failed",
                permanent=False,
            )


def _perform(claim: dict, token: str, deps) -> None:
    if claim.get("action") == "download":
        _download(claim, token, deps)
        return
    if claim.get("action") == "cancel_after_success":
        _download(claim, token, deps)
        return
    if claim.get("action") == "publish":
        _publish(claim, token, deps)
        return
    timer = threading.Timer(
        _CALL_TIMEOUT_SECONDS,
        _commit_failure,
        args=(claim, token, "adapter_transition_timeout"),
    )
    timer.daemon = True
    timer.start()
    try:
        try:
            kind, value = _provider_call(claim, deps)
        except (ValidationError, ValueError, TypeError, KeyError):
            kind, value = "adapter_return_invalid", None
        except BaseException:
            kind, value = "adapter_transient_failure", None
        timer.cancel()
        if kind == "handle":
            metadb.commit_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token, handle=value
            )
        elif kind == "outcome":
            metadb.commit_external_wait_transition(
                claim["task_id"], claim["attempt_id"], token, outcome=value
            )
        else:
            _commit_failure(
                claim, token, kind, 1.0 if kind == "adapter_unavailable" else 0.25
            )
    finally:
        timer.cancel()


def _release_call(task_id: str) -> None:
    with _state_lock:
        count = _inflight.get(task_id, 1) - 1
        if count:
            _inflight[task_id] = count
        else:
            _inflight.pop(task_id, None)
    _capacity.release()


def _worker() -> None:
    while True:
        item = _calls.get()
        try:
            _perform(*item)
        except BaseException:
            pass
        finally:
            _release_call(item[0]["task_id"])
            _calls.task_done()


def _start_workers() -> None:
    global _workers_started
    with _state_lock:
        if _workers_started:
            return
        _workers_started = True
        for index in range(_MAX_PROVIDER_CALLS):
            threading.Thread(
                target=_worker, daemon=True, name=f"dp-external-wait-{index + 1}"
            ).start()


def recover(deps) -> None:
    _start_workers()
    metadb.fail_corrupt_external_wait_tasks()
    metadb.expire_external_wait_deadlines()
    for task_id in metadb.due_external_wait_task_ids():
        if not _capacity.acquire(blocking=False):
            return
        with _state_lock:
            if _inflight.get(task_id, 0) >= 2:
                _capacity.release()
                continue
            _inflight[task_id] = _inflight.get(task_id, 0) + 1
        token = uuid.uuid4().hex
        claim = metadb.claim_external_wait_transition(task_id, token)
        if claim is None:
            with _state_lock:
                _inflight[task_id] -= 1
                if not _inflight[task_id]:
                    _inflight.pop(task_id)
            _capacity.release()
            continue
        _calls.put_nowait((claim, token, deps))
