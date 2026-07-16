"""Installed-wheel conformance checks for one documented plugin capability.

Run this module from an environment containing a Data Playground wheel plus a plugin wheel.  It discovers
the plugin exclusively through the ``dataplay.plugins`` entry-point group, verifies activation, sends one
finished-run telemetry record, and shuts down every sink worker before returning.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import uuid
from pathlib import Path


def _failure(stage: str, message: str) -> int:
    # Do not include loader exception text or configured paths: either could contain a secret reference.
    print(f"{stage}: {message}", file=sys.stderr)
    return 1


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m hub.plugin_conformance",
        description="Verify an installed Data Playground telemetry plugin through its entry point.",
    )
    parser.add_argument("plugin", help="installed dataplay.plugins entry-point name")
    parser.add_argument("--workspace", required=True, help="empty workspace used only for this check")
    parser.add_argument("--telemetry-log", required=True, help="JSONL output expected from the telemetry plugin")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    workspace = Path(args.workspace).resolve()
    data_dir = workspace / "data"
    telemetry_log = Path(args.telemetry_log).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    telemetry_log.parent.mkdir(parents=True, exist_ok=True)
    # The required paths define the isolated check.  Ambient runtime configuration must not redirect
    # metadata into another database or activate a source/configured plugin instead of the wheel entry point.
    os.environ["DP_WORKSPACE"] = str(workspace)
    os.environ["DP_DATA_DIR"] = str(data_dir)
    os.environ.pop("DP_DATABASE_URL", None)
    os.environ.pop("DP_PLUGINS", None)
    os.environ["DP_RUN_LOG"] = str(telemetry_log)

    from hub import metadb
    from hub.deps import Deps, _persist_run
    from hub.models import Graph, RunStatus
    from hub.observability import clear_sinks, drain_sinks

    previous_log_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    result = 1
    try:
        # A plugin loader may print its raw exception. Capture it so conformance failures remain redacted.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                metadb.init_db()
                deps = Deps(str(workspace), str(data_dir), maintain_storage=False)
        except Exception:  # noqa: BLE001 — conformance reports the stage, never plugin-provided detail
            return _failure("activation", "entry point could not be checked")
        installed = next((entry for entry in deps.plugins if entry.get("name") == args.plugin), None)
        if installed is None or installed.get("error"):
            return _failure("activation", "entry point did not activate")
        if not deps.telemetry_sinks:
            return _failure("capability", "entry point did not register a telemetry sink")

        probe_id = f"plugin-conformance-{uuid.uuid4().hex}"
        graph = Graph(id="plugin-conformance", version=1, nodes=[], edges=[])
        status = RunStatus(run_id=probe_id, status="done", job_type="run", per_node=[])
        _persist_run(deps, graph, None, status)
        if not drain_sinks():
            return _failure("capability", "telemetry delivery did not drain")
        try:
            records = [json.loads(line) for line in telemetry_log.read_text().splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return _failure("capability", "telemetry sink did not produce a valid JSONL record")
        if not any(record.get("run_id") == status.run_id for record in records):
            return _failure("capability", "telemetry sink did not receive the finished run")
        print("plugin conformance passed")
        result = 0
    except Exception:  # noqa: BLE001 — keep capability failures normalized and configuration-free
        return _failure("capability", "telemetry capability check failed")
    finally:
        if not clear_sinks():
            print("cleanup: sink workers did not stop", file=sys.stderr)
            result = 1
        logging.disable(previous_log_disable)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
