#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from _lib import find_component, gh_api, load_components, run

CHART_PATH = "charts/truefoundry/Chart.yaml"
UMBRELLA_VALUES = "charts/truefoundry/values.yaml"
COMPONENTS_PATH = Path("config/components.yml")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")
# Source of truth for "latest shipped prod": the chart's final release git tags.
CHART_TAG_PREFIX = "truefoundry-"


@dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int
    rc: int | None = None

    @property
    def base(self) -> "SemVer":
        return SemVer(self.major, self.minor, self.patch)

    def has_rc_suffix(self) -> bool:
        return self.rc is not None

    def __str__(self) -> str:
        if self.rc is None:
            return f"{self.major}.{self.minor}.{self.patch}"
        return f"{self.major}.{self.minor}.{self.patch}-rc.{self.rc}"


def parse_semver(value: str) -> SemVer:
    match = SEMVER_RE.match(value.strip())
    if not match:
        raise ValueError(f"invalid semver: {value}")
    major, minor, patch, rc = match.groups()
    return SemVer(int(major), int(minor), int(patch), int(rc) if rc else None)


def parse_image_tag(value: str) -> SemVer:
    """Parse an image tag that may carry a leading 'v' (e.g. v0.1.0, 0.1.1-rc.2)."""
    text = value.strip()
    if text.startswith("v"):
        text = text[1:]
    return parse_semver(text)


def _semver_key(version: SemVer) -> tuple[int, int, int, float]:
    """Sort key that ranks a FINAL version (rc=None) above any -rc.N of the same
    base. Using the dataclass order directly would compare None < int on the rc
    field and raise TypeError when mixing finals and RCs, so map None -> +inf."""
    return (
        version.major,
        version.minor,
        version.patch,
        version.rc if version.rc is not None else float("inf"),
    )


def bump_minor(version: SemVer) -> SemVer:
    return SemVer(version.major, version.minor + 1, 0)


def bump_patch(version: SemVer) -> SemVer:
    return SemVer(version.major, version.minor, version.patch + 1)


def read_yaml_at_ref(ref: str, path: str) -> dict[str, Any]:
    if ref.startswith("origin/"):
        branch = ref[len("origin/") :]
        fetch = run(
            ["git", "fetch", "--depth", "1", "origin", branch],
            check=False,
        )
        if fetch.returncode != 0:
            raise RuntimeError(
                f"failed to fetch {ref}: {fetch.stderr.strip() or 'unknown error'}",
            )
        result = run(["git", "show", f"FETCH_HEAD:{path}"])
    else:
        result = run(["git", "show", f"{ref}:{path}"])
    return yaml.safe_load(result.stdout) or {}


def walk_to_tag(root: dict[str, Any], dotted_path: str) -> str:
    current: Any = root
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(f"path '{dotted_path}' not found in values")
        current = current[segment]
    if not isinstance(current, dict) or "tag" not in current:
        raise KeyError(f"no image tag at path '{dotted_path}'")
    return str(current["tag"])


def read_component_image_tag(component: dict[str, Any], ref: str) -> str:
    """Read a component's current image tag from its values file at `ref`.

    Subchart components read their own values file/image path; everything else
    reads the umbrella values.yaml at the component's first helm_subpath."""
    repo = component["repository"]
    subchart = component.get("subchart")
    if subchart:
        values_file = subchart["values_file"]
        image_path = subchart.get("image_path", "image")
        values = read_yaml_at_ref(ref, values_file)
        return walk_to_tag(values, image_path)

    subpath = component.get("helm_subpath")
    subpaths = [subpath] if isinstance(subpath, str) else list(subpath or [])
    if not subpaths:
        raise ValueError(f"component {repo} has neither helm_subpath nor subchart")
    values = read_yaml_at_ref(ref, UMBRELLA_VALUES)
    # update_values writes the computed tag to EVERY subpath of this repo, so the
    # subpaths must already agree. Reading just the first and bumping it could
    # move a diverging sibling backward — fail loud instead (mirrors retag.py's
    # read_source_tags consistency check).
    observed = {walk_to_tag(values, f"{sp}.image") for sp in subpaths}
    if len(observed) != 1:
        raise ValueError(
            f"component {repo} has diverging image tags across subpaths "
            f"{subpaths}: {sorted(observed)}; reconcile them before hotfixing",
        )
    return observed.pop()


