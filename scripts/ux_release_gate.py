#!/usr/bin/env python3
"""Fail a release gate when an open P1 UX defect remains.

The tracking issue itself is intentionally excluded: it describes the gate, not a defect in a
golden workflow. Every other open issue carrying both ``P1`` and ``ux`` is release-blocking.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TRACKING_ISSUE = 174


def blockers(issues: Iterable[dict], tracking_issue: int = TRACKING_ISSUE) -> list[dict]:
    result = []
    for issue in issues:
        labels = {label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)}
        if issue.get("number") != tracking_issue and {"P1", "ux"}.issubset(labels):
            result.append(issue)
    return sorted(result, key=lambda issue: int(issue["number"]))


def fetch_open_ux_p1_issues(repo: str, token: str | None, api_url: str) -> list[dict]:
    query = urlencode({"state": "open", "labels": "P1,ux", "per_page": 100})
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "data-playground-ux-release-gate"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{api_url.rstrip('/')}/repos/{repo}/issues?{query}", headers=headers)
    with urlopen(request, timeout=20) as response:  # noqa: S310 - repo/API are CLI-controlled GitHub endpoints
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    args = parser.parse_args()
    open_blockers = blockers(fetch_open_ux_p1_issues(args.repo, args.token, args.api_url))
    if open_blockers:
        lines = [f"#{issue['number']} {issue['title']} ({issue['html_url']})" for issue in open_blockers]
        raise SystemExit("Release blocked by open P1 UX golden-workflow defects:\n" + "\n".join(lines))
    print("No open P1 UX golden-workflow defects.")


if __name__ == "__main__":
    main()
