#!/usr/bin/env python3

"""deploy.py — bump the truefoundry chart version in the ubermold repos.

Handles both ubermold layouts transparently: the ArgoCD Application
`targetRevision` (ubermold-base develop template) and the newer OCI-repo
`source.version` (ubermold-truefoundry prod truefoundry.yaml).

Two targets:

  --target develop
      Edit ubermold-base (develop branch) truefoundry app template and land it
      on develop via an auto-merged PR (develop is PR-protected — direct pushes
      are rejected by branch rules). Merging to develop triggers
      publish-ubermold-develop.yaml, which renders the cookiecutter templates
      and opens a PR into ubermold-develop; ArgoCD then syncs the develop
      cluster. Used for RC releases (QA runs on develop).

  --target prod
      Edit ubermold-truefoundry's prod cluster truefoundry app and open a PR.
      The PR is NOT merged — a human review + merge is the approval gate, and
      merging triggers apply-tfy-prod.yaml against the prod cluster.

--version is the truefoundry helm chart version (same as the release tag without
a leading 'v'), e.g. 0.149.0 for prod or 0.149.0-rc.1 for an RC on develop.

Auth: GH_TOKEN (a token with write access to the ubermold repos). Stdlib-only so
the workflow step needs no extra dependencies.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Overridable for sandbox/fork testing (set as env on the workflow step).
# `or` (not .get default) so an env var set-but-empty — which is what the
# workflow passes in prod, where the vars are unset — still falls through
# to the prod default.
ORG = os.environ.get("UBERMOLD_ORG") or "truefoundry"
UBERMOLD_BASE_REPO = os.environ.get("UBERMOLD_BASE_REPO") or "ubermold-base"
UBERMOLD_PROD_REPO = os.environ.get("UBERMOLD_PROD_REPO") or "ubermold-truefoundry"

# Two on-disk shapes carry the truefoundry chart version, depending on the
# ubermold layout we're bumping. We match exactly one, never another
# component's revision/version.
#
# 1) ArgoCD Application style (ubermold-base develop template): a
#    `targetRevision:` tied to the `chart: truefoundry` source on tfy-helm.
TFY_REVISION_RE = re.compile(
    r'(?P<pre>targetRevision:\s*)(?P<ver>\S+)'
    r'(?P<post>\s*\n\s*repoURL:\s*"?tfy\.jfrog\.io/tfy-helm"?'
    r'\s*\n\s*chart:\s*truefoundry\b)',
)

# 2) OCI-repo source style (ubermold-truefoundry prod truefoundry.yaml):
#       source:
#         type: oci-repo
#         version: 0.152.0
#    The whole file is the truefoundry app, so the single oci-repo source's
#    `version` is the chart version. Handle both key orderings (type-then-version
#    and version-then-type) but only inside an oci-repo source.
TFY_OCI_VERSION_RE = re.compile(
    r'(?P<pre>type:\s*oci-repo\s*\n(?:[ \t]*[\w.-]+:.*\n)*?[ \t]*version:[ \t]*)'
    r'(?P<ver>\S+)',
)
TFY_OCI_VERSION_ALT_RE = re.compile(
    r'(?P<pre>version:[ \t]*)(?P<ver>\S+)'
    r'(?P<post>\s*\n[ \t]*type:[ \t]*oci-repo\b)',
)

# Tried in order; the first that matches wins.
TFY_VERSION_PATTERNS = (
    TFY_REVISION_RE,
    TFY_OCI_VERSION_RE,
    TFY_OCI_VERSION_ALT_RE,
)


def _log(msg: str) -> None:
    print(f"[deploy] {msg}", file=sys.stderr)


def _source_footer() -> str:
    """Traceability footer: which repo/workflow/run opened this PR.

    Reads the standard GITHUB_* env vars GitHub Actions injects into every
    step — no extra wiring needed from the calling workflow.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "truefoundry/helm-charts")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if run_id else f"{server}/{repo}/actions"
    return (
        "\n\n---\n"
        f"Source: `{repo}` workflow `.github/workflows/release-start.yml` "
        "(scripts/release/deploy.py)\n"
        f"Run: {run_url}"
    )


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN (or GITHUB_TOKEN) is required")
    return token


