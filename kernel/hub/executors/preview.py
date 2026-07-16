"""Sample-preview — run source→node on a bounded sample, off the full run.

Uses the SAME build as a full run, with the source bounded to a preview scan budget, so the
rows you see are faithful to what runs at scale. Stops honestly at non-previewable stages (P8),
and distinguishes an honest "needs a full pass" from a real error (bad cell/query/graph).
"""

from __future__ import annotations

import uuid

from hub import db, graph as g
from hub.executors.engine import BuildEngine, NotPreviewable
from hub.models import Graph, SampleResult
from hub.sampling import provenance_for_graph
from hub.sandbox import run_with_timeout
from hub.storage import ManagedSourceReadError

PREVIEW_SCAN = 2000       # rows read at each source during preview (bounds transforms too)
PREVIEW_BUDGET_S = 8.0

# node kinds that run an ARBITRARY user Python cell in-process — the thread-based preview timeout can
# interrupt an in-flight DuckDB query but CANNOT kill a runaway `while True:` in such a cell (no
# thread-kill in Python), so in multi-user mode we refuse to preview/profile them (P0-EXEC-02). Runs
# execute in a killable, deadline-bounded child instead. `section` runs a user driver script via exec
# (section.run_section), so it belongs here too. (vector-search is pure SQL/Lance — interruptible — so
# it is NOT here.)
_CODE_CELL_KINDS = ("transform", "section")
_LOCAL_SAMPLE_ADAPTERS = {"duckdb", "lance"}


def _reservoir_preview_allowed(graph: Graph, node_id: str, resolve_adapter) -> bool:
    """Only a Sample node over current local adapters may turn its preview into a full scan."""
    nodes = {node.id: node for node in g.upstream_chain(graph, node_id)}
    target = nodes.get(node_id)
    if target is None or target.type != "sample":
        return False
    sources = [node for node in nodes.values() if node.type == "source"]
    if not sources:
        return False
    for source in sources:
        config = source.data.get("config", {}) if isinstance(source.data, dict) else {}
        uri = config.get("uri") if isinstance(config, dict) else None
        try:
            if not uri or getattr(resolve_adapter(uri), "name", None) not in _LOCAL_SAMPLE_ADAPTERS:
                return False
        except Exception:
            return False
    return True


def _reservoir_source_total(graph: Graph, node_id: str, resolve_adapter) -> int | None:
    """Read exact bounded metadata only for a direct source → Sample graph, never by scanning."""
    chain = g.upstream_chain(graph, node_id)
    if len(chain) != 2 or chain[0].type != "source" or chain[1].type != "sample":
        return None
    config = chain[0].data.get("config", {}) if isinstance(chain[0].data, dict) else {}
    uri = config.get("uri") if isinstance(config, dict) else None
    try:
        count = getattr(resolve_adapter(uri), "metadata_count", None) if uri else None
        value = count(uri) if callable(count) else None
        return int(value) if value is not None else None
    except Exception:  # unknown metadata is a truthful state, not a preview error
        return None


