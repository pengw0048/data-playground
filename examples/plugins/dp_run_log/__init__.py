"""Reference plugin — a telemetry sink that appends each finished run to a JSONL log.

Shows the `reg.add_telemetry_sink` seam. Core ships NO exporter (offline-first): observability is a
plugin. This one appends one JSON line per finished run; an OTel / StatsD / warehouse exporter is the
SAME shape — swap the file append for an OTLP export or an INSERT. The sink receives a normalized
record per finished run:

    {canvas_id, run_id, job_type, status, rows, ms, error, outputs, placement,
     per_node: [{node_id, label, status, rows, ms}, ...]}

A sink that raises is caught by the core and logged, never failing the run. Delivery is best-effort
through a finite asynchronous queue, so a slow filesystem cannot delay run completion.

Config (dataplay.toml [[config]] → Settings → Plugins, or the DP_RUN_LOG env var): `path`, the log
file. Default: `run-telemetry.jsonl` in the process working directory.
"""

from __future__ import annotations

import json


def register(reg) -> None:
    path = reg.config("path", "run-telemetry.jsonl")

    def sink(record: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    reg.add_telemetry_sink(sink)
