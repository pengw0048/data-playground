#!/usr/bin/env python3
"""Certify an in-place v0.1.0 workspace upgrade using published and candidate wheels."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


SOURCE_VERSION = "0.1.0"
TARGET_VERSION = "0.2.0"
SOURCE_SCHEMA = "0038_inbox_dataset_scoped"
TARGET_SCHEMA = "0039_folder_replays"
SOURCE_SHA = "172866586a503d3df7e9a2ed399bc20b9e510129"
SOURCE_WHEEL_URL = (
    "https://github.com/pengw0048/data-playground/releases/download/v0.1.0/"
    "data_playground-0.1.0-py3-none-any.whl"
)
SOURCE_SUMS_URL = (
    "https://github.com/pengw0048/data-playground/releases/download/v0.1.0/SHA256SUMS"
)


def run(*command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    display = [re.sub(r"(?<=://)[^/@\s]+(?=@)", "***", part) for part in command]
    print("+", " ".join(display), flush=True)
    return subprocess.run(command, env=env, check=True, text=True, capture_output=True)


def request(base_url: str, method: str, path: str, body: Any | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} returned {exc.code}: {detail}") from exc


def wait_for(base_url: str, path: str, *, statuses: set[str] | None = None) -> Any:
    deadline = time.monotonic() + 120
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = request(base_url, "GET", path)
            if statuses is None or last.get("status") in statuses:
                return last
        except (OSError, RuntimeError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {path}; last response: {last!r}")


def install(uv: str, venv: Path, wheel: Path, *, postgres: bool) -> None:
    run(uv, "venv", "--clear", str(venv))
    packages = [str(wheel)]
    if postgres:
        packages.append("psycopg[binary]>=3.1.18,<4")
    run(uv, "pip", "install", "--python", str(venv), *packages)


def start_hub(dataplay: Path, workspace: Path, env: dict[str, str], port: int, log: Path) \
        -> subprocess.Popen[str]:
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(dataplay), "--host", "127.0.0.1", "--port", str(port), "--no-open",
         "--workspace", str(workspace)],
        env=env, stdout=handle, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    process._upgrade_log_handle = handle  # type: ignore[attr-defined]
    try:
        wait_for(f"http://127.0.0.1:{port}", "/api/livez")
    except Exception:
        stop_hub(process)
        print(log.read_text(encoding="utf-8", errors="replace")[-8000:], file=sys.stderr)
        raise
    return process


def stop_hub(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    else:
        # The hub may have exited before a child workload. It still owns the dedicated process group.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    handle = getattr(process, "_upgrade_log_handle", None)
    if handle is not None:
        handle.close()


def schema_head(backend: str, workspace: Path, postgres_url: str | None) -> str:
    if backend == "sqlite":
        with sqlite3.connect(workspace / "dataplay.db") as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is None:
            raise RuntimeError("SQLite alembic_version is empty")
        return str(row[0])
    if not postgres_url:
        raise RuntimeError("PostgreSQL requires --postgres-url")
    return run("psql", postgres_url, "-Atc", "SELECT version_num FROM alembic_version").stdout.strip()


def tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def snapshot(backend: str, workspace: Path, backup: Path, postgres_url: str | None,
             version: dict[str, Any], schema: str) -> dict[str, Any]:
    backup.mkdir(parents=True)
    workspace_copy = backup / "workspace"
    shutil.copytree(workspace, workspace_copy)
    if backend == "postgres":
        if not postgres_url:
            raise RuntimeError("PostgreSQL requires --postgres-url")
        run("pg_dump", "--format=custom", "--file", str(backup / "metadata.dump"), postgres_url)
    manifest = {
        "release": version,
        "schema": schema,
        "backend": backend,
        "workspaceFiles": tree_digest(workspace_copy),
    }
    if backend == "postgres":
        manifest["metadataDumpSha256"] = hashlib.sha256(
            (backup / "metadata.dump").read_bytes()).hexdigest()
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def poll_run(base_url: str, run_id: str) -> dict[str, Any]:
    return wait_for(base_url, f"/api/run/{run_id}", statuses={"done", "failed", "cancelled"})


def poll_restore(base_url: str, task_id: str) -> dict[str, Any]:
    return wait_for(
        base_url, f"/api/restore-revision/{task_id}",
        statuses={"done", "failed", "cancelled"},
    )


def run_write(base_url: str, graph: dict[str, Any]) -> dict[str, Any]:
    submission = str(uuid.uuid4())
    admission = request(base_url, "POST", "/api/run/write-admission", {
        "graph": graph, "nodeId": "write", "submissionId": submission,
    })
    if not admission.get("managed") or admission.get("provider") != "managed-local-file":
        raise RuntimeError(f"write was not admitted as managed-local-file: {admission!r}")
    started = request(base_url, "POST", "/api/run", {
        "graph": graph, "targetNodeId": "write", "confirmed": True,
        "submissionId": submission, "writeIntent": admission["intent"],
    })
    completed = poll_run(base_url, started["runId"])
    if completed["status"] != "done":
        raise RuntimeError(f"write failed: {completed!r}")
    jobs = request(base_url, "GET", f"/api/jobs?run_id={started['runId']}")["items"]
    if len(jobs) != 1 or not jobs[0].get("outputReceipt"):
        raise RuntimeError(f"write receipt missing from Jobs: {jobs!r}")
    return jobs[0]


def fixture(base_url: str) -> dict[str, Any]:
    tables = request(base_url, "GET", "/api/catalog/tables?limit=100")["items"]
    samples = sorted(
        (table for table in tables if table["name"] in {"events", "movies"}),
        key=lambda table: table["name"],
    )
    if [table["name"] for table in samples] != ["events", "movies"]:
        raise RuntimeError(f"published wheel did not seed events and movies: {tables!r}")

    canvas_id = "upgrade-drill-canvas"
    graph = {
        "id": canvas_id, "name": "Upgrade drill", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"title": "Fixture source", "config": {
                "uri": samples[0]["uri"],
            }}},
            {"id": "write", "type": "write", "data": {"title": "Upgrade output", "config": {
                "filename": "upgrade-output.parquet", "writeMode": "overwrite",
            }}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    }
    created = request(base_url, "POST", "/api/canvas", graph)
    if created != {"ok": True, "id": canvas_id, "created": True}:
        raise RuntimeError(f"unexpected Canvas create response: {created!r}")
    saved = request(base_url, "PUT", f"/api/canvas/{canvas_id}?expectedVersion=1", {
        **graph, "name": "Upgrade drill retained",
    })
    if saved.get("version") != 2:
        raise RuntimeError(f"Canvas optimistic update did not produce version 2: {saved!r}")

    first_job = run_write(base_url, graph)
    second_graph = json.loads(json.dumps(graph))
    second_graph["nodes"][0]["data"]["config"]["uri"] = samples[1]["uri"]
    second_job = run_write(base_url, second_graph)
    first_receipt = first_job["outputReceipt"]
    second_receipt = second_job["outputReceipt"]
    if second_receipt.get("parentHead", {}).get("revisionId") != first_receipt["revisionId"]:
        raise RuntimeError("second write did not replace the first managed revision")

    logical_dataset_id = first_receipt["datasetId"]
    if second_receipt["datasetId"] != logical_dataset_id:
        raise RuntimeError("managed writes changed logical dataset identity")
    output_tables = request(base_url, "GET", "/api/catalog/tables?limit=100")["items"]
    output = next(table for table in output_tables if table["name"] == "upgrade-output")
    revisions = request(
        base_url, "GET", f"/api/catalog/tables/{output['id']}/revisions?limit=10")["items"]
    revision_ids = {revision["revisionId"] for revision in revisions}
    if not {first_receipt["revisionId"], second_receipt["revisionId"]} <= revision_ids:
        raise RuntimeError(f"managed revision history is incomplete: {revisions!r}")
    first_exact = request(
        base_url, "GET", f"/api/catalog/revisions/{logical_dataset_id}/{first_receipt['revisionId']}")
    restored = request(
        base_url, "POST",
        f"/api/catalog/revisions/{logical_dataset_id}/{first_receipt['revisionId']}/restore",
        {"submissionId": str(uuid.uuid4()),
         "expectedHeadRevisionId": second_receipt["revisionId"]},
    )
    restore = poll_restore(base_url, restored["taskId"])
    if restore["status"] != "done" or not restore.get("childRevisionId"):
        raise RuntimeError(f"restore-as-new-head failed: {restore!r}")
    restored_exact = request(
        base_url, "GET",
        f"/api/catalog/revisions/{logical_dataset_id}/{restore['childRevisionId']}",
    )
    if restored_exact["preview"] != first_exact["preview"]:
        raise RuntimeError("restored revision content does not match the exact source revision")

    cred = request(base_url, "POST", "/api/creds", {
        "name": "Upgrade drill reference", "kind": "agent",
        "fields": {"apiKey": "env:DP_UPGRADE_DRILL_TOKEN"},
    })
    request(base_url, "PUT", "/api/settings", {
        "key": "plugin.upgrade_drill.mode", "value": "certification", "scope": "global",
    })
    return collect(base_url, {
        "sampleTableIds": [table["id"] for table in samples],
        "canvasId": canvas_id,
        "outputTableId": output["id"],
        "datasetId": logical_dataset_id,
        "revisionIds": [first_receipt["revisionId"], second_receipt["revisionId"],
                        restore["childRevisionId"]],
        "runIds": [first_job["runId"], second_job["runId"], restore["taskId"]],
        "credId": cred["id"],
    })


def collect(base_url: str, identity: dict[str, Any]) -> dict[str, Any]:
    tables = request(base_url, "GET", "/api/catalog/tables?limit=100")["items"]
    by_id = {table["id"]: table for table in tables}
    canvas = request(base_url, "GET", f"/api/canvas/{identity['canvasId']}")
    versions = request(base_url, "GET", f"/api/canvas/{identity['canvasId']}/versions")
    revisions = [request(
        base_url, "GET", f"/api/catalog/revisions/{identity['datasetId']}/{revision_id}")
        for revision_id in identity["revisionIds"]]
    jobs = request(base_url, "GET", "/api/jobs?limit=100")["items"]
    jobs_by_run = {job.get("runId"): job for job in jobs}
    run_history = request(base_url, "GET", f"/api/canvas/{identity['canvasId']}/runs")
    history_by_run = {record["runId"]: record for record in run_history}
    inbox = request(base_url, "GET", "/api/inbox?limit=100&filter=all")["items"]
    creds = {cred["id"]: cred for cred in request(base_url, "GET", "/api/creds")}
    settings = request(base_url, "GET", "/api/settings")
    sample_content = []
    for table_id in identity["sampleTableIds"]:
        sample = request(base_url, "POST", "/api/data/sample", {
            "uri": by_id[table_id]["uri"], "k": 2,
        })
        sample_content.append({
            "tableId": table_id,
            "columns": [{"name": column["name"], "type": column["type"]}
                        for column in sample["columns"]],
            "rows": sample["rows"], "rowCount": sample.get("rowCount"),
        })
    return {
        "identity": identity,
        "sampleTables": [{key: by_id[table_id].get(key) for key in ("id", "name", "uri", "rowCount")}
                         for table_id in identity["sampleTableIds"]],
        "sampleContent": sample_content,
        "outputTable": {key: by_id[identity["outputTableId"]].get(key)
                        for key in ("id", "name", "uri", "rowCount")},
        "canvas": {key: canvas.get(key) for key in ("id", "name", "version", "nodes", "edges")},
        "canvasVersions": sorted(version["id"] for version in versions),
        "revisions": [{
            "datasetId": revision["datasetId"], "revisionId": revision["revisionId"],
            "parentRevisionId": revision.get("parentRevisionId"),
            "summary": revision["summary"], "preview": revision["preview"],
        } for revision in revisions],
        "jobs": [{
            "runId": jobs_by_run[run_id].get("runId"),
            "status": jobs_by_run[run_id]["status"],
            "outputReceipt": {
                "datasetId": jobs_by_run[run_id]["outputReceipt"]["datasetId"],
                "revisionId": jobs_by_run[run_id]["outputReceipt"]["revisionId"],
                "parentRevisionId": (
                    jobs_by_run[run_id]["outputReceipt"].get("parentHead") or {}).get("revisionId"),
                "rows": jobs_by_run[run_id]["outputReceipt"]["rows"],
                "bytes": jobs_by_run[run_id]["outputReceipt"]["bytes"],
            },
        } for run_id in identity["runIds"]],
        "runHistory": [{
            "runId": history_by_run[run_id]["runId"],
            "status": history_by_run[run_id]["status"],
            "targetNodeId": history_by_run[run_id]["targetNodeId"],
            "rows": history_by_run[run_id]["rows"],
            "executionManifestSha256": history_by_run[run_id]["executionManifestSha256"],
        } for run_id in identity["runIds"][:2]],
        "inbox": sorted([{
            "taskId": item["taskId"], "taskKind": item["taskKind"],
            "outcome": item["outcome"], "jobAvailable": item["jobAvailable"],
        } for item in inbox if item["taskId"] in identity["runIds"]], key=lambda item: item["taskId"]),
        "cred": {key: creds[identity["credId"]].get(key) for key in ("id", "name", "kind", "fields")},
        "pluginSetting": settings["global"].get("plugin.upgrade_drill.mode"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument("--candidate-wheel", type=Path, required=True)
    parser.add_argument("--candidate-sha", default=os.environ.get("GITHUB_SHA", "unknown"))
    parser.add_argument("--postgres-url")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--source-wheel-url", default=SOURCE_WHEEL_URL)
    parser.add_argument("--source-sums-url", default=SOURCE_SUMS_URL)
    parser.add_argument("--port", type=int, default=8478)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required")
    if args.backend == "postgres" and not args.postgres_url:
        raise SystemExit("--postgres-url is required for PostgreSQL")
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise SystemExit(f"--work-root must be an empty dedicated directory: {root}")
    workspace = root / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "upgrade-drill.json").write_text(
        json.dumps({"backend": args.backend, "secretRefsOnly": True}, indent=2) + "\n")
    source_wheel = root / "data_playground-0.1.0-py3-none-any.whl"
    source_sums = root / "SHA256SUMS"
    print(f"downloading published source wheel: {args.source_wheel_url}")
    urllib.request.urlretrieve(args.source_wheel_url, source_wheel)
    urllib.request.urlretrieve(args.source_sums_url, source_sums)
    expected = next(
        (line.split()[0] for line in source_sums.read_text().splitlines()
         if line.split() and line.split()[-1].lstrip("*") == source_wheel.name), None)
    actual = hashlib.sha256(source_wheel.read_bytes()).hexdigest()
    if expected is None or actual != expected:
        raise RuntimeError(f"published source wheel checksum mismatch: expected={expected}, actual={actual}")
    source_venv, candidate_venv = root / "venv-v0.1.0", root / "venv-candidate"
    install(uv, source_venv, source_wheel, postgres=args.backend == "postgres")
    install(uv, candidate_venv, args.candidate_wheel.resolve(), postgres=args.backend == "postgres")
    candidate_sha256 = hashlib.sha256(args.candidate_wheel.read_bytes()).hexdigest()
    env = os.environ.copy()
    env.update({
        "DP_WORKSPACE": str(workspace), "DP_DATA_DIR": str(workspace / "data"),
        "DP_GIT_SHA": SOURCE_SHA,
    })
    if args.backend == "postgres":
        env["DP_DATABASE_URL"] = args.postgres_url.replace(
            "postgresql://", "postgresql+psycopg://", 1)

    base_url = f"http://127.0.0.1:{args.port}"
    process: subprocess.Popen[str] | None = None
    try:
        run(str(source_venv / "bin" / "dataplay"), "migrate", "--workspace", str(workspace), env=env)
        process = start_hub(source_venv / "bin" / "dataplay", workspace, env, args.port,
                            root / "source.log")
        source_version = request(base_url, "GET", "/api/version")
        if (source_version.get("version"), source_version.get("sha")) != (SOURCE_VERSION, SOURCE_SHA):
            raise RuntimeError(f"source is not the published v{SOURCE_VERSION} wheel: {source_version!r}")
        before = fixture(base_url)
        stop_hub(process)
        process = None
        before_schema = schema_head(args.backend, workspace, args.postgres_url)
        if before_schema != SOURCE_SCHEMA:
            raise RuntimeError(f"source schema {before_schema!r} != {SOURCE_SCHEMA!r}")
        backup = snapshot(args.backend, workspace, root / "backup-v0.1.0", args.postgres_url,
                          source_version, before_schema)

        candidate_env = {**env, "DP_GIT_SHA": args.candidate_sha}
        run(str(candidate_venv / "bin" / "dataplay"), "migrate", "--workspace", str(workspace),
            env=candidate_env)
        after_schema = schema_head(args.backend, workspace, args.postgres_url)
        if after_schema != TARGET_SCHEMA:
            raise RuntimeError(f"target schema {after_schema!r} != {TARGET_SCHEMA!r}")
        process = start_hub(candidate_venv / "bin" / "dataplay", workspace, candidate_env, args.port,
                            root / "candidate.log")
        target_version = request(base_url, "GET", "/api/version")
        if (target_version.get("version"), target_version.get("sha")) \
                != (TARGET_VERSION, args.candidate_sha):
            raise RuntimeError(f"candidate is not v{TARGET_VERSION}: {target_version!r}")
        after = collect(base_url, before["identity"])
        if after != before:
            raise RuntimeError("bounded retained-state evidence changed:\n" + json.dumps(
                {"before": before, "after": after}, indent=2))
        evidence = {
            "contract": f"v{SOURCE_VERSION}/{SOURCE_SCHEMA} -> v{TARGET_VERSION}/{TARGET_SCHEMA}",
            "backend": args.backend, "sourceVersion": source_version,
            "targetVersion": target_version, "backup": backup, "retainedState": after,
            "artifacts": {
                "source": {"name": source_wheel.name, "sha256": actual, "releaseSha": SOURCE_SHA},
                "candidate": {"name": args.candidate_wheel.name, "sha256": candidate_sha256,
                              "releaseSha": args.candidate_sha},
            },
        }
        args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_out.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"UPGRADE_DRILL PASS {args.backend}: {evidence['contract']}")
    finally:
        stop_hub(process)


if __name__ == "__main__":
    main()
