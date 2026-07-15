"""Real-service acceptance check for the restart-durable Ray Jobs lifecycle.

Run through ``scripts/ray-jobs-acceptance.sh``. Each command is a separate container/process so
``submit-restart`` actually loses all process-local runner state before ``recover-restart`` begins.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


_PREFIX = "ray-jobs-acceptance-"
_SOURCE_URI = "s3://dpray/acceptance/input.parquet"
_MANAGED_SOURCE_URI = "s3://dpray/acceptance/managed-input.parquet"
_MANAGED_SOURCE_ALLOCATION = f"{_PREFIX}managed-source"
_TERMINAL = {"done", "failed", "cancelled"}


def _wait(label: str, fn, predicate, timeout: float = 180.0, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if predicate(last):
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {label}; last observation={last!r}")


def _s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("DP_S3_ENDPOINT"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("DP_S3_KEY"),
        aws_secret_access_key=(
            os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("DP_S3_SECRET")
        ),
        region_name=os.environ.get("AWS_REGION") or "us-east-1",
    )


def bootstrap_storage() -> None:
    client = _s3_client()
    _wait("MinIO", client.list_buckets, lambda _value: True, timeout=90)
    names = {bucket["Name"] for bucket in client.list_buckets().get("Buckets", [])}
    if "dpray" not in names:
        client.create_bucket(Bucket="dpray")
    client.put_bucket_versioning(Bucket="dpray", VersioningConfiguration={"Status": "Enabled"})
    print(json.dumps({"check": "storage", "bucket": "dpray", "versioning": "Enabled"}))


def _configure_control_plane() -> None:
    from hub import metadb

    metadb.init_db()
    fields = {
        "endpoint": os.environ["DP_S3_ENDPOINT"],
        "accessKeyId": "env:DP_S3_KEY",
        "secretAccessKey": "env:DP_S3_SECRET",
        "region": os.environ.get("AWS_REGION", "us-east-1"),
    }
    if os.environ.get("AWS_SESSION_TOKEN"):
        fields["sessionToken"] = "env:AWS_SESSION_TOKEN"
    cred = metadb.cred_upsert(
        "ray-jobs-acceptance-object-store",
        "Ray Jobs acceptance object store",
        "object_store",
        fields,
    )
    metadb.set_setting("defaultObjectStoreCredId", cred["id"], "global")


def _load_dp_ray():
    source = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "dp_ray" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        f"dp_ray_acceptance_{os.getpid()}_{time.monotonic_ns()}", source
    )
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load Ray plugin from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_runner(*, recover: bool):
    from hub.deps import Deps

    root = tempfile.mkdtemp(prefix="dp-ray-jobs-acceptance-hub-")
    workspace, data = Path(root) / "workspace", Path(root) / "data"
    workspace.mkdir()
    data.mkdir()
    deps = Deps(str(workspace), str(data), maintain_storage=False)
    runner = _load_dp_ray().RayRunner(deps, recover=recover)
    # Use the same capability routing path as the API. In a submit phase this runner must be the selected
    # whole-graph backend; in recovery the insertion also restores the normal deps ownership shape.
    deps.runners.insert(0, runner)
    return runner


def _graph(mode: str, source_uri: str = _SOURCE_URI):
    from hub.models import Graph

    return Graph.model_validate({
        "id": f"{_PREFIX}{mode}",
        "version": 1,
        "nodes": [
            {"id": "src", "type": "source", "position": {"x": 0, "y": 0},
             "data": {"config": {"uri": source_uri}}},
            {"id": "map", "type": "transform", "position": {"x": 0, "y": 0},
             "data": {"config": {
                 "mode": "map", "code": "def fn(row): return row",
                 # Exercise the public capability router, not an acceptance-only backend override.
                 "requires": {"labels": {"engine": "ray"}},
             }}},
            {"id": "write", "type": "write", "position": {"x": 0, "y": 0},
             "data": {"config": {
                 "filename": f"acceptance-{mode}.parquet", "writeMode": "overwrite"
             }}},
        ],
        "edges": [
            {"id": "source-map", "source": "src", "target": "map",
             "data": {"wire": "dataset"}},
            {"id": "map-write", "source": "map", "target": "write",
             "data": {"wire": "dataset"}},
        ],
    })


def _seed_source() -> None:
    import pyarrow as pa
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq
    from hub.plugins.adapters import object_fs

    fs, path = object_fs(_SOURCE_URI)
    if fs.get_file_info(path).type == pafs.FileType.File:
        return
    table = pa.table({
        "id": pa.array(range(10_000), type=pa.int64()),
        "group": pa.array([f"g{i % 17}" for i in range(10_000)]),
    })
    with fs.open_output_stream(path) as stream:
        pq.write_table(table, stream)


def _publish_managed_source(runner, *, label: str, rows: int) -> dict:
    """Write and publish one real catalog-managed source generation."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from hub import handoff
    from hub.plugins.adapters import object_fs

    run_id = f"{_PREFIX}source-{label}"
    handle = handoff.allocate_attempt(
        logical_uri=_MANAGED_SOURCE_URI,
        kind="sink",
        run_id=run_id,
        allocation_key=_MANAGED_SOURCE_ALLOCATION,
        catalog_key_base="tbl_ray_jobs_acceptance_source",
        uri_factory=lambda namespace, generation, attempt_id: handoff.physical_attempt_uri(
            _MANAGED_SOURCE_URI, namespace, generation, attempt_id
        ),
        write_lease_seconds=120,
    )
    table = pa.table({
        "id": pa.array(range(rows), type=pa.int64()),
        "source_generation": pa.array([label] * rows),
    })
    fs, path = object_fs(handle["uri"])
    with fs.open_output_stream(path.rstrip("/") + "/part-00000.parquet") as stream:
        pq.write_table(table, stream)
    handoff.write_manifest(handle["uri"], run_id=run_id, rows=rows, schema=table.schema)
    receipt = runner.deps.catalog.publish_managed_output(
        name="ray_jobs_acceptance_source",
        uri=handle["uri"],
        version=label,
        parents=[],
        pipeline="ray-jobs-acceptance",
    )
    if (receipt.get("uri") != handle["uri"]
            or receipt.get("generation") != handle["generation"]):
        raise AssertionError(
            f"managed source publication did not attest its allocation: {receipt!r}"
        )
    return {**handle, "logical_uri": _MANAGED_SOURCE_URI, "rows": rows}


