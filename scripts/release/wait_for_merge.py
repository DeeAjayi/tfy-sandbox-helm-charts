#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from _lib import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for PR merge state")
    parser.add_argument("--head-branch", required=True)
    # Default to the repo this workflow runs in (works in forks/sandboxes);
    # prod value as the non-Actions fallback.
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "truefoundry/helm-charts"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    start = time.time()
    while True:
        result = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                args.repo,
                "--state",
                "all",
                "--head",
                args.head_branch,
                "--json",
                "number,state,mergedAt",
            ],
        )
        payload = json.loads(result.stdout)
        if not payload:
            raise RuntimeError(f"PR not found for head branch {args.head_branch}")
        # The head branch may have older merged PRs from prior runs; only the most
        # recent PR (highest number) is the one this run created.
        pr = max(payload, key=lambda item: item["number"])
        if pr.get("mergedAt"):
            return 0
        if pr.get("state") == "CLOSED":
            raise RuntimeError(f"PR closed without merge: {args.head_branch}")

        if (time.time() - start) > args.timeout_seconds:
            raise TimeoutError(f"timed out waiting for merge: {args.head_branch}")
        time.sleep(10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
