from types import SimpleNamespace

import pytest

from hub.models import ColumnSchema, Graph, RunStatus
from hub.relationships import suggest_joins
from hub.row_reference_diagnosis import (
    ROW_REFERENCE_TARGET_MISMATCH, diagnose_durable_field_projections, diagnose_key_pair,
    diagnose_key_pairs,
    input_identity,
)


def _column(name: str, target: dict | None = None) -> ColumnSchema:
    return ColumnSchema.model_validate({
        "name": name, "type": "int",
        **({"rowReference": {
            "target": target, "keyFields": ["id"], "provenance": "lineage",
        }} if target else {}),
    })


def test_exact_target_conflict_is_not_overridden_by_one_to_one_cardinality():
    left = [_column("customer_id", {"kind": "exact", "datasetId": "customers", "revisionId": "r1"})]
    right = [_column("id")]
    diagnosis = diagnose_key_pair(
        left_input=input_identity(dataset_id="events", revision_id="r1"),
        right_input=input_identity(dataset_id="accounts", revision_id="r1"),
        left_columns=left, right_columns=right, left_field="customer_id", right_field="id")
    assert diagnosis.status == "conflict"
    assert ROW_REFERENCE_TARGET_MISMATCH == "row_reference_target_mismatch"
    assert suggest_joins(
        left, right, lambda _columns: True, lambda _columns: True,
        input_identity(dataset_id="events", revision_id="r1"),
        input_identity(dataset_id="accounts", revision_id="r1"),
    ) == []


def test_matching_exact_target_ranks_before_name_only_candidate():
    left = [
        _column("account_id", {"kind": "exact", "datasetId": "accounts", "revisionId": "r1"}),
        _column("id"),
    ]
    right = [_column("id"), _column("account_id")]
    suggestions = suggest_joins(
        left, right, lambda _columns: True, lambda _columns: True,
        input_identity(dataset_id="events", revision_id="r1"),
        input_identity(dataset_id="accounts", revision_id="r1"),
    )
    assert suggestions[0].left_columns == ["account_id"]
    assert suggestions[0].row_reference[0].status == "compatible"


def test_renamed_reference_candidate_is_ranked_and_cardinality_is_measured():
    scans: list[list[str]] = []
    left = [_column("copied_owner", {
        "kind": "exact", "datasetId": "owners", "revisionId": "r1"})]
    right = [_column("id")]
    suggestions = suggest_joins(
        left, right, lambda columns: scans.append(columns) or True,
        lambda columns: scans.append(columns) or True,
        input_identity(dataset_id="events", revision_id="r1"),
        input_identity(dataset_id="owners", revision_id="r1"),
    )
    assert scans == [["copied_owner"], ["id"]]
    assert suggestions[0].left_columns == ["copied_owner"]
    assert suggestions[0].right_columns == ["id"]
    assert suggestions[0].cardinality == "1:1"
    assert suggestions[0].confidence == "verified"
    assert suggestions[0].row_reference[0].status == "compatible"

    wrong = [_column("copied_owner", {
        "kind": "exact", "datasetId": "other", "revisionId": "r1"})]
    assert suggest_joins(
        wrong, right, lambda _columns: pytest.fail("conflict must not scan"),
        lambda _columns: pytest.fail("conflict must not scan"),
        input_identity(dataset_id="events", revision_id="r1"),
        input_identity(dataset_id="owners", revision_id="r1"),
    ) == []
    assert suggest_joins(
        left, right, lambda _columns: pytest.fail("unknown reference must not scan"),
        lambda _columns: pytest.fail("unknown reference must not scan"),
        input_identity(dataset_id="events", revision_id="r1"), None,
    ) == []


def test_missing_or_exact_peer_unavailability_stays_unknown():
    diagnosis = diagnose_key_pair(
        left_input=input_identity(dataset_id="events", revision_id="r1"),
        right_input=input_identity(dataset_id="accounts"),
        left_columns=[_column("account_id", {"kind": "exact", "datasetId": "accounts", "revisionId": "r1"})],
        right_columns=[_column("id")], left_field="account_id", right_field="id")
    assert diagnosis.status == "unknown"
    assert diagnosis.reason == "peer_exact_identity_unavailable"