def _assert_source_pin(run_id: str, source: dict) -> None:
    from hub import metadb

    expected = [{"uri": source["uri"], "generation": source["generation"]}]
    observed = metadb.backend_source_pins(run_id)
    if observed != expected:
        raise AssertionError(
            f"{run_id}: backend source pin changed; expected={expected!r}, observed={observed!r}"
        )


def _put_restart_barrier(job: dict) -> None:
    """Release the acceptance wrapper only after the catalog replacement is durable."""
    barrier_uri = job["job_uri"].rsplit("/", 1)[0] + "/acceptance-source-replaced"
    parsed = urlsplit(barrier_uri)
    _s3_client().put_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
        Body=b"catalog source generation replaced",
        ContentType="text/plain",
    )


def _put_recovery_ready(job: dict) -> None:
    """Release the remote driver only after a replacement hub reattaches to the live job."""
    barrier_uri = job["job_uri"].rsplit("/", 1)[0] + "/acceptance-recovery-ready"
    parsed = urlsplit(barrier_uri)
    _s3_client().put_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
        Body=b"replacement hub reattached while Ray reported RUNNING",
        ContentType="text/plain",
    )


def _ensure_canvas(graph) -> None:
    from hub import metadb

    with metadb.session() as session:
        canvas = session.get(metadb.Canvas, graph.id)
        if canvas is None:
            session.add(metadb.Canvas(
                id=graph.id,
                owner_id=metadb.DEFAULT_USER_ID,
                name=graph.id,
                version=graph.version,
                doc=graph.model_dump_json(),
            ))


def _submit(mode: str):
    from hub import metadb
    from hub.routers.runs import start_run

    _configure_control_plane()
    runner = _make_runner(recover=False)
    managed_source = None
    if mode == "restart":
        managed_source = _publish_managed_source(runner, label="frozen", rows=10_000)
        source_uri = managed_source["uri"]
    else:
        _seed_source()
        source_uri = _SOURCE_URI
    graph = _graph(mode, source_uri)
    _ensure_canvas(graph)
    status, owner = start_run(
        runner.deps, graph, "write", metadb.DEFAULT_USER_ID, confirmed=True
    )
    if owner is not runner:
        raise AssertionError(f"{mode}: capability routing selected {type(owner).__name__}, not RayRunner")
    if status.backend_ref is None:
        raise AssertionError(f"{mode}: run did not create a durable backend ref: {status}")
    run_id = status.run_id
    client = runner._jobs_client()
    remote = _wait(
        f"Ray job {status.backend_ref.submission_id} to become RUNNING",
        lambda: runner._find_job(client, status.backend_ref.submission_id),
        lambda state: state == "RUNNING",
        timeout=90,
    )
    binding = metadb.backend_job(run_id)
    if not binding or binding["submission_id"] != status.backend_ref.submission_id:
        raise AssertionError(f"{mode}: SQL binding does not match the live Ray job")
    _assert_one_remote_job(client, run_id, status.backend_ref.submission_id)
    if managed_source is not None:
        job = _job_envelope(run_id)
        if job.get("source_attempts") != [managed_source["uri"]]:
            raise AssertionError(
                f"restart job did not freeze its managed source: {job.get('source_attempts')!r}"
            )
        _assert_source_pin(run_id, managed_source)
    print(json.dumps({
        "phase": f"submit-{mode}", "run_id": run_id,
        "submission_id": status.backend_ref.submission_id, "ray_status": remote,
        "process_pid": os.getpid(),
    }, sort_keys=True))
    return runner, status, client, managed_source