def preview_node(graph: Graph, node_id: str, k: int, resolve_adapter, registry,
                 node_builders=None, node_specs=None, offset: int = 0, cache=None,
                 storage=None, port_id: str | None = None) -> SampleResult:
    # clean, up-front graph checks (don't rely on a Python RecursionError for cycles)
    if not g.is_acyclic(graph):
        return SampleResult(error=True, reason="graph has a cycle — control flow must be encapsulated (§5.7)")
    if node_specs:
        errs = g.type_errors(graph, node_specs)
        if errs:
            return SampleResult(error=True, reason="incompatible connection: " + "; ".join(errs[:3]))
        try:
            selected_port = g.require_output_port(graph, node_id, node_specs, port_id).id
        except (KeyError, ValueError) as exc:
            return SampleResult(error=True, reason=str(exc).strip("'"))
    else:
        selected_port = port_id

    from hub import auth
    if auth.auth_enabled() and any(n.type in _CODE_CELL_KINDS for n in g.upstream_chain(graph, node_id)):
        return SampleResult(not_previewable=True, reason=(
            "preview of a Python cell is disabled in multi-user mode — the in-process timeout can't kill "
            "a runaway cell; run it (runs execute in a killable, deadline-bounded child)"))

    reservoir_preview = _reservoir_preview_allowed(graph, node_id, resolve_adapter)
    engine = BuildEngine(graph, resolve_adapter, registry, sample_k=PREVIEW_SCAN, full=False,
                            node_builders=node_builders, node_specs=node_specs,
                            warm=cache, warm_scope="preview", reservoir_preview=reservoir_preview)

    holder: dict = {}  # published by the worker thread so the timeout can interrupt its cursor

    def work() -> SampleResult:
        # run on our OWN cursor (created on THIS worker thread so its thread-local binding is correct),
        # not the process-global lock — a slow preview no longer blocks other users' work
        from hub.storage import source_read_scope
        with source_read_scope(
                storage, g.all_upstream_source_uris(graph, node_id),
                owner=f"preview:{uuid.uuid4().hex}"):
            with db.run_scope() as scope:
                holder["scope"] = scope
                # fetch one extra row to know if a NEXT page exists (so the UI can disable Next at the
                # true end, even when the total is an exact multiple of the page size). NOTE: offset
                # pagination assumes a stable row order; a join/aggregate result is unordered, so pages
                # over such a node may not be perfectly consistent — acceptable for a bounded preview.
                rows, cols = engine.rows(node_id, k + 1, offset, selected_port)
                has_more = len(rows) > k
                target = next((node for node in graph.nodes if node.id == node_id), None)
                config = target.data.get("config", {}) if target and isinstance(target.data, dict) else {}
                raw_n = config.get("n") if isinstance(config, dict) else None
                sample_n = max(0, int(raw_n if raw_n is not None else PREVIEW_SCAN))
                if reservoir_preview:
                    total = _reservoir_source_total(graph, node_id, resolve_adapter)
                    provenance = provenance_for_graph(
                        graph, node_id, resolve_adapter, strategy="reservoir",
                        seed=int(config.get("seed", 42)), requested_rows=sample_n,
                        scanned_rows=total, returned_rows=len(rows[:k]), total_rows=total,
                        limitations=[
                            ("A reservoir sample scanned the complete local input."
                             if total is not None else
                             "A reservoir sample scans the complete local input; total population size is unknown."),
                            "Membership is deterministic for this input revision and seed.",
                        ],
                    )
                else:
                    provenance = provenance_for_graph(
                        graph, node_id, resolve_adapter, strategy="prefix", seed=None,
                        requested_rows=k, scanned_rows=None, returned_rows=len(rows[:k]), total_rows=None,
                        limitations=[
                            f"Each source was bounded to at most {PREVIEW_SCAN} rows; this prefix is not representative or random.",
                        ],
                    )
                return SampleResult(
                    columns=cols,
                    rows=rows[:k],
                    # A graph preview observes only the bounded source prefix. ``rows`` already gives
                    # the page size; publishing it as rowCount made consumers mistake it for a total.
                    row_count=None,
                    has_more=has_more,
                    truncated=True,
                    completeness="sample",
                    row_limit=None if reservoir_preview else PREVIEW_SCAN,
                    limit_reason=None if reservoir_preview else "preview-scan",
                    limit_scope=None if reservoir_preview else "each-source",
                    sample_provenance=provenance,
                )

    def on_timeout() -> None:
        # interrupt THIS preview's cursor so the worker unwinds (its scope exit drops its views);
        # interrupting the base connection would NOT stop a query running on the cursor
        sc = holder.get("scope")
        (sc.interrupt() if sc is not None else db.interrupt())

    try:
        return run_with_timeout(work, PREVIEW_BUDGET_S, on_timeout=on_timeout)
    except ManagedSourceReadError as e:
        return SampleResult(error=True, reason=str(e))
    except NotPreviewable as e:
        return SampleResult(not_previewable=True, reason=e.reason)     # honest P8 state
    except Exception as e:  # noqa: BLE001
        return SampleResult(error=True, reason=f"{type(e).__name__}: {e}")  # a real failure
