"""Catalog-driven join hints — how two datasets can join, and at what cardinality.

The catalog knows each dataset's schema and its candidate keys (id-like columns). Given two
datasets, we match key columns by name + compatible type, then MEASURE cardinality directly on the
data (DuckDB `count` vs `count(distinct …)`) rather than asking a human to declare it — a dataset
whose key is unique is the parent (1) side, a non-unique key is the child (N) side. This is the
generic, provider-agnostic version of a table-relationship model: no PK/FK constraints required,
just the columns + the measured facts.

Composite keys are first-class: a candidate key is a column SET, and uniqueness / cardinality are
measured over the tuple. Matching pairs single- and multi-column key sets between the two sides.
"""

from __future__ import annotations

import uuid
from itertools import combinations

from hub import db
from hub import graph as g
from hub.grain import grain_of
from hub.models import (
    ColumnSchema, Graph, JoinAnalysis, JoinSuggestion, KeyInfo, RowReferenceInputIdentity,
    dataset_ref_identity,
)
from hub.plugins.capabilities import display_base_type, is_key_column
from hub.row_reference_diagnosis import (
    ROW_REFERENCE_TARGET_MISMATCH, diagnose_key_pairs, has_target_conflict, input_identity,
)
from hub.sqlpolicy import (
    SQLPolicyError, identifier, identifier_key, join_equality_columns, parse_identifier_list,
    quote_identifier,
)

# a join key column set is at most this wide — a wider composite is almost never a real join key and
# the combinatorics (C(n,k)) would explode.
_MAX_KEY_WIDTH = 3

# process-level uniqueness cache: (uri, cols, fingerprint) -> (unique, n). Best-effort — the
# fingerprint keys it to the data version, so a changed file re-measures; bounded, safe to drop.
_UNIQUE_CACHE: dict[tuple, tuple[bool | None, int]] = {}


# --------------------------------------------------------------------------- #
# key candidates (by name; uniqueness is MEASURED, not assumed)
# --------------------------------------------------------------------------- #
def key_columns(columns: list[ColumnSchema]) -> list[str]:
    """The id-like columns of a schema, in a stable order (a column literally named `id`/`uuid`
    first, then `*_id`/`*_key` suffixes) — the raw material for key candidates."""
    keys = [c.name for c in columns if is_key_column(c)]
    keys.sort(key=lambda n: (0 if n.lower() in ("id", "uuid", "guid", "pk") else 1, n.lower()))
    return keys


def key_candidates(columns: list[ColumnSchema]) -> list[KeyInfo]:
    """Inferred (name-based, unmeasured) primary-key candidates for a dataset — each key-like column
    as a single-column candidate. Composite candidates are formed at join time against the other
    side (a lone junction-table column isn't a useful PK candidate on its own)."""
    return [KeyInfo(columns=[c], confidence="inferred") for c in key_columns(columns)]


