#!/usr/bin/env python3

"""Wait for independently-published subcharts (e.g. tfy-llm-gateway) to be
available in the OCI registry at EACH SUBCHART'S OWN version (derived from its
own tag in tag_map, not the umbrella's chart_version — the two lines are
independent and can diverge), before the umbrella chart's `helm dependency
update` tries to resolve them."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time

from _lib import load_components, run


def chart_available(oci_registry: str, chart: str, version: str, dest: str) -> bool:
    result = run(
        [
            "helm", "pull", f"oci://{oci_registry}/{chart}",
            "--version", version, "--destination", dest,
        ],
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for published subcharts in OCI")
    parser.add_argument("--tag-map", required=True)
    parser.add_argument("--components-path", required=True)
    parser.add_argument("--oci-registry", default="")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()

    tag_map = json.loads(args.tag_map)
    components = {entry["repository"]: entry for entry in load_components(args.components_path)}

    # A published subchart's OWN chart version is derived from its OWN tag
    # (see update_values.py's subchart_version = tag.lstrip("v")), not the
    # umbrella's chart_version — the two lines are independent and can
    # diverge (e.g. gateway v0.154.1 while the umbrella ships 0.154.2).
    # Checking availability at the umbrella's version would poll for a
    # package that was never published whenever the lines disagree.
    pending: dict[str, str] = {}
    for repo, tag in tag_map.items():
        subchart = (components.get(repo) or {}).get("subchart") or {}
        if subchart.get("published"):
            pending[subchart["name"]] = tag.lstrip("v")

    if not pending:
        print("no independently-published subcharts in this release")
        return 0

    if not args.oci_registry.strip():
        print(
            f"warning: OCI registry not configured; cannot verify {pending} "
            "are published. Skipping availability check.",
            file=sys.stderr,
        )
        return 0

    dest = tempfile.mkdtemp()
    deadline = time.time() + args.timeout_seconds
    while pending:
        for chart, version in list(pending.items()):
            if chart_available(args.oci_registry, chart, version, dest):
                print(f"available: {chart} {version}")
                pending.pop(chart, None)
        if not pending:
            break
        if time.time() > deadline:
            raise TimeoutError(
                f"charts not available after {args.timeout_seconds}s: {pending}",
            )
        time.sleep(args.poll_seconds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
