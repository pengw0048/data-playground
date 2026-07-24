"""Metadata migration startup contracts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from hub import metadb
from hub.settings import settings


@contextlib.contextmanager
def _isolated_metadata(url: str):
    original_url = settings.database_url
    original_engine, original_session = metadb._engine, metadb._Session
    settings.database_url = url
    metadb._engine = metadb._Session = None
    try:
        yield
    finally:
        if metadb._engine is not None:
            metadb._engine.dispose()
        settings.database_url = original_url
        metadb._engine, metadb._Session = original_engine, original_session


def _normalized_default(value) -> str | None:
    if value is None:
        return None
    value = getattr(value, "arg", value)
    rendered = str(value).strip()
    if len(rendered) >= 2 and rendered[0] == rendered[-1] == "'":
        rendered = rendered[1:-1].replace("''", "'")
    normalized = rendered.lower()
    return "now()" if normalized == "current_timestamp" else normalized


def test_migration_graph_has_one_linear_head():
    scripts = ScriptDirectory.from_config(metadb._alembic_cfg())
    revisions = list(scripts.walk_revisions())

    assert [(revision.revision, revision.down_revision) for revision in revisions] == [
        ("0044_provider_lineage_identity", "0043_provider_source_binding"),
        ("0043_provider_source_binding", "0042_field_lineage"),
        ("0042_field_lineage", "0041_provider_canonical"),
        ("0041_provider_canonical", "0040_managed_sidecar"),
        ("0040_managed_sidecar", "0039_folder_replays"),
        ("0039_folder_replays", "0038_inbox_dataset_scoped"),
        ("0038_inbox_dataset_scoped", "0037_keyed_upsert"),
        ("0037_keyed_upsert", "0036_restore_revision"),
        ("0036_restore_revision", "0035_remove_temporal"),
        ("0035_remove_temporal", "0034_external_overlay"),
        ("0034_external_overlay", "0033_temporal_task"),
        ("0033_temporal_task", "0032_temporal_pub"),
        ("0032_temporal_pub", "0031_durable_merge"),
        ("0031_durable_merge", "0030_merge_columns_pub"),
        ("0030_merge_columns_pub", "0029_sparse_output_mat"),
        ("0029_sparse_output_mat", "0028_sparse_output_admission"),
        ("0028_sparse_output_admission", "0027_distribution_reports"),
        ("0027_distribution_reports", "0026_dataset_views"),
        ("0026_dataset_views", "0025_transform_library_keys"),
        ("0025_transform_library_keys", "0024_promoted_transforms"),
        ("0024_promoted_transforms", "0023_catalog_folder_overlay"),
        ("0023_catalog_folder_overlay", "0022_task_manifests"),
        ("0022_task_manifests", "0021_manifest_output_owners"),
        ("0021_manifest_output_owners", "0020_execution_manifests"),
        ("0020_execution_manifests", "0019_exact_local_inputs"),
        ("0019_exact_local_inputs", "0018_bounded_fanout_write"),
        ("0018_bounded_fanout_write", "0017_linear_checkpoint_inbox"),
        ("0017_linear_checkpoint_inbox", "0016_bounded_fanout_plan"),
        ("0016_bounded_fanout_plan", "0015_task_inbox_items"),
        ("0015_task_inbox_items", "0014_checkpoint_mat_identity"),
        ("0014_checkpoint_mat_identity", "0013_linear_checkpoint_commit"),
        ("0013_linear_checkpoint_commit", "0012_linear_checkpoint_admission"),
        ("0012_linear_checkpoint_admission", "0011_external_wait_publication"),
        ("0011_external_wait_publication", "0010_durable_external_waits"),
        ("0010_durable_external_waits", "0009_durable_local_write_tasks"),
        ("0009_durable_local_write_tasks", "0008_managed_local_lance_writes"),
        ("0008_managed_local_lance_writes", "0007_workspace_provider_bindings"),
        ("0007_workspace_provider_bindings", "0006_typed_local_writes"),
        ("0006_typed_local_writes", "0005_profile_output_ports"),
        ("0005_profile_output_ports", "0004_local_run_input_admissions"),
        ("0004_local_run_input_admissions", "0003_repair_historical_metadata"),
        ("0003_repair_historical_metadata", "0002_managed_file_revs"),
        ("0002_managed_file_revs", "0001_schema_baseline"),
        ("0001_schema_baseline", None),
    ]
    assert scripts.get_heads() == ["0044_provider_lineage_identity"]
    assert metadb.expected_schema_head() == "0044_provider_lineage_identity"


def test_field_lineage_forward_migration_preserves_evidence_poor_facts(tmp_path):
    db_path = tmp_path / "field-lineage-forward.db"
    old_mappings = '[{"destination_field":"id","source_field":"raw_id"}]'
    with _isolated_metadata(f"sqlite:///{db_path}"):
        engine = metadb.engine()
        with engine.connect() as connection:
            command.upgrade(
                metadb._alembic_cfg(connection), "0041_provider_canonical"
            )
            connection.execute(sa.text("""
                INSERT INTO catalog_lineage_facts (
                    fact_key, publication_key, fingerprint,
                    source_key, destination_key, source_uri, destination_uri,
                    source_key_hash, destination_key_hash,
                    source_uri_hash, destination_uri_hash,
                    provenance, field_mappings_json, created_at
                ) VALUES (
                    'old-fact', 'old-publication', 'old-fingerprint',
                    'old-source', 'old-destination', 'mem://old-source',
                    'mem://old-destination', :source_hash, :destination_hash,
                    :source_hash, :destination_hash,
                    'manual', :mappings, CURRENT_TIMESTAMP
                )
            """), {
                "source_hash": hashlib.sha256(b"old-source").hexdigest(),
                "destination_hash": hashlib.sha256(b"old-destination").hexdigest(),
                "mappings": old_mappings,
            })
            connection.commit()
            command.upgrade(metadb._alembic_cfg(connection), "head")

            assert "catalog_field_lineage_projections" in inspect(
                connection).get_table_names()
            assert connection.execute(sa.text(
                "SELECT field_mappings_json FROM catalog_lineage_facts "
                "WHERE fact_key = 'old-fact'"
            )).scalar_one() == old_mappings
            assert connection.execute(sa.text(
                "SELECT count(*) FROM catalog_field_lineage_projections"
            )).scalar_one() == 0


def test_provider_source_binding_forward_migration_mints_opaque_generations(tmp_path):
    db_path = tmp_path / "provider-source-binding-forward.db"
    with _isolated_metadata(f"sqlite:///{db_path}"):
        engine = metadb.engine()
        with engine.connect() as connection:
            command.upgrade(
                metadb._alembic_cfg(connection), "0042_field_lineage"
            )
            connection.execute(sa.text("""
                INSERT INTO workspace_provider_datasets (
                    mount_id, provider_dataset_id, provider, uri, columns_doc,
                    state, last_error, last_resolved_at, created_at, updated_at
                ) VALUES (
                    'mount-a', 'dataset-a', 'fixture',
                    'secret://physical-a', '[]', 'current', NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                ), (
                    'mount-a', 'dataset-b', 'fixture',
                    'secret://physical-b', '[]', 'detached', 'resource is detached',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """))
            connection.commit()
            command.upgrade(metadb._alembic_cfg(connection), "head")

            rows = connection.execute(sa.text(
                "SELECT mount_id, provider_dataset_id, source_binding_id, state "
                "FROM workspace_provider_datasets ORDER BY provider_dataset_id"
            )).mappings().all()
            assert [row["provider_dataset_id"] for row in rows] == [
                "dataset-a", "dataset-b"]
            assert all(
                len(row["source_binding_id"]) == 32
                and set(row["source_binding_id"]) <= set("0123456789abcdef")
                for row in rows
            )
            assert rows[0]["source_binding_id"] != rows[1]["source_binding_id"]
            assert rows[1]["state"] == "detached"
            assert "secret" not in json.dumps(
                [row["source_binding_id"] for row in rows])


def test_provider_lineage_identity_forward_migration_widens_only_source_projection(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'provider-lineage-identity.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0043_provider_source_binding")
            before = {
                column["name"]: column["type"]
                for column in inspect(connection).get_columns(
                    "catalog_field_lineage_projections")
            }
            assert before["source_dataset_id"].length == 128
            assert before["destination_dataset_id"].length == 128
            command.upgrade(metadb._alembic_cfg(connection), "head")
            after = {
                column["name"]: column["type"]
                for column in inspect(connection).get_columns(
                    "catalog_field_lineage_projections")
            }
            assert after["source_dataset_id"].length == 512
            assert after["destination_dataset_id"].length == 128


def test_migration_revision_ids_fit_alembic_version_num():
    """PostgreSQL stores alembic_version.version_num as varchar(32); SQLite hides overflows."""
    scripts = ScriptDirectory.from_config(metadb._alembic_cfg())
    for revision in scripts.walk_revisions():
        assert len(revision.revision) <= 32, (
            f"revision id {revision.revision!r} exceeds alembic_version.version_num "
            f"varchar(32) and cannot apply on PostgreSQL")


def test_provider_canonical_state_migration_preserves_placement_snapshots(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'provider-canonical.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0040_managed_sidecar")
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO workspace_provider_bindings "
                "(id, mount_id, provider, container_id, resource_id, kind, name, "
                "parent_binding_id, state, active, last_error, relinked_from_id, "
                "last_resolved_at, created_at, updated_at) VALUES "
                "('provider-parent', 'migration-mount', 'fixture', :root, "
                "'provider-parent-placement', 'container', 'Parent snapshot', NULL, "
                "'current', 1, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP), "
                "('provider-child', 'migration-mount', 'fixture', :root, "
                "'provider-child-placement', 'dataset', 'Child snapshot', "
                "'provider-parent', 'offline', 1, 'safe old error', NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"root": metadb.LOCAL_WORKSPACE_ROOT_ID})
            connection.execute(sa.text(
                "INSERT INTO workspace_external_overlay_anchors "
                "(binding_id, container_id, mount_id, resource_id, created_at) VALUES "
                "('provider-parent', :root, 'migration-mount', "
                "'provider-parent-placement', CURRENT_TIMESTAMP)"
            ), {"root": metadb.LOCAL_WORKSPACE_ROOT_ID})

        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "head")
            rows = connection.execute(sa.text(
                "SELECT id, provider_placement_id, parent_provider_placement_id, "
                "provider_dataset_id, kind, name, state, active, last_error "
                "FROM workspace_provider_bindings ORDER BY id"
            )).mappings().all()
            assert rows == [
                {
                    "id": "provider-child",
                    "provider_placement_id": "provider-child-placement",
                    "parent_provider_placement_id": "provider-parent-placement",
                    "provider_dataset_id": None,
                    "kind": "dataset",
                    "name": "Child snapshot",
                    "state": "offline",
                    "active": 1,
                    "last_error": "safe old error",
                },
                {
                    "id": "provider-parent",
                    "provider_placement_id": "provider-parent-placement",
                    "parent_provider_placement_id": None,
                    "provider_dataset_id": None,
                    "kind": "container",
                    "name": "Parent snapshot",
                    "state": "current",
                    "active": 1,
                    "last_error": None,
                },
            ]
            anchor = connection.execute(sa.text(
                "SELECT binding_id, mount_id, provider_placement_id "
                "FROM workspace_external_overlay_anchors"
            )).mappings().one()
            assert anchor == {
                "binding_id": "provider-parent",
                "mount_id": "migration-mount",
                "provider_placement_id": "provider-parent-placement",
            }
            assert connection.execute(sa.text(
                "SELECT count(*) FROM workspace_provider_datasets"
            )).scalar_one() == 0
        recovered = metadb.workspace_provider_cache_resource(
            mount_id="migration-mount",
            provider="fixture",
            container_id=metadb.LOCAL_WORKSPACE_ROOT_ID,
            provider_placement_id="provider-child-placement",
            kind="dataset",
            name="Child snapshot",
            parent_provider_placement_id="provider-parent-placement",
            parent_binding_id="provider-parent",
            provider_dataset_id="canonical-child",
            uri="file:///canonical-child.parquet",
        )
        assert recovered["bindingId"] == "provider-child"
        assert recovered["providerDatasetId"] == "canonical-child"
        assert recovered["canonicalReferenceState"] == "current"
        assert metadb.workspace_provider_dataset(
            mount_id="migration-mount",
            provider_dataset_id="canonical-child",
        )["uri"] == "file:///canonical-child.parquet"


def test_temporal_parent_blocks_downgrade_without_partial_schema_loss(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'temporal-downgrade.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0032_temporal_pub")
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO compound_dataset_revisions "
                "(owner_id, dataset_id, revision_id, manifest_doc, created_at) VALUES "
                "('local', 'compound', :revision, '{}', CURRENT_TIMESTAMP)"),
                {"revision": "a" * 64})
            connection.execute(sa.text(
                "INSERT INTO compound_dataset_heads (owner_id, dataset_id, revision_id, updated_at) "
                "VALUES ('local', 'compound', :revision, CURRENT_TIMESTAMP)"),
                {"revision": "a" * 64})
        with metadb.engine().connect() as connection:
            with pytest.raises(RuntimeError, match="cannot downgrade"):
                command.downgrade(metadb._alembic_cfg(connection), "0031_durable_merge")
            tables = set(inspect(connection).get_table_names())
            assert {"temporal_resample_publications", "compound_dataset_heads",
                    "compound_dataset_revisions"} <= tables
            assert connection.execute(sa.text(
                "SELECT version_num FROM alembic_version")).scalar_one() == "0032_temporal_pub"
        with metadb.engine().begin() as connection:
            connection.execute(sa.text("DELETE FROM compound_dataset_heads"))
            connection.execute(sa.text("DELETE FROM compound_dataset_revisions"))
        with metadb.engine().connect() as connection:
            command.downgrade(metadb._alembic_cfg(connection), "0031_durable_merge")
            command.upgrade(metadb._alembic_cfg(connection), "head")


def test_remove_temporal_state_upgrade_preserves_ordinary_managed_revision(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'remove-temporal-state.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0034_external_overlay")
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO users (id, name, is_admin, created_at) VALUES "
                "('migration-owner', 'Migration owner', false, CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO local_result_artifacts "
                "(uri, namespace_id, storage_root, lock_name, lock_token, lock_protected, state, "
                "created_at, committed_at) VALUES "
                "('file:///ordinary.parquet', 'migration-namespace', '/tmp', 'ordinary.lock', "
                "'ordinary-token', true, 'ready', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO catalog_logical_datasets "
                "(logical_id, catalog_key, logical_uri, current_uri, updated_at) VALUES "
                "('ordinary-logical', 'ordinary-key', 'dataset://ordinary', "
                "'file:///ordinary.parquet', CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO managed_local_file_revisions "
                "(revision_id, logical_id, artifact_uri, publish_seq, table_doc, committed_at) VALUES "
                "('ordinary-revision', 'ordinary-logical', 'file:///ordinary.parquet', 1, '{}', "
                "CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO temporal_resample_publications "
                "(idempotency_key, owner_id, write_intent_doc, parent_dataset_id, "
                "parent_revision_id, child_revision_id, spec_doc, evidence_doc, candidate_digest, "
                "output_member_id, output_revision_id, created_at) VALUES "
                "('temporal-write', 'migration-owner', '{}', 'compound', :parent, :child, '{}', '{}', "
                ":candidate, 'output-member', 'ordinary-revision', CURRENT_TIMESTAMP)"), {
                    "parent": "a" * 64, "child": "b" * 64, "candidate": "c" * 64,
                })
            connection.execute(sa.text(
                "INSERT INTO durable_tasks "
                "(id, owner_id, submission_id, intent_sha256, target_node_id, task_kind, "
                "backend_kind, write_intent, status, status_doc, cancel_requested, retry_count, "
                "max_attempts, created_at, updated_at) VALUES "
                "('temporal-task', 'migration-owner', 'temporal-submission', :intent, 'temporal-resample', "
                "'temporal_resample_write', 'local', '{}', 'running', '{}', false, 0, 3, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"intent": "d" * 64})
            connection.execute(sa.text(
                "INSERT INTO temporal_resample_task_envelopes "
                "(task_id, request_doc, request_sha256, candidate_sha256, write_idempotency_key, "
                "phase, created_at, updated_at) VALUES "
                "('temporal-task', '{}', :request, :candidate, 'temporal-write', 'recomputing', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {
                    "request": "d" * 64, "candidate": "c" * 64,
                })
            connection.execute(sa.text(
                "INSERT INTO durable_task_attempts "
                "(id, task_id, attempt_number, status, created_at) VALUES "
                "('temporal-attempt', 'temporal-task', 1, 'running', CURRENT_TIMESTAMP)"))

        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "head")
            tables = set(inspect(connection).get_table_names())
            assert {
                "temporal_resample_task_envelopes", "temporal_resample_publications",
                "compound_dataset_heads", "compound_dataset_revisions",
            }.isdisjoint(tables)
            assert connection.execute(sa.text(
                "SELECT count(*) FROM durable_tasks WHERE id = 'temporal-task'"
            )).scalar_one() == 0
            assert connection.execute(sa.text(
                "SELECT count(*) FROM durable_task_attempts WHERE id = 'temporal-attempt'"
            )).scalar_one() == 0
            assert connection.execute(sa.text(
                "SELECT artifact_uri FROM managed_local_file_revisions "
                "WHERE revision_id = 'ordinary-revision'"
            )).scalar_one() == "file:///ordinary.parquet"

            command.downgrade(metadb._alembic_cfg(connection), "0034_external_overlay")
            assert {
                "temporal_resample_task_envelopes", "temporal_resample_publications",
                "compound_dataset_heads", "compound_dataset_revisions",
            } <= set(inspect(connection).get_table_names())
            command.upgrade(metadb._alembic_cfg(connection), "head")


def test_committed_migration_revisions_are_immutable():
    versions_path = Path(metadb._MIGRATIONS_DIR) / "versions"
    expected_hashes = {
        "0044_provider_lineage_identity.py": (
            "41cb6d835a6647674264fa7db1c871c0fff0cbd1ae83c1fa1055e27a8e7a84b9"
        ),
        "0043_provider_source_binding.py": (
            "bb830c82e83793683157ab4e5cb7660f6991013d2c9a92ab30cc6b50c451cd31"
        ),
        "0042_field_lineage_identity.py": (
            "18aad5bb71ac63841c68501d973825335f51e987e302cdefc4eb94770bb34ad4"
        ),
        "0041_provider_canonical_state.py": (
            "b1056c4481642dceab97d8fd17b257fe4ddec3755cdbdd04cf01091e1b868b3f"
        ),
        "0040_managed_sidecar_merge_task.py": (
            "d3c7ef82fe53d06e235c92b641e7190f4d2803d52fbea88de6babccf83ad27e7"
        ),
        "0039_workspace_folder_replays.py": (
            "58553bae37f35f47639c93ec080795ea48cce29fcdd78c5211d9de740ba7fb63"
        ),
        "0038_inbox_dataset_scoped.py": (
            "2d2e21041380660246e008baddc6639ffd33705cfaea725f616cf54905115b44"
        ),
        "0037_keyed_upsert_task.py": (
            "fc07c4f24ac0b44724dcede66e1e79c4a073ba460601d4081b1f5c614f295e39"
        ),
        "0036_restore_revision_task.py": (
            "e63f603fc1daeed7cfbfd32d8e7367e98e1be507d21a546611d3c0df0ac8ffa9"
        ),
        "0035_remove_temporal_state.py": (
            "625bb60791f16073068e5ff46ee91af5f1ef8b983fb77cf4d01bebb124a5aced"
        ),
        "0034_external_overlay_anchor.py": (
            "e9040678c38e06c4c60c623c621afd16664842bb30472ab0b49bbd8cb87ae960"
        ),
        "0001_schema_baseline.py": "f8a793dd0af47e189939f1ce41ec39ae336009bf353e8ac8147fd961386c1e96",
        "0002_managed_local_file_revisions.py": (
            "c69ae2c9e2b6311261b694ecdd057008d5d6ffccd7e88bd3cbadfe04af7095f5"
        ),
        "0003_repair_historical_metadata.py": (
            "66165953789dbc0d2c46c8c8a5f5605c0e9c62b0393235062c8929500aca5b54"
        ),
        "0004_local_run_input_admissions.py": (
            "d47eb32ac70084eab237d48a9f5678bfdc4d09057e47d8cbb727e9d4770026a1"
        ),
        "0005_profile_output_ports.py": (
            "af30394298fed43a53a7be86f23256d63a4d97217d0fceb1718d77c81d351547"
        ),
        "0006_typed_local_writes.py": (
            "132a4a8ff77a5a48ad45538beaded4480b45b7bf4006fe504e02e1481845507c"
        ),
        "0007_workspace_provider_bindings.py": (
            "5bd4feb0205b08e19275f6513644b347a5d5fd0fe1d45d9bd5e47a3fc1b3800c"
        ),
        "0008_managed_local_lance_writes.py": (
            "3aef01923a3b252285a78fbbce9d8173630264bf51315934b90aa4601454e540"
        ),
        "0009_durable_local_write_tasks.py": (
            "2ed6efefd51b1ed51f5487742b8acae0e78c1f960a4f7454687ddbf83ee6f2e1"
        ),
        "0010_durable_external_waits.py": (
            "183506ed4f43142cbaff7e63ee47267d5c9bcd7969c94efb28e62e3b84a4d7cf"
        ),
        "0011_external_wait_publication.py": (
            "bc779148f2d745f0ef0a0e227dd1877eb08b92d7ac8184f28c6608e3fefebfaf"
        ),
        "0012_linear_checkpoint_admission.py": (
            "83237f57a39bdb92aa52d660aea9d7c4dddfc4bf7636ff18523b3fcec418214f"
        ),
        "0013_linear_checkpoint_commit.py": (
            "628fda9d102d6ad024054139552ad9d83123081694bb967443843e8aad19aea4"
        ),
        "0014_checkpoint_mat_identity.py": (
            "34a3986d904368437e6735291fb9f947604b72dcbd6f481f020872e6a8485337"
        ),
        "0015_task_inbox_items.py": (
            "cbec03f8d2d528ed9e7c0d5d214aca4424614c4619a835d221adfaf913abaa1e"
        ),
        "0016_bounded_fanout_plan.py": (
            "ab7c8a3e9453252cb2b00865468e9586b85eedf6585c68aaf25db80b8ffced97"
        ),
        "0017_linear_checkpoint_inbox.py": (
            "8d3270743914ac27bfcdf647c373be6e230804e70320ad27cfeb9e35ff574124"
        ),
        "0018_bounded_fanout_write.py": (
            "a97c12c480e1d1b83a0ee578a4440746cdc18a7aeee86cce4ce7b0e3be426a1f"
        ),
        "0019_exact_local_inputs.py": (
            "624acded9fe082851227f5822d9343b371f91a6814424b77f74e2ec3ded56d35"
        ),
        "0020_execution_manifests.py": (
            "3d4bf47d829ca8d1f1c4c6f3f41a0199e3d718a66f064a03c6e7be3f68e2ad22"
        ),
        "0021_manifest_output_owners.py": (
            "07fbf7ebe8ff9434037a4450e563f97ce62f3efb9bb287787d6b927627deb887"
        ),
        "0022_task_manifests.py": (
            "105d5d314c0ab1a9faf250a6993a9608cf92d2c6a90ad495782f837425fe324f"
        ),
        "0023_catalog_folder_workspace_overlay.py": (
            "4aa7f207cd122847253bb5f40a6c9a8eab454434d1a14a3c0b5415f95aba6de7"
        ),
        "0024_promoted_transform_versions.py": (
            "ecf98726b68f39faa2ebd4fd08f45798baa6d60446d844805f9ddaab9884767a"
        ),
        "0025_transform_library_keys.py": (
            "e8cec95b871d07febaf9762c94f66c1bacb5a812b3064314e5926c70523861cc"
        ),
        "0026_dataset_views.py": (
            "8ce5cbbc09987ad539c9a65ec544373559df0850f4dde27ba079bd13b4b432e9"
        ),
        "0027_distribution_reports.py": (
            "1cb20334daf75c5d010dd296f996ff97daef86baab917f5c7c430855d1a82974"
        ),
        "0028_sparse_output_admission.py": (
            "1c467a37a61799d282705d0c0ad2a547d3f63212841a49a9883e4e1ae32c4f86"
        ),
        "0029_sparse_output_materialization.py": (
            "bf3952ea9f67c3f965af293f2ddcad059a2446a60bec7f7096a4ee98efd62844"
        ),
        "0030_merge_columns_publication.py": (
            "b8f927ee183719645c0daf07bd8ef34b3abdaf99fc019219ede20f7e41dac3f9"
        ),
        "0031_durable_merge_columns.py": (
            "3262058f6dd3b5091c91d2ad241c9051fb1e7dc9afb9ac35118aaa8577eb2f37"
        ),
        "0032_temporal_publication.py": (
            "be5f868b1ab1936a21773771c18d312cbf2e2f13bc5d94450664dff2f551cbeb"
        ),
        "0033_temporal_resample_task.py": (
            "0d47e6da5b505575950b01bb20f1fe340c91a744ee451f15c135727fddc7306c"
        ),
    }
    revision_paths = {path.name: path for path in versions_path.glob("*.py")}

    assert revision_paths.keys() == expected_hashes.keys(), (
        "record every new forward migration in the immutable revision checksum guard"
    )
    for name, expected_hash in expected_hashes.items():
        assert hashlib.sha256(revision_paths[name].read_bytes()).hexdigest() == expected_hash, (
            "committed migration revisions are immutable; add a forward migration instead"
        )


def test_transform_library_keys_backfill_and_round_trip_from_0024(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'transform-library-keys.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0024_promoted_transforms")
        first_id, second_id = "tr_" + "a" * 29, "tr_" + "b" * 29
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO users (id, name, is_admin, created_at) "
                "VALUES ('transform-migration-owner', 'Transform owner', 0, CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO promoted_transforms (id, owner_id, key, created_at) VALUES "
                "(:first_id, 'transform-migration-owner', 'first', CURRENT_TIMESTAMP), "
                "(:second_id, 'transform-migration-owner', 'second', CURRENT_TIMESTAMP)"
            ), {"first_id": first_id, "second_id": second_id})
            connection.execute(sa.text(
                "INSERT INTO promoted_transform_versions "
                "(transform_id, version, semantic_digest, title, blurb, category, mode, code, "
                "input_schema, output_schema, requirements, creator_id, created_at) VALUES "
                "(:first_id, 1, :first_digest, 'Ä robot', 'First', 'Robotics', 'map', "
                "'def fn(row): return row', '[]', '[]', '[]', "
                "'transform-migration-owner', CURRENT_TIMESTAMP), "
                "(:first_id, 2, :moved_digest, 'ZZZ robot', 'First moved', 'Robotics', 'map', "
                "'def fn(row): return row', '[]', '[]', '[]', "
                "'transform-migration-owner', CURRENT_TIMESTAMP), "
                "(:second_id, 2, :second_digest, 'Ö robot', 'Second', 'Robotics', 'map', "
                "'def fn(row): return row', '[]', '[]', '[]', "
                "'transform-migration-owner', CURRENT_TIMESTAMP)"
            ), {
                "first_id": first_id, "second_id": second_id,
                "first_digest": "a" * 64, "second_digest": "b" * 64,
                "moved_digest": "c" * 64,
            })

        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "head")
            identities = connection.execute(sa.text(
                "SELECT id, library_sort_key FROM promoted_transforms ORDER BY library_sort_key"
            )).mappings().all()
            assert [row["id"] for row in identities] == [first_id, second_id]
            assert identities[0]["library_sort_key"] == "ä robot".encode("utf-8").hex()
            assert identities[1]["library_sort_key"] == "ö robot".encode("utf-8").hex()
            rows = connection.execute(sa.text(
                "SELECT transform_id, version, library_search_text, "
                "library_category_key, library_mode_key "
                "FROM promoted_transform_versions ORDER BY transform_id, version"
            )).mappings().all()
            assert [(row["transform_id"], row["version"]) for row in rows] == [
                (first_id, 1), (first_id, 2), (second_id, 2),
            ]
            assert rows[0]["library_search_text"] == (
                f"ä robot\nfirst\nrobotics\nmap\n{first_id}")
            assert rows[0]["library_category_key"] == "robotics"
            assert rows[0]["library_mode_key"] == "map"
            assert all(all(row[key] for key in (
                "library_search_text", "library_category_key", "library_mode_key",
            )) for row in rows)
            assert "library_sort_key" not in {
                column["name"]
                for column in inspect(connection).get_columns("promoted_transform_versions")
            }

        page = metadb.promoted_transform_library_page(
            "transform-migration-owner", limit=10)
        assert [(row["id"], row["version"], row["title"]) for row in page] == [
            (first_id, "v2", "ZZZ robot"),
            (second_id, "v2", "Ö robot"),
        ]

        with metadb.engine().connect() as connection:
            command.downgrade(metadb._alembic_cfg(connection), "0024_promoted_transforms")
            assert "library_sort_key" not in {
                column["name"]
                for column in inspect(connection).get_columns("promoted_transforms")
            }
            assert connection.execute(sa.text(
                "SELECT transform_id, version, title FROM promoted_transform_versions "
                "ORDER BY transform_id, version"
            )).all() == [
                (first_id, 1, "Ä robot"),
                (first_id, 2, "ZZZ robot"),
                (second_id, 2, "Ö robot"),
            ]
            command.upgrade(metadb._alembic_cfg(connection), "head")
            assert connection.execute(sa.text(
                "SELECT count(*) FROM promoted_transform_versions "
                "WHERE library_search_text IS NOT NULL "
                "AND library_category_key IS NOT NULL AND library_mode_key IS NOT NULL"
            )).scalar_one() == 3
            assert connection.execute(sa.text(
                "SELECT count(*) FROM promoted_transforms WHERE library_sort_key IS NOT NULL"
            )).scalar_one() == 2


def test_task_manifest_upgrade_preserves_legacy_frozen_admission(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'legacy-task-manifest.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0021_manifest_output_owners")
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO users (id, name, is_admin, created_at) "
                "VALUES ('legacy-task-owner', 'Legacy owner', 0, CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO canvases "
                "(id, owner_id, name, version, doc, visibility, created_at, updated_at) VALUES "
                "('legacy-task-canvas', 'legacy-task-owner', 'Legacy canvas', 1, '{}', "
                "'private', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO durable_tasks "
                "(id, owner_id, canvas_id, submission_id, intent_sha256, target_node_id, "
                "task_kind, backend_kind, graph_doc, input_manifest, write_intent, status, "
                "status_doc, created_at, updated_at) VALUES "
                "('legacy-task', 'legacy-task-owner', 'legacy-task-canvas', 'submission', "
                ":digest, 'write', 'managed_local_write', 'local', :graph, '[]', :intent, "
                "'queued', :status, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {
                    "digest": "a" * 64,
                    "graph": '{"id":"legacy-task-canvas","version":1,"nodes":[],"edges":[]}',
                    "intent": '{"legacy":true}',
                    "status": '{"run_id":"legacy-task","status":"queued"}',
                })
            connection.execute(sa.text(
                "INSERT INTO durable_task_attempts "
                "(id, task_id, attempt_number, status, created_at) VALUES "
                "('legacy-attempt', 'legacy-task', 1, 'queued', CURRENT_TIMESTAMP)"))
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "head")
            task = connection.execute(sa.text(
                "SELECT execution_manifest_sha256, graph_doc, input_manifest, write_intent "
                "FROM durable_tasks WHERE id = 'legacy-task'")).mappings().one()
            attempt_sha = connection.execute(sa.text(
                "SELECT execution_manifest_sha256 FROM durable_task_attempts "
                "WHERE id = 'legacy-attempt'")).scalar_one()

        assert task["execution_manifest_sha256"] is None
        assert task["graph_doc"] is not None
        assert task["input_manifest"] == "[]"
        assert task["write_intent"] == '{"legacy":true}'
        assert attempt_sha is None


def test_linear_checkpoint_downgrade_rejects_retained_hidden_rows(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'checkpoint-downgrade.db'}"):
        metadb.migrate_db()
        now = metadb._now()
        with metadb.session() as session:
            session.add(metadb.User(id="checkpoint-owner", name="Checkpoint owner"))
            session.flush()
            session.add(metadb.Canvas(
                id="checkpoint-canvas", owner_id="checkpoint-owner", name="Checkpoint", doc="{}"))
            session.flush()
            session.add(metadb.DurableTask(
                id="checkpoint-task", owner_id="checkpoint-owner", canvas_id="checkpoint-canvas",
                submission_id="submission", intent_sha256="a" * 64,
                target_node_id="final", task_kind="linear_checkpoint_write",
                backend_kind="local", graph_doc="{}", input_manifest="[]", write_intent="{}",
                status="queued", status_doc="{}", created_at=now, updated_at=now))
        with metadb.engine().connect() as connection:
            with pytest.raises(RuntimeError, match="cannot downgrade"):
                command.downgrade(
                    metadb._alembic_cfg(connection), "0011_external_wait_publication")
        assert metadb._current_schema_heads() == ("0012_linear_checkpoint_admission",)

        # Runtime metadata has newer manifest columns that are intentionally absent at revision 0012.
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "DELETE FROM durable_tasks WHERE id = 'checkpoint-task'"))
        with metadb.engine().connect() as connection:
            command.downgrade(
                metadb._alembic_cfg(connection), "0011_external_wait_publication")
            assert "durable_checkpoints" not in inspect(connection).get_table_names()
            command.upgrade(metadb._alembic_cfg(connection), "head")


def test_historical_baseline_upgrade_repairs_workspace_metadata_without_data_loss(tmp_path):
    """Exercise the exact old schema shape instead of fixing committed revision 0001 in place."""
    with _isolated_metadata(f"sqlite:///{tmp_path / 'historical.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0001_schema_baseline")

        # These are exactly the post-baseline additions that stranded databases created at 09339e9
        # without. Keep this fixture explicit so the regression exercises a real historical shape.
        with metadb.engine().begin() as connection:
            connection.execute(sa.text("DROP TABLE workspace_placements"))
            connection.execute(sa.text("DROP TABLE workspace_containers"))
            connection.execute(sa.text("ALTER TABLE run_records DROP COLUMN profile"))
            connection.execute(sa.text("""
                INSERT INTO canvases (id, owner_id, name, version, doc, visibility, created_at, updated_at)
                VALUES ('historical-canvas', 'local', 'Historical', 7, '{\"nodes\":[]}', 'private',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """))
            connection.execute(sa.text("""
                INSERT INTO run_records (id, canvas_id, status, outputs, created_at)
                VALUES ('historical-run', 'historical-canvas', 'ok', '[]', CURRENT_TIMESTAMP)
            """))
            connection.execute(sa.text("""
                INSERT INTO catalog_entries (uri, registration_id, name, doc, updated_at)
                VALUES ('file:///historical.parquet', 'historical-catalog-entry-00000001',
                        'Historical catalog entry', '{}', CURRENT_TIMESTAMP)
            """))
            connection.execute(sa.text("""
                INSERT INTO settings (scope, scope_id, key, value)
                VALUES ('global', '', 'historical.setting', :value)
            """), {"value": '{"preserved":true}'})

        assert metadb._current_schema_heads() == ("0001_schema_baseline",)
        assert metadb.migrate_db() == metadb.expected_schema_head()
        metadb.init_db()  # A repaired local database must restart at head cleanly.

        with metadb.engine().connect() as connection:
            inspector = inspect(connection)
            assert {"workspace_containers", "workspace_placements"} <= set(inspector.get_table_names())
            assert "profile" in {column["name"] for column in inspector.get_columns("run_records")}
            assert {index["name"] for index in inspector.get_indexes("workspace_containers")} >= {
                "ix_workspace_containers_parent_id"
            }
            assert {index["name"] for index in inspector.get_indexes("workspace_placements")} >= {
                "ix_workspace_placements_container_id"
            }
            assert connection.execute(sa.text("""
                SELECT id, name FROM workspace_containers WHERE id = 'workspace-local-root'
            """)).one() == ("workspace-local-root", "Workspace")
            assert connection.execute(sa.text("SELECT count(*) FROM workspace_containers")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT version, doc FROM canvases WHERE id = 'historical-canvas'")) \
                .one() == (7, '{"nodes":[]}')
            assert connection.execute(sa.text("SELECT status, outputs FROM run_records WHERE id = 'historical-run'")) \
                .one() == ("ok", "[]")
            assert connection.execute(sa.text("SELECT name FROM catalog_entries WHERE uri = 'file:///historical.parquet'")) \
                .scalar_one() == "Historical catalog entry"
            assert connection.execute(sa.text("SELECT value FROM settings WHERE key = 'historical.setting'")) \
                .scalar_one() == '{"preserved":true}'

            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "target_metadata": metadb.Base.metadata},
            )
            assert compare_metadata(context, metadb.Base.metadata) == []

        assert metadb.migrate_db() == metadb.expected_schema_head()


def test_catalog_folder_overlay_upgrade_and_dataset_only_downgrade_round_trip(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'catalog-folder-overlay.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0022_task_manifests")
        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO catalog_folders (path, created_at) "
                "VALUES ('research', CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO catalog_entries "
                "(uri, registration_id, name, doc, folder, usage, updated_at) VALUES "
                "('file:///migration-dataset.parquet', 'migration-dataset-000000000001', "
                "'Migration dataset', '{}', 'research', 0, CURRENT_TIMESTAMP)"))
            connection.execute(sa.text(
                "INSERT INTO workspace_placements "
                "(id, container_id, target_kind, target_id, name, ordinal, version) VALUES "
                "('migration-placement', 'workspace-local-root', 'dataset', "
                "'migration-dataset-000000000001', 'Migration dataset', 0, 1)"))

        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "head")
            assert connection.execute(sa.text(
                "SELECT c.catalog_folder_path FROM workspace_placements p "
                "JOIN workspace_containers c ON c.id = p.container_id "
                "WHERE p.id = 'migration-placement'"
            )).scalar_one() == "research"

            command.downgrade(metadb._alembic_cfg(connection), "0022_task_manifests")
            assert connection.execute(sa.text(
                "SELECT container_id FROM workspace_placements "
                "WHERE id = 'migration-placement'"
            )).scalar_one() == "workspace-local-root"
            assert "catalog_folder_id" not in {
                column["name"] for column in inspect(connection).get_columns("workspace_containers")
            }

            command.upgrade(metadb._alembic_cfg(connection), "head")
            assert connection.execute(sa.text(
                "SELECT c.catalog_folder_path FROM workspace_placements p "
                "JOIN workspace_containers c ON c.id = p.container_id "
                "WHERE p.id = 'migration-placement'"
            )).scalar_one() == "research"


def test_dataset_view_migration_round_trip_and_retention_guard(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'dataset-views.db'}"):
        with metadb.engine().connect() as connection:
            command.upgrade(metadb._alembic_cfg(connection), "0024_promoted_transforms")
            command.upgrade(metadb._alembic_cfg(connection), "head")
        with metadb.engine().begin() as connection:
            assert "dataset_views" in inspect(connection).get_table_names()
            connection.execute(sa.text(
                "INSERT INTO dataset_views "
                "(id, owner_id, submission_id, request_sha256, definition_sha256, "
                "definition_doc, created_at) VALUES "
                "('migration-view', 'local', 'migration-submission', :request, :definition, "
                "'{}', CURRENT_TIMESTAMP)"
            ), {"request": "a" * 64, "definition": "b" * 64})
            connection.execute(sa.text(
                "INSERT INTO workspace_placements "
                "(id, container_id, target_kind, target_id, name, ordinal, version) VALUES "
                "('migration-view-placement', 'workspace-local-root', 'dataset_view', "
                "'migration-view', 'Migration view', 0, 1)"
            ))

        with metadb.engine().connect() as connection:
            with pytest.raises(RuntimeError, match="cannot downgrade"):
                command.downgrade(
                    metadb._alembic_cfg(connection), "0024_promoted_transforms")

        with metadb.engine().begin() as connection:
            connection.execute(sa.text(
                "DELETE FROM workspace_placements WHERE id = 'migration-view-placement'"))
            connection.execute(sa.text(
                "DELETE FROM dataset_views WHERE id = 'migration-view'"))
        with metadb.engine().connect() as connection:
            command.downgrade(
                metadb._alembic_cfg(connection), "0024_promoted_transforms")
            assert "dataset_views" not in inspect(connection).get_table_names()
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(sa.text(
                    "INSERT INTO workspace_placements "
                    "(id, container_id, target_kind, target_id, name, ordinal, version) VALUES "
                    "('forbidden-view-placement', 'workspace-local-root', 'dataset_view', "
                    "'missing-view', 'Forbidden view', 0, 1)"
                ))
            connection.rollback()
            command.upgrade(metadb._alembic_cfg(connection), "head")


def test_fresh_sqlite_baseline_matches_runtime_metadata(tmp_path):
    with _isolated_metadata(f"sqlite:///{tmp_path / 'baseline.db'}"):
        metadb.migrate_db()
        with metadb.engine().connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "target_metadata": metadb.Base.metadata},
            )
            assert compare_metadata(context, metadb.Base.metadata) == []

            installation = connection.execute(sa.text(
                "SELECT id, owner_token, storage_namespace FROM installation_identity"
            )).one()
            assert installation.id == 1
            assert len(installation.owner_token) == 32
            assert installation.storage_namespace
            local_registry = connection.execute(sa.text(
                "SELECT id, owner_token FROM local_result_registry"
            )).one()
            assert local_registry.id == 1
            assert len(local_registry.owner_token) == 32

            inspector = inspect(connection)
            for table in metadb.Base.metadata.sorted_tables:
                actual_columns = {
                    column["name"]: column
                    for column in inspector.get_columns(table.name)
                }
                for column in table.columns:
                    assert _normalized_default(actual_columns[column.name]["default"]) == (
                        _normalized_default(column.server_default)
                    ), f"server default drift for {table.name}.{column.name}"

                expected_checks = {
                    (constraint.name, str(constraint.sqltext))
                    for constraint in table.constraints
                    if isinstance(constraint, sa.CheckConstraint)
                }
                actual_checks = {
                    (constraint["name"], constraint["sqltext"])
                    for constraint in inspector.get_check_constraints(table.name)
                }
                assert actual_checks == expected_checks, (
                    f"check constraint drift for {table.name}"
                )

                expected_uniques = {
                    (constraint.name, tuple(column.name for column in constraint.columns))
                    for constraint in table.constraints
                    if isinstance(constraint, sa.UniqueConstraint)
                }
                actual_uniques = {
                    (constraint["name"], tuple(constraint["column_names"]))
                    for constraint in inspector.get_unique_constraints(table.name)
                }
                assert actual_uniques == expected_uniques, (
                    f"unique constraint drift for {table.name}"
                )


def test_concurrent_fresh_sqlite_startup_is_serialized_across_48_processes(tmp_path):
    db_path = tmp_path / "metadata.db"
    env = os.environ.copy()
    env.update({
        "DP_DATABASE_URL": f"sqlite:///{db_path}",
        "DP_WORKSPACE": str(tmp_path),
        "DP_DATA_DIR": str(tmp_path / "data"),
    })
    env.pop("DP_AUTH_PASSWORD", None)
    command = [sys.executable, "-c", "from hub import metadb; metadb.init_db()"]
    processes = [
        subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(48)
    ]
    failures = []
    results = {}
    timed_out = set()
    deadline = time.monotonic() + 150
    try:
        for index, process in enumerate(processes):
            remaining = deadline - time.monotonic()
            if process.poll() is None and remaining <= 0:
                timed_out.add(index)
                failures.append(f"process {index}: exceeded the shared 150-second deadline")
                continue
            try:
                results[index] = process.communicate(timeout=max(0, remaining))
            except subprocess.TimeoutExpired:
                timed_out.add(index)
                failures.append(f"process {index}: exceeded the shared 150-second deadline")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for index, process in enumerate(processes):
            if index not in results:
                results[index] = process.communicate(timeout=5)

    for index, process in enumerate(processes):
        if process.returncode != 0 and index not in timed_out:
            stdout, stderr = results[index]
            failures.append(f"process {index}: {stderr or stdout}")

    assert not failures, "\n".join(failures)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            metadb.expected_schema_head(),)
        assert connection.execute("SELECT id FROM users WHERE id = 'local'").fetchone() == ("local",)


def test_sqlite_lock_path_is_canonical_for_relative_symlink_and_uri_urls(tmp_path, monkeypatch):
    real_workspace = tmp_path / "real"
    real_workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_workspace, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    expected = f"{(real_workspace / 'metadata.db').resolve()}.migrate.lock"

    with _isolated_metadata("sqlite:///alias/metadata.db"):
        assert metadb._sqlite_file_lock_path() == expected
    with _isolated_metadata(f"sqlite:///file:{real_workspace / 'metadata.db'}?uri=true"):
        assert metadb._sqlite_file_lock_path() == expected


@pytest.mark.parametrize("url_name, expected_name", [
    ("metadata.db?mode=memory", "metadata.db"),
    ("file::memory:?cache=shared", "file::memory:"),
])
def test_sqlite_memory_syntax_without_uri_semantics_remains_disk_backed(
        url_name, expected_name, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{url_name}"
    expected_path = (tmp_path / expected_name).resolve()

    with _isolated_metadata(url):
        assert metadb._sqlite_is_memory_or_temporary() is False
        assert metadb._sqlite_file_lock_path() == f"{expected_path}.migrate.lock"
        metadb.init_db()
        with metadb.engine().connect() as connection:
            databases = connection.exec_driver_sql("PRAGMA database_list").all()
            database_path = next(row[2] for row in databases if row[1] == "main")
        assert Path(database_path).resolve() == expected_path

    assert expected_path.is_file()


@pytest.mark.parametrize("url", [
    "sqlite://",
    "sqlite:///:memory:",
    "sqlite:///file::memory:?cache=shared&uri=true",
    "sqlite:///file:shared-memory?mode=memory&cache=shared&uri=true",
])
def test_process_local_sqlite_databases_use_no_file_lock_and_share_the_migrated_connection(url):
    with _isolated_metadata(url):
        assert metadb._sqlite_file_lock_path() is None
        metadb.init_db()
        assert metadb.require_schema_at_head() == metadb.expected_schema_head()
        assert metadb.resolve_user("local") == "local"


def test_nonempty_unversioned_database_is_not_silently_stamped(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")

    with _isolated_metadata(f"sqlite:///{db_path}"):
        with pytest.raises(metadb.SchemaNotReadyError, match="non-empty.*valid Alembic revision"):
            metadb.migrate_db()

    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"users"}


def test_bootstrap_password_without_session_secret_is_rejected_not_discarded(tmp_path, monkeypatch):
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)
    monkeypatch.setenv("DP_AUTH_PASSWORD", "must-not-be-discarded")

    with _isolated_metadata(f"sqlite:///{tmp_path / 'metadata.db'}"):
        with pytest.raises(metadb.SchemaNotReadyError, match="requires a non-empty DP_AUTH_SECRET"):
            metadb.migrate_db()

    assert os.environ["DP_AUTH_PASSWORD"] == "must-not-be-discarded"


@pytest.mark.parametrize("secret, message", [
    ("   ", "configured but blank"),
    ("test", "known-weak/default"),
])
def test_migration_rejects_unusable_session_signing_secret(secret, message, tmp_path, monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", secret)
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)

    if not secret.strip():
        from hub import auth
        assert auth.auth_enabled() is False

    with _isolated_metadata(f"sqlite:///{tmp_path / 'metadata.db'}"):
        with pytest.raises(metadb.SchemaNotReadyError, match=message):
            metadb.migrate_db()


def test_fresh_session_auth_database_requires_login_capable_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("DP_AUTH_SECRET", "0123456789abcdef0123456789abcdef")
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("DP_AUTH_MODE", raising=False)

    with _isolated_metadata(f"sqlite:///{tmp_path / 'metadata.db'}"):
        with pytest.raises(metadb.SchemaNotReadyError, match="no administrator has a login credential"):
            metadb.migrate_db()
        assert metadb.schema_at_head() is True
        with pytest.raises(metadb.SchemaNotReadyError, match="no administrator has a login credential"):
            metadb.init_db()


def test_internal_auth_mode_marker_does_not_require_a_login_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("DP_AUTH_SECRET", raising=False)
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("DP_AUTH_MODE", "1")

    with _isolated_metadata(f"sqlite:///{tmp_path / 'worker.db'}"):
        metadb.init_db()
        assert metadb.require_schema_at_head() == metadb.expected_schema_head()
        assert metadb.user_password_hash(metadb.DEFAULT_USER_ID) is None


def test_non_sqlite_service_startup_checks_head_without_running_migrations(monkeypatch):
    calls: list[str] = []
    monkeypatch.delenv("DP_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(metadb, "_is_sqlite_database", lambda: False)
    monkeypatch.setattr(metadb, "require_schema_at_head", lambda: calls.append("check") or "head")
    monkeypatch.setattr(metadb, "_upgrade_schema_and_bootstrap",
                        lambda: pytest.fail("service startup attempted migration"))
    monkeypatch.setattr(metadb, "reap_kernels", lambda: calls.append("reap-kernels"))
    monkeypatch.setattr(metadb, "reap_orphaned_runs", lambda: calls.append("reap-runs"))

    metadb.init_db()

    assert calls == ["check", "reap-kernels", "reap-runs"]


def test_non_sqlite_bootstrap_secret_is_rejected_outside_explicit_migration(monkeypatch):
    monkeypatch.setenv("DP_AUTH_PASSWORD", "migration-only-secret")
    monkeypatch.setattr(metadb, "_is_sqlite_database", lambda: False)
    monkeypatch.setattr(metadb, "require_schema_at_head", lambda: "head")

    with pytest.raises(metadb.SchemaNotReadyError, match="accepted only.*dataplay migrate"):
        metadb.init_db()


def test_explicit_migrate_is_the_only_non_sqlite_upgrade_path(monkeypatch):
    monkeypatch.setattr(metadb, "_is_sqlite_database", lambda: False)
    monkeypatch.setattr(metadb, "_upgrade_schema_and_bootstrap", lambda: "expected-head")

    assert metadb.migrate_db() == "expected-head"


def test_schema_check_detects_a_database_behind_head(tmp_path):
    from alembic import command

    with _isolated_metadata(f"sqlite:///{tmp_path / 'behind.db'}"):
        metadb.migrate_db()
        with metadb.engine().connect() as connection:
            command.downgrade(metadb._alembic_cfg(connection), "-1")
        assert metadb.schema_at_head() is False
        with pytest.raises(metadb.SchemaNotReadyError, match="not at required Alembic head"):
            metadb.require_schema_at_head()