def latest_service_line_tag(repo: str, base: SemVer) -> SemVer | None:
    """The service repo's OWN latest `v<base.major>.<base.minor>.<= base.patch>`
    tag, or None when the repo has no such tag.

    Service repos are versioned INDEPENDENTLY of the umbrella chart, and the
    chart's pinned image tag can LAG a service's actual latest shipped tag (e.g.
    the chart at truefoundry-0.152.1 still pins servicefoundryServer v0.152.0
    while the service already shipped v0.152.1). Reading only the chart pin then
    recomputes an already-shipped patch and the build is skipped. Consulting the
    service repo's own tags closes that gap.

    Bounded to the base's minor line AND to ``patch <= base.patch``. The minor
    bound keeps an OLDER-minor hotfix on its own line; the patch bound keeps an
    OLDER-patch hotfix (explicit `base_version` on a minor that mainline has
    since advanced) from leap-frogging onto a LATER mainline tag — e.g. a hotfix
    off base 0.152.0 must yield v0.152.1, never v0.152.6 just because v0.152.5
    shipped later on the same minor. The stale-pin case this corrects only ever
    has the service sitting AT the base line head (service v0.152.1 == base
    0.152.1), never beyond it; an in-progress `target-rc.N` (one patch past base)
    is detected separately from the chart branch pin, so excluding it here is
    safe. Includes -rc tags at-or-below the base patch. Raises on API failure
    rather than silently falling back, so a transient error can't reintroduce
    the stale-pin bug."""
    prefix = f"v{base.major}.{base.minor}."
    payload = gh_api(f"repos/{repo}/git/matching-refs/tags/{prefix}")
    refs = payload or []
    versions: list[SemVer] = []
    for entry in refs:
        ref = entry.get("ref", "")
        if "refs/tags/" not in ref:
            continue
        name = ref.split("refs/tags/", 1)[1]
        try:
            version = parse_image_tag(name)
        except ValueError:
            continue
        # Same minor line and no further than the base patch (see docstring).
        if (version.major, version.minor) != (base.major, base.minor):
            continue
        if version.patch > base.patch:
            continue
        versions.append(version)
    if not versions:
        return None
    return max(versions, key=_semver_key)


def hotfix_repo_plan(
    repos: list[str],
    ref: str,
    rc_n: int | None,
    base: SemVer,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Per-repo hotfix target image tags, the git ref each repo's release branch
    is cut from, AND the per-repo release branch name — all sequential per repo
    and decoupled from the umbrella chart version.

    `ref` is the git ref to read each component's CURRENT image tag from — the
    release branch (`origin/<branch>`) when it already exists, otherwise the
    source the branch will be cut from (main for the latest line, or the
    `truefoundry-<base>` chart tag for an older line).

    `base` is the chart base version; its minor line scopes the lookup of each
    service repo's own latest tag.

    Returns ``(repo_tags, source_refs, repo_branches)``:

    * ``repo_tags[repo]`` — the TARGET tag to build. Each repo's patch advances
      from its OWN current image tag (e.g. v0.1.0 -> v0.1.1) regardless of how
      far ahead the umbrella chart line is. When the repo is already mid-RC-cycle
      (current tag has an -rc suffix) the patch is held and only the shared RC
      counter advances. ``rc_n=None`` means skip_rc (build the final patch
      directly).
    * ``source_refs[repo]`` — ``refs/tags/v<current>``, the repo's OWN current
      shipped tag. Service repos are versioned INDEPENDENTLY of the umbrella
      chart, so a missing release branch must be cut from this tag — NOT a
      chart-version-aligned ``v<chart_base>`` tag, which frequently does not
      exist in the service repo (e.g. the chart is at 0.151.1 while the service
      only ever shipped v0.151.0).
    * ``repo_branches[repo]`` — ``release-v<repo_target>``, the branch the
      service is BUILT on, named after the SERVICE's own target patch (NOT the
      chart's). When the chart line runs ahead of a service (chart 0.152.5 ->
      0.152.6 while the service goes 0.152.0 -> 0.152.1), the service must build
      on its own ``release-v0.152.1`` — the operator-created branch carrying the
      fix — rather than a chart-named ``release-v0.152.6`` cut from the stale
      base. In the common same-patch case this equals the chart branch.

    The per-repo CURRENT is the newer of the chart's pinned tag and the service
    repo's own latest tag on the base's minor line, so a chart pin that lags the
    service (already-shipped patch) no longer recomputes a colliding tag."""
    repo_tags: dict[str, str] = {}
    source_refs: dict[str, str] = {}
    repo_branches: dict[str, str] = {}
    for repo in repos:
        component = find_component(repo)
        chart_pinned = parse_image_tag(read_component_image_tag(component, ref))
        service_latest = latest_service_line_tag(repo, base)
        candidates = [chart_pinned]
        if service_latest is not None:
            candidates.append(service_latest)
        current = max(candidates, key=_semver_key)
        source_refs[repo] = f"refs/tags/v{current}"
        repo_target = current.base if current.has_rc_suffix() else bump_patch(current)
        repo_branches[repo] = f"release-v{repo_target}"
        if rc_n is None:
            repo_tags[repo] = f"v{repo_target}"
        else:
            repo_tags[repo] = f"v{repo_target}-rc.{rc_n}"
    return repo_tags, source_refs, repo_branches


def git_branch_exists(branch: str) -> bool:
    result = run(["git", "ls-remote", "--heads", "origin", branch], check=False)
    return bool(result.stdout.strip())


def read_ref_for_source(source_ref: str) -> str:
    """Convert a fully-qualified create-ref (refs/heads/<b> | refs/tags/<t>)
    into a ref usable by read_yaml_at_ref (origin/<b> for branches, the bare
    tag name for tags)."""
    if source_ref.startswith("refs/heads/"):
        return f"origin/{source_ref[len('refs/heads/'):]}"
    if source_ref.startswith("refs/tags/"):
        return source_ref[len("refs/tags/"):]
    return source_ref


def all_repos() -> list[str]:
    return [entry["repository"] for entry in load_components(COMPONENTS_PATH)]


def latest_shipped_version() -> SemVer:
    """Latest shipped prod version, derived from the chart's final release tags
    (truefoundry-X.Y.Z, excluding -rc). This is the source of truth for version
    math, decoupled from main's Chart.yaml."""
    result = run(
        ["git", "ls-remote", "--tags", "origin", f"{CHART_TAG_PREFIX}*"],
        check=False,
    )
    versions: list[SemVer] = []
    for line in result.stdout.splitlines():
        if "refs/tags/" not in line:
            continue
        ref = line.split("refs/tags/", 1)[1].strip()
        if ref.endswith("^{}") or not ref.startswith(CHART_TAG_PREFIX):
            continue
        candidate = ref[len(CHART_TAG_PREFIX):]
        try:
            version = parse_semver(candidate)
        except ValueError:
            continue
        if not version.has_rc_suffix():
            versions.append(version)
    if not versions:
        raise ValueError(
            f"no shipped chart tags found matching {CHART_TAG_PREFIX}X.Y.Z",
        )
    return max(versions)