def test_declared_target_key_must_match_the_complete_configured_key_sequence():
    left = [_column("customer_id", {
        "kind": "exact", "datasetId": "customers", "revisionId": "r1"})]
    right = [_column("account_id"), _column("id")]
    mismatch = diagnose_key_pair(
        left_input=input_identity(dataset_id="events", revision_id="r1"),
        right_input=input_identity(dataset_id="customers", revision_id="r1"),
        left_columns=left, right_columns=right, left_field="customer_id", right_field="account_id",
        left_peer_fields=["account_id"], right_peer_fields=["customer_id"])
    assert mismatch.status == "conflict"
    assert mismatch.reason == "declared_target_key_differs_from_join_key"
    composite = diagnose_key_pair(
        left_input=input_identity(dataset_id="events", revision_id="r1"),
        right_input=input_identity(dataset_id="customers", revision_id="r1"),
        left_columns=[_column("customer_id", {
            "kind": "exact", "datasetId": "customers", "revisionId": "r1"})],
        right_columns=right, left_field="customer_id", right_field="account_id",
        left_peer_fields=["account_id", "id"], right_peer_fields=["customer_id"])
    assert composite.status == "conflict"


def test_unequal_key_lists_are_explicit_unknown_not_truncated():
    diagnoses = diagnose_key_pairs(
        left_input=None, right_input=None, left_columns=[], right_columns=[],
        left_fields=["a", "b"], right_fields=["a"])
    assert diagnoses[0].status == "unknown"
    assert diagnoses[0].reason == "join_key_pair_malformed"


def test_retained_durable_replay_adopts_before_row_reference_diagnosis(monkeypatch):
    from hub.routers import runs

    graph = Graph.model_validate({
        "id": "replay-before-diagnosis", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "/not/read"}}},
            {"id": "write", "type": "write", "data": {"config": {}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })
    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs, "_require_graph_read_access", lambda *_args: None)
    monkeypatch.setattr(runs.metadb, "canvas_exists", lambda _canvas: True)
    monkeypatch.setattr(runs.metadb, "durable_task_submission_id", lambda *_args: "retained")
    monkeypatch.setattr(runs.metadb, "durable_task", lambda *_args, **_kwargs: {
        "id": "retained", "execution_manifest_sha256": "a" * 64})
    monkeypatch.setattr(runs, "_adopt_manifest_durable_task", lambda *_args: RunStatus(
        run_id="retained", status="queued"))
    # No catalog/adapter fields exist on this deps object. Reaching the new diagnosis gate would
    # fail, proving that retained adoption remains before it.
    status, owner = runs.start_run(
        SimpleNamespace(), graph, "write", "local", confirmed=True, submission_id="retry")
    assert status.run_id == "retained"
    assert owner is None


def test_fresh_source_free_admission_never_requires_catalog_or_adapter(monkeypatch):
    from hub.routers import runs

    graph = Graph.model_validate({
        "id": "source-free-admission", "version": 1,
        "nodes": [
            {"id": "wait", "type": "external_wait_fixture", "data": {"config": {}}},
            {"id": "write", "type": "write", "data": {"config": {}}},
        ],
        "edges": [{"id": "wait-write", "source": "wait", "target": "write"}],
    })

    class ReachedFreshAdmission(Exception):
        pass

    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs, "_require_graph_read_access", lambda *_args: None)
    monkeypatch.setattr(runs.metadb, "canvas_exists", lambda *_args: False)
    monkeypatch.setattr(runs, "_resolve_parameters", lambda graph, *_args, **_kwargs: graph)
    monkeypatch.setattr(runs, "_reject_invalid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runs.graph_mod, "resolve_source_refs",
        lambda *_args: pytest.fail("source-free admission must not resolve catalog refs"))
    monkeypatch.setattr(
        runs, "_reject_row_reference_target_mismatch",
        lambda *_args: (_ for _ in ()).throw(ReachedFreshAdmission()))

    with pytest.raises(ReachedFreshAdmission):
        runs.start_run(SimpleNamespace(), graph, "write", "owner", confirmed=True)


