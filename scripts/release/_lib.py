#!/usr/bin/env python3

"""Shared helpers for the release orchestration scripts.

Scripts are invoked as `python3 scripts/release/<name>.py`, which puts this
directory on sys.path[0], so a plain `import _lib` works from each script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


COMPONENTS_PATH = Path("config/components.yml")


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, surfacing captured stdout/stderr on failure."""
    proc = subprocess.run(command, text=True, capture_output=True)
    if check and proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout, file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(
            proc.returncode, command, proc.stdout, proc.stderr,
        )
    return proc


def gh_api(
    path: str,
    method: str | None = None,
    fields: dict[str, str] | None = None,
) -> Any:
    """Call the GitHub API via the gh CLI and parse the JSON response."""
    command = ["gh", "api", path]
    if method:
        command += ["--method", method]
    for key, value in (fields or {}).items():
        command += ["-f", f"{key}={value}"]
    result = run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api {method or 'GET'} {path} failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}",
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def load_components(components_path: Path | str = COMPONENTS_PATH) -> list[dict[str, Any]]:
    payload = yaml.safe_load(Path(components_path).read_text(encoding="utf-8")) or {}
    return payload.get("repositories", [])


def find_component(
    repo: str,
    components_path: Path | str = COMPONENTS_PATH,
) -> dict[str, Any]:
    for component in load_components(components_path):
        if component["repository"] == repo:
            return component
    raise ValueError(f"missing component config for repo: {repo}")
