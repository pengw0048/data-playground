"""Schema-contract evidence and compatibility semantics for #125."""

from __future__ import annotations

import uuid

import duckdb

from hub import metadb
from hub.plugins.adapters import relation_columns


def _field(name: str, type: str = "int", **extra) -> dict:
    return {"name": name, "type": type, "provenance": "declared", **extra}


def test_local_relation_schema_is_explicitly_inferred_without_guessed_facts():
    field = relation_columns(duckdb.connect().sql("select 1 as value"))[0]

    assert field.provenance == "inferred"
    assert field.field_id is None
    assert field.nullable is None
    assert field.has_default is None
    assert field.physical_type


def test_schema_compatibility_handles_additions_type_changes_and_proven_renames():
    base = [_field("id", "int", fieldId="src-id", nullable=False)]

    nullable_addition = metadb.diff_columns(base, base + [_field("note", "string", nullable=True)])
    assert nullable_addition.status == "compatible"
    assert nullable_addition.fields[-1].reason == "nullable field was added"

    required_addition = metadb.diff_columns(base, base + [_field("owner", "string", nullable=False, hasDefault=False)])
    assert required_addition.status == "breaking"
    assert required_addition.fields[-1].reason == "non-nullable field was added without a default"

    widening = metadb.diff_columns([_field("id", "int", nullable=True)], [_field("id", "bigint", nullable=True)])
    narrowing = metadb.diff_columns([_field("id", "bigint", nullable=True)], [_field("id", "int", nullable=True)])
    assert widening.status == "compatible" and "widens" in widening.fields[0].reason
    assert narrowing.status == "breaking" and "narrows" in narrowing.fields[0].reason

    renamed = metadb.diff_columns(base, [_field("record_id", "int", fieldId="src-id", nullable=False)])
    assert renamed.status == "compatible"
    assert renamed.fields[0].kind == "renamed"
    assert renamed.fields[0].old_name == "id" and renamed.fields[0].new_name == "record_id"


def test_schema_compatibility_keeps_evidence_poor_changes_unknown_and_proven_removals_breaking():
    inferred = [{"name": "old_name", "type": "int", "provenance": "inferred"}]
    renamed_without_identity = [{"name": "new_name", "type": "int", "provenance": "inferred"}]
    uncertain = metadb.diff_columns(inferred, renamed_without_identity)

    assert uncertain.status == "unknown"
    assert {field.kind for field in uncertain.fields} == {"added", "removed"}
    assert all(field.status == "unknown" for field in uncertain.fields)

    before = [_field("id", fieldId="id", nullable=False), _field("old_name", fieldId="old", nullable=True)]
    after = [_field("id", fieldId="id", nullable=False)]
    removed = metadb.diff_columns(before, after)
    assert removed.status == "breaking"
    assert removed.fields[1].kind == "removed" and removed.fields[1].status == "breaking"

    duplicate = metadb.diff_columns(
        [_field("left", fieldId="duplicated", nullable=True), _field("right", fieldId="duplicated", nullable=True)],
        [_field("left", fieldId="duplicated", nullable=True)],
    )
    assert duplicate.status == "unknown" and duplicate.fields[0].field_id == "duplicated"


def test_saved_contract_round_trips_current_field_model_without_loss():
    metadb.init_db()
    name = f"schema-roundtrip-{uuid.uuid4().hex}"
    fields = [_field(
        "customer_id", "bigint", fieldId="warehouse.customer_id", physicalType="INT64",
        nullable=False, hasDefault=False, provenance="provider", capabilities=["key"])]

    metadb.save_schema_contract(name, fields)
    stored = metadb.get_schema_contract(name)

    assert stored is not None
    assert stored["columns"] == fields