def next_rc_number(branch: str, target: SemVer) -> int:
    if not git_branch_exists(branch):
        return 1
    chart = read_yaml_at_ref(f"origin/{branch}", CHART_PATH)
    branch_version = parse_semver(str(chart.get("version", "")))
    if branch_version.has_rc_suffix() and branch_version.base == target:
        return int(branch_version.rc or 0) + 1
    # The branch is already at a FINAL version at-or-beyond the target we are
    # about to RC. Producing `target-rc.1` here would downgrade an
    # already-promoted line — this happens when the chart's final git tag lags
    # the branch (e.g. a publish step failed after the branch was promoted).
    # Fail loud rather than silently shipping a lower version.
    if not branch_version.has_rc_suffix() and branch_version.base >= target:
        raise ValueError(
            f"branch {branch} is already at final {branch_version} "
            f"(>= target {target}); refusing to create {target}-rc.1 which would "
            f"downgrade it. The chart's truefoundry-X.Y.Z release tag is likely "
            f"lagging the branch (e.g. a failed publish); reconcile the tag first.",
        )
    return 1


def parse_repositories(raw: str) -> list[str]:
    if not raw.strip():
        return []
    repos = json.loads(raw)
    if not isinstance(repos, list):
        raise ValueError("repositories must be a JSON array")
    normalized = [str(repo).strip() for repo in repos if str(repo).strip()]
    if len(normalized) != len(set(normalized)):
        raise ValueError("repositories contains duplicates")
    return normalized


