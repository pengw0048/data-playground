"""Workspace-level AgentDataPolicy for LLM tool egress (SEC-01).

Hosted models default to metadata-only tools: catalog/schema identity may leave the
deployment, but sample row values must not. An admin can opt into sample-values, or mark a
configured OpenAI-compatible endpoint (DP_AGENT_BASE_URL) as local. Enforcement is centralized
in ``sanitize_tool_result`` so individual tools cannot accidentally bypass the policy.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

POLICY_SETTING_KEY = "agentDataPolicy"
LEVEL_METADATA_ONLY = "metadata-only"
LEVEL_SAMPLE_VALUES = "sample-values"
VALID_LEVELS = frozenset({LEVEL_METADATA_ONLY, LEVEL_SAMPLE_VALUES})

# Tools that read the shared catalog / execute over sample data — audited under hosted models.
CATALOG_READING_TOOLS = frozenset({"list_catalog", "preview", "join_hints"})

_VALUE_REFUSAL = (
    "metadata-only: sample row values are withheld under the workspace AgentDataPolicy. "
    "An administrator can enable sample-values, or mark the configured endpoint as local."
)

_AUDIT_ID_MAX_BYTES = 256
_AUDIT_QUERY_MAX_BYTES = 128
_CATALOG_AUDIT_MODES = frozenset({"list", "lexical", "semantic", "hybrid"})
_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)(?:\b(?:password|secret|api[_-]?key|credential|authorization)\s*[:=]\s*\S+"
    r"|\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9_-]{8,}"
    r"|\bgithub_pat_[A-Za-z0-9_-]{8,})"
)


@dataclass(frozen=True)
class AgentDataPolicy:
    """Resolved egress policy for one agent run."""

    level: str
    endpoint_is_local: bool
    model: str
    provider: str
    base_url: str | None
    hosted: bool
    allows_sample_values: bool

    def disclosure(self) -> dict:
        """Fields the UI needs for preflight disclosure before the first tool call."""
        return {
            "provider": self.provider,
            "model": self.model,
            "level": self.level,
            "endpointIsLocal": self.endpoint_is_local,
            "hosted": self.hosted,
            "rowValuesMayLeave": self.allows_sample_values,
        }


def provider_of(model: str, base_url: str | None) -> str:
    if base_url:
        return "openai-compatible"
    token = (model or "").replace(":", "/", 1)
    if "/" in token:
        return token.split("/", 1)[0].lower() or "unknown"
    return "unknown"


def normalize_policy_value(raw: Any) -> dict:
    """Normalize a stored/UI policy value into a canonical dict."""
    if isinstance(raw, str):
        level = raw.strip().lower() if raw.strip().lower() in VALID_LEVELS else LEVEL_METADATA_ONLY
        return {"level": level, "endpointIsLocal": False}
    if not isinstance(raw, dict):
        return {"level": LEVEL_METADATA_ONLY, "endpointIsLocal": False}
    level = str(raw.get("level") or LEVEL_METADATA_ONLY).strip().lower()
    if level not in VALID_LEVELS:
        level = LEVEL_METADATA_ONLY
    endpoint = bool(raw.get("endpointIsLocal") or raw.get("endpoint_is_local") or False)
    return {"level": level, "endpointIsLocal": endpoint}


def resolve_agent_data_policy(
    raw: Any,
    *,
    model: str,
    base_url: str | None,
) -> AgentDataPolicy:
    """Resolve stored policy + model config into an effective egress decision."""
    norm = normalize_policy_value(raw)
    endpoint_is_local = bool(norm["endpointIsLocal"]) and bool(base_url)
    hosted = not endpoint_is_local
    level = norm["level"]
    # Local/self-hosted endpoints marked as such may receive sample values without the
    # sample-values opt-in; hosted providers always need the opt-in.
    allows = (level == LEVEL_SAMPLE_VALUES) or (not hosted)
    return AgentDataPolicy(
        level=level,
        endpoint_is_local=endpoint_is_local,
        model=model,
        provider=provider_of(model, base_url),
        base_url=base_url or None,
        hosted=hosted,
        allows_sample_values=allows,
    )


def load_agent_data_policy(model: str | None = None, base_url: str | None = None) -> AgentDataPolicy:
    """Load and resolve the workspace AgentDataPolicy from global settings (+ env defaults)."""
    from hub import metadb
    from hub.settings import settings

    if model is None or base_url is None:
        stored_model = metadb.get_setting("agentModel", "global") or settings.agent_model
        stored_base = metadb.get_setting("agentBaseUrl", "global") or settings.agent_base_url
        model = stored_model if model is None else model
        base_url = stored_base if base_url is None else base_url
    raw = metadb.get_setting(POLICY_SETTING_KEY, "global", default=None)
    return resolve_agent_data_policy(raw, model=model or settings.agent_model, base_url=base_url)


def _looks_like_rows(value: list) -> bool:
    """True when a list looks like tabular sample rows (list/dict cells), not e.g. column names."""
    if not value:
        return False
    sample = value[0]
    return isinstance(sample, (dict, list, tuple))


def sanitize_tool_result(result: Any, *, allows_sample_values: bool) -> Any:
    """Strip sample row values from a tool result under metadata-only.

    This is the single enforcement point in the tool loop: any current or future tool that
    returns row-shaped data is scrubbed here before the model (and the transcript) see it.
    """
    if allows_sample_values:
        return result
    return _strip_row_values(result)


def _strip_row_values(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        stripped = False
        for key, value in obj.items():
            if key == "rows" and isinstance(value, list) and _looks_like_rows(value):
                out[key] = []
                stripped = True
            else:
                out[key] = _strip_row_values(value)
        if stripped:
            out.setdefault("policy", _VALUE_REFUSAL)
        return out
    if isinstance(obj, list):
        return [_strip_row_values(item) for item in obj]
    return obj


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(str(value).encode('utf-8', errors='replace')).hexdigest()}"


def _bounded_audit_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text.encode("utf-8", errors="replace")) <= _AUDIT_ID_MAX_BYTES:
        return text
    return _digest(text)


def _bounded_audit_query(value: Any) -> str | None:
    if value is None:
        return None
    query = " ".join(str(value).split())
    if not query:
        return None
    encoded = query.encode("utf-8", errors="replace")
    if len(encoded) > _AUDIT_QUERY_MAX_BYTES:
        head = encoded[: _AUDIT_QUERY_MAX_BYTES - 3].decode("utf-8", errors="ignore")
        query = f"{head}..."
    if _SECRET_QUERY_PATTERN.search(query):
        return "[redacted]"
    return query


def _catalog_result_summary(tables: Any) -> tuple[int, str]:
    """Return a count and constant-size digest without retaining dataset identifiers."""
    digest = hashlib.sha256()
    count = 0
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict):
            continue
        count += 1
        identifier = table.get("id") or table.get("uri") or table.get("name") or ""
        encoded = str(identifier).encode("utf-8", errors="replace")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return count, f"sha256:{digest.hexdigest()}"


def audit_event_for_tool(
    policy: AgentDataPolicy,
    tool: str,
    tool_input: dict,
    result: Any,
    *,
    principal_id: str | None = None,
    canvas_id: str | None = None,
    request_id: str | None = None,
) -> dict | None:
    """Build one value-free audit event for a catalog-reading tool call under a hosted model."""
    if not policy.hosted or tool not in CATALOG_READING_TOOLS:
        return None
    base = {
        "provider": _bounded_audit_id(policy.provider) or "",
        "model": _bounded_audit_id(policy.model) or "",
        "tool": tool,
        "level": policy.level,
    }
    if tool == "list_catalog" and isinstance(result, dict):
        count, identifiers_digest = _catalog_result_summary(result.get("tables"))
        raw_mode = str(tool_input.get("mode") or "list").strip().lower()
        mode = raw_mode if raw_mode in _CATALOG_AUDIT_MODES else "other"
        query = tool_input.get("query", tool_input.get("q"))
        return {
            **base,
            "principalId": _bounded_audit_id(principal_id),
            "canvasId": _bounded_audit_id(canvas_id),
            "requestId": _bounded_audit_id(request_id),
            "query": _bounded_audit_query(query),
            "mode": mode,
            "returnedCount": count,
            "datasetIdentifiersDigest": identifiers_digest,
        }
    if tool == "preview" and isinstance(result, dict):
        return {
            **base,
            "dataset": tool_input.get("node_id") or tool_input.get("nodeId"),
            "columns": list(result.get("columns") or []),
            "rowCount": result.get("row_count", result.get("rowCount")),
        }
    if tool == "join_hints":
        left = tool_input.get("left_uri") or tool_input.get("leftUri")
        right = tool_input.get("right_uri") or tool_input.get("rightUri")
        columns: list[str] = []
        if isinstance(result, dict):
            for suggestion in result.get("suggestions") or []:
                if not isinstance(suggestion, dict):
                    continue
                columns.extend(suggestion.get("leftColumns") or [])
                columns.extend(suggestion.get("rightColumns") or [])
        return {
            **base,
            "dataset": f"{left}|{right}" if left or right else None,
            "columns": columns,
            "rowCount": None,
        }
    return {**base, "dataset": None, "columns": [], "rowCount": None}


def record_tool_audit(
    policy: AgentDataPolicy,
    tool: str,
    tool_input: dict,
    result: Any,
    *,
    principal_id: str | None = None,
    canvas_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """Persist one value-free audit event for a hosted catalog-reading tool call."""
    from hub import metadb

    event = audit_event_for_tool(
        policy,
        tool,
        tool_input,
        result,
        principal_id=principal_id,
        canvas_id=canvas_id,
        request_id=request_id,
    )
    if event is None:
        return
    # Never persist sample rows — events are metadata-only by construction.
    if "rows" in event:
        raise ValueError("agent egress audit must not carry raw rows")
    # Best-effort: a transient metadata-DB error must not abort an otherwise-successful tool call.
    try:
        metadb.record_agent_egress_event(event)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("agent egress audit write failed", exc_info=True)
