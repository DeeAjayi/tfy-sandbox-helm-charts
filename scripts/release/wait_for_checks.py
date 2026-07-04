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

from _lib import run

TERMINAL_STATUSES = {"completed"}
# Every completed check-run conclusion that does NOT represent a pass.
# startup_failure (workflow failed to even start, e.g. a broken YAML) and
# stale (GitHub invalidated the run) both mean the check never succeeded —
# treating them as passing would let the merge ladder proceed on a PR whose
# gates never actually ran. Only success / neutral / skipped pass.
FAILING_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}
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


def _gh_api_all_pages(path: str) -> list[dict[str, Any]]:
    """GET a paginated endpoint, returning the parsed page objects as a list.

    `--slurp` wraps the pages in a JSON array (plain `--paginate` concatenates
    JSON documents, which json.loads can't parse for object responses).
    """
    result = run(["gh", "api", "--paginate", "--slurp", path], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api {path} failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}",
        )
    return json.loads(result.stdout) if result.stdout.strip() else []


def fetch_checks(repo: str, sha: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    # Paginated: GitHub returns at most 30 check runs per page by default —
    # a busy commit can exceed one page, and a missing page could hide an
    # in-progress or failing check, letting the merge ladder proceed early.
    for page in _gh_api_all_pages(
        f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
    ):
        for item in (page or {}).get("check_runs", []):
            checks.append(
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                },
            )

    # Legacy commit-status API — some external CI reports here instead of as
    # a check-run. Combined per-context, latest state only.
    for page in _gh_api_all_pages(
        f"repos/{repo}/commits/{sha}/status?per_page=100",
    ):
        for item in (page or {}).get("statuses", []):
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
    # How long "zero checks reported" must persist before it is believed.
    # Checks attach to the commit ASYNCHRONOUSLY after a PR is opened or
    # updated, and this script runs immediately after `gh pr create`/`edit` —
    # inside the window where GitHub can report no checks even though
    # workflows are about to queue. Returning success on the first empty
    # response would let the merge ladder (including its plain-squash
    # fallback, which branch protection does not reliably stop) merge before
    # e.g. helm_test even starts — the exact premature-merge hole this
    # script exists to close. Only after the grace window passes with
    # consistently zero checks do we conclude the PR genuinely has none
    # (e.g. no workflow's path/branch filters matched).
    parser.add_argument("--no-checks-grace-seconds", type=int, default=120)
    args = parser.parse_args()

    deadline = time.time() + args.timeout_seconds
    # Checks are attached PER COMMIT, and other workflows can push new
    # commits to the PR head while we wait (e.g. update-truefoundry-docs.yaml
    # regenerating the README on release-branch PRs). Validating a pinned SHA
    # while the head moves would pass checks on an OLD commit and let the
    # merge ladder merge a NEWER, unvalidated one — so the head SHA is
    # re-resolved on every iteration, and any movement restarts the wait
    # (including the no-checks grace window, since a fresh commit's checks
    # attach asynchronously all over again). The overall --timeout-seconds
    # deadline is NOT reset by head movement.
    sha: str | None = None
    no_checks_deadline = time.time() + args.no_checks_grace_seconds
    while True:
        current_sha = resolve_head_sha(args.repo, args.head_branch)
        if current_sha != sha:
            if sha is not None:
                print(
                    f"PR head moved {sha[:8]} -> {current_sha[:8]}; "
                    "restarting the wait on the new commit",
                )
            sha = current_sha
            no_checks_deadline = time.time() + args.no_checks_grace_seconds

        checks = fetch_checks(args.repo, sha)

        if time.time() > deadline:
            pending_names = ", ".join(
                c["name"] for c in checks if c["status"] not in TERMINAL_STATUSES
            )
            raise TimeoutError(
                f"timed out after {args.timeout_seconds}s waiting for checks "
                f"on {args.head_branch}"
                + (f": still pending: {pending_names}" if pending_names else ""),
            )

        if not checks:
            if time.time() >= no_checks_deadline:
                print(
                    f"no checks reported for {args.head_branch}@{sha[:8]} "
                    f"after {args.no_checks_grace_seconds}s; nothing to wait for",
                )
                return 0
            print(
                f"no checks reported yet for {args.head_branch}@{sha[:8]}; "
                "waiting for checks to attach...",
            )
            time.sleep(args.poll_seconds)
            continue

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
            # Final consistency guard: the head may have moved between the
            # resolve at the top of this iteration and now. Only declare
            # success if the checks we just validated belong to the commit
            # that is STILL the PR head; otherwise loop and re-validate.
            if resolve_head_sha(args.repo, args.head_branch) != sha:
                print(
                    f"PR head moved after checks passed on {sha[:8]}; "
                    "re-validating the new commit",
                )
                continue
            print(
                f"all {len(checks)} check(s) passed for "
                f"{args.head_branch}@{sha[:8]}",
            )
            return 0

        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