# --------------------------------------------------------------------------- #
# measurement (the open-source advantage: we can just look at the data)
# --------------------------------------------------------------------------- #
def measure_unique(uri: str, cols: list[str], resolve_adapter) -> tuple[bool | None, int]:
    """(is the column set unique across rows?, row count). Unique iff count(DISTINCT key) == count(*)
    — a NULL key (excluded by DISTINCT) or any duplicate makes it non-unique, so it's not a clean
    join key. count and distinct are computed in ONE aggregate pass (a single scan): an adapter whose
    scan is a one-shot Arrow reader — Lance — would otherwise be drained by the first pass and report
    every key non-unique. Runs on its own cursor (run_scope), so a big scan doesn't hold the base lock
    and stall other previews. Returns (None, n) when the data/columns can't be read (→ 'unknown'
    cardinality, never a false 'not unique') and (None, 0) for empty data (cardinality is moot)."""
    try:
        adapter = resolve_adapter(uri)
        # cache by (uri, cols, fingerprint) so the Inspector's debounced re-fires (typing on the
        # canvas re-triggers join-analysis) don't re-scan the same data; the fingerprint (path
        # mtime/size for local, uri for object/lance-version) invalidates the entry when data changes.
        key = None
        try:
            key = (uri, tuple(cols), adapter.fingerprint(uri))
            if key in _UNIQUE_CACHE:
                return _UNIQUE_CACHE[key]
        except Exception:  # noqa: BLE001 — adapter without fingerprint → just skip the cache
            key = None
        with db.run_scope():
            selected = adapter.scan(uri, columns=cols)
            canonical = [identifier(c, selected.columns, label="relationship key") for c in cols]
            quoted = ", ".join(quote_identifier(c) for c in canonical)
            if len(canonical) > 1:
                # a composite (a,b) struct is non-null even when a field is NULL, so
                # count(DISTINCT (a,b)) would count a null-bearing tuple as a distinct value.
                notnull = " AND ".join(f"{quote_identifier(c)} IS NOT NULL" for c in canonical)
                dexpr = f"count(DISTINCT ({quoted})) FILTER (WHERE {notnull})"
            else:
                dexpr = f"count(DISTINCT {quoted})"  # excludes NULLs already
            n, d = selected.aggregate(f"count(*) AS n, {dexpr} AS d").fetchone()
        n = int(n)
        result = ((d == n) if n else None, n)
        if key is not None:
            if len(_UNIQUE_CACHE) > 512:  # coarse bound — measurements are cheap to recompute
                _UNIQUE_CACHE.clear()
            _UNIQUE_CACHE[key] = result
        return result
    except Exception:  # noqa: BLE001 — unreadable / bad columns → 'unknown', don't crash hints
        return (None, 0)


def cardinality(left_unique: bool | None, right_unique: bool | None) -> str:
    """Join cardinality left:right from each side's key uniqueness. A unique key is the '1' side.
    Either side unknown (couldn't measure) → 'unknown', never a guess."""
    if left_unique is None or right_unique is None:
        return "unknown"
    if left_unique and right_unique:
        return "1:1"
    if left_unique:
        return "1:N"          # one left row → many right rows
    if right_unique:
        return "N:1"
    return "N:M"


def measured_unique(uri: str, resolve_adapter):
    """A uniqueness oracle that MEASURES a column set on a dataset uri (for two catalog datasets)."""
    return lambda cols: measure_unique(uri, cols, resolve_adapter)[0]


def _memoize(fn):
    """Cache a uniqueness oracle by column set — the same key set is never scanned twice per call."""
    cache: dict[tuple, object] = {}

    def wrapped(cols):
        k = tuple(cols)
        if k not in cache:
            cache[k] = fn(cols)
        return cache[k]
    return wrapped


# --------------------------------------------------------------------------- #
# join suggestions
# --------------------------------------------------------------------------- #
def _matchable(a: ColumnSchema, b: ColumnSchema) -> bool:
    """Two columns can be a join key pair: same normalized name (or one is the other's `*_id`
    form) and a compatible base type."""
    if display_base_type(a.type) != display_base_type(b.type):
        return False
    an, bn = a.name.lower(), b.name.lower()
    if an == bn:
        return True
    # a FK naming match: a bare `id`/`uuid` on one side ↔ `<thing>_id` on the other
    # (e.g. users.id ↔ events.user_id). Requires the underscore so `id` doesn't match `grid`.
    _bare = ("id", "uuid", "guid", "pk")
    return (an in _bare and bn.endswith("_" + an)) or (bn in _bare and an.endswith("_" + bn))


def _column_matches(left: list[ColumnSchema], right: list[ColumnSchema]) -> list[tuple[str, str]]:
    """All (left_col, right_col) pairs that could be a join key, restricted to key-like columns so
    we don't propose joining on an arbitrary shared value column."""
    lkeys = {c.name for c in left if is_key_column(c)}
    rkeys = {c.name for c in right if is_key_column(c)}
    pairs: list[tuple[str, str]] = []
    for a in left:
        if a.name not in lkeys:
            continue
        for b in right:
            if b.name in rkeys and _matchable(a, b):
                pairs.append((a.name, b.name))
    return pairs


