#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from _lib import run

# Matches a GitHub pull-request URL inside auto-generated release notes, e.g.
# "* Fix the thing by @alice in https://github.com/truefoundry/sfy-server/pull/123".
# GitHub's generate-notes body lists one such line per PR merged since the
# previous tag, so this is our source of "what PRs went into this release".
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


def is_prerelease_version(value: str) -> bool:
    """A version/tag is a pre-release iff it carries an `-rc.` suffix.

    We key off the actual tag rather than the run `kind` so the GitHub release
    flag is always correct: rc tags (vX.Y.Z-rc.N, incl. hotfix RCs) are marked
    pre-release, while final tags (vX.Y.Z — prod promotions AND direct
    skip-rc hotfix finals) get a full "latest" release.
    """
    return "-rc." in (value or "")


def resolve_branch_sha(repo: str, branch: str) -> str:
    result = run(
        ["gh", "api", f"repos/{repo}/git/ref/heads/{branch}"],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to resolve branch {branch} on {repo}: "
            f"{result.stderr.strip() or 'unknown error'}",
        )
    payload = json.loads(result.stdout)
    return payload["object"]["sha"]


def tag_ref_sha(repo: str, tag: str) -> str | None:
    result = run(
        ["gh", "api", f"repos/{repo}/git/ref/tags/{tag}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    obj = json.loads(result.stdout)["object"]
    # Annotated tags point at a tag object; dereference to the underlying commit
    # so idempotency comparisons use real commit SHAs.
    if obj.get("type") == "tag":
        tag_obj = json.loads(
            run(["gh", "api", f"repos/{repo}/git/tags/{obj['sha']}"]).stdout,
        )
        return tag_obj["object"]["sha"]
    return obj["sha"]


def ensure_tag(repo: str, tag: str, sha: str) -> None:
    """Create `tag` at `sha` if absent; no-op if already at the same sha; warn
    and reuse the existing tag if it points at a different sha.

    B6: we deliberately do NOT raise on a sha mismatch. Re-runs after a partial
    failure may compute a slightly-different commit (e.g. branch advanced), but
    the tag already in the registry is the source of truth for what shipped.
    Moving the tag would un-pin already-published artifacts, so warn and leave
    it alone; the caller will use the existing tag's sha for ensure_release."""
    current = tag_ref_sha(repo, tag)
    if current == sha:
        return
    if current:
        print(
            f"::warning::tag {tag} on {repo} already exists at {current} "
            f"(expected {sha}); reusing existing tag.",
            file=sys.stderr,
        )
        return
    run(
        [
            "gh",
            "api",
            f"repos/{repo}/git/refs",
            "--method",
            "POST",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={sha}",
        ],
    )


def generate_notes(
    repo: str,
    tag: str,
    sha: str,
    previous_tag: str | None = None,
) -> str:
    command = [
        "gh",
        "api",
        f"repos/{repo}/releases/generate-notes",
        "--method",
        "POST",
        "-f",
        f"tag_name={tag}",
        "-f",
        f"target_commitish={sha}",
    ]
    # Pin the diff base so the "Full Changelog" range is deterministic
    # (previous shipped version → this one). Without it GitHub guesses the
    # previous tag and can pick a far-back release (e.g. v0.145.0 instead of
    # v0.149.0). Only pass it when the tag actually exists on the repo —
    # generate-notes 422s on an unknown previous_tag_name — otherwise fall back
    # to GitHub's auto selection.
    if previous_tag:
        command += ["-f", f"previous_tag_name={previous_tag}"]
    result = run(command)
    payload = json.loads(result.stdout)
    return payload.get("body", "")


def _prior_patch_tag(version: str) -> str | None:
    """Final tag for the previous patch on the same minor line, if any."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    patch = int(parts[2])
    if patch == 0:
        return None
    return f"v{parts[0]}.{parts[1]}.{patch - 1}"


_FINAL_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_RC_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)-rc\.\d+$")


def _semver_tuple(tag: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) for a final OR rc tag; None if unparseable."""
    m = _FINAL_TAG_RE.match(tag or "") or _RC_TAG_RE.match(tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def latest_prior_final_tag(repo: str, tag: str) -> str | None:
    """The repo's OWN most-recent FINAL tag strictly older than `tag`.

    The umbrella base (`v{base}`) isn't tagged on every repo — a component that
    didn't change at that version (e.g. servicefoundry-server has no v0.152.2 if
    the 0.152.2 hotfix didn't touch it) has no such tag, so the v{base} lookup
    fails and GitHub would auto-pick a wrong far-back ancestor. Fall back to
    whatever this repo actually last shipped, from its own tag list.
    """
    cur = _semver_tuple(tag)
    if cur is None:
        return None
    try:
        result = run(
            ["gh", "api", f"repos/{repo}/tags", "--paginate", "--jq", ".[].name"],
            check=False,
        )
    except Exception:  # noqa: BLE001 — gh missing / not runnable: no fallback available
        return None
    if result.returncode != 0:
        return None
    best: tuple[int, int, int] | None = None
    best_tag: str | None = None
    for line in result.stdout.splitlines():
        name = line.strip()
        m = _FINAL_TAG_RE.match(name)  # finals only (skip rc)
        if not m:
            continue
        candidate = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if candidate < cur and (best is None or candidate > best):
            best, best_tag = candidate, name
    return best_tag


def resolve_previous_tag(spec: dict, repo: str, tag: str) -> str | None:
    """The tag to diff this release's notes against.

    Preference order:
      1. The umbrella `base` (`v{base}`) when it exists ON THIS REPO — for prod
         with an explicit `base_version` (base == tag) use the prior patch instead.
      2. The repo's OWN latest prior final tag (covers hotfixes, and the common
         case where the umbrella base version isn't tagged on this repo).

    Must run before `ensure_tag` creates the release tag, and must never return
    the same name as `tag`. Returns None only when no predecessor exists at all,
    so the caller lets GitHub auto-pick.
    """
    base = spec.get("base")
    candidates: list[str] = []
    if spec.get("kind") in ("rc", "prod") and base:
        base_tag = f"v{base}"
        if base_tag != tag:
            candidates.append(base_tag)
        elif spec.get("kind") == "prod":
            prior = _prior_patch_tag(base)
            if prior:
                candidates.append(prior)

    for candidate in candidates:
        if candidate != tag and tag_ref_sha(repo, candidate):
            return candidate

    # Fallback: this repo's actual previous final. Fixes the case (e.g. v0.153.0)
    # where v{base} isn't tagged on the repo, and gives hotfixes a base too.
    return latest_prior_final_tag(repo, tag)


def extract_pull_requests(notes: str) -> list[str]:
    """Pull unique PR URLs out of a generate-notes body, ordered by PR number.

    De-duplicates (a PR can be referenced more than once) and sorts numerically
    so the stored list is stable across re-runs of the same release.
    """
    urls = set(PR_URL_RE.findall(notes or ""))

    def pr_number(url: str) -> int:
        return int(url.rsplit("/", 1)[-1])

    return sorted(urls, key=pr_number)


def emit_output(key: str, value: str) -> None:
    """Append a key=value line to $GITHUB_OUTPUT when running inside Actions.

    Values are single-line (the PR list is compact JSON), so the simple
    `key=value` form is safe. Outside Actions this is a no-op so the script
    stays usable locally / in tests.
    """
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def ensure_release(
    repo: str,
    tag: str,
    sha: str,
    prerelease: bool,
    notes: str,
    latest: bool | None = None,
) -> None:
    """Create/refresh the GitHub Release for `tag`.

    `latest` controls the repo's "Latest" badge: GitHub marks the newest-
    CREATED release as latest by default, so an older-line hotfix (e.g.
    v0.140.6 shipped after v0.148.0) would steal the badge from the actual
    newest version. Callers pass fast_forward_main (true only when this
    release is at/above the latest shipped line). None leaves gh's default
    untouched; the flag is never sent for prereleases (GitHub forbids
    marking a prerelease as latest).
    """
    latest_flags: list[str] = []
    if latest is not None and not prerelease:
        latest_flags = [f"--latest={'true' if latest else 'false'}"]

    exists = run(["gh", "release", "view", tag, "--repo", repo], check=False)
    if exists.returncode == 0:
        run(
            [
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                repo,
                "--target",
                sha,
                "--notes",
                notes,
                # gh treats --prerelease as a boolean flag; the value must be
                # attached with '=' or '--prerelease false' is parsed as true.
                f"--prerelease={'true' if prerelease else 'false'}",
                *latest_flags,
            ],
        )
        return

    command = [
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repo,
        "--target",
        sha,
        "--title",
        tag,
        "--notes",
        notes,
        *latest_flags,
    ]
    if prerelease:
        command.append("--prerelease")
    run(command)


def subcommand_per_service(args: argparse.Namespace) -> int:
    spec = json.loads(args.spec)
    tag_map = json.loads(args.tag_map)
    commit_map = json.loads(args.commit_map)
    repo = args.repo

    tag = tag_map.get(repo)
    sha = commit_map.get(repo)

    if not tag:
        # Truly unchanged repos (excluded from tag_map entirely): nothing to do.
        print(f"skip: {repo} not in update set (unchanged); no tag/release created.")
        return 0

    if not sha:
        # M3: dispatch_build emits `already_tagged` (publish=false), so the per-leg
        # records the tag in tag_map but NOT in commit_map. Resolve the tag's
        # actual commit from the remote so we can still ensure the GitHub Release
        # exists (it may have failed mid-flight on a previous attempt).
        resolved = tag_ref_sha(repo, tag)
        if not resolved:
            print(
                f"skip: {repo} tag {tag} in tag_map but commit_map missing AND "
                f"tag does not exist on the remote; nothing to publish.",
            )
            return 0
        print(
            f"info: {repo} commit_sha missing in commit_map; reusing existing "
            f"tag {tag} -> {resolved}.",
        )
        sha = resolved

    # Pre-release iff the tag itself is an RC (vX.Y.Z-rc.N). Final tags —
    # prod promotions and direct skip-rc hotfix finals — get a full release.
    prerelease = is_prerelease_version(tag)
    previous_tag = resolve_previous_tag(spec, repo, tag)
    notes = generate_notes(repo, tag, sha, previous_tag=previous_tag)
    ensure_tag(repo, tag, sha)
    # Only a latest-line final may take the repo's "Latest" badge; an
    # older-line hotfix final must not steal it from the newest version.
    ensure_release(
        repo, tag, sha, prerelease, notes,
        latest=bool(spec.get("fast_forward_main")),
    )

    # Surface the PRs that went into this service's release + the release URL so
    # the workflow can forward them to release-app (stored in
    # truefoundry_release_component_map). PRs come from the same generate-notes
    # body — no extra API call.
    pull_requests = extract_pull_requests(notes)
    release_url = f"https://github.com/{repo}/releases/tag/{tag}"
    emit_output("github_release_url", release_url)
    emit_output("pull_requests", json.dumps(pull_requests))
    print(
        f"published {repo} {tag}: {len(pull_requests)} PR(s) -> {release_url}",
    )
    return 0


def subcommand_helm_charts(args: argparse.Namespace) -> int:
    spec = json.loads(args.spec)
    tag_map = json.loads(args.tag_map)
    commit_map = json.loads(args.commit_map)

    repo = args.repository
    # The chart's release tag uses the truefoundry-X.Y.Z namespace, which is the
    # source of truth for "latest shipped prod" in compute_release.
    tag = f"truefoundry-{spec['chart_version']}"
    branch = spec["branch"]
    sha = resolve_branch_sha(repo, branch)
    ensure_tag(repo, tag, sha)

    lines = [f"Truefoundry chart {tag}", "", "## Component tags"]
    for component_repo in sorted(tag_map):
        lines.append(f"- {component_repo}: {tag_map[component_repo]}")
    lines.append("")
    lines.append("## Component release SHAs")
    for component_repo in sorted(commit_map):
        lines.append(f"- {component_repo}: {commit_map[component_repo]}")
    notes = "\n".join(lines)
    # Chart tag is truefoundry-X.Y.Z[-rc.N]; pre-release iff it carries -rc.
    prerelease = is_prerelease_version(tag)
    # Same "Latest"-badge rule as per-service releases: only latest-line finals.
    ensure_release(
        repo, tag, sha, prerelease, notes,
        latest=bool(spec.get("fast_forward_main")),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create service and helm release tags/releases")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    per_service = subparsers.add_parser("per-service")
    per_service.add_argument("--spec", required=True)
    per_service.add_argument("--tag-map", required=True)
    per_service.add_argument("--commit-map", required=True)
    per_service.add_argument("--repo", required=True)
    per_service.set_defaults(handler=subcommand_per_service)

    helm = subparsers.add_parser("helm-charts")
    helm.add_argument("--spec", required=True)
    helm.add_argument("--tag-map", required=True)
    helm.add_argument("--commit-map", required=True)
    # Default to the repo this workflow runs in (works in forks/sandboxes);
    # prod value as the non-Actions fallback.
    helm.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "truefoundry/helm-charts"),
    )
    helm.set_defaults(handler=subcommand_helm_charts)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
