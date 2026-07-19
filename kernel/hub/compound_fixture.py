"""Private authority and fixed adapter for the one public compound fixture."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from hub import db, metadb
from hub.compound_datasets import MAX_MANIFEST_BYTES, RevisionManifest, open_compound_manifest
from hub.compound_fixture_definition import _compound_manifest_revision, build_compound_timeline
from hub.models import DatasetViewCreateRequest, ExactDatasetRef, TemporalWindowV1
from hub.plugins.adapters import RevisionUnavailable, relation_columns

if TYPE_CHECKING:
    from hub.models import DatasetViewDefinitionV1


FIXTURE_URI_PREFIX = "fixture://compound-timeline/"
_TARGET = "compound-fixture-v1"
_SOURCE_DATASET_ID = "fixture-compound-timeline"
_SOURCE_REVISION_ID = "24fe23dddf87e41016499e356bed5d3c8f00eee7cf0e9edbca86bd352ce0edb0"
_MEMBERS = ("episodes", "sensor-observations", "interval-annotations", "video-observations")
_MEMBER_DIGESTS = {
    "episodes": "a651b2ad709d2317a018303341f16dd8a0a47208e3b776774db7b18211d391ee",
    "sensor-observations": "fc2fff3a709949c8e7375cbfd301e47dbc0c862cfd446b09eb07620bbd8540a6",
    "interval-annotations": "b6c71de7d85ab28fd796bf579421144ff2bd90c4ad6ee8706ec1b01e319942d4",
    "video-observations": "4e703e9b5c582682bc0ded5a0707c3b4b111eb82827a653441362e4ce27f97ee",
}
_FLOWER_BYTES = 554_058
_FLOWER_SHA256 = "c6f8a348953395598a9a73b9bab1676436410797bce9f398f4be1531d6e76dda"
_VIEW_SPECS = {
    "numeric-sensor": ("sensor-observations", "device_tick", "sensor-device-us", "0", "27100000", ["value"]),
    "interval-annotation": ("interval-annotations", "start_tick", "reference-ms", "0", "27001", ["end_tick", "fixture_phase"]),
    "video": ("video-observations", "start_tick", "reference-ms", "0", "27001", ["end_tick", "asset_id"]),
}


class FixtureUnavailable(RuntimeError):
    """The fixture's declared immutable bytes or exact registrations are unavailable."""


def fixture_uri(member_id: str) -> str:
    if member_id not in _MEMBERS:
        raise FixtureUnavailable("fixture member is unavailable")
    return FIXTURE_URI_PREFIX + member_id


def _resource(member_id: str):
    resource = files("hub").joinpath("_fixtures", "compound", f"{member_id}.csv")
    # Editable installs expose the source package directly; wheels carry the same files above.
    return resource if resource.is_file() else Path(__file__).parents[2] / "fixtures" / "compound" / f"{member_id}.csv"


