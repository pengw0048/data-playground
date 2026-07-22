"""Shared-mode transport guard: Secure cookies and trusted TLS proxies (SEC-04 / #108)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from hub import auth, auth_admission, metadb
from hub.auth_admission import AttemptLimiter, PasswordWorkGate
from hub.main import app
from hub.routers import workspace


def _kernel_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _repo_root() -> str:
    return os.path.dirname(_kernel_root())


def _put_user(user_id: str, password: str) -> None:
    password_hash = auth.hash_password(password)
    with metadb.session() as session:
        existing = session.get(metadb.User, user_id)
        if existing is None:
            session.add(
                metadb.User(
                    id=user_id,
                    name=user_id,
                    password_hash=password_hash,
                    is_admin=False,
                    token_epoch=0,
                )
            )
        else:
            existing.password_hash = password_hash
            existing.token_epoch = 0


def _limiter(*, capacity: int = 100) -> AttemptLimiter:
    return AttemptLimiter(
        capacity=capacity,
        refill_seconds=60,
        entry_ttl_seconds=600,
        max_entries=128,
    )


def _set_cookie_header(response) -> str:
    # Starlette may expose set-cookie as a single header or a multi-value list depending on version.
    raw = response.headers.get("set-cookie")
    if raw:
        return raw
    getlist = getattr(response.headers, "getlist", None)
    if callable(getlist):
        values = getlist("set-cookie")
        if values:
            return values[0]
    return ""


def test_local_mode_is_default_and_allows_insecure_transport(monkeypatch):
    monkeypatch.delenv("DP_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("DP_AUTH_SECURE_COOKIE", raising=False)
    monkeypatch.delenv("DP_TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)

    assert auth.deployment_mode() == "local"
    assert auth.secure_cookie_enabled() is False
    auth.reject_unsafe_transport()


def test_shared_mode_rejects_missing_secure_cookie(monkeypatch):
    monkeypatch.setenv("DP_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("DP_AUTH_SECRET", "shared-mode-transport-test-secret-0123456789")
    monkeypatch.delenv("DP_AUTH_SECURE_COOKIE", raising=False)
    monkeypatch.setenv("DP_TRUSTED_PROXIES", "10.0.0.1")

    with pytest.raises(RuntimeError, match="DP_AUTH_SECURE_COOKIE=1"):
        auth.reject_unsafe_transport()


def test_shared_mode_rejects_missing_proxy_even_if_direct_tls_is_declared(monkeypatch):
    monkeypatch.setenv("DP_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("DP_AUTH_SECRET", "shared-mode-transport-test-secret-0123456789")
    monkeypatch.setenv("DP_AUTH_SECURE_COOKIE", "1")
    monkeypatch.setenv("DP_AUTH_DIRECT_TLS", "1")
    monkeypatch.delenv("DP_TRUSTED_PROXIES", raising=False)

    with pytest.raises(RuntimeError, match="DP_TRUSTED_PROXIES=<proxy-ip>"):
        auth.reject_unsafe_transport()


def test_shared_mode_rejects_wildcard_trusted_proxies(monkeypatch):
    monkeypatch.setenv("DP_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("DP_AUTH_SECRET", "shared-mode-transport-test-secret-0123456789")
    monkeypatch.setenv("DP_AUTH_SECURE_COOKIE", "1")
    monkeypatch.setenv("DP_TRUSTED_PROXIES", "*")

    with pytest.raises(RuntimeError, match=r"DP_TRUSTED_PROXIES=\*"):
        auth.reject_unsafe_transport()


def test_shared_mode_accepts_a_declared_tls_terminating_proxy(monkeypatch):
    monkeypatch.setenv("DP_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("DP_AUTH_SECRET", "shared-mode-transport-test-secret-0123456789")
    monkeypatch.setenv("DP_AUTH_SECURE_COOKIE", "1")

    monkeypatch.setenv("DP_TRUSTED_PROXIES", "10.0.0.1")
    auth.reject_unsafe_transport()


def test_hub_startup_refuses_shared_mode_without_secure_cookie(tmp_path):
    """Importing hub.main is hub startup — refuse before the process can serve requests."""
    env = dict(os.environ)
    env["DP_DEPLOYMENT_MODE"] = "shared"
    env["DP_AUTH_SECRET"] = "shared-mode-transport-test-secret-0123456789"
    env["DP_TRUSTED_PROXIES"] = "10.0.0.1"
    env.pop("DP_AUTH_SECURE_COOKIE", None)
    env["DP_WORKSPACE"] = str(tmp_path)
    env["DP_DATABASE_URL"] = "sqlite:///" + str(tmp_path / "refuse.db")
    result = subprocess.run(
        [sys.executable, "-c", "import hub.main"],
        cwd=_kernel_root(),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "DP_AUTH_SECURE_COOKIE=1" in result.stderr


def test_cli_refuses_shared_mode_before_binding(tmp_path):
    workspace = tmp_path / "workspace"
    env = dict(os.environ)
    env["DP_DEPLOYMENT_MODE"] = "shared"
    env["DP_AUTH_SECRET"] = "shared-mode-transport-test-secret-0123456789"
    env["DP_TRUSTED_PROXIES"] = "10.0.0.1"
    env.pop("DP_AUTH_SECURE_COOKIE", None)
    env.pop("DP_DATABASE_URL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hub.cli",
            "--workspace",
            str(workspace),
            "--no-open",
            "--no-seed",
        ],
        cwd=_kernel_root(),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "DP_AUTH_SECURE_COOKIE=1" in result.stderr
    assert not (workspace / "dataplay.db").exists()


def test_local_mode_login_cookie_has_no_secure_flag(monkeypatch):
    """Regression: unset deployment mode keeps Secure off for localhost HTTP."""
    monkeypatch.delenv("DP_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("DP_AUTH_SECURE_COOKIE", raising=False)
    monkeypatch.setenv("DP_AUTH_SECRET", "local-mode-cookie-regression-secret-0123456789")
    uid = "local_cookie_regression"
    _put_user(uid, "password1")
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "login_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "password1"},
        )
    assert response.status_code == 200
    set_cookie = _set_cookie_header(response)
    assert "dp_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()


def test_shared_proxy_issues_secure_cookie_on_login_and_password_change(monkeypatch):
    monkeypatch.setenv("DP_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("DP_AUTH_SECRET", "shared-proxy-cookie-secret-0123456789")
    monkeypatch.setenv("DP_AUTH_SECURE_COOKIE", "1")
    monkeypatch.setenv("DP_TRUSTED_PROXIES", "10.0.0.1")
    uid = "shared_secure_cookie"
    _put_user(uid, "password1")
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "login_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "password_change_attempts", _limiter())
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"userId": uid, "password": "password1"},
        )
        assert login.status_code == 200
        login_cookie = _set_cookie_header(login)
        assert "Secure" in login_cookie
        assert "HttpOnly" in login_cookie

        rotated = client.post(
            "/api/auth/password",
            json={"oldPassword": "password1", "newPassword": "password2"},
            headers={"Cookie": f"dp_session={login.cookies.get('dp_session')}"},
        )
        assert rotated.status_code == 200
        assert "Secure" in _set_cookie_header(rotated)


def test_compose_reference_is_authenticated_local_http():
    """Keep the checked-in reference runnable at its loopback HTTP URL."""
    compose = open(os.path.join(_repo_root(), "docker-compose.yml"), encoding="utf-8").read()
    kernel = compose.split("\n  kernel:\n", 1)[1].split("\n  postgres:\n", 1)[0]

    assert '"127.0.0.1:8471:8471"' in kernel
    assert "DP_AUTH_SECRET:" in kernel
    assert "DP_DEPLOYMENT_MODE:" not in kernel
    assert "DP_AUTH_SECURE_COOKIE:" not in kernel
    assert "DP_AUTH_DIRECT_TLS:" not in kernel


def test_trusted_proxy_headers_affect_login_rate_limit_only_from_declared_peer(monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "trusted-proxy-rate-limit-secret-0123456789")
    monkeypatch.setenv("DP_TRUSTED_PROXIES", "10.0.0.1")
    uid = "trusted_proxy_rate"
    _put_user(uid, "right-password")
    # Peer capacity 1: a repeated client address is denied on the second miss; a different address still
    # gets through. That distinguishes forwarded-client keys (trusted proxy) from ASGI-peer keys.
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter(capacity=1))
    monkeypatch.setattr(auth_admission, "login_attempts", _limiter(capacity=100))
    monkeypatch.setattr(auth_admission, "password_work_gate", PasswordWorkGate(4))
    work_calls = 0
    real_login = workspace._login_password_work

    def counted_login(user_id: str, password: str):
        nonlocal work_calls
        work_calls += 1
        return real_login(user_id, password)

    monkeypatch.setattr(workspace, "_login_password_work", counted_login)

    # Declared proxy: X-Forwarded-For becomes the rate-limit peer key.
    with TestClient(app, client=("10.0.0.1", 50000)) as from_proxy:
        assert from_proxy.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.10"},
        ).status_code == 401
        assert from_proxy.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.10"},
        ).status_code == 429
        # A different forwarded client still has peer quota — proof the key is not the proxy IP.
        assert from_proxy.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.11"},
        ).status_code == 401
        assert work_calls == 2

    # Undeclared peer: identical X-Forwarded-For values are ignored; both consume the peer address bucket.
    work_calls = 0
    monkeypatch.setattr(auth_admission, "login_peer_attempts", _limiter(capacity=1))
    with TestClient(app, client=("203.0.113.50", 50000)) as undeclared:
        assert undeclared.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.10"},
        ).status_code == 401
        assert undeclared.post(
            "/api/auth/login",
            json={"userId": uid, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.11"},
        ).status_code == 429
        assert work_calls == 1
