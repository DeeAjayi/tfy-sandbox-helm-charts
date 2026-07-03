#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml

from _lib import load_components, run

VALUES_PATH = "charts/truefoundry/values.yaml"

# Maps a component's registry_key to the (username_env, password_env) pair that
# holds WRITE/deploy-capable credentials for that registry. Public and private
# images live on the SAME host (tfy.jfrog.io), so a single `docker login` can
# only hold one identity at a time — we must authenticate with the matching
# credentials before pushing each registry's manifests, otherwise Artifactory
# returns 403 "No permission to write manifest" (e.g. retagging the public
# tfy-workflow-agent image while logged in with private-only creds).
REGISTRY_CREDENTIALS: dict[str, tuple[str, str]] = {
    "artifactory_private": (
        "ARTIFACTORY_PRIVATE_USERNAME",
        "ARTIFACTORY_PRIVATE_PASSWORD",
    ),
    "artifactory_public": (
        "ARTIFACTORY_PUBLIC_USERNAME",
        "ARTIFACTORY_PUBLIC_PASSWORD",
    ),
}


def registry_prefix(registry_key: str | None) -> str:
    key = (registry_key or "artifactory_private").strip()
    mapping = {
        "artifactory_private": os.environ.get(
            "ARTIFACTORY_PRIVATE_REGISTRY",
            "tfy.jfrog.io/tfy-private-images",
        ),
        "artifactory_public": os.environ.get(
            "ARTIFACTORY_PUBLIC_REGISTRY",
            "tfy.jfrog.io/tfy-public-images",
        ),
        "aws_ecr_private": os.environ.get("AWS_ECR_PRIVATE_REGISTRY", ""),
    }
    if key not in mapping or not mapping[key]:
        raise ValueError(f"unknown/empty registry mapping for key: {key}")
    return mapping[key].rstrip("/")


def registry_host(registry: str) -> str:
    """The login host for a registry prefix (tfy.jfrog.io/tfy-images -> tfy.jfrog.io)."""
    return registry.split("/", 1)[0]


def is_rc_source_tag(tag: str) -> bool:
    """True for an in-cycle RC tag (e.g. v0.149.0-rc.2).

    On prod promotion only the components built during this release line carry
    an `-rc.N` image tag on the release branch. Components that did not change
    still point at their previously-shipped FINAL tag (e.g. v0.140.0); those
    must not be retagged — there is no new manifest to write and the chart
    should keep referencing their existing final tag.
    """
    return "-rc." in tag


def registry_login(registry_key: str, registry: str) -> None:
    """Authenticate to a registry with the credentials matching its key.

    Pushing a retagged manifest requires WRITE access, so each registry must be
    entered with its own deploy-capable credentials. Registries we don't manage
    credentials for here (e.g. aws_ecr_private) are assumed pre-authenticated by
    the workflow.
    """
    creds = REGISTRY_CREDENTIALS.get((registry_key or "artifactory_private").strip())
    if not creds:
        return
    user_env, pass_env = creds
    username = os.environ.get(user_env)
    password = os.environ.get(pass_env)
    if not username or not password:
        raise RuntimeError(
            f"missing {user_env}/{pass_env}; cannot authenticate to push "
            f"retagged manifests for registry_key={registry_key}",
        )
    host = registry_host(registry)
    proc = subprocess.run(  # noqa: S603
        ["docker", "login", host, "-u", username, "--password-stdin"],
        input=password,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"docker login failed for {host} ({registry_key}): {detail}")


def git_show(ref: str, path: str) -> str:
    if ref.startswith("origin/"):
        branch = ref[len("origin/") :]
        fetch = run(["git", "fetch", "--depth", "1", "origin", branch], check=False)
        if fetch.returncode != 0:
            raise RuntimeError(
                f"failed to fetch {ref}: {fetch.stderr.strip() or 'unknown error'}",
            )
        return run(["git", "show", f"FETCH_HEAD:{path}"]).stdout
    return run(["git", "show", f"{ref}:{path}"]).stdout


