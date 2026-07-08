"""Storage tiers for region-boundary materialization + a backend reachability model (Phase C).

A region's output parquet lands on a TIER; a backend can reach a subset of tiers. A boundary is
materialized to the cheapest tier that BOTH its producer and its consumers can reach: local disk for a
local→local handoff, a shared object store (s3/gs) when a remote backend is on either side — so "not
every handoff writes S3", only the ones that must. 'local' always exists; 'object' exists when
DP_STORAGE_URL points at an object store.

A backend may declare its reach via reachable_tiers(); by default the in-process/default backend
reaches local+object (it reads/writes both — object via httpfs), and any named (assumed-remote)
backend reaches object only (it can't see the hub's local disk).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from hub.plugins.adapters import is_object_uri

LOCAL_REACH = ("local", "object")   # the local/default backend: local disk AND object store (httpfs)
REMOTE_REACH = ("object",)          # an assumed-remote backend: shared object storage only


@dataclass(frozen=True)
class Tier:
    name: str
    prefix: str    # a local dir path, or an object-store uri prefix (s3://…/regions)
    rank: int      # lower = cheaper / more local → preferred within the reachable intersection

    def uri(self, fname: str) -> str:
        if is_object_uri(self.prefix):
            return self.prefix.rstrip("/") + "/" + fname
        return os.path.join(self.prefix, fname)

    @property
    def is_object(self) -> bool:
        return is_object_uri(self.prefix)


def tiers(workspace: str) -> dict[str, Tier]:
    """The available tiers: local (always) + object (when DP_STORAGE_URL is an object store)."""
    ts = {"local": Tier("local", os.path.join(workspace, "regions"), 0)}
    url = (os.environ.get("DP_STORAGE_URL") or "").strip()
    if is_object_uri(url):
        ts["object"] = Tier("object", url.rstrip("/") + "/regions", 10)
    return ts


def backend_reach(backend, is_default: bool) -> tuple:
    """Which tier NAMES a backend can read/write — its own reachable_tiers() if declared, else the
    default assumption (default backend → local+object; a named/remote backend → object only)."""
    fn = getattr(backend, "reachable_tiers", None)
    if callable(fn):
        try:
            r = tuple(fn())
            if r:
                return r
        except Exception:  # noqa: BLE001 — a misbehaving backend falls back to the default assumption
            pass
    return LOCAL_REACH if is_default else REMOTE_REACH


def pick_tier(tiers_map: dict, reach_sets: list) -> "Tier | None":
    """The cheapest tier every party (producer + consumers) can reach, or None if their reaches don't
    intersect (a misconfiguration — e.g. a remote backend with no object store configured)."""
    common = set(tiers_map)
    for rs in reach_sets:
        common &= set(rs)
    if not common:
        return None
    return min((tiers_map[n] for n in common), key=lambda t: t.rank)
