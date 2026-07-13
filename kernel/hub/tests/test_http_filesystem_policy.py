"""Real-I/O coverage for the authenticated DuckDB network boundary."""

from __future__ import annotations

import http.server
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from hub import db


def _load_httpfs(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")


def _write_parquet(path: Path) -> None:
    con = duckdb.connect()
    try:
        con.execute("COPY (SELECT 'internal-only' AS value) TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        con.close()


@contextmanager
def _serve_file(path: Path):
    payload = path.read_bytes()
    requests: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self, *, include_body: bool) -> None:
            requests.append((self.command, self.path))
            if self.path != "/dataset.parquet":
                self.send_error(404)
                return

            start, end = 0, len(payload) - 1
            status = 200
            value = self.headers.get("Range")
            if value:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", value)
                if not match:
                    self.send_error(416)
                    return
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
                end = min(end, len(payload) - 1)
                if start > end:
                    self.send_error(416)
                    return
                status = 206

            body = payload[start:end + 1]
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve(include_body=False)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve(include_body=True)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/dataset.parquet", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _set_auth_marker(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    if marker == "secret":
        monkeypatch.setenv("DP_AUTH_SECRET", "x" * 40)
    else:
        monkeypatch.setenv("DP_AUTH_MODE", "1")


def test_open_mode_keeps_direct_http_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    parquet = tmp_path / "dataset.parquet"
    _write_parquet(parquet)

    con = duckdb.connect()
    try:
        db._apply_session(con)
        _load_httpfs(con)
        with _serve_file(parquet) as (url, requests):
            assert con.read_parquet(url).fetchall() == [("internal-only",)]
            assert requests
    finally:
        con.close()


@pytest.mark.parametrize("marker", ["secret", "mode"])
@pytest.mark.parametrize("apply_order", ["before_load", "after_load"])
def test_auth_mode_blocks_direct_http_before_and_after_httpfs_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
    apply_order: str,
) -> None:
    _set_auth_marker(monkeypatch, marker)
    parquet = tmp_path / "dataset.parquet"
    _write_parquet(parquet)

    con = duckdb.connect()
    try:
        if apply_order == "before_load":
            db._apply_session(con)
            _load_httpfs(con)
        else:
            _load_httpfs(con)
            db._apply_session(con)

        with _serve_file(parquet) as (url, requests):
            with pytest.raises(duckdb.PermissionException, match="HTTPFileSystem.*disabled"):
                con.read_parquet(url).fetchall()
            assert requests == []
    finally:
        con.close()


def test_auth_http_block_keeps_s3_filesystem_available(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("flask")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    _set_auth_marker(monkeypatch, "mode")
    server = ThreadedMotoServer(port=0)
    server.start()
    con = duckdb.connect()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id="key",
            aws_secret_access_key="secret",
            region_name="us-east-1",
        ).create_bucket(Bucket="policy-test")

        db._apply_session(con)
        _load_httpfs(con)
        con.execute(
            """
            CREATE SECRET policy_s3 (
                TYPE s3,
                KEY_ID 'key',
                SECRET 'secret',
                REGION 'us-east-1',
                ENDPOINT ?,
                URL_STYLE 'path',
                USE_SSL false
            )
            """,
            [f"{host}:{port}"],
        )
        uri = "s3://policy-test/dataset.parquet"
        con.execute(f"COPY (VALUES (1), (2)) TO '{uri}' (FORMAT PARQUET)")
        assert con.read_parquet(uri).fetchall() == [(1,), (2,)]
    finally:
        con.close()
        server.stop()


@pytest.mark.parametrize("marker", ["secret", "mode"])
def test_auth_mode_blocks_huggingface_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    """httpfs also registers hf:// independently of HTTPFileSystem; source URIs must not bypass policy."""
    _set_auth_marker(monkeypatch, marker)
    con = duckdb.connect()
    try:
        db._apply_session(con)
        _load_httpfs(con)
        with pytest.raises(duckdb.PermissionException, match="HuggingFaceFileSystem.*disabled"):
            con.read_csv(
                "hf://datasets/datasets-examples/doc-formats-csv-1/data.csv"
            ).fetchall()
    finally:
        con.close()
