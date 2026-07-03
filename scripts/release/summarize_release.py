#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def parse_json_arg(raw: str, default: Any) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def build_record(args: argparse.Namespace) -> dict[str, Any]:
    spec = parse_json_arg(args.spec, {})
    tag_map = parse_json_arg(args.tag_map, {})
    commit_map = parse_json_arg(args.commit_map, {})
    # MIN6: overall is "success" ONLY when both phases succeeded (or publish
    # was legitimately skipped, e.g. merge_pr=false). The previous OR
    # operator meant a helm-merged-but-publish-failed run reported success
    # in Slack, masking real failures.
    helm_ok = args.helm_result == "success"
    publish_ok = args.publish_result in ("success", "skipped")
    overall = "success" if helm_ok and publish_ok else "failed"
    return {
        "kind": spec.get("kind"),
        "target_version": spec.get("target_version"),
        "tag": spec.get("tag"),
        "branch": spec.get("branch"),
        "mode": spec.get("mode"),
        "repos": spec.get("repos", []),
        "tag_map": tag_map,
        "commit_map": commit_map,
        "results": {
            "helm": args.helm_result,
            "publish": args.publish_result,
        },
        "overall": overall,
        "run_url": args.run_url,
    }


def render_markdown(record: dict[str, Any]) -> str:
    lines = [
        f"# Release {record.get('tag') or '(unknown)'}",
        "",
        f"- Kind: `{record.get('kind')}`",
        f"- Mode: `{record.get('mode')}`",
        f"- Branch: `{record.get('branch')}`",
        f"- Helm: `{record['results']['helm']}` | Publish: `{record['results']['publish']}`",
        f"- Run: {record.get('run_url')}",
        "",
        "## Component image tags",
    ]
    tag_map = record.get("tag_map", {})
    if tag_map:
        for repo in sorted(tag_map):
            lines.append(f"- `{repo}`: `{tag_map[repo]}`")
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def post_slack(webhook_url: str, record: dict[str, Any]) -> None:
    text = (
        f"Release {record.get('tag')} ({record.get('kind')}) "
        f"on {record.get('branch')}: helm={record['results']['helm']}, "
        f"publish={record['results']['publish']}\n{record.get('run_url')}"
    )
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10)  # noqa: S310
    except urllib.error.URLError as error:
        print(f"warning: slack notification failed: {error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a release run")
    parser.add_argument("--spec", default="")
    parser.add_argument("--tag-map", default="")
    parser.add_argument("--commit-map", default="")
    parser.add_argument("--helm-result", default="")
    parser.add_argument("--publish-result", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--output", default="release-record.json")
    args = parser.parse_args()

    record = build_record(args)
    Path(args.output).write_text(json.dumps(record, indent=2), encoding="utf-8")

    markdown = render_markdown(record)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as summary_file:
            summary_file.write(markdown)
    else:
        print(markdown)

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook_url:
        post_slack(webhook_url, record)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