def _terminal(runner, run_id: str):
    return _wait(
        f"run {run_id} terminal publication",
        lambda: runner.status(run_id),
        lambda status: status.status in _TERMINAL,
    )


def _job_items(client) -> list:
    raw = client.list_jobs()
    if isinstance(raw, dict):
        items = []
        for key, value in raw.items():
            if isinstance(value, dict):
                value = {"submission_id": key, **value}
            items.append(value)
        return items
    return list(raw or [])


def _value(item, key: str):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _assert_one_remote_job(client, run_id: str, submission_id: str) -> None:
    matching = []
    for item in _job_items(client):
        metadata = _value(item, "metadata") or {}
        if isinstance(metadata, dict) and metadata.get("dataplay_run_id") == run_id:
            matching.append(item)
    ids = [str(_value(item, "submission_id") or _value(item, "job_id") or "") for item in matching]
    if len(matching) != 1 or ids != [submission_id]:
        raise AssertionError(
            f"expected exactly one remote job for {run_id} ({submission_id}); observed {ids}"
        )


def _job_envelope(run_id: str) -> dict:
    from hub import metadb

    payload = metadb.backend_job_artifact_payload(run_id)
    if payload is None:
        raise AssertionError(f"{run_id}: durable SQL envelope is missing")
    return json.loads(payload)


def _assert_exactly_once(run_id: str, canvas_id: str, expected_status: str) -> None:
    from sqlalchemy import func, select
    from hub import metadb

    with metadb.session() as session:
        history = session.scalar(select(func.count()).select_from(metadb.RunRecord).where(
            metadb.RunRecord.run_id == run_id,
            metadb.RunRecord.canvas_id == canvas_id,
            metadb.RunRecord.status == expected_status,
        ))
        fences = session.scalar(select(func.count()).select_from(metadb.RunTerminalFence).where(
            metadb.RunTerminalFence.run_id == run_id,
            metadb.RunTerminalFence.status == expected_status,
        ))
    if history != 1 or fences != 1:
        raise AssertionError(
            f"{run_id}: expected one {expected_status} history row and terminal fence; "
            f"observed history={history}, fences={fences}"
        )


def _assert_abandoned_sink(run_id: str, job: dict) -> None:
    """Prove a negative terminal released the exact remote writer and both durable leases."""
    from sqlalchemy import select
    from hub import metadb

    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    with metadb.session() as session:
        attempt = session.get(metadb.ObjectAttempt, physical_uri)
        leases = list(session.scalars(select(metadb.ObjectAttemptLease.lease_id).where(
            metadb.ObjectAttemptLease.attempt_uri == physical_uri,
            metadb.ObjectAttemptLease.lease_type.in_(("write", "publish")),
        )))
        observed = None if attempt is None else {
            "uri": attempt.uri, "run_id": attempt.run_id, "kind": attempt.kind,
            "state": attempt.state, "terminal_proof_at": attempt.terminal_proof_at,
        }
    if (observed is None or observed["run_id"] != run_id or observed["kind"] != "sink"
            or observed["state"] != "abandoned"
            or observed["terminal_proof_at"] is None or leases):
        raise AssertionError(
            f"{run_id}: exact sink writer was not durably abandoned; "
            f"attempt={observed!r}, writer_leases={leases!r}"
        )


