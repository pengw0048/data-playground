#!/usr/bin/env python3
"""Headless offline starter-canvas smoke against a running Data Playground hub.

Drives the "Purchases per user" starter from web/src/examples.ts over the seeded
``events`` dataset via the HTTP API: create canvas → run → assert output.

Local usage (hub already serving on :8471 with seeded sample data)::

    python3 scripts/release_smoke.py --base-url http://127.0.0.1:8471

Expect a version identity (optional)::

    python3 scripts/release_smoke.py --base-url http://127.0.0.1:8471 \\
        --expect-version 0.1.0 --expect-sha-not-unknown
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


# Mirror of web/src/examples.ts "purchases" starter (events → filter → aggregate → sort → write).
PURCHASES_GRAPH: dict[str, Any] = {
    "id": "cv-release-smoke",
    "name": "Purchases per user",
    "version": 1,
    "nodes": [
        {
            "id": "src",
            "type": "source",
            "position": {"x": 80, "y": 180},
            "data": {"title": "events", "config": {"uri": "events"}},
        },
        {
            "id": "flt",
            "type": "filter",
            "position": {"x": 360, "y": 180},
            "data": {"title": "filter", "config": {"predicate": "event = 'purchase'"}},
        },
        {
            "id": "agg",
            "type": "aggregate",
            "position": {"x": 640, "y": 180},
            "data": {
                "title": "aggregate",
                "config": {
                    "groupBy": "user_id",
                    "aggs": "sum(amount) AS total, count(*) AS n",
                },
            },
        },
        {
            "id": "srt",
            "type": "sort",
            "position": {"x": 920, "y": 180},
            "data": {"title": "sort", "config": {"by": "total DESC"}},
        },
        {
            "id": "out",
            "type": "write",
            "position": {"x": 1200, "y": 180},
            "data": {"title": "top_users", "config": {"name": "top_users"}},
        },
    ],
    "edges": [
        {
            "id": "e_src_flt",
            "source": "src",
            "target": "flt",
            "sourceHandle": None,
            "targetHandle": None,
            "data": {"wire": "dataset"},
        },
        {
            "id": "e_flt_agg",
            "source": "flt",
            "target": "agg",
            "sourceHandle": None,
            "targetHandle": None,
            "data": {"wire": "dataset"},
        },
        {
            "id": "e_agg_srt",
            "source": "agg",
            "target": "srt",
            "sourceHandle": None,
            "targetHandle": None,
            "data": {"wire": "dataset"},
        },
        {
            "id": "e_srt_out",
            "source": "srt",
            "target": "out",
            "sourceHandle": None,
            "targetHandle": None,
            "data": {"wire": "dataset"},
        },
    ],
}

# Seeded events: 2000 rows, purchase every 4th row → 500 purchases over ~200 user_ids.
EXPECTED_MIN_ROWS = 1
EXPECTED_MAX_ROWS = 200


class SmokeError(RuntimeError):
    pass


def _request(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        raise SmokeError(f"{method} {path} → HTTP {exc.code}: {payload}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise SmokeError(f"{method} {path} failed: {exc}") from exc


def wait_ready(base: str, timeout_s: float = 120.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            status, body = _request(base, "GET", "/api/readyz", timeout=5.0)
            if status == 200 and isinstance(body, dict) and body.get("ready") is True:
                return
            last_err = SmokeError(f"/api/readyz not ready: {body}")
        except SmokeError as exc:
            last_err = exc
        time.sleep(0.5)
    raise SmokeError(f"hub not ready within {timeout_s}s: {last_err}")


def check_version(
    base: str,
    *,
    expect_version: str | None,
    expect_sha_not_unknown: bool,
) -> dict[str, Any]:
    _, body = _request(base, "GET", "/api/version")
    if not isinstance(body, dict):
        raise SmokeError(f"/api/version returned non-object: {body!r}")
    if "version" not in body:
        raise SmokeError("/api/version missing package 'version' field")
    if expect_version is not None and body["version"] != expect_version:
        raise SmokeError(
            f"/api/version version={body['version']!r} != expected {expect_version!r}"
        )
    sha = body.get("sha")
    if expect_sha_not_unknown and (not sha or sha == "unknown"):
        raise SmokeError(f"/api/version sha must be a real build SHA, got {sha!r}")
    return body


def run_purchases_smoke(base: str) -> dict[str, Any]:
    _request(base, "POST", "/api/canvas", PURCHASES_GRAPH)
    # Target the aggregate node so we assert countable rows without depending on write-side
    # catalog naming; the graph is still the full purchases starter.
    _, run_body = _request(
        base,
        "POST",
        "/api/run",
        {"graph": PURCHASES_GRAPH, "targetNodeId": "agg", "confirmed": True},
        timeout=180.0,
    )
    if not isinstance(run_body, dict) or not run_body.get("runId"):
        raise SmokeError(f"POST /api/run returned unexpected body: {run_body!r}")
    run_id = run_body["runId"]

    deadline = time.time() + 180.0
    status_body: dict[str, Any] = {}
    while time.time() < deadline:
        _, status_body = _request(base, "GET", f"/api/run/{run_id}")
        if not isinstance(status_body, dict):
            raise SmokeError(f"GET /api/run/{run_id} returned {status_body!r}")
        if status_body.get("status") in ("done", "failed"):
            break
        time.sleep(0.25)
    else:
        raise SmokeError(f"run {run_id} did not finish: {status_body}")

    if status_body.get("status") != "done":
        raise SmokeError(f"run {run_id} failed: {status_body}")

    total = status_body.get("totalRows")
    if not isinstance(total, int) or not (EXPECTED_MIN_ROWS <= total <= EXPECTED_MAX_ROWS):
        raise SmokeError(
            f"aggregate row count {total!r} outside expected "
            f"[{EXPECTED_MIN_ROWS}, {EXPECTED_MAX_ROWS}] "
            f"(seeded purchases-per-user should yield one row per user_id)"
        )

    outputs = status_body.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 1:
        raise SmokeError(f"run {run_id} done without exactly one named output: {status_body}")
    output = outputs[0]
    if not isinstance(output, dict):
        raise SmokeError(f"run {run_id} returned an invalid named output: {output!r}")
    if (output.get("nodeId"), output.get("portId"), output.get("publicationKind"),
            output.get("outcome")) != ("agg", "out", "result", "committed"):
        raise SmokeError(f"run {run_id} returned an unexpected named-output contract: {output}")
    uri = output.get("uri")
    if not isinstance(uri, str) or not uri:
        raise SmokeError(f"run {run_id} committed output has no URI: {output}")
    if output.get("rows") != total:
        raise SmokeError(
            f"run output rows {output.get('rows')!r} != run totalRows {total}"
        )
    _, sample = _request(base, "POST", "/api/data/sample", {"uri": uri, "k": 50})
    if not isinstance(sample, dict):
        raise SmokeError(f"/api/data/sample returned {sample!r}")
    cols = {c.get("name") for c in sample.get("columns") or []}
    expected_cols = {"user_id", "total", "n"}
    if not expected_cols.issubset(cols):
        raise SmokeError(f"sample columns {cols} missing {expected_cols - cols}")
    if sample.get("rowCount") != total:
        raise SmokeError(
            f"sample rowCount {sample.get('rowCount')!r} != run totalRows {total}"
        )
    return {"runId": run_id, "totalRows": total, "output": output}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8471")
    p.add_argument("--expect-version", default=None,
                   help="Require GET /api/version.version to equal this string")
    p.add_argument("--expect-sha-not-unknown", action="store_true",
                   help="Fail if /api/version.sha is missing or 'unknown'")
    p.add_argument("--ready-timeout", type=float, default=120.0)
    args = p.parse_args(argv)

    wait_ready(args.base_url, timeout_s=args.ready_timeout)
    version = check_version(
        args.base_url,
        expect_version=args.expect_version,
        expect_sha_not_unknown=args.expect_sha_not_unknown,
    )
    result = run_purchases_smoke(args.base_url)
    print(json.dumps({"ok": True, "version": version, "smoke": result}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"release smoke FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