# beyond this many single matches we don't form composites — C(n,k) scans would blow up and 5+
# id-named columns joining two tables is not a real composite key anyway.
_MAX_MATCHES_FOR_COMPOSITE = 4


def _candidate_keysets(left_cols: list[ColumnSchema],
                       right_cols: list[ColumnSchema]) -> list[list[tuple[str, str]]]:
    """Candidate join key sets between two schemas: each single matched (left, right) column pair,
    plus composites (up to width 3) of the matched columns. Drops key sets that reuse a column."""
    pairs = _column_matches(left_cols, right_cols)
    cands: list[list[tuple[str, str]]] = [[p] for p in pairs]
    if len(pairs) <= _MAX_MATCHES_FOR_COMPOSITE:  # bound the combinatorics (each candidate = 2 scans)
        for w in range(2, min(_MAX_KEY_WIDTH, len(pairs)) + 1):
            cands.extend([list(c) for c in combinations(pairs, w)])
    out, seen = [], set()
    for cand in cands:
        lc, rc = [p[0] for p in cand], [p[1] for p in cand]
        if len(set(lc)) != len(lc) or len(set(rc)) != len(rc):
            continue
        key = (tuple(lc), tuple(rc))
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out


def _reference_candidate_keysets(
        left_cols: list[ColumnSchema],
        right_cols: list[ColumnSchema],
) -> list[list[tuple[str, str]]]:
    """Synthesize bounded renamed-key candidates from retained row-reference facts.

    One scalar reference gives a complete mapping only for a single target key field.  Composite
    references do not carry the corresponding local-field sequence, so they remain available to
    configured/declared joins but are never guessed here.
    """
    out: list[list[tuple[str, str]]] = []

    def append(source: list[ColumnSchema], peer: list[ColumnSchema], *, flipped: bool) -> None:
        for source_column in source:
            reference = source_column.row_reference
            if reference is None or len(reference.key_fields) != 1:
                continue
            key = identifier_key(reference.key_fields[0])
            matches = [column for column in peer if identifier_key(column.name) == key]
            if len(matches) != 1:
                continue
            peer_column = matches[0]
            if display_base_type(source_column.type) != display_base_type(peer_column.type):
                continue
            pair = ((peer_column.name, source_column.name) if flipped
                    else (source_column.name, peer_column.name))
            out.append([pair])

    append(left_cols, right_cols, flipped=False)
    append(right_cols, left_cols, flipped=True)
    deduped: list[list[tuple[str, str]]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for candidate in out:
        key = tuple(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def suggest_joins(left_cols: list[ColumnSchema], right_cols: list[ColumnSchema],
                  left_unique, right_unique,
                  left_identity: RowReferenceInputIdentity | None = None,
                  right_identity: RowReferenceInputIdentity | None = None) -> list[JoinSuggestion]:
    """Ranked ways to join, given a uniqueness oracle for each side (`fn(cols) -> bool | None`).
    left_unique/right_unique either MEASURE on a dataset (measured_unique) or decide from a
    canvas node's grain. Cardinality + a 'verified' confidence come from those oracles."""
    left_unique, right_unique = _memoize(left_unique), _memoize(right_unique)  # each key set measured once
    out: list[JoinSuggestion] = []
    named = _candidate_keysets(left_cols, right_cols)
    reference = _reference_candidate_keysets(left_cols, right_cols)
    reference_keys = {tuple(candidate) for candidate in reference}
    named_keys = {tuple(candidate) for candidate in named}
    candidates = reference + [
        candidate for candidate in named if tuple(candidate) not in reference_keys]
    for cand in candidates:
        lc, rc = [p[0] for p in cand], [p[1] for p in cand]
        references = diagnose_key_pairs(
            left_input=left_identity, right_input=right_identity,
            left_columns=left_cols, right_columns=right_cols,
            left_fields=lc, right_fields=rc,
        )
        # A target contradiction is stronger than a name/type/cardinality heuristic. Unknown keeps
        # legacy behavior; it is never promoted to compatible.
        if has_target_conflict(references):
            continue
        # A renamed reference creates a new candidate only after its target is positively comparable.
        # Unknown evidence cannot manufacture a join hint from the target key's name.
        if (tuple(cand) not in named_keys
                and not any(diagnosis.status == "compatible" for diagnosis in references)):
            continue
        lu, ru = left_unique(lc), right_unique(rc)
        card = cardinality(lu, ru)
        conf = "verified" if (lu is not None and ru is not None) else "inferred"
        if card == "unknown":
            reason = "matching key column(s) — cardinality not measurable here"
        elif lu or ru:
            reason = f"{'left' if lu else 'right'} key is unique ({card})"
        else:
            reason = f"neither key is unique ({card}) — a many-to-many bridge"
        exact = all(a == b for a, b in cand)
        # rank: a determinate 1:x/x:1 (has a parent side) beats a bridge; exact name beats FK-style; narrow beats wide
        score = ((2.0 if card in ("1:1", "1:N", "N:1") else 0.0)
                 + (1.0 if exact else 0.0)
                 + (3.0 if any(d.status == "compatible" for d in references) else 0.0)
                 - 0.1 * (len(cand) - 1))
        out.append(JoinSuggestion(left_columns=lc, right_columns=rc, cardinality=card,
                                  confidence=conf, score=round(score, 3), reason=reason,
                                  row_reference=references))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


# --------------------------------------------------------------------------- #
# canvas join analysis (suggestions for a join node's two inputs + fan-out warning)
# --------------------------------------------------------------------------- #
def _lone_source_uri(graph: Graph, node_id: str):
    """The single upstream source dataset feeding node_id, or None if there are zero or several (a
    join upstream) — measuring a key's uniqueness needs the one source the key actually came from."""
    srcs = [n for n in g.upstream_chain(graph, node_id)
            if n.type == "source" and (n.data.get("config", {}) if isinstance(n.data, dict) else {}).get("uri")]
    if len(srcs) != 1:
        return None
    return srcs[0].data["config"].get("uri")


def _grain_unique_oracle(graph: Graph, input_id: str, catalog, resolve_adapter):
    """Uniqueness oracle for a join INPUT (a mid-canvas relation): a group-by/dedup output is unique
    exactly on its grain (so a key containing the grain is unique); otherwise, if the input's grain
    traces unbroken to a lone source, measure the key on that source (filter/sample preserve it).
    Returns fn(cols) -> bool | None (None = not determinable → cardinality stays 'unknown')."""
    grain = grain_of(graph, input_id, catalog)
    src = _lone_source_uri(graph, input_id)

    def fn(cols: list[str]) -> bool | None:
        if grain.verified and grain.columns is not None:
            return set(grain.columns).issubset(set(cols))  # unique per group key → any superset unique
        if src is not None and grain.known:
            return measure_unique(src, cols, resolve_adapter)[0]
        return None
    return fn


def _declared_suggestions(graph: Graph, left_id: str, right_id: str, catalog) -> list[JoinSuggestion]:
    """JoinSuggestions from EVERY owner-declared relationship between the two inputs' source datasets
    (either orientation) — trusted over measurement. Returns all matches, not just the first, so two
    declared relationships on different key columns both surface."""
    lsrc, rsrc = _lone_source_uri(graph, left_id), _lone_source_uri(graph, right_id)
    if not lsrc or not rsrc:
        return []
    out: list[JoinSuggestion] = []
    for r in catalog.relationships():
        if r.left_uri == lsrc and r.right_uri == rsrc:
            out.append(JoinSuggestion(left_columns=r.left_columns, right_columns=r.right_columns,
                                      cardinality=r.cardinality, confidence="declared", score=10.0,
                                      reason="declared relationship"))
        elif r.left_uri == rsrc and r.right_uri == lsrc:  # stored the other way round → flip to left/right
            flip = {"1:N": "N:1", "N:1": "1:N"}.get(r.cardinality, r.cardinality)
            out.append(JoinSuggestion(left_columns=r.right_columns, right_columns=r.left_columns,
                                      cardinality=flip, confidence="declared", score=10.0,
                                      reason="declared relationship"))
    return out


def _configured_join_key(node) -> tuple[list[str], list[str]] | None:
    """The (left_cols, right_cols) the join node is ACTUALLY configured with: `on` (a USING list of
    same-named columns) or `condition` (`a.x = b.y AND …`, matching the engine's a/b aliasing). None
    if unconfigured (a fresh join) — then analyze_join just ranks candidates as before."""
    if node is None:
        return None
    cfg = node.data.get("config", {}) if isinstance(node.data, dict) else {}
    on = str(cfg.get("on") or "").strip()
    if on:
        try:
            cols = parse_identifier_list(on, label="join key")
        except SQLPolicyError:
            return None
        return (cols, cols) if cols else None
    cond = str(cfg.get("condition") or "").strip()
    if cond:
        try:
            return join_equality_columns(cond)
        except SQLPolicyError:
            return None
    return None


def _input_identity(graph: Graph, node_id: str, catalog) -> RowReferenceInputIdentity | None:
    """Resolve only the bounded durable identity carried by one single-source join input."""
    source_uri = _lone_source_uri(graph, node_id)
    source = next((node for node in g.upstream_chain(graph, node_id)
                   if node.type == "source" and (node.data.get("config", {}) if isinstance(node.data, dict) else {}).get("uri") == source_uri), None)
    if not source_uri:
        return None
    from hub import workspace_providers
    if workspace_providers.is_provider_dataset_uri(source_uri):
        if source is None:
            return None
        config = source.data.get("config", {}) if isinstance(source.data, dict) else {}
        try:
            dataset_id, revision_id = dataset_ref_identity(config.get("datasetRef"))
            token = source_uri.removeprefix("workspace-provider://")
            canonical_id = f"workspace-provider:{token}"
            if workspace_providers.provider_dataset_uri_for_identity(canonical_id) != source_uri:
                return None
        except (TypeError, ValueError, workspace_providers.ProviderDatasetUnavailable):
            return None
        if canonical_id != dataset_id:
            return None
        return input_identity(dataset_id=dataset_id, revision_id=revision_id)
    try:
        table = catalog.get_table(source_uri)
    except (KeyError, ValueError):
        return None
    # Ordinary catalog Sources execute the current URI registration.  A client-carried DatasetRef
    # cannot rebind that URI, and transient table ids/names/URIs are not durable identities.
    return input_identity(dataset_id=table.registration_id, revision_id=table.version)


def analyze_join(graph: Graph, node_id: str, columns_by_node: dict[str, list | None],
                 catalog, resolve_adapter, storage=None) -> JoinAnalysis:
    """Fence every managed input through all uniqueness scans."""
    from hub.storage import source_read_scope

    with source_read_scope(
            storage, g.all_upstream_source_uris(graph, node_id),
            owner=f"join-analysis:{uuid.uuid4().hex}"):
        return _analyze_join_unfenced(
            graph, node_id, columns_by_node, catalog, resolve_adapter)


def _analyze_join_unfenced(graph: Graph, node_id: str, columns_by_node: dict[str, list | None],
                           catalog, resolve_adapter) -> JoinAnalysis:
    """Rank join keys for a join node's two inputs and warn if the join fans out (not 1:1).
    columns_by_node = per-node output columns (from executors.schema.schema_for_graph)."""
    ins = g.incoming(graph, node_id)
    if len(ins) < 2:
        return JoinAnalysis(note="connect two inputs to see join suggestions")
    # The engine binds the join's SQL aliases a/b by INCOMING-EDGE order (engine `a,b = view(ins[0]),
    # view(ins[1])` over g.incoming), NOT by target_handle — so resolve left/right the same way, or a
    # suggested `a.x = b.y` condition would reference the wrong physical input.
    left, right = ins[0].source, ins[1].source
    lcols_raw, rcols_raw = columns_by_node.get(left), columns_by_node.get(right)
    if not lcols_raw or not rcols_raw:
        return JoinAnalysis(note="input columns aren't known yet (run an upstream code op to type them)")
    lcols = [ColumnSchema.model_validate(c) for c in lcols_raw]
    rcols = [ColumnSchema.model_validate(c) for c in rcols_raw]
    left_identity, right_identity = (
        _input_identity(graph, left, catalog), _input_identity(graph, right, catalog))
    lo = _grain_unique_oracle(graph, left, catalog, resolve_adapter)
    ro = _grain_unique_oracle(graph, right, catalog, resolve_adapter)
    suggestions = suggest_joins(
        lcols, rcols, lo, ro, left_identity=left_identity, right_identity=right_identity)
    # DECLARED relationships between the two inputs' source datasets lead (owner-asserted, trusted).
    # A declared edge with cardinality 'unknown' borrows the MEASURED cardinality for the same columns
    # so the fan-out warning still fires (declaring a join shouldn't hide that it multiplies rows).
    declared = _declared_suggestions(graph, left, right, catalog)
    cols_key = lambda s: (tuple(s.left_columns), tuple(s.right_columns))  # noqa: E731
    measured_by_cols = {cols_key(s): s.cardinality for s in suggestions}
    compatible_declared = []
    for d in declared:
        d.row_reference = diagnose_key_pairs(
            left_input=left_identity, right_input=right_identity,
            left_columns=lcols, right_columns=rcols,
            left_fields=d.left_columns, right_fields=d.right_columns,
        )
        if has_target_conflict(d.row_reference):
            continue
        if d.cardinality == "unknown":
            d.cardinality = measured_by_cols.get(cols_key(d), "unknown")
        compatible_declared.append(d)
    declared = compatible_declared
    declared_cols = {cols_key(d) for d in declared}
    suggestions = declared + [s for s in suggestions if cols_key(s) not in declared_cols]
    # If the join is already CONFIGURED (on / condition), the warning must reflect the key it ACTUALLY
    # uses — not the top-ranked candidate. Surface that key's cardinality first (measuring it if it
    # isn't among the suggestions), so `validate`'s all-clear can't be a different key's cardinality.
    configured = _configured_join_key(g.node_map(graph).get(node_id))
    configured_references = []
    if configured:
        cl, cr = configured
        configured_references = diagnose_key_pairs(
            left_input=left_identity, right_input=right_identity,
            left_columns=lcols, right_columns=rcols, left_fields=cl, right_fields=cr,
        )
        if has_target_conflict(configured_references):
            return JoinAnalysis(
                suggestions=suggestions, configured_row_reference=configured_references,
                blocking_code=ROW_REFERENCE_TARGET_MISMATCH,
                note="configured join key has a known row-reference target mismatch",
            )
        active = next((s for s in suggestions if s.left_columns == cl and s.right_columns == cr), None)
        if active is None:
            card = cardinality(lo(cl), ro(cr))
            active = JoinSuggestion(left_columns=cl, right_columns=cr, cardinality=card,
                                    confidence="verified" if card != "unknown" else "inferred",
                                    reason="configured join key", row_reference=configured_references)
        suggestions = [active] + [s for s in suggestions if s is not active]
    if not suggestions:
        return JoinAnalysis(note="no matching key columns between the two inputs",
                            configured_row_reference=configured_references)
    warning = None
    top = suggestions[0]
    if top.cardinality in ("1:N", "N:1", "N:M"):
        many = "both sides" if top.cardinality == "N:M" else ("right" if top.cardinality == "1:N" else "left")
        warning = (f"this join is {top.cardinality}: {many} fans out, so the result is at the finer "
                   "grain — rows multiply. Aggregate downstream if you meant the parent grain.")
    return JoinAnalysis(suggestions=suggestions, warning=warning,
                        configured_row_reference=configured_references)