def submit_restart() -> None:
    from sqlalchemy import select
    from hub import metadb

    runner, status, _client, source = _submit("restart")
    if source is None:
        raise AssertionError("restart acceptance requires a managed source generation")
    replacement = _publish_managed_source(runner, label="replacement", rows=7)
    if replacement["generation"] != source["generation"] + 1:
        raise AssertionError(
            "managed source replacement did not advance its durable allocation generation"
        )
    with metadb.session() as session:
        frozen = session.get(metadb.ObjectAttempt, source["uri"])
        logical = session.scalar(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.logical_uri == source["logical_uri"]
        ))
        current = session.get(metadb.ObjectAttempt, replacement["uri"])
        if (frozen is None or frozen.state != "published" or logical is None
                or logical.current_uri != replacement["uri"]
                or current is None or current.state != "published"):
            raise AssertionError(
                "catalog replacement did not preserve the backend-pinned source generation"
            )
    _assert_source_pin(status.run_id, source)
    job = _job_envelope(status.run_id)
    _put_restart_barrier(job)
    print(json.dumps({
        "phase": "replace-source", "run_id": status.run_id,
        "frozen_source": source["uri"], "replacement_source": replacement["uri"],
        "frozen_generation": source["generation"],
        "replacement_generation": replacement["generation"],
        "frozen_rows": source["rows"], "replacement_rows": replacement["rows"],
    }, sort_keys=True))
    # Returning destroys this container and all process-local runner/thread state. The remote wrapper
    # remains RUNNING briefly after the barrier, so recovery cannot accidentally reuse local state.


def recover_restart() -> None:
    from sqlalchemy import func, select
    from hub import metadb

    _configure_control_plane()
    canvas_id = f"{_PREFIX}restart"
    with metadb.session() as session:
        run_ids = list(session.scalars(select(metadb.RunState.run_id).where(
            metadb.RunState.canvas_id == canvas_id
        )))
    if len(run_ids) != 1:
        raise AssertionError(f"restart canvas should own one submitted run; observed {run_ids}")
    run_id = run_ids[0]
    binding = metadb.backend_job(run_id)
    if not binding:
        raise AssertionError("restart binding disappeared with the submitting process")
    submission_id = binding["submission_id"]
    job = _job_envelope(run_id)
    source_attempts = job.get("source_attempts") or []
    if len(source_attempts) != 1:
        raise AssertionError(f"restart job should own one managed source: {source_attempts!r}")
    source_uri = source_attempts[0]
    with metadb.session() as session:
        source = session.get(metadb.ObjectAttempt, source_uri)
        logical = session.get(
            metadb.CatalogLogicalDataset, source.logical_id if source is not None else ""
        )
        if (source is None or source.state != "published" or logical is None
                or logical.current_uri == source_uri):
            raise AssertionError(
                "replacement process did not observe a live pin behind the replaced catalog pointer"
            )
        source_pin = {
            "uri": source_uri, "generation": source.generation,
            "logical_uri": source.logical_uri, "replacement_uri": logical.current_uri,
        }
    _assert_source_pin(run_id, source_pin)
    runner = _make_runner(recover=True)
    client = runner._jobs_client(binding["control_address"])
    live = runner._find_job(client, submission_id)
    if live != "RUNNING":
        raise AssertionError(
            f"replacement hub did not reattach while the Ray job was live: {live!r}"
        )
    _assert_one_remote_job(client, run_id, submission_id)
    _put_recovery_ready(job)
    final = _terminal(runner, run_id)
    if final.status != "done" or final.total_rows != 10_000 or not final.output_uri:
        raise AssertionError(f"replacement hub did not publish the successful result: {final}")
    if runner._find_job(client, submission_id) != "SUCCEEDED":
        raise AssertionError("replacement reached done without an authoritative Ray SUCCEEDED state")
    _assert_one_remote_job(client, run_id, submission_id)
    _assert_exactly_once(run_id, canvas_id, "done")

    if metadb.backend_source_pins(run_id) != []:
        raise AssertionError("terminal publication did not release the backend source pin")
    with metadb.session() as session:
        frozen = session.get(metadb.ObjectAttempt, source_uri)
        logical = session.scalar(select(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.logical_uri == source_pin["logical_uri"]
        ))
        replacement = session.get(metadb.ObjectAttempt, source_pin["replacement_uri"])
        if (frozen is None or frozen.state != "superseded" or logical is None
                or logical.current_uri != source_pin["replacement_uri"]
                or replacement is None or replacement.state != "published"):
            raise AssertionError(
                "terminal source-pin release did not retire only the frozen generation"
            )

    logical_uri = job["sink_contracts"]["write"]["logical_uri"]
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    if final.output_uri != physical_uri:
        raise AssertionError(f"published output {final.output_uri!r} != frozen attempt {physical_uri!r}")
    with metadb.session() as session:
        entries = session.scalar(select(func.count()).select_from(metadb.CatalogEntry).where(
            metadb.CatalogEntry.uri == physical_uri
        ))
        attempts = session.scalar(select(func.count()).select_from(metadb.ObjectAttempt).where(
            metadb.ObjectAttempt.run_id == run_id,
            metadb.ObjectAttempt.uri == physical_uri,
            metadb.ObjectAttempt.logical_uri == logical_uri,
            metadb.ObjectAttempt.state == "published",
        ))
        logical = session.scalar(select(func.count()).select_from(metadb.CatalogLogicalDataset).where(
            metadb.CatalogLogicalDataset.logical_uri == logical_uri,
            metadb.CatalogLogicalDataset.current_uri == physical_uri,
        ))
    if (entries, attempts, logical) != (1, 1, 1):
        raise AssertionError(
            "terminal catalog publication was not exactly once: "
            f"catalog_entries={entries}, published_attempts={attempts}, logical_heads={logical}"
        )
    print(json.dumps({
        "phase": "recover-restart", "run_id": run_id, "submission_id": submission_id,
        "status": final.status, "rows": final.total_rows, "history": 1,
        "catalog_entries": 1, "remote_jobs": 1, "source_pin_released": True,
    }, sort_keys=True))


