#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from _lib import find_component, gh_api, run

POLL_INTERVAL_SECONDS = 15
RUN_DISCOVERY_ATTEMPTS = 20
RUN_COMPLETION_TIMEOUT_SECONDS = 3600
REGISTRY_ENV_BY_KEY = {
    "artifactory_private": "ARTIFACTORY_PRIVATE_REGISTRY",
    "artifactory_public": "ARTIFACTORY_PUBLIC_REGISTRY",
    "aws_ecr_private": "AWS_ECR_PRIVATE_REGISTRY",
}


def workflow_files(component: dict[str, Any]) -> list[str]:
    build = component.get("build", {})
    files = build.get("workflow_files")
    if not files:
        raise ValueError(
            f"build.workflow_files missing for {component['repository']}",
        )
    return [str(value) for value in files]


def resolve_branch_sha(repo: str, branch: str) -> str:
    payload = gh_api(f"repos/{repo}/git/ref/heads/{branch}")
    return payload["object"]["sha"]


def registry_prefix(registry_key: str | None) -> str | None:
    env_name = REGISTRY_ENV_BY_KEY.get((registry_key or "artifactory_private").strip())
    if not env_name:
        return None
    value = os.environ.get(env_name, "").strip()
    return value.rstrip("/") if value else None


def expected_image_refs(component: dict[str, Any], tag: str) -> list[str] | None:
    """Resolve fully-qualified image refs for this component, or None if the
    registry mapping is not configured (which disables the skip-check)."""
    build = component.get("build", {})
    default_registry = build.get("registry")
    refs: list[str] = []
    if isinstance(build.get("images"), list):
        for image in build["images"]:
            prefix = registry_prefix(image.get("registry", default_registry))
            if not prefix:
                return None
            refs.append(f"{prefix}/{image['name']}:{tag}")
        return refs
    image_name = build.get("image_artifact_name")
    if not image_name:
        return None
    prefix = registry_prefix(default_registry)
    if not prefix:
        return None
    return [f"{prefix}/{image_name}:{tag}"]


def manifest_exists(image_ref: str) -> bool:
    result = run(["docker", "manifest", "inspect", image_ref], check=False)
    return result.returncode == 0


def already_built(component: dict[str, Any], tag: str) -> bool:
    refs = expected_image_refs(component, tag)
    if not refs:
        return False
    return all(manifest_exists(ref) for ref in refs)


def unchanged_since_last_build(component: dict[str, Any], commit_sha: str) -> bool:
    """True when an image already exists for this branch HEAD commit SHA.

    build-n-publish tags every image with extra_image_tag=<github.sha>, so an
    existing :<sha> image means the branch content is unchanged since the last
    build and there is nothing new to build for this release."""
    return already_built(component, commit_sha)


def dispatch_workflow(repo: str, workflow_file: str, branch: str, image_tag: str) -> None:
    gh_api(
        f"repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        method="POST",
        fields={"ref": branch, "inputs[image_tag]": image_tag},
    )


def latest_run_id(repo: str, workflow_file: str, branch: str) -> int:
    """Highest run id currently present for this workflow+branch, or 0 if none.

    Run ids are monotonically increasing per repo, so this is a stable watermark
    to detect the run created by our subsequent dispatch."""
    path = (
        f"repos/{repo}/actions/workflows/{workflow_file}/runs"
        f"?branch={branch}&event=workflow_dispatch&per_page=20"
    )
    payload = gh_api(path)
    runs = (payload or {}).get("workflow_runs", [])
    return max((int(workflow_run["id"]) for workflow_run in runs), default=0)


