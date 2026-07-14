"""First-class Cred entities for object-store and agent credentials.

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


def _load_setting(conn, key: str):
    row = conn.execute(
        sa.text("SELECT value FROM settings WHERE scope = 'global' AND scope_id = '' AND key = :key"),
        {"key": key},
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _set_setting(conn, key: str, value) -> None:
    raw = json.dumps(value)
    existing = conn.execute(
        sa.text("SELECT id FROM settings WHERE scope = 'global' AND scope_id = '' AND key = :key"),
        {"key": key},
    ).fetchone()
    if existing:
        conn.execute(sa.text("UPDATE settings SET value = :value WHERE id = :id"), {"id": existing[0], "value": raw})
    else:
        conn.execute(
            sa.text("INSERT INTO settings (scope, scope_id, key, value) VALUES ('global', '', :key, :value)"),
            {"key": key, "value": raw},
        )


def upgrade() -> None:
    op.create_table(
        "creds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("fields_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    conn = op.get_bind()
    obj = _load_setting(conn, "objectStore")
    if isinstance(obj, dict) and any(obj.get(k) for k in ("accessKeyId", "secretAccessKey", "region", "endpoint")):
        cid = "cred-object-store-default"
        conn.execute(
            sa.text("INSERT INTO creds (id, name, kind, fields_json) VALUES (:id, :name, :kind, :fields)"),
            {"id": cid, "name": "Default object store", "kind": "object_store", "fields": json.dumps(obj)},
        )
        _set_setting(conn, "defaultObjectStoreCredId", cid)
        dests = _load_setting(conn, "destinations")
        if isinstance(dests, list):
            changed = False
            for d in dests:
                if isinstance(d, dict) and d.get("backend") in ("s3", "gs") and not d.get("credId"):
                    d["credId"] = cid
                    changed = True
            if changed:
                _set_setting(conn, "destinations", dests)
    agent_key = _load_setting(conn, "agentApiKey")
    if isinstance(agent_key, str) and agent_key:
        cid = "cred-agent-default"
        conn.execute(
            sa.text("INSERT INTO creds (id, name, kind, fields_json) VALUES (:id, :name, :kind, :fields)"),
            {"id": cid, "name": "Default agent", "kind": "agent", "fields": json.dumps({"apiKey": agent_key})},
        )
        _set_setting(conn, "agentCredId", cid)


def downgrade() -> None:
    op.drop_table("creds")