def test_fresh_source_admission_still_resolves_catalog_refs(monkeypatch):
    from hub.routers import runs

    graph = Graph.model_validate({
        "id": "source-admission", "version": 1,
        "nodes": [
            {"id": "source", "type": "source", "data": {"config": {"uri": "catalog-name"}}},
            {"id": "write", "type": "write", "data": {"config": {}}},
        ],
        "edges": [{"id": "source-write", "source": "source", "target": "write"}],
    })

    class ReachedFreshAdmission(Exception):
        pass

    resolved: list[str] = []
    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs, "_require_graph_read_access", lambda *_args: None)
    monkeypatch.setattr(runs.metadb, "canvas_exists", lambda *_args: False)
    monkeypatch.setattr(runs, "_resolve_parameters", lambda graph, *_args, **_kwargs: graph)
    monkeypatch.setattr(runs, "_reject_invalid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runs.graph_mod, "resolve_source_refs",
        lambda _graph, resolver: resolved.append(resolver("catalog-name")))
    monkeypatch.setattr(
        runs, "_reject_row_reference_target_mismatch",
        lambda *_args: (_ for _ in ()).throw(ReachedFreshAdmission()))

    with pytest.raises(ReachedFreshAdmission):
        runs.start_run(
            SimpleNamespace(catalog=SimpleNamespace(resolve_ref=lambda value: f"resolved:{value}")),
            graph, "write", "owner", confirmed=True)
    assert resolved == ["resolved:catalog-name"]


def test_unconfigured_join_does_not_require_row_reference_schema_dependencies(monkeypatch):
    from hub.routers import runs

    graph = Graph.model_validate({
        "id": "unconfigured-join", "version": 1,
        "nodes": [
            {"id": "join", "type": "join", "data": {"config": {}}},
        ],
        "edges": [],
    })
    monkeypatch.setattr(
        runs, "schema_for_graph",
        lambda *_args, **_kwargs: pytest.fail("unconfigured Join must not inspect schema"))

    runs._reject_row_reference_target_mismatch(
        graph, SimpleNamespace(), "join")


