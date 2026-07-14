"""Credentials as a first-class Cred entity (issue #156).

Create the ``creds`` table and backfill the current global ``objectStore`` + ``agentApiKey`` settings
into seeded creds, tagging existing object-store destinations with the seeded object-store cred.
Migration 0024 already scrubbed plaintext, so any backfilled fields are references, not raw secrets.

Revision ID: 0026_creds
Revises: 0025_run_request_id
Create Date: 2026-07-14
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "0026_creds"
down_revision = "0025_run_request_id"
branch_labels = None
depends_on = None

_OBJECT_STORE_CRED_ID = "cred-object-store-default"
_AGENT_CRED_ID = "cred-agent-default"


def _global_setting(conn, key: str):
    row = conn.execute(
        sa.text("SELECT value FROM settings WHERE scope = 'global' AND scope_id = '' AND key = :k"),
        {"k": key},
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _set_global_setting(conn, key: str, value) -> None:
    encoded = json.dumps(value)
    updated = conn.execute(
        sa.text("UPDATE settings SET value = :v WHERE scope = 'global' AND scope_id = '' AND key = :k"),
        {"v": encoded, "k": key},
    )
    if updated.rowcount == 0:
        conn.execute(
            sa.text("INSERT INTO settings (scope, scope_id, key, value) "
                    "VALUES ('global', '', :k, :v)"),
            {"k": key, "v": encoded},
        )


def _insert_cred(conn, cred_id: str, name: str, kind: str, fields: dict) -> None:
    # created_at is left NULL for seeded rows (column is nullable); ORM inserts get the _now default.
    conn.execute(
        sa.text("INSERT INTO creds (id, name, kind, fields_json) "
                "VALUES (:id, :name, :kind, :fields)"),
        {"id": cred_id, "name": name, "kind": kind, "fields": json.dumps(fields)},
    )


def upgrade() -> None:
    conn = op.get_bind()
    op.create_table(
        "creds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("fields_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill the global objectStore setting into a seeded object-store cred + set it as the default,
    # and tag existing s3/gs destinations with it so their writes keep resolving the same credentials.
    object_store = _global_setting(conn, "objectStore")
    if isinstance(object_store, dict) and object_store:
        _insert_cred(conn, _OBJECT_STORE_CRED_ID, "Object store", "object_store", object_store)
        _set_global_setting(conn, "defaultObjectStoreCredId", _OBJECT_STORE_CRED_ID)
        destinations = _global_setting(conn, "destinations")
        if isinstance(destinations, list):
            changed = False
            for d in destinations:
                if isinstance(d, dict) and d.get("backend") in ("s3", "gs") and not d.get("credId"):
                    d["credId"] = _OBJECT_STORE_CRED_ID
                    changed = True
            if changed:
                _set_global_setting(conn, "destinations", destinations)

    # Backfill the global agentApiKey reference into a seeded agent cred + point agentCredId at it.
    agent_key = _global_setting(conn, "agentApiKey")
    if isinstance(agent_key, str) and agent_key:
        _insert_cred(conn, _AGENT_CRED_ID, "Agent", "agent", {"apiKey": agent_key})
        _set_global_setting(conn, "agentCredId", _AGENT_CRED_ID)


def downgrade() -> None:
    op.drop_table("creds")
