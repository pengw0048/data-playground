#!/usr/bin/env python3
"""Select and verify the CI kernel pytest shard partition.

The workflow keeps the short kernel and lifecycle policies explicit while the
final shard remains the complement.  This helper makes that policy executable:
every invocation proves the current test tree is selected exactly once before
pytest receives a shard's file list.
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path


_SHARD_ENV = {
    "kernel": "KERNEL_FILES",
    "lifecycle": "LIFECYCLE_FILES",
    "remainder-0": "GROUP_0_FILES",
    "remainder-1": "GROUP_1_FILES",
}
_COMPLEMENT_SHARD = "remainder-2"


def _test_names(test_root: Path) -> list[str]:
    paths = sorted(test_root.rglob("test_*.py"))
    nested = [path.relative_to(test_root).as_posix() for path in paths if path.parent != test_root]
    if nested:
        raise ValueError(
            "kernel shard selection uses basenames and cannot address nested tests: "
            + ", ".join(nested)
        )

    names = [path.name for path in paths]
    duplicates = sorted(name for name, count in Counter(names).items() if count != 1)
    if duplicates:
        raise ValueError("kernel test basenames are not unique: " + ", ".join(duplicates))
    return names


def select_shards(test_root: Path, environ: dict[str, str]) -> dict[str, list[str]]:
    """Return the complete, validated five-shard test-file partition."""
    all_names = _test_names(test_root)
    all_set = set(all_names)
    selected: dict[str, list[str]] = {}
    explicit: list[str] = []
    for shard, variable in _SHARD_ENV.items():
        names = environ.get(variable, "").split()
        if not names:
            raise ValueError(f"{variable} must select at least one test")
        selected[shard] = names
        explicit.extend(names)

    missing = sorted(set(explicit) - all_set)
    if missing:
        raise ValueError("explicit kernel shards name missing tests: " + ", ".join(missing))

    duplicate_explicit = sorted(name for name, count in Counter(explicit).items() if count != 1)
    if duplicate_explicit:
        raise ValueError("explicit kernel shards overlap: " + ", ".join(duplicate_explicit))

    selected[_COMPLEMENT_SHARD] = sorted(all_set - set(explicit))
    if not selected[_COMPLEMENT_SHARD]:
        raise ValueError("remainder-2 must remain a non-empty catch-all")

    counts = Counter(name for names in selected.values() for name in names)
    missing_all = sorted(all_set - set(counts))
    duplicate_all = sorted(name for name, count in counts.items() if count != 1)
    unexpected = sorted(set(counts) - all_set)
    if missing_all or duplicate_all or unexpected:
        raise ValueError(
            "kernel shard partition must select every test exactly once; "
            f"missing={missing_all}, duplicates={duplicate_all}, unexpected={unexpected}"
        )
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-root", type=Path, default=Path("hub/tests"))
    parser.add_argument("--shard", choices=[*_SHARD_ENV, _COMPLEMENT_SHARD], required=True)
    args = parser.parse_args()

    for name in select_shards(args.test_root, dict(os.environ))[args.shard]:
        print(name)


if __name__ == "__main__":
    main()