def test_start_run_mismatch_gate_precedes_every_allocation(monkeypatch):
    from hub.api_errors import APIError, APIErrorCode
    from hub import relationships
    from hub.routers import runs

    left_uri = "/tmp/issue788-forged-left.parquet"
    right_uri = "/tmp/issue788-forged-right.parquet"
    graph = Graph.model_validate({
        "id": "fresh-known-mismatch", "version": 1,
        "nodes": [
            {"id": "left", "type": "source", "data": {"config": {"uri": left_uri}}},
            {"id": "right", "type": "source", "data": {"config": {
                "uri": right_uri,
                "datasetRef": {
                    "kind": "exact", "datasetId": "forged-right", "revisionId": "r1",
                },
            }}},
            {"id": "join", "type": "join", "data": {"config": {
                "condition": 'a."customer id" = b."id"',
            }}},
            {"id": "write", "type": "write", "data": {"config": {}}},
        ],
        "edges": [
            {"id": "left-join", "source": "left", "target": "join", "targetHandle": "a"},
            {"id": "right-join", "source": "right", "target": "join", "targetHandle": "b"},
            {"id": "join-write", "source": "join", "target": "write"},
        ],
    })
    tables = {
        left_uri: SimpleNamespace(
            id="transient-left", registration_id="current-left", version="r1"),
        right_uri: SimpleNamespace(
            id="transient-right", registration_id="current-right", version="r1"),
    }

    class Catalog:
        @staticmethod
        def resolve_ref(uri):
            return uri

        @staticmethod
        def get_table(uri):
            return tables[uri]

        @staticmethod
        def relationships(*_args):
            return []

    allocations: list[str] = []
    monkeypatch.setattr(runs.auth, "auth_enabled", lambda: False)
    monkeypatch.setattr(runs, "_require_graph_read_access", lambda *_args: None)
    monkeypatch.setattr(runs, "_resolve_parameters", lambda graph, *_args, **_kwargs: graph)
    monkeypatch.setattr(runs, "_reject_invalid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runs.graph_mod, "resolve_source_refs", lambda *_args: None)
    monkeypatch.setattr(runs.metadb, "canvas_exists", lambda *_args: False)
    monkeypatch.setattr(runs, "schema_for_graph", lambda *_args, **_kwargs: {
        "left": [_column("customer id", {
            "kind": "exact", "datasetId": "forged-right", "revisionId": "r1"})],
        "right": [_column("id")],
    })
    monkeypatch.setattr(
        relationships, "_grain_unique_oracle",
        lambda *_args, **_kwargs: lambda _columns: None)
    monkeypatch.setattr(runs.metadb, "preallocate_run_owner", lambda *_args, **_kwargs: allocations.append("run"))
    deps = SimpleNamespace(
        catalog=Catalog(), resolve_adapter=lambda _uri: pytest.fail("mismatch must not scan"),
        registry={}, node_builders={}, node_specs={}, storage=None,
    )
    with pytest.raises(APIError) as exc:
        runs.start_run(deps, graph, "write", "local", confirmed=True)
    assert exc.value.code == APIErrorCode.ROW_REFERENCE_TARGET_MISMATCH
    assert allocations == []


def test_catalog_identity_uses_registration_and_provider_refs_cross_check_uri():
    from hub import relationships, workspace_providers

    local_uri = "/tmp/issue788-registration.parquet"
    graph = Graph.model_validate({
        "id": "identity", "version": 1,
        "nodes": [{"id": "source", "type": "source", "data": {"config": {
            "uri": local_uri,
            "datasetRef": {
                "kind": "exact", "datasetId": "forged", "revisionId": "forged-revision",
            },
        }}}],
        "edges": [],
    })
    table = SimpleNamespace(
        id="transient-table-id", registration_id="stable-registration", version="r1")
    catalog = SimpleNamespace(get_table=lambda _uri: table)
    identity = relationships._input_identity(graph, "source", catalog)
    assert identity is not None
    assert (identity.dataset_id, identity.revision_id) == ("stable-registration", "r1")
    table.registration_id = None
    assert relationships._input_identity(graph, "source", catalog) is None

    provider_uri = workspace_providers.provider_dataset_uri("mount", "a" * 32)
    expected = "workspace-provider:" + provider_uri.removeprefix("workspace-provider://")
    provider_graph = graph.model_copy(deep=True)
    provider_graph.nodes[0].data["config"] = {
        "uri": provider_uri,
        "datasetRef": {
            "kind": "exact", "datasetId": expected, "revisionId": "provider-r1",
        },
    }
    provider_identity = relationships._input_identity(
        provider_graph, "source",
        SimpleNamespace(get_table=lambda _uri: pytest.fail("provider must not use catalog")))
    assert provider_identity is not None
    assert provider_identity.dataset_id == expected
    provider_graph.nodes[0].data["config"]["datasetRef"]["datasetId"] = "forged-provider"
    assert relationships._input_identity(provider_graph, "source", catalog) is None


@pytest.mark.parametrize(
    ("registration_id", "target_id", "expected_status"),
    [
        ("stable-left", "stable-left", "compatible"),
        (None, "transient-left", None),
    ],
)
def test_join_hints_keeps_one_sided_stable_identity(
        registration_id, target_id, expected_status, monkeypatch):
    from hub import graph_ops, relationships

    monkeypatch.setattr(
        relationships, "measure_unique",
        lambda _uri, _columns, _resolve_adapter: (True, 1))

    left = SimpleNamespace(
        id="transient-left", registration_id=registration_id, version="r1",
        uri="/tmp/issue788-known-left.parquet", columns=[_column("id")])
    right_uri = "/tmp/issue788-raw-right.parquet"

    class Catalog:
        @staticmethod
        def get_table(value):
            if value == "left":
                return left
            raise KeyError(value)

        @staticmethod
        def relationships(*_args):
            return []

    right_columns = [_column("copied_left", {
        "kind": "exact", "datasetId": target_id, "revisionId": "r1"})]

    class Adapter:
        @staticmethod
        def schema(uri):
            assert uri == right_uri
            return right_columns

        @staticmethod
        def scan(*_args, **_kwargs):
            raise AssertionError("patched cardinality oracle should not scan")

    result = graph_ops.join_hints(SimpleNamespace(
        catalog=Catalog(), storage=None, resolve_adapter=lambda _uri: Adapter()),
        "left", right_uri)
    if expected_status is None:
        assert result["suggestions"] == []
        return
    suggestion = result["suggestions"][0]
    assert suggestion["leftColumns"] == ["id"]
    assert suggestion["rightColumns"] == ["copied_left"]
    assert suggestion["rowReference"][0]["status"] == expected_status


def test_configured_on_uses_the_shared_quoted_identifier_parser():
    from hub import relationships

    node = SimpleNamespace(data={"config": {
        "on": '"last, first", "quote""key"',
    }})
    assert relationships._configured_join_key(node) == (
        ["last, first", 'quote"key'],
        ["last, first", 'quote"key'],
    )


def test_durable_projection_mismatch_and_missing_remain_distinct():
    sidecar = input_identity(dataset_id="sidecar", revision_id="s1")
    base = input_identity(dataset_id="base", revision_id="b1")
    assert sidecar is not None and base is not None
    conflict = diagnose_durable_field_projections(
        sidecar=sidecar, base=base, fields=["id"], state="available",
        projections=[{"destination_field": "id", "source_dataset_id": "other", "source_version": "o1"}],
    )
    assert conflict[0].status == "conflict"
    missing = diagnose_durable_field_projections(
        sidecar=sidecar, base=base, fields=["id"], state="available", projections=[])
    assert missing[0].status == "unknown"
    assert missing[0].reason == "durable_projection_missing"


def test_durable_projection_source_field_matches_each_base_identity_field():
    sidecar = input_identity(dataset_id="sidecar", revision_id="s1")
    base = input_identity(dataset_id="base", revision_id="b1")
    assert sidecar is not None and base is not None
    diagnoses = diagnose_durable_field_projections(
        sidecar=sidecar, base=base, fields=["tenant_id", "id"], state="available",
        projections=[
            {
                "destination_field": "TENANT_ID", "source_dataset_id": "base",
                "source_version": "b1", "source_field": "tenant_ID",
            },
            {
                "destination_field": "id", "source_dataset_id": "base",
                "source_version": "b1", "source_field": "other_id",
            },
        ],
    )
    assert [diagnosis.status for diagnosis in diagnoses] == ["compatible", "conflict"]
    assert diagnoses[1].reason == "durable_projection_source_field_differs_from_identity"
    unavailable = diagnose_durable_field_projections(
        sidecar=sidecar, base=base, fields=["id"], state="available",
        projections=[{
            "destination_field": "id", "source_dataset_id": "base",
            "source_version": "b1",
        }],
    )
    assert unavailable[0].status == "unknown"
    assert unavailable[0].reason == "durable_projection_source_field_unavailable"


def test_managed_identity_requires_matching_durable_projection_before_admission(monkeypatch):
    from hub import managed_sidecar_merge as managed
    from hub.managed_sidecar_merge import ManagedSidecarMergeError
    from hub.models import ExactDatasetRef

    base = ExactDatasetRef(kind="exact", dataset_id="base", revision_id="b1")
    sidecar = ExactDatasetRef(kind="exact", dataset_id="sidecar", revision_id="s1")
    monkeypatch.setattr(managed.metadb, "catalog_field_lineage_page", lambda **_kwargs: (
        [{
            "destination_field": "id", "source_dataset_id": "base",
            "source_version": "b1", "source_field": "id",
        }],
        None, False, True))
    managed._require_durable_identity_reference(base=base, sidecar=sidecar, identity_columns=["id"])
    monkeypatch.setattr(managed.metadb, "catalog_field_lineage_page", lambda **_kwargs: (
        [{
            "destination_field": "id", "source_dataset_id": "other",
            "source_version": "o1", "source_field": "id",
        }],
        None, False, True))
    with pytest.raises(ManagedSidecarMergeError, match=ROW_REFERENCE_TARGET_MISMATCH):
        managed._require_durable_identity_reference(base=base, sidecar=sidecar, identity_columns=["id"])
    monkeypatch.setattr(managed.metadb, "catalog_field_lineage_page", lambda **_kwargs: (
        [], None, False, False))
    with pytest.raises(ManagedSidecarMergeError, match="identity_reference_unavailable"):
        managed._require_durable_identity_reference(base=base, sidecar=sidecar, identity_columns=["id"])
