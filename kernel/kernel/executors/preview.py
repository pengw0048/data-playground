"""Sample-preview (PRD §5.6 / FR-6.1) — run source→node on a bounded sample, off the full run.

Uses the SAME lowering as a full run, with the source bounded to a preview scan budget, so the
rows you see are faithful to what runs at scale. Stops honestly at non-previewable stages (P8),
and distinguishes an honest "needs a full pass" from a real error (bad cell/query/graph).
"""

from __future__ import annotations

from kernel import db, graph as g
from kernel.executors.engine import LoweringEngine, NotPreviewable
from kernel.models import Graph, SampleResult
from kernel.sandbox import run_with_timeout

PREVIEW_SCAN = 2000       # rows read at each source during preview (bounds transforms too)
PREVIEW_BUDGET_S = 8.0


def preview_node(graph: Graph, node_id: str, k: int, resolve_adapter, registry,
                 node_lowerings=None, node_specs=None) -> SampleResult:
    # clean, up-front graph checks (don't rely on a Python RecursionError for cycles)
    if not g.is_acyclic(graph):
        return SampleResult(error=True, reason="graph has a cycle — control flow must be encapsulated (§5.7)")
    if node_specs:
        errs = g.type_errors(graph, node_specs)
        if errs:
            return SampleResult(error=True, reason="incompatible connection: " + "; ".join(errs[:3]))

    engine = LoweringEngine(graph, resolve_adapter, registry, sample_k=PREVIEW_SCAN, full=False,
                            node_lowerings=node_lowerings, node_specs=node_specs)

    def work() -> SampleResult:
        # serialize all DuckDB access; drop the temp views this eval minted
        with db.lock():
            try:
                rows, cols = engine.rows(node_id, k)
            finally:
                db.drop_created_views()
        return SampleResult(columns=cols, rows=rows, row_count=len(rows), truncated=True)

    try:
        return run_with_timeout(work, PREVIEW_BUDGET_S)
    except NotPreviewable as e:
        return SampleResult(not_previewable=True, reason=e.reason)     # honest P8 state
    except Exception as e:  # noqa: BLE001
        return SampleResult(error=True, reason=f"{type(e).__name__}: {e}")  # a real failure
