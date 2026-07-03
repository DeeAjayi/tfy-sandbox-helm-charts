#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


STATUS_LABEL = {
    "built": "built",
    "unchanged": "unchanged (kept existing tag)",
    "already_tagged": "skipped (already tagged)",
}

# Map the build leg's outcome onto the per-component stage values release-app's
# CallbackDto.ComponentUpdateDto accepts. Anything unknown falls back to
# 'success' when a tag was produced, else 'failed'.
COMPONENT_STATUS = {
    "built": "success",
    "unchanged": "success",
    "already_tagged": "skipped_manifest_exists",
    "failed": "failed",
}


def component_status(raw_status: str, has_tag: bool) -> str:
    if raw_status in COMPONENT_STATUS:
        return COMPONENT_STATUS[raw_status]
    return "success" if has_tag else "failed"


def write_step_summary(rows: list[dict[str, str]]) -> None:
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not step_summary or not rows:
        return
    lines = [
        "## Build phase",
        "",
        "| Repository | Tag | Outcome |",
        "| --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item["repo"]):
        outcome = STATUS_LABEL.get(row.get("status", ""), row.get("status", "") or "unknown")
        lines.append(f"| `{row['repo']}` | `{row['tag']}` | {outcome} |")
    with open(step_summary, "a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge per-repo build artifacts")
    parser.add_argument("--artifact-dir", required=True)
    # Spec (JSON) used to verify every expected repo produced a result; a missing
    # one means a build leg failed/never reported and the release is incomplete.
    parser.add_argument("--spec", default="")
    args = parser.parse_args()

    artifact_root = Path(args.artifact_dir)

    tag_map: dict[str, str] = {}
    commit_map: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    component_updates: list[dict[str, str]] = []
    seen_repos: set[str] = set()

    result_files = (
        sorted(artifact_root.glob("**/result.json")) if artifact_root.exists() else []
    )
    if not result_files:
        print("no build result artifacts found", file=sys.stderr)

    for result_file in result_files:
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        repo = payload["repo"]
        seen_repos.add(repo)
        # tag_map drives the values.yaml update (excludes unchanged repos).
        if payload.get("update", bool(payload.get("tag"))):
            tag_map[repo] = payload["tag"]
        # commit_map drives git tag / GitHub release creation (only freshly built
        # repos; already_tagged/unchanged are excluded to avoid tagging a commit
        # that may not match the shipped image).
        if payload.get("publish", False):
            commit_map[repo] = payload["commit_sha"]
        rows.append(
            {
                "repo": repo,
                "tag": payload.get("tag", "") or "(kept existing)",
                "status": payload.get("status", ""),
            },
        )
        # Per-component update for release-app: carries the CI link + commit so
        # the Step timeline and Pull-requests sections render per repo.
        tag = payload.get("tag", "") or ""
        update: dict[str, str] = {
            "repository": repo,
            "status": component_status(payload.get("status", ""), bool(tag)),
        }
        if tag:
            update["tag"] = tag
        if payload.get("commit_sha"):
            update["commit_sha"] = payload["commit_sha"]
        if payload.get("ci_link"):
            update["ci_link"] = payload["ci_link"]
        component_updates.append(update)

    write_step_summary(rows)

    # Fail if any repo from the spec did not report a result (incomplete release).
    if args.spec.strip():
        expected = set(json.loads(args.spec).get("repos", []))
        missing = sorted(expected - seen_repos)
        if missing:
            raise RuntimeError(
                f"missing build results for: {', '.join(missing)} "
                f"(expected {len(expected)}, got {len(seen_repos)})",
            )

    print(f"tag_map={json.dumps(tag_map, separators=(',', ':'))}")
    print(f"commit_map={json.dumps(commit_map, separators=(',', ':'))}")
    print(
        "component_updates="
        f"{json.dumps(component_updates, separators=(',', ':'))}",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
