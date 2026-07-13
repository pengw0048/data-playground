"""Deterministic coverage for password KDF resource and brute-force admission."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient

from hub import auth, auth_admission, metadb
from hub.auth_admission import AttemptLimiter, PasswordWorkGate
from hub.main import app
from hub.routers import workspace


def _put_user(user_id: str, password: str, *, admin: bool = False, epoch: int = 0) -> str:
    password_hash = auth.hash_password(password)
    with metadb.session() as session:
        existing = session.get(metadb.User, user_id)
        if existing is None:
            session.add(
                metadb.User(
                    id=user_id,
                    name=user_id,
                    password_hash=password_hash,
                    is_admin=admin,
                    token_epoch=epoch,
                )
            )
        else:
            existing.password_hash = password_hash
            existing.is_admin = admin
            existing.token_epoch = epoch
    return password_hash


def _limiter(
    *,
    capacity: int = 100,
    refill_seconds: float = 60,
    max_entries: int = 128,
    clock=None,
) -> AttemptLimiter:
    kwargs = dict(
        capacity=capacity,
        refill_seconds=refill_seconds,
        entry_ttl_seconds=600,
        max_entries=max_entries,
    )
    if clock is not None:
        kwargs["clock"] = clock
    return AttemptLimiter(**kwargs)


class _CountingGate(PasswordWorkGate):
    def __init__(self, capacity: int):
        super().__init__(capacity)
        self._active = 0
        self._active_lock = threading.Lock()
        self.zero = threading.Event()
        self.zero.set()

    @property
    def active(self) -> int:
        with self._active_lock:
            return self._active

    def try_acquire(self) -> bool:
        if not super().try_acquire():
            return False
        with self._active_lock:
            self._active += 1
            self.zero.clear()
        return True

    def release(self) -> None:
        with self._active_lock:
            self._active -= 1
            assert self._active >= 0
            if self._active == 0:
                self.zero.set()
        super().release()


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


def test_password_work_gate_is_nonblocking_and_exactly_capped():
    gate = PasswordWorkGate(4)
    assert [gate.try_acquire() for _ in range(5)] == [True, True, True, True, False]
    gate.release()
    assert gate.try_acquire() is True
    for _ in range(4):
        gate.release()


def test_open_mode_login_resolves_user_off_event_loop(monkeypatch):
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    worker_threads: list[int] = []

    def resolve_user(user_id: str) -> str:
        assert user_id == "requested-user"
        worker_threads.append(threading.get_ident())
        return "local"

    monkeypatch.setattr(metadb, "resolve_user", resolve_user)

    async def scenario() -> tuple[int, dict]:
        loop_thread = threading.get_ident()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/login",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 50000),
            }
        )
        result = await workspace.auth_login(
            workspace.LoginBody(user_id="requested-user", password="unused-password"),
            Response(),
            request,
        )
        return loop_thread, result

    loop_thread, result = asyncio.run(scenario())
    assert result == {"ok": True, "userId": "local"}
    assert len(worker_threads) == 1 and worker_threads[0] != loop_thread


def test_cancelled_running_password_work_keeps_lease_until_worker_exits(monkeypatch):
    gate = _CountingGate(4)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(auth_admission, "password_work_gate", gate)
    monkeypatch.setattr(auth_admission, "password_work_executor", executor)
    release_workers = threading.Event()
    all_entered = threading.Event()
    all_finished = threading.Event()
    counts_lock = threading.Lock()
    entered = 0
    finished = 0

    def blocking_work() -> None:
        nonlocal entered, finished
        with counts_lock:
            entered += 1
            if entered == 4:
                all_entered.set()
        try:
            assert release_workers.wait(timeout=10)
        finally:
            with counts_lock:
                finished += 1
                if finished == 4:
                    all_finished.set()

    async def scenario() -> None:
        tasks = [asyncio.create_task(workspace._run_password_work(blocking_work)) for _ in range(4)]
        try:
            assert await asyncio.to_thread(all_entered.wait, 10)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]

            # The request task is gone, but its sync KDF is still running and must retain the fourth
            # lease. A fifth operation therefore rejects immediately instead of entering the executor.
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(workspace._run_password_work(lambda: None), timeout=1)
            assert exc.value.status_code == 429
        finally:
            release_workers.set()
        await asyncio.gather(*tasks[1:])
        assert await asyncio.to_thread(all_finished.wait, 10)
        await _wait_until(lambda: gate.active == 0)

    try:
        asyncio.run(scenario())
    finally:
        release_workers.set()
        executor.shutdown(wait=True, cancel_futures=True)
    assert gate.active == 0


def test_cancelled_queued_password_work_and_submit_failure_release_lease(monkeypatch):
    gate = _CountingGate(2)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(auth_admission, "password_work_gate", gate)
    monkeypatch.setattr(auth_admission, "password_work_executor", executor)
    first_entered = threading.Event()
    release_first = threading.Event()
    queued_ran = threading.Event()

    def first_work() -> None:
        first_entered.set()
        assert release_first.wait(timeout=10)

    async def scenario() -> None:
        first = asyncio.create_task(workspace._run_password_work(first_work))
        assert await asyncio.to_thread(first_entered.wait, 10)
        queued = asyncio.create_task(workspace._run_password_work(queued_ran.set))
        await _wait_until(lambda: gate.active == 2)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await _wait_until(lambda: gate.active == 1)
        assert not queued_ran.is_set()
        release_first.set()
        await first
        await _wait_until(lambda: gate.active == 0)

    try:
        asyncio.run(scenario())
    finally:
        release_first.set()
        executor.shutdown(wait=True, cancel_futures=True)
    assert gate.active == 0

    class RejectingExecutor:
        @staticmethod
        def submit(*_args, **_kwargs):
            raise RuntimeError("executor is shutting down")

    monkeypatch.setattr(auth_admission, "password_work_executor", RejectingExecutor())
    with pytest.raises(RuntimeError, match="shutting down"):
        asyncio.run(workspace._run_password_work(lambda: None))
    assert gate.active == 0


def test_attempt_limiter_consumption_is_atomic_under_concurrency():
    workers = 20
    start = threading.Barrier(workers)
    limiter = AttemptLimiter(
        capacity=3,
        refill_seconds=60,
        entry_ttl_seconds=600,
        max_entries=10,
        clock=lambda: 0.0,
    )

    def attempt(_index: int) -> bool:
        start.wait(timeout=10)
        return limiter.consume(("client", "user")).allowed

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        admitted = list(pool.map(attempt, range(workers)))
    assert sum(admitted) == 3


def test_attempt_limiter_refills_resets_and_isolates_keys():
    now = [0.0]
    limiter = AttemptLimiter(
        capacity=2,
        refill_seconds=10,
        entry_ttl_seconds=60,
        max_entries=10,
        clock=lambda: now[0],
    )
    key = ("client-a", "user-a")
    assert limiter.consume(key).allowed
    assert limiter.consume(key).allowed
    denied = limiter.consume(key)
    assert not denied.allowed and denied.retry_after == 5
    assert limiter.consume(("client-b", "user-a")).allowed
    assert limiter.consume(("client-a", "user-b")).allowed

    now[0] = 5.0
    assert limiter.consume(key).allowed
    assert not limiter.consume(key).allowed
    limiter.reset(key)
    assert limiter.consume(key).allowed
    assert limiter.consume(key).allowed


def test_attempt_limiter_memory_is_bounded_and_idle_entries_expire():
    now = [0.0]
    limiter = AttemptLimiter(
        capacity=1,
        refill_seconds=60,
        entry_ttl_seconds=10,
        max_entries=3,
        clock=lambda: now[0],
    )
    for index in range(10):
        decision = limiter.consume(("client", f"user-{index}"))
        assert decision.allowed
        assert len(limiter) <= 3
    assert len(limiter) == 3

    now[0] = 11.0
    assert limiter.consume(("client", "fresh-user")).allowed
    assert len(limiter) == 1


def test_saturated_login_gate_rejects_before_starting_another_worker(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "login-gate-test-secret")
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter(capacity=5))
    monkeypatch.setattr(auth_admission, "login_attempts", _limiter())
    entered = 0
    entered_lock = threading.Lock()
    four_entered = threading.Event()
    release = threading.Event()

    def blocking_login(_user_id: str, _password: str) -> None:
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == 4:
                four_entered.set()
        assert release.wait(timeout=10)
        return None

    monkeypatch.setattr(workspace, "_login_password_work", blocking_login)
    clients = [TestClient(app) for _ in range(5)]

    def login(index: int):
        return clients[index].post(
            "/api/auth/login",
            json={"userId": "same-user", "password": "valid-shape"},
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            pending = [pool.submit(login, index) for index in range(4)]
            try:
                assert four_entered.wait(timeout=10)
                rejected = pool.submit(login, 4).result(timeout=2)
                assert rejected.status_code == 429
                assert rejected.headers["Retry-After"] == "1"
                assert rejected.json()["detail"] == "password service is busy"
                with entered_lock:
                    assert entered == 4
            finally:
                release.set()
            assert [future.result(timeout=10).status_code for future in pending] == [401] * 4
            # The gate-busy fifth request still consumed aggregate peer admission. Once workers drain,
            # a sixth request is peer-limited rather than starting fresh password work.
            peer_limited = login(4)
            assert peer_limited.status_code == 429
            assert peer_limited.headers["Retry-After"] == "12"
    finally:
        release.set()
        for request_client in clients:
            request_client.close()


def test_login_change_and_admin_create_all_reject_when_password_gate_is_full(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "shared-gate-test-secret")
    uid = "auth_gate_all_routes"
    _put_user(uid, "old-password", admin=True, epoch=7)
    token = auth.sign(uid)
    busy_gate = PasswordWorkGate(1)
    assert busy_gate.try_acquire()
    monkeypatch.setattr(auth_admission, "password_work_gate", busy_gate)
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "login_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "password_change_attempts", _limiter())
    request_client = TestClient(app)
    cookie = {"Cookie": f"dp_session={token}"}
    try:
        responses = [
            request_client.post(
                "/api/auth/login",
                json={"userId": uid, "password": "old-password"},
            ),
            request_client.post(
                "/api/auth/password",
                json={"oldPassword": "old-password", "newPassword": "new-password"},
                headers=cookie,
            ),
            request_client.post(
                "/api/users",
                json={"name": "Gate child", "password": "child-password"},
                headers=cookie,
            ),
        ]
    finally:
        busy_gate.release()
        request_client.close()
    assert [response.status_code for response in responses] == [429, 429, 429]
    assert [response.headers["Retry-After"] for response in responses] == ["1", "1", "1"]


def test_password_change_keeps_gate_through_verify_hash_and_cas(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "change-span-test-secret")
    uid = "auth_change_gate_span"
    _put_user(uid, "old-password", epoch=3)
    token = auth.sign(uid)

    class RecordingGate:
        held = False

        def try_acquire(self) -> bool:
            assert not self.held
            self.held = True
            return True

        def release(self) -> None:
            assert self.held
            self.held = False

    gate = RecordingGate()
    monkeypatch.setattr(auth_admission, "password_work_gate", gate)
    monkeypatch.setattr(auth_admission, "password_change_attempts", _limiter())
    real_verify = auth.verify_password
    real_hash = auth.hash_password
    real_cas = metadb.compare_and_set_user_password
    observed: list[str] = []

    def verify_while_held(password: str, stored: str | None) -> bool:
        assert gate.held
        observed.append("verify")
        return real_verify(password, stored)

    def hash_while_held(password: str) -> str:
        assert gate.held
        observed.append("hash")
        return real_hash(password)

    def cas_while_held(user_id: str, expected_hash: str | None, expected_epoch: int, new_hash: str):
        assert gate.held
        observed.append("cas")
        return real_cas(user_id, expected_hash, expected_epoch, new_hash)

    monkeypatch.setattr(auth, "verify_password", verify_while_held)
    monkeypatch.setattr(auth, "hash_password", hash_while_held)
    monkeypatch.setattr(metadb, "compare_and_set_user_password", cas_while_held)
    with TestClient(app) as request_client:
        response = request_client.post(
            "/api/auth/password",
            json={"oldPassword": "old-password", "newPassword": "new-password"},
            headers={"Cookie": f"dp_session={token}"},
        )
    assert response.status_code == 200
    assert observed == ["verify", "hash", "cas"]
    assert not gate.held


def test_admin_create_hashes_initial_password_while_gate_is_held(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "create-gate-span-test-secret")
    admin_id = "auth_create_gate_admin"
    _put_user(admin_id, "admin-password", admin=True, epoch=4)
    token = auth.sign(admin_id)

    class RecordingGate:
        held = False

        def try_acquire(self) -> bool:
            assert not self.held
            self.held = True
            return True

        def release(self) -> None:
            assert self.held
            self.held = False

    gate = RecordingGate()
    monkeypatch.setattr(auth_admission, "password_work_gate", gate)
    real_hash = auth.hash_password
    real_verify = auth.verify_password
    hashed_while_held = False

    def hash_while_held(password: str) -> str:
        nonlocal hashed_while_held
        assert gate.held
        hashed_while_held = True
        return real_hash(password)

    monkeypatch.setattr(auth, "hash_password", hash_while_held)
    with TestClient(app) as request_client:
        response = request_client.post(
            "/api/users",
            json={"name": "Created with password", "password": "child-password"},
            headers={"Cookie": f"dp_session={token}"},
        )
    assert response.status_code == 200
    assert hashed_while_held and not gate.held
    with metadb.session() as session:
        created = session.get(metadb.User, response.json()["id"])
        assert created is not None
        assert real_verify("child-password", created.password_hash)


def test_login_attempt_limit_does_not_parse_raw_forwarded_for_and_success_resets(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "login-rate-test-secret")
    uid = "auth_login_attempt_limit"
    _put_user(uid, "right-password")
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter(capacity=4))
    monkeypatch.setattr(auth_admission, "login_attempts", _limiter(capacity=2))
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))
    real_verify = auth.verify_password
    verify_calls = 0

    def counted_verify(password: str, stored: str | None) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return real_verify(password, stored)

    monkeypatch.setattr(auth, "verify_password", counted_verify)
    # TestClient does not run Uvicorn's trusted-proxy middleware. Varying the raw header here proves the
    # route itself never parses it; a production ASGI server may already have normalized request.client.
    with TestClient(app) as request_client:
        assert request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong-1"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        ).status_code == 401
        assert request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "right-password"},
            headers={"X-Forwarded-For": "198.51.100.2"},
        ).status_code == 200
        request_client.cookies.clear()
        assert request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong-2"},
            headers={"X-Forwarded-For": "198.51.100.3"},
        ).status_code == 401
        assert request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong-3"},
            headers={"X-Forwarded-For": "198.51.100.4"},
        ).status_code == 401
        limited = request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong-4"},
            headers={"X-Forwarded-For": "198.51.100.5"},
        )
    assert limited.status_code == 429
    # Pair success reset is proven by wrong-2 being admitted; the fifth request is nevertheless denied
    # by the aggregate peer bucket, which is intentionally never reset by successful authentication.
    assert limited.headers["Retry-After"] == "15"
    assert verify_calls == 4


def test_login_peer_quota_prevents_random_user_spray_from_resetting_target(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "login-cardinality-test-secret")
    now = [0.0]
    peer_limiter = _limiter(capacity=3, refill_seconds=3, max_entries=3, clock=lambda: now[0])
    pair_limiter = _limiter(capacity=1, refill_seconds=1000, max_entries=3, clock=lambda: now[0])
    monkeypatch.setattr(auth_admission, "login_peer_attempts", peer_limiter)
    monkeypatch.setattr(auth_admission, "login_attempts", pair_limiter)
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))
    work_calls = 0

    def rejected_login(_user_id: str, _password: str) -> None:
        nonlocal work_calls
        work_calls += 1
        return None

    monkeypatch.setattr(workspace, "_login_password_work", rejected_login)

    def login(request_client: TestClient, user_id: str):
        return request_client.post(
            "/api/auth/login",
            json={"userId": user_id, "password": "wrong-password"},
        )

    with (
        TestClient(app, client=("peer-a", 50000)) as peer_a,
        TestClient(app, client=("peer-b", 50000)) as peer_b,
    ):
        assert [login(peer_a, user_id).status_code for user_id in ("target", "random-1", "random-2")] == [
            401,
            401,
            401,
        ]
        sprayed = login(peer_a, "random-3")
        assert sprayed.status_code == 429 and sprayed.headers["Retry-After"] == "1"
        assert len(pair_limiter) == 3

        now[0] = 1.0  # one peer token refills; the target pair has only 0.001 token
        target = login(peer_a, "target")
        assert target.status_code == 429
        assert target.headers["Retry-After"] == "999", "target bucket was evicted/reset by random IDs"

        # A legitimate new key from another real peer is admitted by bounded LRU eviction rather than
        # suffering the former ten-minute global cardinality lockout.
        assert login(peer_b, "legitimate-new-user").status_code == 401
        assert len(pair_limiter) == 3
    assert work_calls == 4  # three initial attempts + the other peer; both limited requests skipped work


def test_authenticated_password_change_attempts_are_rate_limited(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "change-rate-test-secret")
    uid = "auth_change_attempt_limit"
    old_hash = _put_user(uid, "right-password", epoch=9)
    token = auth.sign(uid)
    monkeypatch.setattr(auth_admission, "password_change_attempts", _limiter(capacity=2))
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))
    with TestClient(app) as request_client:
        statuses = [
            request_client.post(
                "/api/auth/password",
                json={"oldPassword": f"wrong-{index}", "newPassword": "new-password"},
                headers={
                    "Cookie": f"dp_session={token}",
                    "X-Forwarded-For": f"203.0.113.{index}",
                },
            )
            for index in range(3)
        ]
    assert [response.status_code for response in statuses] == [403, 403, 429]
    assert statuses[-1].headers["Retry-After"] == "30"
    with metadb.session() as session:
        assert session.get(metadb.User, uid).password_hash == old_hash


def test_password_kdf_rejects_oversized_or_invalid_values_before_scrypt(monkeypatch):
    stored = auth.hash_password("stored-password")
    oversized_ascii = "x" * (auth.MAX_PASSWORD_BYTES + 1)
    oversized_multibyte = "é" * (auth.MAX_PASSWORD_BYTES // 2 + 1)
    exact_boundary = "é" * (auth.MAX_PASSWORD_BYTES // 2)
    assert len(auth.password_bytes_for_kdf(exact_boundary)) == auth.MAX_PASSWORD_BYTES
    assert len(oversized_multibyte) < auth.MAX_PASSWORD_BYTES

    class EncodeMustNotRun(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("obviously oversized password was encoded")

    with pytest.raises(ValueError, match="at most 1024 UTF-8 bytes"):
        auth.password_bytes_for_kdf(EncodeMustNotRun("x" * (auth.MAX_PASSWORD_BYTES + 1)))

    calls = 0

    def unexpected_scrypt(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("oversized password reached scrypt")

    monkeypatch.setattr(auth.hashlib, "scrypt", unexpected_scrypt)
    for password in (oversized_ascii, oversized_multibyte):
        with pytest.raises(ValueError, match="at most 1024 UTF-8 bytes"):
            auth.hash_password(password)
        assert auth.verify_password(password, stored) is False
    with pytest.raises(ValueError, match="valid UTF-8"):
        auth.hash_password("\ud800")
    assert auth.verify_password("\ud800", stored) is False
    assert auth.verify_password(123, stored) is False  # type: ignore[arg-type]
    assert calls == 0


def test_auth_request_bodies_are_strict_and_bound_password_bytes(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "strict-auth-body-test-secret")
    uid = "auth_strict_request_bodies"
    _put_user(uid, "right-password", admin=True, epoch=2)
    token = auth.sign(uid)
    cookie = {"Cookie": f"dp_session={token}"}
    oversized = "é" * (auth.MAX_PASSWORD_BYTES // 2 + 1)
    long_user_id = "u" * (workspace.MAX_AUTH_USER_ID_BYTES + 1)
    long_multibyte_user_id = "é" * (workspace.MAX_AUTH_USER_ID_BYTES // 2 + 1)
    long_profile = "p" * (workspace.MAX_AUTH_USER_PROFILE_FIELD_BYTES + 1)
    long_multibyte_profile = "é" * (workspace.MAX_AUTH_USER_PROFILE_FIELD_BYTES // 2 + 1)
    safe_error = {"detail": "invalid authentication request body"}

    class UserIdEncodeMustNotRun(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("obviously oversized user id was encoded")

    with pytest.raises(ValueError, match="at most 128 UTF-8 bytes"):
        workspace._bounded_user_id(UserIdEncodeMustNotRun(long_user_id))

    class ProfileEncodeMustNotRun(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("obviously oversized profile field was encoded")

    with pytest.raises(ValueError, match="at most 1024 UTF-8 bytes"):
        workspace._bounded_database_text(
            ProfileEncodeMustNotRun(long_profile),
            label="user profile field",
            max_bytes=workspace.MAX_AUTH_USER_PROFILE_FIELD_BYTES,
        )

    real_scrypt = auth.hashlib.scrypt
    real_peer_key = auth_admission.login_peer_attempt_key
    real_pair_key = auth_admission.password_attempt_key
    kdf_calls = 0
    key_calls = 0

    def unexpected_scrypt(*_args, **_kwargs):
        nonlocal kdf_calls
        kdf_calls += 1
        raise AssertionError("invalid auth input reached scrypt")

    def unexpected_peer_key(*_args, **_kwargs):
        nonlocal key_calls
        key_calls += 1
        raise AssertionError("invalid auth input reached peer-key hashing")

    def unexpected_pair_key(*_args, **_kwargs):
        nonlocal key_calls
        key_calls += 1
        raise AssertionError("invalid auth input reached pair-key hashing")

    monkeypatch.setattr(auth.hashlib, "scrypt", unexpected_scrypt)
    monkeypatch.setattr(auth_admission, "login_peer_attempt_key", unexpected_peer_key)
    monkeypatch.setattr(auth_admission, "password_attempt_key", unexpected_pair_key)

    with TestClient(app) as request_client:
        cases = [
            ("/api/auth/login", {"userId": uid, "password": oversized}, {}),
            ("/api/auth/login", {"userId": uid, "password": 123}, {}),
            ("/api/auth/login", {"userId": 123, "password": "right-password"}, {}),
            ("/api/auth/login", {"userId": uid, "password": "right-password", "extra": True}, {}),
            ("/api/auth/login", {"userId": long_user_id, "password": "right-password"}, {}),
            ("/api/auth/login", {"userId": long_multibyte_user_id, "password": "right-password"}, {}),
            ("/api/auth/password", {"oldPassword": oversized, "newPassword": "new-password"}, cookie),
            ("/api/auth/password", {"oldPassword": "right-password", "newPassword": oversized}, cookie),
            ("/api/auth/password", {"oldPassword": 123, "newPassword": "new-password"}, cookie),
            ("/api/users", {"name": "Oversized", "password": oversized}, cookie),
            ("/api/users", {"name": "Wrong type", "password": 123}, cookie),
            ("/api/users", {"name": long_profile}, cookie),
            ("/api/users", {"name": long_multibyte_profile}, cookie),
            ("/api/users", {"name": "Long email", "email": long_profile}, cookie),
            ("/api/users", {"name": "Long email", "email": long_multibyte_profile}, cookie),
        ]
        for path, body, headers in cases:
            response = request_client.post(path, json=body, headers=headers)
            assert response.status_code == 422, (path, body, response.text)
            assert response.json() == safe_error
            assert "right-password" not in response.text and oversized not in response.text

        raw_invalid_text = [
            ("/api/auth/login", b'{"userId":"auth_strict_request_bodies","password":"\\ud800"}', {}),
            ("/api/auth/login", b'{"userId":"\\ud800","password":"right-password"}', {}),
            ("/api/auth/login", b'{"userId":"a\\u0000b","password":"right-password"}', {}),
            ("/api/users", b'{"name":"\\ud800"}', cookie),
            ("/api/users", b'{"name":"ok","email":"\\ud800"}', cookie),
            ("/api/users", b'{"name":"a\\u0000b"}', cookie),
            ("/api/users", b'{"name":"ok","email":"a\\u0000b"}', cookie),
        ]
        for path, raw_body, headers in raw_invalid_text:
            response = request_client.post(
                path,
                content=raw_body,
                headers={"Content-Type": "application/json", **headers},
            )
            assert response.status_code == 422
            assert response.json() == safe_error
            assert "surrogate" not in response.text and "right-password" not in response.text

        assert kdf_calls == key_calls == 0

        # Restore real work/key functions before proving both exact multibyte boundaries are accepted.
        monkeypatch.setattr(auth.hashlib, "scrypt", real_scrypt)
        monkeypatch.setattr(auth_admission, "login_peer_attempt_key", real_peer_key)
        monkeypatch.setattr(auth_admission, "password_attempt_key", real_pair_key)
        exact_password = "é" * (auth.MAX_PASSWORD_BYTES // 2)
        exact_user_id = "é" * (workspace.MAX_AUTH_USER_ID_BYTES // 2)
        exact_profile = "é" * (workspace.MAX_AUTH_USER_PROFILE_FIELD_BYTES // 2)
        assert request_client.post(
            "/api/auth/login",
            json={"userId": uid, "password": exact_password},
        ).status_code == 401
        assert request_client.post(
            "/api/auth/login",
            json={"userId": exact_user_id, "password": "right-password"},
        ).status_code == 401
        profile_boundary = request_client.post(
            "/api/users",
            json={"name": exact_profile, "email": exact_profile},
            headers=cookie,
        )
        assert profile_boundary.status_code == 200

        short_change = request_client.post(
            "/api/auth/password",
            json={"oldPassword": "right-password", "newPassword": "short"},
            headers=cookie,
        )
        short_create = request_client.post(
            "/api/users",
            json={"name": "Short password", "password": "short"},
            headers=cookie,
        )
    assert short_change.status_code == 400
    assert short_create.status_code == 400
    assert short_change.json()["detail"] == "password must be at least 6 characters"
    assert short_create.json()["detail"] == "password must be at least 6 characters"
