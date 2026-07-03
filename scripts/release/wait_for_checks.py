#!/usr/bin/env python3

"""Wait for a PR's status checks to finish and PASS before the caller
proceeds to merge it.

Every merge step in the release pipeline calls this first. Without it, the
merge ladder's fallback rungs (a plain `gh pr merge --squash`, attempted when
`--auto` can't be enabled) perform an IMMEDIATE merge attempt — which
GitHub only rejects if a required-status-check rule is actually scoped to
that branch. Several branches in this pipeline (release-v* lines, infra-charts
per-version branches) have no such rule reliably scoped to them, so a plain
merge can succeed while chart-test.yaml (or any other check) is still
running, or has already failed, without anyone noticing. This makes "checks
passed" an explicit precondition instead of an implicit, branch-protection-
dependent one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from _lib import gh_api, run

TERMINAL_STATUSES = {"completed"}
FAILING_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
FAILING_STATUS_STATES = {"failure", "error"}


def resolve_head_sha(repo: str, head_branch: str) -> str:
    result = run(
        [
            "gh", "pr", "list", "--repo", repo, "--head", head_branch,
            "--state", "open", "--json", "number,headRefOid",
        ],
    )
    prs = json.loads(result.stdout)
    if not prs:
        raise RuntimeError(f"no open PR found for head branch {head_branch!r} in {repo}")
    # A head branch could in theory have more than one PR across re-runs;
    # the highest PR number is the one from this run.
    pr = max(prs, key=lambda item: item["number"])
    return pr["headRefOid"]


def fetch_checks(repo: str, sha: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    check_runs = gh_api(f"repos/{repo}/commits/{sha}/check-runs") or {}
    for item in check_runs.get("check_runs", []):
        checks.append(
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
            },
        )

    # Legacy commit-status API — some external CI reports here instead of as
    # a check-run. Combined per-context, latest state only.
    combined = gh_api(f"repos/{repo}/commits/{sha}/status") or {}
    for item in combined.get("statuses", []):
        state = item.get("state")
        checks.append(
            {
                "name": item.get("context"),
                "status": "in_progress" if state == "pending" else "completed",
                "conclusion": state,
            },
        )

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wait for a PR's checks to finish and pass before merging",
    )
    # Default to the repo this workflow runs in (works in forks/sandboxes);
    # prod value as the non-Actions fallback.
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "truefoundry/helm-charts"),
    )
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()

    sha = resolve_head_sha(args.repo, args.head_branch)

    deadline = time.time() + args.timeout_seconds
    while True:
        checks = fetch_checks(args.repo, sha)
        if not checks:
            print(
                f"no checks reported for {args.head_branch}@{sha[:8]}; "
                "nothing to wait for",
            )
            return 0

        pending = [c for c in checks if c["status"] not in TERMINAL_STATUSES]
        if not pending:
            failing = [
                c
                for c in checks
                if c["conclusion"] in FAILING_CONCLUSIONS
                or c["conclusion"] in FAILING_STATUS_STATES
            ]
            if failing:
                names = ", ".join(f"{c['name']} ({c['conclusion']})" for c in failing)
                raise RuntimeError(
                    f"check(s) failed on {args.head_branch}@{sha[:8]}: {names}",
                )
            print(
                f"all {len(checks)} check(s) passed for "
                f"{args.head_branch}@{sha[:8]}",
            )
            return 0

        if time.time() > deadline:
            names = ", ".join(c["name"] for c in pending)
            raise TimeoutError(
                f"timed out after {args.timeout_seconds}s waiting for checks "
                f"on {args.head_branch}: still pending: {names}",
            )
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
