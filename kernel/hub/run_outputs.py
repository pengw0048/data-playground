"""Shared named-output contract helpers.

This module intentionally supports the #263 single-publication execution boundary.  It validates and
persists the collection shape now, while multi-output materialization/ownership remains fail-closed
until the dedicated local and subprocess state-machine changes land.
"""

from __future__ import annotations

from typing import Any

from hub import graph as g
from hub.models import CompilePlan, Graph, RunOutput, RunStatus


class UnsupportedRunOutputs(ValueError):
    """The declared output shape cannot be published by the current atomic runner contract."""


_MAX_OUTPUT_ERROR_LENGTH = 4096


def _bounded_output_error(error: str | None) -> str | None:
    """Keep a terminal output valid even when the execution error itself is unbounded."""
    return error[:_MAX_OUTPUT_ERROR_LENGTH] if error is not None else None


def effective_run_target(plan: CompilePlan, requested: str | None) -> str | None:
    """Return the output identity for a run without guessing across independent branches."""
    if requested is not None:
        return requested
    if plan.target_node_id is not None:
        return plan.target_node_id
    writes = [step.node_id for step in plan.steps if step.kind == "write"]
    return writes[0] if len(writes) == 1 else None


def preflight_run_output_target(
        plan: CompilePlan, requested: str | None) -> str | None:
    """Resolve one public output identity or reject an ambiguous multi-write run.

    ``requested`` remains the execution-cone selector elsewhere; callers must not replace it with the
    returned output identity because ``None`` means execute the complete topological graph.
    """
    writes = [step.node_id for step in plan.steps if step.kind == "write"]
    if len(writes) > 1:
        raise UnsupportedRunOutputs(
            "full runs do not yet support multiple write outputs; select one target")
    return effective_run_target(plan, requested)


def expected_run_outputs(
        graph: Graph, node_id: str, node_specs: dict) -> list[RunOutput]:
    """Snapshot the declaration-ordered expected outputs for one run target."""
    node = g.node_map(graph).get(node_id)
    if node is None:
        raise ValueError(f"target node '{node_id}' does not exist")
    publication_kind = "catalog" if node.type == "write" else "result"
    return [RunOutput(
        node_id=node_id,
        port_id=port.id,
        port_label=port.label,
        wire=port.wire,
        publication_kind=publication_kind,
        outcome="pending",
    ) for port in g.effective_output_ports(graph, node_id, node_specs)]


def require_single_run_output(
        graph: Graph, node_id: str, node_specs: dict) -> RunOutput:
    outputs = expected_run_outputs(graph, node_id, node_specs)
    if len(outputs) != 1:
        raise ValueError(
            f"node '{node_id}' has {len(outputs)} outputs; this backend does not yet support "
            "multi-output materialization")
    return outputs[0]


def initialize_run_outputs(
        status: RunStatus, graph: Graph, node_id: str | None, node_specs: dict) -> None:
    if status.job_type == "profile":
        status.outputs = []
        return
    status.outputs = ([require_single_run_output(graph, node_id, node_specs)]
                      if node_id is not None else [])


def sole_output(status: RunStatus, *, committed: bool = False) -> RunOutput | None:
    if len(status.outputs) != 1:
        return None
    output = status.outputs[0]
    if committed and output.outcome != "committed":
        return None
    return output


def committed_output_snapshot(
        status: RunStatus, *, uri: str, rows: int, table: str | None = None) -> RunOutput:
    """Build and validate a committed snapshot without making it public."""
    expected = sole_output(status)
    if expected is None:
        raise RuntimeError("run does not have exactly one expected output")
    return RunOutput(
        node_id=expected.node_id,
        port_id=expected.port_id,
        port_label=expected.port_label,
        wire=expected.wire,
        publication_kind=expected.publication_kind,
        outcome="committed",
        uri=uri,
        table=table,
        rows=rows,
    )


def preflight_output_table(status: RunStatus, table: str) -> None:
    """Validate the known catalog identity against the actual expected output before effects."""
    committed_output_snapshot(
        status, uri="dp-preflight://run-output", rows=0, table=table)


def commit_output(
        status: RunStatus, *, uri: str, rows: int, table: str | None = None) -> RunOutput:
    committed = committed_output_snapshot(status, uri=uri, rows=rows, table=table)
    status.outputs = [committed]
    return committed


def settle_uncommitted_outputs(
        status: RunStatus, outcome: str, error: str | None = None) -> None:
    """Settle only unpublished ports; a committed publication is never relabelled or hidden."""
    if outcome not in ("failed", "skipped", "cancelled"):
        raise ValueError(f"invalid non-committed output outcome '{outcome}'")
    settled: list[RunOutput] = []
    for output in status.outputs:
        if output.outcome == "committed":
            settled.append(output)
            continue
        settled.append(RunOutput(
            node_id=output.node_id,
            port_id=output.port_id,
            port_label=output.port_label,
            wire=output.wire,
            publication_kind=output.publication_kind,
            outcome=outcome,
            error=_bounded_output_error(error),
        ))
    status.outputs = settled


def discard_unpublished_outputs(
        status: RunStatus, outcome: str, error: str | None = None) -> None:
    """Remove provisional identities after the caller proved publication did not commit."""
    if outcome not in ("pending", "failed", "skipped", "cancelled"):
        raise ValueError(f"invalid unpublished output outcome '{outcome}'")
    status.outputs = [RunOutput(
        node_id=output.node_id,
        port_id=output.port_id,
        port_label=output.port_label,
        wire=output.wire,
        publication_kind=output.publication_kind,
        outcome=outcome,
        error=_bounded_output_error(error),
    ) for output in status.outputs]


def outputs_cache_document(status: RunStatus) -> dict:
    output = sole_output(status, committed=True)
    if output is None or output.rows is None:
        raise RuntimeError(
            "only one committed output with a known row count can enter the result cache")
    return {"outputs": [output.model_dump()]}


def outputs_from_document(raw: Any) -> list[RunOutput]:
    if not isinstance(raw, dict) or set(raw) != {"outputs"}:
        return []
    values = raw.get("outputs")
    if not isinstance(values, list) or len(values) > 64:
        return []
    try:
        outputs = [RunOutput.model_validate(value) for value in values]
    except (TypeError, ValueError):
        return []
    keys = [(output.node_id, output.port_id) for output in outputs]
    return outputs if len(keys) == len(set(keys)) else []


def sole_committed_document_output(raw: Any) -> RunOutput | None:
    outputs = outputs_from_document(raw)
    if len(outputs) != 1 or outputs[0].outcome != "committed":
        return None
    return outputs[0]


def apply_cached_output(status: RunStatus, raw: Any) -> RunOutput | None:
    cached = sole_committed_document_output(raw)
    expected = sole_output(status)
    if cached is None or cached.rows is None or expected is None:
        return None
    if ((cached.node_id, cached.port_id, cached.port_label,
         cached.wire, cached.publication_kind)
            != (expected.node_id, expected.port_id, expected.port_label,
                expected.wire, expected.publication_kind)):
        return None
    return commit_output(
        status, uri=str(cached.uri), table=cached.table, rows=int(cached.rows))