def _canonical_member_bytes(member_id: str) -> bytes:
    try:
        payload = _resource(member_id).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise FixtureUnavailable("canonical fixture member is unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != _MEMBER_DIGESTS[member_id]:
        raise FixtureUnavailable("canonical fixture member is corrupt")
    return payload


class CompoundFixtureAdapter:
    """The only resolver allowed to open fixed opaque compound member URIs."""

    name = "compound-fixture"
    retention_owner = "provider"
    revision_selectors = frozenset({"exact", "latest"})

    def matches(self, uri: str) -> bool:
        return isinstance(uri, str) and uri.startswith(FIXTURE_URI_PREFIX) and uri.removeprefix(FIXTURE_URI_PREFIX) in _MEMBERS

    def _member(self, uri: str) -> tuple[str, bytes]:
        if not self.matches(uri):
            raise RevisionUnavailable("revision_unavailable")
        member_id = uri.removeprefix(FIXTURE_URI_PREFIX)
        return member_id, _canonical_member_bytes(member_id)

    def _table(self, uri: str):
        import pyarrow.csv as pacsv

        import io
        _member_id, payload = self._member(uri)
        return pacsv.read_csv(io.BytesIO(payload))

    def schema(self, uri: str):
        with db.base_guard():
            return relation_columns(db.conn().from_arrow(self._table(uri)))

    def count(self, uri: str) -> int:
        return self._table(uri).num_rows

    def fingerprint(self, uri: str) -> str:
        member_id, _payload = self._member(uri)
        return "compound-fixture:" + _MEMBER_DIGESTS[member_id]

    def revision_history(self, uri: str, *, limit: int, cursor: str | None = None):
        member_id, _payload = self._member(uri)
        if cursor is not None or int(limit) < 1:
            return [], None
        return [{"revision_id": _MEMBER_DIGESTS[member_id], "committed_at": None}], None

    def resolve_revision(self, uri: str, *, as_of=None) -> dict:
        if as_of is not None:
            raise RevisionUnavailable("revision_unavailable")
        member_id, _payload = self._member(uri)
        return {"revision_id": _MEMBER_DIGESTS[member_id], "committed_at": None}

    def _exact(self, uri: str, revision_id: str):
        member_id, _payload = self._member(uri)
        if revision_id != _MEMBER_DIGESTS[member_id]:
            raise RevisionUnavailable("revision_unavailable")
        return self._table(uri)

    def open_revision(self, uri: str, revision_id: str):
        return db.conn().from_arrow(self._exact(uri, revision_id))

    def preview_revision(self, uri: str, revision_id: str, *, limit: int):
        return db.conn().from_arrow(self._exact(uri, revision_id).slice(0, max(0, int(limit))))

    def revision_detail(self, uri: str, revision_id: str, *, preview_limit: int) -> dict:
        table = self._exact(uri, revision_id)
        relation = db.conn().from_arrow(table)
        return {
            "revision_id": revision_id, "committed_at": None, "parent_revision_id": None,
            "producer_operation": "fixture", "columns": relation_columns(relation),
            "row_count": table.num_rows, "data_file_count": 1, "total_bytes": table.nbytes,
            "fragment_count": 1, "preview_table": table.slice(0, max(0, int(preview_limit))),
        }


@dataclass(frozen=True)
class FixtureAuthority:
    manifest: RevisionManifest
    views: dict[str, "DatasetViewDefinitionV1"]
    root: Path


def materialize_fixture(data_dir: str) -> Path | None:
    """Bootstrap the private asset target once; an existing target is never replaced."""
    # The configured data directory is operator-trusted. Canonicalize platform aliases such as
    # macOS /var -> /private/var, then reject symlinks only inside this managed fixture boundary.
    root = Path(data_dir).resolve(strict=False)
    target = root / _TARGET
    lock = root / f".{_TARGET}.lock"
    manifest = target / "manifest.json"

    def has_managed_symlink(path: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return True
        current, managed = root, [root]
        for part in relative.parts:
            current = current / part
            managed.append(current)
        return any(item.is_symlink() for item in managed)

    def valid_target() -> bool:
        return (not has_managed_symlink(target) and not has_managed_symlink(manifest)
                and target.is_dir() and manifest.is_file())

    try:
        if any(has_managed_symlink(path) for path in (root, target, lock, manifest)):
            return None
        root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return target if valid_target() else None
        from filelock import FileLock
        with FileLock(str(lock)):
            if target.exists():
                return target if valid_target() else None
            staging = Path(tempfile.mkdtemp(prefix=f".{_TARGET}-", dir=root))
            try:
                build_compound_timeline(staging)
                os.replace(staging / "compound", target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return target
    except (OSError, RuntimeError, ValueError):
        return None


def _registrations() -> dict[str, tuple[str, str]]:
    from hub.deps import get_deps

    deps, result = get_deps(), {}
    catalog = deps._reference_catalog
    for member_id in _MEMBERS:
        uri = fixture_uri(member_id)
        _canonical_member_bytes(member_id)
        binding = metadb.catalog_revision_binding_for_uri(uri)
        if binding is None:
            catalog.register_output(name=f"fixture_compound_{member_id.replace('-', '_')}", uri=uri, parents=[], pipeline="compound-fixture")
            binding = metadb.catalog_revision_binding_for_uri(uri)
        revision = deps.resolve_adapter(uri).resolve_revision(uri)
        if (binding is None or binding["uri"] != uri
                or revision.get("revision_id") != _MEMBER_DIGESTS[member_id]):
            raise FixtureUnavailable("fixture exact member registration is unavailable")
        result[member_id] = (str(binding["dataset_id"]), _MEMBER_DIGESTS[member_id])
    return result


def _manifest(root: Path, members: dict[str, tuple[str, str]]) -> RevisionManifest:
    try:
        with (root / "manifest.json").open("rb") as source:
            payload = source.read(MAX_MANIFEST_BYTES + 1)
        source_manifest = open_compound_manifest(payload)
        if (source_manifest.ref.dataset_id, source_manifest.digest) != (
                _SOURCE_DATASET_ID, _SOURCE_REVISION_ID):
            raise FixtureUnavailable("fixture source manifest is unavailable")
        document = json.loads(payload)
        for member in document["members"]:
            member["datasetId"], member["revisionId"] = members[member["id"]]
        document["revisionId"] = _compound_manifest_revision(document)
        return open_compound_manifest(json.dumps(document, separators=(",", ":")).encode())
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise FixtureUnavailable("fixture manifest is unavailable") from exc


def _views(manifest: RevisionManifest, owner_id: str) -> dict[str, "DatasetViewDefinitionV1"]:
    from fastapi import Response
    from hub.routers.dataset_views import create_dataset_view

    members, result = {item.id: item for item in manifest.members}, {}
    for stream, (member_id, field, domain, start, end, values) in _VIEW_SPECS.items():
        member = members[member_id]
        request = DatasetViewCreateRequest(
            submissionId=f"compound-fixture-v1:{stream}:{manifest.digest[:32]}", name=f"Compound fixture {stream}",
            datasetRef=ExactDatasetRef(kind="exact", datasetId=member.dataset_id, revisionId=member.revision_id),
            selectedColumns=[field, "observation_id", "episode_id", *values],
            temporalWindow=TemporalWindowV1(timeField=field, timeDomain=domain, startTick=start, endTick=end),
        )
        result[stream] = create_dataset_view(request, Response(), owner_id)
    return result


def fixture_authority(*, owner_id: str | None = None) -> FixtureAuthority:
    from hub.deps import get_deps

    root = materialize_fixture(get_deps().data_dir)
    if root is None:
        raise FixtureUnavailable("fixture bootstrap is unavailable")
    manifest = _manifest(root, _registrations())
    return FixtureAuthority(manifest, _views(manifest, owner_id) if owner_id else {}, root)


def current_user_fixture_reference(owner_id: str) -> FixtureAuthority:
    """Internal #442 seam: exact manifest plus this user's saved #441 member views."""
    return fixture_authority(owner_id=owner_id)


def fixture_asset_available(authority: FixtureAuthority) -> bool:
    path = authority.root / "flower.webm"
    try:
        return (path.is_file() and not path.is_symlink() and path.stat().st_size == _FLOWER_BYTES
                and hashlib.sha256(path.read_bytes()).hexdigest() == _FLOWER_SHA256)
    except OSError:
        return False


def episode_reference_windows() -> dict[str, tuple[str, str]]:
    """Public bounds come from the immutable packaged episode member, never a local path."""
    import io
    import pyarrow.csv as pacsv

    rows = pacsv.read_csv(io.BytesIO(_canonical_member_bytes("episodes"))).to_pylist()
    return {str(row["episode_id"]): (str(row["reference_start_tick"]), str(row["reference_end_tick"]))
            for row in rows}


def fixture_asset_path(authority: FixtureAuthority, *, episode_id: str, stream_id: str, asset_id: str) -> Path:
    binding = next((item for item in authority.manifest.bindings if (item.episode_id, item.stream_id) == (episode_id, stream_id)), None)
    if binding is None or binding.state != "present" or asset_id not in binding.asset_ids:
        raise FixtureUnavailable("fixture asset is not declared")
    return authority.root / "flower.webm"