def yaml_get(node: dict[str, Any], dotted_path: str) -> Any:
    current: Any = node
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(dotted_path)
        current = current[segment]
    return current


def _image_tag_locations(component: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (values_file, image-tag dotted path) pairs for a component.

    - subchart component: its own values_file at <image_path>.tag
    - helm_subpath component: the umbrella values at <subpath>.image.tag
    """
    subchart = component.get("subchart")
    if subchart:
        image_path = subchart.get("image_path", "image")
        return [(subchart["values_file"], f"{image_path}.tag")]
    subpaths = component.get("helm_subpath")
    if isinstance(subpaths, str):
        subpath_list = [subpaths]
    elif isinstance(subpaths, list):
        subpath_list = [str(value) for value in subpaths]
    else:
        raise ValueError(f"component {component['repository']} has no subchart/helm_subpath")
    return [(VALUES_PATH, f"{subpath}.image.tag") for subpath in subpath_list]


def read_source_tags(branch: str, repos: list[str]) -> dict[str, str]:
    values_cache: dict[str, Any] = {}

    def load_values(values_file: str) -> dict[str, Any]:
        if values_file not in values_cache:
            values_cache[values_file] = yaml.safe_load(
                git_show(f"origin/{branch}", values_file),
            ) or {}
        return values_cache[values_file]

    source_tags: dict[str, str] = {}
    components = {entry["repository"]: entry for entry in load_components()}

    for repo in repos:
        component = components.get(repo)
        if not component:
            raise ValueError(f"component config missing for {repo}")

        observed_tags: set[str] = set()
        for values_file, tag_path in _image_tag_locations(component):
            image_tag = yaml_get(load_values(values_file), tag_path)
            if not isinstance(image_tag, str) or not image_tag.strip():
                raise ValueError(f"invalid image tag at {tag_path} ({values_file}) for {repo}")
            observed_tags.add(image_tag.strip())
        if len(observed_tags) != 1:
            raise ValueError(f"mismatched source tags for {repo}: {sorted(observed_tags)}")
        source_tags[repo] = observed_tags.pop()
    return source_tags


def _tag_suffixes(node: dict[str, Any]) -> list[str]:
    """Normalize an optional `tag_suffixes` field into a list of strings."""
    raw = node.get("tag_suffixes")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(value) for value in raw]
    raise ValueError(f"tag_suffixes must be a string or list, got {type(raw).__name__}")


def image_names(component: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
    """Return (image_name, registry_key, tag_suffixes) per image.

    `tag_suffixes` lists extra tags the build publishes as `<tag><suffix>` in
    addition to the plain `<tag>` (e.g. `-optimized` for deltafusion-ingestor /
    -query-server). The chart references those suffixed tags, so each must be
    retagged alongside the base tag — otherwise the promoted final tag exists
    but `<final_tag><suffix>` does not. Suffixes can be set per-image inside the
    `images:` list or build-wide via `build.tag_suffixes`.
    """
    build = component.get("build", {})
    default_registry = build.get("registry")
    build_suffixes = _tag_suffixes(build)
    if isinstance(build.get("images"), list):
        result: list[tuple[str, str, list[str]]] = []
        for image in build["images"]:
            # An explicit per-image `tag_suffixes` (even an empty list) overrides
            # the build-wide default; absence inherits it.
            suffixes = _tag_suffixes(image) if "tag_suffixes" in image else build_suffixes
            result.append(
                (image["name"], image.get("registry", default_registry), suffixes),
            )
        return result
    image_name = build.get("image_artifact_name")
    if not image_name:
        raise ValueError(f"missing image_artifact_name for {component['repository']}")
    return [(image_name, default_registry, build_suffixes)]


def copy_tag(image: str, source_tag: str, target_tag: str) -> None:
    source_ref = f"{image}:{source_tag}"
    target_ref = f"{image}:{target_tag}"
    exists = run(["docker", "manifest", "inspect", source_ref], check=False)
    if exists.returncode != 0:
        raise RuntimeError(f"source tag missing: {source_ref}")

    # `imagetools create` copies the source manifest to the target tag registry-
    # side, preserving the FULL (possibly multi-arch) manifest list. The old
    # pull→tag→push only handled the runner's single platform and would silently
    # drop non-amd64 variants for any multi-arch image.
    run(
        ["docker", "buildx", "imagetools", "create", "--tag", target_ref, source_ref],
    )


def github_request(path: str, token: str) -> dict[str, Any] | None:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req) as response:  # noqa: S310
            text = response.read().decode("utf-8")
            return json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        message = error.read().decode("utf-8")
        raise RuntimeError(f"github api failed ({error.code}): {message}") from error


def resolve_tag_sha(repo: str, tag: str, token: str) -> str:
    owner, name = repo.split("/", 1)
    # Encode only the tag name; keep the 'tags/' path separator intact (a %2F here
    # would make GitHub treat it as a literal char in one segment and 404).
    encoded_tag = urllib.parse.quote(tag, safe="")
    ref = github_request(f"/repos/{owner}/{name}/git/ref/tags/{encoded_tag}", token)
    if not ref:
        raise RuntimeError(f"tag {tag} not found for repo {repo}")
    obj = ref["object"]
    # Annotated tags reference a tag object; dereference to the commit SHA.
    if obj.get("type") == "tag":
        tag_obj = github_request(
            f"/repos/{owner}/{name}/git/tags/{obj['sha']}", token,
        )
        if not tag_obj:
            raise RuntimeError(f"could not dereference annotated tag {tag} for {repo}")
        return tag_obj["object"]["sha"]
    return obj["sha"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Retag release artifacts using per-repo source tags")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required for commit_map resolution")

    spec = json.loads(args.spec)
    repos = spec.get("repos", [])
    branch = spec["branch"]
    final_tag = spec["tag"]
    source_tag_map = read_source_tags(branch, repos)
    components = {entry["repository"]: entry for entry in load_components()}

    # Only promote components that were actually built this release cycle —
    # those carry an `-rc.N` tag on the release branch. Unchanged components
    # keep their previously-shipped final tag and are left untouched (not
    # retagged, and absent from tag_map so update_values.py won't move them).
    changed_repos = [repo for repo in repos if is_rc_source_tag(source_tag_map[repo])]
    skipped = [repo for repo in repos if repo not in changed_repos]
    if skipped:
        print(
            f"retag: skipping {len(skipped)} unchanged repo(s) (no -rc tag): "
            f"{', '.join(sorted(skipped))}",
            file=sys.stderr,
        )

    # Collect every (image, source_tag, target_tag) push grouped by
    # registry_key. Pushing all images of one registry together means we only
    # `docker login` once per registry, while still using the correct deploy
    # credentials for each.
    copies_by_registry: dict[str, list[tuple[str, str, str]]] = {}
    tag_map: dict[str, str] = {}
    commit_map: dict[str, str] = {}
    for repo in changed_repos:
        component = components[repo]
        source_tag = source_tag_map[repo]
        for image_name, registry_key, tag_suffixes in image_names(component):
            registry = registry_prefix(registry_key)
            image = f"{registry}/{image_name}"
            copies = copies_by_registry.setdefault(registry_key, [])
            copies.append((image, source_tag, final_tag))
            # The chart also references suffixed tags the build publishes (e.g.
            # `<tag>-optimized`), so promote each of those manifests too.
            for suffix in tag_suffixes:
                copies.append(
                    (image, f"{source_tag}{suffix}", f"{final_tag}{suffix}"),
                )
        tag_map[repo] = final_tag
        commit_map[repo] = resolve_tag_sha(repo, source_tag, token)

    for registry_key, copies in copies_by_registry.items():
        registry_login(registry_key, registry_prefix(registry_key))
        for image, source_tag, target_tag in copies:
            copy_tag(image, source_tag, target_tag)

    print(f"tag_map={json.dumps(tag_map, separators=(',', ':'))}")
    print(f"commit_map={json.dumps(commit_map, separators=(',', ':'))}")
    print(f"source_tag_map={json.dumps(source_tag_map, separators=(',', ':'))}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
