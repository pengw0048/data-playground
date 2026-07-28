"""Backfill exact names in retained pre-#860 write receipts.

Revision ID: 0050_receipt_names
Revises: 0046_relationship_incident
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable

import sqlalchemy as sa
from alembic import op


revision = "0050_receipt_names"
down_revision = "0046_relationship_incident"
branch_labels = None
depends_on = None


_PROVIDERS = {"managed-local-file", "managed-local-lance"}


def _fail(label: str, message: str) -> None:
    raise RuntimeError(f"cannot backfill receipt name in {label}: {message}")


def _document(raw: object, label: str) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        _fail(label, "document is not valid JSON")
        raise AssertionError from exc  # pragma: no cover - _fail always raises
    if not isinstance(value, dict):
        _fail(label, "document is not a JSON object")
    return value


def _string(value: object, label: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(label, f"{field} is not a non-empty string")
    return value


def _integer(value: object, label: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(label, f"{field} is not a positive integer")
    return value


def _optional_string(value: object, label: str, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, label, field)


def _field(document: dict, snake: str, camel: str, label: str, style: str) -> object:
    key = snake if style == "snake" else camel
    if style not in ("snake", "camel") or key not in document:
        _fail(label, f"requires {style}-case {key!r}")
    return document[key]


def _receipt_identity(
        receipt: object, label: str, style: str,
        ) -> tuple[str, str, str, str, str, str, int, str | None, str | None]:
    if not isinstance(receipt, dict):
        _fail(label, "receipt is not a JSON object")
    publication = receipt.get("publication")
    if not isinstance(publication, dict):
        _fail(label, "receipt publication is not a JSON object")
    provider = _string(publication.get("provider"), label, "publication.provider")
    if provider not in _PROVIDERS:
        _fail(label, "receipt publication provider is not managed-local")
    return (
        provider,
        _string(_field(receipt, "dataset_id", "datasetId", label, style),
                label, "dataset identity"),
        _string(_field(receipt, "revision_id", "revisionId", label, style),
                label, "revision identity"),
        _string(_field(publication, "idempotency_key", "idempotencyKey", label, style),
                label, "publication idempotency identity"),
        _string(_field(publication, "logical_uri", "logicalUri", label, style),
                label, "publication logical identity"),
        _string(_field(publication, "artifact_uri", "artifactUri", label, style),
                label, "publication artifact identity"),
        _integer(_field(publication, "publish_sequence", "publishSequence", label, style),
                 label, "publication sequence"),
        _optional_string(
            _field(publication, "catalog_version", "catalogVersion", label, style),
            label, "publication catalog version"),
        _optional_string(
            _field(publication, "backend_version", "backendVersion", label, style),
            label, "publication backend version"),
    )


def _intent_destination(raw: object, label: str, provider: str) -> tuple[dict, str]:
    intent = _document(raw, label)
    destination = intent.get("destination")
    if not isinstance(destination, dict):
        _fail(label, "write intent destination is not a JSON object")
    if destination.get("provider") != provider:
        _fail(label, "write intent provider does not match receipt ledger")
    return destination, _string(intent.get("idempotencyKey"), label, "write intent idempotencyKey")


def _file_ledger_name(row: dict, receipt: dict, label: str) -> str:
    destination, intent_key = _intent_destination(
        row["write_intent_doc"], label, "managed-local-file")
    table = _document(row["table_doc"], label)
    name = _string(destination.get("name"), label, "write intent destination.name")
    if table.get("name") != name:
        _fail(label, "write intent name does not match exact revision table name")
    identity = _receipt_identity(receipt, label, "camel")
    if identity[:7] != (
            "managed-local-file", row["logical_id"], row["revision_id"],
            row["write_idempotency_key"], destination.get("logicalUri"), row["artifact_uri"],
            _integer(row["publish_seq"], label, "ledger publish_seq")):
        _fail(label, "receipt does not match its exact managed-local-file ledger")
    if intent_key != row["write_idempotency_key"]:
        _fail(label, "write intent idempotency key does not match receipt ledger")
    if "name" in receipt and receipt["name"] != name:
        _fail(label, "receipt name does not match immutable managed-local-file evidence")
    return name


def _lance_ledger_name(row: dict, receipt: dict, label: str) -> str:
    destination, intent_key = _intent_destination(
        row["write_intent_doc"], label, "managed-local-lance")
    name = _string(destination.get("name"), label, "write intent destination.name")
    try:
        revision_sequence = int(row["revision_id"])
    except (TypeError, ValueError):
        _fail(label, "ledger revision_id is not a publication sequence")
    identity = _receipt_identity(receipt, label, "camel")
    if identity[:7] != (
            "managed-local-lance", row["dataset_id"], row["revision_id"],
            row["idempotency_key"], row["logical_uri"], row["logical_uri"],
            _integer(revision_sequence, label, "ledger revision sequence")):
        _fail(label, "receipt does not match its exact managed-local-lance ledger")
    if (intent_key != row["idempotency_key"]
            or destination.get("datasetId") != row["dataset_id"]
            or destination.get("logicalUri") != row["logical_uri"]):
        _fail(label, "write intent does not match exact managed-local-lance ledger")
    if "name" in receipt and receipt["name"] != name:
        _fail(label, "receipt name does not match immutable managed-local-lance evidence")
    return name


def _encoded(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _backfill_outputs(
        document: dict, label: str, resolve: Callable[[dict, str, str], str]) -> bool:
    outputs = document.get("outputs")
    if outputs is None:
        return False
    if not isinstance(outputs, list):
        _fail(label, "outputs is not a JSON array")
    changed = False
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            _fail(label, f"outputs[{index}] is not a JSON object")
        receipt_keys = [key for key in ("write_receipt", "writeReceipt") if key in output]
        if len(receipt_keys) > 1:
            _fail(label, f"outputs[{index}] has conflicting write receipt spellings")
        receipt = output.get(receipt_keys[0]) if receipt_keys else None
        if receipt is None:
            continue
        if not isinstance(receipt, dict):
            _fail(label, f"outputs[{index}].write receipt is not a JSON object")
        if "name" not in receipt:
            receipt["name"] = resolve(
                receipt, f"{label}.outputs[{index}].write receipt", "snake")
            changed = True
    return changed


def upgrade() -> None:
    connection = op.get_bind()
    ledgers: dict[
        tuple[str, str, str, str, str, str, int, str | None, str | None],
        list[tuple[dict, str, Callable[[], str]]],
    ] = defaultdict(list)
    updates: list[tuple[str, str, str, str, str]] = []

    file_rows = connection.execute(sa.text("""
        SELECT revision_id, logical_id, artifact_uri, publish_seq, table_doc,
               write_idempotency_key, write_intent_doc, write_receipt_doc
        FROM managed_local_file_revisions
        WHERE write_receipt_doc IS NOT NULL
    """)).mappings().all()
    for row in file_rows:
        row = dict(row)
        label = f"managed_local_file_revisions[{row['revision_id']}]"
        receipt = _document(row["write_receipt_doc"], label)
        if "name" not in receipt:
            receipt["name"] = _file_ledger_name(row, receipt, label)
            updates.append((
                "managed_local_file_revisions", "revision_id", row["revision_id"],
                "write_receipt_doc", _encoded(receipt)))
        identity = _receipt_identity(receipt, label, "camel")
        ledgers[identity].append((
            row, label, lambda row=row, receipt=receipt, label=label: _file_ledger_name(row, receipt, label)))

    lance_rows = connection.execute(sa.text("""
        SELECT idempotency_key, dataset_id, logical_uri, revision_id,
               write_intent_doc, write_receipt_doc
        FROM managed_local_lance_write_receipts
    """)).mappings().all()
    for row in lance_rows:
        row = dict(row)
        label = f"managed_local_lance_write_receipts[{row['idempotency_key']}]"
        receipt = _document(row["write_receipt_doc"], label)
        if "name" not in receipt:
            receipt["name"] = _lance_ledger_name(row, receipt, label)
            updates.append((
                "managed_local_lance_write_receipts", "idempotency_key", row["idempotency_key"],
                "write_receipt_doc", _encoded(receipt)))
        identity = _receipt_identity(receipt, label, "camel")
        ledgers[identity].append((
            row, label, lambda row=row, receipt=receipt, label=label: _lance_ledger_name(row, receipt, label)))

    def resolve(receipt: dict, label: str, style: str) -> str:
        identity = _receipt_identity(receipt, label, style)
        candidates = ledgers.get(identity, [])
        if len(candidates) != 1:
            _fail(label, "receipt does not resolve to one exact managed receipt ledger")
        return candidates[0][2]()

    for row in connection.execute(sa.text(
            "SELECT id, outputs FROM run_records")).mappings():
        label = f"run_records[{row['id']}].outputs"
        try:
            outputs = json.loads(row["outputs"])
        except (TypeError, ValueError):
            _fail(label, "document is not valid JSON")
        if not isinstance(outputs, list):
            _fail(label, "document is not a JSON array")
        wrapper = {"outputs": outputs}
        if _backfill_outputs(wrapper, label, resolve):
            updates.append(("run_records", "id", row["id"], "outputs", _encoded(outputs)))

    for table, key, column in (
            ("run_states", "run_id", "doc"),
            ("durable_tasks", "id", "status_doc"),
            ("run_backend_jobs", "run_id", "result_doc")):
        for row in connection.execute(sa.text(
                f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL")).mappings():
            label = f"{table}[{row[key]}].{column}"
            try:
                document = _document(row[column], label)
            except RuntimeError:
                if table == "run_backend_jobs":
                    # Backend result docs are only authoritative here when they are a RunStatus.
                    continue
                raise
            if table == "run_backend_jobs" and "run_id" not in document:
                continue
            if _backfill_outputs(document, label, resolve):
                updates.append((table, key, row[key], column, _encoded(document)))

    for table, key in (("durable_tasks", "id"), ("durable_task_attempts", "id")):
        for row in connection.execute(sa.text(
                f"SELECT {key}, output_receipt FROM {table} WHERE output_receipt IS NOT NULL")).mappings():
            label = f"{table}[{row[key]}].output_receipt"
            receipt = _document(row["output_receipt"], label)
            if "name" not in receipt:
                receipt["name"] = resolve(receipt, label, "camel")
                updates.append((table, key, row[key], "output_receipt", _encoded(receipt)))

    # All evidence is validated before the first write. Alembic runs this upgrade in one transaction,
    # so a later database error also leaves the retained documents unchanged.
    for table, key, identifier, column, value in updates:
        connection.execute(sa.text(
            f"UPDATE {table} SET {column} = :value WHERE {key} = :identifier"),
            {"value": value, "identifier": identifier})


def downgrade() -> None:
    # Removing exact names would deliberately recreate invalid retained receipts.
    pass