def compute_release(
    kind: str,
    repos_input: list[str],
    base_version_input: str,
    skip_rc: bool = False,
    chart_only: bool = False,
) -> dict[str, Any]:
    if chart_only and kind != "hotfix":
        raise ValueError("chart_only is only supported for kind=hotfix")
    # Latest shipped prod comes from the chart's final release tags, not main.
    latest_shipped = latest_shipped_version()
    latest_line = bump_minor(latest_shipped)
    latest_line_branch = f"release-v{latest_line.major}.{latest_line.minor}.0"

    all_component_repos = all_repos()
    base_version = parse_semver(base_version_input) if base_version_input else None

    if kind == "rc":
        target = bump_minor(latest_shipped)
        branch = f"release-v{target.major}.{target.minor}.0"
        rc_n = next_rc_number(branch, target)
        if repos_input:
            repos = repos_input
        elif rc_n == 1:
            repos = all_component_repos
        else:
            raise ValueError("continuing RC requires repositories input")
        return {
            "kind": "rc",
            "target_version": str(target),
            "chart_version": f"{target}-rc.{rc_n}",
            "tag": f"v{target}-rc.{rc_n}",
            "branch": branch,
            # Missing service-repo release branches are cut from main for rc.
            "branch_source_ref": "refs/heads/main",
            "base": str(latest_shipped),
            "repos": repos,
            "mode": "build",
            "fast_forward_main": False,
            "is_latest_line": branch == latest_line_branch,
            "chart_only": False,
        }

    if kind == "prod":
        if base_version:
            if base_version.has_rc_suffix():
                raise ValueError("base_version for prod must not have -rc suffix")
            # Resolve the FULL version branch so we can promote RCs that live on
            # a per-patch hotfix branch (release-v0.147.2) as well as mainline
            # lines (release-v0.149.0). For a mainline line this is identical to
            # the old release-v<minor>.0 since its patch is 0.
            branch = f"release-v{base_version}"
            base = base_version
        else:
            base = latest_shipped
            candidate = bump_minor(latest_shipped)
            branch = f"release-v{candidate.major}.{candidate.minor}.0"

        if not git_branch_exists(branch):
            raise ValueError(f"target release branch {branch} does not exist")

        branch_chart = read_yaml_at_ref(f"origin/{branch}", CHART_PATH)
        branch_version = parse_semver(str(branch_chart.get("version", "")))
        if not branch_version.has_rc_suffix():
            raise ValueError(
                f"branch {branch} is not at an RC version (found {branch_version})",
            )
        target = branch_version.base
        return {
            "kind": "prod",
            "target_version": str(target),
            "chart_version": str(target),
            "tag": f"v{target}",
            "source_rc_tag": f"v{branch_version}",
            "branch": branch,
            "base": str(base),
            "repos": all_component_repos,
            "mode": "retag",
            "fast_forward_main": target >= latest_shipped,
            "is_latest_line": branch == latest_line_branch,
            "chart_only": False,
        }

    if kind == "hotfix":
        base = base_version or latest_shipped
        if base.has_rc_suffix():
            raise ValueError("base_version for hotfix must not have -rc suffix")
        if chart_only:
            # Chart-only: bump ONLY the umbrella chart (templates/version) —
            # no service is rebuilt or retagged. Used for template-only fixes
            # that don't touch any service image, so `repositories` must be
            # empty (a chart-only run that also lists repos would silently
            # rebuild them, defeating the point of the distinction).
            if repos_input:
                raise ValueError("chart_only hotfix must not specify repositories")
        elif not repos_input:
            raise ValueError("hotfix requires repositories input")
        target = bump_patch(base)
        # Per-PATCH hotfix branch, named after the TARGET it builds (e.g. base
        # 0.148.4 -> target 0.148.5 -> release-v0.148.5). Naming it after the
        # target keeps it consistent with mainline (where the branch is named
        # after the version it ships) so `prod` can promote the RC from
        # release-v<target> by the same release-v<base_version> rule.
        branch = f"release-v{target}"
        branch_exists = git_branch_exists(branch)

        # A hotfix that extends the current line (target >= latest shipped) is
        # promoted into main at the end (fast_forward_main), so it deploys to the
        # latest-line clusters too.
        on_latest_line = target >= latest_shipped

        # CHART branch source: a LATEST-LINE hotfix cuts from main — main is
        # fast-forwarded (squash) on every latest-line prod, so it already
        # holds this base version's chart files as a single commit. Cutting
        # from main instead of the truefoundry-<base> tag keeps the
        # merge-base with main close, so the eventual "promote to main" PR
        # lists only this hotfix's own commit(s), not the entire unsquashed
        # RC/hotfix lineage the tag sits on (that tag points at the release
        # branch's own tip, not the squash commit — see
        # release-helm-update.yml's promote step).
        #
        # main is only a valid source when it actually REFLECTS the base:
        # latest_shipped comes from git tags, and main can lag the tag when
        # a promote failed/is pending (or drifted). Cutting from a lagging
        # main would give the hotfix a pre-<base> chart baseline (stale
        # templates/values), so verify main's own Chart.yaml is at the base
        # version and fall back to the shipped truefoundry-<base> tag when
        # it isn't — correct content at the cost of a noisier promote PR.
        #
        # An OLDER-LINE hotfix never promotes to main, so its chart branch is
        # still cut from the previously-shipped chart tag, isolating the fix
        # from anything newer that has landed on main.
        chart_branch_source_ref = f"refs/tags/{CHART_TAG_PREFIX}{base}"
        if on_latest_line:
            try:
                main_chart = read_yaml_at_ref("origin/main", CHART_PATH)
                main_version = parse_semver(str(main_chart.get("version", "")))
            except (ValueError, RuntimeError):
                main_version = None
            if main_version == base:
                chart_branch_source_ref = "refs/heads/main"
            else:
                print(
                    f"warning: main is at {main_version or 'unreadable'} but the "
                    f"hotfix base is {base}; cutting the chart branch from "
                    f"refs/tags/{CHART_TAG_PREFIX}{base} instead of main so the "
                    "branch baseline matches what actually shipped.",
                    file=sys.stderr,
                )
        # Coarse fallback / RC parity. Service repos are versioned independently
        # of the chart, so each one's release branch is cut from its OWN current
        # shipped tag (computed per-repo into branch_source_refs below).
        branch_source_ref = f"refs/tags/v{base}"

        # Read each repo's current image tag from the per-patch branch when it
        # already exists (a prior hotfix on this base), otherwise from the shipped
        # truefoundry-<base> chart the branch will be cut from.
        chart_read_ref = (
            f"origin/{branch}"
            if branch_exists
            else read_ref_for_source(chart_branch_source_ref)
        )

        common = {
            "kind": "hotfix",
            # CHART release branch (named after the chart target). Used for the
            # chart PR in helm-charts; the per-SERVICE build branches live in
            # repo_branches and may differ when chart/service lines diverge.
            "branch": branch,
            "branch_source_ref": branch_source_ref,
            "chart_branch_source_ref": chart_branch_source_ref,
            "base": str(base),
            "repos": repos_input,
            "mode": "build",
            "is_latest_line": branch == latest_line_branch,
            # Deploy gate for the develop/prod clusters (which track the latest
            # line). True iff this patch is at-or-ahead-of the latest shipped
            # final — i.e. a hotfix on the current line, NOT an older line whose
            # fix belongs only on its own customer cluster. Set for BOTH the RC
            # and the final stage (unlike fast_forward_main, which is the final-
            # only "advance main" signal). release-start.yml uses this to let a
            # latest-line hotfix RC reach develop and a latest-line hotfix final
            # reach develop+prod, while skipping cluster deploys for older lines.
            "on_latest_line": on_latest_line,
            "chart_only": chart_only,
        }
        if skip_rc:
            # Build the final patch directly (no RC stage). Advances main only when
            # this patch is at-or-ahead-of the current latest line (same rule as prod).
            # Service image tags are sequential PER REPO (decoupled from the
            # umbrella chart version); the chart still bumps its own line patch.
            repo_tags, branch_source_refs, repo_branches = hotfix_repo_plan(
                repos_input, chart_read_ref, rc_n=None, base=base,
            )
            return {
                **common,
                "target_version": str(target),
                "chart_version": str(target),
                "tag": f"v{target}",
                "repo_tags": repo_tags,
                "branch_source_refs": branch_source_refs,
                "repo_branches": repo_branches,
                "fast_forward_main": target >= latest_shipped,
            }
        rc_n = next_rc_number(branch, target)
        repo_tags, branch_source_refs, repo_branches = hotfix_repo_plan(
            repos_input, chart_read_ref, rc_n=rc_n, base=base,
        )
        return {
            **common,
            "target_version": str(target),
            "chart_version": f"{target}-rc.{rc_n}",
            "tag": f"v{target}-rc.{rc_n}",
            "repo_tags": repo_tags,
            "branch_source_refs": branch_source_refs,
            "repo_branches": repo_branches,
            "fast_forward_main": False,
        }

    raise ValueError(f"unsupported kind: {kind}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute release orchestration spec")
    parser.add_argument("--kind", required=True, choices=["rc", "prod", "hotfix"])
    parser.add_argument("--repositories", required=False, default="[]")
    parser.add_argument("--base-version", required=False, default="")
    parser.add_argument("--skip-rc", action="store_true")
    parser.add_argument("--chart-only", action="store_true")
    args = parser.parse_args()

    repos_input = parse_repositories(args.repositories)
    spec = compute_release(
        args.kind, repos_input, args.base_version.strip(),
        skip_rc=args.skip_rc, chart_only=args.chart_only,
    )

    print(f"spec={json.dumps(spec, separators=(',', ':'))}")
    print(f"target_version={spec['target_version']}")
    print(f"branch={spec['branch']}")
    print(f"mode={spec['mode']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
