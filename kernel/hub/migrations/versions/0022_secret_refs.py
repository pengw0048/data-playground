"""Strip legacy plaintext secrets from the settings table (SEC-03 / issue 107).

Revision ID: 0022_secret_refs
Revises: 0021_local_result_artifacts
Create Date: 2026-07-14
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "0022_secret_refs"
down_revision = "0021_local_result_artifacts"
branch_labels = None
depends_on = None

_SCALAR_SECRET_KEYS = ("agentApiKey",)
_OBJECT_STORE_SECRET_SUBKEYS = ("accessKeyId", "secretAccessKey", "sessionToken")
# Plugin manifests are not available during Alembic; clear plaintext for plugin.* keys whose
# trailing field name matches common secret field names (and any key the runtime pass later finds).
_PLUGIN_SECRET_FIELD_NAMES = frozenset({
    "token", "password", "secret", "api_key", "apikey", "access_key",
    "secretaccesskey", "accesskeyid", "sessiontoken",
})


def _is_secret_ref(value) -> bool:
    if not isinstance(value, str) or ":" not in value:
        return False
    scheme, _, rest = value.partition(":")
    return bool(scheme) and bool(rest) and scheme.isidentifier()


def _is_plaintext(value) -> bool:
    return isinstance(value, str) and value != "" and not _is_secret_ref(value)


def _clear_plaintext_secrets(conn, *, extra_plugin_keys: set[str] | frozenset = frozenset()) -> list[str]:
    """Delete / blank plaintext secret settings. Returns affected key paths for operator messages."""
    rows = conn.execute(sa.text("SELECT id, key, value, scope FROM settings")).fetchall()
    cleared: list[str] = []
    for row in rows:
        sid, key, raw, scope = row[0], row[1], row[2], row[3]
        if scope != "global":
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue

        if key in _SCALAR_SECRET_KEYS and _is_plaintext(value):
            conn.execute(sa.text("DELETE FROM settings WHERE id = :id"), {"id": sid})
            cleared.append(key)
            continue

        if key == "objectStore" and isinstance(value, dict):
            changed = False
            for sub in _OBJECT_STORE_SECRET_SUBKEYS:
                if _is_plaintext(value.get(sub)):
                    value[sub] = ""
                    cleared.append(f"objectStore.{sub}")
                    changed = True
            if changed:
                cleaned = {k: v for k, v in value.items() if v not in (None, "")}
                if cleaned:
                    conn.execute(
                        sa.text("UPDATE settings SET value = :value WHERE id = :id"),
                        {"id": sid, "value": json.dumps(cleaned)},
                    )
                else:
                    conn.execute(sa.text("DELETE FROM settings WHERE id = :id"), {"id": sid})
            continue

        if key.startswith("plugin.") and _is_plaintext(value):
            field = key.rsplit(".", 1)[-1].lower()
            if key in extra_plugin_keys or field in _PLUGIN_SECRET_FIELD_NAMES:
                conn.execute(sa.text("DELETE FROM settings WHERE id = :id"), {"id": sid})
                cleared.append(key)
    return cleared


def upgrade() -> None:
    """Destructive: remove plaintext secrets so the DB never retains credential bytes.

    Operators must re-enter ``env:VAR`` / ``file:/path`` references. See README.md and
    docs/PLUGINS.md for the reference format and migration steps.
    """
    conn = op.get_bind()
    cleared = _clear_plaintext_secrets(conn)
    if cleared:
        keys = ", ".join(sorted(set(cleared)))
        print(  # noqa: T201 — intentional operator-facing migrate message
            f"SEC-03 migration removed plaintext secrets for: {keys}. "
            "Re-enter each as a secret reference — env:VAR_NAME or file:/path/to/secret. "
            "See README.md (agent API key / object-store credentials) and docs/PLUGINS.md "
            "(plugin [[config]] secret fields)."
        )


def downgrade() -> None:
    # Irreversible: plaintext credentials were deleted and cannot be restored.
    pass