def discover_run_id(repo: str, workflow_file: str, branch: str, after_run_id: int) -> int:
    """Return the first run whose id is greater than the pre-dispatch watermark.

    Deterministic relative to dispatch ordering: we never match a pre-existing run,
    and we pick the smallest new id (the earliest run created after our dispatch)."""
    path = (
        f"repos/{repo}/actions/workflows/{workflow_file}/runs"
        f"?branch={branch}&event=workflow_dispatch&per_page=20"
    )
    for _ in range(RUN_DISCOVERY_ATTEMPTS):
        payload = gh_api(path)
        runs = (payload or {}).get("workflow_runs", [])
        new_run_ids = [
            int(workflow_run["id"])
            for workflow_run in runs
            if int(workflow_run["id"]) > after_run_id
        ]
        if new_run_ids:
            return min(new_run_ids)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"could not find dispatched run for {repo}/{workflow_file} on {branch}",
    )


def wait_for_run(repo: str, run_id: int) -> dict[str, Any]:
    deadline = time.time() + RUN_COMPLETION_TIMEOUT_SECONDS
    while True:
        workflow_run = gh_api(f"repos/{repo}/actions/runs/{run_id}")
        if workflow_run.get("status") == "completed":
            return workflow_run
        if time.time() > deadline:
            raise TimeoutError(f"timed out waiting for run {run_id} in {repo}")
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch and track a repo's build-n-publish workflow",
    )
    parser.add_argument("--spec", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    spec = json.loads(args.spec)
    repo = args.repo
    # Hotfixes build each service on its OWN branch (release-v<service_target>),
    # which can differ from the chart branch when the chart line runs ahead of
    # the service. Fall back to the shared chart branch for rc/prod and
    # same-patch hotfixes.
    branch = spec.get("repo_branches", {}).get(repo, spec["branch"])
    # Hotfixes carry per-repo sequential tags (each service advances its own
    # patch, decoupled from the umbrella chart version); fall back to the shared
    # release tag for rc/prod.
    image_tag = spec.get("repo_tags", {}).get(repo, spec["tag"])

    component = find_component(repo)
    files = workflow_files(component)

    commit_sha = resolve_branch_sha(repo, branch)

    # 1. Release tag already present (idempotent re-run) -> pin it in values, but do
    #    NOT (re)publish a git tag: the run that originally built this tag already
    #    created it at the correct commit. We don't know that commit here, so emit
    #    publish=false to avoid tagging at a possibly-moved branch HEAD.
    if already_built(component, image_tag):
        print(f"tag={image_tag}")
        print(f"commit_sha={commit_sha}")
        print("ci_link=")
        print("status=already_tagged")
        print("update=true")
        print("publish=false")
        return 0

    # 2. Branch unchanged since the last build (image exists for this commit SHA) ->
    #    skip entirely. The repo keeps its existing values.yaml tag (excluded from
    #    the update/publish set), so no rebuild and no new tag is created.
    if unchanged_since_last_build(component, commit_sha):
        print("tag=")
        print(f"commit_sha={commit_sha}")
        print("ci_link=")
        print("status=unchanged")
        print("update=false")
        print("publish=false")
        return 0

    ci_links: list[str] = []
    built_sha: str | None = None

    for workflow_file in files:
        watermark = latest_run_id(repo, workflow_file, branch)
        dispatch_workflow(repo, workflow_file, branch, image_tag)
        run_id = discover_run_id(repo, workflow_file, branch, watermark)
        workflow_run = wait_for_run(repo, run_id)
        ci_links.append(workflow_run.get("html_url", ""))
        # head_sha is the exact commit the build ran against (authoritative).
        built_sha = workflow_run.get("head_sha") or built_sha
        conclusion = workflow_run.get("conclusion")
        if conclusion != "success":
            print(f"ci_link={workflow_run.get('html_url', '')}")
            raise RuntimeError(
                f"build workflow {workflow_file} for {repo} concluded '{conclusion}'",
            )

    print(f"tag={image_tag}")
    print(f"commit_sha={built_sha or commit_sha}")
    print(f"ci_link={ci_links[0] if ci_links else ''}")
    print("status=built")
    print("update=true")
    print("publish=true")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
