#!/usr/bin/env python3
"""Print complete pytest JUnit testcase duration totals by file or module."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree


def summarize(path: Path) -> str:
    root = ElementTree.parse(path).getroot()
    durations: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for testcase in root.iter("testcase"):
        label = testcase.get("file") or testcase.get("classname") or "<unknown module>"
        try:
            duration = float(testcase.get("time", "0"))
        except ValueError as exc:
            raise ValueError(f"invalid testcase duration for {label}: {testcase.get('time')!r}") from exc
        durations[label] += duration
        counts[label] += 1

    total_tests = sum(counts.values())
    total_seconds = sum(durations.values())
    lines = [
        "Complete testcase duration by file/module:",
        *(
            f"{durations[label]:8.2f}s  {counts[label]:4d} tests  {label}"
            for label in sorted(durations, key=lambda item: (-durations[item], item))
        ),
        f"Total: {total_seconds:.2f}s across {total_tests} testcases.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", type=Path)
    args = parser.parse_args()
    print(summarize(args.junit_xml))


if __name__ == "__main__":
    main()