def run(command: list[str], cwd: str | None = None) -> None:
    proc = subprocess.run(command, text=True, capture_output=True, cwd=cwd)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout, file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(proc.returncode, command)


def try_admin_merge(repo: str, branch: str, cwd: str) -> bool:
    """Admin-merge the PR for `branch`, trying each merge method until one is
    accepted (repos differ in which of squash/merge/rebase are enabled)."""
    for method in ("--squash", "--merge", "--rebase"):
        proc = subprocess.run(
            ["gh", "pr", "merge", branch, "--repo", f"{ORG}/{repo}", method,
             "--admin"],
            text=True, capture_output=True, cwd=cwd,
        )
        if proc.returncode == 0:
            return True
    return False


def merge_pr(repo: str, branch: str, cwd: str) -> bool:
    """Merge the PR for `branch` immediately. Tries each enabled merge method
    without admin first (so required checks/approvals still apply when they can
    be satisfied), then falls back to an admin merge that bypasses branch
    protection. Returns True on the first method that's accepted."""
    for method in ("--squash", "--merge", "--rebase"):
        proc = subprocess.run(
            ["gh", "pr", "merge", branch, "--repo", f"{ORG}/{repo}", method],
            text=True, capture_output=True, cwd=cwd,
        )
        if proc.returncode == 0:
            return True
    return try_admin_merge(repo, branch, cwd)


def open_or_reuse_pr(
    repo: str, base: str, head: str, title: str, body: str, cwd: str,
) -> None:
    """Open a PR base<-head, or reuse/refresh an existing open one. Idempotent
    so a re-run after a partial failure doesn't error on 'a PR already exists'."""
    existing = subprocess.run(
        ["gh", "pr", "list", "--repo", f"{ORG}/{repo}",
         "--head", head, "--base", base, "--state", "open",
         "--json", "number", "--jq", "length"],
        text=True, capture_output=True, cwd=cwd,
    )
    open_count = (existing.stdout or "").strip()
    if open_count.isdigit() and int(open_count) >= 1:
        run(["gh", "pr", "edit", head, "--repo", f"{ORG}/{repo}",
             "--title", title, "--body", body], cwd=cwd)
        return
    run(["gh", "pr", "create", "--repo", f"{ORG}/{repo}",
         "--base", base, "--head", head, "--title", title, "--body", body],
        cwd=cwd)


def clone(repo: str, branch: str, dest: str) -> None:
    url = f"https://x-access-token:{_token()}@github.com/{ORG}/{repo}.git"
    run(["git", "clone", "--depth", "1", "--branch", branch, url, dest])
    run(["git", "config", "user.email", "ci@truefoundry.com"], cwd=dest)
    run(["git", "config", "user.name", "truefoundry-ci"], cwd=dest)


def bump_target_revision(path: Path, version: str) -> bool:
    """Set the truefoundry chart version to `version`, handling both the ArgoCD
    `targetRevision` and the OCI-repo `source.version` layouts. Returns True if
    the file changed (False if it was already at that version)."""
    text = path.read_text(encoding="utf-8")

    def _repl(m: re.Match) -> str:
        post = m.groupdict().get("post") or ""
        return f"{m.group('pre')}{version}{post}"

    for pattern in TFY_VERSION_PATTERNS:
        new, count = pattern.subn(_repl, text, count=1)
        if count == 0:
            continue
        if new == text:
            return False
        path.write_text(new, encoding="utf-8")
        return True

    raise RuntimeError(
        "could not find the truefoundry chart version (neither a "
        f"`targetRevision` nor an oci-repo `source.version`) in {path}",
    )