def cancel() -> None:
    from hub import metadb

    runner, status, client, _source = _submit("cancel")
    submission_id = status.backend_ref.submission_id
    requested = runner.cancel(status.run_id)
    final = requested if requested.status in _TERMINAL else _terminal(runner, status.run_id)
    remote = _wait(
        f"Ray STOPPED acknowledgement for {submission_id}",
        lambda: runner._find_job(client, submission_id),
        lambda state: state == "STOPPED",
        timeout=60,
    )
    if final.status != "cancelled" or not runner.cancel_acknowledged(status.run_id):
        raise AssertionError(f"cancel was not durably acknowledged: {final}")
    _assert_exactly_once(status.run_id, _graph("cancel").id, "cancelled")
    job = _job_envelope(status.run_id)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    if metadb.catalog_get(physical_uri) is not None:
        raise AssertionError("cancelled attempt was exposed in the catalog")
    _assert_abandoned_sink(status.run_id, job)
    print(json.dumps({
        "phase": "cancel", "run_id": status.run_id, "submission_id": submission_id,
        "status": final.status, "ray_status": remote, "stop_acknowledged": True,
    }, sort_keys=True))


def bad_result(mode: str) -> None:
    if mode not in ("missing", "corrupt"):
        raise ValueError(mode)
    from hub import metadb

    runner, status, client, _source = _submit(mode)
    submission_id = status.backend_ref.submission_id
    final = _terminal(runner, status.run_id)
    remote = runner._find_job(client, submission_id)
    if final.status != "failed" or final.output_uri or final.output_table:
        raise AssertionError(f"{mode} result was not fail-closed: {final}")
    if mode == "missing":
        if remote != "SUCCEEDED" or "TerminalResultMissing" not in (final.error or ""):
            raise AssertionError(
                f"missing-result check did not exercise SUCCEEDED-without-receipt: remote={remote}, "
                f"error={final.error!r}"
            )
    # The entrypoint proves a valid receipt before replacing it. The supervisor can observe the
    # corrupt replacement before Ray publishes SUCCEEDED, in which case the production quarantine
    # path deliberately stops the still-live job. Both official terminal states preserve the
    # contract under test; no other state may satisfy this oracle.
    elif (remote not in ("SUCCEEDED", "STOPPED")
            or "artifact_contract_invalid" not in (final.error or "")
            or "TerminalResultMissing" in (final.error or "")):
        raise AssertionError(
            f"corrupt-result check did not fail on the artifact contract: remote={remote}, "
            f"error={final.error!r}"
        )
    _assert_exactly_once(status.run_id, _graph(mode).id, "failed")
    job = _job_envelope(status.run_id)
    physical_uri = job["sink_contracts"]["write"]["physical_uri"]
    if metadb.catalog_get(physical_uri) is not None:
        raise AssertionError(f"{mode} result exposed an untrusted catalog output")
    _assert_abandoned_sink(status.run_id, job)
    print(json.dumps({
        "phase": mode, "run_id": status.run_id, "submission_id": submission_id,
        "status": final.status, "ray_status": remote, "error": final.error,
    }, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=(
        "bootstrap-storage", "submit-restart", "recover-restart", "cancel", "missing", "corrupt",
    ))
    args = parser.parse_args()
    if args.phase == "bootstrap-storage":
        bootstrap_storage()
    elif args.phase == "submit-restart":
        submit_restart()
    elif args.phase == "recover-restart":
        recover_restart()
    elif args.phase == "cancel":
        cancel()
    else:
        bad_result(args.phase)


if __name__ == "__main__":
    main()
