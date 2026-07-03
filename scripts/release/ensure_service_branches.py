#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def github_request(
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict | None]:
    url = f"https://api.github.com{path}"
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req) as response:  # noqa: S310
            text = response.read().decode("utf-8")
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8")
        if text:
            try:
                return error.code, json.loads(text)
            except json.JSONDecodeError:
                return error.code, {"message": text}
        return error.code, None


def parse_repo(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


def resolve_ref_sha(repo: str, source_ref: str, token: str) -> str:
    """Resolve a fully-qualified ref (refs/heads/main or refs/tags/v0.140.5) to
    its underlying commit sha. Dereferences annotated tags one extra hop so the
    returned sha is always a commit."""
    if not source_ref.startswith("refs/"):
        raise ValueError(
            f"source_ref must be fully-qualified (refs/heads/... or refs/tags/...); got {source_ref!r}",
        )
    owner, name = parse_repo(repo)
    encoded = urllib.parse.quote(source_ref[len("refs/"):], safe="/")
    status, payload = github_request("GET", f"/repos/{owner}/{name}/git/ref/{encoded}", token)
    if status != 200 or not payload:
        raise RuntimeError(
            f"failed reading source ref {repo}:{source_ref} (status={status})",
        )
    obj = payload["object"]
    if obj.get("type") == "tag":
        status, tag_obj = github_request(
            "GET", f"/repos/{owner}/{name}/git/tags/{obj['sha']}", token,
        )
        if status != 200 or not tag_obj:
            raise RuntimeError(
                f"failed dereferencing annotated tag {repo}:{source_ref} (status={status})",
            )
        return tag_obj["object"]["sha"]
    return obj["sha"]


def ensure_branch(repo: str, branch: str, source_ref: str, token: str) -> None:
    """Create `branch` on `repo` from `source_ref` if it doesn't already exist.

    source_ref MUST be fully qualified (refs/heads/... or refs/tags/...).
    422 from create-ref normally means 'ref already exists' — we accept that
    but surface any OTHER 422 (e.g. invalid ref name) as an error, so syntax
    bugs don't get silently swallowed."""
    owner, name = parse_repo(repo)
    branch_path = f"/repos/{owner}/{name}/git/ref/heads/{urllib.parse.quote(branch)}"
    status, _ = github_request("GET", branch_path, token)
    if status == 200:
        return
    if status != 404:
        raise RuntimeError(f"failed reading {repo}:{branch} (status={status})")

    sha = resolve_ref_sha(repo, source_ref, token)
    status, body = github_request(
        "POST",
        f"/repos/{owner}/{name}/git/refs",
        token,
        payload={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    if status in (200, 201):
        return
    if status == 422:
        message = (body or {}).get("message", "") if isinstance(body, dict) else ""
        if "already exists" in message.lower() or "reference already" in message.lower():
            return
    raise RuntimeError(
        f"failed creating branch {repo}:{branch} from {source_ref} "
        f"(status={status}, body={body})",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure release branch exists in service repos")
    parser.add_argument("--spec", required=True)
    parser.add_argument(
        "--base-branch",
        default=None,
        help="Fully-qualified source ref to cut missing branches from, applied "
             "to ALL repos as a manual override. If omitted, each repo uses its "
             "own ref from spec.branch_source_refs (hotfix), falling back to the "
             "coarse spec.branch_source_ref, then refs/heads/main.",
    )
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required")

    spec = json.loads(args.spec)
    if spec.get("mode") != "build":
        print("ensured_repos=[]")
        return 0

    # Resolution order per repo:
    #   CLI override (--base-branch, all repos)
    #   > per-repo spec.branch_source_refs[repo] (hotfix: each service's OWN
    #     current shipped tag, since service repos version independently of the
    #     chart and may not have a v<chart_base> tag at all)
    #   > coarse spec.branch_source_ref
    #   > legacy 'refs/heads/main' default.
    per_repo_refs = spec.get("branch_source_refs") or {}
    fallback_ref = spec.get("branch_source_ref") or "refs/heads/main"
    # Per-repo build branch (hotfix): each service builds on a branch named after
    # its OWN target patch (release-v<service_target>), which can differ from the
    # chart branch (spec.branch) when the chart line runs ahead of a service.
    # Falls back to the shared chart branch for rc/prod and same-patch hotfixes.
    per_repo_branches = spec.get("repo_branches") or {}
    chart_branch = spec["branch"]

    def resolve_source_ref(repo: str) -> str:
        ref = args.base_branch or per_repo_refs.get(repo) or fallback_ref
        # Tolerate the legacy plain-branch form ('main') for backwards compat.
        if not ref.startswith("refs/"):
            ref = f"refs/heads/{ref}"
        return ref

    repos = spec.get("repos", [])
    ensured: list[str] = []
    resolved: dict[str, str] = {}
    resolved_branches: dict[str, str] = {}
    for repo in repos:
        branch = per_repo_branches.get(repo, chart_branch)
        source_ref = resolve_source_ref(repo)
        ensure_branch(repo, branch, source_ref, token)
        ensured.append(repo)
        resolved[repo] = source_ref
        resolved_branches[repo] = branch

    print(f"ensured_repos={json.dumps(ensured)}")
    print(f"source_refs={json.dumps(resolved)}")
    print(f"branches={json.dumps(resolved_branches)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