def find_develop_template(root: Path) -> Path:
    """Locate the truefoundry app template in ubermold-base (its filename is a
    cookiecutter conditional, so match by content rather than exact name)."""
    for candidate in root.glob("k8s/**/templates/*truefoundry.yaml*"):
        if "chart: truefoundry" in candidate.read_text(encoding="utf-8"):
            return candidate
    raise RuntimeError("ubermold-base: truefoundry app template not found")


def deploy_develop(version: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, UBERMOLD_BASE_REPO)
        clone(UBERMOLD_BASE_REPO, "develop", dest)
        template = find_develop_template(Path(dest))
        if not bump_target_revision(template, version):
            _log(f"{UBERMOLD_BASE_REPO} develop already at {version}; nothing to push.")
            return
        # develop is PR-protected — land the bump through an auto-merged PR
        # rather than a direct push (which the branch rules reject). Merging to
        # develop is what triggers publish-ubermold-develop.yaml.
        branch = f"deploy-develop-truefoundry-{version}"
        run(["git", "checkout", "-b", branch], cwd=dest)
        run(["git", "commit", "-am",
             f"[release] deploy truefoundry {version} to develop"], cwd=dest)
        run(["git", "push", "-f", "origin", branch], cwd=dest)
        open_or_reuse_pr(
            UBERMOLD_BASE_REPO, "develop", branch,
            f"[release] Deploy truefoundry {version} to develop",
            f"Bumps the develop truefoundry `targetRevision` to `{version}`.\n\n"
            "Auto-merged by the release pipeline. Merging to develop triggers "
            "`publish-ubermold-develop.yaml`, which renders the cookiecutter "
            "templates and opens a PR into ubermold-develop for the ArgoCD sync."
            + _source_footer(),
            dest,
        )
        if not merge_pr(UBERMOLD_BASE_REPO, branch, dest):
            raise RuntimeError(
                f"opened the develop deploy PR for {version} but could not "
                "merge it (check branch protection / token permissions)",
            )
        _log(
            f"Merged develop deploy for truefoundry {version} into "
            f"{UBERMOLD_BASE_REPO}.",
        )


def deploy_prod(version: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, UBERMOLD_PROD_REPO)
        clone(UBERMOLD_PROD_REPO, "main", dest)
        target = Path(dest) / (
            "clusters/production/eu-west-1/tfy-ctl/truefoundry.yaml"
        )
        if not bump_target_revision(target, version):
            _log(f"{UBERMOLD_PROD_REPO} already at {version}; no PR needed.")
            return
        branch = f"deploy-prod-truefoundry-{version}"
        run(["git", "checkout", "-b", branch], cwd=dest)
        run(["git", "commit", "-am",
             f"[release] deploy truefoundry {version} to production"], cwd=dest)
        run(["git", "push", "-f", "origin", branch], cwd=dest)
        open_or_reuse_pr(
            UBERMOLD_PROD_REPO, "main", branch,
            f"[release] Deploy truefoundry {version} to production",
            f"Bumps the prod truefoundry `targetRevision` to `{version}`.\n\n"
            "**Review and merge manually** — this is the production gate. "
            "Merging triggers `apply-tfy-prod.yaml` against the prod cluster."
            + _source_footer(),
            dest,
        )
        # NO auto-merge for prod: merging is the deliberate human approval gate.
        # The PR is left open for a release admin to review and merge manually,
        # which is what triggers apply-tfy-prod.yaml against the prod cluster.
        _log(
            f"Opened prod deploy PR for truefoundry {version} — leaving it open "
            "for manual review and merge.",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump ubermold truefoundry version")
    parser.add_argument("--target", required=True, choices=["develop", "prod"])
    parser.add_argument("--version", required=True, help="chart version, e.g. 0.149.0")
    args = parser.parse_args()
    version = args.version.strip().lstrip("v")  # chart version carries no 'v'
    if not version:
        raise RuntimeError("--version is empty")
    if args.target == "develop":
        deploy_develop(version)
    else:
        deploy_prod(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
